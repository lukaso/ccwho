"""What the test modules share.

No test writes to the real ~/.ccwho (pin_ccwho_dir): tests set CCWHO_DIR and
pop it again; a scan in a test that forgot to reached the user's own
~/.ccwho/launching - and swept it (2026-09-30). While CCWHO_DIR is unset,
ccwho_dir() is a temp dir of the test module's own.

The spawn rule is checked on each Python that runs ccwho (FORK_POINT,
SPAWN_RULE_CHECK, SPAWN_RULE_SORTS): it reads CPython's own frames and fields.
"""
import os
import shutil
import subprocess
import tempfile


def pin_ccwho_dir(runner):
    """Point runner.ccwho_dir() at a temp dir whenever CCWHO_DIR is unset. A
    CCWHO_DIR exported by the shell that runs the tests is the user's own data
    dir: it is taken out for the module, and put back after. Returns the
    undo, for tearDownModule."""
    real, tmp = runner.ccwho_dir, tempfile.mkdtemp(prefix="ccwho-test-")
    inherited = os.environ.pop("CCWHO_DIR", None)
    runner.ccwho_dir = lambda: os.environ.get("CCWHO_DIR") or tmp

    def undo():
        runner.ccwho_dir = real
        shutil.rmtree(tmp, True)
        if inherited is not None:
            os.environ["CCWHO_DIR"] = inherited
    return undo


# where Popen makes its process: subprocess._fork_exec from 3.11,
# _posixsubprocess.fork_exec before
FORK_POINT = ((subprocess, "_fork_exec") if hasattr(subprocess, "_fork_exec")
              else (subprocess._posixsubprocess, "fork_exec"))

# ccwho's spawn rule (_being_made, _never_ran, _ended, _popen_in, _interrupted)
# reads CPython's own frames and
# fields: run this with each Python that runs ccwho. A fork that fails and an
# exec that fails ran nothing; an error once the process is made - before
# exec's report, or while its output is read - may have. argv[1]: the repo.
# Only harmless programs are run.
SPAWN_RULE_CHECK = """
import contextlib, errno, os, signal, subprocess, sys
from unittest import mock
sys.path.insert(0, sys.argv[1])
import ccwho as r
import testkit
fd = os.open(os.devnull, os.O_RDONLY)       # a pass_fds, as the send lock is: the fork path


def sort(argv, patch=None):
    try:
        with (mock.patch.object(*patch[0], side_effect=patch[1]) if patch else contextlib.nullcontext()):
            subprocess.run(argv, capture_output=True, pass_fds=(fd,), timeout=30)
    except (OSError, ValueError, TypeError) as ex:
        p = r._being_made(ex)
        if r._never_ran(p) and not r._interrupted(ex):
            return "none-ran"
        if not r._ended(p):                 # made, not heard from: ended - killed - as _send does
            return "not-ended"
        return "may-run" if p is None or p.returncode == -signal.SIGKILL else "not-killed"
    return "ran"


def masked():
    # Ctrl-C just after the real fork, hidden by an error raised as it unwinds
    real, forked = getattr(*testkit.FORK_POINT), []

    def fork(*a, **k):
        forked.append(real(*a, **k))
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt:
            raise OSError(errno.EBADF, "x")
    try:
        return sort(["/bin/sleep", "30"], (testkit.FORK_POINT, fork))
    finally:
        for pid in forked:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def ctrl_c(where):
    # Ctrl-C in subprocess.run once its Popen is made: from communicate
    # (run's own process, found and ended) or its `with` line (none found)
    made = []

    def interrupted(self, *a, **k):
        made.append(self)
        raise KeyboardInterrupt
    try:
        with mock.patch.object(subprocess.Popen, where, interrupted):
            subprocess.run(["/bin/sleep", "30"], capture_output=True, pass_fds=(fd,), timeout=30)
    except KeyboardInterrupt as ex:
        p = r._popen_in(ex)
        if p is None:
            return "not-found"
        return "found-ended" if r._ended(p) and p.returncode == -signal.SIGKILL else "found-not-killed"
    finally:
        for q in made:
            q.kill()
            q.wait()
    return "ran"


print(sort(["/usr/bin/true"], (testkit.FORK_POINT, BlockingIOError(errno.EAGAIN, "no process"))),
      sort(["/nonexistent/ccwho-test/osascript"]),
      sort(["/bin/sleep", "30"], ((subprocess.Popen, "_close_pipe_fds"), OSError(errno.EBADF, "x"))),
      sort(["/usr/bin/true"], ((subprocess.Popen, "communicate"), OSError(errno.EIO, "x"))),
      masked(), ctrl_c("communicate"), ctrl_c("__enter__"))
"""
SPAWN_RULE_SORTS = ["none-ran", "none-ran", "may-run", "may-run", "not-ended", "found-ended", "not-found"]


