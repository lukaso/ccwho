"""ccwho_terms: the terminal apps ccwho talks to, one backend each, and the one
gate every background Apple Event goes through.

These tests never reach a real app: every ask is faked at ccwho_terms.ask, or
at the osascript the gate would start.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import time
import unittest

import ccwho_engine as engine
import testkit


def _unguarded(*a, **k):
    raise AssertionError("a test reached a real terminal app through ccwho_terms.ask")


class _Live:
    """The terminal module the engine uses NOW: a hot reload swaps it in
    sys.modules, and a guard put on the old one guards nothing."""

    def __getattr__(self, name):
        return getattr(engine.terms, name)

    def __setattr__(self, name, value):
        setattr(engine.terms, name, value)


terms = _Live()
REAL_ASK = None       # the live module's own gate, taken in setUpModule


def setUpModule():
    global REAL_ASK
    REAL_ASK = testkit.fresh_terms(engine).ask
    terms.ask = _unguarded


def tearDownModule():
    testkit.fresh_terms(engine)                 # no guard or fake of ours stays on it


class TestTheTerminalModuleStandsAlone(unittest.TestCase):
    """ccwho_terms is a rule module: the engine imports it, never the other
    way round, and a hot reload re-reads it before the engine."""

    def test_it_imports_only_rule_modules_re_read_before_it(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ccwho_terms.py")
        with open(path) as fh:
            tree = ast.parse(fh.read())
        names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        names += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        self.assertTrue(names, "the scan found no import at all")          # control
        order = list(engine.RELOAD_FIRST)
        before = order[:order.index("ccwho_terms")]
        self.assertEqual([n for n in names if n.startswith("ccwho") and n not in before], [])

    def test_a_reload_rereads_it_before_the_engine(self):
        self.assertIn("ccwho_terms", engine.RELOAD_FIRST)


class TestITerm2KeepsItsGateFiles(unittest.TestCase):
    """The lock is what keeps one background ask in flight across processes. A
    ccwho still running the code from before this move uses iterm-ae.lock: a
    new name would let both send at once."""

    def test_the_lock_and_state_keep_their_names(self):
        for part in ("lock", "json", "out", "err", "status"):
            self.assertEqual(terms._gate_path(terms.ITERM2, part),
                             os.path.join(terms.STATE_DIR, "iterm-ae." + part))

    def test_the_state_dir_is_ccwho_s_cache(self):
        self.assertEqual(terms.STATE_DIR, os.path.expanduser("~/.cache/ccwho"))


class TestEveryBackgroundAskGoesThroughTheGate(unittest.TestCase):
    """terms.ask is the one place a background Apple Event starts. Tab names and
    panes reach an app only through it - so a guard there covers them all."""

    def setUp(self):
        self.seen = []
        testkit.patch(self, engine.terms, "ask", lambda args, **k: self.seen.append((args, k)) or None)

    def test_tab_names_are_asked_through_it(self):
        self.assertIsNone(terms.ITERM2.titles())
        self.assertEqual(len(self.seen), 1)
        self.assertIs(self.seen[0][1]["app"], terms.ITERM2)

    def test_panes_are_asked_through_it(self):
        self.assertIsNone(terms.ITERM2.panes())
        self.assertEqual(len(self.seen), 1)
        self.assertIs(self.seen[0][1]["app"], terms.ITERM2)

    def test_the_guard_is_what_stops_it(self):                          # control
        # without the fake, the real ask would start osascript: here a fake
        # Popen records it instead, so the fake above is what kept it out
        started = []
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        live = engine.terms
        testkit.patch(self, live, "ask", REAL_ASK)
        testkit.patch(self, live, "app_snapshot",
                      lambda: "  PID   UID UCOMM\n  100   %d iTerm2\n" % os.getuid())
        testkit.patch(self, live, "STATE_DIR", tmp)

        def popen(*a, **k):
            started.append(a[0])
            raise OSError("not starting anything in a test")
        testkit.patch(self, subprocess, "Popen", popen)
        terms.ITERM2.titles()
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0][3], terms.OSASCRIPT)
        # and the gate it went through kept its state where the test put it:
        # a misnamed STATE_DIR would have used the real ~/.cache/ccwho
        self.assertTrue(os.path.exists(os.path.join(tmp, "iterm-ae.json")), os.listdir(tmp))


class TestWhoShowsATty(unittest.TestCase):
    """A tty belongs to an app when its first process was started by that app
    of ours, or by its daemon - read from ps alone, no Apple Event."""

    ME = os.getuid()

    def procs(self, *rows):
        return "  PID TT       UID UCOMM\n" + "".join(
            "%5d %-8s %5d %s\n" % (pid, tty, uid, name) for pid, tty, uid, name in rows)

    def ps(self, *rows):
        return "  PID  PPID COMMAND\n" + "".join("%5d %5d %s\n" % r for r in rows)

    def test_a_pane_of_the_iterm2_daemon_is_iterm2_s(self):
        p = self.procs((100, "??", self.ME, "iTerm2"), (200, "??", self.ME, "iTermServer-3.7."),
                       (300, "ttys024", 0, "login"))
        out = self.ps((100, 1, "/Applications/iTerm.app/Contents/MacOS/iTerm2"),
                      (200, 1, "iTermServer-3.7.3 /sock"), (300, 200, "/usr/bin/login -fpl me"))
        self.assertEqual(terms.owners(out, p), {"ttys024": 300})
        self.assertEqual(terms.ITERM2.ttys(out, p), {"ttys024"})

    def test_another_user_s_iterm2_owns_nothing(self):                  # control
        p = self.procs((100, "??", self.ME + 1, "iTerm2"), (300, "ttys024", 0, "login"))
        out = self.ps((100, 1, "iTerm2"), (300, 100, "login"))
        self.assertEqual(terms.owners(out, p), {})
        self.assertIsNone(terms.ITERM2.ttys(out, p))


    def test_a_pid_listed_twice_counts_by_either_row(self):
        # the engine's own ps rows (the parser both share): a second row for one
        # pid is not a second parent
        p = self.procs((100, "??", self.ME, "iTerm2"), (200, "??", self.ME, "iTermServer-3.7."),
                       (300, "ttys001", 0, "login"))
        for rows in (((300, 200, "login"), (300, 999, "login")),
                     ((300, 999, "login"), (300, 200, "login"))):     # a later row counts too
            out = self.ps((100, 1, "iTerm2"), (200, 1, "iTermServer-3.7.3"), *rows)
            self.assertEqual(terms.owners(out, p), {"ttys001": 300}, rows)



class TestAPatchPutsBackWhatItsObjectHeld(unittest.TestCase):
    """testkit.patch: a fake for one test, undone on the object it was put on -
    not on whatever the name leads to when the cleanup runs (a hot reload swaps
    ccwho_terms) - and to exactly what that object held (review 3 of slice 1:
    a restore that put back an app's method left a bound copy on the app)."""

    class App:
        def ask(self):
            return "the class's"

    def test_a_method_from_the_class_is_popped(self):
        app = self.App()
        undo = testkit.patch(self, app, "ask", lambda: "fake")
        self.assertEqual(app.ask(), "fake")
        undo()
        self.assertEqual(vars(app), {})
        self.assertEqual(app.ask(), "the class's")

    def test_a_guard_on_the_object_is_put_back(self):                  # control
        app = self.App()
        guard = app.ask = lambda: "guard"
        testkit.patch(self, app, "ask", lambda: "fake")()
        self.assertIs(vars(app)["ask"], guard)

    def test_a_module_attribute_is_put_back(self):
        import types
        mod = types.ModuleType("m")
        mod.STATE_DIR = "real"
        testkit.patch(self, mod, "STATE_DIR", "tmp")()
        self.assertEqual(mod.STATE_DIR, "real")

    def test_it_is_undone_on_its_own_object(self):
        import types
        holder, old, new = types.SimpleNamespace(), self.App(), self.App()
        holder.app = old
        undo = testkit.patch(self, holder.app, "ask", lambda: "fake")
        holder.app = new                                # what a reload does to the name
        undo()
        self.assertEqual((vars(old), vars(new)), ({}, {}))

    def test_a_name_the_object_does_not_have_is_refused(self):
        # a typo would put a fake nothing reads, and leave the real one on
        # (review 4 of slice 1): refused before anything is set
        app = self.App()
        with self.assertRaises(AttributeError):
            testkit.patch(self, app, "asks", lambda: "fake")
        self.assertEqual(vars(app), {})
        testkit.patch(self, app, "ask", lambda: "fake")                 # control

    def test_a_value_that_does_not_land_on_the_object_is_refused(self):
        # a proxy passes it on, a property keeps it elsewhere: the undo
        # could not find it - refused, and what was there is put back
        import types
        mod = types.ModuleType("m")
        mod.STATE_DIR = "real"

        class Proxy:
            def __getattr__(self, name):
                return getattr(mod, name)

            def __setattr__(self, name, value):
                setattr(mod, name, value)

        class Held:
            _v = "real"
            v = property(lambda self: self._v, lambda self, x: setattr(self, "_v", x))
        held = Held()
        for obj, name, before in ((Proxy(), "STATE_DIR", lambda: mod.STATE_DIR),
                                  (held, "v", lambda: held.v)):
            with self.assertRaises(TypeError):
                testkit.patch(self, obj, name, "tmp")
            self.assertEqual(before(), "real", name)

    def test_a_method_a_class_inherits_is_popped_from_it(self):
        class Sub(self.App):
            pass
        testkit.patch(self, Sub, "ask", lambda self: "fake")()
        self.assertNotIn("ask", vars(Sub))
        self.assertEqual(Sub().ask(), "the class's")

    def test_an_undo_after_a_later_patch_changes_nothing(self):
        app = self.App()
        guard = app.ask = lambda: "guard"
        first = testkit.patch(self, app, "ask", lambda: "fake1")
        testkit.patch(self, app, "ask", lambda: "fake2")
        with self.assertRaises(AssertionError):
            first()                                  # out of order: it would put back the guard
        self.doCleanups()                            # the later one, then the first (a no-op)
        self.assertIs(vars(app)["ask"], guard)

    def test_none_is_a_value_like_any_other(self):
        # None must not read as "not on the object" (review 5 of slice 1)
        import types
        mod = types.ModuleType("m")
        mod.STATE_DIR = "real"

        class Proxy:
            def __getattr__(self, name):
                return getattr(mod, name)

            def __setattr__(self, name, value):
                setattr(mod, name, value)
        with self.assertRaises(TypeError):
            testkit.patch(self, Proxy(), "STATE_DIR", None)
        self.assertEqual(mod.STATE_DIR, "real")
        testkit.patch(self, mod, "STATE_DIR", None)()                    # control
        self.assertEqual(mod.STATE_DIR, "real")

    def test_a_refused_patch_leaves_nothing_on_the_object(self):
        # refused before anything is set: no bound copy put back (review 5)
        app = self.App()

        class Proxy:
            def __getattr__(self, name):
                return getattr(app, name)

            def __setattr__(self, name, value):
                setattr(app, name, value)
        with self.assertRaises(TypeError):
            testkit.patch(self, Proxy(), "ask", lambda: "fake")
        self.assertEqual(vars(app), {})

    def test_an_undo_after_the_code_rebound_it_puts_the_original_back(self):
        # not a later patch: the code under test set it, or a tearDown
        # popped it - the original comes back all the same (review 5)
        app = self.App()
        guard = app.ask = lambda: "guard"
        undo = testkit.patch(self, app, "ask", lambda: "fake")
        app.ask = lambda: "set by the code"
        undo()
        self.assertIs(vars(app)["ask"], guard)
        undo = testkit.patch(self, app, "ask", lambda: "fake")
        del app.ask
        undo()
        self.assertIs(vars(app)["ask"], guard)

    def test_undo_twice_and_the_cleanup_do_nothing_more(self):
        app = self.App()
        undo = testkit.patch(self, app, "ask", lambda: "fake")
        undo()
        app.ask = later = lambda: "set after"
        undo()
        self.doCleanups()
        self.assertIs(vars(app)["ask"], later)


TERMINAL_APP = "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"


class TestTerminalAppIsABackend(unittest.TestCase):
    """Terminal.app, read from ps like iTerm2: its tabs' first process is the
    `login` it starts. It has no daemon - its shells end with it."""

    ME = os.getuid()
    procs = TestWhoShowsATty.procs
    ps = TestWhoShowsATty.ps

    def tables(self, uid=None):
        uid = self.ME if uid is None else uid
        p = self.procs((400, "??", uid, "Terminal"), (401, "ttys050", 0, "login"),
                       (402, "ttys050", uid, "zsh"))
        out = self.ps((400, 1, TERMINAL_APP), (401, 400, "login -pf me"), (402, 401, "-zsh"))
        return out, p

    def test_its_tabs_are_its(self):
        out, p = self.tables()
        self.assertEqual(terms.TERMINAL.pids(p), [400])
        self.assertEqual(terms.TERMINAL.owners(out, p), {"ttys050": 401})
        self.assertEqual(terms.TERMINAL.ttys(out, p), {"ttys050"})
        self.assertEqual(terms.owners(out, p), {"ttys050": 401})

    def test_another_user_s_terminal_app_is_not_ours(self):              # control
        out, p = self.tables(uid=self.ME + 1)
        self.assertEqual(terms.TERMINAL.pids(p), [])
        self.assertIsNone(terms.TERMINAL.ttys(out, p))

    def test_each_app_s_panes_are_its_own(self):
        p = self.procs((100, "??", self.ME, "iTerm2"), (300, "ttys024", 0, "login"),
                       (400, "??", self.ME, "Terminal"), (401, "ttys050", 0, "login"))
        out = self.ps((100, 1, "iTerm2"), (300, 100, "login"), (400, 1, TERMINAL_APP),
                      (401, 400, "login"))
        self.assertEqual(terms.survey(out, p), {"iterm2": ({"ttys024": 300}, True),
                                                "terminal": ({"ttys050": 401}, True)})
        self.assertEqual(terms.owners(out, p), {"ttys024": 300, "ttys050": 401})

    def test_it_has_its_own_gate_files(self):
        self.assertEqual(terms._gate_path(terms.TERMINAL, "lock"),
                         os.path.join(terms.STATE_DIR, "terminal-ae.lock"))
        self.assertNotEqual(terms.TERMINAL.gate, terms.ITERM2.gate)

    def test_its_tab_names_come_from_the_custom_title_never_the_screen(self):
        script = terms.TERMINAL.titles_script()
        self.assertIn("custom title", script)
        self.assertNotIn("contents", script)        # a tab's contents is its screen text
        self.assertIn('tell application "Terminal"', script)
        self.assertEqual(terms.parse_titles("/dev/ttys050\t\u2733 fixing it\n\n"),
                         {"ttys050": "\u2733 fixing it"})


class TestABackgroundAskChecksTheAppInsideTheScript(unittest.TestCase):
    """ps says the app runs; quit in the moment after, and `tell application`
    would start it again - osascript starts an app when it compiles a tell,
    before any `is running` check in the same script runs (probe 2026-09-29:
    TextEdit started). So the gate sends the tell inside `run script`, behind
    the check, and "not running" comes back as a refusal (D13)."""

    INNER = 'tell application "iTerm2"\n  return "a \\"quote\\" and a \\\\"\nend tell\n'

    def test_the_tell_is_inside_run_script_behind_the_check(self):
        script = terms.guarded(terms.TERMINAL, self.INNER)
        self.assertTrue(script.startswith('if application "Terminal" is running then\n'))
        self.assertIn("run script ", script)
        self.assertNotIn("\ntell application", script, "a tell outside run script")

    def test_the_inner_script_survives_the_quoting(self):
        script = terms.guarded(terms.ITERM2, self.INNER)
        lit = script[script.index("run script ") + len("run script "):script.index("\nend if")]
        self.assertTrue(lit.startswith('"') and lit.endswith('"'), lit)
        unescaped = re.sub(r'\\(.)', lambda m: {"n": "\n"}.get(m.group(1), m.group(1)), lit[1:-1])
        self.assertEqual(unescaped, self.INNER)

    def stub(self, text):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "osascript")
        with open(path, "w") as fh:
            fh.write('#!/bin/sh\nprintf "%%s" "$2" > "%s/sent"\n%s\n' % (tmp, text))
        os.chmod(path, 0o755)
        saved = (terms.STATE_DIR, terms.OSASCRIPT, terms.app_snapshot)
        self.addCleanup(lambda: [setattr(terms, n, v) for n, v in
                                 zip(("STATE_DIR", "OSASCRIPT", "app_snapshot"), saved)])
        terms.STATE_DIR, terms.OSASCRIPT = os.path.join(tmp, "state"), path
        terms.app_snapshot = lambda: ("  PID   UID UCOMM\n  100 %d iTerm2\n  400 %d Terminal\n"
                                      % (self.ME, self.ME))
        return tmp

    ME = os.getuid()

    def test_the_gate_sends_the_guarded_script(self):
        tmp = self.stub('echo "ttys050\tname"')
        why = {}
        self.assertEqual(REAL_ASK(["-e", self.INNER], app=terms.TERMINAL, why=why), "ttys050\tname\n")
        with open(os.path.join(tmp, "sent")) as fh:
            self.assertEqual(fh.read(), terms.guarded(terms.TERMINAL, self.INNER))

    def test_an_app_gone_before_the_script_ran_is_a_refusal(self):
        self.stub('echo "%s"' % terms.NOT_RUNNING)
        why = {}
        self.assertIsNone(REAL_ASK(["-e", self.INNER], app=terms.TERMINAL, why=why))
        self.assertEqual(why.get("refused"), "not-running")
        self.assertIs(why.get("asked"), False, "no event reached the app")

    def test_a_quarantine_is_one_app_s(self):
        # one state dir for both apps: Terminal.app stuck (-1712) stops asks to
        # it, never to iTerm2 - and iTerm2 answering does not free Terminal.app
        self.stub('case "$2" in *\'"Terminal"\'*) echo "execution error: AppleEvent timed out.'
                  ' (-1712)" >&2; exit 1;; *) echo ok;; esac')
        self.assertIsNone(REAL_ASK(["-e", "x"], app=terms.TERMINAL))
        why = {}
        self.assertIsNone(REAL_ASK(["-e", "x"], app=terms.TERMINAL, why=why))
        self.assertEqual(why.get("refused"), "stuck")
        self.assertEqual(REAL_ASK(["-e", "x"], app=terms.ITERM2), "ok\n")
        why = {}
        self.assertIsNone(REAL_ASK(["-e", "x"], app=terms.TERMINAL, why=why))
        self.assertEqual(why.get("refused"), "stuck", "iTerm2's answer freed Terminal.app")

    def test_a_malformed_ask_raises_before_the_gate_changes(self):
        tmp = self.stub("echo ok")
        with self.assertRaises(ValueError):
            REAL_ASK(["-e", "a", "-e", "b"], app=terms.ITERM2)
        state = os.path.join(tmp, "state")
        self.assertEqual([n for n in os.listdir(state) if not n.endswith(".lock")]
                         if os.path.isdir(state) else [], [])

    def test_the_guarded_scripts_compile(self):
        # compiling starts no app in this form (the tell is a string): osacompile
        # proves the quoting keeps every background script valid AppleScript
        import tempfile
        for app, script in ((terms.ITERM2, terms.ITERM2.titles_script()),
                            (terms.ITERM2, terms.ITERM2.panes_script()),
                            (terms.TERMINAL, terms.TERMINAL.titles_script())):
            with self.subTest(app=app.key), tempfile.NamedTemporaryFile(suffix=".scpt") as out:
                done = subprocess.run(["osacompile", "-o", out.name, "-e", terms.guarded(app, script)],
                                      capture_output=True, text=True, timeout=20)
                self.assertEqual(done.returncode, 0, done.stderr)



