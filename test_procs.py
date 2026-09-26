"""Tests for ccwho_procs: the pure rules about agents' processes and sessions.

Every input is built here, byte for byte where it matters, so nothing depends on
what this machine is running. The fake token below must never come back out.
"""
import struct
import unittest

import ccwho_procs as procs

TOKEN = "sk-ant-FAKE-0000-must-never-leave-the-read"
SID = "51fddd61-822b-49e0-9aeb-2145e91e1244"


def procargs(argv, env, exec_path="/usr/local/bin/node", pad=3):
    """A KERN_PROCARGS2 buffer: argc, exec path, NUL padding, argv, envp."""
    out = struct.pack("i", len(argv)) + exec_path.encode() + b"\0" * (1 + pad)
    for a in argv:
        out += a.encode() + b"\0"
    for e in env:
        out += e.encode() + b"\0"
    return out + b"\0"


class TestParseProcargs(unittest.TestCase):
    """The environment is read for a handful of named keys and nothing else.
    It holds secrets, and ccwho's output is read by LLMs."""

    ENV = [f"CLAUDE_CODE_SESSION_ID={SID}", "CLAUDE_CONFIG_DIR=/Users/x/.claude-work",
           f"ANTHROPIC_API_KEY={TOKEN}", f"CLAUDE_CODE_OAUTH_TOKEN={TOKEN}",
           "PATH=/usr/bin:/bin", "HOME=/Users/x"]

    def test_only_allowlisted_keys_come_back(self):
        got = procs.parse_procargs(procargs(["node", "server.js"], self.ENV))
        self.assertEqual(got, {"CLAUDE_CODE_SESSION_ID": SID,
                               "CLAUDE_CONFIG_DIR": "/Users/x/.claude-work"})

    def test_the_token_is_nowhere_in_the_result(self):
        got = procs.parse_procargs(procargs(["node"], self.ENV))
        self.assertNotIn(TOKEN, repr(got))
        self.assertNotIn(TOKEN, str(got))

    def test_the_token_is_nowhere_in_an_error(self):
        # a malformed buffer that still carries the token: whatever happens, the
        # token does not travel in an exception message either
        bad = b"\xff\xff\xff\xff" + f"ANTHROPIC_API_KEY={TOKEN}\0".encode()
        try:
            got = procs.parse_procargs(bad)
        except Exception as ex:                     # pragma: no cover - must not raise
            self.fail(f"raised instead of answering None: {type(ex).__name__}")
        self.assertIsNone(got)

    def test_the_starting_claude_pid_comes_back(self):
        # review 4 #4: which claude process started it - a number, nothing else
        got = procs.parse_procargs(procargs(["node"], self.ENV + ["CLAUDE_PID=4242"]))
        self.assertEqual(got.get("CLAUDE_PID"), "4242")

    def test_every_allowed_key(self):
        env = [f"CODEX_THREAD_ID=01a06435-aaaa", "CODEX_HOME=/Users/x/.codex-a",
               f"CLAUDE_CODE_SESSION_ID={SID}", "CLAUDE_CONFIG_DIR=/c"]
        got = procs.parse_procargs(procargs(["codex"], env))
        self.assertEqual(set(got), {"CODEX_THREAD_ID", "CODEX_HOME",
                                    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CONFIG_DIR"})

    def test_no_marker_is_an_empty_answer_not_none(self):          # control
        got = procs.parse_procargs(procargs(["zsh"], ["PATH=/bin", "HOME=/x"]))
        self.assertEqual(got, {})

    def test_an_empty_argv0_does_not_eat_the_first_env_entry(self):
        # an empty argument is an empty string, not padding
        got = procs.parse_procargs(procargs(["", "x"], [f"CLAUDE_CODE_SESSION_ID={SID}"]))
        self.assertEqual(got.get("CLAUDE_CODE_SESSION_ID"), SID)

    def test_a_process_that_rewrote_its_title_is_still_read(self):
        # libuv's process.title overwrites argv in place; the kernel keeps the
        # old argc, so walking argc strings lands somewhere in the environment
        title = "npm exec mcp HOME=/x PATH=/p"
        buf = struct.pack("i", 4) + b"/usr/local/bin/node\0\0\0" + title.encode() \
            + b"\0" * 20 + f"CLAUDE_CODE_SESSION_ID={SID}\0".encode() + b"\0"
        self.assertEqual(procs.parse_procargs(buf).get("CLAUDE_CODE_SESSION_ID"), SID)

    def test_a_key_inside_an_argument_is_not_a_key(self):
        # `echo CLAUDE_CODE_SESSION_ID=x` in argv is not the process's environment...
        # but a rewritten title can put env text anywhere, so an exact KEY=value
        # string still counts; only a string that merely CONTAINS it does not
        got = procs.parse_procargs(procargs(["echo", f"see CLAUDE_CODE_SESSION_ID={SID}"],
                                            []))
        self.assertEqual(got, {})

    def test_malformed_buffers_are_unreadable(self):
        for name, buf in (("empty", b""), ("short", b"\x01\x00"),
                          ("negative argc", struct.pack("i", -1) + b"/bin/x\0"),
                          ("no NUL after the path", struct.pack("i", 1) + b"/bin/x")):
            with self.subTest(case=name):
                self.assertIsNone(procs.parse_procargs(buf))

    def test_a_caller_cannot_widen_the_allowlist(self):
        # the allowlist is the contract, not a default: no argument reopens it
        buf = procargs(["node"], self.ENV)
        with self.assertRaises(TypeError):
            procs.parse_procargs(buf, keys=("ANTHROPIC_API_KEY",))


class TestConfigDirs(unittest.TestCase):
    def test_running_sessions_add_their_own_config_dirs(self):
        got = procs.config_dirs(["/Users/x/.claude-work", "/Users/x/.claude"],
                                ["/Users/x/.claude", "/Users/x/.claude-old"])
        self.assertEqual(got, ["/Users/x/.claude", "/Users/x/.claude-old",
                               "/Users/x/.claude-work"])

    def test_empty_and_duplicate_dirs_are_dropped(self):
        self.assertEqual(procs.config_dirs(["", None, "/a", "/a"], ["/a"]), ["/a"])

    def test_a_relative_dir_from_a_process_is_dropped(self):
        # it would resolve against ccwho's own cwd, not the session's
        self.assertEqual(procs.config_dirs(["work", "~/x", "/abs"], []), ["/abs"])

    def test_a_dir_with_control_characters_is_dropped(self):
        # it is printed in rows, attach and resume lines, and saved manifests
        self.assertEqual(procs.config_dirs(["/tmp/a\x1b[31m", "/tmp/b\n", "/ok"], []),
                         ["/ok"])

    def test_a_trailing_slash_is_the_same_dir(self):
        self.assertEqual(procs.config_dirs(["/a/"], ["/a"]), ["/a"])


def entry(pid, sid, start="Tue Sep 22 13:45:15 2026", **kw):
    d = {"pid": pid, "sessionId": sid, "cwd": "/Users/x/p/app", "startedAt": 1790084716216,
         "procStart": start, "kind": "interactive", "name": "app-4e", "status": "idle"}
    d.update(kw)
    return d


class TestLiveSessionFiles(unittest.TestCase):
    """A session file outlives a crash. It counts only while its pid is alive AND
    is still the same process - a reused pid is somebody else."""

    STARTS = {101: "Tue Sep 22 13:45:15 2026", 102: "Wed Sep 23 09:00:00 2026"}

    def test_a_live_session_is_kept(self):
        got = procs.live_session_files([entry(101, SID)], self.STARTS)
        self.assertEqual([r["sessionId"] for r in got], [SID])

    def test_a_dead_pid_is_dropped(self):
        self.assertEqual(procs.live_session_files([entry(999, SID)], self.STARTS), [])

    def test_a_reused_pid_is_dropped(self):
        # pid 102 is alive, but it started a day later: not this session
        stale = entry(102, SID, start="Tue Sep 22 13:45:15 2026")
        self.assertEqual(procs.live_session_files([stale], self.STARTS), [])

    def test_ps_spacing_does_not_matter(self):
        got = procs.live_session_files([entry(101, SID, start="Tue Sep 22  13:45:15 2026")],
                                       {101: " Tue Sep 22 13:45:15 2026  "})
        self.assertEqual(len(got), 1)

    def test_who_started_it_is_kept(self):
        row = procs.live_session_files([entry(101, SID, entrypoint="sdk-cli")],
                                       self.STARTS)[0]
        self.assertEqual(row["entrypoint"], "sdk-cli")

    def test_rows_come_out_in_the_agents_shape(self):
        bg = entry(101, SID, kind="bg", jobId="51fddd61")
        row = procs.live_session_files([bg], self.STARTS)[0]
        self.assertEqual(row["kind"], "background")
        self.assertEqual(row["id"], "51fddd61")
        for k in ("pid", "sessionId", "cwd", "startedAt", "name", "status"):
            self.assertIn(k, row)

    def test_a_file_without_a_session_id_is_skipped(self):
        self.assertEqual(procs.live_session_files([{"pid": 101}], self.STARTS), [])

    def test_a_file_without_a_start_time_is_not_trusted(self):
        # no start time means no way to tell a reused pid from the session: a
        # file that cannot prove it is still the same process does not count
        for start in (None, ""):
            with self.subTest(start=start):
                e = entry(101, SID)
                e["procStart"] = start
                self.assertEqual(procs.live_session_files([e], self.STARTS), [])

    def test_wrong_types_are_dropped_not_fatal(self):
        # these files come from dirs other processes name; one bad one must
        # not take the whole list (and doctor) down with it
        bad = [entry(101, ["x"]), entry(101, {"a": 1}), entry([1], SID),
               entry(True, SID), entry(101, SID, start=5), entry("101", SID)]
        for e in bad:
            with self.subTest(entry=e):
                rows = procs.live_session_files([e], {1: "x", 101: self.STARTS[101]})
                self.assertEqual(rows, [])
                procs.merge_sessions([{"sessionId": "b"}], rows)      # no raise

    def test_a_session_id_must_be_a_uuid(self):
        # it becomes a glob pattern and a shell argument further on
        for sid in ("*", "../../x", "x; touch /tmp/p", SID.upper() + "; ls"):
            with self.subTest(sid=sid):
                self.assertEqual(procs.live_session_files([entry(101, sid)],
                                                          self.STARTS), [])

    def test_control_characters_in_text_are_a_problem(self):
        # name and cwd reach the terminal: an escape sequence could retitle the
        # window or repaint the list
        for field in ("name", "cwd"):
            with self.subTest(field=field):
                e = dict(entry(101, SID), **{field: "x\x1b]0;pwned\x07"})
                self.assertTrue(procs.entry_problem(e))

    def test_ordinary_text_is_fine(self):                            # control
        e = dict(entry(101, SID), name="fix the navbar — café", cwd="/Users/x/p/a b")
        self.assertIsNone(procs.entry_problem(e))

    def test_a_trailing_newline_is_not_a_session_id(self):
        # `$` matches before a final newline; the id must end where it ends
        self.assertTrue(procs.entry_problem(entry(101, SID + "\n")))

    def test_a_valid_entry_has_no_problem(self):                      # control
        self.assertIsNone(procs.entry_problem(entry(101, SID)))
        self.assertTrue(procs.entry_problem(entry(101, "*")))

    def test_the_row_remembers_its_config_dir(self):
        # its transcript lives under that dir, not under ~/.claude
        e = dict(entry(101, SID), configDir="/Users/x/.claude-work")
        self.assertEqual(procs.live_session_files([e], self.STARTS)[0]["configDir"],
                         "/Users/x/.claude-work")

    def test_shell_reads_as_busy_as_the_agents_feed_says(self):
        # measured: a session running a shell command is `shell` in its file and
        # `busy` in `claude agents --json`
        row = procs.live_session_files([entry(101, SID, status="shell")], self.STARTS)[0]
        self.assertEqual(row["status"], "busy")

    def test_other_statuses_pass_through(self):                      # control
        for st in ("idle", "busy", "waiting"):
            with self.subTest(status=st):
                row = procs.live_session_files([entry(101, SID, status=st)],
                                               self.STARTS)[0]
                self.assertEqual(row["status"], st)

    def test_waiting_for_is_kept_and_checked(self):
        row = procs.live_session_files([entry(101, SID, waitingFor="input needed")],
                                       self.STARTS)[0]
        self.assertEqual(row["waitingFor"], "input needed")
        self.assertTrue(procs.entry_problem(entry(101, SID, waitingFor="x\x1b[2J")))

    def test_missing_fields_are_left_out_not_none(self):
        e = {"pid": 101, "sessionId": SID, "procStart": self.STARTS[101]}
        row = procs.live_session_files([e], self.STARTS)[0]
        self.assertNotIn("status", row)
        self.assertNotIn("name", row)

    def test_the_same_session_twice_is_one_row(self):
        # two roots can name one dir (a symlink, a trailing slash)
        got = procs.live_session_files([entry(101, SID), entry(101, SID)], self.STARTS)
        self.assertEqual(len(got), 1)


class TestOnlyClaudeProcessesVouchForASession(unittest.TestCase):
    """A pid and its start time are public (`ps`). A file that names a live
    shell's pid and start is not a session: only a claude process is."""

    def test_a_live_non_claude_pid_does_not_count(self):
        table = {101: ("Tue Sep 22 13:45:15 2026", "/bin/bash"),
                 102: ("Tue Sep 22 13:45:15 2026", "claude")}
        got = procs.live_session_files([entry(101, SID), entry(102, SID[:-1] + "0")],
                                       procs.claude_starts(table))
        self.assertEqual([r["pid"] for r in got], [102])


class TestMergeSessions(unittest.TestCase):
    A = {"pid": 1, "sessionId": "aaa", "name": "from-agents", "status": "busy"}
    B = {"pid": 2, "sessionId": "bbb", "name": "isolated-dir", "status": "idle"}

    def test_a_session_only_in_a_file_is_added(self):
        got = procs.merge_sessions([self.A], [self.B])
        self.assertEqual([r["sessionId"] for r in got], ["aaa", "bbb"])

    def test_a_session_in_both_appears_once_and_the_agents_row_wins(self):
        dup = dict(self.A, name="from-file", status="idle")
        got = procs.merge_sessions([self.A], [dup])
        self.assertEqual(got, [self.A])

    def test_the_agents_row_learns_who_started_it_from_the_file(self):
        # `claude agents --json` has no entrypoint; the session file has. A
        # session a program started must not lose that on the way in
        got = procs.merge_sessions([self.A], [dict(self.A, entrypoint="sdk-cli",
                                                   status="idle")])
        self.assertEqual(got[0]["entrypoint"], "sdk-cli")
        self.assertEqual(got[0]["status"], "busy", "the agents row still wins")

    def test_no_files_changes_nothing(self):                         # regression
        self.assertEqual(procs.merge_sessions([self.A], []), [self.A])


class TestPsTable(unittest.TestCase):
    """`LC_ALL=C TZ=UTC ps -axo pid=,lstart=,comm=`: the start time is five words,
    and it is the same text Claude Code stores as procStart."""

    PS = ("  101 Tue Sep 22 13:45:15 2026     /Users/x/.local/share/claude/versions/2.1.280\n"
          "  102 Wed Sep 23  9:00:00 2026     claude\n"
          "  103 Wed Sep 23 09:01:00 2026     /bin/zsh\n"
          "garbage line\n")

    def test_pid_start_and_command(self):
        got = procs.parse_ps_table(self.PS)
        self.assertEqual(got[101], ("Tue Sep 22 13:45:15 2026",
                                    "/Users/x/.local/share/claude/versions/2.1.280"))
        self.assertEqual(set(got), {101, 102, 103})

    def test_a_retitled_claude_is_still_claude(self):
        # a background session shows as `claude bg-spare` in `ps -o comm`
        self.assertEqual(procs.claude_pids({7: ("x", "claude bg-spare"),
                                            8: ("x", "claude bg-pty-host --x")}), [7, 8])

    def test_a_claude_under_a_path_with_spaces(self):
        # the desktop app ships Claude Code under ~/Library/Application Support
        path = "/Users/x/Library/Application Support/Claude/claude-code/2.1.280/claude"
        self.assertEqual(procs.claude_pids({9: ("x", path),
                                            10: ("x", path + " bg-spare")}), [9, 10])

    def test_not_claude(self):                                        # control
        for cmd in ("claudette", "claude-helper", "1.2", "1.2.3.4", "/bin/zsh",
                    "/Applications/Claude.app/Contents/Frameworks/Claude Helper"):
            with self.subTest(cmd=cmd):
                self.assertEqual(procs.claude_pids({1: ("x", cmd)}), [])

    def test_claude_processes_by_name_or_version_binary(self):
        # the versioned binary is how daemon-run sessions appear; missing it
        # missed sessions when this was measured
        self.assertEqual(procs.claude_pids(procs.parse_ps_table(self.PS)), [101, 102])

    def test_nothing_parses_to_nothing(self):                         # control
        self.assertEqual(procs.parse_ps_table(""), {})


if __name__ == "__main__":
    unittest.main()


class TestMarkOf(unittest.TestCase):
    """Which harness and which session started a process, from its env."""

    def test_claude(self):
        self.assertEqual(procs.mark_of({"CLAUDE_CODE_SESSION_ID": SID}), ("claude", SID))

    def test_codex(self):
        self.assertEqual(procs.mark_of({"CODEX_THREAD_ID": "01a06435-aaaa"}),
                         ("codex", "01a06435-aaaa"))

    def test_claude_with_the_pid_that_started_it(self):
        self.assertEqual(procs.mark_of({"CLAUDE_CODE_SESSION_ID": SID, "CLAUDE_PID": "4242"}),
                         ("claude", SID, 4242))

    def test_a_pid_that_is_not_a_number_is_not_kept(self):
        self.assertEqual(procs.mark_of({"CLAUDE_CODE_SESSION_ID": SID, "CLAUDE_PID": "4x"}),
                         ("claude", SID))

    def test_nobody(self):                                            # control
        self.assertIsNone(procs.mark_of({"CLAUDE_CONFIG_DIR": "/x"}))
        self.assertIsNone(procs.mark_of(None))


class TestParseLsofListen(unittest.TestCase):
    """`lsof -nP -iTCP -sTCP:LISTEN -Fpn`: p lines name the pid, n lines the
    address. IPv4, IPv6 and wildcard forms; one pid can listen more than once."""

    OUT = ("p101\nn*:3000\nn[::1]:3000\np102\nn127.0.0.1:5173\nn*:9229\n"
           "p103\nnlocalhost:8080\np104\nn[::1]:4000\n")

    def test_ports_per_pid(self):
        self.assertEqual(procs.parse_lsof_listen(self.OUT),
                         {101: [3000], 102: [5173, 9229], 103: [8080], 104: [4000]})

    def test_nothing_listening(self):                                 # control
        self.assertEqual(procs.parse_lsof_listen(""), {})


class TestRedactCommand(unittest.TestCase):
    """Command lines are printed and read by agents. A node process that renames
    itself can spill its environment into its command line (seen on this
    Mac: `npm exec chrome-devtools-mcp ... HOME=... PATH=...`)."""

    def test_secrets_are_masked(self):
        for cmd in (f"node srv.js --token={TOKEN}", f"node srv.js --api-key {TOKEN}",
                    f"x --password {TOKEN}", f"curl -H 'Authorization: Bearer {TOKEN}'",
                    f"git clone https://user:{TOKEN}@github.com/x/y",
                    f"npm exec mcp HOME=/Users/x PATH=/bin ANTHROPIC_API_KEY={TOKEN}",
                    f"run {TOKEN}"):
            with self.subTest(cmd=cmd[:30]):
                self.assertNotIn(TOKEN, procs.redact_command(cmd))

    def test_every_reported_shape_is_masked(self):
        # the shapes the security review found passing through unchanged
        for cmd in (
            "node x npm_config_//registry.npmjs.org/:_authToken=SECRET",
            "node x github_token=SECRET", "node x ApiKey=SECRET",
            "docker run -e openai_api_key=SECRET img",
            "x ghs_SECRETSECRETSECRETSECRET", "x ghu_SECRETSECRETSECRETSECRET",
            "x glpat-SECRETSECRETSECRET", "x sk_live_SECRETSECRETSECRET",
            "x rk_test_SECRETSECRETSECRET", "x AIzaSECRETSECRETSECRETSECRETSECRETSECRE",
            "x ASIASECRETSECRETSE", "x npm_SECRETSECRETSECRETSECRETSECRETSECRET",
            "x hf_SECRETSECRETSECRETSECRET",
            "x eyJSECRETSECRET.eyJSECRETSECRET.SECRETSIG",
            "mysql -pSECRET db", "redis-cli -a SECRET", "sshpass -p SECRET ssh h",
            "curl -u user:SECRET https://h", "curl -H 'X-Api-Key: SECRET' h",
            "curl -H 'Authorization: Basic SECRET' h",
            "curl -H 'Authorization: token SECRET' h", "curl -H 'Cookie: session=SECRET' h",
            "curl 'https://h/x?access_token=SECRET&y=1'", "curl https://h/x?api_key=SECRET",
            'node x {"token":"SECRET"}', 'node x {"apiKey": "SECRET"}',
            "git clone https://SECRET@github.com/x/y", "x --pat SECRET",
            'x --token="abc SECRET"', "x --token='abc SECRET'"):
            with self.subTest(cmd=cmd):
                self.assertNotIn("SECRET", procs.redact_command(cmd))

    def test_ordinary_arguments_survive(self):                        # control
        for cmd in ("ls -a /tmp", "grep -p x", "x ghost_town", "x skeleton",
                    "node x --port=3000 file.js", "curl https://h/x?page=2",
                    "git clone https://github.com/x/y", "vite --host 0.0.0.0",
                    "python3 -m http.server 8765", "next-server (v15)",
                    "npm exec chrome-devtools-mcp@latest --isolated"):
            with self.subTest(cmd=cmd):
                self.assertEqual(procs.redact_command(cmd), cmd)

    def test_control_characters_never_reach_a_terminal(self):
        # any process can set its own title: ESC ] 0 ; ... BEL retitles a window
        got = procs.redact_command("x \x1b]0;pwned\x07 \x1b[31mred\x9b")
        self.assertNotRegex(got, r"[\x00-\x1f\x7f-\x9f]")
        self.assertIn("pwned", got)

    def test_env_spill_keeps_the_names(self):
        got = procs.redact_command("npm exec mcp HOME=/Users/x PATH=/bin")
        self.assertEqual(got, "npm exec mcp HOME=*** PATH=***")

    def test_an_ordinary_command_is_unchanged(self):                  # control
        for cmd in ("node server.js --port 3000", "vite --host 0.0.0.0 --port=5173",
                    "python3 -m http.server 8765", "next-server (v15)"):
            with self.subTest(cmd=cmd):
                self.assertEqual(procs.redact_command(cmd), cmd)


def proc(pid, ppid=500, cmd="node server.js", start="Tue Sep 22 13:45:15 2026"):
    return pid, (ppid, start, cmd)


LIVE_SID = "aaaa1111-0000-4000-8000-000000000001"
DEAD_SID = "dddd4444-0000-4000-8000-000000000004"


class TestAttribute(unittest.TestCase):
    """Which processes each session started - the env mark says so, even after
    the process was orphaned - and which were left behind by sessions that
    ended. Codex processes are their own group: whether a Codex session is still
    running cannot be told cheaply (measured)."""

    def world(self):
        table = dict([proc(10, cmd="claude"),                       # the session itself
                      proc(11, ppid=10), proc(12, ppid=1),           # its work, one orphaned
                      proc(13, ppid=10, cmd="npm exec chrome-devtools-mcp@latest"),
                      proc(20, ppid=1, cmd="vite"),                  # left behind
                      proc(21, ppid=1, cmd="npx -y some-mcp-server"),
                      proc(30, ppid=1, cmd="workerd serve"),         # codex
                      proc(40, ppid=1, cmd="my own server")])        # nobody's
        # 10 is the session itself; if its own environment carries its own id,
        # it is still not its own work
        marks = {10: ("claude", LIVE_SID), 11: ("claude", LIVE_SID), 12: ("claude", LIVE_SID),
                 13: ("claude", LIVE_SID), 20: ("claude", DEAD_SID),
                 21: ("claude", DEAD_SID), 30: ("codex", "01a0abc8-x")}
        ports = {12: [3000], 13: [9222], 20: [5173], 30: [60805, 60808]}
        sessions = [{"sessionId": LIVE_SID, "pid": 10}]
        return procs.attribute(table, marks, ports, sessions)

    def test_a_sessions_processes_include_its_orphans(self):
        got = self.world()["sessions"][LIVE_SID]
        self.assertEqual(sorted(p["pid"] for p in got), [11, 12, 13])
        self.assertEqual([p["pid"] for p in got if p["orphan"]], [12])

    def test_helpers_are_marked_so_live_rows_can_hide_them(self):
        got = {p["pid"]: p["helper"] for p in self.world()["sessions"][LIVE_SID]}
        self.assertEqual(got, {11: False, 12: False, 13: True})

    def test_the_npm_proxy_is_a_helper(self):
        table = dict([proc(60, cmd="/Users/x/.safe-chain/bin/safe-chain npm install")])
        w = procs.attribute(table, {60: ("claude", LIVE_SID)}, {},
                            [{"sessionId": LIVE_SID, "pid": 10}])
        self.assertTrue(w["sessions"][LIVE_SID][0]["helper"])

    def test_the_command_is_the_short_safe_form(self):
        # a token with no known prefix: the blocklist lets it through, the
        # allowlist does not. `command` is what every default view prints;
        # `command_full` is for `ccwho ps --full` only
        table = dict([proc(60, cmd="/usr/bin/deploy /Users/x/app --key k8Hq2vX9pLm3nR7tW1yZ4bC6dF0gJ5sA")])
        p = procs.attribute(table, {60: ("claude", LIVE_SID)}, {},
                            [{"sessionId": LIVE_SID, "pid": 10}])["sessions"][LIVE_SID][0]
        self.assertNotIn("k8Hq2vX9pLm3nR7tW1yZ4bC6dF0gJ5sA", p["command"])
        self.assertTrue(p["command"].startswith("deploy app"))
        self.assertIn("/Users/x/app", p["command_full"])                  # control

    def test_left_behind_keeps_helpers(self):
        # an MCP server whose session ended is garbage like any other leftover
        self.assertEqual(sorted(p["pid"] for p in self.world()["left_behind"]), [20, 21])

    def test_codex_is_its_own_group_never_left_behind(self):
        w = self.world()
        self.assertEqual([p["pid"] for p in w["codex"]], [30])
        self.assertNotIn(30, [p["pid"] for p in w["left_behind"]])

    def test_ports_travel_with_the_process(self):
        got = {p["pid"]: p["ports"] for p in self.world()["codex"]}
        self.assertEqual(got, {30: [60805, 60808]})

    def test_a_live_sessions_process_is_never_anyones_work(self):
        # a claude started from session A's shell carries A's mark; it is a live
        # session of its own, and listing it for A - or as left behind - would
        # hand a clean-up agent a live session to kill (adversarial F1)
        table = dict([proc(100, cmd="claude"), proc(101, cmd="claude")])
        marks = {100: ("claude", DEAD_SID), 101: ("codex", "01a0-x")}
        sessions = [{"sessionId": LIVE_SID, "pid": 100},
                    {"sessionId": "bbbb2222-0000-4000-8000-000000000002", "pid": 101}]
        w = procs.attribute(table, marks, {}, sessions)
        every = [p["pid"] for grp in list(w["sessions"].values()) + [w["left_behind"],
                                                                    w["codex"]] for p in grp]
        self.assertEqual(every, [])

    def test_the_session_itself_and_unmarked_processes_are_nobodys(self):  # control
        w = self.world()
        every = [p["pid"] for grp in (list(w["sessions"].values()) + [w["left_behind"],
                                                                     w["codex"]])
                 for p in grp]
        self.assertNotIn(10, every, "the session's own process is not its work")
        self.assertNotIn(40, every, "an unmarked process is never an agent's")

    def test_another_sessions_mark_is_not_this_one(self):            # control
        w = self.world()
        self.assertNotIn(20, [p["pid"] for p in w["sessions"][LIVE_SID]])

    # what must never be called left behind: a clean-up agent kills that group
    # (review cycle 2, adversarial #1 and #7)
    def test_with_the_session_list_incomplete_nothing_is_left_behind(self):
        w = self.world_with(sessions_known=False)
        self.assertEqual(w["left_behind"], [])
        self.assertEqual(sorted(p["pid"] for p in w["unsure"]), [20, 21])

    def test_under_a_running_claude_is_not_left_behind(self):
        # a session ccwho does not list is still a session: its work is not litter
        extra = dict([proc(50, ppid=1, cmd="/Users/x/.local/bin/claude --resume"),
                      proc(51, ppid=50, cmd="node dev.js"),
                      proc(52, ppid=51, cmd="vite")])
        w = self.world_with(extra=extra, extra_marks={51: ("claude", DEAD_SID),
                                                      52: ("claude", DEAD_SID)})
        self.assertEqual(sorted(p["pid"] for p in w["unsure"]), [51, 52])
        self.assertEqual(sorted(p["pid"] for p in w["left_behind"]), [20, 21])  # control

    def test_an_app_bundle_is_not_left_behind(self):
        extra = dict([proc(53, ppid=1,
                           cmd="/Applications/Docker.app/Contents/MacOS/com.docker.backend")])
        w = self.world_with(extra=extra, extra_marks={53: ("claude", DEAD_SID)})
        self.assertEqual([p["pid"] for p in w["unsure"]], [53])
        self.assertNotIn(53, [p["pid"] for p in w["left_behind"]])

    def test_an_app_codex_started_is_not_codex_work(self):
        # measured: Codex started Docker Desktop, and all ten of its processes
        # carried CODEX_THREAD_ID. Cleaning up after Codex must not quit Docker
        extra = dict([proc(54, ppid=1,
                           cmd="/Applications/Docker.app/Contents/MacOS/com.docker.backend")])
        w = self.world_with(extra=extra, extra_marks={54: ("codex", "01a0abc8-x")})
        self.assertEqual([p["pid"] for p in w["unsure"]], [54])
        self.assertEqual([p["pid"] for p in w["codex"]], [30])               # control

    def test_an_app_a_live_session_started_is_not_its_work(self):
        # stopping the session and its processes must not quit the app either
        extra = dict([proc(55, ppid=1, cmd="/Applications/Docker.app/Contents/MacOS/Docker")])
        w = self.world_with(extra=extra, extra_marks={55: ("claude", LIVE_SID)})
        self.assertEqual([p["pid"] for p in w["unsure"]], [55])
        self.assertNotIn(55, [p["pid"] for p in w["sessions"][LIVE_SID]])

    def test_an_unlisted_claude_is_never_left_behind(self):
        # cycle 3 #1: the process itself is a claude - a session ccwho does not list
        w = procs.attribute({200: (1, "t", "claude --resume x")},
                            {200: ("claude", DEAD_SID)}, {}, [])
        self.assertEqual(w["left_behind"], [])
        self.assertEqual([p["pid"] for p in w["unsure"]], [200])

    def test_a_claude_named_by_its_full_path_is_a_claude(self):
        # attribute gets `ps -o command`, not `comm`: path and arguments included
        table = {200: (1, "t", "/opt/homebrew/bin/claude --add-dir /Users/me/x"),
                 201: (1, "t", "/Users/me/Library/Application Support/Claude/"
                                "claude-code/2.1.280/claude --x"),
                 300: (200, "t", "node server.js"), 301: (201, "t", "vite")}
        w = procs.attribute(table, {300: ("claude", DEAD_SID), 301: ("claude", DEAD_SID)},
                            {}, [])
        self.assertEqual(w["left_behind"], [])

    def test_the_users_work_under_an_app_or_tmux_is_not_left_behind(self):
        # cycle 3 #5: an agent opened VS Code or started tmux; the session ended and
        # the user works on in it. The mark is inherited, the work is the user's
        table = {10: (1, "t", "/Applications/Visual Studio Code.app/Contents/MacOS/Electron"),
                 11: (10, "t", "/bin/zsh -il"), 12: (11, "t", "node server.js --port 3000"),
                 20: (1, "t", "tmux new -d -s dev"), 21: (20, "t", "-zsh"),
                 22: (21, "t", "vim notes.md"), 30: (1, "t", "vite")}
        w = procs.attribute(table, {pid: ("claude", DEAD_SID) for pid in table}, {}, [])
        self.assertEqual([p["pid"] for p in w["left_behind"]], [30])       # 30: control

    def test_its_claude_still_running_is_not_left_behind(self):
        # review 4 #4: after /clear, or for a claude ccwho does not list, the
        # session id is gone but the claude that started it runs on
        table = {200: (1, "t", "/Users/x/.local/bin/claude"), 300: (1, "t", "node s.js"),
                 301: (1, "t", "vite")}
        w = procs.attribute(table, {300: ("claude", DEAD_SID, 200),
                                    301: ("claude", DEAD_SID, 999)}, {}, [])
        self.assertEqual([p["pid"] for p in w["unsure"]], [300])
        self.assertEqual([p["pid"] for p in w["left_behind"]], [301])      # control: 999 is gone

    def test_a_wrapper_of_a_running_claude_is_not_left_behind(self):
        # review 6 #1: killing `caffeinate` or `script` kills the claude under it
        for wrapper in ("caffeinate -i claude", "npx @anthropic-ai/claude-code",
                        "script -q /dev/null claude", "/bin/zsh -c claude --resume x",
                        "nice claude"):
            with self.subTest(wrapper=wrapper):
                w = procs.attribute({400: (1, "s", wrapper), 500: (400, "s", "claude")},
                                    {400: ("claude", DEAD_SID)}, {},
                                    [{"sessionId": LIVE_SID, "pid": 500}])
                self.assertEqual(w["left_behind"], [])
                self.assertEqual([p["pid"] for p in w["unsure"]], [400])
        w = procs.attribute({400: (1, "s", "node s.js"), 401: (400, "s", "vite")},  # control
                            {400: ("claude", DEAD_SID)}, {}, [])
        self.assertEqual([p["pid"] for p in w["left_behind"]], [400])

    def test_a_screen_server_is_a_multiplexer(self):
        # review 6 #2: ps shows the GNU screen server as SCREEN
        table = {100: (1, "s", "SCREEN -dmS dev"), 101: (100, "s", "/bin/zsh"),
                 102: (101, "s", "vim notes.md"), 103: (1, "s", "screenshot-tool")}
        w = procs.attribute(table, {pid: ("claude", DEAD_SID) for pid in table}, {}, [])
        self.assertEqual([p["pid"] for p in w["left_behind"]], [103])        # 103: control

    def test_a_shared_user_daemon_is_not_left_behind(self):
        # issue #9: `git commit -S`, `git push` with ControlPersist or the
        # credential cache start a per-user daemon from inside an agent's command.
        # It inherits the mark and serves the whole login: killing it as litter
        # drops cached passphrases and loaded keys
        daemons = ("gpg-agent --homedir /Users/u/.gnupg --use-standard-socket --daemon",
                   "ssh: /Users/u/.ssh/cm-git@github.com:22 [mux]",
                   "git credential-cache--daemon /Users/u/.cache/git/credential/socket",
                   "/opt/homebrew/bin/gpg-agent --daemon",
                   "dirmngr --daemon --homedir /Users/u/.gnupg", "scdaemon --multi-server",
                   "keyboxd --homedir /Users/u/.gnupg --daemon",
                   # review: a control path with spaces, the dashed git spelling,
                   # and the per-user services an ordinary command starts
                   "ssh: /Users/u/Library/Application Support/ssh/cm-abc [mux]",
                   "git-credential-cache--daemon /Users/u/.cache/git/credential/socket",
                   "/Library/Developer/CommandLineTools/usr/libexec/git-core/"
                   "git-credential-cache--daemon /x/socket",
                   "watchman --foreground --logfile=/x/log --sockname=/x/sock",
                   "/opt/homebrew/bin/watchman --foreground",
                   "limactl hostagent --pidfile /x/ha.pid --socket /x/ha.sock colima",
                   "ollama serve", "/usr/local/bin/ollama serve")
        for cmd in daemons:
            with self.subTest(cmd=cmd):
                w = procs.attribute({60: (1, "t", cmd)}, {60: ("claude", DEAD_SID)}, {}, [])
                self.assertEqual(w["left_behind"], [])
                self.assertEqual([(p["pid"], p["why"]) for p in w["unsure"]],
                                 [(60, "it is a shared user daemon")])
        w = procs.attribute({61: (1, "t", "node server.js")},                 # control
                            {61: ("claude", DEAD_SID)}, {}, [])
        self.assertEqual([p["pid"] for p in w["left_behind"]], [61])

    def test_what_a_shared_daemon_runs_is_not_left_behind(self):
        # killing pinentry closes a passphrase dialog; a model runner or a VM's
        # port forward serve the whole login like their parent
        for daemon, child in (("gpg-agent --homedir /Users/u/.gnupg --daemon",
                               "/opt/homebrew/bin/pinentry-mac"),
                              ("ollama serve", "/Applications/Ollama.app/x/ollama runner --model m"),
                              ("limactl hostagent --pidfile /x/ha.pid colima",
                               "ssh -F /dev/null -N -L 2375:127.0.0.1:2375 lima")):
            with self.subTest(child=child):
                w = procs.attribute({70: (1, "t", daemon), 71: (70, "t", child)},
                                    {70: ("claude", DEAD_SID), 71: ("claude", DEAD_SID)}, {}, [])
                self.assertEqual(w["left_behind"], [])
                self.assertEqual({p["pid"]: p["why"] for p in w["unsure"]},
                                 {70: "it is a shared user daemon",
                                  71: "it runs under a shared user daemon"})
        w = procs.attribute({72: (1, "t", "node server.js"),                   # control
                             73: (72, "t", "python3 worker.py")},
                            {72: ("claude", DEAD_SID), 73: ("claude", DEAD_SID)}, {}, [])
        self.assertEqual(sorted(p["pid"] for p in w["left_behind"]), [72, 73])

    def test_an_orphaned_ssh_transport_is_not_left_behind(self):
        # measured (second review, real /usr/bin/ssh with ControlPersist): the
        # ProxyJump or ProxyCommand of a mux master is NOT its child - it is an
        # orphan at ppid 1. Killing it drops the master and every session on it
        for cmd in ("ssh -W [github.com]:22 bastion",
                    "/usr/bin/ssh -o BatchMode=yes -p 2222 -W 127.0.0.1:22 bastion",
                    "ssh -W %h:%p jump.example.com",
                    "/usr/bin/nc 127.0.0.1 22999", "nc -X connect -x proxy:8080 github.com 22",
                    "cloudflared access ssh --hostname git.example.com",
                    "/opt/homebrew/bin/colima daemon start default --inotify"):
            with self.subTest(cmd=cmd):
                w = procs.attribute({60: (1, "t", cmd)}, {60: ("claude", DEAD_SID)}, {}, [])
                self.assertEqual(w["left_behind"], [], cmd)
        for cmd in ("ssh -N -L 3000:localhost:3000 host", "nc -l 3000", "nc -lk 8080",   # control
                    "colima list", "ssh host uptime", "cloudflared tunnel run x"):
            with self.subTest(control=cmd):
                w = procs.attribute({61: (1, "t", cmd)}, {61: ("claude", DEAD_SID)}, {}, [])
                self.assertEqual([p["pid"] for p in w["left_behind"]], [61], cmd)

    def test_a_program_named_like_a_daemon_is_not_one(self):             # control
        for cmd in ("node gpg-agent-mock.js", "vim ssh-agent.md", "python3 ssh.py",
                    "man gpg-agent", "grep -r ssh-agent src", "echo ssh: x [mux]",
                    # the edges of each pattern
                    "gpg-agent-mock --daemon", "python3 /x/gpg-agent",
                    "grep -r git credential-cache--daemon src", "ssh: x [mux] extra",
                    "node watchman-mock.js", "ollama run llama3", "limactl list",
                    # an agent's own `eval $(ssh-agent)` is its own litter: the
                    # login's agent is launchd's, and carries no mark
                    "ssh-agent -s", "ssh-agent npm run dev"):
            with self.subTest(cmd=cmd):
                w = procs.attribute({62: (1, "t", cmd)}, {62: ("claude", DEAD_SID)}, {}, [])
                self.assertEqual([p["pid"] for p in w["left_behind"]], [62])

    def test_unsure_says_why(self):
        w = self.world_with(sessions_known=False)
        self.assertTrue(all(p.get("why") for p in w["unsure"]))

    def test_ccwho_and_what_it_runs_are_nobodys(self):
        # `ccwho ps` run by an agent carries the agent's mark; so do its ps and lsof
        extra = dict([proc(70, ppid=10, cmd="python3 ccwho.py ps"),
                      proc(71, ppid=70, cmd="lsof -iTCP")])
        w = self.world_with(extra=extra, extra_marks={70: ("claude", LIVE_SID),
                                                      71: ("claude", LIVE_SID)}, own=70)
        self.assertEqual(sorted(p["pid"] for p in w["sessions"][LIVE_SID]), [11, 12, 13])

    def test_a_command_line_that_names_the_session_joins_its_list(self):
        # the fallback for a process whose environment cannot be read (/bin/bash):
        # listed with the session, so the row's count and `ccwho ps` agree (#8)
        extra = dict([proc(60, ppid=1, cmd=f"bash -c sleep 9 {LIVE_SID}")])
        w = self.world_with(extra=extra, named={LIVE_SID: {60}})
        got = {p["pid"]: p["orphan"] for p in w["sessions"][LIVE_SID]}
        self.assertEqual(got, {11: False, 12: True, 13: False, 60: True})

    def world_with(self, extra=None, extra_marks=None, sessions_known=True, own=None,
                   named=None):
        table = {**dict([proc(10, cmd="claude"), proc(11, ppid=10), proc(12, ppid=1),
                         proc(13, ppid=10, cmd="npm exec chrome-devtools-mcp@latest"),
                         proc(20, ppid=1, cmd="vite"), proc(21, ppid=1, cmd="npx -y some-mcp"),
                         proc(30, ppid=1, cmd="workerd serve")]), **(extra or {})}
        marks = {11: ("claude", LIVE_SID), 12: ("claude", LIVE_SID),
                 13: ("claude", LIVE_SID), 20: ("claude", DEAD_SID),
                 21: ("claude", DEAD_SID), 30: ("codex", "01a0abc8-x"),
                 **(extra_marks or {})}
        return procs.attribute(table, marks, {}, [{"sessionId": LIVE_SID, "pid": 10}],
                               sessions_known=sessions_known, own=own, named=named)

    def test_commands_are_redacted(self):
        table = dict([proc(50, cmd=f"node x --token={TOKEN}")])
        w = procs.attribute(table, {50: ("claude", DEAD_SID)}, {}, [])
        self.assertNotIn(TOKEN, repr(w))


class TestHelpers(unittest.TestCase):
    """A helper (an MCP server, the npm proxy) is hidden from rows. A dev server
    that happens to run from the npx cache or a folder named *-mcp* is not one."""

    def test_dev_servers_are_not_helpers(self):
        for cmd in ("node /Users/me/.npm/_npx/6a9b/node_modules/.bin/vite --port 3000",
                    "node /Users/me/code/my-mcp-app/server.js",
                    "node /Users/x/dev/docs-mcp/index.js",
                    "npx @modelcontextprotocol/inspector"):
            with self.subTest(cmd=cmd):
                self.assertFalse(procs.is_helper(cmd))

    def test_mcp_servers_are_helpers(self):                            # control
        for cmd in ("npm exec chrome-devtools-mcp@latest", "npx -y some-mcp-server",
                    "node /Users/me/.npm/_npx/1/node_modules/.bin/chrome-devtools-mcp",
                    "uvx mcp-server-fetch", "/Users/x/.safe-chain/bin/safe-chain npm i",
                    "node /x/node_modules/@modelcontextprotocol/server-github/dist/index.js",
                    # measured: chrome-devtools-mcp's watchdog, one per session
                    "node /Users/x/.npm/_npx/15c6/node_modules/chrome-devtools-mcp/build/src/"
                    "telemetry/watchdog/main.js --parent-pid=7324 --app-version=1.9.0"):
            with self.subTest(cmd=cmd):
                self.assertTrue(procs.is_helper(cmd))


class TestSummarise(unittest.TestCase):
    """What a row and the header say, from the attribution."""

    def test_a_row_counts_work_and_shows_its_ports(self):
        mine = [{"pid": 11, "ports": [], "helper": False, "orphan": False},
                {"pid": 12, "ports": [3000], "helper": False, "orphan": True},
                {"pid": 13, "ports": [9222], "helper": True, "orphan": False}]
        self.assertEqual(procs.row_summary(mine),
                         {"procs": 2, "ports": [3000], "detached": 1})

    def test_nothing_is_nothing(self):                                # control
        self.assertEqual(procs.row_summary([]), {"procs": 0, "ports": [], "detached": 0})


class TestSafeCommand(unittest.TestCase):
    """What ccwho prints of a command line: the program and the argument shapes
    known to be harmless - everything else is `…`. An allowlist, because two
    reviews of a list of secret shapes each found shapes it missed."""

    SHAPES = (  # every shape both reviews found, plus the first table's
        "node x npm_config_//registry.npmjs.org/:_authToken=SECRET",
        "node x github_token=SECRET", "node x ApiKey=SECRET",
        "docker run -e openai_api_key=SECRET img", "x ghs_SECRETSECRETSECRETSECRET",
        "x glpat-SECRETSECRETSECRET", "x sk_live_SECRETSECRETSECRET",
        "x eyJSECRETSECRET.eyJSECRETSECRET.SECRETSIG", "mysql -pSECRET db",
        "redis-cli -a SECRET", "sshpass -p SECRET ssh h", "sshpass -pSECRET ssh h",
        "curl -u user:SECRET https://h", "curl -uuser:SECRET h",
        "curl --user=user:SECRET h", "curl -H 'X-Api-Key: SECRET' h",
        "curl -H 'PRIVATE-TOKEN: SECRET' h", "curl --header 'X-Auth-Token: SECRET' h",
        "curl -H 'Authorization: Basic SECRET' h", "curl -H 'Cookie: session=SECRET' h",
        "curl 'https://h/x?access_token=SECRET&y=1'", "curl 'https://h/x?client_secret=SECRET'",
        "curl 'https://b.s3/x?X-Amz-Signature=SECRET'", 'node x {"token":"SECRET"}',
        'node x {"accessToken": "SECRET"}', "git clone https://SECRET@github.com/x/y",
        "x --pat SECRET", 'x --token="abc SECRET"', "docker login -p SECRET -u me",
        "x --api-key secret", "x --client-secret secret", "x --token=secret",
        "x OPENAI_API_KEY=secret", f"run {TOKEN}", f"x --password {TOKEN}")

    def test_no_secret_shape_gets_through(self):
        for cmd in self.SHAPES:
            with self.subTest(cmd=cmd):
                got = procs.safe_command(cmd)
                self.assertNotIn("SECRET", got)
                self.assertNotIn("secret", got.replace("client-secret", ""))
                self.assertNotIn(TOKEN, got)

    # lower case, digits and dashes: the shapes that the plain-word allowlist
    # would print if the flag, key or prefix rules were not there
    PLAIN = ("redis-cli -a hunter2", "mysql -p hunter2 db", "sshpass -p hunter2 ssh h",
             "OPENAI_API_KEY=hunter2 node x", "db_password=hunter2 node x",
             "x ghp_hunter2hunter2", "x sk-proj-hunter2", "x npm_hunter2hunter2",
             "x --token hunter2", "x --db-password=hunter2")

    # review cycle 3 (#3, #4): flag names, keys and header values it did not know
    CYCLE3 = ("node s.js --passphrase hunter2", "x --passcode hunter2", "x --pw hunter2",
              "x --bearer hunter2", "x --jwt hunter2", "gpg --passphrase hunter2",
              "plink -pw hunter2 host", "openssl enc -aes-256-cbc -k hunter2",
              "ssh-keygen -N hunter2", "DB_PW=hunter2 node s.js", "API_TOK=hunter2 node x",
              "JWT=hunter2 node x", "x sessionid=hunter2", "curl -H Cookie: sid=hunter2",
              "curl -H 'authorization: bearer hunter2' h",
              "curl -H Authorization: Bearer hunter2hunter2hunt https://api.x.com",
              "curl -H X-Api-Key: hunter2hunter2 h",
              "curl https://hooks.x.com/services/t0/b0/hunter2hunterhunterhunt",
              "app --password 12345678hunter2", "app --token 12345678901234567890hunter2",
              "redis-cli -a 987654321hunter2", "sshpass -p 12345hunter2 ssh h")

    def test_cycle3_shapes_do_not_get_through(self):
        for cmd in self.CYCLE3:
            with self.subTest(cmd=cmd):
                self.assertNotIn("hunter2", procs.safe_command(cmd))

    # review 4: hiding one token after a secret signal was not enough - after the
    # first signal, nothing more of the line is shown
    REVIEW4 = ("/usr/bin/sudo /opt/homebrew/bin/sshpass -p sunshine ssh host",
               "sudo sshpass -p 123456 ssh host", "env sshpass -p 123456 ssh host",
               "node x.js Token: -x sunshine",
               "node s.js c2VjcmV0cGFzc3dvcmQxMjM0NTY=",
               "node s.js ghp_1234567890abcdefghijABCDEFGHIJ123456=x",
               "myapp --password correct horse battery staple",
               "curl -H X-Secret: correct horse battery",
               "sqlcmd -S db -U sa -P 12345678", "mongosh -u admin -p 12345678",
               "security add-generic-password -a me -s svc -w 123456",
               "zip -P 4711 out.zip a.txt", "app --code 123456",
               "node pay.js 4111111111111111",
               "htpasswd -b .htpasswd admin sunshine", "echo sunshine | sudo -S ls",
               "printf sunshine | docker login -u me --password-stdin")

    def test_review4_shapes_do_not_get_through(self):
        for cmd in self.REVIEW4:
            with self.subTest(cmd=cmd):
                got = procs.safe_command(cmd)
                for secret in ("sunshine", "123456", "4711", "horse", "c2VjcmV0",
                               "ghp_", "4111111111111111"):
                    self.assertNotIn(secret, got)

    # review 5: each shape the allowlist trusted was a password shape somewhere
    REVIEW5 = (("mytool login admin welcome@123", "welcome@123"),
               ("mytool --code summer@2024", "summer@2024"),
               ("deploy --x 1 hunter@next", "hunter"),
               ("mytool --admin_code hunter", "hunter"), ("mytool --Port hunter", "hunter"),
               ("mytool -xyz hunter", "hunter"), ("mytool --x=1 hunter", "hunter"),
               ("mytool -- hunter", "hunter"),
               ("mysql --p\u0430ssword 12345", "12345"),
               ("mytool --p\u0430ssword hunter", "hunter"),
               ("mytool --x 1 P4ssw0rd.ab", "P4ssw0rd"),
               ("mytool --x 1 AbCdEf0123456789AbCdEf0123456789.sig", "AbCdEf0123456789"),
               ("mytool JBSWY3DPEHPK3PXPABC=", "JBSWY3DPEHPK3PXPABC"),
               ("mytool --x 1 JBSWY3DPEHPK3PXPABC2=x", "JBSWY3DPEHPK3PXPABC"),
               ("ipmitool -H 10.0.0.1 -P 12345", "12345"),
               ("echo 12345 | cryptsetup open /dev/disk2 vault", "12345"),
               ("curl https://u:1234/w@host/x", "1234"),
               ("mytool --user alice (hunter)", "hunter"), ("mytool --x y hunter)", "hunter"))

    def test_review5_shapes_do_not_get_through(self):
        for cmd, secret in self.REVIEW5:
            with self.subTest(cmd=cmd):
                self.assertNotIn(secret, procs.safe_command(cmd))
        self.assertNotIn("welcome@123", procs.safe_command("Log in as bob with welcome@123",
                                                           program=False))

    def test_review5_controls_stay_readable(self):                     # control
        for cmd in ("npx chrome-devtools-mcp@latest", "npm run dev", "node server.js",
                    "PORT=3000 node x", "node (vitest 1)", "vite --port 5173"):
            with self.subTest(cmd=cmd):
                self.assertEqual(procs.safe_command(cmd), cmd)

    def test_a_file_is_a_known_extension_and_no_capitals_with_digits(self):
        # one guard per input: an unknown extension, capitals mixed with digits
        # (--opt, not --x: a flag too short to parse closes the line before these)
        for cmd, secret in (("mytool --opt p4ssw0rd.ab", "p4ssw0rd"),
                            ("mytool --opt P4ssw0rdX.js", "P4ssw0rdX")):
            with self.subTest(cmd=cmd):
                self.assertNotIn(secret, procs.safe_command(cmd))
        for name in ("App.tsx", "README.md", "test_api2.py"):             # control
            with self.subTest(name=name):
                self.assertEqual(procs.safe_command(f"code -g /x/{name}"), f"code -g {name}")

    def test_each_key_rule_on_its_own(self):
        # before any flag, so each reaches the KEY=value rule: an empty value,
        # digits in the key, a long key without `_`
        for cmd, secret in (("mytool JBSWYDPEHPKPXPABC=", "JBSWYDPEHPKPXPABC"),
                            ("mytool QK_ZX=", "QK_ZX"),        # padding: empty value
                            ("mytool AB_12345=x", "AB_12345"),
                            ("mytool ABCDEFGHIJKLMNOP=x", "ABCDEFGHIJKLMNOP")):
            with self.subTest(cmd=cmd):
                self.assertNotIn(secret, procs.safe_command(cmd))
        self.assertEqual(procs.safe_command("mytool MY_VAR=x"), "mytool MY_VAR=…")  # control

    def test_a_number_in_a_long_flag_is_hidden(self):
        self.assertEqual(procs.safe_command("mytool --code=4242"), "mytool --code=…")

    def test_each_closing_rule_on_its_own(self):
        # one rule per input: a secret-named program, a header value, -w
        for cmd, secret in (("passgen abcdef", "abcdef"), ("curl -H X-Pin: 4242 h", "4242"),
                            ("tool -w 4242", "4242")):
            with self.subTest(cmd=cmd):
                self.assertNotIn(secret, procs.safe_command(cmd))

    def test_what_the_model_wrote_is_closed_after_a_secret_word(self):
        for text in ("password = sunshine", "password is sunshine",
                     "Set the password to sunshine"):
            with self.subTest(text=text):
                self.assertNotIn("sunshine", procs.safe_command(text, program=False))

    def test_a_key_is_shown_only_in_the_shape_of_one(self):              # control
        self.assertEqual(procs.safe_command("x NODE_OPTIONS=--max"), "x NODE_OPTIONS=…")

    def test_anything_that_is_not_text_does_not_raise(self):
        for value in (5, ["ls"], {"q": 1}, None, 2.5):
            with self.subTest(value=value):
                self.assertIsInstance(procs.safe_command(value), str)
        self.assertEqual(procs.safe_command(5), "5")                      # control

    def test_a_letters_only_secret_is_hidden_by_its_place(self):
        # letters only passes the plain-word shape: its PLACE must hide it
        for cmd in ("DB_PASSWORD=correcthorse node x", "MY_VAR=correcthorse node x",
                    "curl -H authorization: bearer correcthorse h",
                    "curl -H cookie: correcthorse h", "echo pass correcthorse"):
            with self.subTest(cmd=cmd):
                self.assertNotIn("correcthorse", procs.safe_command(cmd))

    def test_an_allowlisted_key_keeps_its_value(self):                 # control
        self.assertEqual(procs.safe_command("MY_VAR=abc NODE_ENV=test node x"),
                         "MY_VAR=… NODE_ENV=test node x")

    def test_a_number_after_a_secret_flag_is_hidden(self):
        for cmd in ("app --password 12345678", "app --token 12345678901234567890",
                    "redis-cli -a 987654321", "sshpass -p 12345 ssh h",
                    "openssl enc -k 4242"):
            with self.subTest(cmd=cmd):
                got = procs.safe_command(cmd)
                self.assertNotRegex(got, r"\b(12345678|987654321|12345|4242)\b")

    def test_a_port_number_stays(self):                               # control
        # a number after a flag is a port or a PIN - which, ccwho cannot tell; the
        # ports column says which ports it holds. After --port, it is a port
        self.assertEqual(procs.safe_command("ssh -p 2222 h"), "ssh -p …")
        self.assertEqual(procs.safe_command("vite --port 5173"), "vite --port 5173")
        self.assertEqual(procs.safe_command("curl http://localhost:3000/api/x?y=1"),
                         "curl http://localhost:3000/…")

    def test_no_plain_word_secret_gets_through(self):
        for cmd in self.PLAIN:
            with self.subTest(cmd=cmd):
                self.assertNotIn("hunter2", procs.safe_command(cmd))

    def test_dev_commands_stay_readable(self):                         # control
        for cmd, shown in (
                ("vite --port 5173", "vite --port 5173"),
                ("next-server (v15)", "next-server (v15)"),
                ("npm run dev", "npm run dev"),
                ("PORT=3000 npm run dev", "PORT=3000 npm run dev"),
                ("NODE_ENV=production node server.js", "NODE_ENV=production node server.js"),
                ("/usr/local/bin/node /Users/x/p/app/server.js --port=3000",
                 "node server.js --port=3000"),
                ("python3 -m http.server 8765", "python3 -m http.server 8765"),
                ("vite --host 0.0.0.0", "vite --host 0.0.0.0"),
                ("python3 -m pytest -x tests/test_api.py", "python3 -m pytest -x test_api.py"),
                ("git log --oneline -5", "git log --oneline -5"),
                ("-zsh", "zsh"),
                ("node (vitest 1)", "node (vitest 1)"), ("node (worker)", "node (worker)"),
                ("npm exec -y chrome-devtools-mcp@latest",
                 "npm exec -y chrome-devtools-mcp@latest")):
            with self.subTest(cmd=cmd):
                self.assertEqual(procs.safe_command(cmd), shown)

    def test_the_rest_is_an_ellipsis(self):
        self.assertEqual(procs.safe_command("node x.js abcDEF123/xyz+==  q9ZZ"),
                         "node x.js …")

    def test_control_characters_never_get_through(self):
        got = procs.safe_command("x \x1b]0;pwned\x07 run")
        self.assertNotRegex(got, r"[\x00-\x1f\x7f-\x9f]")

    def test_nothing_is_nothing(self):
        self.assertEqual(procs.safe_command(""), "")


# The two real wait loops of 2026-09-24, as `ps -o command` showed them. Each
# polled a task output for a vitest summary that its test run never printed.
_WRAP = ("/bin/zsh -c source /Users/x/.claude/shell-snapshots/snapshot-zsh-1-a.sh "
         "2>/dev/null || true && eval '{}' < /dev/null && pwd -P >| /tmp/claude-c8ee-cwd")
TASKS = "/private/tmp/claude-501/-Users-x-projects-liveapp/c9080000/tasks/"
LOOP_VAR = _WRAP.format(
    "F=" + TASKS + "bf6yb2pu5.output; until grep -qE \"^      Tests |Test Files \" \"$F\" "
    "2>/dev/null; do sleep 5; done; grep -E \"^      Tests \" \"$F\" | head -8")
LOOP_ARG = _WRAP.format(
    "until grep -qE '\"'\"'^ *(Test Files|Tests) '\"'\"' " + TASKS + "bscl8fc6k.output "
    "2>/dev/null; do sleep 5; done; echo READY")


class TestWaitLoop(unittest.TestCase):
    """Only the one shape that can be judged: a loop whose condition is a single
    grep on task outputs and whose body is only a sleep. Anything else - another
    exit test, a counter, work after it - is unknown, and unknown is never dead:
    the flagged pid is the Bash tool's shell, and it runs the WHOLE command."""

    def test_the_real_loop_with_its_file_in_a_variable(self):
        self.assertEqual(procs.wait_loop(LOOP_VAR),
                         {"files": [TASKS + "bf6yb2pu5.output"], "sleep": 5.0,
                          "grep": ["-q", "-E", "-e", "^      Tests |Test Files "]})

    def test_the_real_loop_with_its_file_as_an_argument(self):
        self.assertEqual(procs.wait_loop(LOOP_ARG),
                         {"files": [TASKS + "bscl8fc6k.output"], "sleep": 5.0,
                          # the pattern as grep got it, out of the eval's quoting:
                          # ccwho asks grep again, and a wrong pattern never matches
                          "grep": ["-q", "-E", "-e", "^ *(Test Files|Tests) "]})

    def test_while_not_grep_is_the_same_loop(self):
        cmd = "while ! grep -q done " + TASKS + "b1.output; do sleep 2; done"
        self.assertEqual(procs.wait_loop(cmd), {"files": [TASKS + "b1.output"], "sleep": 2.0,
                                                "grep": ["-q", "-e", "done"]})

    def test_the_sleep_is_measured(self):
        cmd = "until grep -q DONE " + TASKS + "b1.output; do sleep 600; done"
        self.assertEqual(procs.wait_loop(cmd)["sleep"], 600.0)

    # the review's cases: each was called dead, and one would have killed e2e
    def test_a_loop_on_something_else_after_reading_a_task_file(self):
        cmd = ("tail -3 " + TASKS + "abc.output; while ! curl -s localhost:3000; "
               "do sleep 1; done; npm run e2e")
        self.assertIsNone(procs.wait_loop(cmd))

    def test_a_read_loop_and_a_long_sleep(self):
        cmd = ("grep FAIL " + TASKS + "abc.output; while read l; do echo $l; done "
               "< list; sleep 900")
        self.assertIsNone(procs.wait_loop(cmd))

    def test_a_bounded_loop_ends_by_itself(self):
        cmd = ("until grep -q ok " + TASKS + "abc.output || [ $n -ge 60 ]; "
               "do sleep 5; n=$((n+1)); done")
        self.assertIsNone(procs.wait_loop(cmd))

    def test_a_body_that_does_more_than_sleep(self):
        cmd = "until grep -q ok " + TASKS + "b1.output; do sleep 5; make; done"
        self.assertIsNone(procs.wait_loop(cmd))

    def test_a_condition_on_a_file_that_is_not_a_task_output(self):
        cmd = ("until grep -q ok " + TASKS + "b1.output /tmp/other.log; "
               "do sleep 5; done")
        self.assertIsNone(procs.wait_loop(cmd))

    def test_two_greps_are_not_judged(self):
        cmd = ("until grep -q x " + TASKS + "b1.output && grep -q y " + TASKS
               + "b2.output; do sleep 5; done")
        self.assertIsNone(procs.wait_loop(cmd))

    def test_while_grep_waits_for_the_opposite(self):
        # loops while the line IS there: whether it can end depends on content
        cmd = "while grep -q running " + TASKS + "b1.output; do sleep 5; done"
        self.assertIsNone(procs.wait_loop(cmd))

    def test_a_sleep_that_is_not_plain_seconds_is_unknown(self):
        for body in ("sleep 5m", "sleep $X"):
            with self.subTest(body=body):
                cmd = "until grep -q x " + TASKS + "b1.output; do " + body + "; done"
                self.assertIsNone(procs.wait_loop(cmd))

    # review round 2
    def test_only_options_that_keep_the_question_plain(self):
        # -v, -L, -c, -z, -r ask something else: the condition could be true
        for opts in ("-vq", "-q -L", "-qc", "-qz", "-qr", "-q -m 1"):
            with self.subTest(opts=opts):
                cmd = "until grep " + opts + " ok " + TASKS + "b1.output; do sleep 5; done"
                self.assertIsNone(procs.wait_loop(cmd))

    def test_the_options_that_do_are_kept(self):                       # control
        cmd = "until grep -qiF -s ok " + TASKS + "b1.output; do sleep 5; done"
        self.assertEqual(procs.wait_loop(cmd)["grep"], ["-q", "-i", "-F", "-s", "-e", "ok"])

    def test_the_assignment_before_the_loop_is_the_one(self):
        cmd = ("F=" + TASKS + "abc.output; until grep -q ok $F; do sleep 5; done; "
               "F=/x/tasks/b.output")
        self.assertEqual(procs.wait_loop(cmd)["files"], [TASKS + "abc.output"])

    def test_two_assignments_before_the_loop_are_unknown(self):
        cmd = ("F=" + TASKS + "a.output; F=" + TASKS + "b.output; "
               "until grep -q ok $F; do sleep 5; done")
        self.assertIsNone(procs.wait_loop(cmd))

    def test_a_loop_sent_to_the_background_is_unknown(self):
        # the shell that runs it is not the loop: killing it misses the loop
        cmd = "until grep -q ok " + TASKS + "b1.output; do sleep 5; done & wait; echo hi"
        self.assertIsNone(procs.wait_loop(cmd))

    def test_and_then_is_not_the_background(self):                     # control
        cmd = "until grep -q ok " + TASKS + "b1.output; do sleep 5; done && echo hi"
        self.assertIsNotNone(procs.wait_loop(cmd))

    # review round 3: what the shell expands, ccwho cannot ask again
    def test_a_pattern_the_shell_expands_is_unknown(self):
        for pat in ('"$X"', "$X", "$'ok\\x20line'", '"$(cat p)"', "`cat p`", '"${X}"',
                    '"`cat p`"'):
            with self.subTest(pat=pat):
                cmd = "until grep -q " + pat + " " + TASKS + "b1.output; do sleep 5; done"
                self.assertIsNone(procs.wait_loop(cmd))

    def test_a_dollar_in_single_quotes_is_a_dollar(self):              # control
        cmd = "until grep -q 'Done$' " + TASKS + "b1.output; do sleep 5; done"
        self.assertEqual(procs.wait_loop(cmd)["grep"], ["-q", "-e", "Done$"])

    def test_a_variable_set_to_a_task_file_is_still_the_file(self):    # control
        cmd = ("F=" + TASKS + "b1.output; until grep -q ok \"${F}\"; do sleep 5; done")
        self.assertEqual(procs.wait_loop(cmd)["files"], [TASKS + "b1.output"])

    def test_an_escape_only_some_greps_know_is_unknown(self):
        for pat in (r"'\d+ passed'", r"'a\sb'", r"'\bword'", r"'\w+'", r"'a\|b'", r"'a\+'"):
            with self.subTest(pat=pat):
                cmd = "until grep -qE " + pat + " " + TASKS + "b1.output; do sleep 5; done"
                self.assertIsNone(procs.wait_loop(cmd))

    def test_a_posix_escape_is_fine(self):                              # control
        cmd = r"until grep -q 'v1\.0 \(3\)' " + TASKS + "b1.output; do sleep 5; done"
        self.assertEqual(procs.wait_loop(cmd)["grep"], ["-q", "-e", r"v1\.0 \(3\)"])

    def test_ignoring_case_beyond_ascii_is_unknown(self):
        cmd = "until grep -qi 'Ünïcode' " + TASKS + "b1.output; do sleep 5; done"
        self.assertIsNone(procs.wait_loop(cmd))

    def test_ignoring_case_in_ascii_is_fine(self):                      # control
        cmd = "until grep -qi 'done' " + TASKS + "b1.output; do sleep 5; done"
        self.assertIsNotNone(procs.wait_loop(cmd))

    # review round 4: an assignment is only one the shell would make as written
    def test_an_assigned_path_the_shell_would_expand_is_unknown(self):
        for val in ("/a/`id`/tasks/b.output", "/a/$(id)/tasks/b.output", "/a/$D/tasks/b.output"):
            with self.subTest(val=val):
                cmd = "F=" + val + "; until grep -q ok $F; do sleep 5; done"
                self.assertIsNone(procs.wait_loop(cmd))

    def test_text_that_only_looks_like_an_assignment_is_not_one(self):
        for pre in ("echo F=" + TASKS + "b.output; ", "echo 'F=" + TASKS + "b.output'; "):
            with self.subTest(pre=pre):
                self.assertIsNone(procs.wait_loop(
                    pre + "until grep -q ok $F; do sleep 5; done"))

    def test_an_exported_assignment_is_one(self):                        # control
        cmd = "export F=" + TASKS + "b1.output && until grep -q ok $F; do sleep 5; done"
        self.assertEqual(procs.wait_loop(cmd)["files"], [TASKS + "b1.output"])

    def test_a_pattern_read_from_a_file_is_unknown(self):
        cmd = "until grep -qf pats " + TASKS + "b1.output; do sleep 5; done"
        self.assertIsNone(procs.wait_loop(cmd))

    def test_reading_a_task_file_once_is_not_a_loop(self):             # control
        self.assertIsNone(procs.wait_loop("tail -5 " + TASKS + "b1.output"))

    def test_a_relative_path_is_not_taken(self):
        # it would be read against ccwho's own directory, not the loop's
        self.assertIsNone(procs.wait_loop("until grep -q x tasks/b1.output; do sleep 5; done"))

    def test_a_path_that_climbs_out_is_not_taken(self):
        cmd = "until grep -q x /tmp/s/tasks/../../etc/tasks/b1.output; do sleep 5; done"
        self.assertIsNone(procs.wait_loop(cmd))

    def test_nothing_is_nothing(self):
        self.assertIsNone(procs.wait_loop(""))
        self.assertIsNone(procs.wait_loop(None))


class TestCannotEnd(unittest.TestCase):
    """A loop cannot end when every file it polls is finished - Claude Code wrote
    its last line - and has stayed that way longer than any loop sleeps. Unknown
    is never "cannot end": a file not read is a loop that may be fine."""
    ENDED = "   × §1 the real repo\n\n[exited with code 0]\n"

    def test_a_finished_file_left_alone_cannot_end(self):
        self.assertTrue(procs.cannot_end([(self.ENDED, 600)], grace=120))

    def test_a_killed_task_is_finished_too(self):
        self.assertTrue(procs.cannot_end([("partial\n[killed]\n", 600)], grace=120))

    def test_any_exit_code(self):
        self.assertTrue(procs.cannot_end([("x\n[exited with code 137]", 600)], grace=120))

    def test_a_file_still_being_written_can(self):                    # control
        self.assertFalse(procs.cannot_end([(" ✓ test/a.test.ts (3)\n", 600)], grace=120))

    def test_just_finished_gives_the_loop_its_next_look(self):        # control
        self.assertFalse(procs.cannot_end([(self.ENDED, 30)], grace=120))

    def test_a_marker_quoted_mid_file_is_not_the_end(self):           # control
        self.assertFalse(procs.cannot_end(
            [("echo '[exited with code 0]'\nstill going\n", 600)], grace=120))

    def test_a_marker_on_its_own_line_mid_file_is_not_the_end(self):  # control
        # a running task that prints another task's output
        self.assertFalse(procs.cannot_end(
            [("[exited with code 0]\nstill going\n", 600)], grace=120))

    def test_one_file_still_open_keeps_it_alive(self):                # control
        self.assertFalse(procs.cannot_end(
            [(self.ENDED, 600), ("running\n", 600)], grace=120))

    def test_a_file_it_could_not_read_is_unknown(self):               # control
        self.assertFalse(procs.cannot_end([None], grace=120))
        self.assertFalse(procs.cannot_end([(self.ENDED, 600), None], grace=120))

    def test_no_files_is_not_a_dead_loop(self):
        self.assertFalse(procs.cannot_end([], grace=120))



class TestKillPlan(unittest.TestCase):
    """What a kill would signal, and why everything else is spared - decided
    from one scan, before anything is signalled. Named targets act (you meant
    it); computed ones (clean, a session's work) take only what is certainly
    litter. Nothing here signals anything."""

    LIVE = LIVE_SID
    T = "Tue Sep 22 13:45:15 2026"

    def world(self, extra=None, marks=None, pipes=None):
        table = {10: (1, self.T, "/Users/x/.local/bin/claude"),          # a live session
                 11: (10, self.T, "node server.js"),                     # its work
                 20: (1, self.T, "node server.js --port 3000"),          # left behind
                 21: (1, self.T, "vite --port 5173"),                    # left behind
                 30: (1, self.T, "workerd serve"),                       # codex
                 40: (1, self.T, "python3 my_own.py"),                   # nobody's
                 50: (1, self.T, "/Applications/Docker.app/Contents/MacOS/com.docker.backend"),
                 60: (1, self.T, "gpg-agent --daemon"),                  # shared daemon
                 99: (1, self.T, "python3 ccwho.py kill 20")}            # ccwho itself
        table.update(extra or {})
        m = {11: ("claude", self.LIVE), 20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID),
             30: ("codex", "01a0-x"), 50: ("claude", DEAD_SID), 60: ("claude", DEAD_SID)}
        m.update(marks or {})
        sessions = [{"sessionId": self.LIVE, "pid": 10}]
        att = procs.attribute(table, m, {20: [3000], 21: [5173], 40: [8000], 50: [2375]},
                              sessions, own=99)
        return {"table": table, "att": att, "sessions": sessions, "own": 99,
                "ports": {20: [3000], 21: [5173], 40: [8000], 50: [2375]},
                "pipes": {**{q: set() for q in table}, **(pipes or {})}, "marks": m, "connections": []}

    def plan(self, mode, target, **kw):
        return procs.kill_plan(mode, target, self.world(**kw))

    def kills(self, plan):
        return sorted(p["pid"] for p in plan["kill"])

    def spared(self, plan):
        return {p["pid"]: p["why"] for p in plan["spare"]}

    # ---- a named pid: you meant it
    def test_a_named_pid_is_killed(self):
        self.assertEqual(self.kills(self.plan("pid", {"pid": 20, "start": self.T})), [20])

    def test_a_named_pid_of_your_own_is_killed_too(self):
        # the port message says "ccwho kill 4412 if you mean it": then it acts
        self.assertEqual(self.kills(self.plan("pid", {"pid": 40, "start": self.T})), [40])

    def test_a_named_live_session_is_not_killed(self):
        p = self.plan("pid", {"pid": 10, "start": self.T})
        self.assertEqual(self.kills(p), [])
        self.assertIn("does not kill a session", self.spared(p)[10])

    def test_ccwho_itself_is_not_killed(self):
        p = self.plan("pid", {"pid": 99, "start": self.T})
        self.assertEqual(self.kills(p), [])
        self.assertIn(99, self.spared(p))

    def test_a_reused_pid_is_not_killed(self):
        p = self.plan("pid", {"pid": 20, "start": "Wed Sep 23 09:00:00 2026"})
        self.assertEqual(self.kills(p), [])
        self.assertIn("now a different process", self.spared(p)[20])

    def test_a_gone_pid_is_said_so(self):
        p = self.plan("pid", {"pid": 777, "start": self.T})
        self.assertEqual(self.kills(p), [])
        self.assertIn("already exited", self.spared(p)[777])

    # ---- a port: its agent-started holders
    def test_a_port_kills_its_agent_started_holder(self):
        self.assertEqual(self.kills(self.plan("port", 3000)), [20])

    def note(self, plan, pid):
        return next(k.get("note", "") for k in plan["kill"] if k["pid"] == pid)

    def test_an_unmarked_holder_is_taken_with_a_note(self):
        p = self.plan("port", 8000, marks={40: None})       # read, no mark
        self.assertEqual(self.kills(p), [40])
        self.assertIn("no agent started it", self.note(p, 40))
        p = self.plan("port", 8000)                         # not read: said so
        self.assertEqual(self.kills(p), [40])
        self.assertIn("could not read who started it", self.note(p, 40))

    def test_a_not_sure_holder_is_taken_with_a_note(self):
        w = self.world()
        w["ports"][60] = [9999]                                         # gpg-agent
        p = procs.kill_plan("port", 9999, w)
        self.assertEqual(self.kills(p), [60])
        self.assertIn("not sure: it is a shared user daemon", self.note(p, 60))

    def test_a_port_nobody_holds_plans_nothing(self):                   # control
        p = self.plan("port", 4444)
        self.assertEqual((p["kill"], p["spare"]), ([], []))

    # ---- computed: clean, and a session's work
    def test_clean_takes_what_was_left_behind_only(self):
        p = self.plan("clean", None)
        self.assertEqual(self.kills(p), [20, 21])
        for pid in (10, 11, 30, 40, 50, 60, 99):
            self.assertNotIn(pid, self.kills(p))

    def test_a_sessions_work_is_that_session_only(self):
        # v1: session mode is not built; attribute still files the work
        self.assertEqual(self.kills(self.plan("session", self.LIVE)), [])
        self.assertEqual([p["pid"] for p in self.world()["att"]["sessions"][self.LIVE]], [11])

    def test_a_live_sessions_helpers_are_not_its_work(self):
        # its MCP servers: killing one breaks the session that is still running
        extra = {12: (10, self.T, "npm exec chrome-devtools-mcp@latest")}
        w = self.world(extra=extra, marks={12: ("claude", self.LIVE)})
        self.assertEqual(procs.kill_plan("session", self.LIVE, w)["kill"], [])
        self.assertEqual(self.kills(procs.kill_plan("pid", {"pid": 11, "start": self.T}, w)), [11])

    def test_a_computed_kill_never_takes_ccwho_or_a_session_whatever_it_is_given(self):
        # attribute() leaves them out; kill_plan does not rely on its caller
        w = self.world()
        w["att"] = dict(w["att"], left_behind=w["att"]["left_behind"] + [
            {"pid": 99, "session": DEAD_SID}, {"pid": 10, "session": DEAD_SID}])
        self.assertEqual(self.kills(procs.kill_plan("clean", None, w)), [20, 21])

    # ---- the pipe rule (plan, decided after #9's reviews)
    def test_a_process_feeding_a_live_one_says_so(self):
        # an ssh mux's ProxyCommand is an orphan whose stdin/stdout lead to the
        # `ssh: ... [mux]` master: killing it drops every connection on it
        extra = {70: (1, self.T, "ssh: /Users/u/.ssh/cm-x [mux]"),
                 71: (1, self.T, "aws ssm start-session --target i-1")}
        p = procs.kill_plan("clean", None, self.world(
            extra=extra, marks={71: ("claude", DEAD_SID)}, pipes={71: {70}}))
        self.assertIn("piped to 70", self.note(p, 71))                 # said; a person decides
        self.assertEqual(self.note(p, 20), "")                                     # control

    def test_a_pipe_to_another_target_is_no_reason_to_spare(self):     # control
        # `a | b`, both left behind: killing both is the whole pipeline
        p = procs.kill_plan("clean", None, self.world(pipes={20: {21}, 21: {20}}))
        self.assertEqual(self.kills(p), [20, 21])

    def test_a_pipe_to_a_gone_process_is_no_reason_to_spare(self):      # control
        # pipes are read at kill time: a gone peer is simply not in them
        p = procs.kill_plan("clean", None, self.world(pipes={20: set()}))
        self.assertIn(20, self.kills(p))

    def test_a_pipe_to_a_process_newer_than_the_scan_spares(self):
        # review: a peer lsof reports at kill time is alive, scan or no scan
        p = procs.kill_plan("clean", None, self.world(pipes={20: {555}}))
        self.assertIn("piped to 555 (a process newer than the scan - it may be an agent)", self.note(p, 20))

    def test_a_pipe_inside_the_kill_is_no_note(self):
        # 20 feeds 21, 21 feeds the live mux: only 21's pipe leads out
        extra = {70: (1, self.T, "ssh: /Users/u/.ssh/cm-x [mux]")}
        p = procs.kill_plan("clean", None, self.world(extra=extra,
                                                      pipes={20: {21}, 21: {70}}))
        self.assertIn("piped to 70", self.note(p, 21))
        self.assertNotIn("piped", self.note(p, 20))

    def test_the_pipe_rule_holds_for_a_port_too(self):
        extra = {70: (1, self.T, "ssh: /Users/u/.ssh/cm-x [mux]")}
        p = procs.kill_plan("port", 3000, self.world(extra=extra, pipes={20: {70}}))
        self.assertIn("piped to 70", self.note(p, 20))

    # ---- review: what must never be killed, in any mode
    def ccwho_under_a_shell(self, extra=None, marks=None):
        # an agent in live session L runs ccwho from its tool shell 12
        table = {12: (10, self.T, "/bin/zsh -c ccwho kill"), 99: (12, self.T, "python3 ccwho.py")}
        table.update(extra or {})
        return self.world(extra=table, marks={12: ("claude", self.LIVE), **(marks or {})})

    def test_the_shell_that_runs_ccwho_is_never_killed(self):
        w = self.ccwho_under_a_shell()
        p = procs.kill_plan("pid", {"pid": 12, "start": self.T}, w)
        self.assertNotIn(12, self.kills(p))
        self.assertIn("ccwho", self.spared(p)[12])
        self.assertIn(11, self.kills(procs.kill_plan("pid", {"pid": 11, "start": self.T}, w)))  # control

    def test_a_dead_shell_running_ccwho_is_not_cleaned(self):
        w = self.world(extra={25: (1, self.T, "bash"), 99: (25, self.T, "python3 ccwho.py clean")},
                       marks={25: ("claude", DEAD_SID)})
        self.assertNotIn(25, self.kills(procs.kill_plan("clean", None, w)))

    def test_a_claude_and_what_wraps_one_are_never_killed(self):
        # `caffeinate -i claude` in session L's work: killing it ends L2
        extra = {13: (10, self.T, "caffeinate -i claude"), 14: (13, self.T, "claude"),
                 15: (10, self.T, "/Users/x/.local/bin/claude -p hi")}
        w = self.world(extra=extra, marks={13: ("claude", self.LIVE), 14: ("claude", self.LIVE),
                                           15: ("claude", self.LIVE)})
        for pid in (13, 14, 15):
            p = procs.kill_plan("pid", {"pid": pid, "start": self.T}, w)
            self.assertEqual(self.kills(p), [])
            self.assertIn("claude", self.spared(p)[pid])
        p = procs.kill_plan("pid", {"pid": 14, "start": self.T}, w)
        self.assertEqual(self.kills(p), [])

    def test_an_empty_start_proves_nothing(self):
        w = self.world(extra={71: (1, "", "x")}, marks={71: ("claude", DEAD_SID)})
        p = procs.kill_plan("pid", {"pid": 71, "start": ""}, w)
        self.assertEqual(self.kills(p), [])
        self.assertIn(71, self.spared(p))
        self.assertEqual(self.kills(self.plan("pid", {"pid": 20, "start": self.T})), [20])  # control

    def test_nothing_it_leaves_is_left_unsaid(self):
        # every left-behind, not-sure or codex process clean does not take has
        # its reason
        w = self.world(extra={22: (20, self.T, "esbuild --service")},
                       marks={22: ("claude", DEAD_SID)})
        p = procs.kill_plan("clean", None, w)
        listed = {x["pid"] for g in ("left_behind", "unsure", "codex") for x in w["att"][g]}
        self.assertTrue(listed <= set(self.kills(p)) | set(self.spared(p)))

    def test_a_live_sessions_helper_holding_a_port_says_so(self):
        extra = {12: (10, self.T, "npm exec chrome-devtools-mcp@latest")}
        w = self.world(extra=extra, marks={12: ("claude", self.LIVE)})
        w["ports"][12] = [9222]
        w["att"] = procs.attribute(w["table"], dict(
            {11: ("claude", self.LIVE), 12: ("claude", self.LIVE)}), w["ports"],
            w["sessions"], own=99)
        p = procs.kill_plan("port", 9222, w)
        self.assertIn("helper", self.note(p, 12))

    def test_a_codex_holder_is_named_with_its_note(self):
        # v1: named, not killed - ccwho cannot tell whether its session still runs
        w = self.world()
        w["ports"][30] = [4000]
        p = procs.kill_plan("port", 4000, w)
        self.assertIn("Codex", self.note(p, 30))

    def test_the_port_notes_say_what_the_plan_knows(self):
        self.assertEqual(self.note(self.plan("port", 8000, marks={40: None}), 40), "no agent started it")
        self.assertEqual(self.note(self.plan("port", 2375), 50), "not sure: it is an app")

    def test_a_port_given_as_text_is_the_same_port(self):
        self.assertEqual(self.kills(self.plan("port", "3000")), [20])

    # ---- second review
    def live_world(self):
        # live session L's work holds what the doubt rules protect for a dead one
        extra = {60: (10, self.T, "gpg-agent --daemon"), 70: (1, self.T, "ssh: /u/.ssh/cm-h [mux]"),
                 71: (1, self.T, "ssh -W host:22 jump"), 80: (1, self.T, "tmux new-session -d"),
                 81: (80, self.T, "-zsh"), 82: (81, self.T, "vim notes.txt"),
                 90: (1, self.T, "ollama serve"), 91: (1, self.T, "git-credential-cache--daemon x")}
        marks = {pid: ("claude", self.LIVE) for pid in extra}
        w = self.world(extra=extra, marks=marks, pipes={70: {71}, 71: {70}})
        w["ports"][90] = [11434]
        w["att"] = procs.attribute(w["table"], {11: ("claude", self.LIVE), **marks},
                                   w["ports"], w["sessions"], own=99)
        return w

    def test_a_live_sessions_daemons_and_tmux_are_never_its_work_to_kill(self):
        w = self.live_world()
        self.assertEqual(procs.kill_plan("session", self.LIVE, w)["kill"], [])
        for pid in (60, 70, 71, 80, 81, 82, 90, 91):      # named, each says what it is
            k = procs.kill_plan("pid", {"pid": pid, "start": self.T}, w)["kill"]
            self.assertRegex(k[0].get("note", "") if k else "spared", r"daemon|ssh|tmux|spared", pid)
        p = procs.kill_plan("port", 11434, w)
        self.assertIn("shared user daemon", self.note(p, 90))

    def test_without_ccwho_in_the_scan_a_computed_kill_takes_nothing(self):
        for own in (None, 12345):
            with self.subTest(own=own):
                w = self.world()
                w["own"] = own
                for mode, target in (("clean", None), ("session", self.LIVE), ("port", 3000)):
                    p = procs.kill_plan(mode, target, w)
                    self.assertEqual(p["kill"], [], mode)
                    self.assertIn("ccwho", p["why"])
        self.assertEqual(self.kills(self.plan("clean", None)), [20, 21])        # control

    def test_every_form_of_claude_is_never_killed(self):
        forms = ("node /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js --resume",
                 "npm exec @anthropic-ai/claude-code", "/usr/local/bin/claude-code",
                 "claude.exe", "/Users/x/.local/share/claude/versions/2.1.282")
        for cmd in forms:
            with self.subTest(cmd=cmd):
                w = self.world(extra={16: (10, self.T, cmd), 17: (1, self.T, "caffeinate -i x"),
                                      18: (17, self.T, cmd)},
                               marks={16: ("claude", self.LIVE), 17: ("claude", DEAD_SID),
                                      18: ("claude", DEAD_SID)})
                self.assertEqual(self.kills(procs.kill_plan("pid", {"pid": 16, "start": self.T}, w)), [])
                self.assertFalse({17, 18} & set(self.kills(procs.kill_plan("clean", None, w))))

    def test_a_live_session_whatever_its_command_is_never_killed(self):
        w = self.world(extra={10: (1, self.T, "node /x/cli.js")})
        for mode, target in (("pid", {"pid": 10, "start": self.T}), ("clean", None),
                             ("port", 3000)):
            with self.subTest(mode=mode):
                self.assertNotIn(10, self.kills(procs.kill_plan(mode, target, w)))

    def test_pipes_not_read_is_said_for_every_target(self):
        w = self.world()
        w["pipes"] = None
        p = procs.kill_plan("clean", None, w)
        self.assertIn("pipes were not read", self.note(p, 20))
        self.assertNotIn("pipes", self.note(self.plan("clean", None), 20))      # control: read, none

    def test_a_named_pid_says_what_killing_it_means(self):
        extra = {70: (1, self.T, "ssh: /u/.ssh/cm-h [mux]")}
        w = self.world(extra=extra, marks={70: ("claude", DEAD_SID)}, pipes={20: {70}})
        notes = {pid: procs.kill_plan("pid", {"pid": pid, "start": self.T}, w)["kill"][0].get("note", "")
                 for pid in (20, 30, 60, 70)}
        self.assertIn("70", notes[20])                                          # feeds the mux
        self.assertIn("Codex", notes[30])
        self.assertIn("shared user daemon", notes[60])
        self.assertIn("shared user daemon", notes[70])
        self.assertNotIn("note", self.plan("pid", {"pid": 21, "start": self.T})["kill"][0])  # control

    def test_the_messages_are_the_plans(self):
        self.assertEqual(self.spared(self.plan("pid", {"pid": 20, "start": "Wed Sep 23 09:00:00 2026"}))[20],
                         "20 is now a different process - not killed; run ccwho ps again")
        self.assertEqual(self.plan("port", 4444)["why"], "nothing holds :4444")

    def test_bad_input_is_answered_never_raised(self):
        for mode, target in (("pid", {"pid": 20, "start": 12345}), ("pid", 20),
                             ("pid", {"pid": "20"}), ("port", ":abc"), ("session", "no-such"),
                             ("nonsense", None)):
            with self.subTest(mode=mode, target=target):
                p = procs.kill_plan(mode, target, self.world())
                self.assertEqual(p["kill"], [])
                self.assertTrue(p.get("why") or p["spare"], "an empty plan says why")

    def test_a_pid_given_as_text_is_the_same_pid(self):
        # from the command line or JSON a pid arrives as text
        self.assertEqual(self.kills(self.plan("pid", {"pid": "20", "start": self.T})), [20])

    def test_a_computed_kill_says_why_it_passed_over_what_is_not_sure(self):
        p = self.plan("clean", None)
        self.assertIn(60, self.spared(p))                                        # gpg-agent, unsure
        self.assertIn(50, self.spared(p))                                        # Docker, unsure

    # ---- third review
    FORMS = ("node /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js",
             "/usr/local/bin/claude-code", "node /x/@anthropic-ai/claude-agent-sdk/cli.js",
             "npm exec @anthropic-ai/claude-code@latest", "npx -y @anthropic-ai/claude-code@1.0.3")

    def test_every_claude_form_protects_its_work_in_attribute_too(self):
        # after /clear the claude runs on under a new id: its work carries the
        # old one, and attribute must know every form of claude, as kill_plan does
        for form in self.FORMS:
            with self.subTest(form=form):
                # 11 is an orphan (ppid 1): only its starter, 10, says it is work
                w = self.world(extra={10: (1, self.T, form), 11: (1, self.T, "node server.js")},
                               marks={11: ("claude", DEAD_SID, 10)})
                self.assertNotIn(11, self.kills(procs.kill_plan("clean", None, w)))
        w = self.world(extra={17: (1, self.T, "node server.js"),                 # control
                              11: (1, self.T, "node worker.js")},
                       marks={17: ("claude", DEAD_SID), 11: ("claude", DEAD_SID, 17)})
        self.assertIn(11, self.kills(procs.kill_plan("clean", None, w)))

    def test_a_package_that_is_not_claude_is_not_one(self):             # control
        self.assertFalse(procs._is_a_claude("npm exec @anthropic-ai/claude-code-foo"))

    def test_port_mode_says_what_attribute_is_not_sure_of(self):
        # 11's claude (10) still runs: attribute calls it not sure; so must port
        w = self.world(extra={10: (1, self.T, "/Users/x/.local/bin/claude"),
                              11: (10, self.T, "node server.js")},
                       marks={11: ("claude", DEAD_SID, 10)})
        w["sessions"] = []
        w["ports"][11] = [3001]
        w["att"] = procs.attribute(w["table"], {11: ("claude", DEAD_SID, 10), 20: ("claude", DEAD_SID)},
                                   w["ports"], [], own=99)
        p = procs.kill_plan("port", 3001, w)
        # 11 runs under 10, a claude no source lists: the nearest agent above it
        self.assertIn("not sure: it runs under a claude that ccwho does not list", self.note(p, 11))
        p = procs.kill_plan("pid", {"pid": 11, "start": self.T}, w)
        self.assertIn("not sure: it runs under a claude", p["kill"][0]["note"])
        self.assertEqual(self.kills(procs.kill_plan("port", 3000, w)), [20])      # control

    def test_a_named_pid_is_never_launchd_or_a_bool(self):
        for target in ({"pid": 1, "start": self.T}, {"pid": True, "start": self.T},
                       {"pid": 0, "start": self.T}):
            with self.subTest(target=target):
                w = self.world(extra={1: (0, self.T, "/sbin/launchd")})
                self.assertEqual(procs.kill_plan("pid", target, w)["kill"], [])

    def test_a_named_pid_under_ccwho_is_not_killed(self):
        w = self.world(extra={98: (99, self.T, "ps -axo pid")})
        p = procs.kill_plan("pid", {"pid": 98, "start": self.T}, w)
        self.assertEqual(self.kills(p), [])
        self.assertIn("ccwho", self.spared(p)[98])

    def test_a_live_sessions_ssh_transport_and_pinentry_are_spared(self):
        extra = {71: (10, self.T, "ssh -W h:22 j"), 60: (10, self.T, "gpg-agent --daemon"),
                 61: (60, self.T, "/opt/homebrew/bin/pinentry-mac")}
        marks = {pid: ("claude", self.LIVE) for pid in extra}
        w = self.world(extra=extra, marks=marks, pipes={})
        w["att"] = procs.attribute(w["table"], {11: ("claude", self.LIVE), **marks},
                                   w["ports"], w["sessions"], own=99)
        notes = {pid: procs.kill_plan("pid", {"pid": pid, "start": self.T}, w)["kill"][0]
                 .get("note", "") for pid in (71, 61, 11)}
        self.assertIn("ssh connection", notes[71])
        self.assertIn("runs under a shared user daemon", notes[61])
        self.assertNotRegex(notes[11], "ssh|daemon")                             # control

    def test_an_empty_plan_always_says_why(self):
        w = self.world()
        self.assertEqual(procs.kill_plan("port", ":4444", w).get("why"), "nothing holds :4444")
        for mode, target in (("port", 4444), ("session", self.LIVE)):
            with self.subTest(mode=mode):
                if mode == "session":
                    w["att"] = dict(w["att"], sessions={self.LIVE: []})
                p = procs.kill_plan(mode, target, w)
                self.assertTrue(p["kill"] or p["spare"] or p.get("why"))
        w = self.world()
        w["att"] = dict(w["att"], left_behind=[])
        p = procs.kill_plan("clean", None, w)
        self.assertTrue(p["spare"] or p.get("why"))
        self.assertIn(30, self.spared(self.plan("clean", None)))                 # codex is named

    def test_what_it_echoes_has_no_control_characters(self):
        for mode, target in (("nonsense\x1b]0;x\x07", None), ("session", "a\x1b[2J")):
            with self.subTest(mode=mode):
                p = procs.kill_plan(mode, target, self.world())
                self.assertNotRegex(p.get("why", ""), r"[\x00-\x1f\x7f-\x9f]")

    def test_a_path_among_the_arguments_is_no_agent(self):
        # an agent is known by the program that runs, not a folder it cds into
        w = self.world(extra={26: (1, self.T, "sh -c cd /Users/x/src/claude-code && npm run dev")},
                       marks={26: ("claude", DEAD_SID)})
        p = procs.kill_plan("pid", {"pid": 26, "start": self.T}, w)
        self.assertEqual(self.kills(p), [26])

    # ---- fourth review
    def test_a_live_sessions_wrapper_is_never_killed_whatever_its_command(self):
        # a dev build or a symlink name: the live session's own command need not
        # look like claude, and killing `script` above it hangs it up
        for wrapper, cmd in (("script -q /tmp/log node /u/src/claude-cli/dist/cli.js",
                              "node /u/src/claude-cli/dist/cli.js"),
                             ("caffeinate -i cc", "cc")):
            with self.subTest(cmd=cmd):
                w = self.world(extra={11: (1, self.T, wrapper), 12: (11, self.T, cmd)},
                               marks={11: ("claude", DEAD_SID)})
                w["sessions"] = [{"sessionId": self.LIVE, "pid": 12}]
                w["att"] = procs.attribute(w["table"], {11: ("claude", DEAD_SID),
                                                        20: ("claude", DEAD_SID)},
                                           w["ports"], w["sessions"], own=99)
                self.assertNotIn(11, [p["pid"] for p in w["att"]["left_behind"]])
                p = procs.kill_plan("clean", None, w)
                self.assertNotIn(11, self.kills(p))
                self.assertIn(20, self.kills(p))                                # control
                self.assertEqual(self.kills(procs.kill_plan("pid", {"pid": 11, "start": self.T}, w)), [])

    FORMS4 = ("node /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js",
              "npm exec @anthropic-ai/claude-code@latest",
              "node /x/@anthropic-ai/claude-agent-sdk/cli.js", "/usr/local/bin/claude-code")

    def test_attribute_knows_every_claude_form_at_each_rule(self):
        for form in self.FORMS4:
            with self.subTest(form=form):
                # the form itself, what runs under it, what wraps it - no starter
                table = {5: (1, self.T, form), 6: (5, self.T, "zsh -c npm run dev"),
                         7: (6, self.T, "node vite"), 8: (1, self.T, "caffeinate -i x"),
                         9: (8, self.T, form)}
                marks = {pid: ("claude", DEAD_SID) for pid in (5, 6, 7, 8)}
                w = procs.attribute(table, marks, {}, [])
                why = {p["pid"]: p["why"] for p in w["unsure"]}
                self.assertEqual(w["left_behind"], [])
                self.assertIn("claude that ccwho does not list", why[5])
                self.assertIn("runs under a claude", why[7])
                self.assertIn("a claude runs under it", why[8])

    def test_a_named_pid_needs_ccwho_in_the_scan_too(self):
        w = self.world()
        w["own"] = 12345
        p = procs.kill_plan("pid", {"pid": 20, "start": self.T}, w)
        self.assertEqual(p["kill"], [])
        self.assertIn("ccwho", p.get("why", ""))

    def test_a_pid_that_is_not_whole_is_not_a_pid(self):
        p = self.plan("pid", {"pid": 20.9, "start": self.T})
        self.assertEqual((p["kill"], p.get("why")), ([], "no pid given"))

    def test_what_looks_like_claude_but_is_no_session_is_left_alone_plainly(self):
        w = self.world(extra={27: (1, self.T, "claude -p hi")},
                       marks={27: ("claude", DEAD_SID)})
        why = self.spared(procs.kill_plan("pid", {"pid": 27, "start": self.T}, w))[27]
        self.assertNotIn("ccwho stop", why)
        self.assertIn("ccwho does not kill", why)

    # ---- fifth review
    def test_work_under_a_live_session_is_that_sessions_whatever_its_mark(self):
        # live B was started from A's shell; its work may carry A's mark. The
        # process tree says whose it is: the nearest live session above it
        for b_cmd in ("claude", "node /u/dev/claude-code/dist/cli.js", "/Users/u/bin/cc"):
            with self.subTest(b=b_cmd):
                B = "bbbb2222-0000-4000-8000-000000000002"
                table = {150: (1, self.T, "zsh"), 200: (150, self.T, b_cmd),
                         210: (200, self.T, "npx -y some-mcp-server"),
                         220: (200, self.T, "zsh -c npm run dev"), 221: (220, self.T, "node server.js"),
                         300: (1, self.T, "node orphan.js"), 99: (1, self.T, "python3 ccwho.py")}
                marks = {pid: ("claude", DEAD_SID) for pid in (150, 210, 220, 221, 300)}
                sessions = [{"sessionId": B, "pid": 200}]
                att = procs.attribute(table, marks, {}, sessions, own=99)
                self.assertEqual(sorted(p["pid"] for p in att["sessions"][B]), [210, 220, 221])
                w = {"table": table, "att": att, "sessions": sessions, "own": 99,
                     "ports": {}, "pipes": {q: set() for q in table}, "marks": marks, "connections": []}
                self.assertEqual(self.kills(procs.kill_plan("clean", None, w)), [300])   # control

    def test_another_live_sessions_work_is_not_this_ones(self):
        A, B = self.LIVE, "bbbb2222-0000-4000-8000-000000000002"
        table = {100: (1, self.T, "claude"), 150: (100, self.T, "zsh -c claude"),
                 200: (150, self.T, "claude"), 210: (200, self.T, "npx -y foo-mcp"),
                 220: (200, self.T, "node server.js"), 110: (100, self.T, "node mine.js"),
                 99: (1, self.T, "python3 ccwho.py")}
        marks = {pid: ("claude", A) for pid in (150, 210, 220, 110)}
        sessions = [{"sessionId": A, "pid": 100}, {"sessionId": B, "pid": 200}]
        ports = {220: [3000]}
        att = procs.attribute(table, marks, ports, sessions, own=99)
        self.assertEqual(sorted(p["pid"] for p in att["sessions"][A]), [110, 150])
        self.assertEqual(sorted(p["pid"] for p in att["sessions"][B]), [210, 220])

    def test_a_computed_kill_needs_a_start_time_too(self):
        w = self.world(extra={300: (1, "", "sleep 1000")}, marks={300: ("claude", DEAD_SID)})
        w["att"]["left_behind"].append({"pid": 555, "session": DEAD_SID})
        p = procs.kill_plan("clean", None, w)
        self.assertNotIn(300, self.kills(p))
        self.assertNotIn(555, self.kills(p))
        self.assertIn("cannot prove", self.spared(p)[300])
        self.assertIn(20, self.kills(p))                                          # control

    def test_a_named_pid_says_when_its_pipes_were_not_read(self):
        w = self.world()
        w["pipes"] = None
        k = procs.kill_plan("pid", {"pid": 20, "start": self.T}, w)["kill"][0]
        self.assertIn("pipes were not read", k.get("note", ""))
        self.assertNotIn("note", self.plan("pid", {"pid": 21, "start": self.T})["kill"][0])  # control

    def test_odd_digits_and_odd_pipes_are_answered_never_raised(self):
        for raw in ("²", "9" * 5000, "٣"):
            with self.subTest(raw=raw):
                p = self.plan("pid", {"pid": raw, "start": self.T})
                self.assertEqual((p["kill"], p.get("why")), ([], "no pid given"))
        p = procs.kill_plan("clean", None, self.world(pipes={20: [400, "a"]}))
        self.assertIn("not in its shape", p["why"])          # a text peer: out of shape
        w = self.world()
        # a session with no id, and a group filed under None: `session None` is no session
        w["sessions"] = w["sessions"] + [{"pid": 12}]
        w["att"] = dict(w["att"], sessions={**w["att"]["sessions"],
                                            None: w["att"]["sessions"][self.LIVE]})
        p = procs.kill_plan("session", None, w)
        self.assertEqual(p["kill"], [])

    # ---- sixth review
    def test_an_unlisted_claude_between_keeps_its_own_work(self):
        # the nearest-live-session walk must stop at the FIRST claude above
        C = "cccc3333-0000-4000-8000-000000000003"
        for mid in ("node /x/@anthropic-ai/claude-agent-sdk/cli.js", "claude -p fix it"):
            with self.subTest(mid=mid):
                table = {10: (1, self.T, "claude"), 11: (10, self.T, "zsh"),
                         20: (11, self.T, mid), 21: (20, self.T, "node server.js"),
                         99: (1, self.T, "python3 ccwho.py")}
                marks = {11: ("claude", self.LIVE), 21: ("claude", C, 20)}
                sessions = [{"sessionId": self.LIVE, "pid": 10}]
                ports = {21: [3000]}
                att = procs.attribute(table, marks, ports, sessions, own=99)
                self.assertIn(21, [p["pid"] for p in att["unsure"]])
                w = {"table": table, "att": att, "sessions": sessions, "own": 99,
                     "ports": ports, "pipes": {q: set() for q in table}, "marks": marks, "connections": []}
                p = procs.kill_plan("port", 3000, w)
                self.assertIn("runs under a", self.note(p, 21))
                self.assertEqual(self.kills(p), [21])        # never the agent above it

    def test_a_session_in_a_gui_terminal_can_have_its_work_killed(self):
        # every terminal app runs from .app/Contents/: what is ABOVE the claude
        # is not a reason to doubt what it started
        table = {1000: (1, self.T, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"),
                 1001: (1000, self.T, "login -pf u"), 1002: (1001, self.T, "-zsh"),
                 10: (1002, self.T, "claude"), 11: (10, self.T, "zsh -c npm run dev"),
                 12: (11, self.T, "node vite --port 3000"), 99: (1, self.T, "python3 ccwho.py"),
                 13: (10, self.T, "tmux new -d"), 14: (13, self.T, "node in-tmux.js")}
        marks = {11: ("claude", self.LIVE), 12: ("claude", self.LIVE),
                 13: ("claude", self.LIVE), 14: ("claude", self.LIVE)}
        sessions = [{"sessionId": self.LIVE, "pid": 10}]
        ports = {12: [3000]}
        w = {"table": table, "att": procs.attribute(table, marks, ports, sessions, own=99),
             "sessions": sessions, "own": 99, "ports": ports, "pipes": {q: set() for q in table}, "marks": marks, "connections": []}
        # the terminal app above the claude is no doubt
        self.assertNotIn("app", procs.kill_plan("pid", {"pid": 12, "start": self.T}, w)["kill"][0]
                         .get("note", ""))
        # control: tmux the session itself started still is
        k = procs.kill_plan("pid", {"pid": 14, "start": self.T}, w)["kill"][0]
        self.assertIn("runs in an app or tmux", k.get("note", ""))

    def test_a_running_codex_agent_is_never_killed(self):
        extra = {20: (11, self.T, "codex exec fix the tests"),
                 22: (11, self.T, "node /x/node_modules/@openai/codex/bin/codex.js")}
        w = self.world(extra=extra, marks={20: ("claude", self.LIVE), 22: ("claude", self.LIVE)})
        for pid in (20, 22):
            p = procs.kill_plan("pid", {"pid": pid, "start": self.T}, w)
            self.assertEqual(self.kills(p), [])
            self.assertIn("Codex", self.spared(p)[pid])
        self.assertEqual(self.kills(procs.kill_plan("pid", {"pid": 20, "start": self.T}, w)), [])

    def test_what_wraps_a_codex_agent_is_never_killed(self):
        # killing the `script` above it hangs the agent up
        w = self.world(extra={40: (1, self.T, "script -q /tmp/log zsh"),
                              41: (40, self.T, "codex exec fix the tests")},
                       marks={40: ("claude", DEAD_SID)})
        p = procs.kill_plan("clean", None, w)
        self.assertNotIn(40, self.kills(p))
        self.assertIn("Codex agent runs under", self.spared(p)[40])
        self.assertIn(20, self.kills(p))                                         # control

    def test_work_from_before_clear_is_the_live_sessions(self):
        # the same claude runs on under a new id: its old work is its work,
        # clean leaves it; reparented away from it, it stays not sure
        table = {10: (1, self.T, "claude"), 11: (10, self.T, "zsh"), 21: (11, self.T, "node a.js"),
                 22: (1, self.T, "node b.js"), 99: (1, self.T, "python3 ccwho.py")}
        marks = {11: ("claude", self.LIVE), 21: ("claude", DEAD_SID, 10),
                 22: ("claude", DEAD_SID, 10)}
        sessions = [{"sessionId": self.LIVE, "pid": 10}]
        att = procs.attribute(table, marks, {}, sessions, own=99)
        self.assertIn(21, [p["pid"] for p in att["sessions"][self.LIVE]])
        self.assertIn(22, [p["pid"] for p in att["unsure"]])
        w = {"table": table, "att": att, "sessions": sessions, "own": 99, "ports": {}, "pipes": {q: set() for q in table}, "marks": marks, "connections": []}
        self.assertNotIn(21, self.kills(procs.kill_plan("clean", None, w)))

    # ---- seventh review: one rule - a process belongs to the nearest agent above it
    CODEX_TREE = {500: (1, "T0", "node /opt/homebrew/bin/codex"),
                  501: (500, "T0", "/opt/homebrew/lib/node_modules/@openai/codex/vendor/x/codex/codex"),
                  502: (501, "T0", "/bin/bash -lc npm run dev"),
                  503: (502, "T0", "node vite --port 5173")}

    def codex_world(self, codex_alive=True):
        table = {k: (pp, self.T, c) for k, (pp, _s, c) in self.CODEX_TREE.items()}
        if not codex_alive:
            table = {k: ((1 if k == 502 else pp), s, c) for k, (pp, s, c) in table.items()
                     if k in (502, 503)}
        table[99] = (1, self.T, "python3 ccwho.py")
        marks = {k: ("claude", DEAD_SID) for k in table if k != 99}
        ports = {503: [5173]}
        att = procs.attribute(table, marks, ports, [], own=99)
        return {"table": table, "att": att, "sessions": [], "own": 99, "ports": ports,
                "pipes": {q: set() for q in table}, "marks": marks, "connections": []}

    def test_a_running_codex_agents_work_is_never_left_behind(self):
        w = self.codex_world()
        self.assertEqual(w["att"]["left_behind"], [])
        self.assertEqual(self.kills(procs.kill_plan("clean", None, w)), [])
        p = procs.kill_plan("port", 5173, w)            # named: taken, and said whose it is
        self.assertEqual(self.kills(p), [503])
        self.assertIn("Codex", self.note(p, 503))
        gone = self.codex_world(codex_alive=False)                                # control
        self.assertEqual(self.kills(procs.kill_plan("clean", None, gone)), [502, 503])

    def test_an_sdk_agents_work_under_a_live_session_is_not_that_sessions(self):
        # the SDK agent's work carries A's mark; the nearest agent above it is the SDK
        table = {10: (1, self.T, "claude"), 11: (10, self.T, "zsh -c python3 bot.py"),
                 20: (11, self.T, "node /x/@anthropic-ai/claude-agent-sdk/cli.js"),
                 21: (20, self.T, "zsh -c npm test"), 22: (21, self.T, "node jest"),
                 12: (10, self.T, "zsh -c npm run dev"), 99: (1, self.T, "python3 ccwho.py")}
        marks = {k: ("claude", self.LIVE) for k in (11, 20, 21, 22, 12)}
        sessions = [{"sessionId": self.LIVE, "pid": 10}]
        att = procs.attribute(table, marks, {}, sessions, own=99)
        self.assertEqual([p["pid"] for p in att["sessions"][self.LIVE]], [11, 12])  # control
        self.assertTrue({21, 22} <= {p["pid"] for p in att["unsure"]})

    def test_what_wraps_a_codex_agent_is_not_left_behind_in_the_list_either(self):
        table = {40: (1, self.T, "script -q /tmp/log zsh"), 41: (40, self.T, "codex exec x")}
        att = procs.attribute(table, {40: ("claude", DEAD_SID)}, {}, [])
        self.assertEqual(att["left_behind"], [])

    def test_the_kill_entry_carries_the_real_mark(self):
        # /clear: listed as A's work, but its environment says D - the
        # signaller compares against D
        table = {10: (1, self.T, "claude"), 11: (10, self.T, "zsh"), 12: (11, self.T, "node s.js"),
                 99: (1, self.T, "python3 ccwho.py")}
        marks = {11: ("claude", self.LIVE), 12: ("claude", DEAD_SID, 10)}
        sessions = [{"sessionId": self.LIVE, "pid": 10}]
        ports = {12: [3000]}
        att = procs.attribute(table, marks, ports, sessions, own=99)
        w = {"table": table, "att": att, "sessions": sessions, "own": 99, "ports": ports, "pipes": {q: set() for q in table}, "marks": marks, "connections": []}
        k = procs.kill_plan("pid", {"pid": 12, "start": self.T}, w)["kill"][0]
        self.assertEqual((k["session"], k["marked"]), (self.LIVE, DEAD_SID))

    def test_a_pipe_to_an_agent_spares_too(self):
        # v1, review 8: `claude -p x | tee` - kill the tee and the claude dies
        w = self.world(pipes={20: {10}, 10: {20}})         # as lsof reads it: both sides
        self.assertNotIn(20, self.kills(procs.kill_plan("clean", None, w)))
        extra = {70: (1, self.T, "ssh: /u/.ssh/cm-h [mux]")}                      # control
        w = self.world(extra=extra, pipes={20: {70}})
        self.assertIn(20, self.kills(procs.kill_plan("clean", None, w)))       # a person decides

    def test_an_agent_is_known_by_the_program_that_runs(self):
        for cmd in ("/bin/zsh -c cd /Users/u/src/codex && npm run dev",
                    "sh -c cd /Users/x/src/claude-code && npm run dev",
                    "vim /Users/u/claude", "git -C /Users/u/codex status",
                    "/usr/bin/git /Users/u/claude", "/usr/bin/vim notes about claude"):
            with self.subTest(cmd=cmd):
                self.assertFalse(procs._is_an_agent(cmd))
        for cmd in ("claude", "/Users/x/.local/bin/claude --resume x", "claude -p hi",
                    "/Users/x/.local/share/claude/versions/2.1.282", "claude-code", "claude.exe",
                    "node /opt/homebrew/lib/node_modules/@anthropic-ai/claude-code/cli.js",
                    "npm exec @anthropic-ai/claude-code@latest",
                    "npx -y @anthropic-ai/claude-code@1.0.3",
                    "node /x/@anthropic-ai/claude-agent-sdk/cli.js", "codex exec fix",
                    "node /opt/homebrew/bin/codex",
                    "/opt/homebrew/lib/node_modules/@openai/codex/vendor/x/codex/codex",
                    "/Users/me/Library/Application Support/Claude/claude-code/2.1.280/claude --x"):
            with self.subTest(agent=cmd):
                self.assertTrue(procs._is_an_agent(cmd))

    def test_a_lone_codex_agent_is_not_left_behind(self):
        att = procs.attribute({45: (1, self.T, "codex exec fix")},
                              {45: ("claude", DEAD_SID)}, {}, [])
        self.assertEqual(att["left_behind"], [])
        self.assertIn("Codex agent that ccwho does not list", att["unsure"][0]["why"])

    def test_a_relative_path_is_no_agent_either(self):
        for cmd in ("cat notes/claude", "less logs/codex"):
            with self.subTest(cmd=cmd):
                self.assertFalse(procs._is_an_agent(cmd))

    def test_a_pipe_to_an_unlisted_agent_spares_too(self):
        w = self.world(extra={46: (1, self.T, "claude -p summarize")}, pipes={20: {46}, 46: {20}})
        self.assertNotIn(20, self.kills(procs.kill_plan("clean", None, w)))

    def test_the_plan_carries_the_mark_the_signaller_rechecks(self):
        k = self.plan("port", 3000)["kill"][0]
        self.assertEqual((k["harness"], k["session"]), ("claude", DEAD_SID))

    def test_the_plan_carries_what_the_message_needs(self):
        k = self.plan("port", 3000)["kill"][0]
        for key in ("pid", "start", "command", "ports"):
            self.assertIn(key, k)
        self.assertEqual(k["ports"], [3000])


class TestKillPlanV1(unittest.TestCase):
    """v1, after 8 review rounds (owner's choice, 2026-09-25): without a named
    pid ccwho kills only a left-behind ORPHAN with no agent above or below it and
    no pipe to any live process. `kill <pid>` acts on what you name. Session mode
    is not built."""

    T = "Tue Sep 22 13:45:15 2026"

    def world(self, extra=None, marks=None, pipes=None, sessions=None, ports=None):
        table = {10: (1, self.T, "claude"), 11: (10, self.T, "node dev.js"),
                 20: (1, self.T, "node server.js --port 3000"),
                 21: (20, self.T, "esbuild --service"),
                 99: (1, self.T, "python3 ccwho.py")}
        table.update(extra or {})
        m = {11: ("claude", LIVE_SID), 20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID)}
        m.update(marks or {})
        s = sessions if sessions is not None else [{"sessionId": LIVE_SID, "pid": 10}]
        p = ports if ports is not None else {20: [3000], 11: [5173]}
        return {"table": table, "att": procs.attribute(table, m, p, s, own=99),
                "sessions": s, "own": 99, "ports": p,
                "pipes": {**{q: set() for q in table}, **pipes} if pipes is not None else
                         {q: set() for q in table}, "marks": m, "connections": []}

    def kills(self, plan):
        return sorted(p["pid"] for p in plan["kill"])

    def spared(self, plan):
        return {p["pid"]: p["why"] for p in plan["spare"]}

    def note(self, plan, pid):
        return next(k.get("note", "") for k in plan["kill"] if k["pid"] == pid)

    def test_session_mode_is_not_built(self):
        p = procs.kill_plan("session", LIVE_SID, self.world())
        self.assertEqual(p["kill"], [])
        self.assertIn("ccwho kill <pid>", p["why"])

    def test_clean_takes_an_orphan_and_what_runs_under_it(self):
        # review 9: the tree is the unit - the orphan alone left the rest running
        p = procs.kill_plan("clean", None, self.world())
        self.assertEqual(self.kills(p), [20, 21])

    def test_a_pipe_to_an_agent_spares_an_orphan(self):
        # an agent reading or feeding it included: `claude -p | tee`
        extra = {30: (1, self.T, "claude -p x")}
        for peer in (30, 10):
            with self.subTest(peer=peer):
                p = procs.kill_plan("clean", None, self.world(extra=extra, pipes={20: {peer}, peer: {20}}))
                self.assertNotIn(20, self.kills(p))
        p = procs.kill_plan("clean", None, self.world(extra=extra, pipes={20: {11}}))
        self.assertIn("piped to 11", self.note(p, 20))           # no agent: said, a person decides
        self.assertIn(20, self.kills(procs.kill_plan("clean", None, self.world(pipes={20: set()}))))

    def test_a_port_takes_its_holder_and_says_whose(self):
        self.assertEqual(self.kills(procs.kill_plan("port", 3000, self.world())), [20, 21])
        p = procs.kill_plan("port", 5173, self.world())                   # a live session's work
        self.assertEqual(self.kills(p), [11])
        self.assertEqual(self.note(p, 11), "it is the work of a live session")

    def test_a_port_held_by_codex_says_so(self):
        w = self.world(extra={30: (1, self.T, "workerd serve")}, marks={30: ("codex", "01a0-x")},
                       ports={30: [4000]})
        p = procs.kill_plan("port", 4000, w)
        self.assertEqual(self.kills(p), [30])
        self.assertIn("Codex", self.note(p, 30))

    def test_a_named_pid_still_acts(self):
        p = procs.kill_plan("pid", {"pid": 11, "start": self.T}, self.world())
        self.assertEqual(self.kills(p), [11])

    def test_a_named_pid_that_feeds_an_agent_is_spared(self):
        extra = {30: (1, self.T, "claude -p x")}
        p = procs.kill_plan("pid", {"pid": 20, "start": self.T}, self.world(extra=extra, pipes={20: {30}, 30: {20}}))
        self.assertEqual(p["kill"], [])                  # killing it would end the agent
        self.assertIn("20 is piped to 30, an agent", self.spared(p)[20])

    # ---- the agent detector, for the live list and the orphans alike
    NOT_AGENTS = ("npm --prefix /Users/u/src/codex run dev", "pnpm --filter codex dev",
                  "yarn --cwd /x/codex dev", "bun --cwd codex dev", "npm run -w codex dev",
                  "npm -w claude run dev", "npm run codex", "yarn claude",
                  "node /Users/u/src/codex", "node /Users/u/proj/scripts/claude",
                  "/usr/bin/git clone https://github.com/openai/codex",
                  "/opt/homebrew/bin/code src/claude", "/usr/bin/ssh host bin/claude")
    AGENTS = {"node --require /x/otel.js /x/node_modules/@anthropic-ai/claude-code/cli.js -p hi": "claude",
              "node -r ts-node/register /x/node_modules/@anthropic-ai/claude-code/cli.js": "claude",
              "node --max-old-space-size 8192 /x/@anthropic-ai/claude-code/cli.js": "claude",
              "/Users/u/Library/Application Support/fnm/node-v22/bin/node "
              "/x/@anthropic-ai/claude-code/cli.js": "claude",
              "/Users/u/Library/Application Support/fnm/node-v22/bin/node "
              "/x/@openai/codex/bin/codex.js": "Codex agent",
              "tsx /x/@anthropic-ai/claude-code/cli.js": "claude",
              "node /opt/homebrew/bin/codex": "Codex agent",
              "node /Users/u/.npm-global/bin/claude": "claude"}

    def test_a_port_held_under_an_orphan_takes_the_orphans_tree(self):
        p = procs.kill_plan("port", 7000, self.world(ports={20: [3000], 21: [7000]}))
        self.assertEqual(self.kills(p), [20, 21])

    def test_a_port_held_by_a_non_orphan_takes_it_alone(self):
        # its parent still runs: not an orphan's tree - the holder and what it runs
        w = self.world(extra={24: (11, self.T, "node child.js")}, marks={24: ("claude", DEAD_SID)},
                       ports={24: [7001]})
        p = procs.kill_plan("port", 7001, w)
        self.assertEqual(self.kills(p), [24])
        self.assertIn("a live session", self.note(p, 24))

    def test_a_runner_reads_past_its_flags_and_names_its_package(self):
        self.assertEqual(procs._agent_kind("npm --cache /tmp/c exec @anthropic-ai/claude-code"), "claude")
        self.assertEqual(procs._agent_kind("npx -p @anthropic-ai/claude-code claude"), "claude")
        self.assertIsNone(procs._agent_kind("npm install @anthropic-ai/claude-code"))    # installs

    def test_launcher_flags_and_script_names_are_no_agents(self):
        for cmd in self.NOT_AGENTS:
            with self.subTest(cmd=cmd):
                self.assertIsNone(procs._agent_kind(cmd))

    def test_every_launch_form_is_found(self):
        for cmd, kind in self.AGENTS.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(procs._agent_kind(cmd), kind)

    def test_codex_work_under_its_own_codex_stays_in_the_codex_line(self):
        table = {699: (1, self.T, "-zsh"), 700: (699, self.T, "codex"),
                 701: (700, self.T, "node mcp.js"), 702: (700, self.T, "bash -lc npm test")}
        att = procs.attribute(table, {701: ("codex", "th1"), 702: ("codex", "th1")}, {}, [])
        self.assertEqual(sorted(p["pid"] for p in att["codex"]), [701, 702])


class TestKillPlanV1Review9(unittest.TestCase):
    """Review 9 of v1: a kill takes an orphan's whole subtree or none of it;
    an agent is known by its children's marks too; a subtree with a person's
    shell in it is never litter."""

    T = "Tue Sep 22 13:45:15 2026"
    E = "eeee5555-0000-4000-8000-000000000005"

    def plan(self, mode, target, table, marks, ports=None, pipes=None, sessions=()):
        table = {**table, 99: (1, self.T, "python3 ccwho.py")}
        att = procs.attribute(table, marks, ports or {}, list(sessions), own=99)
        return procs.kill_plan(mode, target, {"table": table, "att": att, "sessions": list(sessions),
                                              "own": 99, "ports": ports or {},
                                              "pipes": {**{q: set() for q in table}, **pipes}
                                              if pipes is not None else {q: set() for q in table},
                                              "marks": marks, "connections": []})

    def kills(self, p):
        return sorted(k["pid"] for k in p["kill"])

    NPM = {20: (1, "Tue Sep 22 13:45:15 2026", "/bin/zsh -c -l source /u/.claude/shell-snapshots/s.sh && eval 'npm run dev'"),
           21: (20, "Tue Sep 22 13:45:15 2026", "npm run dev"),
           22: (21, "Tue Sep 22 13:45:15 2026", "node /u/app/node_modules/.bin/vite")}

    def npm_marks(self, child_mark=None):
        return {20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID),
                22: ("claude", child_mark or DEAD_SID)}

    def test_clean_takes_the_whole_left_behind_tree(self):
        # SIGTERM to `zsh -c` is not passed on: killing only it left :5173 held
        p = self.plan("clean", None, self.NPM, self.npm_marks(), ports={22: [5173]})
        self.assertEqual(self.kills(p), [20, 21, 22])
        for s in p["spare"]:
            self.assertNotIn("ends with its parent", s["why"])

    def test_a_port_takes_the_holders_whole_tree(self):
        p = self.plan("port", 5173, self.NPM, self.npm_marks(), ports={22: [5173]})
        self.assertEqual(self.kills(p), [20, 21, 22])

    def test_one_member_in_doubt_spares_the_whole_tree(self):
        table = {**self.NPM, 23: (21, self.T, "claude -p x")}
        p = self.plan("clean", None, table, {**self.npm_marks(), 23: ("claude", DEAD_SID)},
                      ports={22: [5173]})
        self.assertEqual(p["kill"], [])
        self.assertIn(20, {s["pid"] for s in p["spare"]})

    def test_children_marked_by_another_session_mean_an_agent_runs_here(self):
        # a dev build, a symlink, gemini, opencode: no name list covers them, but
        # an agent gives its children its own session id
        for cmd in ("node /Users/u/src/claude-cli/dist/cli.js -p long task", "/Users/u/bin/cl -p x",
                    "node /opt/homebrew/bin/gemini -p x", "opencode run x"):
            with self.subTest(cmd=cmd):
                table = {20: (1, self.T, cmd), 21: (20, self.T, "zsh -c npm test")}
                p = self.plan("clean", None, table, {20: ("claude", DEAD_SID), 21: ("claude", self.E)})
                self.assertEqual(p["kill"], [])
                self.assertIn("an agent runs here", " ".join(s["why"] for s in p["spare"]))
        table = {20: (1, self.T, "node server.js"), 21: (20, self.T, "node worker.js")}      # control
        p = self.plan("clean", None, table, {20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID)})
        self.assertEqual(self.kills(p), [20, 21])

    def test_a_tree_with_a_persons_shell_in_it_says_so(self):
        for host in ("dtach -n /tmp/work -z bash", "mosh-server new -s", "ttyd -p 7681 bash",
                     "/usr/local/bin/code tunnel", "/usr/sbin/sshd -D -p 2222"):
            for shell in ("-bash", "bash", "/bin/zsh -i"):
                with self.subTest(host=host, shell=shell):
                    table = {20: (1, self.T, host), 21: (20, self.T, shell),
                             22: (21, self.T, "vim notes.md")}
                    marks = {k: ("claude", DEAD_SID) for k in (20, 21, 22)}
                    p = self.plan("clean", None, table, marks)
                    notes = {k["pid"]: k.get("note", "") for k in p["kill"]}
                    self.assertIn("session host", notes[20])
                    self.assertIn("a shell someone may be working in", notes[21])
        p = self.plan("clean", None, self.NPM, self.npm_marks())                        # control
        self.assertFalse(any(k.get("note") for k in p["kill"]))

    def test_more_shared_daemons_are_named(self):
        for cmd in ("adb -L tcp:5037 fork-server server --reply-fd 4",
                    "/usr/bin/java -Xmx512m -cp /x/gradle-launcher.jar org.gradle.launcher.daemon.bootstrap.GradleDaemon 8.5",
                    "/opt/homebrew/Cellar/podman/5/libexec/podman/gvproxy -listen x",
                    "/opt/homebrew/bin/vfkit --cpus 2", "emacs --daemon", "mutagen daemon run",
                    "sccache", "/Users/u/Library/Android/sdk/emulator/qemu/darwin-aarch64/qemu-system-aarch64 -avd Pixel"):
            with self.subTest(cmd=cmd):
                p = self.plan("clean", None, {20: (1, self.T, cmd)}, {20: ("claude", DEAD_SID)})
                self.assertEqual(p["kill"], [], cmd)

    def test_a_prompt_word_does_not_hide_an_agent(self):
        for cmd, kind in (("/Users/u/.local/bin/claude Fix the login bug", "claude"),
                          ("/opt/homebrew/bin/codex Refactor auth", "Codex agent"),
                          ("/Users/u/.local/share/claude/versions/2.0.14 Fix it", "claude")):
            with self.subTest(cmd=cmd):
                self.assertEqual(procs._agent_kind(cmd), kind)

    def test_a_runtimes_script_is_its_first_argument(self):
        self.assertIsNone(procs._agent_kind(
            "node /Users/u/proj/node_modules/.bin/vite --config /Users/u/proj/bin/claude"))
        self.assertEqual(procs._agent_kind("deno run -A npm:@anthropic-ai/claude-code"), "claude")

    def test_a_runtimes_later_arguments_are_not_its_script(self):
        self.assertIsNone(procs._agent_kind("node /x/vite.js /Users/u/proj/bin/claude"))

    def test_an_unmarked_daemon_in_the_tree_says_so(self):
        # its environment could not be read, so no mark: its shape still counts
        table = {**self.NPM, 25: (21, self.T, "gpg-agent --daemon")}
        p = self.plan("clean", None, table, self.npm_marks())
        self.assertIn("shared user daemon", next(k["note"] for k in p["kill"] if k["pid"] == 25))

    def test_a_member_attribute_doubts_says_so(self):
        # same dead mark, but its claude (10) still runs: attribute is not sure
        table = {**self.NPM, 10: (1, self.T, "/Users/u/.local/bin/claude"),
                 26: (21, self.T, "node worker.js")}
        marks = {**self.npm_marks(), 26: ("claude", DEAD_SID, 10)}
        p = self.plan("clean", None, table, marks)
        self.assertIn("not sure", next(k["note"] for k in p["kill"] if k["pid"] == 26))

    def test_bad_world_data_is_answered_never_raised(self):
        base = {20: (1, self.T, "node s.js")}
        for label, world in (
                ("ports None", {"ports": {20: None}}), ("pipes None", {"pipes": {20: None}}),
                ("att None", {"att": None}), ("start None", {"table": {20: (1, None, "node s.js")}}),
                ("ppid None", {"table": {20: (None, self.T, "node s.js")}})):
            with self.subTest(label=label):
                w = {"table": {**base, 99: (1, self.T, "python3 ccwho.py")},
                     "att": {"left_behind": [{"pid": 20, "session": DEAD_SID}]},
                     "sessions": [], "own": 99, "ports": {}, "pipes": {20: set()}, "marks": {20: ("claude", DEAD_SID)}, "connections": []}
                w.update(world)
                if "table" in world:
                    w["table"] = {**world["table"], 99: (1, self.T, "python3 ccwho.py")}
                for mode, target in (("clean", None), ("port", 3000), ("pid", {"pid": 20, "start": self.T})):
                    procs.kill_plan(mode, target, w)
        p = procs.kill_plan("port", "٣٠٠٠", {"table": {99: (1, self.T, "x")}, "att": {}, "own": 99,
                                              "sessions": [], "ports": {}, "pipes": {20: set()}, "marks": {20: ("claude", DEAD_SID)}, "connections": []})
        self.assertEqual((p["kill"], p.get("why")), ([], "not a port number"))


