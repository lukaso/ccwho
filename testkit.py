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