_ABSENT = object()                 # "not on the object": None is a value like any other
_LIVE = {}                         # (id(obj), name) -> the patches of it still on, oldest first


def patch(test, obj, name, value):
    """obj.name = value until `test` ends, or until the undo it returns is
    called. The undo acts on THIS obj - a restore that looks the object up
    again when it runs lands on whatever the name leads to then, and a hot
    reload swaps ccwho_terms - and puts back exactly what obj itself held: a
    method an app has from its class is removed from the app, never replaced
    by a bound copy (review 3 of the terminal-app slice 1).

    Refused before anything is set (reviews 4, 5): a name obj does not have -
    a typo would put a fake nothing reads, and leave the real one on - and an
    obj whose setattr does not simply store the value on it: one that passes
    it on (a proxy), or a data descriptor of its class (a property, a slot).
    An undo while a later patch of the same name is on changes nothing and
    raises; the cleanups, last first, undo both. An undo after the code under
    test set the name itself, or took it away, puts the original back."""
    import types
    if not hasattr(obj, name):
        raise AttributeError(f"{obj!r} has no attribute {name!r} to patch")
    cls = type(obj)
    plain = cls.__setattr__ in (object.__setattr__, types.ModuleType.__setattr__, type.__setattr__)
    held = next((vars(k)[name] for k in cls.__mro__ if name in vars(k)), None)
    if not plain or hasattr(type(held), "__set__"):
        raise TypeError(f"{name!r} set on {obj!r} would not land on it: patch it where it lives")
    own = vars(obj)
    old = own.get(name, _ABSENT)
    setattr(obj, name, value)
    if vars(obj).get(name, _ABSENT) is not value:
        raise AssertionError(f"{name!r} did not land on {obj!r}")    # refused above; never seen
    token, key = object(), (id(obj), name)
    _LIVE.setdefault(key, []).append(token)

    def undo():
        live = _LIVE.get(key, [])
        if token not in live:
            return                                  # undone already
        if live[-1] is not token:
            raise AssertionError(f"{name!r} on {obj!r} was patched again: undo that first")
        live.pop()
        if not live:
            _LIVE.pop(key, None)
        if old is not _ABSENT:
            setattr(obj, name, old)
        elif name in vars(obj):
            delattr(obj, name)
    test.addCleanup(undo)
    return undo


def compiles(test, app, script):
    """osacompile's result for `script`, which tells `app` (a terms.App) in the
    open. Compiling such a tell can start the app (probe 2026-09-29: TextEdit
    started), so it is compiled only while a copy of ours already runs - the
    test is skipped otherwise: no test starts an app (review of the
    terminal-app slices 2-3). Only the moment between the check and the
    compile is left."""
    import sys
    live = sys.modules[type(app).__module__]
    if not app.pids(live.app_snapshot()):
        test.skipTest(f"{app.label} is not running: compiling a tell to it could start it")
    return subprocess.run(["osacompile", "-o", os.devnull, "-e", script],
                          capture_output=True, text=True, timeout=20)


def fresh_terms(engine):
    """ccwho_terms loaded new from its file - its own functions, nothing a test
    put on it - and made the engine's and sys.modules'. Returns it.

    A hot reload SWAPS that module (the engine is re-read in place, the rule
    modules are not): a guard or a fake put on the old one guards nothing, and
    a function kept from it runs with the old module's STATE_DIR and
    OSASCRIPT - the real ~/.cache/ccwho, a real osascript (review of the
    terminal-app slice 1). So a test module takes the live module fresh when it
    starts and when it ends, and keeps no ccwho_terms object across tests."""
    import sys
    fresh = engine._load_beside(sys.modules["ccwho_terms"])
    sys.modules["ccwho_terms"] = fresh
    engine.terms = fresh
    return fresh