class TestKillPlanV1Review10(unittest.TestCase):
    """Review 10 of v1: what a person started stays theirs; pipes and marks not
    read mean nothing is taken; a tool shell's eval text is not its options."""

    T = "Tue Sep 22 13:45:15 2026"

    def plan(self, mode, target, table, marks, ports=None, pipes=None, read=None):
        table = {**table, 99: (1, self.T, "python3 ccwho.py")}
        att = procs.attribute(table, {k: v for k, v in marks.items() if v}, ports or {}, [], own=99)
        full_pipes = {q: set() for q in table} if pipes is None else pipes
        return procs.kill_plan(mode, target, {"table": table, "att": att, "sessions": [], "own": 99,
                                              "ports": ports or {}, "pipes": full_pipes,
                                              "marks": marks if read is None else read,
                                              "connections": []})

    def kills(self, p):
        return sorted(k["pid"] for k in p["kill"])

    def spared(self, p):
        return {s["pid"]: s["why"] for s in p["spare"]}

    PM2 = {20: (1, "Tue Sep 22 13:45:15 2026", "PM2 v5.3.0: God Daemon (/Users/u/.pm2)"),
           21: (20, "Tue Sep 22 13:45:15 2026", "node /u/app/server.js"),
           22: (20, "Tue Sep 22 13:45:15 2026", "node /u/mine/app.js")}

    def test_what_a_person_started_under_an_orphan_says_so(self):
        # 22's environment was READ and carries no mark: someone else started it
        marks = {20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID), 22: None}
        for mode, target in (("clean", None), ("port", 8080)):
            with self.subTest(mode=mode):
                p = self.plan(mode, target, self.PM2, marks, ports={22: [8080]})
                self.assertEqual(self.kills(p), [20, 21, 22])
                self.assertIn("no agent started it", next(k["note"] for k in p["kill"] if k["pid"] == 22))

    def test_a_member_whose_environment_could_not_be_read_is_part_of_the_tree(self):   # control
        # platform binaries (/bin/sleep) hide their environment: not read, not "no mark"
        table = {20: (1, self.T, "/bin/zsh -c npm run dev"), 21: (20, self.T, "/bin/sleep 100")}
        p = self.plan("clean", None, table, {20: ("claude", DEAD_SID)})
        self.assertEqual(self.kills(p), [20, 21])

    def test_marks_not_given_leave_attributes_list(self):
        table = {20: (1, self.T, "node s.js")}
        p = self.plan("clean", None, table, {20: ("claude", DEAD_SID)}, read=None)
        self.assertEqual(self.kills(p), [20])            # a person: attribute's list still holds

    def test_a_member_missing_from_the_pipes_says_so(self):
        table = {20: (1, self.T, "/bin/zsh -c npm run dev"), 21: (20, self.T, "npm run dev"),
                 22: (21, self.T, "tee log")}
        marks = {k: ("claude", DEAD_SID) for k in table}
        p = self.plan("clean", None, table, marks, pipes={20: set(), 99: set()})
        notes = {k["pid"]: k.get("note", "") for k in p["kill"]}
        self.assertIn("pipes were not read", notes[21])
        self.assertNotIn("pipes", notes[20])                                     # control

    TOOL = "/bin/zsh -c -l source /Users/u/.claude/shell-snapshots/snap.sh && eval '{}'"

    def test_a_tool_shells_eval_text_is_not_its_options(self):
        for inner in ("npm run dev 2>&1 | grep -i error", "sed -i '' s/a/b/ x && npm run dev",
                      "cargo watch -i *.md -x run", "npx jest -i --watch", "docker run -it img"):
            with self.subTest(inner=inner):
                self.assertFalse(procs._person_shell(self.TOOL.format(inner)))
        for cmd in ("bash deploy.sh -i", "sh ./run.sh", "zsh build.zsh"):          # scripts
            with self.subTest(script=cmd):
                self.assertFalse(procs._person_shell(cmd))
        for shell in ("bash", "-zsh", "zsh -il", "bash --norc", "bash --noprofile --norc",
                      "bash --rcfile x", "zsh -o vi", "pwsh", "mksh"):                 # control
            with self.subTest(shell=shell):
                self.assertTrue(procs._person_shell(shell))

    def test_an_agent_path_anywhere_in_a_member_spares_its_tree(self):
        # refusal only: a false positive here costs a refusal, a miss costs an agent
        for cmd in ("node --stack-size 4000 /x/@anthropic-ai/claude-code/cli.js",
                    "/Users/u/Library/Application Support/Claude/claude-code/2.1.0/claude Fix the bug",
                    "node /Users/u/Library/Application Support/fnm/x/@anthropic-ai/claude-code/cli.js"):
            with self.subTest(cmd=cmd):
                table = {20: (1, self.T, "/bin/zsh -c x"), 21: (20, self.T, cmd)}
                p = self.plan("clean", None, table, {20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID)})
                self.assertEqual(p["kill"], [])

    def test_each_kill_entry_carries_its_parent_and_root(self):
        table = {20: (1, self.T, "/bin/zsh -c npm run dev"), 21: (20, self.T, "npm run dev")}
        p = self.plan("clean", None, table, {20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID)})
        self.assertEqual({k["pid"]: (k["ppid"], k["root"]) for k in p["kill"]},
                         {20: (1, 20), 21: (20, 20)})

    def test_malformed_rows_are_skipped_never_raised(self):
        for label, table, extra in (("row None", {20: None}, {}), ("row short", {20: (20,)}, {}),
                                    ("command None", {20: (1, self.T, None)}, {}),
                                    ("port int", {20: (1, self.T, "node s.js")}, {"ports": {20: 3000}}),
                                    ("session None", {20: (1, self.T, "node s.js")}, {"sessions": [None]}),
                                    ("att no pid", {20: (1, self.T, "node s.js")},
                                     {"att": {"left_behind": [{"session": DEAD_SID}]}})):
            with self.subTest(label=label):
                w = {"table": {**table, 99: (1, self.T, "python3 ccwho.py")},
                     "att": {"left_behind": [{"pid": 20, "session": DEAD_SID}]}, "sessions": [],
                     "own": 99, "ports": {}, "pipes": {20: set(), 99: set()},
                     "marks": {20: ("claude", DEAD_SID)}, "connections": []}
                w.update(extra)
                for mode, target in (("clean", None), ("port", 3000), ("pid", {"pid": 20, "start": self.T})):
                    procs.kill_plan(mode, target, w)

    def test_members_of_a_spared_tree_carry_its_reason(self):
        table = {20: (1, self.T, "/bin/zsh -c x"), 21: (20, self.T, "bash"),
                 22: (21, "", "npm run dev")}                       # no start: not provable
        p = self.plan("clean", None, table, {k: ("claude", DEAD_SID) for k in table})
        for pid in (21, 22):
            self.assertIn("20's tree, which is spared", self.spared(p)[pid])
            self.assertNotIn("ccwho kill", self.spared(p)[pid])


