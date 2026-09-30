"""No test writes to the real ~/.ccwho.

Tests set CCWHO_DIR and pop it again; a scan in a test that forgot to reached
the user's own ~/.ccwho/launching - and swept it (2026-09-30). While CCWHO_DIR
is unset, ccwho_dir() is a temp dir of the test module's own.
"""
import os
import shutil
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
