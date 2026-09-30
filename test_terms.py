"""ccwho_terms: the terminal apps ccwho talks to, one backend each, and the one
gate every background Apple Event goes through.

These tests never reach a real app: every ask is faked at ccwho_terms.ask, or
at the osascript the gate would start.
"""
from __future__ import annotations

import ast
import os
import subprocess
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
        self.run_in_order("test_runner.TestInterval",
                          "test_ccwho.TestBackgroundAsksNeverPileUpInITerm2.test_an_answer_comes_back")

    def test_a_brief_reload_then_a_scan(self):
        self.run_in_order(
            "test_runner.TestHotReloadCoversTheBriefModule.test_a_broken_brief_edit_does_not_kill_the_loop",
            "test_ccwho.TestCollectTakesWindowsFromPsNotFromTabNames"
            ".test_a_scan_runs_ps_once_even_when_iterm2_is_not_asked")

    def test_one_module_alone(self):                                    # control
        self.run_in_order("test_terms.TestITerm2KeepsItsGateFiles")


# Run in a child after a test module has ended: is the terminal module the
# engine uses untouched - no guard, no fake, its own functions?
PRISTINE = """
import sys, unittest
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
ran = unittest.TextTestRunner(stream=sys.stderr).run(suite)
import ccwho_engine as engine
t, bad = engine.terms, []
if vars(t.ITERM2):
    bad.append("ITERM2 keeps " + ",".join(sorted(vars(t.ITERM2))))
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
        self.after("test_runner.TestInterval")

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