class TestKillPlanPort(unittest.TestCase):
    """The port slice (review 11's findings), under the owner's model: a tree
    whose port somebody is using, or a session host a person may be using, is
    taken with a note that says so. The person decides."""

    T = "Tue Sep 22 13:45:15 2026"
    TREE = {20: (1, "Tue Sep 22 13:45:15 2026", "/bin/zsh -c -l source /u/.claude/shell-snapshots/s.sh && eval 'npm run dev'"),
            21: (20, "Tue Sep 22 13:45:15 2026", "npm run dev"),
            22: (21, "Tue Sep 22 13:45:15 2026", "node /u/app/node_modules/.bin/vite")}

    def plan(self, mode, target, table=None, marks=None, ports=None, conns=(), pipes=None,
             sessions=()):
        table = {**(table or self.TREE), 99: (1, self.T, "python3 ccwho.py")}
        marks = marks if marks is not None else {k: ("claude", DEAD_SID) for k in (table or self.TREE)
                                                 if k != 99}
        ports = ports if ports is not None else {22: [5173]}
        att = procs.attribute(table, {k: v for k, v in marks.items() if v}, ports, list(sessions), own=99)
        return procs.kill_plan(mode, target, {
            "table": table, "att": att, "sessions": list(sessions), "own": 99, "ports": ports,
            "pipes": pipes if pipes is not None else {q: set() for q in table}, "marks": marks,
            "connections": None if conns is None else list(conns)})

    def kills(self, p):
        return sorted(k["pid"] for k in p["kill"])

    @staticmethod
    def c(pid, lport, rport, local=True, laddr="127.0.0.1"):
        """One ESTABLISHED socket: (pid, local addr, local port, remote addr, remote port)."""
        return (pid, laddr, lport, laddr if local else "192.168.1.20", rport)

    def whys(self, p):
        return " ".join(s["why"] for s in p["spare"])

    def notes(self, p):
        return " ".join(k.get("note", "") for k in p["kill"])

    # ---- somebody is using the port: said, never a refusal
    def test_a_port_another_process_is_using_says_who(self):
        # a live session's `npx playwright test` against the dev server
        table = {**self.TREE, 10: (1, self.T, "claude"), 11: (10, self.T, "node playwright test")}
        conns = [self.c(22, 5173, 61234), self.c(11, 61234, 5173)]
        for mode, target in (("port", 5173), ("clean", None)):
            with self.subTest(mode=mode):
                p = self.plan(mode, target, table=table, conns=conns,
                              marks={20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID),
                                     22: ("claude", DEAD_SID), 11: ("claude", LIVE_SID)},
                              sessions=[{"sessionId": LIVE_SID, "pid": 10}])
                self.assertEqual(self.kills(p), [20, 21, 22])
                self.assertIn("node (11) is connected to :5173", self.notes(p))
    def test_a_port_nobody_uses_is_taken(self):                               # control
        self.assertEqual(self.kills(self.plan("port", 5173)), [20, 21, 22])

    def test_a_client_inside_the_tree_is_part_of_it(self):                    # control
        conns = [self.c(22, 5173, 61234), self.c(21, 61234, 5173)]
        self.assertEqual(self.kills(self.plan("port", 5173, conns=conns)), [20, 21, 22])

    def test_a_client_from_another_machine_says_so(self):
        p = self.plan("port", 5173, conns=[self.c(22, 5173, 50112, local=False)])
        self.assertIn("talks to another machine", self.notes(p))
    def test_a_client_ccwho_cannot_see_says_so(self):
        p = self.plan("port", 5173, conns=[self.c(22, 5173, 61234)])
        self.assertIn("a client ccwho cannot see is using :5173", self.notes(p))
    def test_connections_not_read_are_said(self):
        p = self.plan("port", 5173, conns=None)
        self.assertIn("connections were not read", self.notes(p))
        self.assertNotIn("connections", self.notes(self.plan("port", 5173)))     # control
    def test_established_connections_are_read_from_lsof(self):
        text = ("p22\nn127.0.0.1:5173->127.0.0.1:61234\n"
                "p11\nn127.0.0.1:61234->127.0.0.1:5173\n"
                "p30\nn[::1]:8080->[::1]:50000\n"
                "p31\nn192.168.1.5:8888->192.168.1.20:50112\n")
        self.assertEqual(sorted(procs.parse_lsof_established(text)),
                         [(11, "127.0.0.1", 61234, "127.0.0.1", 5173),
                          (22, "127.0.0.1", 5173, "127.0.0.1", 61234),
                          (30, "::1", 8080, "::1", 50000),
                          (31, "192.168.1.5", 8888, "192.168.1.20", 50112)])

    def test_a_line_that_does_not_parse_means_not_read(self):
        # without -P a port is a name: `localhost:http-alt` - not read, not "none"
        self.assertIsNone(procs.parse_lsof_established("p22\nnlocalhost:http-alt->localhost:61234\n"))
        self.assertEqual(procs.parse_lsof_established(""), [])                  # control

    # ---- review 12
    def test_only_the_exact_other_end_is_the_client(self):
        # a proxy's own upstream socket and an outside client share a port
        # number; and a socket whose two ports are equal matches itself
        table = {**self.TREE, 300: (1, self.T, "/opt/x/bin/curl-loop")}
        marks = {20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID), 22: ("claude", DEAD_SID), 300: None}
        conns = [(22, "192.168.1.5", 50000, "10.0.0.9", 8080), self.c(22, 8080, 50000),
                 self.c(300, 50000, 8080)]
        p = self.plan("clean", None, table=table, ports={22: [8080]}, conns=conns, marks=marks)
        self.assertIn("(300) is connected to :8080", self.notes(p))
        conns = [self.c(22, 5173, 5173), (300, "::1", 5173, "::1", 5173)]
        p = self.plan("clean", None, table=table, ports={22: [5173]}, conns=conns, marks=marks)
        self.assertIn("a client ccwho cannot see is using :5173", self.notes(p))
        self.assertNotIn("300", self.notes(p))
    def test_a_socket_that_only_shares_port_numbers_is_no_client(self):
        # the true client is not visible; the tree's own upstream socket shares
        # the port numbers. Matching ports alone "found" it inside the tree
        conns = [self.c(22, 8080, 50000), (22, "192.168.1.5", 50000, "10.0.0.9", 8080)]
        p = self.plan("clean", None, ports={22: [8080]}, conns=conns)
        self.assertIn("a client ccwho cannot see is using :8080", self.notes(p))
    def test_every_holder_of_the_client_socket_counts(self):
        # an inherited socket: held by 21 (inside) and 300 (outside)
        table = {**self.TREE, 300: (1, self.T, "/opt/x/bin/relay")}
        conns = [self.c(22, 5173, 61000), self.c(21, 61000, 5173), self.c(300, 61000, 5173)]
        p = self.plan("clean", None, table=table, conns=conns,
                      marks={20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID), 22: ("claude", DEAD_SID),
                             300: None})
        self.assertIn("(300) is connected to :5173", self.notes(p))
        self.assertNotIn("(21)", self.notes(p))                                   # control: inside
    def test_bad_connection_data_means_not_read(self):
        for bad in ([("22", "127.0.0.1", 5173, "127.0.0.1", 61234)],
                    [(22, "127.0.0.1", "5173", "127.0.0.1", 61234)],
                    [(22, 5173, 61234, True)], [([22], "127.0.0.1", 3000, "127.0.0.1", 1)]):
            with self.subTest(bad=bad):
                p = self.plan("port", 5173, conns=bad)             # out of shape: the whole plan
                self.assertEqual(p["kill"], [])
                self.assertIn("not in its shape", p["why"])
    BROWSER = "/Applications/Google Chrome.app/Contents/Frameworks/Google Chrome Framework.framework/Helpers/Google Chrome Helper --type=utility"

    def test_your_app_on_another_port_of_the_tree_is_said(self):
        # you named :5173; your tab is on the tree's storybook :6006
        table = {**self.TREE, 23: (21, self.T, "node storybook"), 300: (1, self.T, self.BROWSER)}
        conns = [self.c(23, 6006, 61000), self.c(300, 61000, 6006)]
        marks = {20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID), 22: ("claude", DEAD_SID),
                 23: ("claude", DEAD_SID), 300: None}
        p = self.plan("port", 5173, table=table, conns=conns, marks=marks, ports={22: [5173], 23: [6006]})
        self.assertIn("Google Chrome (300) is connected to :6006", self.notes(p))
    # ---- review 14
    def test_ports_not_read_are_said_and_no_port_is_found(self):
        # lsof failed: the ports come as None - the client check had nothing to check
        for bad in (None,):                                   # other forms: out of shape (TestWorldShape)
            with self.subTest(ports=bad):
                table = {**self.TREE, 99: (1, self.T, "python3 ccwho.py")}
                marks = {k: ("claude", DEAD_SID) for k in self.TREE}
                w = {"table": table, "att": procs.attribute(table, marks, {}, [], own=99),
                     "sessions": [], "own": 99, "ports": bad, "pipes": {q: set() for q in table},
                     "marks": marks, "connections": []}
                p = procs.kill_plan("clean", None, w)
                self.assertEqual(self.kills(p), [20, 21, 22])
                self.assertIn("listening ports were not read", self.notes(p))
                p = procs.kill_plan("port", 5173, w)
                self.assertEqual(p["kill"], [])
                self.assertIn("ports were not read", p.get("why", ""))
        self.assertNotIn("ports", self.notes(self.plan("clean", None, ports={})))  # control: read, none
    def test_an_app_is_known_by_its_program_word(self):
        self.assertFalse(procs._is_app("node /x/tool.js --open /Applications/Foo.app/Contents/x"))
        self.assertTrue(procs._is_app("/Applications/Foo.app/Contents/MacOS/Foo --flag"))  # control

    def test_the_person_is_told_the_apps_name(self):
        table = {**self.TREE, 300: (1, self.T, self.BROWSER)}
        conns = [self.c(22, 5173, 61000), self.c(300, 61000, 5173)]
        marks = {20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID), 22: ("claude", DEAD_SID), 300: None}
        p = self.plan("port", 5173, table=table, conns=conns, marks=marks)
        self.assertIn("Google Chrome (300) is connected to :5173", self.notes(p))
    # ---- review 15
    APPS = ("/Applications/GIMP 2.app/Contents/MacOS/gimp", "/Applications/GIMP (Nightly).app/Contents/MacOS/gimp",
            "/Applications/pgAdmin 4.app/Contents/MacOS/pgAdmin4", "/Applications/Chief of Staff.app/Contents/MacOS/cos",
            "/Applications/DB Browser for SQLite.app/Contents/MacOS/DB Browser for SQLite",
            "/Applications/Adobe Photoshop 2024/Adobe Photoshop 2024.app/Contents/MacOS/Adobe Photoshop 2024",
            "/Applications/Utilities/1Password 7.app/Contents/MacOS/1Password 7",
            "/Users/me/my apps/Foo.app/Contents/MacOS/Foo")

    def test_every_app_bundle_path_is_an_app(self):
        for cmd in self.APPS:
            for tail in ("", " --flag value"):
                with self.subTest(cmd=cmd + tail):
                    self.assertTrue(procs._is_app(cmd + tail))
        for cmd in ("/usr/bin/open -W /Applications/Foo.app/Contents/MacOS/Foo",        # control
                    "/usr/bin/python3 /Applications/Foo.app/Contents/Resources/x.py",
                    f"{self.FW} -m http.server"):
            with self.subTest(not_app=cmd):
                self.assertFalse(procs._is_app(cmd))
        p = self.plan("clean", None, table={20: (1, self.T, "/bin/zsh -c open-gimp"),
                                            21: (20, self.T, self.APPS[0])},
                      ports={}, marks={20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID)})
        self.assertIn("it is an app", self.notes(p))

    def test_a_tree_talking_to_another_machine_says_so(self):
        # a tunnel (lt, ngrok, cloudflared, ssh -R) serves remote people through
        # an OUTGOING socket - no client on the listening port shows it
        table = {**self.TREE, 23: (21, self.T, "node /x/.bin/lt --port 5173")}
        marks = {k: ("claude", DEAD_SID) for k in (20, 21, 22, 23)}
        conns = [(23, "192.168.1.5", 50100, "104.16.1.1", 443)]
        for mode, target in (("clean", None), ("port", 5173)):
            with self.subTest(mode=mode):
                p = self.plan(mode, target, table=table, marks=marks, conns=conns)
                self.assertIn("talks to another machine", self.notes(p))
        conns = [self.c(21, 61000, 5173), self.c(22, 5173, 61000)]                  # control: loopback only
        self.assertNotIn("another machine", self.notes(self.plan("clean", None, table=table, marks=marks,
                                                                  conns=conns)))
    def test_attribute_lists_of_the_wrong_type_never_raise(self):
        good = {"table": {20: (1, self.T, "node s.js"), 99: (1, self.T, "python3 ccwho.py")},
                "sessions": [], "own": 99, "ports": {}, "pipes": {20: set(), 99: set()},
                "marks": {20: ("claude", DEAD_SID)}, "connections": []}
        for att in ({"left_behind": 10 ** 30}, {"sessions": {1: 2}}, {"codex": "x"}):
            with self.subTest(att=att):
                for mode, target in (("clean", None), ("port", 3000), ("pid", {"pid": 20, "start": self.T})):
                    procs.kill_plan(mode, target, {**good, "att": att})

    def test_a_browser_under_an_agent_is_named_too(self):
        # Playwright's Chromium under a live claude is a test run, not you
        table = {**self.TREE, 10: (1, self.T, "claude"), 300: (10, self.T, self.BROWSER)}
        conns = [self.c(22, 5173, 61000), self.c(300, 61000, 5173)]
        for mark in (("claude", LIVE_SID), None):           # marked, or env read and unmarked
            with self.subTest(mark=mark):
                p = self.plan("port", 5173, table=table, conns=conns,
                              marks={20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID),
                                     22: ("claude", DEAD_SID), 300: mark},
                              sessions=[{"sessionId": LIVE_SID, "pid": 10}])
                self.assertIn("(300) is connected to :5173", self.notes(p))
    FW = "/opt/homebrew/Cellar/python@3.13/3.13.5/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python"

    def test_a_host_run_by_an_interpreter_is_a_host(self):
        for host in (f"{self.FW} /opt/homebrew/bin/jupyter-lab --port 8888", "python3 -m jupyterlab",
                     "/usr/bin/python3 /Users/u/.local/bin/jupyter-notebook",
                     "node /usr/lib/code-server/out/node/entry", "sshd: u@ttys003", "sshd-session: u"):
            with self.subTest(host=host):
                self.assertTrue(procs._session_host(host))
                table = {20: (1, self.T, host), 21: (20, self.T, f"{self.FW} -m ipykernel_launcher")}
                p = self.plan("clean", None, table=table, ports={20: [8888]},
                              marks={20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID)})
                self.assertIn("session host", self.notes(p))
        p = self.plan("port", 8000, table={20: (1, self.T, f"{self.FW} -m http.server 8000")},   # control
                      ports={20: [8000]}, marks={20: ("claude", DEAD_SID)})
        self.assertNotIn("session host", self.notes(p))
    def test_the_framework_python_is_known_by_its_program_word(self):
        apple = ("/Applications/Xcode.app/Contents/Developer/Library/Frameworks/Python3.framework/"
                 "Versions/3.9/Resources/Python.app/Contents/MacOS/Python -m http.server 8000")
        self.assertFalse(procs._is_app(apple))
        self.assertTrue(procs._is_app("/Applications/Foo.app/Contents/MacOS/Foo --py "      # control
                                      "/Library/Frameworks/Python.framework/Versions/3.12/"
                                      "Resources/Python.app/Contents/MacOS/Python"))

    # ---- a session host a person may be using, whatever runs under it
    def test_a_session_host_says_so(self):
        for host, child in (("dtach -n /tmp/s python3", "python3"), ("abduco -n s python3 -i", "python3 -i"),
                            ("ttyd -W -p 7681 node", "node"), ("gotty -w node", "node"),
                            ("/Users/u/.local/bin/jupyter-lab --port 8888", "python3 -m ipykernel_launcher"),
                            ("/usr/local/bin/code-server --bind-addr 0:8080", "node extensionHost"),
                            ("/usr/sbin/sshd -D -p 2222", "sshd: u@notty"),
                            ("mosh-server new -s", "node repl.js"), ("/usr/local/bin/code tunnel", "node x")):
            with self.subTest(host=host):
                table = {20: (1, self.T, host), 21: (20, self.T, child)}
                p = self.plan("clean", None, table=table, ports={21: [7000]},
                              marks={20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID)})
                self.assertIn("session host", p["kill"][0].get("note", ""))
        table = {20: (1, self.T, "nohup node server.js"), 21: (20, self.T, "node worker.js")}  # control
        p = self.plan("clean", None, table=table, ports={20: [3000]},
                      marks={20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID)})
        self.assertNotIn("session host", self.notes(p))
    # ---- python3 -m http.server is no app
    def test_a_framework_python_is_no_app(self):
        py = ("/opt/homebrew/Cellar/python@3.13/3.13.1/Frameworks/Python.framework/Versions/3.13/"
              "Resources/Python.app/Contents/MacOS/Python -m http.server 8000")
        p = self.plan("port", 8000, table={20: (1, self.T, py)}, ports={20: [8000]},
                      marks={20: ("claude", DEAD_SID)})
        self.assertEqual(self.kills(p), [20])
        for app in ("/Applications/Docker.app/Contents/MacOS/com.docker.backend",           # control
                    "/Applications/Slack.app/Contents/MacOS/Slack"):
            with self.subTest(app=app):
                p = self.plan("clean", None, table={20: (1, self.T, app)}, ports={},
                              marks={20: ("claude", DEAD_SID)})
                self.assertEqual(p["kill"], [])         # attribute is not sure: named, not cleaned
                self.assertIn("it is an app", self.whys(p))

    # ---- the confirm list shows every member of a spared tree
    # ---- malformed worlds
    def test_more_malformed_worlds_never_raise(self):
        good = {"table": {20: (1, self.T, "node s.js"), 99: (1, self.T, "python3 ccwho.py")},
                "att": {"left_behind": [{"pid": 20, "session": DEAD_SID}]}, "sessions": [],
                "own": 99, "ports": {20: [3000]}, "pipes": {20: set(), 99: set()},
                "marks": {20: ("claude", DEAD_SID)}, "connections": []}
        for label, change in {"sessions int": {"sessions": 5},
                              "session pid list": {"sessions": [{"pid": [1]}]},
                              "session id list": {"sessions": [{"sessionId": [1], "pid": 3}]},
                              "own list": {"own": [99]}, "pipe peer list": {"pipes": {20: [[1]]}},
                              "ports nested": {"ports": {20: [[3000]]}}}.items():
            with self.subTest(label=label):
                for mode, target in (("clean", None), ("port", 3000)):
                    procs.kill_plan(mode, target, {**good, **change})

    def test_malformed_worlds_never_raise(self):
        good = {"table": {20: (1, self.T, "node s.js"), 99: (1, self.T, "python3 ccwho.py")},
                "att": {"left_behind": [{"pid": 20, "session": DEAD_SID}]}, "sessions": [],
                "own": 99, "ports": {}, "pipes": {20: set(), 99: set()},
                "marks": {20: ("claude", DEAD_SID)}, "connections": []}
        bad = {"pipes list": {"pipes": [20]}, "pipes set": {"pipes": {20}},
               "pipe value int": {"pipes": {20: 5}}, "table list": {"table": [20]},
               "ports list": {"ports": [3000]}, "att sessions list": {"att": {"sessions": [1]}},
               "connections junk": {"connections": [None, (1,), "x"]}}
        for label, change in bad.items():
            with self.subTest(label=label):
                for mode, target in (("clean", None), ("port", 3000), ("pid", {"pid": 20, "start": self.T})):
                    procs.kill_plan(mode, target, {**good, **change})
        for mode in ("clean", "port", "pid"):
            with self.subTest(world=None, mode=mode):
                self.assertEqual(procs.kill_plan(mode, 3000, None)["kill"], [])

    def test_a_session_pid_given_as_text_still_protects(self):
        w = {"table": {20: (1, self.T, "node s.js"), 99: (1, self.T, "python3 ccwho.py")},
             "att": {"left_behind": [{"pid": 20, "session": DEAD_SID}]},
             "sessions": [{"sessionId": LIVE_SID, "pid": "20"}], "own": 99, "ports": {},
             "pipes": {20: set(), 99: set()}, "marks": {20: ("claude", DEAD_SID)}, "connections": []}
        self.assertEqual(procs.kill_plan("clean", None, w)["kill"], [])

    # ---- more shells
    def test_more_shells_are_shells(self):
        for shell in ("-elvish", "yash", "osh", "ash", "rbash", "zsh --emulate sh",
                      "fish -C 'set x'", "fish --init-command 'set x'"):
            with self.subTest(shell=shell):
                self.assertTrue(procs._person_shell(shell))
        self.assertFalse(procs._person_shell("fish -c 'echo hi'"))                  # control


