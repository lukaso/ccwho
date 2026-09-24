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
