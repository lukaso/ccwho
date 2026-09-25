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