class TestKillPlanTrustsThePerson(unittest.TestCase):
    """The owner's model (2026-09-25): a person at a terminal decides. A plan
    lists every process a kill takes, with notes on what ccwho doubts; the
    notes never refuse. Hard guards refuse for everyone: a live session, an
    agent or what runs one, ccwho and its shell, a pid not proved the same. An
    agent (`mine` in the world: its own session id) takes only what its own
    session started."""

    T = "Tue Sep 22 13:45:15 2026"
    TREE = {20: (1, T, "/bin/zsh -c -l source /u/.claude/shell-snapshots/s.sh && eval 'npm run dev'"),
            21: (20, T, "npm run dev"),
            22: (21, T, "node /u/app/node_modules/.bin/vite")}
    BROWSER = TestKillPlanPort.BROWSER

    def plan(self, mode, target, table=None, marks=None, ports=None, conns=(), pipes=None,
             sessions=(), mine=None):
        table = {99: (1, self.T, "python3 ccwho.py"), **(table if table is not None else self.TREE)}
        if marks is None:
            marks = {k: ("claude", DEAD_SID) for k in table if k != 99}
        ports = ports if ports is not None else {22: [5173]}
        att = procs.attribute(table, {k: v for k, v in marks.items() if v}, ports,
                              list(sessions), own=99)
        world = {"table": table, "att": att, "sessions": list(sessions), "own": 99,
                 "ports": ports, "marks": marks,
                 "pipes": pipes if pipes is not None else {q: set() for q in table},
                 "connections": None if conns is None else list(conns)}
        if mine is not None:
            world["mine"] = mine
        return procs.kill_plan(mode, target, world)

    def kills(self, p):
        return sorted(k["pid"] for k in p["kill"])

    def note(self, p, pid):
        return next(k.get("note", "") for k in p["kill"] if k["pid"] == pid)

    def whys(self, p):
        return " ".join([s["why"] for s in p["spare"]] + [p.get("why", "")])

    c = staticmethod(TestKillPlanPort.c)

    # ---- a person: what ccwho doubts is said, never refused
    def test_a_client_on_the_port_is_a_note(self):
        table = {**self.TREE, 300: (1, self.T, self.BROWSER)}
        conns = [self.c(22, 5173, 61000), self.c(300, 61000, 5173)]
        marks = {20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID), 22: ("claude", DEAD_SID), 300: None}
        for mode, target in (("port", 5173), ("clean", None)):
            with self.subTest(mode=mode):
                p = self.plan(mode, target, table=table, conns=conns, marks=marks)
                self.assertEqual(self.kills(p), [20, 21, 22])
                self.assertIn("Google Chrome (300) is connected to :5173", self.note(p, 22))
                self.assertNotIn("needs_person", p)
        p = self.plan("port", 5173)                                              # control
        self.assertEqual(self.note(p, 22), "")

    def test_what_ccwho_doubts_about_a_member_is_its_note(self):
        cases = {"talks to another machine": dict(conns=[(22, "192.168.1.5", 50100, "104.16.1.1", 443)]),
                 "connections were not read": dict(conns=None),
                 "pipes were not read": dict(pipes={20: set(), 21: set(), 99: set()}),
                 "a client ccwho cannot see": dict(conns=[self.c(22, 5173, 61234)])}
        for text, kw in cases.items():
            with self.subTest(note=text):
                p = self.plan("port", 5173, **kw)
                self.assertEqual(self.kills(p), [20, 21, 22])
                self.assertIn(text, " ".join(k.get("note", "") for k in p["kill"]))

    def test_a_process_that_feeds_a_live_one_is_a_note(self):
        table = {**self.TREE, 70: (1, self.T, "ssh: /Users/u/.ssh/cm-x [mux]")}
        pipes = {**{q: set() for q in table}, 99: set(), 22: {70}}
        p = self.plan("clean", None, table=table, pipes=pipes,
                      marks={20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID),
                             22: ("claude", DEAD_SID), 70: None})
        self.assertEqual(self.kills(p), [20, 21, 22])
        self.assertIn("piped to 70", self.note(p, 22))

    def test_the_shape_of_a_member_is_a_note(self):
        for child, text in (("/Applications/Slack.app/Contents/MacOS/Slack", "an app"),
                            ("gpg-agent --daemon", "shared user daemon"),
                            ("-zsh", "a shell someone may be working in"),
                            ("dtach -n /tmp/s python3", "session host")):
            with self.subTest(child=child):
                table = {20: (1, self.T, "/bin/zsh -c x"), 21: (20, self.T, child)}
                p = self.plan("clean", None, table=table, ports={})
                self.assertEqual(self.kills(p), [20, 21])
                self.assertIn(text, self.note(p, 21))

    def test_a_port_takes_any_holder_with_what_ccwho_knows_of_it(self):
        table = {10: (1, self.T, "claude"), 11: (10, self.T, "node live.js"),
                 40: (1, self.T, "python3 my_own.py"), 60: (1, self.T, "gpg-agent --daemon"),
                 30: (1, self.T, "workerd serve")}
        marks = {11: ("claude", LIVE_SID), 40: None, 60: ("claude", DEAD_SID), 30: ("codex", "01a0-x")}
        ports = {11: [3000], 40: [8000], 60: [9999], 30: [7000]}
        sessions = [{"sessionId": LIVE_SID, "pid": 10}]
        for port, pid, text in ((3000, 11, "a live session"), (8000, 40, "no agent started it"),
                                (9999, 60, "shared user daemon"), (7000, 30, "Codex")):
            with self.subTest(port=port):
                p = self.plan("port", port, table=table, marks=marks, ports=ports, sessions=sessions)
                self.assertEqual(self.kills(p), [pid])
                self.assertIn(text, self.note(p, pid))

    def test_a_named_pid_takes_its_tree(self):
        # SIGTERM to a `zsh -c` tool shell is not passed on: its dev server lives on
        p = self.plan("pid", {"pid": 20, "start": self.T})
        self.assertEqual(self.kills(p), [20, 21, 22])
        self.assertEqual(self.kills(self.plan("pid", {"pid": 22, "start": self.T})), [22])   # control

    def test_no_plan_carries_needs_person(self):
        table = {**self.TREE, 300: (1, self.T, self.BROWSER)}
        conns = [self.c(22, 5173, 61000), self.c(300, 61000, 5173)]
        p = self.plan("port", 5173, table=table, conns=conns,
                      marks={20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID),
                             22: ("claude", DEAD_SID), 300: None})
        self.assertNotIn("needs_person", p)
        self.assertFalse(any("needs_person" in k for k in p["kill"]))

    # ---- hard guards: for everyone
    def test_a_live_session_is_never_taken(self):
        table = {10: (1, self.T, "/opt/x/my-dev-build"), 11: (10, self.T, "node s.js")}
        sessions = [{"sessionId": LIVE_SID, "pid": 10}]
        for mode, target in (("pid", {"pid": 10, "start": self.T}), ("port", 3000)):
            with self.subTest(mode=mode):
                p = self.plan(mode, target, table=table, ports={10: [3000]}, sessions=sessions,
                              marks={11: ("claude", LIVE_SID)})
                self.assertEqual(p["kill"], [])
                self.assertIn("live Claude session", self.whys(p))

    def test_an_agent_or_what_runs_one_is_never_taken(self):
        table = {20: (1, self.T, "/bin/zsh -c x"), 21: (20, self.T, "node /u/.bin/claude -p hi"),
                 22: (1, self.T, "npx @openai/codex")}
        for mode, target in (("pid", {"pid": 20, "start": self.T}), ("pid", {"pid": 22, "start": self.T}),
                             ("port", 3000), ("clean", None)):
            with self.subTest(mode=mode, target=target):
                p = self.plan(mode, target, table=table, ports={20: [3000]})
                self.assertEqual(p["kill"], [])
        p = self.plan("pid", {"pid": 20, "start": self.T}, table=table)
        self.assertIn("a claude runs under 20", self.whys(p))

    def test_ccwho_and_its_shell_are_never_taken(self):
        table = {**self.TREE, 98: (1, self.T, "/bin/zsh -c 'ccwho kill :5173'")}
        table[99] = (98, self.T, "python3 ccwho.py")                 # ccwho, run by that shell
        for pid, why in ((98, "98 runs ccwho"), (99, "99 is part of this ccwho run")):
            with self.subTest(pid=pid):
                p = self.plan("pid", {"pid": pid, "start": self.T}, table=table)
                self.assertEqual(p["kill"], [])
                self.assertIn(why, self.whys(p))
        self.assertEqual(self.kills(self.plan("pid", {"pid": 20, "start": self.T}, table=table)),
                         [20, 21, 22])                                           # control

    def test_a_member_without_a_start_time_spares_its_tree(self):
        table = {20: (1, self.T, "/bin/zsh -c x"), 21: (20, "", "node s.js")}
        p = self.plan("clean", None, table=table, ports={})
        self.assertEqual(p["kill"], [])
        self.assertIn("cannot prove 21", self.whys(p))
        table[21] = (20, self.T, "node s.js")                                        # control
        self.assertEqual(self.kills(self.plan("clean", None, table=table, ports={})), [20, 21])

    def test_a_reused_pid_is_never_taken(self):
        p = self.plan("pid", {"pid": 20, "start": "Wed Sep 23 09:00:00 2026"})
        self.assertEqual(p["kill"], [])
        self.assertIn("now a different process", self.whys(p))

    # ---- an agent: only what its own session started
    LIVE_TREE = {10: (1, T, "claude"), 20: (10, T, "/bin/zsh -c 'npm run dev'"),
                 21: (20, T, "npm run dev"), 22: (21, T, "node vite"),
                 23: (10, T, "npm exec chrome-devtools-mcp@latest"),
                 98: (10, T, "/bin/zsh -c 'ccwho clean --mine'"),
                 99: (98, T, "python3 ccwho.py clean --mine"),
                 40: (1, T, "node other.js")}

    def agent(self, mode, target, **kw):
        marks = {k: ("claude", LIVE_SID) for k in (20, 21, 22, 23, 98)}
        marks[40] = ("claude", DEAD_SID)
        table = {**self.LIVE_TREE}
        kw.setdefault("marks", marks)
        kw.setdefault("ports", {22: [5173], 40: [3000]})
        kw.setdefault("mine", LIVE_SID)
        p = self.plan(mode, target, table=table, sessions=[{"sessionId": LIVE_SID, "pid": 10}], **kw)
        return p

    def test_an_agent_takes_its_own_sessions_port(self):
        self.assertEqual(self.kills(self.agent("port", 5173)), [22])

    def test_an_agent_never_takes_another_sessions_work(self):
        for mode, target in (("port", 3000), ("pid", {"pid": 40, "start": self.T})):
            with self.subTest(mode=mode):
                p = self.agent(mode, target)
                self.assertEqual(p["kill"], [])
                self.assertIn("your session did not start", self.whys(p))

    def test_an_agents_clean_is_mine_only(self):
        p = self.agent("clean", None)
        self.assertEqual(p["kill"], [])
        self.assertIn("--mine", self.whys(p))
        p = self.agent("mine", LIVE_SID)
        self.assertEqual(self.kills(p), [20, 21, 22])           # not its helper, not ccwho's shell
        self.assertIn("helper", self.whys(p))

    def test_mine_takes_nothing_without_an_agent_or_its_environments(self):
        p = self.agent("mine", LIVE_SID, mine=None)                                # a person
        self.assertEqual(p["kill"], [])
        self.assertIn("--mine is for an agent", self.whys(p))
        p = self.agent("port", 5173, mine="not-a-session")
        self.assertEqual(p["kill"], [])
        self.assertIn("not one", self.whys(p))
        p = self.agent("mine", LIVE_SID, marks={k: None for k in self.LIVE_TREE})
        self.assertEqual(p["kill"], [])
        self.assertEqual(self.agent("mine", DEAD_SID)["kill"], [])                  # not its own id

    def test_mine_takes_what_runs_beside_a_nested_agent(self):
        # the agent's tool shell runs codex and a dev server: the shell stays
        # (killing it ends codex), the dev server goes
        table = {10: (1, self.T, "claude"), 20: (10, self.T, "/bin/zsh -c 'codex exec x & npm run dev'"),
                 21: (20, self.T, "codex exec x"), 23: (20, self.T, "node server.js"),
                 98: (10, self.T, "/bin/zsh -c 'ccwho clean --mine'"),
                 99: (98, self.T, "python3 ccwho.py clean --mine")}
        marks = {20: ("claude", LIVE_SID), 21: ("claude", LIVE_SID), 23: ("claude", LIVE_SID),
                 98: ("claude", LIVE_SID)}
        p = self.plan("mine", LIVE_SID, table=table, marks=marks, ports={23: [3000]},
                      sessions=[{"sessionId": LIVE_SID, "pid": 10}], mine=LIVE_SID)
        self.assertEqual(self.kills(p), [23])

    def test_an_agent_needs_the_environments_read(self):
        w_marks = None
        p = self.agent("port", 5173, marks=None)
        # marks=None in the fixture means "all marked dead"; pass a non-dict to mean unread
        table = {**self.LIVE_TREE, 99: (1, self.T, "python3 ccwho.py")}
        p = procs.kill_plan("port", 5173, {"table": table, "att": {}, "sessions": [{"sessionId": LIVE_SID, "pid": 10}],
                                           "own": 99, "ports": {22: [5173]}, "marks": w_marks,
                                           "pipes": {q: set() for q in table}, "connections": [],
                                           "mine": LIVE_SID})
        self.assertEqual(p["kill"], [])
        self.assertIn("environments were not read", self.whys(p))

    def test_an_app_bundle_name_with_a_dash_is_an_app(self):
        # review 16: a lone `-` in a name is no flag
        self.assertTrue(procs._is_app("/Applications/Visual Studio Code - Insiders.app/Contents/MacOS/Electron"))
        self.assertFalse(procs._is_app("/usr/bin/open -a /Applications/Foo.app/Contents/MacOS/Foo"))  # control