class TestWhatTheLastAskCameTo(unittest.TestCase):
    """doctor reads what an app's gate recorded of the list's own asks
    (gate_says) - it asks nothing itself: the first Apple Event to an app
    shows macOS's prompt (D7). Read from the files only (review of slices
    4-5: doctor used to ask Terminal.app, and so did setup)."""

    ME = os.getuid()
    stub = TestABackgroundAskChecksTheAppInsideTheScript.stub
    RUNS = "  PID   UID UCOMM\n  400 %d Terminal\n" % os.getuid()

    def says(self, procs=None):
        def nothing(*a, **k):
            raise AssertionError("gate_says started a process")
        undo = [testkit.patch(self, subprocess, "Popen", nothing),
                testkit.patch(self, subprocess, "run", nothing)]
        try:
            return terms.gate_says(terms.TERMINAL, procs=self.RUNS if procs is None else procs)
        finally:
            for u in reversed(undo):            # the test's own asks start processes
                u()

    def ask(self, text):
        self.stub(text)
        REAL_ASK(["-e", "x"], app=terms.TERMINAL)

    def stub_again(self, text):
        """A new osascript stub in the same state dir."""
        with open(terms.OSASCRIPT, "w") as fh:
            fh.write('#!/bin/sh\n%s\n' % text)

    def test_never_asked(self):
        self.stub("echo 1")
        self.assertIsNone(self.says())

    def test_answered(self):
        self.ask("echo 1")
        self.assertEqual(self.says(), "answered")

    def test_refused(self):
        self.ask('echo "execution error: Not authorized. (-1743)" >&2; exit 1')
        self.assertEqual(self.says(), "refused")

    def test_refused_and_settled_by_a_later_ask(self):
        self.ask('echo "execution error: Not authorized. (-1743)" >&2; exit 1')
        REAL_ASK(["-e", "x"], app=terms.TERMINAL)     # settles the first, then waits: not sent
        self.assertEqual(self.says(), "refused")

    def test_a_timeout_records_when_it_asks_again(self):
        # doctor says when (terms.STUCK_RETRY), not "has stopped asking"
        self.ask('echo "execution error: AppleEvent timed out. (-1712)" >&2; exit 1')
        got = terms.gate_retry_at(terms.TERMINAL)
        self.assertTrue(time.time() + terms.STUCK_RETRY - 30 < got <= time.time() + terms.STUCK_RETRY, got)

    def test_an_unsettled_timeout_has_no_retry_time_yet(self):
        # its -1712 came back and no ask has settled it: the next ask starts
        # a wait, so there is no time to tell yet (review 3 of env-panes)
        self.stub("echo 1")
        os.makedirs(terms.STATE_DIR, exist_ok=True)
        first = {"pending": True, "asked_pid": 400, "boot": terms.boot_id()}
        # the try after a wait that is over, timed out again
        again = dict(first, quarantine=400, last_error="-1712", strikes=1,
                     retry_at=time.time() - 5, slow=True)
        for state in (first, again):
            for name, text in (("json", json.dumps(state)), ("status", "1\n"),
                               ("err", "AppleEvent timed out. (-1712)"), ("out", "")):
                with open(terms._gate_path(terms.TERMINAL, name), "w") as fh:
                    fh.write(text)
            self.assertEqual(self.says(), "stuck", state)
            self.assertIsNone(terms.gate_retry_at(terms.TERMINAL), state)

    def gate_files(self, state, **files):
        """The Terminal.app gate as an ask left it: its state and its files."""
        self.stub("echo 1")
        os.makedirs(terms.STATE_DIR, exist_ok=True)
        with open(terms._gate_path(terms.TERMINAL, "json"), "w") as fh:
            json.dump(dict(state, boot=terms.boot_id()), fh)
        for name, text in files.items():
            with open(terms._gate_path(terms.TERMINAL, name), "w") as fh:
                fh.write(text)

    SHUT = {"quarantine": 400, "asked_pid": 400, "last_error": "-1712", "strikes": 1,
            "pending": True}

    def test_a_try_that_ended_with_no_code_reads_as_stuck_before_and_after(self):
        # _settle keeps such a gate shut, as stuck: gate_says must say so
        # before the next ask settles it too (review 4 of env-panes)
        for files in ({"status": "1\n", "err": ""}, {"err": ""}):          # killed; wrapper died
            self.gate_files(dict(self.SHUT, retry_at=time.time() - 5), **files)
            self.assertEqual(self.says(), "stuck", files)
            REAL_ASK(["-e", "x"], app=terms.TERMINAL)
            self.assertEqual(self.says(), "stuck", files)

    def test_a_try_with_no_code_names_the_quarantined_copies(self):
        # the try may reach two copies; the quarantine names the one that shut
        # it - with that one gone, ask asks at once, so it is not stuck (review 5)
        self.gate_files(dict(self.SHUT, asked_pid=[400, 500], retry_at=time.time() - 5),
                        status="1\n", err="")
        only_500 = "  PID   UID UCOMM\n  500 %d Terminal\n" % self.ME
        self.assertIsNone(self.says(procs=only_500))
        self.assertEqual(self.says(procs=only_500 + "  400 %d Terminal\n" % self.ME), "stuck")

    def test_a_status_left_after_its_settle_has_no_retry_time(self):
        # settled, but its status file not removed: the next ask settles it
        # again and the wait doubles - the time on record is not the one
        self.gate_files({"quarantine": 400, "asked_pid": 400, "last_error": "-1712", "strikes": 1,
                         "retry_at": time.time() + 600},
                        status="1\n", err="AppleEvent timed out. (-1712)")
        self.assertIsNone(terms.gate_retry_at(terms.TERMINAL))

    def test_with_no_quarantine_it_reads_as_failed(self):                     # control
        self.gate_files({"asked_pid": 400, "pending": True}, status="1\n", err="")
        self.assertEqual(self.says(), "failed")
        REAL_ASK(["-e", "x"], app=terms.TERMINAL)
        self.assertEqual(self.says(), "failed")

    def test_a_try_in_flight_or_whose_wrapper_died_has_no_retry_time(self):
        # pending, no status file: the next ask decides (review 4)
        self.gate_files(dict(self.SHUT, retry_at=time.time() - 5), err="AppleEvent timed out. (-1712)")
        self.assertIsNone(terms.gate_retry_at(terms.TERMINAL))

    def test_a_settled_wait_that_is_over_keeps_its_time(self):              # control
        self.ask('echo "execution error: AppleEvent timed out. (-1712)" >&2; exit 1')
        state = json.loads(open(terms._gate_path(terms.TERMINAL, "json")).read())
        state["retry_at"] = time.time() - 5
        with open(terms._gate_path(terms.TERMINAL, "json"), "w") as fh:
            json.dump(state, fh)
        self.assertLess(terms.gate_retry_at(terms.TERMINAL), time.time())

    def test_no_retry_time_without_a_quarantine(self):                     # control
        self.ask("echo 1")
        self.assertIsNone(terms.gate_retry_at(terms.TERMINAL))
        self.ask('echo "execution error: Not authorized. (-1743)" >&2; exit 1')
        self.assertIsNone(terms.gate_retry_at(terms.TERMINAL))

    def test_stuck_while_that_copy_runs(self):
        self.ask('echo "execution error: AppleEvent timed out. (-1712)" >&2; exit 1')
        self.assertEqual(self.says(), "stuck")
        restarted = "  PID   UID UCOMM\n  401 %d Terminal\n" % self.ME
        self.assertIsNone(self.says(restarted), "a new copy has not been asked")

    def test_the_app_quit_before_the_script_ran(self):
        self.ask('echo "%s"' % terms.NOT_RUNNING)
        self.assertIsNone(self.says())

    def test_another_error(self):
        self.ask('echo "execution error: odd. (-1728)" >&2; exit 1')
        self.assertEqual(self.says(), "failed")

    def state(self, **fields):
        """A gate state as an ask leaves it, in the stub's state dir."""
        import json
        tmp = self.stub("echo 1")
        d = os.path.join(tmp, "state")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, terms.TERMINAL.gate + ".json"), "w") as fh:
            json.dump(fields, fh)
        return d

    def hold_the_lock(self, d):
        import fcntl
        fd = os.open(os.path.join(d, terms.TERMINAL.gate + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)

    # review 2 of slices 4-5: an ask in flight, an ask whose wrapper died, and
    # what another boot left
    def age(self, d, seconds):
        """The gate's record made `seconds` ago."""
        path = os.path.join(d, terms.TERMINAL.gate + ".json")
        t = time.time() - seconds
        os.utime(path, (t, t))

    def test_an_ask_timed_out_whose_osascript_runs_is_slow(self):
        testkit.patch(self, engine.terms, "boot_id", lambda: "NOW")
        d = self.state(asked_pid=400, pending=True, slow=True, boot="NOW")
        self.hold_the_lock(d)
        self.assertEqual(self.says(), "slow")

    def test_an_ask_in_flight_longer_than_any_may_take_is_slow(self):
        # its list was killed while it waited: no one marked it slow
        testkit.patch(self, engine.terms, "boot_id", lambda: "NOW")
        d = self.state(asked_pid=400, pending=True, boot="NOW")
        self.hold_the_lock(d)
        self.age(d, terms.ASK_LONGEST + 5)
        self.assertEqual(self.says(), "slow")

    def test_a_healthy_ask_in_flight_is_not_slow(self):
        # review 3 of slices 4-5: every ask is pending while it runs - one
        # inside its time reads as what was recorded before it
        import threading
        self.ask("echo 1")                                  # answered before
        self.stub_again("sleep 0.6; echo 1")
        seen, done, raised = [], threading.Event(), []

        def work():
            try:
                REAL_ASK(["-e", "x"], app=terms.TERMINAL, timeout=5)
            except BaseException as ex:             # said below, never a hang
                raised.append(ex)
            finally:
                done.set()
        worker = threading.Thread(target=work)
        worker.start()
        deadline = time.time() + 10
        while not done.is_set() and time.time() < deadline:
            held = terms._lock_held(terms.TERMINAL)
            seen.append((held, terms.gate_says(terms.TERMINAL, procs=self.RUNS)))
            time.sleep(0.05)
        worker.join(10)
        self.assertEqual(raised, [])
        self.assertTrue(done.is_set(), "the ask did not end")
        self.assertNotIn("slow", [says for _held, says in seen])
        # the in-flight reading ran: while the ask held the lock, nothing
        # was known yet of this app but an answer from before - None
        self.assertIn((True, None), seen)

    def in_flight(self, **fields):
        testkit.patch(self, engine.terms, "boot_id", lambda: "NOW")
        d = self.state(asked_pid=400, pending=True, boot="NOW", **fields)
        self.hold_the_lock(d)
        return self.says()

    def test_an_ask_in_flight_after_a_refusal_is_refused(self):
        self.assertEqual(self.in_flight(last_error="-1743"), "refused")

    def test_a_first_ask_in_flight_is_not_known(self):
        # never answered: not "answered", not "slow" (review 4 of slices 4-5)
        self.assertIsNone(self.in_flight())

    def test_a_dead_wrapper_is_read_by_what_its_osascript_wrote(self):
        # review 3 of slices 4-5: as the next ask settles it - the err file
        testkit.patch(self, engine.terms, "boot_id", lambda: "NOW")
        for err, want in (("execution error: No. (-1743)", "refused"),
                          ("execution error: timed out. (-1712)", "stuck"),
                          ("", "failed")):
            d = self.state(asked_pid=400, pending=True, slow=True, boot="NOW")
            with open(os.path.join(d, terms.TERMINAL.gate + ".err"), "w") as fh:
                fh.write(err)
            self.assertEqual(self.says(), want, err)

    def test_no_boot_to_compare_keeps_stuck(self):
        # boot_id() unreadable: as ask does, the quarantine holds
        testkit.patch(self, engine.terms, "boot_id", lambda: None)
        self.state(asked_pid=400, quarantine=400, last_error="-1712", boot="OLD")
        self.assertEqual(self.says(), "stuck")

    def test_a_reader_is_not_a_holder(self):
        # two doctors reading at once: a shared look sees the lock free
        import fcntl
        d = self.state(asked_pid=400, pending=True, slow=True)
        fd = os.open(os.path.join(d, terms.TERMINAL.gate + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_SH)
        self.assertIs(terms._lock_held(terms.TERMINAL), False)

    def test_a_late_answer_dates_the_record(self):
        d = self.state(asked_pid=400)
        self.age(d, 600)
        with open(os.path.join(d, terms.TERMINAL.gate + ".status"), "w") as fh:
            fh.write("0\n")
        self.assertLess(time.time() - terms.gate_when(terms.TERMINAL), 30)

    def test_one_whose_osascript_is_gone_failed(self):
        testkit.patch(self, engine.terms, "boot_id", lambda: "NOW")
        self.state(asked_pid=400, pending=True, boot="NOW")
        self.assertEqual(self.says(), "failed")                   # the lock is free
        self.state(asked_pid=400, pending=True, slow=True, boot="NOW")
        self.assertEqual(self.says(), "failed")

    def test_stuck_in_another_boot_is_not(self):
        testkit.patch(self, engine.terms, "boot_id", lambda: "NOW")
        self.state(asked_pid=400, quarantine=400, last_error="-1712", boot="OLD")
        self.assertIsNone(self.says())
        self.state(asked_pid=400, quarantine=400, last_error="-1712", boot="NOW")
        self.assertEqual(self.says(), "stuck")                                  # control

    def test_stuck_when_ps_could_not_be_read(self):
        self.ask('echo "execution error: AppleEvent timed out. (-1712)" >&2; exit 1')
        self.assertEqual(self.says(procs=""), "stuck")

    def test_a_real_answer_after_the_app_quit_first(self):
        # one state dir for both asks: the sentinel first, an answer after
        self.ask('d=$(dirname "$0"); if [ -f "$d/once" ]; then echo 1;'
                 ' else touch "$d/once"; echo "%s"; fi' % terms.NOT_RUNNING)
        self.assertIsNone(self.says())
        REAL_ASK(["-e", "x"], app=terms.TERMINAL)
        self.assertEqual(self.says(), "answered")

    def test_a_list_killed_while_its_ask_waits(self):
        # the osascript lives on in a session of its own: slow while it
        # runs, what it ends with after
        import sys
        tmp = self.stub('sleep 1; echo "execution error: No. (-1743)" >&2; exit 1')
        code = ("import os, sys, ccwho_terms as t\n"
                "t.STATE_DIR, t.OSASCRIPT = sys.argv[1], sys.argv[2]\n"
                "t.app_snapshot = lambda: '  PID   UID UCOMM\\n  400 %d Terminal\\n' % os.getuid()\n"
                "t.ask(['-e', 'x'], app=t.TERMINAL, timeout=10)\n")
        state = os.path.join(tmp, "state")
        child = subprocess.Popen([sys.executable, "-c", code, state, os.path.join(tmp, "osascript")],
                                 cwd=REPO, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.addCleanup(lambda: (child.poll() is None and child.kill(), child.wait(timeout=10)))
        deadline = time.time() + 5
        while not os.path.exists(os.path.join(tmp, "sent")) and time.time() < deadline:
            time.sleep(0.02)
        child.kill()
        child.wait(timeout=10)
        self.age(state, terms.ASK_LONGEST + 5)       # no one marked it slow: its age does
        self.assertEqual(self.says(), "slow")
        status = os.path.join(state, terms.TERMINAL.gate + ".status")
        while not os.path.exists(status) and time.time() < deadline + 5:
            time.sleep(0.05)
        self.assertEqual(self.says(), "refused")

    def test_how_old_the_record_is(self):
        self.ask("echo 1")
        age = time.time() - terms.gate_when(terms.TERMINAL)
        self.assertTrue(0 <= age < 30, age)
        self.stub("echo 1")
        self.assertIsNone(terms.gate_when(terms.TERMINAL))                 # never asked

    def late(self, text):
        """An ask not answered in time, and what it says once its osascript
        ends - before any later ask settles it."""
        tmp = self.stub("sleep 0.5; " + text)
        self.assertIsNone(REAL_ASK(["-e", "x"], app=terms.TERMINAL, timeout=0.1))
        self.assertEqual(self.says(), "slow")
        status = os.path.join(tmp, "state", terms.TERMINAL.gate + ".status")
        deadline = time.time() + 5
        while not os.path.exists(status) and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(os.path.exists(status))
        return self.says()

    def test_an_ask_answered_late(self):
        self.assertEqual(self.late("echo 1"), "answered")

    def test_a_late_answer_that_the_app_quit_first(self):
        self.assertIsNone(self.late('echo "%s"' % terms.NOT_RUNNING))

    def test_a_late_refusal(self):
        self.assertEqual(self.late('echo "execution error: No. (-1743)" >&2; exit 1'), "refused")

    def test_a_late_timeout_error_on_a_copy_that_runs(self):
        self.assertEqual(self.late('echo "execution error: timed out. (-1712)" >&2; exit 1'), "stuck")


class TestAJumpGoesToTheAppThatShowsTheTty(unittest.TestCase):
    """Jump asks the app whose tab shows the tty. The tty goes in as an argument
    of its own - never into the script text - and the script says where focus
    landed (D9). Terminal.app's script lives in this module: the brew formula
    ships *.py by pattern, AppleScript files only by name."""

    def test_terminal_app_s_script_takes_the_tty_as_an_argument(self):
        args = terms.TERMINAL.jump_args("/dev/ttys050")
        self.assertEqual(args[0], "-e")
        self.assertEqual(args[-1], "/dev/ttys050")
        self.assertTrue(args[1].startswith("on run argv"))
        self.assertNotIn("ttys050", args[1])

    def test_a_tty_with_a_quote_stays_an_argument(self):
        odd = '/dev/ttys050"; do shell script "echo no'
        args = terms.TERMINAL.jump_args(odd)
        self.assertEqual(args[-1], odd)
        self.assertEqual(args[1], terms.TERMINAL.jump_args("/dev/ttys050")[1])   # the same script

    def test_iterm2_keeps_its_script_file(self):
        args = terms.ITERM2.jump_args("/dev/ttys024")
        self.assertEqual(args, [os.path.join(REPO, "jump.applescript"), "/dev/ttys024"])
        self.assertTrue(os.path.isfile(args[0]))

    def test_terminal_app_s_jump_script_compiles(self):
        done = testkit.compiles(self, terms.TERMINAL, terms.TERMINAL.jump_args("/dev/x")[1])
        self.assertEqual(done.returncode, 0, done.stderr)

    def fake_run(self, answer=None, raises=None):
        calls = []
        real = subprocess.run

        def run(argv, **kw):
            calls.append((argv, kw))
            if raises:
                raise raises
            return subprocess.CompletedProcess(argv, 0, stdout=answer or "", stderr="")
        subprocess.run = run
        self.addCleanup(setattr, subprocess, "run", real)
        return calls

    def test_focus_runs_the_app_s_jump_with_a_deadline(self):
        calls = self.fake_run("focused /dev/ttys050\n")
        self.assertEqual(terms.focus(terms.TERMINAL, "ttys050", 5.0), "focused /dev/ttys050")
        argv, kw = calls[0]
        self.assertEqual(argv, ["osascript", *terms.TERMINAL.jump_args("/dev/ttys050")])
        self.assertEqual(kw.get("timeout"), 5.0)

    def test_focus_says_which_app_did_not_answer(self):
        self.fake_run(raises=subprocess.TimeoutExpired("osascript", 5.0))
        self.assertEqual(terms.focus(terms.TERMINAL, "ttys050", 5.0), "Terminal.app did not answer in 5s")
        self.assertEqual(terms.focus(terms.ITERM2, "ttys024", 5.0), "iTerm2 did not answer in 5s")

    def test_a_row_names_its_app(self):
        self.assertIs(terms.app_of({"terminal": "terminal"}), terms.TERMINAL)
        self.assertIs(terms.app_of({"terminal": "iterm2"}), terms.ITERM2)
        self.assertIsNone(terms.app_of({"terminal": ""}))          # no app we know shows it
        self.assertIsNone(terms.app_of({"terminal": "ghostty"}))   # one this ccwho does not know
        # a row from an engine before the app was recorded: everything was iTerm2
        self.assertIs(terms.app_of({"tty": "ttys022"}), terms.ITERM2)


class TestTerminalAppOpensNewWindows(unittest.TestCase):
    """A session opened or restored in Terminal.app gets a new window, running
    its command (`do script`); a restore does not fill the windows Terminal.app
    reopens (D8, issue #30). Each script's first event writes nothing, so a
    refusal there means nothing was sent (AE_PROBE)."""

    def test_one_window_one_command(self):
        script = terms.TERMINAL.run_script('cd "/x y" && claude --resume abc')
        self.assertTrue(script.startswith(terms.TERMINAL.head))
        self.assertIn(terms.AE_PROBE, terms.TERMINAL.head)
        self.assertIn('do script %s' % terms.applescript_str('cd "/x y" && claude --resume abc'), script)

    def test_a_restore_opens_a_window_each_and_fills_none(self):
        script = terms.TERMINAL.open_script([("PANE-1", "one"), (None, "two")])
        self.assertEqual(script.count("do script "), 2)
        self.assertNotIn("PANE-1", script)
        self.assertEqual(terms.TERMINAL.open_script([]), "")

    def test_its_scripts_compile(self):
        for script in (terms.TERMINAL.run_script("claude --resume abc"),
                       terms.TERMINAL.open_script([(None, "one"), (None, "two")])):
            done = testkit.compiles(self, terms.TERMINAL, script)
            self.assertEqual(done.returncode, 0, done.stderr)


class TestANewWindowOpensWhereYouAre(unittest.TestCase):
    """With no record of a session's app (D6): the terminal ccwho runs in; with
    none (a link click), an app of ours that runs, iTerm2 first; with none
    running, iTerm2 when it is installed, else Terminal.app. Finding out never
    asks an app (no AppleScript)."""

    ME = os.getuid()

    def table(self, *names):
        return "  PID   UID UCOMM\n" + "".join("%5d %5d %s\n" % (100 + i, self.ME, n)
                                             for i, n in enumerate(names))

    def installed(self, yes):
        real = terms.ITERM2.installed
        terms.ITERM2.installed = lambda: yes
        self.addCleanup(terms.ITERM2.__dict__.pop, "installed", None)
        return real

    def test_the_terminal_ccwho_runs_in(self):
        def untouched():
            raise AssertionError("TERM_PROGRAM decides: no process table is taken")
        self.assertIs(terms.default_app({"TERM_PROGRAM": "Apple_Terminal"}, untouched), terms.TERMINAL)
        self.assertIs(terms.default_app({"TERM_PROGRAM": "iTerm.app"}, untouched), terms.ITERM2)

    def test_else_an_app_that_runs_iterm2_first(self):
        self.assertIs(terms.default_app({}, lambda: self.table("Terminal")), terms.TERMINAL)
        self.assertIs(terms.default_app({"TERM_PROGRAM": "vscode"},
                                        lambda: self.table("Terminal", "iTerm2")), terms.ITERM2)

    def test_else_iterm2_when_installed(self):
        self.installed(True)
        self.assertIs(terms.default_app({}, lambda: self.table("zsh")), terms.ITERM2)

    def test_else_terminal_app(self):                                           # control
        self.installed(False)
        self.assertIs(terms.default_app({}, lambda: self.table("zsh")), terms.TERMINAL)

    def test_not_knowing_picks_the_app_that_is_always_there(self):
        # None (Spotlight could not answer) is not a reason to pick iTerm2 for a
        # window nothing else chose: Terminal.app always is (a record that
        # names iTerm2 keeps it - it was there)
        none = "  PID UID UCOMM\n1 0 launchd\n"
        self.assertIs(terms.default_app({}, lambda: none, installed=lambda app: None), terms.TERMINAL)
        self.assertIs(terms.default_app({}, lambda: none, installed=lambda app: True), terms.ITERM2)

    def test_a_search_that_failed_is_not_knowing(self):
        # Spotlight that could not answer is not "not installed" (review 2
        # of slices 2-3): None
        testkit.patch(self, engine.terms, "APP_PATHS", {"iterm2": ["/nowhere/iTerm.app"]})
        for outcome in (OSError("gone"), subprocess.TimeoutExpired("mdfind", 5),
                        subprocess.CompletedProcess(["mdfind"], 1, stdout="", stderr="odd")):
            def run(cmd, *a, _o=outcome, **k):
                if isinstance(_o, BaseException):
                    raise _o
                return _o
            undo = testkit.patch(self, subprocess, "run", run)
            self.assertIsNone(terms.ITERM2.installed(), outcome)
            undo()
        undo = testkit.patch(self, subprocess, "run",
                             lambda cmd, *a, **k: subprocess.CompletedProcess(cmd, 0, stdout="", stderr=""))
        self.assertIs(terms.ITERM2.installed(), False)                          # control: none found
        undo()

    def test_installed_is_read_from_the_disk_never_asked(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        real_paths, real_run = terms.APP_PATHS, subprocess.run
        self.addCleanup(setattr, terms, "APP_PATHS", real_paths)
        self.addCleanup(setattr, subprocess, "run", real_run)
        ran = []
        subprocess.run = lambda argv, **k: ran.append(argv) or subprocess.CompletedProcess(
            argv, 0, stdout="", stderr="")
        terms.APP_PATHS = {"iterm2": [os.path.join(tmp, "iTerm.app")]}
        self.assertFalse(terms.ITERM2.installed())
        self.assertEqual([a[0] for a in ran], ["mdfind"])             # not osascript
        os.makedirs(os.path.join(tmp, "iTerm.app"))
        ran.clear()
        self.assertTrue(terms.ITERM2.installed())
        self.assertEqual(ran, [])                                    # found on disk: nothing run


class TestANewWindowIsOpenedWithADeadline(unittest.TestCase):
    """open_window: something you do, asked directly, never without a deadline."""

    def test_it_says_which_app_did_not_answer(self):
        real = subprocess.run
        self.addCleanup(setattr, subprocess, "run", real)
        seen = []

        def stuck(argv, **k):
            seen.append((argv, k))
            raise subprocess.TimeoutExpired(argv, k.get("timeout"))
        subprocess.run = stuck
        self.assertEqual(terms.open_window(terms.TERMINAL, "claude attach x", 5.0),
                         "Terminal.app did not answer in 5s")
        self.assertEqual(seen[0][0], ["osascript", "-e", terms.TERMINAL.run_script("claude attach x")])
        self.assertEqual(seen[0][1].get("timeout"), 5.0)


class TestAScanReadsTheTablesOnce(unittest.TestCase):
    """What every app shows is worked out from one reading of each table, however
    many apps there are (review of slice 2: each app parsed both again)."""

    def test_one_parse_of_each_table_for_all_apps(self):
        counts = {"procs": 0, "rows": 0}
        real_procs, real_rows = terms.parse_procs, terms.procs.ps_rows

        def parse_procs(text):
            counts["procs"] += 1
            return real_procs(text)

        def ps_rows(text):
            counts["rows"] += 1
            return real_rows(text)

        class Ghost(terms.App):
            key, label, script_name, gate = "ghost", "Ghost", "Ghost", "ghost-ae"

            def is_app(self, name):
                return name == "Ghost"
        apps = terms.APPS
        terms.APPS = apps + (Ghost(),)
        terms.parse_procs, terms.procs.ps_rows = parse_procs, ps_rows
        try:
            got = terms.survey(TestWhoShowsATty.ps(None, (400, 1, TERMINAL_APP), (401, 400, "login")),
                               TestWhoShowsATty.procs(None, (400, "??", os.getuid(), "Terminal"),
                                                      (401, "ttys050", 0, "login")))
        finally:
            terms.APPS, terms.parse_procs, terms.procs.ps_rows = apps, real_procs, real_rows
        self.assertEqual(counts, {"procs": 1, "rows": 1})
        self.assertEqual(got["terminal"], ({"ttys050": 401}, True))
        self.assertEqual(got["iterm2"], ({}, False))
        self.assertEqual(got["ghost"], ({}, False))


class TestTabNamesAskOnlyWhatTheCallerNames(unittest.TestCase):
    """titles_cached asks Terminal.app only when the caller says so (D7):
    called with no list of apps, it asks the apps that are asked whenever they
    run - iTerm2 - and never Terminal.app."""

    def test_no_list_never_asks_terminal_app(self):
        asked = []
        for app in terms.APPS:
            setattr(app, "titles", lambda timeout=5.0, _k=app.key, **k: asked.append(_k) or {})
            self.addCleanup(app.__dict__.pop, "titles", None)
        terms.titles_cached({})
        self.assertEqual(asked, ["iterm2"])


class TestARestoreAsksForPanesWithoutStartingITerm2(unittest.TestCase):
    """A restore asks iTerm2 for its panes directly - something you do - but
    never so that the ask itself starts iTerm2 (D13's form); not running is no
    panes (review of slices 2-3)."""

    def test_one_that_may_start_it_is_not_guarded(self):
        # a restore's --open: a window of iTerm2's follows anyway, and its
        # panes are worth finding as main did (review 2 of slices 2-3)
        sent = []

        class Done:
            returncode, stdout, stderr = 0, "", ""
        testkit.patch(self, subprocess, "run", lambda cmd, *a, **k: sent.append(cmd) or Done())
        terms.ITERM2.panes(direct=True, may_start=True)
        self.assertEqual(sent[0][-1], terms.ITERM2.panes_script())

    def test_the_direct_ask_is_guarded(self):
        sent = []

        class Done:
            returncode, stdout, stderr = 0, terms.NOT_RUNNING + "\n", ""
        testkit.patch(self, subprocess, "run", lambda cmd, *a, **k: sent.append(cmd) or Done())
        self.assertEqual(terms.ITERM2.panes(direct=True), {})
        self.assertIn('if application "iTerm2" is running then', sent[0][-1])
        self.assertIn("run script", sent[0][-1])


class TestACompileTestStartsNoApp(unittest.TestCase):
    """Compiling a tell to an app can start it (probe 2026-09-29), so a test
    compiles one only while a copy of ours already runs (testkit.compiles):
    else it skips, and osacompile never runs (review of slices 2-3)."""

    def setUp(self):
        self.ran = []
        real_run = subprocess.run
        self.addCleanup(setattr, subprocess, "run", real_run)
        subprocess.run = lambda cmd, *a, **k: self.ran.append(cmd) or real_run(["/usr/bin/true"])

    def test_not_running_it_skips_and_compiles_nothing(self):
        testkit.patch(self, engine.terms, "app_snapshot", lambda: "  PID UID UCOMM\n1 0 launchd\n")
        with self.assertRaises(unittest.SkipTest):
            testkit.compiles(self, terms.TERMINAL, 'tell application "Terminal" to count windows')
        self.assertEqual(self.ran, [])

    def test_running_it_compiles(self):                                       # control
        testkit.patch(self, engine.terms, "app_snapshot",
                      lambda: "  PID UID UCOMM\n555 %d Terminal\n" % os.getuid())
        testkit.compiles(self, terms.TERMINAL, 'tell application "Terminal" to count windows')
        self.assertEqual([c[0] for c in self.ran], ["osacompile"])

REPO = os.path.dirname(os.path.abspath(__file__))



def sandbox(test, prefix):
    """(dir, env, tripwire file, HOME) for a child test run: HOME a temp dir,
    and an osascript and osacompile first on PATH that note their call in the
    tripwire file and fail. Removed when `test` ends."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix=prefix)
    test.addCleanup(shutil.rmtree, tmp, True)
    home, bin_ = os.path.join(tmp, "home"), os.path.join(tmp, "bin")
    os.makedirs(home)
    os.makedirs(bin_)
    trip = os.path.join(tmp, "osascript-called")
    for tool in ("osascript", "osacompile"):
        with open(os.path.join(bin_, tool), "w") as fh:
            fh.write('#!/bin/sh\necho "%s $@" >> "%s"\nexit 1\n' % (tool, trip))
        os.chmod(os.path.join(bin_, tool), 0o755)
    env = dict(os.environ, HOME=home, PATH=bin_ + os.pathsep + os.environ.get("PATH", ""),
               PYTHONDONTWRITEBYTECODE="1")
    env.pop("CCWHO_DIR", None)
    return tmp, env, trip, home


class TestGuardsHoldWhenModulesShareAProcess(unittest.TestCase):
    """./test runs every CORE module in ONE process, and a hot-reload test there
    swaps ccwho_terms. A guard, a fake or a real function kept from the old
    module then reaches the real ~/.cache/ccwho, or a real osascript (review of
    slice 1). Each order below runs in a child of its own: in a copy of the
    repo (a reload test rewrites module files), with HOME a temp dir and a
    tripwire osascript first on PATH."""

    def run_in_order(self, *ids):
        import shutil
        import sys
        tmp, env, trip, home = sandbox(self, "ccwho-order-")
        repo = os.path.join(tmp, "repo")
        shutil.copytree(REPO, repo, ignore=shutil.ignore_patterns(".git", "__pycache__"))
        # the timeout kills the python child itself (no coreutils needed)
        done = subprocess.run([sys.executable, "-m", "unittest", *ids], cwd=repo, env=env,
                              capture_output=True, text=True, timeout=25)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        self.assertFalse(os.path.exists(trip), "a test ran the osascript on PATH")
        # every gate makes its state dir before anything else
        self.assertFalse(os.path.exists(os.path.join(home, ".cache", "ccwho")),
                         "a test used the real gate state under HOME")

    RELOAD = "test_ccwho.TestReloadPutsEarlierModulesBackWhenALaterOneFails"

    def test_a_reload_then_this_module(self):
        self.run_in_order(self.RELOAD, "test_terms.TestEveryBackgroundAskGoesThroughTheGate")

    def test_a_reload_then_this_module_then_the_real_gate(self):
        self.run_in_order(self.RELOAD, "test_terms.TestTheTerminalModuleStandsAlone",
                          "test_ccwho.TestBackgroundAsksNeverPileUpInITerm2.test_an_answer_comes_back")

    def test_a_lock_that_cannot_work_is_put_back(self):
        self.run_in_order("test_ccwho.TestTheGateSaysAnUnreadableTableAsOne.test_a_lock_that_cannot_work",
                          "test_ccwho.TestTheGateWritesOneIterm2AsOnePid.test_one")

    def test_the_runner_s_guards_come_off(self):
        self.run_in_order("test_runner.TestRestoreDir",
                          "test_ccwho.TestBackgroundAsksNeverPileUpInITerm2.test_an_answer_comes_back")

    def test_a_brief_reload_then_a_scan(self):
        self.run_in_order(
            "test_ccwho.TestTheListsReloadCoversEveryRuleModule"
            ".test_an_edit_that_blows_up_at_import_leaves_the_old_rules_working",
            "test_ccwho.TestCollectTakesWindowsFromPsNotFromTabNames"
            ".test_a_scan_runs_ps_once_even_when_iterm2_is_not_asked")

    def test_one_module_alone(self):                                    # control
        self.run_in_order("test_terms.TestITerm2KeepsItsGateFiles")

    def test_the_setup_tests_reach_no_app(self):
        # doctor's facts reach the machine; its tests reach no app: not even
        # the Accessibility probe's System Events (review of slices 4-5)
        self.run_in_order("test_setup")


# Run in a child after a test module has ended: is the terminal module the
# engine uses untouched - no guard, no fake, its own functions?
PRISTINE = """
import sys, unittest
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
ran = unittest.TextTestRunner(stream=sys.stderr).run(suite)
import ccwho_engine as engine
t, bad = engine.terms, []
for app in ("ITERM2", "TERMINAL"):
    if vars(getattr(t, app)):
        bad.append(app + " keeps " + ",".join(sorted(vars(getattr(t, app)))))
own = engine._load_beside(t)            # what its file makes, beside it
for name in ("APP_PATHS", "STATE_DIR", "OSASCRIPT", "TITLES_TTL", "PANES_WAIT"):
    if getattr(t, name) != getattr(own, name):
        bad.append(name + " is not the file's")
if [type(a).__name__ for a in t.APPS] != [type(a).__name__ for a in own.APPS]:
    bad.append("APPS is not the file's")
for name in ("ask", "app_snapshot"):
    f = getattr(t, name)
    code = getattr(f, "__code__", None)
    if (getattr(f, "__qualname__", "") != name or code is None
            or not code.co_filename.endswith("ccwho_terms.py") or f.__globals__ is not vars(t)):
        bad.append(name + " is not the module's own")
failed = [t.id() for t, _ in ran.failures + ran.errors]
print("RESULT", ran.wasSuccessful(), bad, failed)
"""


class TestAModuleLeavesTheTerminalModuleUntouched(unittest.TestCase):
    """A test module that put guards and fakes on ccwho_terms takes them off
    when it ends (testkit.fresh_terms): the next module - one that does not
    freshen it itself - must find the real functions (review 2 of slice 1)."""

    def after(self, *ids, path=None):
        # under its own HOME and a tripwire osascript: a guard that leaked
        # must not reach the machine where it is found (review 4 of slice 1)
        import sys
        _tmp, env, trip, home = sandbox(self, "ccwho-pristine-")
        if path:
            env["PYTHONPATH"] = path
        done = subprocess.run([sys.executable, "-c", PRISTINE, *ids], cwd=REPO, env=env,
                              capture_output=True, text=True, timeout=25)
        line = [x for x in done.stdout.splitlines() if x.startswith("RESULT")]
        self.assertFalse(os.path.exists(trip), "a test ran the osascript on PATH (the tripwire)")
        self.assertFalse(os.path.exists(os.path.join(home, ".cache", "ccwho")),
                         "a test used the gate's state under HOME")
        self.assertEqual(line, ["RESULT True [] []"], done.stdout[-1000:] + done.stderr[-2000:])

    def test_a_fake_on_any_app_or_a_changed_constant_is_found(self):
        # every app and the module's constants, not only ITERM2 (review of
        # slices 4-5)
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "a_leaky_module.py"), "w") as fh:
            fh.write("import unittest\nimport ccwho_engine as engine\n\nclass T(unittest.TestCase):\n"
                     "    def test_it_leaves_things(self):\n"
                     "        engine.terms.TERMINAL.titles = lambda **k: {}\n"
                     "        engine.terms.APP_PATHS = {}\n")
        with self.assertRaises(AssertionError) as caught:
            self.after("a_leaky_module", path=tmp)
        self.assertIn("TERMINAL keeps titles", str(caught.exception))
        self.assertIn("APP_PATHS is not the file's", str(caught.exception))

    def test_a_child_test_that_fails_is_named(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "a_failing_module.py"), "w") as fh:
            fh.write("import unittest\n\nclass T(unittest.TestCase):\n"
                     "    def test_it_fails_here(self):\n        self.fail('x' * 5000)\n")
        with self.assertRaises(AssertionError) as caught:
            self.after("a_failing_module", path=tmp)
        # named on the RESULT line: a long message does not push it out
        self.assertIn("test_it_fails_here", str(caught.exception))

    def test_a_child_test_that_fails_says_why(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "a_failing_module.py"), "w") as fh:
            fh.write("import unittest\n\nclass T(unittest.TestCase):\n"
                     "    def test_it_fails_here(self):\n        self.fail('on purpose')\n")
        with self.assertRaises(AssertionError) as caught:
            self.after("a_failing_module", path=tmp)
        self.assertIn("on purpose", str(caught.exception))

    def test_a_child_that_makes_the_gate_state_is_caught_there(self):
        # no osascript started: the gate made its state dir, then refused
        # (no app of ours) - the HOME check alone finds it (review 5)
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "a_state_module.py"), "w") as fh:
            fh.write("import unittest\nimport ccwho_engine as engine\n\n"
                     "class T(unittest.TestCase):\n    def test_it_asks(self):\n"
                     "        t = engine.terms\n"
                     "        t.app_snapshot = lambda: '  PID UID UCOMM\\n1 0 launchd\\n'\n"
                     "        t.ITERM2.ask(['-e', 'return 1'])\n")
        with self.assertRaises(AssertionError) as caught:
            self.after("a_state_module", path=tmp)
        self.assertIn("HOME", str(caught.exception))
        self.assertNotIn("tripwire", str(caught.exception))

    def test_a_child_that_reaches_the_machine_is_caught_there(self):
        # it runs where a leaked guard would matter: under its own HOME and a
        # tripwire osascript - never the real ones (review 4 of slice 1)
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "a_reaching_module.py"), "w") as fh:
            fh.write("import os, unittest\nimport ccwho_engine as engine\n\n"
                     "class T(unittest.TestCase):\n    def test_it_asks(self):\n"
                     "        t = engine.terms\n"
                     "        t.app_snapshot = lambda: '  PID UID UCOMM\\n%d %d iTerm2\\n'"
                     " % (os.getpid(), os.getuid())\n"
                     "        t.ITERM2.ask(['-e', 'return 1'])\n")
        with self.assertRaises(AssertionError) as caught:
            self.after("a_reaching_module", path=tmp)
        self.assertIn("tripwire", str(caught.exception))

    def test_after_the_setup_tests(self):
        self.after("test_setup.TestFactGatherersSurviveTheMachine.test_the_iterm_probe_asks_iterm_not_the_process_list",
                   "test_setup.TestFactGatherersSurviveTheMachine.test_a_refused_automation_prompt_is_not_scriptable",
                   "test_setup.TheIterm2CheckTellsBusyFromBroken.test_a_busy_gate_is_not_knowing")

    def test_after_the_panel_tests(self):
        self.after("test_panel.ThePanelWindowIsABackgroundAsk")

    def test_after_the_runner_s_tests(self):
        self.after("test_runner.TestRestoreDir")

    def test_after_the_engine_s_tests(self):
        self.after("test_ccwho.TestParseSessions")

    def test_after_this_module(self):
        self.after("test_terms.TestITerm2KeepsItsGateFiles")


# Run in a child, where every ccwho module is as its file makes it: the live
# modules of this process may carry an attribute an earlier test left behind,
# which would hide the very mistake the scan looks for (review 2 of slice 1).
SCAN = """
import ast, builtins, json, sys, threading, types

# a name that stands for an object: a module alias, a proxy of the live module
# (a class whose __getattr__ is getattr(X, name)), or a local or self. name set
# once from one - each is looked through to the module it starts at. A name
# bound any other way in a scope (a parameter, twice, by a loop) stands for
# something the scan cannot know: patches through it are not checked
SHADOW = object()
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)

def key(node):
    if isinstance(node, ast.Name):
        return node.id
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == "self"):
        return "self." + node.attr
    return None

def expand(node, env, aliases, depth=0):
    names = []
    while depth < 8:
        k = key(node)
        if k in env:
            if env[k] is SHADOW:
                return None
            if isinstance(env[k], tuple):           # ("import", module) in a function
                return env[k][1], names[::-1]
            base = expand(env[k], env, aliases, depth + 1)
            return None if base is None else (base[0], base[1] + names[::-1])
        if not isinstance(node, ast.Attribute):
            break
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name) and node.id in aliases:
        return aliases[node.id], names[::-1]
    return None

def resolve(node, env, aliases):
    got = expand(node, env, aliases)
    if got is None:
        return None, ""
    module, names = got
    if module == "ccwho_ui" and MODE == "no-ui":
        return None, ""
    try:
        obj = __import__(module)
    except ImportError:
        if MODE == "ui":
            raise
        return None, ""                 # ccwho_ui without Textual: not checked here
    for n in names:
        if not hasattr(obj, n):
            return False, n             # a name on the way that is not there
        obj = getattr(obj, n)
    return obj, ""

def own_nodes(body):
    # every node of a scope's body, not of the functions and classes in it
    # (a lambda is read in the scope it is written in)
    stack = list(body)
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, FUNCS + (ast.ClassDef,)):
            stack.extend(ast.iter_child_nodes(n))

