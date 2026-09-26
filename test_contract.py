"""The shapes other things read: `--json` rows and the restore manifest.

These two are consumed by things this repo cannot see - a status line, a key
binding, a manifest written by a version of ccwho that is no longer installed.
So the rule is ADDITIVE ONLY: an existing key keeps its name and its meaning
forever, a new key is allowed and has to be listed here in the same commit that
adds it. A rename or a removal is a break, and it is silent everywhere except
here.

The allow-list is the test. Adding a field to the list is the deliberate act
that makes the new field legal; nothing enforces the meaning but this comment.

Stdlib only: python3 -m unittest -v
"""
import json
import unittest

import ccwho_engine as ccwho

# Every key `ccwho --json` has ever emitted per row. Append only.
ROW_KEYS = {
    "project", "status", "attention", "waitingFor", "name", "title", "doing",
    "ask", "since", "ts", "topic", "first", "age", "orphans", "work", "tty",
    "pid", "sessionId", "cwd",
    "tab_title",          # added 2026-09-21: the name iTerm2 shows for that tty
    "recap", "recap_ts", "recap_age", "turns_since_recap",   # added 2026-09-21
    "windowed",           # added 2026-09-22: is there a window to go to at all
    "kind",               # added 2026-09-22: interactive, or a background session
    "configDir",          # added 2026-09-24: set for a session in another config dir
    "procs", "ports",     # added 2026-09-24: the session's work processes, their ports
    "dead_loops",         # added 2026-09-24: its wait loops that can never end
    "entrypoint",         # added 2026-09-26: who started it - cli, or a program (sdk-*)
    "parked",             # added 2026-09-26: ids of terminals that parked this job
                          # (ctrl+b) - they show it, and have no row of their own
}

# Every key a manifest session entry has ever carried. Append only.
MANIFEST_SESSION_KEYS = {
    "sessionId", "cwd", "project", "topic", "first", "ask", "attention",
    "status", "tty", "pid", "since",
    "configDir",          # added 2026-09-24: resume runs in that dir; older
                          # manifests lack it and resume in the default dir
    "pane", "tabTitle",   # added 2026-09-26: the iTerm2 pane and its title, so
                          # a restore resumes in the pane iTerm2 brought back;
                          # older manifests lack them and open new windows
}

MANIFEST_TOP_KEYS = {"version", "savedAt", "count", "skipped", "skippedWhy", "sessions"}

SESSION = {"pid": 4242, "cwd": "/Users/x/projects/liveapp", "kind": "interactive",
           "startedAt": 1788200000000, "sessionId": "4f2b91ac-1111-4222-8333-abc",
           "name": "liveapp-4e", "status": "idle"}
HEAD = [json.dumps({"type": "user", "timestamp": "2026-09-01T10:00:00.000Z",
                    "message": {"role": "user", "content": "fix the gate"}})]
TAIL = [json.dumps({"type": "assistant", "timestamp": "2026-09-01T11:00:00.000Z",
                    "message": {"role": "assistant",
                                "content": [{"type": "text", "text": "done."}]}})]


class TestJsonRowShape(unittest.TestCase):
    def setUp(self):
        self.row = ccwho.build_row(SESSION, HEAD, TAIL, mtime=1788203600, now=1788207200)

    def test_no_key_was_removed_or_renamed(self):
        missing = ROW_KEYS - set(self.row)
        self.assertEqual(missing, set(),
                         "a consumer of --json reads these by name")

    def test_a_new_key_has_to_be_declared_here(self):
        undeclared = set(self.row) - ROW_KEYS
        self.assertEqual(undeclared, set(),
                         "adding a field is fine - add it to ROW_KEYS in the same commit")

    def test_the_row_is_json_serialisable(self):
        json.loads(json.dumps(self.row))          # --json would die on anything else


class TestManifestShape(unittest.TestCase):
    def setUp(self):
        row = ccwho.build_row(SESSION, HEAD, TAIL, mtime=1788203600, now=1788207200)
        row["tty"] = "ttys032"
        self.man = ccwho.manifest_from_rows([row], now=1788207200)

    def test_top_level_keys_are_stable(self):
        self.assertEqual(set(self.man), MANIFEST_TOP_KEYS)

    def test_session_entry_keys_are_stable(self):
        entry = self.man["sessions"][0]
        self.assertEqual(set(entry) - MANIFEST_SESSION_KEYS, set(),
                         "declare new manifest fields here in the same commit")
        self.assertEqual(MANIFEST_SESSION_KEYS - set(entry), set(),
                         "an older ccwho reading this manifest expects these")

    def test_version_is_the_one_restore_understands(self):
        self.assertEqual(self.man["version"], ccwho.MANIFEST_VERSION)

    def test_an_entry_still_builds_a_resume_command(self):
        cmd = ccwho.restore_command(self.man["sessions"][0])
        self.assertIn("claude --resume " + SESSION["sessionId"], cmd)
        self.assertTrue(cmd.startswith("cd "))


if __name__ == "__main__":
    unittest.main()