class TestKillPlanReview17(unittest.TestCase):
    """Review 17 of kill_plan (the trust model): malformed session pids, a
    member whose environment was not read, two holders in one tree, a client
    on a forked child, pipe peers as text or unknown, an agent with no person
    to read its notes, and what reaches the terminal."""

    T = "Tue Sep 22 13:45:15 2026"
    OTHER = "0ce0ce00-0000-4000-8000-000000000009"
    plan = TestKillPlanTrustsThePerson.plan
    kills = TestKillPlanTrustsThePerson.kills
    whys = TestKillPlanTrustsThePerson.whys
    c = staticmethod(TestKillPlanPort.c)
    TREE = TestKillPlanTrustsThePerson.TREE

    def notes(self, p):
        return " ".join(k.get("note", "") for k in p["kill"])

    def test_a_member_whose_environment_was_not_read_keeps_attributes_mark(self):
        table = {10: (1, self.T, "claude"), 20: (10, self.T, "/bin/zsh -c x"),
                 21: (20, self.T, "node s.js")}
        att_marks = {20: ("claude", LIVE_SID), 21: ("claude", self.OTHER)}
        sessions = [{"sessionId": LIVE_SID, "pid": 10}]
        full = {**table, 99: (1, self.T, "python3 ccwho.py")}
        for mark21, want in ((None, []), (LIVE_SID, [20, 21])):     # not read; control: ours
            with self.subTest(mark21=mark21):
                am = dict(att_marks) if mark21 is None else {**att_marks, 21: ("claude", mark21)}
                att = procs.attribute(full, am, {}, sessions, own=99)
                p = procs.kill_plan("pid", {"pid": 20, "start": self.T},
                                    {"table": full, "att": att, "sessions": sessions, "own": 99,
                                     "ports": {}, "marks": {20: ("claude", LIVE_SID),
                                                            **({21: ("claude", mark21)} if mark21 else {})},
                                     "pipes": {q: set() for q in full}, "connections": [],
                                     "mine": LIVE_SID})
                self.assertEqual(self.kills(p), want)
                if not want:        # review 18: a note refuses an agent - here "not read"
                    self.assertIn("its environment was not read", self.whys(p))

    def test_two_holders_in_one_tree_are_one_tree(self):
        table = {10: (1, self.T, "claude"), 20: (10, self.T, "node server.js"),
                 21: (20, self.T, "node worker.js")}
        sessions = [{"sessionId": LIVE_SID, "pid": 10}]
        for mine, marks in ((None, {20: ("claude", LIVE_SID), 21: ("claude", LIVE_SID)}),
                            (LIVE_SID, {20: ("claude", LIVE_SID), 21: ("claude", LIVE_SID)})):
            with self.subTest(mine=mine):
                p = self.plan("port", 3000, table=table, marks=marks, ports={20: [3000], 21: [3000]},
                              sessions=sessions, mine=mine)
                pids = [k["pid"] for k in p["kill"]]
                self.assertEqual(sorted(pids), [20, 21])
                self.assertEqual({k["root"] for k in p["kill"]}, {20})
                self.assertFalse(set(pids) & {s["pid"] for s in p["spare"]})
        table = {20: (1, self.T, "node a.js"), 21: (1, self.T, "node b.js")}      # control
        p = self.plan("port", 3000, table=table, ports={20: [3000], 21: [3000]})
        self.assertEqual({k["root"] for k in p["kill"]}, {20, 21})

    def test_a_client_on_a_forked_child_is_named(self):
        table = {20: (1, self.T, "node server.js"), 21: (20, self.T, "node worker.js"),
                 300: (1, self.T, "curl localhost:8000")}
        marks = {20: ("claude", DEAD_SID), 21: ("claude", DEAD_SID), 300: None}
        conns = [self.c(21, 8000, 61000), self.c(300, 61000, 8000)]
        p = self.plan("port", 8000, table=table, marks=marks, ports={20: [8000]}, conns=conns)
        self.assertIn("curl (300) is connected to :8000", self.notes(p))
        conns = [self.c(21, 8000, 61000), self.c(20, 61000, 8000)]               # control: inside
        p = self.plan("port", 8000, table=table, marks=marks, ports={20: [8000]}, conns=conns)
        self.assertNotIn("connected", self.notes(p))

    def test_a_peer_newer_than_the_scan_may_be_an_agent(self):
        table = {20: (1, self.T, "tee log")}
        pipes = {20: {555}, 99: set()}
        p = self.plan("pid", {"pid": 20, "start": self.T}, table=table, ports={}, pipes=pipes)
        self.assertIn("newer than the scan - it may be an agent", self.notes(p))    # a person decides
        p = self.plan("pid", {"pid": 20, "start": self.T}, table=table, ports={}, pipes=pipes,
                      marks={20: ("claude", LIVE_SID)}, mine=LIVE_SID)
        self.assertEqual(p["kill"], [])                                            # no person: refused

    def test_an_agent_leaves_what_serves_the_whole_login(self):
        # no person reads an agent's notes: a shared daemon, an ssh master, an app stay
        table = {10: (1, self.T, "claude"), 60: (10, self.T, "gpg-agent --daemon"),
                 61: (10, self.T, "ssh: /u/.ssh/cm-h [mux]"),
                 62: (10, self.T, "/Applications/Docker.app/Contents/MacOS/Docker"),
                 63: (10, self.T, "node server.js")}
        marks = {k: ("claude", LIVE_SID) for k in (60, 61, 62, 63)}
        sessions = [{"sessionId": LIVE_SID, "pid": 10}]
        p = self.plan("mine", LIVE_SID, table=table, marks=marks, ports={}, sessions=sessions,
                      mine=LIVE_SID)
        self.assertEqual(self.kills(p), [63])
        for pid in (60, 61, 62):
            p = self.plan("pid", {"pid": pid, "start": self.T}, table=table, marks=marks, ports={},
                          sessions=sessions)
            self.assertEqual(self.kills(p), [pid])                                 # control: a person

    def test_what_attribute_says_is_printable_too(self):
        table = {20: (1, self.T, "node s.js")}
        full = {**table, 99: (1, self.T, "python3 ccwho.py")}
        bad = "x\x1b]0;pwned\x07y"
        att = {"unsure": [{"pid": 20, "why": bad, "session": DEAD_SID}]}
        w = {"table": full, "att": att, "sessions": [], "own": 99, "ports": {},
             "marks": {20: ("claude", DEAD_SID)}, "pipes": {20: set(), 99: set()}, "connections": []}
        for mode, target in (("pid", {"pid": 20, "start": self.T}), ("clean", None)):
            with self.subTest(mode=mode):
                p = procs.kill_plan(mode, target, w)
                text = self.notes(p) + self.whys(p)
                self.assertNotRegex(text, r"[\x00-\x1f\x7f-\x9f]")
                self.assertIn("not sure: x", text)                                 # control: said

    def test_every_spare_has_the_same_keys(self):
        p = self.plan("pid", {"pid": 777, "start": self.T})
        q = self.plan("pid", {"pid": 20, "start": "Wed Sep 23 09:00:00 2026"})       # control
        self.assertEqual(set(p["spare"][0]), set(q["spare"][0]))