def binds(t):
    # the keys a target binds: its names and self. names - not the names
    # inside an attribute it sets (ccwho in ccwho.x = 1 is only read)
    if isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            yield from binds(e)
    elif isinstance(t, ast.Starred):
        yield from binds(t.value)
    elif key(t) is not None:
        yield key(t)

def bound(body, want):
    # key -> the value it is set to once, or SHADOW
    seen = {}
    for n in own_nodes(body):
        found = []
        if isinstance(n, ast.Assign):
            t, v = n.targets[0], n.value
            if len(n.targets) == 1 and key(t) is not None:
                found = [(key(t), v)]
            elif (len(n.targets) == 1 and isinstance(t, (ast.Tuple, ast.List))
                    and isinstance(v, (ast.Tuple, ast.List)) and len(t.elts) == len(v.elts)
                    and not any(isinstance(e, ast.Starred) for e in t.elts + v.elts)):
                # a, b = x, y: each its own
                found = [(k, ve if key(te) is not None else SHADOW)
                         for te, ve in zip(t.elts, v.elts) for k in binds(te)]
            else:
                found = [(k, SHADOW) for t in n.targets for k in binds(t)]
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                name = a.asname or a.name.split(".")[0]
                ours = isinstance(n, ast.Import) and a.name.startswith("ccwho") and (a.asname or "." not in a.name)
                found.append((name, ("import", a.name) if ours else SHADOW))
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)):
            found = [(k, SHADOW) for k in binds(n.target)]
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign, ast.NamedExpr)):
            found = [(k, SHADOW) for k in binds(n.target)]
        elif isinstance(n, ast.withitem) and n.optional_vars is not None:
            found = [(k, SHADOW) for k in binds(n.optional_vars)]
        for k, v in found:
            if want(k):
                seen.setdefault(k, []).append(v)
    return {k: (vs[0] if len(vs) == 1 else SHADOW) for k, vs in seen.items()}

