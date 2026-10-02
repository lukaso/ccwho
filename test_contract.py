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
import os
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
                          # 2026-09-27: and its stdin readers - each still has
                          # pid and tasks; a reader adds kind, program and root
    "entrypoint",         # added 2026-09-26: who started it - cli, or a program (sdk-*)
    "parked",             # added 2026-09-26: ids of terminals that parked this job
                          # (ctrl+b) - they show it, and have no row of their own
    "terminal",           # added 2026-09-30: the app whose tab shows it (iterm2,
                          # terminal), "" when no app ccwho knows does
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
    "terminal",           # added 2026-09-30: the app it was in; older manifests
                          # lack it: a pane id means iTerm2, else the default app
}

MANIFEST_TOP_KEYS = {
    "version", "savedAt", "count", "skipped", "skippedWhy", "sessions",
    "boot",               # added 2026-10-01: the boot it was saved in; a save that
                          # could not ask iTerm2 copies panes only from a save of
                          # this boot; older manifests lack it and give none
}

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


class TestNoNameIsDefinedTwice(unittest.TestCase):
    """A second top-level `def` of the same name replaces the first without a
    word. 2026-09-27: a new `unix_sockets(pids)` was shadowed by the netstat
    `unix_sockets()` further down - every test injected a fake, so only the
    live list raised TypeError."""

    def test_each_module_defines_each_name_once(self):
        import ast
        import collections
        import glob
        here = os.path.dirname(os.path.abspath(__file__))
        files = sorted(glob.glob(os.path.join(here, "ccwho*.py"))) + [os.path.join(here, "ccwho")]
        self.assertGreater(len(files), 5, "the modules were not found")
        for path in files:
            with open(path) as f:
                tree = ast.parse(f.read())
            names = collections.Counter(
                n.name for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
            with self.subTest(module=os.path.basename(path)):
                self.assertEqual({k: v for k, v in names.items() if v > 1}, {})


class TestNoTestIsDefinedTwice(unittest.TestCase):
    """A second method of one name in a class replaces the first without a
    word, and its test never runs (two slices each added
    test_after_the_setup_tests, 2026-09-30)."""

    def test_every_test_method_has_a_name_of_its_own(self):
        import ast
        import collections
        import glob
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        twice, classes = [], 0
        for path in sorted(glob.glob(os.path.join(here, "test_*.py"))):
            with open(path) as fh:
                tree = ast.parse(fh.read())
            for c in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                classes += 1
                names = collections.Counter(f.name for f in c.body
                                            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)))
                twice += [f"{os.path.basename(path)} {c.name}.{n}" for n, k in names.items() if k > 1]
        self.assertGreater(classes, 100)                                 # control: it read them
        self.assertEqual(twice, [])


class TestEveryTestFileRunsInTheScript(unittest.TestCase):
    """`./test` is "every test, in one command": a test file it does not name
    never runs there, and a regression in it passes green (test_codex was
    missing, review 2026-09-29)."""

    @staticmethod
    def modules_run(script):
        """The modules `./test` runs whole: CORE, and the UI stage's bare ids -
        a stage that names one class of a module (module.Class) does not run
        the module (review 4 of the terminal-app slice 1)."""
        import re
        script = script.replace("\\\n", " ")
        core = re.search(r'^CORE="([^"]*)"', script, re.M).group(1).split()
        ui = re.search(r"python -m unittest ([\w. ]+?)\s+\"\$@\"", script).group(1).split()
        return set(core) | {t for t in ui if "." not in t}

    def script(self):
        import os
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test")) as fh:
            return fh.read()

    def test_every_test_module_is_named(self):
        import os
        import re
        here = os.path.dirname(os.path.abspath(__file__))
        run = self.modules_run(self.script())
        files = self.module_names(os.listdir(here))
        self.assertEqual(sorted(set(files) - run), [])
        self.assertIn("test_ui", run)                                    # control: it parsed

    @staticmethod
    def module_names(names):
        """The test modules among file names: every test_*.py."""
        import re
        return sorted(n[:-3] for n in names if re.fullmatch(r"test_\w+\.py", n))

    def test_a_name_with_digits_or_capitals_is_a_test_module(self):
        # test_[a-z_]+ skipped them: test_v2.py would never have run (2026-10-01)
        self.assertEqual(self.module_names(["test_a.py", "test_V2.py", "test_x_1.py",
                                            "testkit.py", "test_a.pyc", "test_.py.bak"]),
                         ["test_V2", "test_a", "test_x_1"])
        script = ('CORE="test_a test_b2"\n'
                  'exec uv run python -m unittest test_ui test_Ui3 test_x.Y \\\n  "$@"\n')
        self.assertEqual(self.modules_run(script), {"test_a", "test_b2", "test_ui", "test_Ui3"})

    def test_the_script_names_its_modules_once(self):
        # a list in a comment goes stale (it named eight of fourteen): CORE says
        comments = [l for l in self.script().splitlines() if l.startswith("#")]
        self.assertEqual([l for l in comments if "test_" in l], [])

    def test_a_class_a_stage_names_does_not_run_its_module(self):
        script = self.script()
        self.assertIn("test_terms.TestTheTestsPatchNamesThatExist", script)
        without = script.replace(" test_terms ", " ", 1)
        self.assertNotIn("test_terms", self.modules_run(without))
        self.assertIn("test_terms", self.modules_run(script))           # control