class TestKillPlanReview18(unittest.TestCase):
    """Review 18: an agent has no person to read its notes, so any note on a
    tree refuses it (one structural rule, not a list of shapes); what is piped
    to this ccwho run is part of it; marks are what the environment said."""

    T = "Tue Sep 22 13:45:15 2026"
    OTHER = TestKillPlanReview17.OTHER
    kills = TestKillPlanTrustsThePerson.kills
    whys = TestKillPlanTrustsThePerson.whys
    c = staticmethod(TestKillPlanPort.c)
    BASE = {10: (1, T, "claude"), 98: (10, T, "/bin/zsh -c 'ccwho kill --mine | head'"),
            99: (98, T, "python3 ccwho.py")}

    def world(self, extra, marks, mine=LIVE_SID, ports=None, pipes="all", conns=(), named=None,
              sessions=None, att=True):
        table = {**self.BASE, **extra}
        marks = {98: ("claude", LIVE_SID), **marks}
        sessions = sessions if sessions is not None else [{"sessionId": LIVE_SID, "pid": 10}]
        ports = ports if ports is not None else {}
        w = {"table": table, "sessions": sessions, "own": 99, "ports": ports, "marks": marks,
             "att": procs.attribute(table, {k: v for k, v in marks.items() if v},
                                    ports if isinstance(ports, dict) else {},
                                    sessions, own=99, named=named) if att else {},
             "pipes": {q: set() for q in table} if pipes == "all" else pipes,
             "connections": None if conns is None else list(conns)}
        if mine:
            w["mine"] = mine
        return w

    def notes(self, p):
        return " ".join(k.get("note", "") for k in p["kill"])

    def test_what_reads_this_ccwhos_output_is_part_of_it(self):
        extra = {97: (98, self.T, "head -50")}
        pipes = {**{q: set() for q in {**self.BASE, **extra}}, 97: {99}, 99: {97}}
        for mode, target, mine in (("mine", LIVE_SID, LIVE_SID), ("pid", {"pid": 97, "start": self.T}, LIVE_SID),
                                   ("pid", {"pid": 97, "start": self.T}, None)):
            with self.subTest(mode=mode, mine=mine):
                p = procs.kill_plan(mode, target, self.world(extra, {97: ("claude", LIVE_SID)},
                                                             mine=mine, pipes=pipes))
                self.assertEqual(p["kill"], [])
                self.assertIn("this ccwho run", self.whys(p))
        p = procs.kill_plan("mine", LIVE_SID, self.world(extra, {97: ("claude", LIVE_SID)}))  # control
        self.assertEqual(self.kills(p), [97])

    def test_an_agent_takes_nothing_a_person_would_be_told_about(self):
        cases = {"tmux": ({50: (1, self.T, "tmux new-session -d -s dev"), 51: (50, self.T, "-zsh"),
                           52: (51, self.T, "vim notes.md")}, {}),
                 "shell": ({50: (1, self.T, "-zsh")}, {}),
                 "host": ({50: (1, self.T, "dtach -n /tmp/s python3")}, {}),
                 "under a daemon": ({49: (1, self.T, "ollama serve"),
                                     50: (49, self.T, "/opt/ollama runner --port 51234")}, {50: [51234]}),
                 "app": ({50: (10, self.T, "/Applications/Docker.app/Contents/MacOS/Docker")}, {})}
        for label, (extra, ports) in cases.items():
            with self.subTest(case=label):
                marks = {k: ("claude", LIVE_SID) for k in extra}
                p = procs.kill_plan("mine", LIVE_SID, self.world(extra, marks, ports=ports))
                self.assertNotIn(50, self.kills(p))
                self.assertIn("no person is here to decide", self.whys(p))
                w = self.world(extra, marks, ports=ports, mine=None)                     # control: a person
                self.assertIn(50, self.kills(procs.kill_plan("pid", {"pid": 50, "start": self.T}, w)))
        extra = {50: (98, self.T, "node server.js")}                                    # control
        p = procs.kill_plan("mine", LIVE_SID, self.world({50: (10, self.T, "node server.js")},
                                                         {50: ("claude", LIVE_SID)}))
        self.assertEqual(self.kills(p), [50])

    def test_an_agent_refuses_what_was_not_read(self):
        extra = {70: (10, self.T, "tee out.log")}
        marks = {70: ("claude", LIVE_SID)}
        target = {"pid": 70, "start": self.T}
        full = {**self.BASE, **extra}
        for label, kw in {"pipes None": dict(pipes=None),
                          "member missing": dict(pipes={q: set() for q in full if q != 70}),
                          "text key": dict(pipes={"70": ["10"], **{q: set() for q in full if q not in (70, 10)}}),
                          "conns None": dict(conns=None)}.items():
            with self.subTest(case=label):
                p = procs.kill_plan("pid", target, self.world(extra, marks, **kw))
                self.assertEqual(p["kill"], [])
                p = procs.kill_plan("pid", target, self.world(extra, marks, mine=None, **kw))
                # control: a person - except the text key, which IS pid 70 piped to claude 10
                self.assertEqual(self.kills(p), [] if label == "text key" else [70])
        w = self.world(extra, marks)                                  # ports not read: None
        w["ports"] = None
        self.assertEqual(procs.kill_plan("pid", target, w)["kill"], [])
        w = self.world(extra, marks, mine=None)
        w["ports"] = None
        self.assertEqual(self.kills(procs.kill_plan("pid", target, w)), [70])     # control: a person
        p = procs.kill_plan("pid", target, self.world({**extra, 71: (70, self.T, "node vite")}, marks))
        self.assertEqual(p["kill"], [])                               # a member whose environment was not read
        self.assertEqual(self.kills(procs.kill_plan("pid", target, self.world(extra, marks))), [70])  # control

    def test_a_command_line_naming_a_session_is_no_mark(self):
        tail = f"tail -f /u/.claude/projects/p/{LIVE_SID}.jsonl"
        extra = {80: (1, self.T, tail)}
        for mode, target in (("mine", LIVE_SID), ("pid", {"pid": 80, "start": self.T})):
            with self.subTest(mode=mode):
                p = procs.kill_plan(mode, target, self.world(extra, {}, named={LIVE_SID: [80]}))
                self.assertEqual(p["kill"], [])
        p = procs.kill_plan("mine", LIVE_SID, self.world(extra, {80: ("claude", LIVE_SID)}))  # control
        self.assertEqual(self.kills(p), [80])
        p = procs.kill_plan("pid", {"pid": 80, "start": self.T},
                            self.world(extra, {}, named={LIVE_SID: [80]}, mine=None))
        self.assertIsNone(p["kill"][0]["marked"])                       # a guess is never the mark

    def test_a_person_is_told_what_a_command_line_names(self):
        extra = {20: (1, self.T, "/bin/zsh -c 'npm test'"),
                 21: (20, self.T, f"tail -f /u/.claude/projects/p/{self.OTHER}.jsonl"),
                 30: (1, self.T, "claude")}
        sessions = [{"sessionId": LIVE_SID, "pid": 10}, {"sessionId": self.OTHER, "pid": 30}]
        target = {"pid": 20, "start": self.T}
        w = self.world(extra, {20: ("claude", DEAD_SID)}, mine=None, named={self.OTHER: [21]},
                       sessions=sessions)
        p = procs.kill_plan("pid", target, w)
        self.assertEqual(self.kills(p), [20, 21])
        self.assertIn("not read", next(k["note"] for k in p["kill"] if k["pid"] == 21))
        w = self.world(extra, {20: ("claude", DEAD_SID), 21: ("claude", self.OTHER)}, mine=None,
                       sessions=sessions)                                                # control
        self.assertIn("carries another session's mark", self.whys(procs.kill_plan("pid", target, w)))

    def test_a_read_mark_is_the_mark_without_attribute(self):
        extra = {40: (1, self.T, "node s.js")}
        p = procs.kill_plan("pid", {"pid": 40, "start": self.T},
                            self.world(extra, {40: ("claude", DEAD_SID)}, mine=None, att=False))
        self.assertEqual(p["kill"][0]["marked"], DEAD_SID)
        self.assertNotIn("could not read", p["kill"][0].get("note", ""))
        w = self.world(extra, {}, mine=None, att=False)                                   # control
        w["marks"].pop(40, None)
        p = procs.kill_plan("pid", {"pid": 40, "start": self.T}, w)
        self.assertIsNone(p["kill"][0]["marked"])
        self.assertIn("could not read", p["kill"][0]["note"])

    def test_a_server_at_the_other_end_is_hedged(self):
        extra = {20: (1, self.T, "node server.js"), 400: (1, self.T, "postgres")}
        conns = [(20, "192.168.1.5", 55000, "192.168.1.5", 5432), (400, "192.168.1.5", 5432, "192.168.1.5", 55000)]
        p = procs.kill_plan("port", 55000, self.world(extra, {20: ("claude", DEAD_SID), 400: None}, mine=None,
                                                      ports={20: [55000], 400: [5432]}, conns=conns))
        # review 19: both ends listen - a libp2p client, or an outgoing connection:
        # ccwho cannot tell, so it says so, hedged
        self.assertIn("postgres (400) may be connected to :55000", self.notes(p))
        conns = [self.c(20, 55000, 61000), self.c(400, 61000, 55000)]                   # control: a client
        p = procs.kill_plan("port", 55000, self.world(extra, {20: ("claude", DEAD_SID), 400: None}, mine=None,
                                                      ports={20: [55000]}, conns=conns))
        self.assertIn("(400) is connected to :55000", self.notes(p))

    def test_a_pipe_says_piped_and_a_bool_is_no_pid(self):
        extra = {20: (1, self.T, "tee log"), 30: (1, self.T, "node reader.js"), 1: (0, self.T, "/sbin/launchd")}
        full = {**self.BASE, **extra}
        target = {"pid": 20, "start": self.T}
        p = procs.kill_plan("pid", target, self.world(extra, {20: ("claude", DEAD_SID)}, mine=None,
                                                      pipes={**{q: set() for q in full}, 20: {True}}))
        self.assertNotIn("True", self.notes(p))
        p = procs.kill_plan("pid", target, self.world(extra, {20: ("claude", DEAD_SID)}, mine=None,
                                                      pipes={**{q: set() for q in full}, 20: {30}}))
        self.assertIn("is piped to 30 (node)", self.notes(p))                           # control

    def test_attribute_text_is_printable_and_notes_are_all_said(self):
        extra = {51: (1, self.T, "-zsh")}
        full = {**self.BASE, **extra}
        att = {"unsure": [{"pid": 51, "why": "it is a shell someone may be working in and fine",
                           "session": "\x1b[2J", "marked": "\x1b[2J", "harness": "\x1b[2J"}]}
        w = {"table": full, "att": att, "sessions": [{"sessionId": LIVE_SID, "pid": 10}], "own": 99,
             "ports": {}, "marks": {51: None}, "pipes": {q: set() for q in full}, "connections": []}
        k = procs.kill_plan("pid", {"pid": 51, "start": self.T}, w)["kill"][0]
        self.assertIn("; it is a shell someone may be working in", k["note"])
        for key in ("session", "harness"):
            self.assertNotRegex(str(k[key]), r"[\x00-\x1f\x7f-\x9f]")
        att["unsure"][0]["why"] = "it is an app"                                          # control
        w["table"] = {**full, 51: (1, self.T, "/Applications/Slack.app/Contents/MacOS/Slack")}
        k = procs.kill_plan("pid", {"pid": 51, "start": self.T}, w)["kill"][0]
        self.assertEqual(k["note"].count("it is an app"), 1)

    def test_a_tree_piped_to_one_an_agent_leaves_is_left_too(self):
        # 60 reads tmux 50's pipe: 50 has a note (an agent leaves it), so 50
        # keeps running - and then 60 is piped to a live process: a note too
        extra = {50: (1, self.T, "tmux new-session -d"), 60: (1, self.T, "tee log")}
        full = {**self.BASE, **extra}
        pipes = {**{q: set() for q in full}, 60: {50}, 50: {60}}
        marks = {50: ("claude", LIVE_SID), 60: ("claude", LIVE_SID)}
        p = procs.kill_plan("mine", LIVE_SID, self.world(extra, marks, pipes=pipes))
        self.assertEqual(p["kill"], [])
        self.assertIn("piped to 50", self.whys(p))
        pipes = {q: set() for q in full}                                            # control
        self.assertEqual(self.kills(procs.kill_plan("mine", LIVE_SID, self.world(extra, marks, pipes=pipes))), [60])