def proxies(tree):
    # module-level names bound to a proxy class of this file
    through = {}
    for c in tree.body:
        if not isinstance(c, ast.ClassDef):
            continue
        for f in c.body:
            if (isinstance(f, FUNCS) and f.name == "__getattr__" and f.body
                    and isinstance(f.body[-1], ast.Return)):
                r = f.body[-1].value
                if (isinstance(r, ast.Call) and isinstance(r.func, ast.Name)
                        and r.func.id == "getattr" and len(r.args) == 2):
                    through[c.name] = r.args[0]
    return {t.id: through[n.value.func.id] for n in tree.body if isinstance(n, ast.Assign)
            for t in n.targets if isinstance(t, ast.Name) and isinstance(n.value, ast.Call)
            and isinstance(n.value.func, ast.Name) and n.value.func.id in through}

def scopes(body, env, env_of, in_class=False):
    # each node read in the scope it is in: a function's locals and
    # parameters, its class's self. names, what the scopes around it bind. A
    # class's self. names are its methods' own: not a nested class's, nor a
    # nested function's that takes a self of its own
    for inner in list(own_nodes(body)):
        env_of[id(inner)] = env
        if isinstance(inner, FUNCS):
            a = inner.args
            names = [p.arg for p in a.posonlyargs + a.args + a.kwonlyargs
                     + [x for x in (a.vararg, a.kwarg) if x]]
            outer = env if in_class or "self" not in names else {
                k: v for k, v in env.items() if not k.startswith("self.")}
            params = {n: SHADOW for n in names if n != "self"}
            scopes(inner.body, {**outer, **params, **bound(inner.body, lambda k: "." not in k)},
                   env_of)
        elif isinstance(inner, ast.ClassDef):
            selfs = {}
            for f in inner.body:
                if isinstance(f, FUNCS):
                    for k, v in bound(f.body, lambda k: k.startswith("self.")).items():
                        selfs[k] = v if k not in selfs else SHADOW
            outer = {k: v for k, v in env.items() if not k.startswith("self.")}
            scopes(inner.body, {**outer, **selfs}, env_of, in_class=True)