class TestKillPlanReview19(unittest.TestCase):
    """Review 19: pipes are a graph - both directions, through what stays
    alive; one strict reading of the world: whole numbers only, and a
    malformed entry is not read."""

    T = "Tue Sep 22 13:45:15 2026"
    kills = TestKillPlanTrustsThePerson.kills
    whys = TestKillPlanTrustsThePerson.whys
    c = staticmethod(TestKillPlanPort.c)
    BASE = TestKillPlanReview18.BASE

    def world(self, extra, marks=None, pipes=None, mine=None, own=99, ports=None, conns=(), att=True):
        table = {**self.BASE, **extra}
        marks = {98: ("claude", LIVE_SID), **(marks or {})}
        sessions = [{"sessionId": LIVE_SID, "pid": 10}]
        ports = ports or {}
        good = {k: v for k, v in marks.items() if isinstance(v, tuple) and len(v) > 1}
        w = {"table": table, "sessions": sessions, "own": own, "ports": ports, "marks": marks,
             "att": procs.attribute(table, good, ports, sessions, own=99) if att else {},
             "pipes": {**{q: set() for q in table if isinstance(q, int) and q >= 1}, **(pipes or {})},
             "connections": list(conns)}
        if mine:
            w["mine"] = mine
        return w

    def pid(self, pid, w):
        return procs.kill_plan("pid", {"pid": pid, "start": self.T}, w)

    def test_a_pipe_chain_to_ccwho_or_an_agent_is_followed(self):
        # `ccwho | tee | head`: kill the head, the tee dies of it, then ccwho
        extra = {96: (98, self.T, "tee log"), 97: (98, self.T, "head -5")}
        p = self.pid(97, self.world(extra, pipes={99: {96}, 96: {99, 97}, 97: {96}}))
        self.assertEqual(p["kill"], [])
        self.assertIn("this ccwho run", self.whys(p))
        extra2 = {30: (1, self.T, "claude -p x"), 31: (1, self.T, "tee log"), 32: (1, self.T, "cat")}
        p = self.pid(32, self.world(extra2, pipes={30: {31}, 31: {30, 32}, 32: {31}}))
        self.assertEqual(p["kill"], [])
        self.assertIn("an agent", self.whys(p))
        # control: the middle is taken too (head under tee) - nothing stays alive between
        extra3 = {31: (1, self.T, "tee log"), 32: (31, self.T, "cat")}
        w = self.world(extra3, pipes={31: {32}, 32: {31}})
        self.assertEqual(self.kills(self.pid(31, w)), [31, 32])

    def test_a_pipe_known_from_the_other_side_counts(self):
        extra = {97: (98, self.T, "head -5")}
        w = self.world(extra, pipes={99: {97}})
        del w["pipes"][97]
        p = self.pid(97, w)
        self.assertEqual(p["kill"], [])
        self.assertIn("this ccwho run", self.whys(p))
        w = self.world({70: (1, self.T, "tee x")}, pipes={10: {70}})
        del w["pipes"][70]
        self.assertIn("an agent", self.whys(self.pid(70, w)))
        w = self.world(extra)                                                       # control
        del w["pipes"][97]
        p = self.pid(97, w)
        self.assertEqual(self.kills(p), [97])
        self.assertIn("pipes were not read", p["kill"][0]["note"])

    def test_attribute_skips_a_short_mark(self):
        att = procs.attribute({20: (1, self.T, "node s.js")}, {20: ("claude",)}, {}, [])
        self.assertEqual(att["left_behind"], [])
        att = procs.attribute({20: (1, self.T, "node s.js")}, {20: ("claude", DEAD_SID)}, {}, [])  # control
        self.assertEqual([p["pid"] for p in att["left_behind"]], [20])

    def test_what_is_echoed_has_no_control_characters(self):
        bad = "\x1b[2J" + self.T
        w = self.world({70: (1, bad, "node s.js")})
        p = procs.kill_plan("pid", {"pid": 70, "start": bad}, w)
        self.assertEqual(self.kills(p), [70])
        self.assertNotRegex(p["kill"][0]["start"], r"[\x00-\x1f\x7f-\x9f]")
        p = procs.kill_plan("pid", {"pid": 70, "start": self.T}, w)     # not the same start: raw compare
        self.assertEqual(p["kill"], [])
        self.assertEqual(self.pid(99, self.world({}))["spare"][0]["start"], self.T)   # control

    def test_a_client_that_also_listens_is_still_said(self):
        # libp2p dials from its listening port: both ends listen on the other's port
        extra = {20: (1, self.T, "node server.js"), 400: (1, self.T, "ipfs daemon")}
        conns = [(20, "127.0.0.1", 3000, "127.0.0.1", 4001), (400, "127.0.0.1", 4001, "127.0.0.1", 3000)]
        w = self.world(extra, {20: ("claude", LIVE_SID), 400: None}, ports={20: [3000], 400: [4001]},
                       conns=conns)
        p = procs.kill_plan("port", 3000, w)
        self.assertIn("ipfs (400) may be connected to :3000", p["kill"][0].get("note", ""))
        w["mine"] = LIVE_SID
        self.assertEqual(procs.kill_plan("port", 3000, w)["kill"], [])
        conns = [(20, "127.0.0.1", 3001, "127.0.0.1", 4001), (400, "127.0.0.1", 4001, "127.0.0.1", 3001)]
        w = self.world(extra, {20: ("claude", LIVE_SID), 400: None}, ports={20: [3000], 400: [4001]},
                       conns=conns)                                                  # control: outgoing
        self.assertNotIn("ipfs", procs.kill_plan("port", 3000, w)["kill"][0].get("note", ""))

    def test_marks_not_read_leave_marked_empty(self):
        extra = {80: (1, self.T, f"tail -f /u/.claude/projects/p/{LIVE_SID}.jsonl")}
        table = {**self.BASE, **extra}
        att = procs.attribute(table, {}, {}, [{"sessionId": LIVE_SID, "pid": 10}], own=99,
                              named={LIVE_SID: [80]})
        w = self.world(extra)
        w["att"], w["marks"] = att, None
        self.assertIsNone(self.pid(80, w)["kill"][0]["marked"])
        w = self.world({80: (1, self.T, "tail -f x")}, {80: ("claude", DEAD_SID)})     # control
        self.assertEqual(self.pid(80, w)["kill"][0]["marked"], DEAD_SID)