def targets(t):
    if isinstance(t, (ast.Tuple, ast.List)):
        for e in t.elts:
            yield from targets(e)
    elif isinstance(t, ast.Starred):
        yield from targets(t.value)
    elif isinstance(t, ast.Attribute):
        yield t

def popped(call):
    # X.__dict__.pop("n", ...): the undo of an attribute put on X
    f = call if isinstance(call, ast.Attribute) else None
    if (f is not None and f.attr == "pop" and isinstance(f.value, ast.Attribute)
            and f.value.attr == "__dict__"):
        return f.value.value
    return None

args = sys.argv[1:]
MODE = "auto"
if args and args[0] in ("--ui", "--no-ui"):
    MODE, args = args[0][2:], args[1:]
checked, missing = 0, []
for path in args:
    with open(path) as fh:
        tree = ast.parse(fh.read())
    aliases = {a.asname or a.name: a.name for n in tree.body if isinstance(n, ast.Import)
               for a in n.names if a.name.startswith("ccwho")}
    top = proxies(tree)
    env_of = {}
    scopes(tree.body, top, env_of)
    for node in ast.walk(tree):
        pairs = []
        if isinstance(node, ast.Call):
            a, fn = node.args, node.func
            if isinstance(fn, ast.Name) and fn.id == "setattr" and len(a) >= 2:
                pairs.append((a[0], a[1]))
            elif len(a) >= 3 and isinstance(a[0], ast.Name) and a[0].id == "setattr":
                pairs.append((a[1], a[2]))             # addCleanup(setattr, X, "n", v)
            elif isinstance(fn, ast.Attribute) and fn.attr == "patch" and len(a) >= 3 and (
                    isinstance(fn.value, ast.Name) and fn.value.id == "testkit"):
                pairs.append((a[1], a[2]))             # testkit.patch(test, X, "n", v)
            elif popped(fn) is not None and a:
                pairs.append((popped(fn), a[0]))       # X.__dict__.pop("n", None)
            elif len(a) >= 2 and popped(a[0]) is not None:
                pairs.append((popped(a[0]), a[1]))     # addCleanup(X.__dict__.pop, "n", None)
        elif isinstance(node, ast.Assign):
            pairs += [(t.value, ast.Constant(t.attr)) for tt in node.targets
                      for t in targets(tt)]             # X.n = fake, (X.n, Y.m) = saved
        for target, name in pairs:
            if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
                continue
            obj, gone = resolve(target, env_of.get(id(node), top), aliases)
            if obj is None or isinstance(obj, threading.local):
                continue                    # a thread's own store: its names are made by use
            checked += 1
            if (obj is not False and isinstance(obj, types.ModuleType)
                    and hasattr(builtins, name.value)):
                continue                    # a module global that shadows a builtin for it
            if obj is False or not hasattr(obj, name.value):
                missing.append(f"{path.rsplit('/', 1)[-1]}:{node.lineno} {gone or name.value}")