class TestKillPlanReview20(unittest.TestCase):
    """Review 20: a pipe has a direction - killing q ends what writes into q
    (SIGPIPE), not what only reads q's output (EOF). {"in", "out"} says which;
    a plain set does not, so it counts both ways. One root per tree in every
    mode; mapped loopback is loopback; pids are >= 1."""

    T = "Tue Sep 22 13:45:15 2026"
    kills = TestKillPlanTrustsThePerson.kills
    whys = TestKillPlanTrustsThePerson.whys
    world = TestKillPlanReview19.world
    pid = TestKillPlanReview19.pid
    BASE = TestKillPlanReview19.BASE

    def test_what_only_reads_a_process_refuses_too(self):
        # owner's choice (2026-09-26): any pipe link to an agent refuses - even
        # claude only reading a dev server's output (it would live on)
        extra = {300: (10, self.T, "/bin/zsh -c 'npm run dev'"), 301: (300, self.T, "node next dev")}
        w = self.world(extra, {300: ("claude", LIVE_SID), 301: ("claude", LIVE_SID)},
                       pipes={301: {"out": {10}}, 300: {"out": {10}}, 10: set()})
        p = self.pid(301, w)
        self.assertEqual(p["kill"], [])
        self.assertIn("301 is piped to 10, an agent", self.whys(p))
        w = self.world(extra, {300: ("claude", LIVE_SID), 301: ("claude", LIVE_SID)},
                       pipes={301: set(), 300: set(), 10: set()})                         # control: no pipe
        self.assertEqual(self.kills(self.pid(301, w)), [301])
    def test_a_hub_links_what_it_reads_to_an_agent(self):
        # the VS Code plugin host is an app: no pipe end itself (review 29), but
        # the walk goes through it to the claude that holds a pipe to it
        extra = {150: (1, self.T, "/Applications/Visual Studio Code.app/Contents/MacOS/Electron"),
                 200: (150, self.T, "/Applications/Visual Studio Code.app/Contents/Frameworks/Code Helper (Plugin).app/Contents/MacOS/Code Helper (Plugin)"),
                 11: (200, self.T, "claude"), 220: (200, self.T, "node tsserver.js")}
        p = self.pid(220, self.world(extra, pipes={220: {"out": {200}}, 11: {"out": {200}}, 200: set()}))
        self.assertEqual(p["kill"], [])
        self.assertIn("220 is piped to 200, and on to 11, an agent", self.whys(p))
        p = self.pid(220, self.world(extra, pipes={220: {"out": {200}}, 11: set(), 200: set()}))  # control
        self.assertEqual(self.kills(p), [220])
        self.assertIn("a claude runs under it", p["kill"][0]["note"])
    def test_a_nested_top_is_in_its_tree_once(self):
        extra = {70: (10, self.T, "node a.js"), 71: (70, self.T, "node b.js"), 72: (71, self.T, "node c.js")}
        for mid in (("claude", DEAD_SID), None):
            with self.subTest(mid=mid):
                w = self.world(extra, {70: ("claude", LIVE_SID), 71: mid, 72: ("claude", LIVE_SID)},
                               mine=LIVE_SID)
                p = procs.kill_plan("mine", LIVE_SID, w)
                self.assertEqual(p["kill"], [])
                self.assertEqual([s["pid"] for s in p["spare"]].count(72), 1)
        w = self.world(extra, {k: ("claude", LIVE_SID) for k in extra}, mine=LIVE_SID)   # control
        p = procs.kill_plan("mine", LIVE_SID, w)
        self.assertEqual([k["pid"] for k in p["kill"]], [70, 71, 72])

    def test_mapped_loopback_is_loopback(self):
        self.assertTrue(procs._loopback("::ffff:127.0.0.1"))
        self.assertFalse(procs._loopback("::ffff:10.0.0.5"))                     # control
        extra = {70: (1, self.T, "node server.js"),
                 80: (1, self.T, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")}
        conns = [(70, "::ffff:127.0.0.1", 3000, "::ffff:127.0.0.1", 52345), (80, "127.0.0.1", 52345, "127.0.0.1", 3000)]
        w = self.world(extra, {70: ("claude", DEAD_SID), 80: None}, ports={70: [3000]}, conns=conns)
        note = procs.kill_plan("port", 3000, w)["kill"][0]["note"]
        self.assertIn("Google Chrome (80) is connected to :3000", note)
        self.assertNotIn("another machine", note)

    def test_a_large_pipe_hub_is_quick(self):
        import time
        extra = {5000: (1, self.T, "cat")}
        pipes = {5000: set()}
        for i in range(1, 3001):
            extra[i + 1000] = (1, self.T, "node x.js")
            pipes[i + 1000] = {5000}
        marks = {k: ("claude", DEAD_SID) for k in extra}
        w = self.world(extra, marks, pipes=pipes)
        t0 = time.monotonic()
        p = procs.kill_plan("clean", None, w)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertIn(1001, self.kills(p))


    def test_a_writer_into_a_process_refuses(self):
        # `claude -p x | tee`: claude writes into the tee - and the other way round
        extra = {30: (1, self.T, "claude -p x"), 31: (1, self.T, "tee log")}
        for pipes in ({31: {"in": {30}}, 30: {"out": {31}}}, {31: {"out": {30}}, 30: {"in": {31}}}):
            with self.subTest(pipes=pipes):
                p = self.pid(31, self.world(extra, pipes=pipes))
                self.assertEqual(p["kill"], [])
                self.assertIn("an agent", self.whys(p))
        self.assertEqual(self.kills(self.pid(31, self.world(extra))), [31])            # control: no pipe
    def test_a_long_pipe_chain_is_followed_to_its_end(self):
        # ccwho | tee | tee | head: every hop, not two
        extra = {95: (98, self.T, "tee a"), 96: (98, self.T, "tee b"), 97: (98, self.T, "head -5")}
        pipes = {99: {95}, 95: {99, 96}, 96: {95, 97}, 97: {96}}
        p = self.pid(97, self.world(extra, pipes=pipes))
        self.assertEqual(p["kill"], [])
        self.assertIn("this ccwho run", self.whys(p))
        pipes = {95: {96}, 96: {95, 97}, 97: {96}, 99: set()}                  # control: not to ccwho
        self.assertEqual(self.kills(self.pid(97, self.world(extra, pipes=pipes))), [97])

    def test_a_peer_whose_own_pipes_were_not_read_is_followed_as_read(self):
        extra = {301: (1, self.T, "node next dev")}
        w = self.world(extra, pipes={301: {"out": {10}}})
        del w["pipes"][10]
        self.assertEqual(self.pid(301, w)["kill"], [])
        w = self.world(extra, pipes={301: set(), 10: set()})                             # control
        self.assertEqual(self.kills(self.pid(301, w)), [301])
    def test_a_mapped_client_is_matched_too(self):
        extra = {70: (1, self.T, "node server.js"),
                 80: (1, self.T, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")}
        conns = [(70, "127.0.0.1", 3000, "127.0.0.1", 52345), (80, "::ffff:127.0.0.1", 52345, "::ffff:127.0.0.1", 3000)]
        w = self.world(extra, {70: ("claude", DEAD_SID), 80: None}, ports={70: [3000]}, conns=conns)
        self.assertIn("Google Chrome (80) is connected to :3000",
                      procs.kill_plan("port", 3000, w)["kill"][0]["note"])


class TestKillPlanReview21(unittest.TestCase):
    """Review 21: a writer dies with q only through its own stdout/stderr
    ("out", fd 1 and 2); a note says what a pipe peer will do; a table or a
    pipes entry that is not whole is not read."""

    T = "Tue Sep 22 13:45:15 2026"
    kills = TestKillPlanTrustsThePerson.kills
    whys = TestKillPlanTrustsThePerson.whys
    world = TestKillPlanReview19.world
    pid = TestKillPlanReview19.pid
    BASE = TestKillPlanReview19.BASE

    def test_a_server_claude_feeds_refuses(self):
        # an MCP stdio server: claude would live on, but no direction is inferred
        extra = {200: (10, self.T, "node /x/playwright-mcp/cli.js")}
        for pipes in ({200: {"in": {10}, "out": {10}}, 10: {"in": set(), "out": set()}},
                      {200: {"in": {10}}, 10: {"out": {200}}}, {200: {10}}):
            with self.subTest(pipes=pipes):
                p = self.pid(200, self.world(extra, {200: ("claude", LIVE_SID)}, pipes=pipes))
                self.assertEqual(p["kill"], [])
                self.assertIn("an agent", self.whys(p))
        p = self.pid(200, self.world(extra, {200: ("claude", LIVE_SID)}))                  # control: no pipe
        self.assertEqual(self.kills(p), [200])
    def test_stderr_counts_like_any_fd(self):
        extra = {30: (1, self.T, "claude -p x"), 31: (1, self.T, "logger -t claude")}
        for pipes in ({30: {"out": {31}}, 31: {"in": {30}}}, {30: {"in": {31}}, 31: {"out": {30}}}):
            with self.subTest(pipes=pipes):
                p = self.pid(31, self.world(extra, pipes=pipes))
                self.assertEqual(p["kill"], [])
                self.assertIn("an agent", self.whys(p))
        self.assertEqual(self.kills(self.pid(31, self.world(extra))), [31])            # control
        self.assertIn("any fd", procs.kill_plan.__doc__)
    def test_a_note_names_every_pipe_peer(self):
        extra = {301: (1, self.T, "npm run build"), 302: (1, self.T, "tee build.log"),
                 303: (1, self.T, "grep ERROR")}
        pipes = {301: {"out": {302}}, 302: {"in": {301}, "out": {303}}, 303: {"in": {302}}}
        note = self.pid(302, self.world(extra, pipes=pipes))["kill"][0]["note"]
        self.assertIn("piped to 301 (npm), which may end with it", note)
        self.assertIn("piped to 303 (grep), which may end with it", note)
        self.assertNotIn("keeps running", note)
        w = self.world(extra, pipes={302: {301}, 303: set()})                             # 301 not read
        del w["pipes"][301]
        note = self.pid(302, w)["kill"][0]["note"]
        self.assertIn("piped to 301 (npm), whose own pipes were not read", note)
    def test_a_table_with_a_hole_was_not_read_whole(self):
        for label, table in (("short row", {40: (1, self.T, "tmux new -d"), 41: (40, self.T),
                                            42: (41, self.T, "claude")}),
                             ("bad key", {40: (1, self.T, "tmux new -d"), "x41": (40, self.T, "script"),
                                          42: (41, self.T, "claude")}),
                             ("missing parent", {40: (1, self.T, "tmux new -d"), 42: (41, self.T, "claude")})):
            with self.subTest(label=label):
                w = self.world(table, att=False)
                w["pipes"] = {k: v for k, v in w["pipes"].items() if isinstance(k, int)}
                p = self.pid(40, w)
                self.assertEqual(p["kill"], [])
                self.assertIn("process table was not read whole" if label == "missing parent"
                              else "not in its shape", p.get("why", ""))
        w = self.world({1: (0, self.T, "/sbin/launchd"), 70: (1, self.T, "sleep 9")})     # control
        self.assertEqual(self.kills(self.pid(70, w)), [70])



class TestKillPlanReview22(unittest.TestCase):
    """Review 22 and the owner's choice (2026-09-26): ANY pipe link from what a
    kill takes to a running agent or to ccwho refuses - any fd, any direction,
    through other processes. Nothing is inferred about who survives. Output
    fields are one line; a table hole refuses only when it could hide one."""

    T = "Tue Sep 22 13:45:15 2026"
    kills = TestKillPlanTrustsThePerson.kills
    whys = TestKillPlanTrustsThePerson.whys
    world = TestKillPlanReview19.world
    pid = TestKillPlanReview19.pid
    BASE = TestKillPlanReview19.BASE

    def test_any_pipe_link_to_an_agent_refuses(self):
        cases = {
            "mcp server claude feeds": ({200: (10, self.T, "node /x/playwright-mcp/cli.js")},
                                        {200: {"in": {10}, "out": {10}}, 10: {"in": set(), "out": set()}}, 200),
            "output claude reads": ({301: (10, self.T, "node next dev")}, {301: {"out": {10}}, 10: set()}, 301),
            "writer at fd 3": ({30: (1, self.T, "claude -p x"), 31: (1, self.T, "tee /dev/fd/63 out.log"),
                                32: (1, self.T, "grep ERROR")},
                               {30: {"out": {31}}, 31: {"in": {30}}, 32: {"in": {31}}}, 32),
            "shared read end": ({30: (1, self.T, "claude -p list"), 40: (1, self.T, "zsh -c loop"),
                                 41: (40, self.T, "node job.js")},
                                {30: {"out": {40, 41}}, 40: {"in": {30}}, 41: {"in": {30}}}, 41),
            "driver of a stream-json claude": ({30: (1, self.T, "python3 driver.py"),
                                                31: (1, self.T, "claude -p --input-format stream-json")},
                                               {30: {"out": {31}}, 31: {"in": {30}}}, 30)}
        for label, (extra, pipes, target) in cases.items():
            with self.subTest(case=label):
                p = self.pid(target, self.world(extra, pipes=pipes))
                self.assertEqual(p["kill"], [])
                self.assertIn("an agent", self.whys(p))
        extra = {31: (1, self.T, "tee log"), 32: (1, self.T, "grep x"), 33: (1, self.T, "npm run build")}
        p = self.pid(32, self.world(extra, pipes={33: {"out": {31}}, 31: {"in": {33}, "out": {32}},
                                                  32: {"in": {31}}}))                   # control
        self.assertEqual(self.kills(p), [32])
        self.assertIn("piped to 31 (tee)", p["kill"][0]["note"])

    def test_the_builder_contract_is_written_down(self):
        for words in ("any fd", "left out", "FIFO"):
            self.assertIn(words, procs.kill_plan.__doc__)

    def test_every_output_field_is_one_line(self):
        bad = "dddd\r\nKILL 1 | ok\x1b[2J\t"
        extra = {70: (1, self.T, "node s.js"), 71: (1, self.T, "cat")}
        w = self.world(extra, {70: ("claude", bad)}, pipes={70: {"55\n5", 71}})
        p = self.pid(70, w)
        for entry in p["kill"] + p["spare"]:
            for key, value in entry.items():
                if isinstance(value, str):
                    self.assertNotRegex(value, r"[\x00-\x1f\x7f-\x9f]", key)
        w = self.world(extra, {70: ("claude", DEAD_SID)})                               # control
        self.assertEqual(self.pid(70, w)["kill"][0]["marked"], DEAD_SID)

    def test_a_table_hole_refuses_only_what_it_could_hide(self):
        extra = {70: (1, self.T, "node server.js"), 4243: (4242, self.T, "sleep 5")}
        self.assertEqual(self.kills(self.pid(70, self.world(extra, att=False))), [70])
        for hidden in ("claude", "node /x/@anthropic-ai/claude-code/cli.js"):           # control
            with self.subTest(hidden=hidden):
                extra[4243] = (4242, self.T, hidden)
                p = self.pid(70, self.world(extra, att=False))
                self.assertEqual(p["kill"], [])
                self.assertIn("not read whole", p["why"])
                self.assertIn("run it again", p["why"])
        extra[4243] = (4242, self.T, "sleep 5")                          # a marked loose row: may be anyone's
        p = self.pid(70, self.world(extra, {4243: ("claude", LIVE_SID)}, att=False))
        self.assertEqual(p["kill"], [])
        self.assertIn("not read whole", p["why"])
        w = self.world({70: (1, self.T, "node s.js"), 41: (40, self.T)}, att=False)    # out of shape
        self.assertIn("not in its shape", self.pid(70, w)["why"])

    def test_the_kernels_row_is_no_hole(self):
        w = self.world({0: (0, self.T, "kernel_task"), 70: (1, self.T, "sleep 9")}, att=False)
        self.assertEqual(self.kills(self.pid(70, w)), [70])
        w = self.world({70: (1, self.T, "sleep 9"), 71: (4242, self.T, "claude")}, att=False)   # control
        self.assertIn("not read whole", self.pid(70, w).get("why", ""))


class TestKillPlanReview23(unittest.TestCase):
    """Review 23: every result leaves through one door that makes it one line;
    a pipes entry keeps the links it did read; every reading of a mark
    counts; launchd is no root."""

    T = "Tue Sep 22 13:45:15 2026"
    kills = TestKillPlanTrustsThePerson.kills
    whys = TestKillPlanTrustsThePerson.whys
    world = TestKillPlanReview19.world
    pid = TestKillPlanReview19.pid
    BASE = TestKillPlanReview19.BASE
    CTRL = r"[\x00-\x1f\x7f-\x9f]"

    def texts(self, p):
        out = [p.get("why", "")]
        for e in p["kill"] + p["spare"]:
            out += [v for v in e.values() if isinstance(v, str)]
        return out

    def test_every_door_out_is_one_line(self):
        mark = ("codex", "x\ny\tz")
        cases = {"different process": (self.world({70: (1, self.T, "node s.js")}, {70: mark}),
                                       "pid", {"pid": 70, "start": "Wed Sep 23 10:00:00 2026"}),
                 "cannot prove": (self.world({70: (1, "", "node s.js")}, {70: mark}),
                                  "pid", {"pid": 70, "start": self.T}),
                 "already exited": (self.world({}, {70: mark}), "pid", {"pid": 70, "start": self.T}),
                 "unknown mode": (self.world({}), "zz\n", None),
                 "a spared tree": (self.world({70: (1, self.T, "node a.js"), 71: (70, self.T, "claude")},
                                              {70: mark}), "pid", {"pid": 70, "start": self.T})}
        for label, (w, mode, target) in cases.items():
            with self.subTest(case=label):
                for text in self.texts(procs.kill_plan(mode, target, w)):
                    self.assertNotRegex(text, self.CTRL)
        w = self.world({70: (1, self.T, "node s.js")}, {70: ("claude", DEAD_SID)})     # control
        p = procs.kill_plan("pid", {"pid": 70, "start": "Wed Sep 23 10:00:00 2026"}, w)
        self.assertEqual(p["spare"][0]["marked"], DEAD_SID)
        self.assertIn("fresh", procs.kill_plan.__doc__)          # the signaller's rule is written down

    def test_launchd_is_no_root(self):
        extra = {1: (0, self.T, "/sbin/launchd"), 70: (1, self.T, "sleep 9")}
        p = procs.kill_plan("port", 22, self.world(extra, ports={1: [22]}))
        self.assertEqual(p["kill"], [])
        self.assertEqual([s["pid"] for s in p["spare"]], [1])
        self.assertIn("no process ccwho would kill", self.whys(p))
        w = self.world(extra, {1: ("claude", LIVE_SID)}, mine=LIVE_SID)
        # mine: launchd is never "what the agent started" - not taken, not listed (review 24)
        self.assertEqual([s["pid"] for s in procs.kill_plan("mine", LIVE_SID, w)["spare"]], [])
        self.assertEqual(self.kills(procs.kill_plan("port", 22, self.world(extra, ports={70: [22]}))), [70])

    def test_a_loose_ccwho_or_agent_refuses_and_says_which(self):
        w = self.world({40: (1, self.T, "tmux new -d"), 98: (41, self.T, "-zsh"),
                        99: (98, self.T, "python3 ccwho.py")}, att=False)
        p = self.pid(40, w)
        self.assertEqual(p["kill"], [])
        self.assertIn("not read whole", p["why"])
        self.assertIn("this ccwho run", p["why"])
        w = self.world({40: (1, self.T, "tmux new -d"), 70: (4242, self.T, "claude")}, att=False)
        self.assertIn("not read whole", self.pid(40, w)["why"])
        w = self.world({40: (1, self.T, "tmux new -d"), 41: (40, self.T, "-zsh"), 98: (41, self.T, "-zsh"),
                        99: (98, self.T, "python3 ccwho.py")}, att=False)               # control: whole
        self.assertIn("40 runs ccwho", self.whys(self.pid(40, w)))

    def test_a_pipe_to_ccwhos_shell_refuses(self):
        w = self.world({70: (1, self.T, "cat")}, pipes={98: {70}})
        p = self.pid(70, w)
        self.assertEqual(p["kill"], [])
        self.assertIn("this ccwho run", self.whys(p))
        w = self.world({70: (1, self.T, "cat"), 97: (1, self.T, "tee")}, pipes={97: {70}})   # control
        self.assertEqual(self.kills(self.pid(70, w)), [70])

    def test_the_table_rule_is_written_down(self):
        for words in ("run it again", "parent", "text"):
            self.assertIn(words, procs.kill_plan.__doc__)

    def test_a_member_read_as_unmarked_and_marked_was_not_read(self):
        extra = {70: (10, self.T, "node a.js"), 71: (70, self.T, "node b.js")}
        for order in ((71, "71"), ("71", 71)):
            with self.subTest(order=order):
                w = self.world(extra, {70: ("claude", LIVE_SID)}, mine=LIVE_SID, att=False)
                w["marks"][order[0]] = None
                w["marks"][order[1]] = ("claude", LIVE_SID)
                self.assertEqual(self.pid(70, w)["kill"], [])
        w = self.world(extra, {70: ("claude", LIVE_SID), 71: ("claude", LIVE_SID)}, mine=LIVE_SID, att=False)
        self.assertEqual(self.kills(self.pid(70, w)), [70, 71])                         # control


class TestKillPlanReview24(unittest.TestCase):
    """Review 24: no kill takes pid 0 or 1, root or member; a bare pipes value
    still links; every reading of a loose mark counts; format characters are
    no text a person should read."""

    T = "Tue Sep 22 13:45:15 2026"
    kills = TestKillPlanTrustsThePerson.kills
    whys = TestKillPlanTrustsThePerson.whys
    world = TestKillPlanReview19.world
    pid = TestKillPlanReview19.pid
    BASE = TestKillPlanReview19.BASE

    def test_no_kill_takes_pid_0_or_1(self):
        for low in (0, 1):
            with self.subTest(pid=low):
                w = self.world({70: (98 if low else 1, self.T, "node a.js"), low: (70, self.T, "kernel")},
                               {70: ("claude", DEAD_SID)})
                if low == 1:            # launchd as a member: a table where nothing hangs under it
                    w = self.world({}, att=False)
                    w["table"] = {70: (0, self.T, "node a.js"), 1: (70, self.T, "launchd"),
                                  99: (70, self.T, "python3 ccwho.py")}
                    w["table"][99] = (0, self.T, "python3 ccwho.py")
                p = self.pid(70, w)
                self.assertNotIn(low, self.kills(p))
                # review 27: pid 0 or 1 under another pid is a table out of shape
                self.assertIn("not in its shape", self.whys(p))
        w = self.world({70: (1, self.T, "node a.js"), 71: (70, self.T, "node b.js")}, {70: ("claude", DEAD_SID)})
        self.assertEqual(self.kills(self.pid(70, w)), [70, 71])                          # control

    def test_a_marked_launchd_hides_no_orphan_of_mine(self):
        w = self.world({1: (0, self.T, "/sbin/launchd"), 70: (1, self.T, "sleep 9")},
                       {1: ("claude", LIVE_SID), 70: ("claude", LIVE_SID)}, mine=LIVE_SID)
        p = procs.kill_plan("mine", LIVE_SID, w)
        self.assertEqual(self.kills(p), [70])
        w = self.world({1: (0, self.T, "/sbin/launchd"), 70: (1, self.T, "sleep 9")},       # control
                       {70: ("claude", LIVE_SID)}, mine=LIVE_SID)
        self.assertEqual(self.kills(procs.kill_plan("mine", LIVE_SID, w)), [70])

    def test_format_characters_are_not_shown(self):
        w = self.world({70: (1, self.T, "node s.js")}, {70: ("claude", "x‮y z‏")})
        k = self.pid(70, w)["kill"][0]
        self.assertNotRegex(k["marked"], "[‎‏‪-‮⁦-⁩  ]")
        w = self.world({70: (1, self.T, "node s.js")}, {70: ("claude", DEAD_SID)})          # control
        self.assertEqual(self.pid(70, w)["kill"][0]["marked"], DEAD_SID)

    def test_the_rules_are_written_down(self):
        for words in ("launchd", "world_problem"):
            self.assertIn(words, procs.kill_plan.__doc__)


class TestKillPlanReview25(unittest.TestCase):
    """Review 25: every input is read whole or not read - marks, sessions and
    att too. Sessions or att not read refuses for everyone (the live-session
    guard would be blind); a junk reading of a mark is no reading."""

    T = "Tue Sep 22 13:45:15 2026"
    SUB = "5ub5ub00-0000-4000-8000-000000000005"
    kills = TestKillPlanTrustsThePerson.kills
    whys = TestKillPlanTrustsThePerson.whys
    world = TestKillPlanReview19.world
    pid = TestKillPlanReview19.pid
    BASE = TestKillPlanReview19.BASE

    def test_a_live_sessions_mark_says_so_without_att(self):
        w = self.world({70: (1, self.T, "sleep 9")}, {70: ("claude", LIVE_SID)}, att=False)
        self.assertIn("work of a live session", self.pid(70, w)["kill"][0]["note"])
        w = self.world({70: (1, self.T, "sleep 9")}, {70: ("claude", DEAD_SID)}, att=False)  # control
        self.assertNotIn("live session", self.pid(70, w)["kill"][0].get("note", ""))

    def test_an_agent_takes_only_what_att_gives_its_own_session(self):
        OTHER = TestKillPlanReview17.OTHER
        w = self.world({70: (98, self.T, "sleep 9")}, {70: ("claude", LIVE_SID)}, mine=LIVE_SID, att=False)
        w["att"] = {"sessions": {OTHER: [{"pid": 70}]}}
        self.assertEqual(self.pid(70, w)["kill"], [])
        w["att"] = {"sessions": {LIVE_SID: [{"pid": 70}]}}                                  # control
        self.assertEqual(self.kills(self.pid(70, w)), [70])

    def test_every_text_can_be_printed(self):
        w = self.world({70: (1, self.T, "node s.js")}, {70: ("claude", "x\ud800y")})
        for e in self.pid(70, w)["kill"]:
            for v in e.values():
                if isinstance(v, str):
                    v.encode("utf-8")
        w = self.world({70: (1, self.T, "node s.js")}, {70: ("claude", "é日本")})           # control
        self.assertEqual(self.pid(70, w)["kill"][0]["marked"], "é日本")

    def test_a_pid_has_a_bound(self):
        w = self.world({2 ** 40: (1, self.T, "sleep 9")}, att=False)
        self.assertEqual(self.pid(2 ** 40, w)["kill"], [])
        p = procs.kill_plan("pid", {"pid": 2 ** 40, "start": self.T}, self.world({}, att=False))
        self.assertEqual(p.get("why"), "no pid given")             # a target too big is no pid
        w = self.world({9999999: (1, self.T, "sleep 9")}, att=False)                        # control
        self.assertEqual(self.kills(self.pid(9999999, w)), [9999999])

    def test_every_agent_test_is_the_same_test(self):
        for child in ("python3 -m agent /x/claude-code/run.py", "claude -p x"):
            with self.subTest(child=child):
                extra = {50: (98, self.T, "/bin/zsh -c x"), 51: (50, self.T, child), 52: (50, self.T, "sleep 9")}
                marks = {k: ("claude", LIVE_SID) for k in extra}
                p = procs.kill_plan("mine", LIVE_SID, self.world(extra, marks, mine=LIVE_SID))
                self.assertEqual(self.kills(p), [52])

    def test_the_input_shapes_are_written_down(self):
        for words in ("\"sessions\" [{", "att", "a list"):
            self.assertIn(words, procs.kill_plan.__doc__)

    def test_an_agent_path_named_directly_is_refused(self):
        cmd = "node --stack-size 4000 /x/@anthropic-ai/claude-code/cli.js"
        p = self.pid(21, self.world({21: (1, self.T, cmd)}))
        self.assertEqual(p["kill"], [])
        self.assertIn("may be an agent", self.whys(p))
        self.assertEqual(self.kills(self.pid(21, self.world({21: (1, self.T, "node server.js")}))), [21])


class TestWorldShape(unittest.TestCase):
    """The owner's choice (2026-09-26, after review 26): the world is built by
    ccwho's own reader, so one strict check reads it whole. Any part not in
    its shape refuses the whole plan, for everyone - a ccwho bug, not a fact
    about the processes. The "not read" forms are explicit: None for ports,
    pipes, marks or connections, and a pid left out of marks or pipes."""

    T = "Tue Sep 22 13:45:15 2026"
    world = TestKillPlanReview19.world
    BASE = TestKillPlanReview19.BASE

    def good(self):
        w = self.world({70: (1, self.T, "sleep 9")}, {70: ("claude", DEAD_SID)}, ports={70: [3000]},
                       conns=[(70, "127.0.0.1", 3000, "127.0.0.1", 50000)])
        w["sessions"].append({"sessionId": TestKillPlanReview17.OTHER, "kind": "background"})  # no pid yet
        return w

    BAD = {
        "world not a map": lambda w: None,
        "unknown key": lambda w: w.update(extra=1),
        "table missing": lambda w: w.pop("table"),
        "table key text": lambda w: w["table"].update({"71": (1, "T", "x")}),
        "table key bool": lambda w: w["table"].update({True: (0, "T", "x")}),
        "table key huge": lambda w: w["table"].update({10 ** 5000: (1, "T", "x")}),
        "table row short": lambda w: w["table"].update({71: (1, "T")}),
        "table ppid text": lambda w: w["table"].update({71: ("1", "T", "x")}),
        "table start int": lambda w: w["table"].update({71: (1, 12345, "x")}),
        "table command list": lambda w: w["table"].update({71: (1, "T", ["claude", "-p"])}),
        "own text": lambda w: w.update(own="99"),
        "own bool": lambda w: w.update(own=True),
        "sessions not a list": lambda w: w.update(sessions="x"),
        "session not a map": lambda w: w["sessions"].append(None),
        "session pid text": lambda w: w["sessions"].append({"sessionId": "s", "pid": "ten"}),
        "session pid float": lambda w: w["sessions"].append({"sessionId": "s", "pid": 30.0}),
        "session id int": lambda w: w["sessions"].append({"sessionId": 5, "pid": 30}),
        "att not a map": lambda w: w.update(att=None),
        "att list not a list": lambda w: w["att"].update(unsure=5),
        "att entry pid text": lambda w: w["att"].update(left_behind=[{"pid": "x"}]),
        "att why int": lambda w: w["att"].update(codex=[{"pid": 555, "why": 10 ** 5000}]),
        "att sessions not a map": lambda w: w["att"].update(sessions=[1]),
        "att pid in two groups": lambda w: w["att"].update(left_behind=[{"pid": 10}], codex=[{"pid": 10}]),
        "ports key text": lambda w: w["ports"].update({"70": [3000]}),
        "port huge": lambda w: w["ports"].update({70: [10 ** 5000]}),
        "port zero": lambda w: w["ports"].update({70: [0]}),
        "ports value set": lambda w: w["ports"].update({70: {3000}}),
        "pipes key text": lambda w: w["pipes"].update({"10x": {70}}),
        "pipe peer text": lambda w: w["pipes"].update({70: {"10"}}),
        "pipe peer bool": lambda w: w["pipes"].update({70: {True}}),
        "pipe peer float": lambda w: w["pipes"].update({70: {10.0}}),
        "pipe bare value": lambda w: w["pipes"].update({70: 10}),
        "pipe other key": lambda w: w["pipes"].update({70: {"err": set()}}),
        "marks key text": lambda w: w["marks"].update({"70": ("claude", DEAD_SID)}),
        "mark junk": lambda w: w["marks"].update({70: "7x"}),
        "mark short": lambda w: w["marks"].update({70: ("claude",)}),
        "mark sid int": lambda w: w["marks"].update({70: ("claude", 5)}),
        "conn short": lambda w: w.update(connections=[(70, "127.0.0.1", 3000)]),
        "conn port bool": lambda w: w.update(connections=[(70, "127.0.0.1", True, "127.0.0.1", 5)]),
        "conn port huge": lambda w: w.update(connections=[(70, "127.0.0.1", 10 ** 5000, "127.0.0.1", 5)]),
        "mine int": lambda w: w.update(mine=5),
        "own zero": lambda w: w.update(own=0),
        "table key negative": lambda w: w["table"].update({-5: (1, "T", "x")}),
        "session pid zero": lambda w: w["sessions"].append({"sessionId": "s", "pid": 0}),
        "pipe peer zero": lambda w: w["pipes"].update({70: {0}}),
        "mark starter text": lambda w: w["marks"].update({70: ("claude", DEAD_SID, "10")}),
        "att entry marked int": lambda w: w["att"].update(codex=[{"pid": 555, "marked": 5}]),
    }

    def test_every_malformed_part_refuses_the_whole_plan(self):
        import json
        for label, spoil in self.BAD.items():
            with self.subTest(part=label):
                w = self.good()
                w = spoil(w) if label == "world not a map" else (spoil(w), w)[1]
                self.assertIsNotNone(procs.world_problem(w))
                for mode, target in (("pid", {"pid": 70, "start": self.T}), ("port", 3000), ("clean", None)):
                    p = procs.kill_plan(mode, target, w)
                    self.assertEqual(p["kill"], [])
                    self.assertIn("not in its shape", p["why"])
                    json.dumps(p)

    def test_the_not_read_forms_are_no_malformation(self):
        for label, change in {"good": lambda w: None,
                              "ports None": lambda w: w.update(ports=None),
                              "pipes None": lambda w: w.update(pipes=None),
                              "marks None": lambda w: w.update(marks=None),
                              "connections None": lambda w: w.update(connections=None),
                              "a pid left out of marks": lambda w: w["marks"].pop(70),
                              "a pid left out of pipes": lambda w: w["pipes"].pop(70),
                              "a session without a pid": lambda w: w["sessions"].append({"sessionId": "q"}),
                              "a mark with its starter": lambda w: w["marks"].update({70: ("claude", DEAD_SID, 10)}),
                              "the kernel's row": lambda w: w["table"].update({0: (0, self.T, "kernel_task")}),
                              "directed pipes": lambda w: w["pipes"].update({70: {"in": set(), "out": [31]}})}.items():
            with self.subTest(form=label):
                w = self.good()
                change(w)
                self.assertIsNone(procs.world_problem(w))
                p = procs.kill_plan("pid", {"pid": 70, "start": self.T}, w)
                self.assertEqual([k["pid"] for k in p["kill"]], [70])


class TestKillPlanReview27(unittest.TestCase):
    """Review 27: the readers and the check agree - what mark_of and
    parse_lsof_listen give passes world_problem; the table has no loop (pid 0
    and 1 have parent 0); types are exact; att parts have their shape."""

    T = "Tue Sep 22 13:45:15 2026"
    kills = TestKillPlanTrustsThePerson.kills
    whys = TestKillPlanTrustsThePerson.whys
    world = TestKillPlanReview19.world
    pid = TestKillPlanReview19.pid
    BASE = TestKillPlanReview19.BASE

    def test_what_mark_of_gives_passes_the_check(self):
        for env in ({}, {"CLAUDE_CODE_SESSION_ID": DEAD_SID},
                    *({"CLAUDE_CODE_SESSION_ID": DEAD_SID, "CLAUDE_PID": v}
                      for v in ("0", "0000000", "1", "4x", "99999999", "4242")),
                    {"CODEX_THREAD_ID": "019a-thread"}):
            with self.subTest(env=env):
                w = self.world({70: (1, self.T, "sleep 9")})
                w["marks"][70] = procs.mark_of(env)
                self.assertIsNone(procs.world_problem(w))
        self.assertEqual(procs.mark_of({"CLAUDE_CODE_SESSION_ID": DEAD_SID, "CLAUDE_PID": "0"}),
                         ("claude", DEAD_SID))
        self.assertEqual(procs.mark_of({"CLAUDE_CODE_SESSION_ID": DEAD_SID, "CLAUDE_PID": "4242"}),
                         ("claude", DEAD_SID, 4242))                                   # control

    def test_what_lsof_gives_passes_the_check(self):
        self.assertEqual(procs.parse_lsof_listen("p13\nn*:0\n"), {})
        self.assertEqual(procs.parse_lsof_listen("p13\nn*:3000\n"), {13: [3000]})          # control
        w = self.world({13: (1, self.T, "node s.js")})
        w["ports"] = procs.parse_lsof_listen("p13\nn*:0\np13\nn127.0.0.1:3000\n")
        self.assertIsNone(procs.world_problem(w))

    def test_the_table_has_no_loop(self):
        for label, rows in (("launchd under ccwho", {98: (1, self.T, "-zsh"), 1: (99, self.T, "launchd"),
                                                     13: (1, self.T, "sleep 9")}),
                            ("kernel under ccwho", {0: (99, self.T, "kernel_task"), 13: (0, self.T, "sleep 9")}),
                            ("a loop", {70: (71, self.T, "a"), 71: (70, self.T, "b")})):
            with self.subTest(table=label):
                w = self.world(rows, pipes={13: {99}} if 13 in rows else None)
                self.assertIsNotNone(procs.world_problem(w))
                p = self.pid(13 if 13 in rows else 70, w)
                self.assertEqual(p["kill"], [])
                self.assertIn("not in its shape", p["why"])
        w = self.world({1: (0, self.T, "launchd"), 13: (1, self.T, "sleep 9")}, pipes={13: {99}})  # control
        self.assertIsNone(procs.world_problem(w))
        self.assertIn("this ccwho run", self.whys(self.pid(13, w)))

    def test_types_are_exact(self):
        class S(str):
            pass

        class P(int):
            pass

        class D(dict):
            pass
        spoil = {"start subclass": lambda w: w["table"].update({70: (1, S(self.T), "sleep 9")}),
                 "session pid subclass": lambda w: w["sessions"].append({"sessionId": "x", "pid": P(70)}),
                 "table a dict subclass": lambda w: w.update(table=D(w["table"])),
                 "session a dict subclass": lambda w: w["sessions"].append(D(sessionId="x", pid=70))}
        for label, change in spoil.items():
            with self.subTest(case=label):
                w = self.world({70: (1, self.T, "sleep 9")})
                change(w)
                self.assertIsNotNone(procs.world_problem(w))
        w = self.world({70: (1, self.T, "sleep 9")})                                      # control
        self.assertIsNone(procs.world_problem(w))
        w = D(w)
        self.assertIsNotNone(procs.world_problem(w))

    def test_att_parts_have_their_shape(self):
        for att in ({"unsure": ""}, {"left_behind": 0}, {"sessions": []}, {"sessions": ""}):
            with self.subTest(att=att):
                w = self.world({70: (1, self.T, "sleep 9")})
                w["att"] = att
                self.assertIsNotNone(procs.world_problem(w))
        for att in ({"unsure": []}, {"sessions": {}}, {}):                                 # control
            with self.subTest(ok=att):
                w = self.world({70: (1, self.T, "sleep 9")})
                w["att"] = att
                self.assertIsNone(procs.world_problem(w))

    def test_a_member_of_ccwhos_chain_is_refused_as_such(self):
        # defence in depth: a member that is this ccwho run's own is never taken
        w = self.world({97: (99, self.T, "ps -axo pid")})
        p = self.pid(97, w)
        self.assertEqual(p["kill"], [])
        self.assertIn("ccwho", self.whys(p))

    def test_a_session_without_a_pid_is_a_session_with_no_process(self):
        self.assertIn("a session with no process", procs.kill_plan.__doc__)


class TestKillPlanReview28(unittest.TestCase):
    """Review 28: a process with an agent under it is a pipe end too (a pty
    wrapper: `script ... claude`); every reader gives only what the check
    accepts; a session without a pid is read, with no process."""

    T = "Tue Sep 22 13:45:15 2026"
    kills = TestKillPlanTrustsThePerson.kills
    whys = TestKillPlanTrustsThePerson.whys
    world = TestKillPlanReview19.world
    pid = TestKillPlanReview19.pid
    BASE = TestKillPlanReview19.BASE

    def test_a_pipe_to_what_runs_an_agent_refuses(self):
        extra = {20: (1, self.T, "script -q /dev/null claude -p x"), 21: (20, self.T, "claude -p x"),
                 30: (1, self.T, "tail -f log"), 31: (1, self.T, "tee x")}
        for pipes in ({30: {20}, 20: {30}}, {30: {31}, 31: {30, 20}, 20: {31}}):
            with self.subTest(pipes=pipes):
                p = self.pid(30, self.world(extra, pipes=pipes))
                self.assertEqual(p["kill"], [])
                self.assertIn("a claude runs under 20", self.whys(p))
        extra[20], extra[21] = (1, self.T, "script -q /dev/null sleep 9"), (20, self.T, "sleep 9")   # control
        p = self.pid(30, self.world(extra, pipes={30: {20}, 20: {30}}))
        self.assertEqual(self.kills(p), [30])
        self.assertIn("piped to 20", p["kill"][0]["note"])

    def test_every_reader_gives_what_the_check_accepts(self):
        for text in ("p0\nn127.0.0.1:5->127.0.0.1:6\n", "p12345678\nn127.0.0.1:5->127.0.0.1:6\n",
                     "p²\nn1.2.3.4:1->1.2.3.4:2\n", "p13\nn1.2.3.4:²->5.6.7.8:9\n",
                     "p13\nn1.2.3.4:٣->5.6.7.8:9\n"):
            with self.subTest(established=text):
                got = procs.parse_lsof_established(text)
                if got is not None:
                    w = self.world({13: (1, self.T, "x")})
                    w["connections"] = got
                    self.assertIsNone(procs.world_problem(w))
        self.assertEqual(procs.parse_lsof_established("p13\nn127.0.0.1:5000->127.0.0.1:6000\n"),
                         [(13, "127.0.0.1", 5000, "127.0.0.1", 6000)])                  # control
        for text in ("123456789 Tue Sep 22 13:45:15 2026 x\n", "    0 Tue Sep 22 13:45:15 2026 kernel_task\n",
                     "² Tue Sep 22 13:45:15 2026 x\n"):
            with self.subTest(ps=text):
                for k in procs.parse_ps_table(text):
                    self.assertTrue(1 <= k < 10 ** 7)
        self.assertEqual(procs.parse_ps_table("12 Tue  Sep 22 13:45:15 2026 /a b/c\n"),          # control
                         {12: ("Tue Sep 22 13:45:15 2026", "/a b/c")})

    def test_a_session_without_a_pid_is_read_with_no_process(self):
        OTHER = TestKillPlanReview17.OTHER
        w = self.world({70: (1, self.T, "sleep 9")}, {70: ("claude", OTHER)})
        w["sessions"].append({"sessionId": OTHER})
        self.assertIsNone(procs.world_problem(w))
        self.assertIn("work of a live session", self.pid(70, w)["kill"][0]["note"])
        for bad in (0, "10"):                                                               # control
            w["sessions"][-1] = {"sessionId": OTHER, "pid": bad}
            self.assertEqual(procs.world_problem(w), "sessions")
        self.assertNotIn("without a whole pid - refuses", procs.kill_plan.__doc__)
        not_read = procs.world_problem.__doc__.split('"not read" forms')[1]
        self.assertNotIn("a session without a pid", not_read.split(".")[0] + not_read.split(".")[1])


class TestKillPlanReview29(unittest.TestCase):
    """Review 29: a pipe end is what runs an agent (or ccwho) up to the nearest
    app or multiplexer - the IDE that hosts claude in its terminal is not the
    agent: a pipe to it is a note that names the agent under it."""

    T = "Tue Sep 22 13:45:15 2026"
    IDEA = "/Applications/IntelliJ IDEA.app/Contents/MacOS/idea"
    kills = TestKillPlanTrustsThePerson.kills
    whys = TestKillPlanTrustsThePerson.whys
    world = TestKillPlanReview19.world
    pid = TestKillPlanReview19.pid
    BASE = TestKillPlanReview19.BASE

    def ide(self, **kw):
        extra = {150: (1, self.T, self.IDEA), 152: (150, self.T, "/bin/zsh -il"), 11: (152, self.T, "claude"),
                 400: (150, self.T, "node server.js"), **kw.pop("extra", {})}
        return self.world(extra, {400: ("claude", LIVE_SID)}, pipes={400: {"out": {150}}}, ports={400: [3000]},
                          **kw)

    def test_a_pipe_to_the_ide_that_hosts_claude_is_a_note(self):
        p = procs.kill_plan("port", 3000, self.ide())
        self.assertEqual(self.kills(p), [400])
        self.assertIn("piped to 150 (IntelliJ IDEA)", p["kill"][0]["note"])
        self.assertIn("a claude runs under it", p["kill"][0]["note"])
        self.assertEqual(procs.kill_plan("port", 3000, self.ide(mine=LIVE_SID))["kill"], [])   # an agent

    def test_a_pipe_to_the_ide_that_runs_ccwho_is_a_note(self):
        w = self.ide()
        w["table"][98] = (150, self.T, "/bin/zsh -il")            # ccwho's shell in the IDE terminal
        del w["table"][11]
        self.assertEqual(self.kills(procs.kill_plan("port", 3000, w)), [400])

    def test_the_ide_itself_is_still_never_taken(self):
        p = self.pid(150, self.ide())
        self.assertEqual(p["kill"], [])
        self.assertIn("a claude runs under 150", self.whys(p))

    def test_what_is_between_the_app_and_the_agent_is_still_an_end(self):
        extra = {150: (1, self.T, self.IDEA), 20: (150, self.T, "script -q /dev/null claude"),
                 21: (20, self.T, "claude"), 30: (1, self.T, "tail -f log")}
        p = self.pid(30, self.world(extra, pipes={30: {20}}))
        self.assertEqual(p["kill"], [])
        self.assertIn("a claude runs under 20", self.whys(p))

    def test_an_agent_is_named_with_the_right_article(self):
        extra = {20: (1, self.T, "script -q /dev/null zsh -c go"), 21: (20, self.T, "node /opt/claude-code/cli.js"),
                 30: (1, self.T, "tail -f log")}
        for target in (30, 20):
            with self.subTest(target=target):
                self.assertNotIn("a an ", self.whys(self.pid(target, self.world(extra, pipes={30: {20}}))))

    def test_the_wrapper_rule_is_written_down(self):
        self.assertIn("a process with an agent under it", procs.kill_plan.__doc__)
        self.assertIn("this ccwho run", procs.kill_plan.__doc__)

    def test_more_shapes(self):
        for conn in ((70, "", 5, "127.0.0.1", 6), (70, "127.0.0.1", 5, "", 6)):
            w = self.world({70: (1, self.T, "sleep 9")}, conns=[conn])
            self.assertEqual(procs.world_problem(w), "connections")
        w = self.world({70: (1, self.T, "sleep 9")})
        w["own"] = 1
        self.assertEqual(procs.world_problem(w), "own")
        w = self.world({70: (1, self.T, "sleep 9")})
        w["att"]["unsure"] = [{"pid": 555}]
        self.assertEqual(procs.world_problem(w), "att entries")
        self.assertIsNone(procs.world_problem(self.world({70: (1, self.T, "sleep 9")},       # control
                                                         conns=[(70, "127.0.0.1", 5, "127.0.0.1", 6)])))