print(json.dumps({"checked": checked, "missing": missing}))
"""


class TestTheTestsPatchNamesThatExist(unittest.TestCase):
    """A fake put on a name the code no longer reads does nothing, and a real
    function put back on the wrong object leaves the fake in place (review of
    slice 1: `addCleanup(setattr, ccwho, "fcntl", real)` after the gate moved
    left the gate's fcntl a fake for every later test). Every setattr in the
    tests on a ccwho module - called, or passed to addCleanup - and every
    `X.name = ...` on one names an attribute that is there. Checked in a
    child, against modules as their files make them; ccwho_ui only where
    Textual can be imported (the venv the UI tests use)."""

    def scan(self, paths):
        import json
        import sys
        done = subprocess.run([sys.executable, "-c", SCAN, *paths], cwd=REPO,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                              capture_output=True, text=True, timeout=25)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        return json.loads(done.stdout)

    def test_every_patch_names_an_attribute_that_exists(self):
        import glob
        got = self.scan(sorted(glob.glob(os.path.join(REPO, "test_*.py"))))
        self.assertGreater(got["checked"], 500, "the scan resolved almost nothing")   # control
        self.assertEqual(got["missing"], [])

    def test_the_ui_s_patches_are_checked_where_textual_is(self):
        # ./test runs this class again in its Textual stage: there ccwho_ui must
        # import - an error, not a skip, if it cannot - and the patches on it
        # are checked too (review 3 of slice 1: the stdlib stage skips them)
        import glob
        import importlib.util
        if importlib.util.find_spec("textual") is None:
            self.skipTest("no Textual here: ./test's UI stage runs this")
        paths = sorted(glob.glob(os.path.join(REPO, "test_*.py")))
        got, bare = self.scan(["--ui", *paths]), self.scan(["--no-ui", *paths])
        self.assertEqual(got["missing"], [])
        self.assertGreater(got["checked"], bare["checked"], "no patch on the UI was checked")

    def test_the_ui_stage_of_the_test_command_runs_this(self):
        with open(os.path.join(REPO, "test")) as fh:
            stage = fh.read().split("uv run", 1)[-1]
        self.assertIn("test_terms.TestTheTestsPatchNamesThatExist", stage)

    def test_a_ui_that_will_not_import_is_an_error_where_it_must(self):
        import shutil
        import sys
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "textual.py"), "w") as fh:
            fh.write("raise ImportError('no Textual in this test')\n")
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=tmp)

        def scan(*mode):
            return subprocess.run([sys.executable, "-c", SCAN, *mode, os.path.join(REPO, "test_ui.py")],
                                  cwd=REPO, env=env, capture_output=True, text=True, timeout=25)
        done = scan("--ui")
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn("no Textual in this test", done.stderr)
        self.assertEqual(scan().returncode, 0)                          # control: skipped

    def test_an_attribute_left_on_a_live_module_does_not_hide_one(self):
        import tempfile
        stray = "_stray_from_an_earlier_test"
        setattr(engine, stray, 1)                       # what a wrong cleanup leaves behind
        self.addCleanup(delattr, engine, stray)
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write("import ccwho_engine as ccwho\n"
                     "setattr(ccwho, '_stray_from_an_earlier_test', 1)\n"
                     "ccwho.collect = 1\n")                 # a name that is there: control
        self.addCleanup(os.remove, fh.name)
        got = self.scan([fh.name])
        self.assertEqual(got["checked"], 2)
        self.assertEqual(got["missing"], [os.path.basename(fh.name) + ":2 _stray_from_an_earlier_test"])

    FORMS = """import ccwho_engine as engine
import ccwho_engine as ccwho


class _Live:
    def __getattr__(self, name):
        return getattr(engine.terms, name)


terms = _Live()
(ccwho.collect, ccwho.no_such_name) = (1, 2)
terms.STATE_DIR = 1
terms.STATE_DIRS = 1


def f():
    iterm = ccwho.terms.ITERM2
    iterm.titles = 1
    iterm.titlez = 1


class T:
    def setUp(self):
        self.app = ccwho.terms.ITERM2

    def test(self):
        self.app.ask = 1
        self.app.asks = 1


# review 4 of slice 1: every other form a patch or its undo takes
import testkit
testkit.patch(None, ccwho.terms.ITERM2, "titles", 1)
testkit.patch(None, ccwho.terms.ITERM2, "titlez", 1)
ccwho.terms.ITERM2.__dict__.pop("panes", None)
ccwho.terms.ITERM2.__dict__.pop("panez", None)
T().addCleanup(ccwho.terms.ITERM2.__dict__.pop, "askz", None)
[ccwho.collect, ccwho.nope] = (1, 2)
(ccwho.collect, *ccwho.nope2) = (1, 2)


class U:
    async def test_async(self):
        iterm = ccwho.terms.ITERM2
        iterm.panez2 = 1

    def test_closure(self):
        iterm = ccwho.terms.ITERM2

        def inner():
            iterm.titlez2 = 1
        inner()

    def test_rebound(self):
        app = ccwho.terms.ITERM2
        app = ccwho
        app.nothing_to_check = 1

    def test_shadow(self, ccwho):
        ccwho.anything = 1


def f2():
    iterm = ccwho.terms.ITERM2

    class Inner:
        def test(self):
            iterm.titlez3 = 1
    return Inner


# review 5 of slice 1: read - a tuple unpacked element by element, an import
# inside a function; not read - every other way a name is bound again
def f3():
    calls, it = [], ccwho.terms.ITERM2
    it.ownerz = 1
    T().addCleanup(it.__dict__.pop, "ownerz2", None)


def f4():
    import ccwho_engine as e2
    e2.nope3 = 1


def f5():
    import json as ccwho
    ccwho.anything2 = 1


def shadows():
    # each: bound once to an object the scan can read, and once more by the
    # form - so a scan that missed the form would report the name
    first, *rest = ccwho.terms.ITERM2, 1
    first.no1 = 1
    loop = ccwho.terms.ITERM2
    for loop in (1,):
        pass
    loop.no2 = 1
    w = ccwho.terms.ITERM2
    with open("x") as w:
        w.no4 = 1
    aug = ccwho.terms.ITERM2
    aug += 1
    aug.no5 = 1
    ann = ccwho.terms.ITERM2
    ann: int = 2
    ann.no6 = 1
    walrus = ccwho.terms.ITERM2
    if (walrus := 3):
        walrus.no7 = 1


def loop_over_the_alias():
    for ccwho in (1,):
        ccwho.no3 = 1


def star(*ccwho):
    ccwho.no8 = 1


def stars(**ccwho):
    ccwho.no9 = 1


def keyword(*, ccwho=None):
    ccwho.no10 = 1


class V:
    def setUp(self):
        self.app = ccwho.terms.ITERM2

    def other(self):
        self.app = ccwho

    def test(self):
        self.app.no11 = 1


class W:
    def setUp(self):
        self.app = ccwho.terms.ITERM2

    def test(self):
        class Fake:
            app = object()

            def run(self):
                self.app.no12 = 1

        def helper(self):
            self.app.no13 = 1
        return Fake, helper
"""

    def test_every_form_of_patch_is_checked(self):
        # a tuple target (the usual tearDown restore), a proxy for the live
        # module (this module's _Live), and a local or self. name for an object
        # (review 3 of slice 1): each checked, and each wrong name found
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write(self.FORMS)
        self.addCleanup(os.remove, fh.name)
        got = self.scan([fh.name])
        self.assertEqual(sorted(m.split(" ", 1)[1] for m in got["missing"]),
                         sorted(["STATE_DIRS", "asks", "no_such_name", "titlez",
                                 "titlez", "panez", "askz", "nope", "nope2",
                                 "panez2", "titlez2", "titlez3",
                                 "ownerz", "ownerz2", "nope3"]))
        self.assertEqual(got["checked"], 8 + 5 + 4 + 3 + 3)

if __name__ == "__main__":
    unittest.main()
