"""Tests for the ccwho runner's argument handling.

A flag that is silently ignored is the failure mode these cover: --watch=3 and -w
both used to fall through to one-shot with no complaint.
"""
import contextlib
import errno
import fcntl
import io
import json
import os
import re
import shutil
import signal
import stat
import sys
import tempfile
import threading
import time
import subprocess
import unittest
from unittest import mock

import ccwho as runner
import testkit

REAL_CCWHO_DIR = runner.ccwho_dir


# Reading this machine's processes, ports and session files is machine state; see
# test_ccwho. Every test here runs with guards that fail loudly instead.
# The terminal apps are in ccwho_terms, which a hot reload SWAPS rather than
# re-reads in place: its guards go on the live module (runner.engine.terms),
# taken fresh from its file each time they go on (testkit.fresh_terms), and
# nothing of it is kept from an import.
TERMS_NAMES = {"panes_snapshot": ("ITERM2", "panes"), "iterm_ask": ("ITERM2", "ask"),
               "app_snapshot": (None, "app_snapshot")}


def _where(name):
    """(object, attribute) a guarded name lives at now."""
    if name in TERMS_NAMES:
        owner, attr = TERMS_NAMES[name]
        live = runner.engine.terms
        return (live if owner is None else getattr(live, owner)), attr
    return runner.engine, name


REAL = {name: getattr(runner.engine, name) for name in ("live_file_sessions",
                                                       "ps_table", "listen_ports",
                                                       "idle_snapshot")}
REAL_LIVE_FILE_SESSIONS = REAL["live_file_sessions"]


def _guard(name):
    def unpinned(*a, **k):
        raise AssertionError(f"a test reached this machine through engine.{name}")
    return unpinned


GUARDS = {name: _guard(name) for name in list(REAL) + list(TERMS_NAMES)}
# iTerm2's panes: every save and restore asks, and "iTerm2 could not be asked"
# is an answer they handle - so the stand-in is that answer, not a failure
GUARDS["panes_snapshot"] = lambda *a, **k: {}
GUARDS["idle_snapshot"] = lambda *a, **k: set()
GUARDS["iterm_ask"] = lambda *a, **k: None       # the same answer, one level down
GUARDS["app_snapshot"] = lambda: "  PID UID UCOMM\n"   # no iTerm2 of ours, unless a test says so
_unpinned_live_file_sessions = GUARDS["live_file_sessions"]
TERMS_GATE_GUARD = lambda *a, **k: None       # noqa: E731 - the gate every app's asks go through


def install_guards():
    testkit.fresh_terms(runner.engine)
    for name, guard in GUARDS.items():
        setattr(*_where(name), guard)
    runner.engine.terms.ask = TERMS_GATE_GUARD


_UNPIN = []
REAL_RUN, REAL_TIME, REAL_KILL = subprocess.run, time.time, os.kill
# ccwho's names as imported: a test that replaced one - a function, a clock,
# a constant - and did not put it back is named at the end (tearDownModule)
AT_IMPORT = dict(vars(runner))
MOVES_ITSELF = {"_SHOWN_LAST", "index", "usage"}     # ccwho rebinds these (global, reload_engine)


def setUpModule():
    install_guards()
    _UNPIN.append(testkit.pin_ccwho_dir(runner))
    _fresh_claim_state()


def _fresh_claim_state():
    """Every test here starts with no claim state an earlier one left in
    ccwho's thread-local store (_LOCAL: why a claim could not be recorded,
    the tokens of claims taken, refusals, a give-up mark) - the next
    launch_in_iterm on the thread reads it. Wraps each test class's setUp."""
    for cls in list(globals().values()):
        if isinstance(cls, type) and issubclass(cls, unittest.TestCase) and cls.__module__ == __name__:
            def setUp(self, _real=cls.setUp):
                runner._LOCAL.__dict__.clear()
                self.addCleanup(runner._LOCAL.__dict__.clear)
                _real(self)
            cls.setUp = setUp


def spawn_failing(ex):
    """A subprocess.run for osascript whose process is never made: the real
    run - of a harmless program, never osascript - its fork failing with
    `ex`, raised where Popen makes the process."""
    def run(cmd, **kw):
        with mock.patch.object(*testkit.FORK_POINT, side_effect=ex):
            return REAL_RUN(["/usr/bin/true"], **kw)
    return run


def exec_failing():
    """A subprocess.run for osascript whose exec fails: the real run of a
    program that is not there."""
    return lambda cmd, **kw: REAL_RUN(["/nonexistent/ccwho-test/osascript"], **kw)


def failing_after_start(test, ex, where="communicate", argv=("/usr/bin/true",)):
    """A subprocess.run for osascript whose process - `argv`, a harmless
    program, never osascript - is made, and then `ex` is raised: while its
    output is read (communicate), or while Popen still waits for exec's
    report (_close_pipe_fds). Each child's Popen goes to test.children; one
    left running is ended when the test ends."""
    test.children = getattr(test, "children", [])

    def failing(self, *a, **k):
        test.children.append(self)
        test.addCleanup(lambda p=self: (p.kill(), p.wait()))      # a no-op once reaped
        raise ex

    def run(cmd, **kw):
        with mock.patch.object(subprocess.Popen, where, failing):
            return REAL_RUN(list(argv), **kw)
    return run


def tearDownModule():
    for name, real in REAL.items():
        setattr(runner.engine, name, real)
    testkit.fresh_terms(runner.engine)          # no guard or fake of ours stays on it
    while _UNPIN:
        _UNPIN.pop()()
    # a test that replaced these and did not put them back breaks the next
    # module's tests, far from the cause: say so here
    leaked = [n for n, now, real in (("subprocess.run", subprocess.run, REAL_RUN),
                                     ("time.time", time.time, REAL_TIME),
                                     ("os.kill", os.kill, REAL_KILL)) if now is not real]
    subprocess.run, time.time, os.kill = REAL_RUN, REAL_TIME, REAL_KILL
    for name, real in AT_IMPORT.items():
        if name not in MOVES_ITSELF and vars(runner).get(name) is not real:
            leaked.append(f"ccwho.{name}")
            setattr(runner, name, real)
    assert not leaked, f"left replaced by a test: {leaked}"


class TestASearchFindsTheJobForItsParkedTerminal(unittest.TestCase):
    """The parked terminal (ctrl+b) has no row, but its transcript is the long
    conversation a search finds. Running, it is the job's row - not "ended"."""

    JOB = "4e3efc1d-3639-4af3-91e9-6d6373c1cf94"
    PARKED = "fc509261-e383-4ed4-aacc-44087dc5a599"

    def setUp(self):
        real = runner.fresh_index
        self.addCleanup(setattr, runner, "fresh_index", real)
        runner.fresh_index = lambda quiet=False: {self.PARKED: {
            "sessionId": self.PARKED, "title": "copy paste in the tui",
            "project": "ccwho", "entrypoint": "cli"}}

    def test_it_is_the_running_job(self):
        job = {"sessionId": self.JOB, "project": "ccwho", "title": "x",
               "parked": [self.PARKED], "attention": "stopped"}
        live, ended = runner.matches("paste", [job])
        self.assertEqual(([r["sessionId"] for r in live], ended), ([self.JOB], []))

    def test_with_its_job_gone_it_ended(self):                       # control
        live, ended = runner.matches("paste", [])
        self.assertEqual((live, [e["sessionId"] for e in ended]), ([], [self.PARKED]))


class TestWatchRequested(unittest.TestCase):
    def test_long_flag(self):
        self.assertTrue(runner.watch_requested(["--watch"]))

    def test_short_flag(self):
        self.assertTrue(runner.watch_requested(["-w"]))

    def test_equals_form(self):
        self.assertTrue(runner.watch_requested(["--watch=3"]))

    def test_absent(self):
        self.assertFalse(runner.watch_requested(["--blocked"]))


class TestInterval(unittest.TestCase):
    def test_default_when_bare(self):
        self.assertEqual(runner.parse_interval(["--watch"], default=5.0), 5.0)

    def test_space_form(self):
        self.assertEqual(runner.parse_interval(["--watch", "3"]), 3.0)

    def test_equals_form(self):
        self.assertEqual(runner.parse_interval(["--watch=3"]), 3.0)

    def test_short_flag_with_value(self):
        self.assertEqual(runner.parse_interval(["-w", "2"]), 2.0)

    def test_trailing_s_is_tolerated(self):
        self.assertEqual(runner.parse_interval(["--watch", "10s"]), 10.0)

    def test_next_flag_is_not_an_interval(self):
        self.assertEqual(runner.parse_interval(["--watch", "--blocked"], default=5.0), 5.0)

    def test_floor_of_one_second(self):
        self.assertEqual(runner.parse_interval(["--watch", "0.1"]), 1.0)

    def test_garbage_falls_back_to_default(self):
        self.assertEqual(runner.parse_interval(["--watch", "abc"], default=5.0), 5.0)


class TestUnknownFlags(unittest.TestCase):
    def test_accepts_every_known_flag(self):
        known = ["--watch", "3", "--blocked", "--prompt", "--json", "--no-color", "-p", "-w"]
        self.assertEqual(runner.unknown_flags(known), [])

    def test_reports_a_typo(self):
        self.assertEqual(runner.unknown_flags(["--wathc"]), ["--wathc"])

    def test_reports_several(self):
        self.assertEqual(runner.unknown_flags(["--nope", "--zzz"]), ["--nope", "--zzz"])

    def test_equals_form_is_known(self):
        self.assertEqual(runner.unknown_flags(["--watch=3"]), [])

    def test_bare_values_are_not_flags(self):
        self.assertEqual(runner.unknown_flags(["--watch", "3"]), [])

    def test_main_refuses_an_unknown_flag(self):
        self.assertEqual(runner.main(["--wathc"]), 2)


if __name__ == "__main__":
    unittest.main()


class TestPositional(unittest.TestCase):
    """A flag's VALUE is not a positional argument. `reap --older-than 1h` took
    "1h" as the pattern and matched an unrelated system process."""

    VALUE_FLAGS = ("--older-than",)

    def test_flag_value_is_not_the_positional(self):
        got = runner.positional(["--older-than", "1h"], self.VALUE_FLAGS, "default")
        self.assertEqual(got, "default")

    def test_real_positional_after_a_flag_and_value(self):
        got = runner.positional(["--older-than", "1h", "mypattern"], self.VALUE_FLAGS, "d")
        self.assertEqual(got, "mypattern")

    def test_positional_before_the_flag(self):
        got = runner.positional(["mypattern", "--older-than", "1h"], self.VALUE_FLAGS, "d")
        self.assertEqual(got, "mypattern")

    def test_bare_flags_are_skipped(self):
        self.assertEqual(runner.positional(["--kill"], self.VALUE_FLAGS, "d"), "d")

    def test_kill_flag_does_not_swallow_the_pattern(self):
        self.assertEqual(runner.positional(["--kill", "pat"], self.VALUE_FLAGS, "d"), "pat")

    def test_empty(self):
        self.assertEqual(runner.positional([], self.VALUE_FLAGS, "d"), "d")


class TestParseAge(unittest.TestCase):
    def test_units(self):
        self.assertEqual(runner.parse_age("90m"), 5400)
        self.assertEqual(runner.parse_age("2h"), 7200)
        self.assertEqual(runner.parse_age("3d"), 259200)
        self.assertEqual(runner.parse_age("45s"), 45)

    def test_bare_number_is_seconds(self):
        self.assertEqual(runner.parse_age("600"), 600)

    def test_garbage_uses_the_default(self):
        self.assertEqual(runner.parse_age("abc", default=99), 99)

    def test_empty_uses_the_default(self):
        self.assertEqual(runner.parse_age("", default=99), 99)


class TestRestoreDir(unittest.TestCase):
    def test_defaults_under_home_because_it_must_outlive_a_reboot(self):
        old = os.environ.pop("CCWHO_DIR", None)
        pinned, runner.ccwho_dir = runner.ccwho_dir, REAL_CCWHO_DIR   # a path only: nothing written
        try:
            d = runner.restore_dir()
            self.assertTrue(d.startswith(os.path.expanduser("~")), d)
            self.assertNotIn("/tmp", d, "a temp dir does not survive the reboot it exists for")
        finally:
            runner.ccwho_dir = pinned
            if old is not None:
                os.environ["CCWHO_DIR"] = old

    def test_env_override(self):
        os.environ["CCWHO_DIR"] = "/somewhere/else"
        try:
            self.assertTrue(runner.restore_dir().startswith("/somewhere/else"))
        finally:
            os.environ.pop("CCWHO_DIR", None)


class TestSaveAndRestore(unittest.TestCase):
    ROWS = [{"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef", "cwd": "/Users/x/p/liveapp",
             "project": "liveapp", "topic": "the reaper", "ask": "Land it?", "attention": "asks",
             "tty": "ttys032", "since": "2h", "status": "waiting", "first": "", "pid": 1},
            {"sessionId": "", "cwd": "/Users/x/p/nope", "project": "nope", "topic": "t",
             "ask": "", "attention": "stopped", "tty": "", "since": "1h", "status": "waiting",
             "first": "", "pid": 2}]

    def setUp(self):
        # about the manifest FILE, not the disk: TestSaveSkipsWhatCannotResume
        # covers which sessions are worth saving
        self.real_save_problem = runner.save_problem
        runner.save_problem = lambda row: ""
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        self.real_collect = runner.engine.collect
        def fake_collect(cache=None, status=None):
            if status is not None:
                status["source_ok"] = True      # this class tests a REACHABLE source
            return (list(self.ROWS), 0)

        runner.engine.collect = fake_collect

    def tearDown(self):
        runner.save_problem = self.real_save_problem
        runner.engine.collect = self.real_collect
        os.environ.pop("CCWHO_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _save(self, argv=()):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = runner.save(list(argv))
        return rc, buf.getvalue()

    def _restore(self, argv=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.restore(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def test_save_writes_a_manifest_that_restore_reads_back(self):
        rc, _ = self._save()
        self.assertEqual(rc, 0)
        rc, out = self._restore()
        self.assertEqual(rc, 0)
        self.assertIn("claude --resume 4f2b91ac-1111-4222-8333-abcdefabcdef", out)

    def test_save_records_the_pane_each_session_is_in(self):
        iterm = runner.engine.terms.ITERM2
        real = iterm.panes
        iterm.panes = lambda **k: {"ttys032": {"pane": "G-1", "name": "t"}}
        try:
            self._save()
        finally:
            iterm.panes = real
        path = os.path.join(runner.restore_dir(), os.listdir(runner.restore_dir())[0])
        with open(path) as fh:
            self.assertEqual(json.load(fh)["sessions"][0]["pane"], "G-1")

    def test_save_prunes_usage_readings_older_than_eight_days(self):
        d = os.path.join(self.tmp, "usage")
        os.makedirs(d)
        old, new = os.path.join(d, "old.json"), os.path.join(d, "new.json")
        for p in (old, new):
            with open(p, "w") as fh:
                fh.write("{}")
        stamp = time.time() - 9 * 86400
        os.utime(old, (stamp, stamp))
        self._save()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))                                # control

    def test_save_records_the_session_it_could_not_capture(self):
        self._save()
        path = os.path.join(runner.restore_dir(),
                            os.listdir(runner.restore_dir())[0])
        with open(path) as fh:
            man = json.load(fh)
        self.assertEqual(man["count"], 1)
        self.assertEqual(man["skipped"], 1)

    def test_save_leaves_no_partial_file_behind(self):
        self._save()
        self.assertEqual([n for n in os.listdir(runner.restore_dir()) if n.endswith(".tmp")], [])

    def test_a_write_that_dies_midway_leaves_no_half_manifest(self):
        """The vacuous version of this checked only that no .tmp survives - which a
        direct, non-atomic write also satisfies. The assertion that bites is that the
        TARGET is never a partial file, which only os.replace can give you."""
        d = runner.restore_dir()
        os.makedirs(d, exist_ok=True)
        target = os.path.join(d, "2026-01-01T0000.json")
        real_dump = runner.json.dump

        def dies(obj, fh, **kw):
            fh.write('{"version": 1, "sessi')      # a plausible partial write
            raise OSError("disk full")

        runner.json.dump = dies
        try:
            rc, _ = self._save(["--out", target])
        finally:
            runner.json.dump = real_dump
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(target),
                         "a partially written manifest is worse than none after a reboot")

    def test_a_failed_write_does_not_destroy_the_manifest_already_there(self):
        d = runner.restore_dir()
        os.makedirs(d, exist_ok=True)
        target = os.path.join(d, "2026-01-01T0000.json")
        self._save(["--out", target])
        good = open(target).read()
        real_dump = runner.json.dump

        def dies(obj, fh, **kw):
            fh.write("{ruined")
            raise OSError("disk full")

        runner.json.dump = dies
        try:
            self._save(["--out", target])
        finally:
            runner.json.dump = real_dump
        self.assertEqual(open(target).read(), good, "the last good manifest must survive")

    def test_restore_with_nothing_saved_says_what_to_do_and_fails(self):
        rc, out = self._restore()
        self.assertEqual(rc, 1)
        self.assertIn("ccwho save", out)

    def test_restore_from_an_explicit_path(self):
        self._save()
        d = runner.restore_dir()
        path = os.path.join(d, os.listdir(d)[0])
        rc, out = self._restore(["--from", path])
        self.assertEqual(rc, 0)
        self.assertIn("liveapp", out)

    def test_restore_from_a_corrupt_manifest_fails_loudly_not_with_a_traceback(self):
        d = runner.restore_dir()
        os.makedirs(d, exist_ok=True)
        bad = os.path.join(d, "2026-01-01T0000.json")
        with open(bad, "w") as fh:
            fh.write("{not json")
        rc, out = self._restore()
        self.assertEqual(rc, 1)
        self.assertIn("cannot read", out)

    def test_restore_picks_the_newest_manifest(self):
        d = runner.restore_dir()
        os.makedirs(d, exist_ok=True)
        for name, proj in (("2026-01-01T0000.json", "old"), ("2026-09-09T2359.json", "new")):
            with open(os.path.join(d, name), "w") as fh:
                json.dump({"version": 1, "savedAt": 1, "count": 1, "skipped": 0, "sessions": [
                    {"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef",
                     "cwd": "/p", "project": proj, "topic": "t", "ask": ""}]}, fh)
        rc, out = self._restore()
        self.assertIn("new", out)
        self.assertNotIn("old", out)

    # the list's `o` shows reopen_saved's answer: an error by its type, never its
    # text - it can hold a path (the rule of 2026-09-29)
    SECRET = "/Users/x/secret-project/.env"

    def test_a_reopen_that_cannot_drive_iterm2_says_the_type_only(self):
        said = self.reopened_with(spawn_failing(FileNotFoundError(errno.ENOENT, "No such file",
                                                                  self.SECRET)))
        self.assertTrue(said.startswith("could not reopen"), said)
        self.assertIn("could not drive iTerm2 (FileNotFoundError)", said)
        self.assertNotIn(runner.MAY_STILL_RUN, said)
        self.assertNotIn(self.SECRET, said)

    def test_a_reopen_that_may_have_run_says_the_type_only(self):
        said = self.reopened_with(failing_after_start(self, OSError(errno.EIO, self.SECRET)))
        self.assertFalse(said.startswith("could not reopen"), said)
        self.assertIn("could not drive iTerm2 (OSError)", said)
        self.assertIn(runner.MAY_STILL_RUN, said)
        self.assertNotIn(self.SECRET, said)

    def reopened_with(self, fake):
        self._save()
        d = runner.restore_dir()
        path = os.path.join(d, os.listdir(d)[0])

        def no_rows(cache=None, status=None):
            if status is not None:
                status["source_ok"] = True
            return ([], 0)
        runner.engine.collect = no_rows                 # nothing live: it would reopen
        real_rp, real_run = runner.resume_problem, runner.subprocess.run
        runner.resume_problem = lambda row: ""

        def run(argv, *a, **k):
            if argv and argv[0] == "osascript":
                return fake(argv, *a, **k)
            return real_run(argv, *a, **k)
        runner.subprocess.run = run
        try:
            return runner.reopen_saved(path)
        finally:
            runner.subprocess.run, runner.resume_problem = real_run, real_rp

    def test_a_reopen_of_a_bad_manifest_says_the_type_only(self):
        d = runner.restore_dir()
        os.makedirs(d, exist_ok=True)
        bad = os.path.join(d, "2026-01-01T0000.json")
        with open(bad, "w") as fh:
            fh.write("{not json")
        said = runner.reopen_saved(bad)
        self.assertIn("(JSONDecodeError)", said)
        self.assertNotIn("Expecting", said)                 # json's own text
        said = runner.reopen_saved(os.path.join(d, "gone.json"))
        self.assertIn("(FileNotFoundError)", said)
        self.assertNotIn("Errno", said)

    def test_restore_does_not_open_windows_unless_asked(self):
        self._save()
        calls = []
        real = runner.subprocess.run
        runner.subprocess.run = lambda *a, **k: calls.append(a) or real(["true"])
        try:
            self._restore()
            self.assertEqual(calls, [], "printing must not launch anything")
        finally:
            runner.subprocess.run = real


class TestAutosave(unittest.TestCase):
    """`ccwho save` only helps if you remember it. The reboot this exists for is
    often the one you did not plan, so a running watch keeps the manifest fresh."""

    def test_saves_on_the_first_tick_so_a_watch_is_immediately_useful(self):
        self.assertTrue(runner.should_autosave(None, now=1000, interval=300))

    def test_does_not_save_again_until_the_interval_has_passed(self):
        self.assertFalse(runner.should_autosave(1000, now=1299, interval=300))

    def test_saves_once_the_interval_has_passed(self):
        self.assertTrue(runner.should_autosave(1000, now=1300, interval=300))

    def test_an_interval_of_zero_turns_it_off(self):
        self.assertFalse(runner.should_autosave(None, now=1000, interval=0))
        self.assertFalse(runner.should_autosave(1, now=99999, interval=0))

    def test_a_clock_that_went_backwards_does_not_wedge_it_forever(self):
        # ntp step, or a laptop waking with a stale monotonic-ish value
        self.assertTrue(runner.should_autosave(9999, now=1000, interval=300))


class TestPruneManifests(unittest.TestCase):
    """A tool built because the disk filled up must not fill the disk."""

    def names(self, n):
        return ["2026-08-%02dT0900.json" % (i + 1) for i in range(n)]

    def test_keeps_the_newest_n(self):
        doomed = runner.prune_manifests(self.names(25), keep=20)
        self.assertEqual(len(doomed), 5)
        self.assertEqual(sorted(doomed), sorted(self.names(25))[:5])

    def test_nothing_to_do_under_the_limit(self):
        self.assertEqual(runner.prune_manifests(self.names(3), keep=20), [])

    def test_ignores_files_that_are_not_manifests(self):
        doomed = runner.prune_manifests(self.names(25) + ["notes.md", "keep.txt"], keep=20)
        self.assertNotIn("notes.md", doomed)
        self.assertNotIn("keep.txt", doomed)

    def test_a_nonpositive_keep_deletes_nothing(self):
        """keep=0 alone is a VACUOUS check: `found[:-0]` is `found[:0]` == [], so it
        passes with the guard deleted. keep=-1 is where the guard earns its place -
        `found[:-(-1)]` is `found[:1]`, which deletes. Found by mutation."""
        self.assertEqual(runner.prune_manifests(self.names(25), keep=0), [])
        self.assertEqual(runner.prune_manifests(self.names(25), keep=-1), [])
        self.assertEqual(runner.prune_manifests(self.names(25), keep=-5), [])


class TestSaveSkipsWhatCannotResume(unittest.TestCase):
    """A save at 04:25 held 21 sessions; 8 of them could never resume - five
    liveapp test runs in a $TMPDIR that was deleted before the reboot, three
    headless workers that never wrote a transcript. The restore opened a window
    onto an error for each. None of them was part of the fleet to begin with."""

    GOOD = "11111111-1111-4111-8111-111111111111"
    TEMP = "22222222-2222-4222-8222-222222222222"
    GONE = "33333333-3333-4333-8333-333333333333"
    HEADLESS = "44444444-4444-4444-8444-444444444444"

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        os.environ["CCWHO_DIR"] = os.path.join(self.tmp, "ccwho")
        self.temp_root = os.path.join(self.tmp, "T")
        for d in ("p/good", "T/vr-x/liveapp-d1-y/app"):
            os.makedirs(os.path.join(self.tmp, d))
        self.real_temp_roots = runner.temp_roots
        runner.temp_roots = lambda: [self.temp_root]
        self.on_disk = {self.GOOD, self.TEMP, self.GONE}
        self.real_transcript_path = runner.engine.transcript_path
        runner.engine.transcript_path = (
            lambda sid, roots=None: "/tx/%s.jsonl" % sid if sid in self.on_disk else None)
        self.rows = [self.row(self.GOOD, "p/good", "good"),
                     self.row(self.TEMP, "T/vr-x/liveapp-d1-y/app", "app"),
                     self.row(self.GONE, "p/deleted", "gone"),
                     self.row(self.HEADLESS, "p/good", "headless")]
        self.real_collect = runner.engine.collect

        def fake_collect(cache=None, status=None):
            if status is not None:
                status["source_ok"] = True
            return (list(self.rows), 0)

        runner.engine.collect = fake_collect

    def tearDown(self):
        runner.engine.collect = self.real_collect
        runner.engine.transcript_path = self.real_transcript_path
        runner.temp_roots = self.real_temp_roots
        os.environ.pop("CCWHO_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def row(self, sid, rel, project):
        return {"sessionId": sid, "cwd": os.path.join(self.tmp, rel), "project": project,
                "topic": "t", "first": "f", "ask": "", "attention": "stopped",
                "tty": "", "since": "1h", "status": "idle", "pid": 1, "configDir": ""}

    def _save(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.save([])
        return rc, out.getvalue() + err.getvalue()

    def _saved(self):
        d = runner.restore_dir()
        names = sorted(n for n in os.listdir(d) if n.endswith(".json")) if os.path.isdir(d) else []
        if not names:
            return None
        with open(os.path.join(d, names[-1])) as fh:
            return json.load(fh)

    def test_only_what_would_resume_is_saved_and_the_rest_is_named(self):
        rc, out = self._save()
        self.assertEqual(rc, 0, out)
        man = self._saved()
        self.assertEqual([e["sessionId"] for e in man["sessions"]], [self.GOOD])
        self.assertEqual(man["skipped"], 3)
        self.assertEqual(man["skippedWhy"].get("cwd is in a temp dir"), 1)
        self.assertEqual(sum(man["skippedWhy"].values()), 3)
        self.assertIn("temp dir", out)
        self.assertIn("transcript is gone", out)

    def test_a_fleet_that_would_all_resume_is_all_saved(self):          # control
        self.rows = [self.row(self.GOOD, "p/good", "good"),
                     self.row(self.GONE, "p/good", "also good")]
        rc, out = self._save()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self._saved()["count"], 2)
        self.assertEqual(self._saved()["skipped"], 0)

    def test_a_temp_dir_reached_through_a_symlink_is_still_a_temp_dir(self):
        # /tmp is a symlink to /private/tmp: the saved cwd may name either
        os.symlink(self.temp_root, os.path.join(self.tmp, "tmplink"))
        self.rows = [self.row(self.GOOD, "p/good", "good"),
                     self.row(self.TEMP, "tmplink/vr-x/liveapp-d1-y/app", "app")]
        self._save()
        self.assertEqual([e["sessionId"] for e in self._saved()["sessions"]], [self.GOOD])

    def test_a_fleet_of_only_test_runs_writes_nothing(self):
        # an all-skipped manifest would still push a real one out of the keep-20
        self.rows = [self.row(self.TEMP, "T/vr-x/liveapp-d1-y/app", "app")]
        rc, out = self._save()
        self.assertEqual(rc, 0, out)
        self.assertIsNone(self._saved())


class TestRestoreCheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp

    def tearDown(self):
        os.environ.pop("CCWHO_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, sessions):
        d = runner.restore_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "2026-01-01T0000.json")
        with open(path, "w") as fh:
            json.dump({"version": 1, "savedAt": 1, "count": len(sessions),
                       "skipped": 0, "sessions": sessions}, fh)
        return path

    def run_check(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.restore(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def test_a_manifest_pointing_at_a_live_session_passes(self):
        # this very test's own cwd exists, and we let the transcript check see it
        self.write([{"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef",
                     "cwd": self.tmp, "project": "self", "first": "f", "topic": "t", "ask": ""}])
        real = runner.engine.transcript_path
        runner.engine.transcript_path = lambda sid, roots=None: "/tx"
        try:
            rc, out = self.run_check(["--check"])
        finally:
            runner.engine.transcript_path = real
        self.assertEqual(rc, 0, out)
        self.assertIn("1", out)

    def test_a_gone_cwd_fails_the_check_and_is_named(self):
        self.write([{"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef",
                     "cwd": "/definitely/not/here", "project": "reaped",
                     "first": "f", "topic": "t", "ask": ""}])
        rc, out = self.run_check(["--check"])
        self.assertEqual(rc, 1)
        self.assertIn("reaped", out)
        self.assertIn("/definitely/not/here", out)

    def test_check_opens_nothing(self):
        self.write([{"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef",
                     "cwd": self.tmp, "project": "self", "first": "f", "topic": "t", "ask": ""}])
        calls = []
        real = runner.subprocess.run
        runner.subprocess.run = lambda *a, **k: calls.append(a)
        try:
            self.run_check(["--check", "--open"])
        finally:
            runner.subprocess.run = real
        self.assertEqual(calls, [], "--check must never launch anything, even with --open")


class TestSaveRefusesAnUnsourcedManifest(unittest.TestCase):
    """A save that cannot ASK must not answer.

    This is not academic once the save is on a 15-minute timer: `claude` off PATH
    (a launchd job's minimal environment does exactly this) makes every tick write
    a 0-session manifest, and retention keeps only the newest 20. Twenty ticks is
    five hours to evict every manifest that had anything in it. The tool would
    delete precisely the record it exists to keep.
    """

    ROWS = [{"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef", "cwd": "/Users/x/p/liveapp",
             "project": "liveapp", "topic": "t", "ask": "", "attention": "asks",
             "tty": "s032", "since": "2h", "status": "waiting", "first": "", "pid": 1}]

    def setUp(self):
        # about the manifest FILE, not the disk: TestSaveSkipsWhatCannotResume
        # covers which sessions are worth saving
        self.real_save_problem = runner.save_problem
        runner.save_problem = lambda row: ""
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        self.real_collect = runner.engine.collect
        self.source_ok = True
        self.rows = list(self.ROWS)

        def fake_collect(cache=None, status=None):
            if status is not None:
                status["source_ok"] = self.source_ok
            return (list(self.rows), 0)

        runner.engine.collect = fake_collect

    def tearDown(self):
        runner.save_problem = self.real_save_problem
        runner.engine.collect = self.real_collect
        os.environ.pop("CCWHO_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _save(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.save([])
        return rc, out.getvalue() + err.getvalue()

    def _manifests(self):
        d = runner.restore_dir()
        return sorted(n for n in os.listdir(d)) if os.path.isdir(d) else []

    def test_an_unreachable_source_fails_loudly_and_writes_nothing(self):
        self.source_ok = False
        rc, out = self._save()
        self.assertEqual(rc, 1)
        self.assertEqual(self._manifests(), [])
        self.assertIn("claude", out.lower())

    def test_an_unreachable_source_never_evicts_a_good_manifest(self):
        # 20 good manifests, then 20 blind ticks: every one must survive.
        d = runner.restore_dir()
        os.makedirs(d, exist_ok=True)
        good = ["2026-08-01T00%02d.json" % i for i in range(1, 21)]
        for n in good:
            with open(os.path.join(d, n), "w") as fh:
                fh.write("{}")
        self.source_ok = False
        for _ in range(20):
            self.assertEqual(self._save()[0], 1)
        self.assertEqual(self._manifests(), good)

    def test_a_working_source_with_nothing_open_writes_nothing_and_succeeds(self):
        self.rows = []
        rc, _ = self._save()
        self.assertEqual(rc, 0)
        self.assertEqual(self._manifests(), [], "an empty manifest has no restore value")

    def test_a_working_source_with_sessions_still_saves(self):
        rc, _ = self._save()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self._manifests()), 1)


class TestUrlDispatch(unittest.TestCase):
    """One registered handler, every verb decided here.

    The URL arrives from LaunchServices, so anything on the machine can hand us
    one. Nothing unrecognised may reach a shell.
    """

    def setUp(self):
        self.calls = []
        self.real_jump, self.real_open = runner.jump, runner.open_session
        runner.jump = lambda argv: self.calls.append(("jump", list(argv))) or 0
        runner.open_session = lambda argv: self.calls.append(("open", list(argv))) or 0

    def tearDown(self):
        runner.jump, runner.open_session = self.real_jump, self.real_open

    def _url(self, u):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = runner.main(["url", u])
        return rc, err.getvalue()

    def test_an_open_url_reaches_open_session(self):
        sid = "4f2b91ac-1111-4222-8333-abcdefabcdef"
        rc, _ = self._url("ccwho://open/" + sid)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, [("open", [sid])])

    def test_a_jump_url_still_reaches_jump(self):
        rc, _ = self._url("ccwho://jump/s032")
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls, [("jump", ["s032"])])

    def test_an_unrecognised_url_runs_nothing(self):
        for bad in ("https://evil.example/x", "ccwho://delete/all",
                    "ccwho://open/$(whoami)", "ccwho://jump/; rm -rf /", "nonsense"):
            self.calls = []
            rc, err = self._url(bad)
            self.assertEqual(rc, 1, bad)
            self.assertEqual(self.calls, [], "unrecognised URL must reach no verb: " + bad)


class TestOpenSession(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    ENTRY = {"sessionId": SID, "cwd": "/Users/x/p/liveapp", "project": "liveapp"}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        d = os.path.join(self.tmp, "restore")
        os.makedirs(d)
        self.cwd = os.path.join(self.tmp, "liveapp")
        os.makedirs(self.cwd)
        self.write_entry(dict(self.ENTRY, cwd=self.cwd))
        self.on_disk = True
        self.real_transcript_path = runner.engine.transcript_path
        runner.engine.transcript_path = (
            lambda sid, roots=None: "/tx/%s.jsonl" % sid if self.on_disk else None)
        self.live = []
        self.real_collect = runner.engine.collect
        # a reachable, parseable source: these cases are about WHAT is running,
        # not about whether we could find out
        def fake_collect(cache=None, status=None):
            if status is not None:
                status["source_ok"] = True
            return (list(self.live), 0)

        runner.engine.collect = fake_collect
        self.runs = []
        self.real_run = runner.subprocess.run

        class Done:
            returncode, stdout, stderr = 0, "focused s032", ""

        runner.subprocess.run = lambda *a, **k: self.runs.append(a[0]) or Done()

    def write_entry(self, entry):
        with open(os.path.join(self.tmp, "restore", "2026-08-01T0001.json"), "w") as fh:
            json.dump({"version": 1, "savedAt": 1788213090, "count": 1,
                       "skipped": 0, "sessions": [entry]}, fh)

    def tearDown(self):
        runner.engine.collect = self.real_collect
        runner.engine.transcript_path = self.real_transcript_path
        runner.subprocess.run = self.real_run
        os.environ.pop("CCWHO_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open(self, sid):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.open_session([sid])
        return rc, out.getvalue() + err.getvalue()

    def test_a_dead_session_is_reopened_in_a_new_window(self):
        rc, _ = self._open(self.SID)
        self.assertEqual(rc, 0)
        script = " ".join(" ".join(c) for c in self.runs)
        self.assertIn("claude --resume " + self.SID, script)
        self.assertIn("create window", script)

    def test_a_live_session_is_focused_not_reopened(self):
        self.live = [{"sessionId": self.SID, "tty": "ttys032", "pid": 7}]
        rc, _ = self._open(self.SID)
        self.assertEqual(rc, 0)
        joined = " ".join(" ".join(c) for c in self.runs)
        self.assertIn("jump.applescript", joined)
        self.assertNotIn("claude --resume", joined,
                         "reopening a live session would fork the conversation")

    def test_an_unknown_session_says_so_and_runs_nothing(self):
        rc, out = self._open("deadbeef-0000-0000-0000-000000000000")
        self.assertEqual(rc, 1)
        self.assertEqual(self.runs, [])

    # A link in an old restore list outlives its temp dir and its transcript.
    # Clicking it must say why, not open a window onto a failed command.
    def test_a_saved_session_whose_cwd_is_gone_opens_nothing_and_says_why(self):
        self.write_entry(dict(self.ENTRY, cwd="/definitely/not/here"))
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1)
        self.assertEqual(self.runs, [])
        self.assertIn("cwd is gone", out)

    def test_a_saved_session_whose_transcript_is_gone_opens_nothing_and_says_why(self):
        self.on_disk = False
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1)
        self.assertEqual(self.runs, [])
        self.assertIn("transcript is gone", out)


class TestManifestList(unittest.TestCase):
    """With 20 kept, you need to see them before you can choose one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        self.d = os.path.join(self.tmp, "restore")
        os.makedirs(self.d)

    def tearDown(self):
        os.environ.pop("CCWHO_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, n, saved=1788213090):
        with open(os.path.join(self.d, name), "w") as fh:
            json.dump({"version": 1, "savedAt": saved, "count": n, "skipped": 0,
                       "sessions": [{"sessionId": "%08d-1111-4222-8333-abcdefabcdef" % i,
                                     "cwd": "/Users/x/p/a", "project": "a"}
                                    for i in range(n)]}, fh)

    def _list(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.restore(["--list"])
        return rc, out.getvalue() + err.getvalue()

    def test_lists_every_manifest_with_its_session_count(self):
        self._write("2026-08-01T0001.json", 3)
        self._write("2026-08-02T0002.json", 7)
        rc, out = self._list()
        self.assertEqual(rc, 0)
        self.assertIn("2026-08-01T0001.json", out)
        self.assertIn("2026-08-02T0002.json", out)
        self.assertIn("3", out)
        self.assertIn("7", out)

    def test_marks_the_one_a_bare_restore_would_use(self):
        self._write("2026-08-01T0001.json", 3)
        self._write("2026-08-02T0002.json", 7)
        rc, out = self._list()
        newest_line = [l for l in out.splitlines() if "2026-08-02T0002" in l][0]
        older_line = [l for l in out.splitlines() if "2026-08-01T0001" in l][0]
        self.assertIn("newest", newest_line.lower())
        self.assertNotIn("newest", older_line.lower())

    def test_nothing_saved_is_not_an_empty_success(self):
        rc, out = self._list()
        self.assertEqual(rc, 1)
        self.assertIn("save", out.lower())

    def test_list_never_opens_anything(self):
        self._write("2026-08-01T0001.json", 2)
        real = runner.subprocess.run
        calls = []
        runner.subprocess.run = lambda *a, **k: calls.append(a)
        try:
            self._list()
        finally:
            runner.subprocess.run = real
        self.assertEqual(calls, [])


class TestOpenLooksAcrossManifests(unittest.TestCase):
    """A link is clicked from whatever list is on screen, not from the newest one.

    `ccwho restore --from <an older manifest>` prints links for sessions that the
    newest manifest may never have seen - the newest is a snapshot of a later
    moment, and the whole reason to keep 20 is to read the older ones.
    """

    OLD = "11111111-1111-4222-8333-abcdefabcdef"
    NEW = "22222222-1111-4222-8333-abcdefabcdef"

    def setUp(self):
        # about WHAT is running and which record wins, not about the disk: the
        # disk check has its own tests in TestOpenSession
        self.real_resume_problem = runner.resume_problem
        runner.resume_problem = lambda entry: ""
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        self.d = os.path.join(self.tmp, "restore")
        os.makedirs(self.d)
        self._write("2026-08-01T0001.json", [(self.OLD, "/Users/x/p/old")])
        self._write("2026-08-02T0002.json", [(self.NEW, "/Users/x/p/new")])
        self.real_collect = runner.engine.collect
        def fake_collect(cache=None, status=None):      # reachable, and nothing live
            if status is not None:
                status["source_ok"] = True
            return ([], 0)

        runner.engine.collect = fake_collect
        self.runs = []
        self.real_run = runner.subprocess.run

        class Done:
            returncode, stdout, stderr = 0, "", ""

        runner.subprocess.run = lambda *a, **k: self.runs.append(a[0]) or Done()

    def tearDown(self):
        runner.resume_problem = self.real_resume_problem
        runner.engine.collect = self.real_collect
        runner.subprocess.run = self.real_run
        os.environ.pop("CCWHO_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, pairs):
        with open(os.path.join(self.d, name), "w") as fh:
            json.dump({"version": 1, "savedAt": 1788213090, "count": len(pairs),
                       "skipped": 0,
                       "sessions": [{"sessionId": s, "cwd": c, "project": "p"}
                                    for s, c in pairs]}, fh)

    def _open(self, sid):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.open_session([sid])
        return rc, out.getvalue() + err.getvalue()

    def test_a_session_only_in_an_older_manifest_still_reopens(self):
        rc, _ = self._open(self.OLD)
        self.assertEqual(rc, 0)
        self.assertIn("claude --resume " + self.OLD,
                      " ".join(" ".join(c) for c in self.runs))

    def test_the_newest_record_of_a_session_wins(self):
        # Same session saved twice; the later cwd is the one that is still true.
        self._write("2026-08-03T0003.json", [(self.OLD, "/Users/x/p/moved")])
        rc, _ = self._open(self.OLD)
        self.assertEqual(rc, 0)
        joined = " ".join(" ".join(c) for c in self.runs)
        self.assertIn("cd /Users/x/p/moved", joined)
        self.assertNotIn("/Users/x/p/old", joined)


class TestLogTrim(unittest.TestCase):
    """The autosave log is the one thing under ~/.ccwho that nothing bounded.

    Same shape as the manifests: keep the newest N, prune on a schedule. Checked
    once a day rather than every run - 96 runs a day do not each need to rewrite
    the file to decide it is already short enough.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        self.log = os.path.join(self.tmp, "autosave.log")

    def tearDown(self):
        os.environ.pop("CCWHO_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, n):
        with open(self.log, "w") as fh:
            fh.write("".join("line %d\n" % i for i in range(n)))

    def test_keeps_the_newest_lines_and_drops_the_rest(self):
        self._write(100)
        runner.trim_log(self.log, keep=10)
        lines = open(self.log).read().splitlines()
        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[0], "line 90")
        self.assertEqual(lines[-1], "line 99")

    def test_a_short_log_is_left_alone(self):
        self._write(5)
        before = open(self.log).read()
        runner.trim_log(self.log, keep=10)
        self.assertEqual(open(self.log).read(), before)

    def test_leaves_no_temp_file_behind(self):
        self._write(100)
        runner.trim_log(self.log, keep=10)
        self.assertEqual([n for n in os.listdir(self.tmp) if n.endswith(".tmp")], [])

    def test_a_missing_log_is_not_an_error(self):
        runner.trim_log(os.path.join(self.tmp, "nope.log"), keep=10)   # must not raise

    def test_keep_zero_deletes_nothing(self):
        # Same fail-safe as prune_manifests: keep<=0 is "no bound", never "empty it".
        self._write(50)
        runner.trim_log(self.log, keep=0)
        self.assertEqual(len(open(self.log).read().splitlines()), 50)

    def test_checks_once_a_day_not_once_a_run(self):
        self._write(100)
        self.assertTrue(runner.maybe_trim_log(keep=10), "first check must run")
        self.assertEqual(len(open(self.log).read().splitlines()), 10)
        self._write(100)
        self.assertFalse(runner.maybe_trim_log(keep=10), "same day: no second pass")
        self.assertEqual(len(open(self.log).read().splitlines()), 100,
                         "an untrimmed log is the proof the check was skipped")

    def test_a_day_later_it_checks_again(self):
        self._write(100)
        runner.maybe_trim_log(keep=10)
        marker = runner.log_trim_marker()
        old = time.time() - 86400 - 60
        os.utime(marker, (old, old))
        self._write(100)
        self.assertTrue(runner.maybe_trim_log(keep=10))
        self.assertEqual(len(open(self.log).read().splitlines()), 10)


class TestOpenNeverForksALiveSession(unittest.TestCase):
    """`ccwho open` is one of three ways to start `claude --resume`. All three
    have to agree that a session we can SEE running, or a fleet we could not read
    at all, is not something to reopen."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    ENTRY = {"sessionId": SID, "cwd": "/Users/x/p/liveapp", "project": "liveapp"}

    def setUp(self):
        # about WHAT is running and which record wins, not about the disk: the
        # disk check has its own tests in TestOpenSession
        self.real_resume_problem = runner.resume_problem
        runner.resume_problem = lambda entry: ""
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        d = os.path.join(self.tmp, "restore")
        os.makedirs(d)
        with open(os.path.join(d, "2026-08-01T0001.json"), "w") as fh:
            json.dump({"version": 1, "savedAt": 1788213090, "count": 1,
                       "skipped": 0, "sessions": [self.ENTRY]}, fh)
        self.live, self.source_ok = [], True
        self.real_collect = runner.engine.collect

        def fake_collect(cache=None, status=None):
            if status is not None:
                status["source_ok"] = self.source_ok
            return (list(self.live), 0)

        runner.engine.collect = fake_collect
        self.runs = []
        self.real_run = runner.subprocess.run

        class Done:
            returncode, stdout, stderr = 0, "focused s032", ""

        runner.subprocess.run = lambda *a, **k: self.runs.append(a[0]) or Done()

    def tearDown(self):
        runner.resume_problem = self.real_resume_problem
        runner.engine.collect = self.real_collect
        runner.subprocess.run = self.real_run
        os.environ.pop("CCWHO_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open(self, sid):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.open_session([sid])
        return rc, out.getvalue() + err.getvalue()

    def test_a_dead_session_is_still_reopened(self):
        rc, _ = self._open(self.SID)            # control: the case reopening is FOR
        self.assertEqual(rc, 0)
        self.assertIn("claude --resume", " ".join(" ".join(c) for c in self.runs))

    def test_a_live_session_without_a_window_is_attached_not_reopened(self):
        # It used to refuse and say so. A running session with no window is a
        # background session, and `claude attach` gives it one - which is what
        # you wanted from it. Reopening it would still fork the conversation.
        self.live = [{"sessionId": self.SID, "tty": "", "pid": 90266,
                      "kind": "background"}]
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 0, out)
        script = " ".join(" ".join(c) for c in self.runs)
        self.assertIn("claude attach", script)
        self.assertNotIn("--resume", script,
                         "a running session must never be resumed")

    def test_a_session_a_program_runs_is_neither_attached_nor_reopened(self):
        self.live = [{"sessionId": self.SID, "tty": "", "pid": 90267,
                      "attention": "program"}]
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1, out)
        self.assertEqual(self.runs, [], "no window: a program runs it")
        self.assertIn("a program runs it", out)

    def test_an_unreadable_fleet_opens_nothing(self):
        self.source_ok = False
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1)
        self.assertEqual(self.runs, [], "an empty list we could not trust is not 'dead'")
        self.assertIn("not reopening", out)


class TestRestoreOpenSkipsWhatIsAlreadyRunning(unittest.TestCase):
    """`restore --open` launched every entry in the manifest, blind. Run it while
    some of those sessions are still up - after iTerm2 crashed but claude did not,
    or just out of habit - and each live one gets a second process on its
    transcript."""

    LIVE_SID = "11111111-1111-4111-8111-111111111111"
    DEAD_SID = "22222222-2222-4222-8222-222222222222"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        d = os.path.join(self.tmp, "restore")
        os.makedirs(d)
        self.man = os.path.join(d, "2026-08-01T0001.json")
        # Real directories and transcripts: --open reopens only what would resume,
        # so an entry pointing nowhere is no longer a fair stand-in for a good one.
        for proj in ("a", "b", "c"):
            os.makedirs(os.path.join(self.tmp, "p", proj))
        self.cwd = lambda proj: os.path.join(self.tmp, "p", proj)
        self.on_disk = {self.LIVE_SID, self.DEAD_SID}
        self.real_transcript_path = runner.engine.transcript_path
        runner.engine.transcript_path = (
            lambda sid, roots=None: "/tx/%s.jsonl" % sid if sid in self.on_disk else None)
        self.write_manifest([{"sessionId": self.LIVE_SID, "cwd": self.cwd("a"), "project": "a"},
                             {"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"}])
        self.live, self.source_ok = [], True
        self.real_collect = runner.engine.collect

        def fake_collect(cache=None, status=None):
            if status is not None:
                status["source_ok"] = self.source_ok
            return (list(self.live), 0)

        runner.engine.collect = fake_collect
        self.runs = []
        self.real_run = runner.subprocess.run

        class Done:
            returncode, stdout, stderr = 0, "", ""

        runner.subprocess.run = lambda *a, **k: self.runs.append(a) or Done()

    def write_manifest(self, sessions):
        with open(self.man, "w") as fh:
            json.dump({"version": 1, "savedAt": 1788213090, "count": len(sessions),
                       "skipped": 0, "sessions": sessions}, fh)

    def tearDown(self):
        runner.engine.collect = self.real_collect
        runner.engine.transcript_path = self.real_transcript_path
        runner.subprocess.run = self.real_run
        os.environ.pop("CCWHO_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _restore_open(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.restore(["--open"])
        return rc, out.getvalue() + err.getvalue()

    def _script(self):
        return " ".join(str(a) for run in self.runs for a in run[0])

    def test_all_dead_opens_all_of_them(self):
        rc, out = self._restore_open()          # control
        self.assertEqual(rc, 0)
        script = self._script()
        self.assertIn("claude --resume " + self.LIVE_SID, script)
        self.assertIn("claude --resume " + self.DEAD_SID, script)

    def test_a_running_session_is_skipped_and_named(self):
        self.live = [{"sessionId": self.LIVE_SID, "tty": "ttys009", "pid": 7}]
        rc, out = self._restore_open()
        self.assertEqual(rc, 0)
        script = self._script()
        self.assertNotIn(self.LIVE_SID, script, "it is already running: opening forks it")
        self.assertIn("claude --resume " + self.DEAD_SID, script)
        self.assertIn("already open", out)

    def test_a_running_session_without_a_window_is_also_skipped(self):
        self.live = [{"sessionId": self.LIVE_SID, "tty": "", "pid": 8}]
        rc, out = self._restore_open()
        self.assertEqual(rc, 0)
        self.assertNotIn(self.LIVE_SID, self._script())

    def test_a_session_a_program_runs_is_also_skipped(self):
        self.live = [{"sessionId": self.LIVE_SID, "tty": "", "pid": 8,
                      "attention": "program"}]
        rc, out = self._restore_open()
        self.assertEqual(rc, 0)
        self.assertNotIn(self.LIVE_SID, self._script())
        self.assertIn("claude --resume " + self.DEAD_SID, self._script(), "control")
        # running, and said so - not "cannot be reopened", and no advice to open it
        self.assertIn("a: a program runs it", out)
        self.assertNotIn("cannot be reopened", out)
        self.assertNotIn("ccwho open " + self.LIVE_SID, out)

    def test_an_unreadable_fleet_opens_nothing_at_all(self):
        self.source_ok = False
        rc, out = self._restore_open()
        self.assertEqual(rc, 1)
        self.assertEqual(self.runs, [], "17 windows on a guess is the worst outcome")
        self.assertIn("not reopening", out)

    def test_the_same_session_twice_in_a_manifest_opens_once(self):
        # a hand-edited or double-written manifest must not start two processes
        # on one transcript - the exact harm this guard exists to prevent
        self.write_manifest([{"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"},
                             {"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"}])
        rc, out = self._restore_open()
        self.assertEqual(rc, 0)
        self.assertEqual(self._script().count("claude --resume " + self.DEAD_SID), 1)
        # and the duplicate is simply not a second entry: it is the same session,
        # not another process starting it, so it must not be reported as one
        self.assertNotIn("already starting", out)

    def test_an_entry_that_cannot_be_rebuilt_is_reported_not_swallowed(self):
        # one running + one unusable is not "all of them are already running"
        self.write_manifest([{"sessionId": self.LIVE_SID, "cwd": self.cwd("a"), "project": "a"},
                             {"sessionId": "not a session id", "cwd": self.cwd("c"),
                              "project": "c"}])
        self.live = [{"sessionId": self.LIVE_SID, "tty": "ttys009", "pid": 7}]
        rc, out = self._restore_open()
        self.assertEqual(self.runs, [])
        self.assertEqual(rc, 1, "nothing opened and something was unusable")
        self.assertIn("cannot be reopened", out)

    # --check already knew these would fail; --open launched them anyway. A real
    # restore opened eight windows onto `cd: no such file or directory` and
    # "No conversation found": test runs in a deleted $TMPDIR, and headless
    # workers that never wrote a transcript.
    def test_a_gone_cwd_is_not_opened_and_is_named(self):
        self.write_manifest([
            {"sessionId": self.LIVE_SID, "cwd": "/definitely/not/here", "project": "reaped"},
            {"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"}])
        rc, out = self._restore_open()
        self.assertEqual(rc, 0, out)
        script = self._script()
        self.assertNotIn(self.LIVE_SID, script, "a window onto a failed cd is left behind")
        self.assertIn("claude --resume " + self.DEAD_SID, script)
        self.assertIn("reaped", out)
        self.assertIn("cwd is gone", out)

    def test_a_gone_transcript_is_not_opened_and_is_named(self):
        self.on_disk = {self.DEAD_SID}
        rc, out = self._restore_open()
        self.assertEqual(rc, 0, out)
        script = self._script()
        self.assertNotIn(self.LIVE_SID, script, "--resume has nothing to find")
        self.assertIn("claude --resume " + self.DEAD_SID, script)
        self.assertIn("transcript is gone", out)

    def test_nothing_that_would_resume_opens_nothing_and_fails(self):
        self.on_disk = set()
        rc, out = self._restore_open()
        self.assertEqual(self.runs, [])
        self.assertEqual(rc, 1, out)

    def test_a_session_it_will_not_open_is_not_claimed(self):
        # a claim is "I am opening this"; holding one for a window never opened
        # would make the next, legitimate open report "already starting"
        self.on_disk = {self.DEAD_SID}
        self._restore_open()
        self.assertTrue(runner.claim_launch(self.LIVE_SID))

    def test_every_session_live_opens_nothing_and_says_so(self):
        self.live = [{"sessionId": self.LIVE_SID, "tty": "ttys009", "pid": 7},
                     {"sessionId": self.DEAD_SID, "tty": "ttys010", "pid": 8}]
        rc, out = self._restore_open()
        self.assertEqual(self.runs, [])
        self.assertIn("already open", out)


class TestRestoreOpenFillsRestoredPanes(unittest.TestCase):
    """The person's workaround was to let iTerm2 bring its windows back, then
    move every reopened session into its old pane by hand. A pane iTerm2
    restores keeps its unique id, so the restore can write into it."""

    LIVE_SID = TestRestoreOpenSkipsWhatIsAlreadyRunning.LIVE_SID
    DEAD_SID = TestRestoreOpenSkipsWhatIsAlreadyRunning.DEAD_SID
    setUp = TestRestoreOpenSkipsWhatIsAlreadyRunning.setUp
    write_manifest = TestRestoreOpenSkipsWhatIsAlreadyRunning.write_manifest
    _restore_open = TestRestoreOpenSkipsWhatIsAlreadyRunning._restore_open
    _script = TestRestoreOpenSkipsWhatIsAlreadyRunning._script

    def setUp(self):
        TestRestoreOpenSkipsWhatIsAlreadyRunning.setUp(self)
        self.write_manifest([
            {"sessionId": self.LIVE_SID, "cwd": self.cwd("a"), "project": "a"},
            {"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b",
             "pane": "G-B", "tabTitle": "b"}])
        self.panes = {"ttys050": {"pane": "G-B", "name": "-zsh"}}
        self.idle = {"ttys050"}
        self.wrote = "G-B\n"
        real = (runner.engine.terms.ITERM2.panes, runner.engine.idle_snapshot)
        runner.engine.terms.ITERM2.panes = lambda **k: self.panes
        runner.engine.idle_snapshot = lambda: self.idle
        self.addCleanup(setattr, runner.engine.terms.ITERM2, "panes", real[0])
        self.addCleanup(setattr, runner.engine, "idle_snapshot", real[1])

        test = self

        class Done:
            returncode, stderr = 0, ""

            @property
            def stdout(self):
                return test.wrote

        runner.subprocess.run = lambda *a, **k: self.runs.append(a) or Done()

    tearDown = TestRestoreOpenSkipsWhatIsAlreadyRunning.tearDown

    def test_a_session_whose_pane_came_back_resumes_in_it(self):
        rc, out = self._restore_open()
        self.assertEqual(rc, 0, out)
        script = self._script()
        self.assertIn('if u is "G-B"', script)
        self.assertEqual(script.count("create window with default profile"), 1,
                         "only the session with no pane gets a window")
        self.assertIn("claude --resume " + self.LIVE_SID, script)
        self.assertIn("filled 1 pane", out)
        self.assertIn("opened 1 window", out)

    def test_a_busy_pane_gets_a_window_instead(self):                   # control
        self.idle = set()
        self.wrote = ""
        rc, out = self._restore_open()
        self.assertEqual(rc, 0, out)
        script = self._script()
        self.assertNotIn(" u is ", script)
        self.assertEqual(script.count("create window with default profile"), 2)

    def test_a_pane_that_closed_before_the_write_gets_a_window(self):
        self.wrote = ""
        rc, out = self._restore_open()
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.runs), 2, "a second script opens what the first could not")
        second = " ".join(str(a) for a in self.runs[1][0])
        self.assertIn("create window with default profile", second)
        self.assertIn("claude --resume " + self.DEAD_SID, second)
        self.assertNotIn(self.LIVE_SID, second, "already opened by the first script")
        self.assertIn("pane closed", out)
        self.assertNotIn("filled 1 pane", out)
        self.assertIn("opened 2 window", out)
        self.assertNotIn("not reopening", out)

    def test_every_pane_written_needs_no_second_script(self):          # control
        rc, out = self._restore_open()
        self.assertEqual(len(self.runs), 1)

    def test_a_title_another_saved_session_had_is_not_used(self):
        # LIVE is running, so only DEAD opens - but both were saved as "X"
        self.write_manifest([
            {"sessionId": self.LIVE_SID, "cwd": self.cwd("a"), "project": "a",
             "pane": "PA", "tabTitle": "X"},
            {"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b",
             "pane": "OLD", "tabTitle": "X"}])
        self.live = [{"sessionId": self.LIVE_SID, "tty": "ttys009", "pid": 7}]
        self.panes = {"ttys050": {"pane": "P-NEW", "name": "X"}}
        self._restore_open()
        self.assertNotIn("unique id", self._script())

    def test_a_manifest_with_no_panes_does_not_ask_iterm(self):
        self.write_manifest([{"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"}])
        asked = []
        runner.engine.terms.ITERM2.panes = lambda **k: asked.append(1) or {}
        self._restore_open()
        self.assertEqual(asked, [])


class TestRestoreCheckCountsPanes(unittest.TestCase):
    setUp = TestRestoreCheck.setUp
    tearDown = TestRestoreCheck.tearDown
    write = TestRestoreCheck.write
    run_check = TestRestoreCheck.run_check

    def test_check_says_how_many_saved_panes_are_open(self):
        self.write([{"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef", "cwd": self.tmp,
                     "project": "a", "pane": "G-A"},
                    {"sessionId": "a1b2c3d4-1111-4222-8333-abcdefabcdef", "cwd": self.tmp,
                     "project": "b", "pane": "G-GONE"}])
        iterm = runner.engine.terms.ITERM2
        real = iterm.panes
        iterm.panes = lambda **k: {"ttys050": {"pane": "G-A", "name": ""}}
        try:
            _rc, out = self.run_check(["--check"])
        finally:
            iterm.panes = real
        self.assertIn("1 of 2 saved panes", out)
        self.assertNotIn("resumes", out, "--open writes only into the idle ones")


class TestLaunchClaim(unittest.TestCase):
    """The guard reads the world, then launches. Between those two moments another
    ccwho - a second click, a restore running in another window - reads the same
    world and launches too. A new session also takes a moment to show up in
    `claude agents`, so the window is real even for one caller in a hurry.

    A file per session, read and replaced under one lock, and a claim whose
    holder is gone is reclaimed rather than blocking forever.
    """

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp

    def tearDown(self):
        os.environ.pop("CCWHO_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_first_caller_gets_the_claim(self):
        self.assertTrue(runner.claim_launch(self.SID))

    def test_a_second_caller_is_refused_while_it_is_held(self):
        self.assertTrue(runner.claim_launch(self.SID))
        self.assertFalse(runner.claim_launch(self.SID),
                         "two launches on one transcript is the fork we are preventing")

    def test_a_different_session_is_not_blocked(self):
        runner.claim_launch(self.SID)
        self.assertTrue(runner.claim_launch("99999999-9999-4999-8999-999999999999"))

    def test_a_claim_whose_owner_died_is_reclaimed(self):
        runner.claim_launch(self.SID, pid=999999)      # a pid that is not alive
        self.assertTrue(runner.claim_launch(self.SID),
                        "a crashed launcher must not lock a session out forever")

    def test_an_expired_claim_is_reclaimed_even_if_the_owner_lives(self):
        runner.claim_launch(self.SID, now=1000.0)
        self.assertFalse(runner.claim_launch(self.SID, now=1000.0 + 30))
        self.assertTrue(runner.claim_launch(self.SID, now=1000.0 + 600),
                        "the claim covers the launch, not the session's lifetime")

    def test_a_corrupt_claim_file_does_not_wedge_the_session(self):
        runner.claim_launch(self.SID)
        path = os.path.join(runner.ccwho_dir(), "launching",
                            self.SID + ".json")
        with open(path, "w") as fh:
            fh.write("{ not json")
        self.assertTrue(runner.claim_launch(self.SID))


class TestOpenUsesTheLaunchClaim(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    ENTRY = {"sessionId": SID, "cwd": "/Users/x/p/liveapp", "project": "liveapp"}

    def setUp(self):
        # about WHAT is running and which record wins, not about the disk: the
        # disk check has its own tests in TestOpenSession
        self.real_resume_problem = runner.resume_problem
        runner.resume_problem = lambda entry: ""
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        d = os.path.join(self.tmp, "restore")
        os.makedirs(d)
        with open(os.path.join(d, "2026-08-01T0001.json"), "w") as fh:
            json.dump({"version": 1, "savedAt": 1788213090, "count": 1,
                       "skipped": 0, "sessions": [self.ENTRY]}, fh)
        self.real_collect = runner.engine.collect

        def fake_collect(cache=None, status=None):
            if status is not None:
                status["source_ok"] = True
            return ([], 0)

        runner.engine.collect = fake_collect
        self.runs = []
        self.real_run = runner.subprocess.run

        class Done:
            returncode, stdout, stderr = 0, "", ""

        runner.subprocess.run = lambda *a, **k: self.runs.append(a[0]) or Done()

    def tearDown(self):
        runner.resume_problem = self.real_resume_problem
        runner.engine.collect = self.real_collect
        runner.subprocess.run = self.real_run
        os.environ.pop("CCWHO_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.open_session([self.SID])
        return rc, out.getvalue() + err.getvalue()

    def test_the_first_open_launches(self):
        rc, _ = self._open()                       # control
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.runs), 1)

    def test_a_second_open_while_the_first_is_starting_does_not_launch_again(self):
        self._open()
        rc, out = self._open()
        self.assertEqual(len(self.runs), 1, "the session is already being started")
        self.assertEqual(rc, 1)
        self.assertIn("already starting", out)


class TestHotReloadCoversTheBriefModule(unittest.TestCase):
    """The engine is reloaded every tick so an edit lands in a running watch.
    The brief moved half the extraction rules into a second module; if only the
    engine reloads, a fix to those rules looks like it did nothing."""

    def tearDown(self):
        # a reload re-executes the engine and rebinds the real functions
        install_guards()

    def test_editing_the_brief_module_lands_on_the_next_tick(self):
        import ccwho_brief
        path = ccwho_brief.__file__
        original = open(path).read()
        marker = "def _hot_reload_probe():\n    return 'edited'\n"
        try:
            with open(path, "w") as fh:
                fh.write(original + "\n\n" + marker)
            runner.reload_engine({"engine_error": ""})
            self.assertTrue(hasattr(runner.engine.brief, "_hot_reload_probe"),
                            "the brief module was not re-read")
        finally:
            with open(path, "w") as fh:
                fh.write(original)
            runner.reload_engine({"engine_error": ""})

    def test_an_edit_that_blows_up_at_import_leaves_the_old_rules_working(self):
        """A syntax error is the easy case: nothing runs. The one that bites is an
        edit that RUNS and then raises - half the module's new definitions are
        already in place, and catching the error leaves that half live. The next
        tick then extracts with a module that is neither version."""
        import ccwho_brief
        path = ccwho_brief.__file__
        original = open(path).read()
        state = {"engine_error": ""}
        try:
            with open(path, "w") as fh:
                fh.write(original.replace(
                    'MAX_PROGRESS = 5', 'MAX_PROGRESS = 5\nLOW_SIGNAL = set()\n'
                    'raise RuntimeError("boom")'))
            runner.reload_engine(state)
            self.assertTrue(state["engine_error"], "the error has to reach the banner")
            self.assertTrue(
                runner.engine.brief.LOW_SIGNAL,
                "the half-applied edit must not become the live rule set")
            self.assertFalse(runner.engine.brief.is_substantive("continue"),
                             "extraction still works, with the last good rules")
        finally:
            with open(path, "w") as fh:
                fh.write(original)
            runner.reload_engine({"engine_error": ""})

    def test_every_hot_module_is_re_read_not_just_the_engine(self):
        # every rule module the engine imports; a module left out of the reload is
        # a module whose fix silently does not land in a running watch
        for mod in ("ccwho_brief", "ccwho_index", "ccwho_procs", "ccwho_terms"):
            with self.subTest(module=mod):
                m = __import__(mod)
                path = m.__file__
                original = open(path).read()
                try:
                    with open(path, "w") as fh:
                        fh.write(original + "\n\ndef _hot_probe():\n    return 1\n")
                    runner.reload_engine({"engine_error": ""})
                    self.assertTrue(hasattr(__import__(mod), "_hot_probe"), mod)
                    # and the runner's own handle has to be the new module too,
                    # or `ccwho ls` keeps calling yesterday's code
                    handle = {"ccwho_brief": runner.engine.brief,
                              "ccwho_index": runner.index,
                              "ccwho_procs": runner.engine.procs,
                              "ccwho_terms": runner.engine.terms}[mod]
                    self.assertTrue(hasattr(handle, "_hot_probe"),
                                    f"runner still holds the old {mod}")
                finally:
                    with open(path, "w") as fh:
                        fh.write(original)
                    runner.reload_engine({"engine_error": ""})

    def test_a_broken_brief_edit_does_not_kill_the_loop(self):
        import ccwho_brief
        path = ccwho_brief.__file__
        original = open(path).read()
        state = {"engine_error": ""}
        try:
            with open(path, "w") as fh:
                fh.write(original + "\nthis is not python(")
            runner.reload_engine(state)
            self.assertTrue(state["engine_error"], "the error has to reach the banner")
        finally:
            with open(path, "w") as fh:
                fh.write(original)
            runner.reload_engine({"engine_error": ""})


class TestShowVerb(unittest.TestCase):
    """`ccwho show <anything>` answers "what was this session doing" without
    opening it."""

    SID = "6c4c20f6-c6fb-46e5-bc8a-699f013cbe69"

    def setUp(self):
        self.real_collect = runner.engine.collect
        self.rows = [{"sessionId": self.SID, "project": "liveapp", "tty": "ttys022",
                      "pid": 90266, "name": "liveapp-f0", "title": "Issue 362",
                      "attention": "stopped", "status": "idle", "since": "5h",
                      "cwd": "/Users/x/projects/liveapp", "topic": "", "first": "",
                      "ask": "", "doing": "", "orphans": 0, "work": 0}]

        def fake_collect(cache=None, status=None):
            if status is not None:
                status["source_ok"] = True
            return (list(self.rows), 0)

        runner.engine.collect = fake_collect
        self.real_windows = runner.engine.read_windows
        head = [json.dumps({"type": "user", "timestamp": "2026-09-14T09:00:00.000Z",
                            "message": {"role": "user",
                                        "content": "fix issue 362, the memory bloat"}})]
        tail = [json.dumps({"type": "system", "subtype": "away_summary",
                            "content": "Goal: fix the loop's memory bloat (#362).",
                            "timestamp": "2026-09-16T10:18:00.000Z"}),
                json.dumps({"type": "assistant", "timestamp": "2026-09-18T15:48:00.000Z",
                            "message": {"role": "assistant",
                                        "content": [{"type": "text",
                                                     "text": "Shall I land it?"}]}})]
        runner.engine.read_windows = lambda sid, **kw: (head, tail, 1788213090)
        # the index is on this machine, and a test that reads it measures this
        # laptop rather than the code
        self.real_index = runner.fresh_index
        runner.fresh_index = lambda quiet=False: {}

    def tearDown(self):
        runner.engine.collect = self.real_collect
        runner.engine.read_windows = self.real_windows
        runner.fresh_index = self.real_index

    def _show(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.show(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def test_it_finds_a_session_by_a_word_in_its_title(self):
        rc, out = self._show("362")
        self.assertEqual(rc, 0)
        self.assertIn("memory bloat", out, "the recap is the point of the brief")
        self.assertIn("Shall I land it?", out)
        self.assertIn("fix issue 362", out)

    def test_it_shows_the_other_names_of_the_session(self):
        _, out = self._show("362")
        self.assertIn("6c4c", out)          # short id
        self.assertIn("s022", out)          # same short tty the list column shows
        self.assertIn(self.SID, out)

    def test_the_recap_never_appears_without_its_age(self):
        _, out = self._show("362")
        lines = out.splitlines()
        i = [n for n, x in enumerate(lines) if "memory bloat" in x][0]
        self.assertRegex(lines[i - 1], r"recap \d+[smhd] old",
                         "a recap with no age hides how stale it is")

    def test_an_ambiguous_query_lists_the_candidates_instead_of_guessing(self):
        self.rows.append(dict(self.rows[0], sessionId="9999", title="Issue 362 part two",
                              tty="ttys099"))
        rc, out = self._show("362")
        self.assertEqual(rc, 2)
        self.assertIn("matches 2 sessions", out)

    def test_no_match_says_so(self):
        rc, out = self._show("nothing-like-this")
        self.assertEqual(rc, 1)
        self.assertIn("no session matches", out)

    def test_a_session_with_nothing_in_it_says_so(self):
        # a session started but never used has no transcript at all: "(no recap
        # yet)" reads as "it is working on something", which is not true
        runner.engine.read_windows = lambda sid, **kw: ([], [], 0)
        _, out = self._show("362")
        self.assertIn("nothing typed in this session yet", out)

    def test_json_output_carries_the_brief(self):
        rc, out = self._show("362", "--json")
        self.assertEqual(rc, 0)
        got = json.loads(out)
        self.assertEqual(got["aka"]["short_id"], "6c4c")
        self.assertIn("memory bloat", got["recap"])


class TestDoctorVerb(unittest.TestCase):
    """`ccwho doctor` reports; it never repairs. Every line says what was checked
    and, when it is wrong, the command to run."""

    def setUp(self):
        self.real_gather = runner.setup.gather
        self.facts = {"claude": "/usr/local/bin/claude", "iterm_ok": True,
                      "handler_registered": True, "launchd_loaded": True,
                      "last_run_age": 300.0, "newest_manifest_age": 300.0,
                      "cc_status_hook": True,
                      "settings_path": "/Users/x/.claude/settings.json"}
        runner.setup.gather = lambda **kw: dict(self.facts)
        self.real_usage_facts = runner.usage_facts
        self.usage = {"usage_roots": [], "usage_newest_age": None}
        runner.usage_facts = lambda now=None: dict(self.usage)

    def tearDown(self):
        runner.usage_facts = self.real_usage_facts
        runner.setup.gather = self.real_gather

    def _doctor(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.doctor(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def test_a_healthy_machine_exits_zero_and_says_so(self):
        rc, out = self._doctor()
        self.assertEqual(rc, 0)
        self.assertIn("claude", out)
        self.assertIn("ok", out.lower())

    def test_a_fault_exits_one_and_prints_the_command_to_run(self):
        self.facts["handler_registered"] = False
        rc, out = self._doctor()
        self.assertEqual(rc, 1)
        self.assertIn("install-handler.sh", out)

    def test_it_repairs_nothing(self):
        self.facts["cc_status_hook"] = False
        real_run = runner.subprocess.run
        runs = []
        runner.subprocess.run = lambda *a, **k: runs.append(a) or None
        try:
            self._doctor()
        finally:
            runner.subprocess.run = real_run
        self.assertEqual(runs, [], "R1 doctor writes nothing and runs nothing")

    def test_json_for_a_status_line(self):
        rc, out = self._doctor("--json")
        got = json.loads(out)
        self.assertTrue(all("name" in c and "ok" in c for c in got["checks"]))
        self.assertEqual(got["ok"], True)


class TestWatchShowsTheWorstFault(unittest.TestCase):
    def test_the_header_carries_one_banner_line(self):
        checks = runner.setup.doctor_checks(
            {"claude": "", "iterm_ok": True, "handler_registered": True,
             "launchd_loaded": True, "last_run_age": 10.0,
             "newest_manifest_age": 10.0, "cc_status_hook": True,
             "settings_path": "/x"})
        line = runner.setup.doctor_banner(checks)
        self.assertIn("ccwho doctor", line)
        self.assertEqual(line.count("\n"), 0, "a header has room for one line")


class TestDoctorSaysUsage(TestDoctorVerb):
    def test_a_dir_without_the_statusline_is_bad_with_the_fix(self):
        self.usage = {"usage_roots": [{"root": "/h/.claude", "state": "missing",
                                       "opted_out": False}], "usage_newest_age": None}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = runner.doctor([])
        self.assertEqual(rc, 1)
        self.assertIn("usage (/h/.claude)", out.getvalue())
        self.assertIn("ccwho setup", out.getvalue())


class TestWatchBannerIsCheap(unittest.TestCase):
    """The watch header should say when something drifted, but the checks shell
    out to launchctl, osascript and plutil. At a 5s tick that is three processes
    a second for an answer that changes about once a month."""

    def setUp(self):
        self.calls = []
        self.real = runner.setup.gather
        self.real_usage_facts = runner.usage_facts
        runner.usage_facts = lambda now=None: {"usage_roots": [], "usage_newest_age": None}
        runner.setup.gather = lambda **kw: self.calls.append(1) or {
            "claude": "", "iterm_ok": True, "handler_registered": True,
            "launchd_loaded": True, "last_run_age": 10.0,
            "newest_manifest_age": 10.0, "cc_status_hook": True,
            "settings_path": "/x"}

    def tearDown(self):
        runner.usage_facts = self.real_usage_facts
        runner.setup.gather = self.real

    def test_the_banner_names_the_fault(self):
        state = {}
        self.assertIn("claude", runner.doctor_banner_cached(state, now=1000.0))

    def test_it_does_not_re_check_every_tick(self):
        state = {}
        for t in range(0, 20, 5):        # four ticks of a 5s watch
            runner.doctor_banner_cached(state, now=1000.0 + t)
        self.assertEqual(len(self.calls), 1)

    def test_it_re_checks_eventually(self):
        state = {}
        runner.doctor_banner_cached(state, now=1000.0)
        runner.doctor_banner_cached(state, now=1000.0 + runner.DOCTOR_TTL + 1)
        self.assertEqual(len(self.calls), 2)

    def test_a_healthy_machine_shows_nothing(self):
        runner.setup.gather = lambda **kw: {
            "claude": "/bin/claude", "iterm_ok": True, "handler_registered": True,
            "launchd_loaded": True, "last_run_age": 10.0,
            "newest_manifest_age": 10.0, "cc_status_hook": True,
            "settings_path": "/x"}
        self.assertEqual(runner.doctor_banner_cached({}, now=1000.0), "")


class TestFindsSessionsThatAreNotRunning(unittest.TestCase):
    """The live fleet is fifteen; the disk holds a month. "Sometimes I need to
    find sessions from a while ago, so all sessions are fair game.\""""

    LIVE = "live1111-0000-4000-8000-000000000001"
    DEAD = "dead2222-0000-4000-8000-000000000002"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        self.rows = [{"sessionId": self.LIVE, "project": "liveapp", "tty": "ttys022",
                      "pid": 90266, "name": "liveapp-f0", "title": "Issue 362",
                      "tab_title": "✳ Issue 362 (claude)", "attention": "stopped",
                      "status": "idle", "since": "5h", "cwd": "/Users/x/liveapp",
                      "topic": "", "first": "", "ask": "", "doing": "", "orphans": 0,
                      "work": 0}]
        self.index = {
            self.LIVE: {"sessionId": self.LIVE, "title": "Issue 362", "recap": "",
                        "you_said": "rebase", "opened": "", "project": "liveapp",
                        "last_ts": "2026-09-20T10:00:00.000Z"},
            self.DEAD: {"sessionId": self.DEAD, "title": "Docker high CPU",
                        "recap": "containers pegged a core", "you_said": "why hot",
                        "opened": "", "project": "liveapp",
                        "last_ts": "2026-09-01T10:00:00.000Z"},
        }
        self.real_collect = runner.engine.collect
        self.real_load = runner.index.load
        self.real_update = runner.index.update
        self.real_windows = runner.engine.read_windows

        def fake_collect(cache=None, status=None):
            if status is not None:
                status["source_ok"] = True
            return (list(self.rows), 0)

        runner.engine.collect = fake_collect
        runner.index.load = lambda path: dict(self.index)
        runner.index.update = lambda idx, paths, **kw: dict(self.index)
        runner.engine.read_windows = lambda sid, **kw: ([], [json.dumps(
            {"type": "system", "subtype": "away_summary",
             "content": "containers pegged a core",
             "timestamp": "2026-09-01T09:00:00.000Z"})], 0)

    def tearDown(self):
        runner.engine.collect = self.real_collect
        runner.index.load = self.real_load
        runner.index.update = self.real_update
        runner.engine.read_windows = self.real_windows
        os.environ.pop("CCWHO_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, fn, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(list(argv))
        return rc, out.getvalue() + err.getvalue()

    def test_show_falls_back_to_a_session_that_ended(self):
        rc, out = self._run(runner.show, "docker")
        self.assertEqual(rc, 0)
        self.assertIn("pegged a core", out)
        self.assertIn("ended", out.lower(), "say that it is not running any more")
        self.assertIn("claude --resume " + self.DEAD, out)

    def test_a_live_session_still_wins(self):                     # control
        rc, out = self._run(runner.show, "362")
        self.assertEqual(rc, 0)
        self.assertNotIn("ended", out.lower())

    def test_ls_with_words_lists_live_and_ended(self):
        rc, out = self._run(runner.ls, "liveapp")
        self.assertEqual(rc, 0)
        self.assertIn("Issue 362", out)
        self.assertIn("Docker high CPU", out)
        self.assertIn("ended", out.lower())

    def test_ls_without_words_is_the_table_as_it_was(self):
        rc, out = self._run(runner.ls)
        self.assertEqual(rc, 0)
        self.assertIn("Issue 362", out)
        self.assertNotIn("Docker high CPU", out, "ended sessions are not the fleet")

    def test_a_live_session_found_through_the_index_is_still_live(self):
        # the index knows the recap; the live table does not. A word from the
        # recap must not turn a running session into an "ended" one.
        self.index[self.LIVE]["recap"] = "the gate flake, finally understood"
        rc, out = self._run(runner.ls, "flake")
        self.assertEqual(rc, 0)
        self.assertIn("Issue 362", out)
        self.assertNotIn("ended", out.lower(),
                         "it is running: the row belongs in the table")

    def test_words_match_across_fields_not_just_as_a_phrase(self):
        # "gate flake" should find a session whose title has one word and whose
        # recap has the other - the live table matches contiguous text only
        self.index[self.LIVE]["recap"] = "flake in the timing suite"
        rc, out = self._run(runner.ls, "362 flake")
        self.assertEqual(rc, 0)
        self.assertIn("Issue 362", out)

    def test_show_keeps_the_recap_the_index_found(self):
        # the brief reads a head and a tail window; a recap in the unread middle
        # of a long transcript is exactly what the index is for
        self.index[self.DEAD]["recap"] = "the middle-of-file summary"
        self.index[self.DEAD]["turns_since_recap"] = 7
        runner.engine.read_windows = lambda sid, **kw: ([], [], 0)
        rc, out = self._run(runner.show, "docker")
        self.assertEqual(rc, 0)
        self.assertIn("middle-of-file summary", out)
        self.assertIn("7 turns", out)

    def test_ls_says_when_nothing_matches(self):
        rc, out = self._run(runner.ls, "nothing-like-this")
        self.assertEqual(rc, 1)
        self.assertIn("no session", out.lower())


class TestPlainCcwhoOpensTheUi(unittest.TestCase):
    """`ccwho` on a terminal opens the list; piped or under launchd it prints the
    table exactly as it always did. The one-shot output is what scripts, the
    status line and the autosave job read."""

    def setUp(self):
        self.runs = []
        self.real_run = runner.subprocess.run
        self.real_collect = runner.engine.collect

        class Done:
            returncode = 0

        runner.subprocess.run = lambda *a, **k: self.runs.append(a[0]) or Done()
        runner.engine.collect = lambda cache=None, status=None: ([], 0)

    def tearDown(self):
        runner.subprocess.run = self.real_run
        runner.engine.collect = self.real_collect

    class _Captured(io.StringIO):
        """redirect_stdout replaces stdout with a StringIO, whose isatty() is
        always False - so the thing under test has to be told, here."""

        tty = False

        def isatty(self):
            return self.tty

    def _main(self, argv, tty):
        out, err = self._Captured(), io.StringIO()
        out.tty = tty
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.main(argv)
        return rc, out.getvalue() + err.getvalue()

    def test_on_a_terminal_it_runs_the_ui(self):
        self._main([], tty=True)
        self.assertEqual(len(self.runs), 1)
        self.assertIn("ccwho_ui.py", " ".join(self.runs[0]))

    def test_piped_it_prints_the_table(self):
        self._main([], tty=False)
        self.assertEqual(self.runs, [], "a pipe wants the table, not a full-screen app")

    def test_ls_always_prints_the_table(self):
        self._main(["ls"], tty=True)
        self.assertEqual(self.runs, [])

    def test_a_flag_still_means_the_one_shot(self):
        for flag in ("--json", "--blocked", "--prompt"):
            self.runs.clear()
            self._main([flag], tty=True)
            self.assertEqual(self.runs, [], flag)

    def test_without_uv_it_says_what_to_install(self):
        real_which = runner.shutil.which
        real_exists = runner.os.path.exists
        runner.os.path.exists = lambda p: False      # nor in the usual places
        self.addCleanup(setattr, runner.os.path, "exists", real_exists)
        runner.shutil.which = lambda name: None
        try:
            rc, out = self._main([], tty=True)
        finally:
            runner.shutil.which = real_which
        self.assertEqual(rc, 1)
        self.assertIn("uv", out)
        self.assertEqual(self.runs, [], "no traceback, no half-started app")

    def test_a_restart_from_the_ui_starts_it_again(self):
        class Restart:
            returncode = 42            # the ui asks to be re-executed

        answers = [Restart(), type("Done", (), {"returncode": 0})()]
        runner.subprocess.run = lambda *a, **k: self.runs.append(a[0]) or answers.pop(0)
        self._main([], tty=True)
        self.assertEqual(len(self.runs), 2, "R restarts it with the new code")


class SetupHarness(unittest.TestCase):
    """`ccwho setup` is the one command a new machine runs. Nothing here may
    touch the real machine: every write goes to a temp home, and every
    subprocess is recorded rather than run."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.dir = os.path.join(self.home, ".ccwho")
        os.makedirs(self.dir, exist_ok=True)
        os.environ[runner.TEST_HOME_VAR] = self.home
        self.addCleanup(os.environ.pop, runner.TEST_HOME_VAR, None)
        self.ran = []
        self.real_run = runner.subprocess.run
        self.real_gather = runner.setup.gather
        self.real_which = runner.shutil.which
        self.real_dir = runner.ccwho_dir
        self.returns = {}

        def fake_run(cmd, *a, **k):
            self.ran.append(cmd)
            rc = self.returns.get(cmd[0], 0)
            return type("Done", (), {"returncode": rc, "stdout": "", "stderr": ""})()

        runner.subprocess.run = fake_run
        runner.ccwho_dir = lambda: self.dir
        self.claude = os.path.join(self.home, ".local", "bin", "claude")
        # ccwho itself, and the interpreter running these tests, live under the
        # REAL home - which is another user's home as far as a temp home is
        # concerned, and plist_problems is right to say so. Pin both.
        self.real_bin = runner.ccwho_bin
        self.real_exe = runner.sys.executable
        runner.ccwho_bin = lambda: os.path.join(self.home, "src", "ccwho", "ccwho.py")
        runner.sys.executable = "/usr/bin/python3"
        runner.shutil.which = lambda n: {"uv": "/opt/homebrew/bin/uv",
                                         "claude": self.claude,
                                         "bash": "/bin/bash"}.get(n, "")
        # The default machine is one where everything is installed AND current:
        # the applet calls this ccwho, the loaded job is the one we would write.
        self.real_script = runner.handler_script
        self.real_render = runner.render_autosave
        runner.handler_script = lambda: (
            'do shell script quoted form of "%s" & " url "' % runner.ccwho_bin())
        with open(os.path.join(runner.repo_dir(),
                               runner.setup.PLIST_TEMPLATE_NAME)) as fh:
            self.job_text = runner.setup.render_plist(
                fh.read(), home=self.home, ccwho=runner.ccwho_bin(),
                claude=self.claude, python="/usr/bin/python3")
        runner.render_autosave = lambda home: self.job_text
        self.seed_plist(self.job_text)
        self.facts = {"claude": self.claude, "iterm_ok": True,
                      "handler_registered": True, "launchd_loaded": True,
                      "last_run_age": 60.0, "newest_manifest_age": 60.0,
                      "cc_status_hook": True, "settings_path": "s.json"}
        runner.setup.gather = lambda **k: dict(self.facts)
        # Which config dirs are interactive reads the machine (ps, the index).
        self.real_usage_roots = runner.usage_roots
        self.usage_dirs = ([], [])
        runner.usage_roots = lambda: self.usage_dirs

    def tearDown(self):
        runner.usage_roots = self.real_usage_roots
        runner.subprocess.run = self.real_run
        runner.setup.gather = self.real_gather
        runner.shutil.which = self.real_which
        runner.ccwho_dir = self.real_dir
        runner.ccwho_bin = self.real_bin
        runner.sys.executable = self.real_exe
        runner.handler_script = self.real_script
        runner.render_autosave = self.real_render

    def plist(self):
        return os.path.join(self.home, "Library", "LaunchAgents",
                            "com.lukaso.ccwho.save.plist")

    def seed_plist(self, body):
        os.makedirs(os.path.dirname(self.plist()), exist_ok=True)
        with open(self.plist(), "w") as fh:
            fh.write(body)

    def run_setup(self, argv=()):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.setup_cmd(list(argv))
        return rc, out.getvalue() + err.getvalue()


class TestSetupSaysWhatItDidAndDoesItOnce(SetupHarness):
    def test_on_a_finished_machine_it_writes_nothing(self):          # control
        rc, out = self.run_setup(["--yes",
                                  "--no-hotkey", "--no-list"])
        self.assertEqual(rc, 0)
        self.assertEqual(self.ran, [], "a second run is not a second install")
        self.assertIn("already", out.lower())

    def test_without_claude_it_stops_before_writing_anything(self):
        self.facts["claude"] = ""
        rc, out = self.run_setup(["--yes"])
        self.assertEqual(rc, 1)
        self.assertEqual(self.ran, [])
        self.assertIn("claude", out.lower())

    def test_it_registers_the_handler_when_it_is_missing(self):
        self.facts["handler_registered"] = False
        self.run_setup(["--yes", "--no-hotkey", "--no-list"])
        self.assertTrue(any("install-handler.sh" in " ".join(c) for c in self.ran),
                        self.ran)

    def test_a_failing_step_is_reported_not_swallowed(self):
        self.facts["handler_registered"] = False
        self.returns["/bin/bash"] = 3
        rc, out = self.run_setup(["--yes",
                                  "--no-hotkey", "--no-list"])
        self.assertEqual(rc, 1)
        self.assertIn("handler", out.lower())


class TestSetupInstallsTheAutosaveJobForThisMachine(SetupHarness):
    def test_it_renders_the_template_for_this_home(self):
        self.facts["launchd_loaded"] = False
        rc, out = self.run_setup(["--yes",
                                  "--no-hotkey", "--no-list"])
        with open(self.plist()) as fh:
            body = fh.read()
        self.assertEqual(rc, 0, out)
        self.assertNotIn("{{", body)
        self.assertIn(self.home, body)
        self.assertIn(os.path.dirname(self.claude), body, "PATH must find claude")

    def test_it_loads_the_job_it_just_wrote(self):
        self.facts["launchd_loaded"] = False
        self.run_setup(["--yes", "--no-hotkey", "--no-list"])
        calls = [" ".join(c) for c in self.ran if "launchctl" in c[0]]
        self.assertTrue(any("bootout" in c for c in calls), calls)
        self.assertTrue(any("bootstrap" in c for c in calls), calls)
        self.assertLess([i for i, c in enumerate(calls) if "bootout" in c][0],
                        [i for i, c in enumerate(calls) if "bootstrap" in c][0],
                        "bootstrap over a loaded job fails; replace it")

    def test_a_plist_with_a_problem_is_never_loaded(self):
        self.facts["launchd_loaded"] = False
        self.seed_plist("<plist>the one that works</plist>")
        real = runner.setup.plist_problems
        runner.setup.plist_problems = lambda *a, **k: ["path belongs to someone else"]
        try:
            rc, out = self.run_setup(["--yes",
                                      "--no-hotkey", "--no-list"])
        finally:
            runner.setup.plist_problems = real
        self.assertEqual(rc, 1)
        self.assertEqual([c for c in self.ran if "launchctl" in c[0]], [])
        with open(self.plist()) as fh:
            self.assertIn("the one that works", fh.read(),
                          "and what was installed is untouched")


class TestTheHotkeyIsWrittenAndProved(SetupHarness):
    def profile(self):
        return runner.setup.profile_path(self.home)

    def read_profile(self):
        with open(self.profile()) as fh:
            return json.load(fh)["Profiles"][0]

    def test_it_writes_the_profile_with_the_plain_command_when_not_proving(self):
        self.run_setup(["--yes", "--no-list"])
        p = self.read_profile()
        self.assertTrue(p["Has Hotkey"])
        self.assertNotIn("--proof", p["Command"], "no nonce left in the profile")

    def test_proving_runs_the_nonce_command_and_then_cleans_it_out(self):
        nonces = []

        def prove(ccwho_dir, nonce, **k):
            nonces.append(nonce)
            return True

        real = runner.setup.wait_for_proof
        runner.setup.wait_for_proof = prove
        try:
            rc, out = self.run_setup(["--no-list"])
        finally:
            runner.setup.wait_for_proof = real
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(nonces), 1)
        self.assertNotIn(nonces[0], json.dumps(self.read_profile()),
                         "the proof command must not stay as the hotkey's job")
        self.assertIn("⌥/", out)

    def test_a_key_that_never_answers_puts_the_machine_back(self):
        os.makedirs(os.path.dirname(self.profile()), exist_ok=True)
        with open(self.profile(), "w") as fh:
            fh.write('{"Profiles": [{"Guid": "mine", "Name": "kept"}]}')
        real = runner.setup.wait_for_proof
        runner.setup.wait_for_proof = lambda *a, **k: False
        try:
            rc, out = self.run_setup(["--no-list"])
        finally:
            runner.setup.wait_for_proof = real
        with open(self.profile()) as fh:
            self.assertIn("kept", fh.read(), "what was there before is back")
        self.assertIn("did not", out.lower())
        self.assertIn("--hotkey", out, "and it says how to pick another key")

    def test_iterm2_not_running_installs_the_profile_without_proving_it(self):
        self.facts["iterm_ok"] = False
        rc, out = self.run_setup(["--no-list"])
        self.assertEqual(rc, 0, out)
        self.assertTrue(os.path.exists(self.profile()),
                        "the profile loads when iTerm2 next starts")
        self.assertIn("start iterm2", out.lower())

    def test_another_key_can_be_asked_for(self):
        self.run_setup(["--yes", "--no-list",
                        "--hotkey", "option-w"])
        self.assertEqual(self.read_profile()["HotKey Key Code"],
                         runner.setup.HOTKEYS["option-w"]["code"])

    def test_a_key_nobody_has_heard_of_is_refused_with_the_list(self):
        rc, out = self.run_setup(["--yes", "--no-list",
                                  "--hotkey", "option-banana"])
        self.assertEqual(rc, 2)
        self.assertIn("option-slash", out)

    def test_it_says_what_the_key_used_to_type(self):
        _, out = self.run_setup(["--yes", "--no-list"])
        self.assertIn("÷", out)


class TestTheHotkeyWindowProvesItself(SetupHarness):
    """The hotkey window's first job is to record the nonce setup is waiting
    for; then it becomes the list, so the first press is already useful."""

    def test_the_proof_run_records_the_nonce_and_opens_the_list(self):
        opened = []
        real = runner.run_ui
        runner.run_ui = lambda: opened.append(True) or 0
        try:
            rc, _ = self.run_setup(["--proof", "n0m1"])
        finally:
            runner.run_ui = real
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(runner.setup.proof_path(self.dir, "n0m1")))
        self.assertEqual(opened, [True])

    def test_it_ends_in_the_live_list(self):
        opened = []
        real = runner.run_ui
        runner.run_ui = lambda: opened.append(True) or 0
        try:
            self.run_setup(["--yes", "--no-hotkey"])
        finally:
            runner.run_ui = real
        self.assertEqual(opened, [True], "setup ends where the user needs to be")


class TestSetupIsReachableFromTheCommandLine(SetupHarness):
    def test_the_verb_dispatches(self):
        called = []
        real = runner.setup_cmd
        runner.setup_cmd = lambda argv: called.append(argv) or 0
        try:
            runner.main(["setup", "--yes"])
        finally:
            runner.setup_cmd = real
        self.assertEqual(called, [["--yes"]])

    def test_help_mentions_it(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.main(["--help"])
        self.assertIn("ccwho setup", out.getvalue())


class TestSetupDoesNotPesterOrHalfWrite(SetupHarness):
    """Everything a second run, a missing uv, or a crashed write would do."""

    def profile(self):
        return runner.setup.profile_path(self.home)

    def test_an_installed_hotkey_is_not_asked_for_again(self):
        asked = []
        real = runner.setup.wait_for_proof
        runner.setup.wait_for_proof = lambda *a, **k: asked.append(1) or True
        try:
            self.run_setup(["--yes", "--no-list"])   # install
            rc, out = self.run_setup(["--no-list"])  # again
        finally:
            runner.setup.wait_for_proof = real
        self.assertEqual(asked, [], "re-running setup must not grab the key again")
        self.assertEqual(rc, 0, out)

    def test_asking_for_a_different_key_does_replace_it(self):
        asked = []
        real = runner.setup.wait_for_proof
        runner.setup.wait_for_proof = lambda *a, **k: asked.append(1) or True
        try:
            self.run_setup(["--yes", "--no-list"])
            self.run_setup(["--no-list",
                            "--hotkey", "option-w"])
        finally:
            runner.setup.wait_for_proof = real
        self.assertEqual(len(asked), 1, "a new key is proved like any other")
        with open(self.profile()) as fh:
            self.assertEqual(json.load(fh)["Profiles"][0]["HotKey Key Code"],
                             runner.setup.HOTKEYS["option-w"]["code"])

    def test_without_uv_setup_still_finishes_and_says_what_is_missing(self):
        runner.shutil.which = lambda n: "" if n == "uv" else "/bin/" + n
        rc, out = self.run_setup(["--yes", "--no-hotkey"])
        self.assertEqual(rc, 0, "the install worked; only the list cannot open")
        self.assertIn("brew install uv", out)

    def test_a_crash_mid_write_never_leaves_half_a_profile(self):
        os.makedirs(os.path.dirname(self.profile()), exist_ok=True)
        with open(self.profile(), "w") as fh:
            fh.write('{"Profiles": [{"Guid": "mine"}]}')
        real = runner.write_atomic

        def explode(*a, **k):
            raise OSError("no space left on device")

        runner.write_atomic = explode
        try:
            with self.assertRaises(OSError):
                self.run_setup(["--yes", "--no-list"])
        finally:
            runner.write_atomic = real
        with open(self.profile()) as fh:
            self.assertIn("mine", fh.read(),
                          "the old profile survives a failed write")

    def test_a_proof_with_no_nonce_is_refused_not_a_traceback(self):
        rc, out = self.run_setup(["--proof"])
        self.assertEqual(rc, 2)
        self.assertIn("nonce", out.lower())


class TestSetupLeavesNothingWorseThanItFoundIt(SetupHarness):
    """Every way an install can fail half way through. The rule is the one the
    old plist taught: never report success for something that does nothing, and
    never take away what was working."""

    def profile(self):
        return runner.setup.profile_path(self.home)

    def test_a_refused_bootstrap_puts_the_working_job_back(self):
        self.facts["launchd_loaded"] = False
        self.seed_plist("<plist>the one that works</plist>")
        self.returns["launchctl"] = 1
        rc, out = self.run_setup(["--yes", "--no-hotkey", "--no-list"])
        self.assertEqual(rc, 1)
        with open(self.plist()) as fh:
            self.assertIn("the one that works", fh.read(),
                          "the plist that was loaded is back on disk")
        loads = [c for c in self.ran if c[0] == "launchctl" and "bootstrap" in c]
        self.assertGreaterEqual(len(loads), 2, "and it is loaded again")

    def test_a_loaded_job_that_runs_a_ccwho_that_moved_is_replaced(self):
        # launchctl says loaded; the plist on disk runs a copy that is not here.
        self.seed_plist("""<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict><key>Label</key><string>com.lukaso.ccwho.save</string>
<key>ProgramArguments</key><array><string>/usr/bin/python3</string>
<string>/Users/gone/ccwho/ccwho.py</string><string>save</string></array></dict></plist>""")
        rc, out = self.run_setup(["--yes", "--no-hotkey", "--no-list"])
        self.assertEqual(rc, 0, out)
        self.assertIn("loaded, but", out)
        with open(self.plist()) as fh:
            self.assertNotIn("/Users/gone", fh.read())

    def test_an_applet_calling_another_copy_is_rebuilt(self):
        # registered, claims ccwho://, and calls a clone that was deleted
        runner.handler_script = lambda: (
            'do shell script quoted form of "/Users/sam/old/ccwho" & " url "')
        self.run_setup(["--yes", "--no-hotkey", "--no-list"])
        self.assertTrue(any("install-handler.sh" in " ".join(c) for c in self.ran),
                        self.ran)

    def test_other_profiles_in_the_file_are_left_alone(self):
        os.makedirs(os.path.dirname(self.profile()), exist_ok=True)
        with open(self.profile(), "w") as fh:
            json.dump({"Profiles": [{"Guid": "someone-elses", "Name": "mine"},
                                    {"Guid": runner.setup.PROFILE_GUID,
                                     "Name": "old ccwho"}]}, fh)
        self.run_setup(["--yes", "--no-list"])
        with open(self.profile()) as fh:
            profiles = json.load(fh)["Profiles"]
        guids = [p["Guid"] for p in profiles]
        self.assertIn("someone-elses", guids, "we replace ours, not the file")
        self.assertEqual(guids.count(runner.setup.PROFILE_GUID), 1)
        mine = [p for p in profiles if p["Guid"] == "someone-elses"][0]
        self.assertEqual(mine["Name"], "mine")

    def test_a_key_that_never_answers_is_not_a_successful_setup(self):
        real = runner.setup.wait_for_proof
        runner.setup.wait_for_proof = lambda *a, **k: False
        try:
            rc, out = self.run_setup(["--no-list"])
        finally:
            runner.setup.wait_for_proof = real
        self.assertEqual(rc, 1, "a hotkey that was not proved is not installed")
        self.assertNotIn("nothing was changed", out.lower(),
                         "other steps in this run did change things")

    def test_an_unwritable_launch_agents_directory_is_one_line(self):
        self.facts["launchd_loaded"] = False
        real = runner.os.makedirs
        runner.os.makedirs = lambda *a, **k: (_ for _ in ()).throw(
            PermissionError("read-only file system"))
        try:
            rc, out = self.run_setup(["--yes", "--no-hotkey", "--no-list"])
        finally:
            runner.os.makedirs = real
        self.assertEqual(rc, 1)
        self.assertIn("ccwho setup:", out)
        self.assertNotIn("Traceback", out)


class TestTheJobRunsAnInterpreterThatWillStillBeThere(SetupHarness):
    """launchd runs this job for years without anyone watching. A pyenv or
    homebrew python can be uninstalled by an unrelated command, and the job then
    fails silently forever - so the job names the one interpreter macOS always
    has, as long as it can run ccwho."""

    def test_it_names_the_system_python(self):
        runner.sys.executable = "/Users/x/.pyenv/versions/3.13.5/bin/python3"
        self.facts["launchd_loaded"] = False
        self.seed_plist("<plist>old</plist>")
        self.run_setup(["--yes", "--no-hotkey", "--no-list"])
        with open(self.plist()) as fh:
            body = fh.read()
        self.assertIn("/usr/bin/python3", body)
        self.assertNotIn(".pyenv", body)

    def test_without_one_it_falls_back_to_the_python_running_now(self):
        real = runner.os.path.exists
        runner.os.path.exists = lambda p: False if p == runner.SYSTEM_PYTHON \
            else real(p)
        runner.sys.executable = "/opt/homebrew/bin/python3"
        try:
            self.assertEqual(runner.job_python(), "/opt/homebrew/bin/python3")
        finally:
            runner.os.path.exists = real

    def test_the_system_python_is_preferred_when_it_is_there(self):   # control
        self.assertEqual(runner.job_python(), runner.SYSTEM_PYTHON)


class TestSetupInstallsForTheUserRunningIt(SetupHarness):
    def test_there_is_no_flag_that_installs_into_another_home(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.main(["--help"])
        self.assertNotIn("--home", out.getvalue())

    def test_a_flag_that_aims_setup_at_another_home_is_refused(self):
        rc, out = self.run_setup(["--yes", "--no-hotkey", "--no-list",
                                  "--home", "/Users/someone-else"])
        self.assertEqual(rc, 2, "an option setup does not know is never guessed at")
        self.assertIn("--home", out)
        self.assertEqual(self.ran, [])
        self.assertFalse(os.path.exists(os.path.join(
            "/Users/someone-else", "Library", "LaunchAgents")))

    def test_the_flags_it_does_know_still_work(self):                  # control
        rc, _ = self.run_setup(["--yes", "--no-hotkey", "--no-list"])
        self.assertEqual(rc, 0)

    def test_the_home_it_uses_is_the_one_this_account_has(self):
        del os.environ[runner.TEST_HOME_VAR]
        try:
            self.assertEqual(runner.setup_home(), os.path.expanduser("~"))
        finally:
            os.environ[runner.TEST_HOME_VAR] = self.home


class TestTheListStartsFromAWindowWithNoPath(unittest.TestCase):
    """A hotkey window inherits iTerm2's environment, and iTerm2 was started by
    the Dock: PATH is /usr/bin:/bin:/usr/sbin:/sbin and nothing else. uv lives in
    none of those. The window then ran ccwho, ccwho said "install uv", and the
    window closed before anyone could read it - iTerm2 reports only "a session
    ended very soon after starting"."""

    def setUp(self):
        self.real_which = runner.shutil.which
        self.real_exists = runner.os.path.exists
        self.addCleanup(setattr, runner.shutil, "which", self.real_which)
        self.addCleanup(setattr, runner.os.path, "exists", self.real_exists)

    def only(self, *paths):
        runner.shutil.which = lambda n: ""
        runner.os.path.exists = lambda p: p in paths

    def test_uv_on_path_is_used_as_it_always_was(self):               # control
        runner.shutil.which = lambda n: "/opt/homebrew/bin/uv" if n == "uv" else ""
        self.assertEqual(runner.find_uv(), "/opt/homebrew/bin/uv")

    def test_homebrew_is_found_with_no_path_at_all(self):
        self.only("/opt/homebrew/bin/uv")
        self.assertEqual(runner.find_uv(), "/opt/homebrew/bin/uv")

    def test_every_place_uv_installs_itself_is_looked_at(self):
        for p in ("/usr/local/bin/uv",
                  os.path.expanduser("~/.local/bin/uv"),
                  os.path.expanduser("~/.cargo/bin/uv")):
            self.only(p)
            self.assertEqual(runner.find_uv(), p)

    def test_no_uv_anywhere_is_still_nothing(self):
        self.only()
        self.assertEqual(runner.find_uv(), "")


class TestAHotkeyWindowNeverDiesWithTheReasonUnread(unittest.TestCase):
    def setUp(self):
        self.real_ui = runner.run_ui
        self.addCleanup(setattr, runner, "run_ui", self.real_ui)
        self.waited = []
        self.real_wait = runner.wait_for_a_key
        runner.wait_for_a_key = lambda: self.waited.append(True)
        self.addCleanup(setattr, runner, "wait_for_a_key", self.real_wait)

    def run_hotkey(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = runner.main(["hotkey"])
        return rc, out.getvalue()

    def test_a_list_that_cannot_start_holds_the_window_open(self):
        runner.run_ui = lambda: 1
        rc, out = self.run_hotkey()
        self.assertEqual(rc, 1)
        self.assertEqual(self.waited, [True],
                         "otherwise the window closes and iTerm2 says only that"
                         " a session ended very soon after starting")

    def test_a_list_that_runs_does_not_hold_anything_open(self):      # control
        runner.run_ui = lambda: 0
        rc, _ = self.run_hotkey()
        self.assertEqual(rc, 0)
        self.assertEqual(self.waited, [])

    def test_the_profile_that_gets_installed_runs_that_verb(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        runner.install_hotkey(home, runner.setup.DEFAULT_HOTKEY, prove=False)
        with open(runner.setup.profile_path(home)) as fh:
            command = json.load(fh)["Profiles"][0]["Command"]
        self.assertTrue(command.endswith(" hotkey"), command)


class TestSetupAsksForTheHotkeyPermission(SetupHarness):
    """"The ask on install, as other apps do." macOS raises that dialog when a
    process without the permission tries to use it - and a process inside
    iTerm2 raises it AS iTerm2, which is the app that needs it."""

    def setUp(self):
        super().setUp()
        self.asked = []
        self.real_ask = runner.setup.ask_for_accessibility
        runner.setup.ask_for_accessibility = lambda *a, **k: (
            self.asked.append(1) or self.answer)
        self.answer = True
        self.addCleanup(setattr, runner.setup, "ask_for_accessibility",
                        self.real_ask)
        self.facts.update({"accessibility": True, "iterm_granted_at": 1000.0,
                           "iterm_started_at": 2000.0})

    def test_it_asks_when_the_permission_is_missing(self):
        self.facts["accessibility"] = False
        rc, out = self.run_setup(["--yes", "--no-hotkey", "--no-list"])
        self.assertEqual(self.asked, [1])
        self.assertIn("accessibility", out.lower())

    def test_it_does_not_ask_when_there_is_nothing_to_ask_for(self):  # control
        self.run_setup(["--yes", "--no-hotkey", "--no-list"])
        self.assertEqual(self.asked, [])

    def test_a_refused_permission_is_said_plainly_and_not_fatal(self):
        self.facts["accessibility"] = False
        self.answer = False
        rc, out = self.run_setup(["--yes", "--no-hotkey", "--no-list"])
        self.assertEqual(rc, 0, "everything else was installed")
        self.assertIn("system settings", out.lower())

    def test_a_restart_is_asked_for_but_never_done(self):
        self.facts["iterm_granted_at"] = 3000.0      # granted after it started
        rc, out = self.run_setup(["--yes", "--no-hotkey", "--no-list"])
        self.assertIn("restart iterm2", out.lower())
        self.assertEqual([c for c in self.ran if "iTerm" in " ".join(c)], [],
                         "ccwho never quits iTerm2: it would end every session")


class TestTheUiCanReopenTheLastSave(unittest.TestCase):
    """The list has always offered "o = restore the last save" with no key
    behind it. The key calls these, so they have to exist and to be the same
    restore the command line runs - not a second implementation of it."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.dir = os.path.join(self.home, ".ccwho")
        os.makedirs(os.path.join(self.dir, "restore"))
        self.real_dir = runner.ccwho_dir
        runner.ccwho_dir = lambda: self.dir
        self.addCleanup(setattr, runner, "ccwho_dir", self.real_dir)

    def write(self, name, sessions):
        with open(os.path.join(self.dir, "restore", name), "w") as fh:
            json.dump({"saved": name, "sessions": sessions}, fh)

    def test_nothing_saved_is_an_empty_manifest(self):
        self.assertEqual(runner.newest_manifest(), {})

    def test_the_newest_is_the_one_it_reads(self):
        self.write("2026-09-22T1000.json", [{"sessionId": "a"}])
        self.write("2026-09-22T1200.json", [{"sessionId": "b"},
                                            {"sessionId": "c"}])
        self.assertEqual(len(runner.newest_manifest()["sessions"]), 2)

    def test_a_broken_manifest_is_not_a_crash(self):
        with open(os.path.join(self.dir, "restore", "2026-09-22T1300.json"),
                  "w") as fh:
            fh.write("{not json")
        self.assertEqual(runner.newest_manifest(), {})

    def test_reopening_runs_the_same_restore_the_command_line_does(self):
        seen = []
        real = runner.restore
        runner.restore = lambda argv: seen.append(argv) or 0
        try:
            said = runner.reopen_saved()
        finally:
            runner.restore = real
        self.assertEqual(seen, [["--open"]])
        self.assertIn("reopen", said.lower())

    def test_it_counts_the_windows_opened_not_the_ones_it_would_not_open(self):
        # the count used to be every line containing "reopen" - which is also
        # what a session it refused to reopen prints
        def fake(argv):
            for p in ("app", "app", "football"):
                print(f"ccwho restore: not reopening {p} - cwd is gone: /x", file=sys.stderr)
            print("opening 13 iTerm2 window(s)...")
            print("opened 13 window(s). each is at its project, resuming its own session.")
            return 0
        real = runner.restore
        runner.restore = fake
        try:
            said = runner.reopen_saved()
        finally:
            runner.restore = real
        self.assertIn("13", said)
        self.assertIn("3", said.replace("13", ""), "the ones left out are named as a number")
        self.assertNotIn("reopened 3 ", said)

    def test_a_filled_pane_counts_as_a_reopened_session(self):
        def fake(argv):
            print("filled 2 pane(s) iTerm2 restored.")
            print("opened 3 window(s). each is at its project, resuming its own session.")
            return 0
        real = runner.restore
        runner.restore = fake
        try:
            said = runner.reopen_saved()
        finally:
            runner.restore = real
        self.assertIn("reopened 5 session(s)", said)

    def test_only_filled_panes_still_count(self):
        real = runner.restore
        runner.restore = lambda argv: print("filled 2 pane(s) iTerm2 restored.") or 0
        try:
            said = runner.reopen_saved()
        finally:
            runner.restore = real
        self.assertIn("reopened 2 session(s)", said)

    def test_it_says_when_the_restore_refused(self):
        real = runner.restore
        runner.restore = lambda argv: 1
        try:
            said = runner.reopen_saved()
        finally:
            runner.restore = real
        self.assertIn("could not", said.lower())

    def test_a_save_that_is_all_running_says_so_not_reopened(self):
        def fake(argv):
            print("already open: app - focus it with `ccwho open x`")
            print("all 3 session(s) in that manifest are already running or starting.")
            return 0
        real = runner.restore
        runner.restore = fake
        try:
            said = runner.reopen_saved()
        finally:
            runner.restore = real
        self.assertIn("already running", said)
        self.assertNotIn("reopened", said)

    def test_a_file_that_is_not_a_save_is_never_opened(self):
        self.write("2026-09-22T1000.json", [{"sessionId": "a"}])
        self.write("notes.json", [{"sessionId": "b"}])
        import builtins
        opened = []
        # a module global `open` shadows the builtin for ccwho.py only
        runner.open = lambda path, *a, **k: (opened.append(os.path.basename(path))
                                             or builtins.open(path, *a, **k))
        try:
            points = self.points()
        finally:
            del runner.open
        self.assertNotIn("notes.json", opened)
        self.assertIn("2026-09-22T1000.json", opened)                   # control
        self.assertEqual(len(points), 1)

    def test_a_chosen_save_is_not_called_the_last_save(self):
        real = runner.restore
        runner.restore = lambda argv: 0
        try:
            said = runner.reopen_saved("/x/2026-09-22T1000.json")
            plain = runner.reopen_saved()
        finally:
            runner.restore = real
        self.assertNotIn("last save", said)
        self.assertIn("last save", plain)                                # control

    def test_starting_is_not_called_running(self):
        def fake(argv):
            print("all 3 session(s) in that manifest are already running or starting.")
            return 0
        real = runner.restore
        runner.restore = fake
        try:
            said = runner.reopen_saved()
        finally:
            runner.restore = real
        self.assertIn("already running or starting", said)

    def test_a_chosen_save_is_the_one_restored(self):
        seen = []
        real = runner.restore
        runner.restore = lambda argv: seen.append(argv) or 0
        try:
            runner.reopen_saved("/x/2026-09-22T1000.json")
        finally:
            runner.restore = real
        self.assertEqual(seen, [["--open", "--from", "/x/2026-09-22T1000.json"]])

    def points(self, live_ids=(), booted=None):
        real = runner.booted_at
        runner.booted_at = lambda: booted
        try:
            return runner.save_points(live_ids)
        finally:
            runner.booted_at = real

    def test_the_menu_lists_every_save_newest_first_with_its_path(self):
        self.write("2026-09-22T1000.json", [{"sessionId": "a"}])
        self.write("2026-09-22T1200.json", [{"sessionId": "b"}, {"sessionId": "c"}])
        points = self.points(live_ids={"c"})
        self.assertEqual([(p["name"], p["count"], p["running"]) for p in points],
                         [("2026-09-22T1200.json", 2, 1), ("2026-09-22T1000.json", 1, 0)])
        self.assertEqual(points[0]["path"],
                         os.path.join(self.dir, "restore", "2026-09-22T1200.json"))

    def test_the_menu_marks_the_save_before_the_restart(self):
        self.write("2026-09-22T1000.json", [{"sessionId": "a"}])
        self.write("2026-09-22T1200.json", [{"sessionId": "b"}])
        boot = int(time.mktime((2026, 9, 22, 11, 0, 0, 0, 0, -1)))
        points = self.points(booted=boot)
        self.assertEqual([p["name"] for p in points if p["before_reboot"]],
                         ["2026-09-22T1000.json"])
        self.assertTrue(all(p["booted"] == boot for p in points))

    def test_a_broken_save_is_in_the_menu_as_unreadable(self):
        with open(os.path.join(self.dir, "restore", "2026-09-22T1300.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual([p["count"] for p in self.points()], [None])

    def test_no_restore_dir_is_an_empty_menu(self):
        shutil.rmtree(os.path.join(self.dir, "restore"))
        self.assertEqual(self.points(), [])

    def test_a_restore_dir_it_cannot_read_raises_not_nothing_saved(self):
        real = runner.os.listdir
        def denied(d):
            raise PermissionError(13, "Permission denied", d)
        runner.os.listdir = denied
        try:
            with self.assertRaises(PermissionError):
                self.points()
        finally:
            runner.os.listdir = real

    def test_boot_time_comes_from_sysctl(self):
        real = runner.subprocess.run
        runner.subprocess.run = lambda *a, **k: runner.subprocess.CompletedProcess(
            a[0], 0, "{ sec = 1790423298, usec = 5 } Sat Sep 26 12:48:18 2026\n", "")
        try:
            self.assertEqual(runner.booted_at(), 1790423298)
        finally:
            runner.subprocess.run = real

    def test_no_sysctl_is_no_boot_time_not_a_crash(self):
        def boom(*a, **k):
            raise OSError("no sysctl")
        real = runner.subprocess.run
        runner.subprocess.run = boom
        try:
            self.assertIsNone(runner.booted_at())
        finally:
            runner.subprocess.run = real


class TestOpenGivesABackgroundSessionAWindow(unittest.TestCase):
    """`ccwho open` on a running background session: put it in a terminal, do
    not reopen it. Reopening a live session forks the conversation."""

    SID = "51fddd61-822b-49e0-9aeb-2145e91e1244"

    def setUp(self):
        self.ran = []
        self.real_run = runner.subprocess.run
        self.real_collect = runner.engine.collect
        runner.subprocess.run = lambda cmd, *a, **k: self.ran.append(cmd) or type(
            "D", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        runner.engine.collect = lambda cache=None, status=None: (
            status.update({"source_ok": True}) or
            ([{"sessionId": self.SID, "tty": "", "pid": 42,
               "kind": "background"}], 0))
        self.addCleanup(setattr, runner.subprocess, "run", self.real_run)
        self.addCleanup(setattr, runner.engine, "collect", self.real_collect)

    def run_open(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.open_session([self.SID])
        return rc, out.getvalue() + err.getvalue()

    def test_it_opens_a_window_that_attaches(self):
        rc, out = self.run_open()
        self.assertEqual(rc, 0, out)
        script = " ".join(" ".join(c) for c in self.ran)
        self.assertIn("claude attach", script)
        self.assertIn("create window", script)

    def test_it_never_resumes_it(self):
        self.run_open()
        script = " ".join(" ".join(c) for c in self.ran)
        self.assertNotIn("--resume", script,
                         "that would fork a conversation that is running")

    def test_it_says_what_it_did(self):
        _, out = self.run_open()
        self.assertIn("attach", out.lower())


class TestChangingTheHotkeyActuallyChangesIt(SetupHarness):
    """Measured: after `ccwho setup --hotkey option-slash`, the file on disk
    said key code 44 and iTerm2 was still firing on key code 13 - the old key.
    iTerm2 registers a hotkey when a profile APPEARS and does not re-register
    when one is edited in place, so the profile has to go away and come back.
    """

    def setUp(self):
        super().setUp()
        self.real_pause = runner.PROFILE_RELOAD_PAUSE
        runner.PROFILE_RELOAD_PAUSE = 0.0
        self.addCleanup(setattr, runner, "PROFILE_RELOAD_PAUSE", self.real_pause)
        self.events = []
        real_remove, real_write = runner.os.remove, runner.write_atomic
        runner.os.remove = lambda p: self.events.append(("remove", p)) or real_remove(p)
        runner.write_atomic = lambda p, t: self.events.append(("write", p)) or \
            real_write(p, t)
        self.addCleanup(setattr, runner.os, "remove", real_remove)
        self.addCleanup(setattr, runner, "write_atomic", real_write)

    def profile(self):
        return runner.setup.profile_path(self.home)

    def key_code(self):
        with open(self.profile()) as fh:
            return json.load(fh)["Profiles"][0]["HotKey Key Code"]

    def test_a_new_key_takes_the_profile_away_and_brings_it_back(self):
        self.run_setup(["--yes", "--no-list", "--hotkey", "option-w"])
        self.events.clear()
        self.run_setup(["--yes", "--no-list", "--hotkey", "option-slash"])
        kinds = [k for k, _ in self.events]
        self.assertIn("remove", kinds, "iTerm2 only registers a profile that appears")
        self.assertLess(kinds.index("remove"), kinds.index("write"))
        self.assertEqual(self.key_code(), runner.setup.HOTKEYS["option-slash"]["code"])

    def test_the_same_key_twice_does_not_churn(self):                 # control
        self.run_setup(["--yes", "--no-list", "--hotkey", "option-slash"])
        self.events.clear()
        self.run_setup(["--yes", "--no-list", "--hotkey", "option-slash"])
        self.assertEqual([k for k, _ in self.events], [],
                         "nothing changed: taking the hotkey away and back would"
                         " make the window disappear for no reason")

    def test_a_first_install_just_writes_it(self):                    # control
        self.run_setup(["--yes", "--no-list", "--hotkey", "option-slash"])
        self.assertEqual([k for k, _ in self.events], ["write"])



SESSION = "aaaa1111-0000-4000-8000-000000000001"
TOKEN_R = "sk-ant-FAKE-runner-must-never-print"


class TestPsLists_WhatAgentsStarted(unittest.TestCase):
    """`ccwho ps` - the plain view of every process an agent started: its session,
    its ports, its command (redacted). The same data as the list, as text or JSON."""

    def setUp(self):
        self.real = runner.engine.collect
        row = {"sessionId": SESSION, "project": "app", "title": "fix the navbar",
               "tab_title": "", "name": "app-4e"}
        fleet = {"ports_ok": True, "agent_ports": [],
                 "by_session": {SESSION: [
                     {"pid": 11, "ports": [5173], "command": "vite --port 5173",
                      "command_full": "/Users/x/app/node_modules/.bin/vite --port 5173",
                      "helper": False, "orphan": True, "harness": "claude",
                      "session": SESSION},
                     {"pid": 13, "ports": [9222], "command": "npm exec some-mcp",
                      "helper": True, "orphan": False, "harness": "claude",
                      "session": SESSION}]},
                 "left_behind": [{"pid": 20, "ports": [3000], "helper": False,
                                  "command": "next-server --token=***", "orphan": True,
                                  "harness": "claude", "session": "dddd"}],
                 "codex": [{"pid": 30, "ports": [60805], "helper": False,
                            "command": "workerd serve", "orphan": True,
                            "harness": "codex", "session": "01a0abc8"}]}
        runner.engine.collect = lambda cache=None, status=None: ([row], fleet)
        self.addCleanup(setattr, runner.engine, "collect", self.real)

    def run_ps(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.main(["ps", *args])
        return rc, out.getvalue(), err.getvalue()

    def test_every_group_is_listed_with_its_ports(self):
        rc, out, _ = self.run_ps()
        self.assertEqual(rc, 0)
        for text in ("fix the navbar", ":5173", "left behind", ":3000", "codex", ":60805"):
            self.assertIn(text, out)

    def test_helpers_only_with_all(self):
        _, out, _ = self.run_ps()
        self.assertNotIn("some-mcp", out)
        _, out_all, _ = self.run_ps("--all")
        self.assertIn("some-mcp", out_all)

    def test_a_port_filter(self):
        rc, out, _ = self.run_ps("--port", "3000")
        self.assertEqual(rc, 0)
        self.assertIn("next-server", out)
        self.assertNotIn("vite", out)

    def test_a_port_nobody_agent_holds_is_said_so(self):             # control
        rc, _, err = self.run_ps("--port", "4444")
        self.assertEqual(rc, 1)
        self.assertIn(":4444", err)

    def test_a_port_with_an_equals_sign_is_the_same_question(self):
        # review 4 #5: `--port=1` listed everything with exit 0
        self.assertEqual(self.run_ps("--port=4444")[0], 1)
        rc, out, _ = self.run_ps("--port=3000")
        self.assertEqual(rc, 0)
        self.assertNotIn("vite", out)
        self.assertEqual(self.run_ps("--port=")[0], 2)
        self.assertEqual(self.run_ps("--port=70000")[0], 2)          # review 5: no such port

    def test_a_port_with_a_colon_is_the_same_port(self):
        rc, out, _ = self.run_ps("--port", ":3000")
        self.assertEqual(rc, 0)
        self.assertIn("next-server", out)

    def test_a_bad_port_is_a_usage_error(self):
        # never a traceback, and never "everything" (adversarial F2)
        for args in (("--port",), ("--port", "abc"), ("--port", "\u00b3")):
            with self.subTest(args=args):
                rc, out, err = self.run_ps(*args)
                self.assertEqual(rc, 2)
                self.assertIn("--port", err)
                self.assertEqual(out, "")

    def test_unknown_processes_are_not_no_processes(self):
        fleet = dict(runner.engine.collect()[1], procs_ok=False, by_session={},
                     left_behind=[], codex=[])
        runner.engine.collect = lambda cache=None, status=None: ([], fleet)
        rc, out, err = self.run_ps()
        self.assertEqual(rc, 3)
        self.assertIn("unknown", err)
        self.assertNotIn("no processes", out)

    def test_a_port_question_with_ports_unknown_has_its_own_answer(self):
        # adversarial #4: exit 1 means "nobody holds it"; unknown is not that
        row, fleet = runner.engine.collect()
        runner.engine.collect = lambda cache=None, status=None: (
            row, dict(fleet, ports_ok=False))
        rc, out, err = self.run_ps("--port", "3000")
        self.assertEqual(rc, 3)
        self.assertIn("unknown", err)

    def test_ports_unknown_shows_a_question_mark(self):
        row, fleet = runner.engine.collect()
        runner.engine.collect = lambda cache=None, status=None: (
            row, dict(fleet, ports_ok=False))
        _, out, _ = self.run_ps()
        line = [l for l in out.splitlines() if l.startswith("11 ")][0]
        self.assertEqual(line.split()[1], "?")
        runner.engine.collect = lambda cache=None, status=None: (row, fleet)
        _, out, _ = self.run_ps()
        self.assertEqual([l for l in out.splitlines() if l.startswith("11 ")][0]
                         .split()[1], ":5173")

    def test_unsure_is_never_called_left_behind(self):
        row, fleet = runner.engine.collect()
        unsure = [{"pid": 50, "ports": [], "helper": False, "command": "vite",
                   "orphan": True, "harness": "claude", "session": "dddd",
                   "why": "the session list is incomplete"}]
        runner.engine.collect = lambda cache=None, status=None: (
            row, dict(fleet, unsure=unsure))
        _, out, _ = self.run_ps("--json")
        got = {p["pid"]: p["group"] for p in json.loads(out)}
        self.assertEqual(got[50], "unsure")
        self.assertEqual(got[20], "left behind")                          # control
        _, out, _ = self.run_ps()
        line = [l for l in out.splitlines() if l.startswith("50 ")][0]
        self.assertNotIn("left behind", line)

    def test_processes_unknown_has_the_unknown_exit(self):
        # cycle 3 #7: 1 means "no agent holds it"; a script starts a server on 1
        row, fleet = runner.engine.collect()
        runner.engine.collect = lambda cache=None, status=None: (
            row, dict(fleet, procs_ok=False))
        for args in ((), ("--port", "3000")):
            with self.subTest(args=args):
                self.assertEqual(self.run_ps(*args)[0], 3)

    def test_a_port_held_by_an_unreadable_process_is_unknown(self):
        row, fleet = runner.engine.collect()
        runner.engine.collect = lambda cache=None, status=None: (
            row, dict(fleet, unknown_ports=[8080]))
        rc, _, err = self.run_ps("--port", "8080")
        self.assertEqual(rc, 3)
        self.assertIn(":8080", err)
        self.assertEqual(self.run_ps("--port", "4444")[0], 1)                # control

    def test_an_unreadable_holder_is_named(self):
        # macOS hides ControlCenter's environment: who started it is not known,
        # but a person reads "AirPlay" in the name at once
        row, fleet = runner.engine.collect()
        runner.engine.collect = lambda cache=None, status=None: (
            row, dict(fleet, unknown_ports=[5000],
                      unknown_holders={5000: [{"pid": 1190, "name": "ControlCenter"}]}))
        rc, _, err = self.run_ps("--port", "5000")
        self.assertEqual(rc, 3)
        self.assertIn("ControlCenter (pid 1190)", err)

    def test_an_unreadable_holders_name_cannot_repaint_the_terminal(self):
        row, fleet = runner.engine.collect()
        runner.engine.collect = lambda cache=None, status=None: (
            row, dict(fleet, unknown_ports=[5000],
                      unknown_holders={5000: [{"pid": 7, "name": "x\x1b[2Jy"}]}))
        _, _, err = self.run_ps("--port", "5000")
        self.assertNotIn("\x1b", err)
        self.assertIn("x?[2Jy (pid 7)", err)

    def test_a_port_question_sees_helpers(self):
        # who holds :9222 - an MCP helper does, and "nobody" would be wrong
        rc, out, _ = self.run_ps("--port", "9222")
        self.assertEqual(rc, 0)
        self.assertIn("some-mcp", out)

    def test_left_behind_helpers_are_listed(self):
        # cycle 3 #10: the bottom line counts them, so ps lists them: litter is
        # litter, an MCP server whose session ended included
        row, fleet = runner.engine.collect()
        lb = fleet["left_behind"] + [{"pid": 21, "ports": [], "helper": True,
                                      "command": "npx some-mcp", "orphan": True,
                                      "harness": "claude", "session": "dddd"}]
        runner.engine.collect = lambda cache=None, status=None: (
            row, dict(fleet, left_behind=lb))
        _, out, _ = self.run_ps()
        self.assertIn("some-mcp", out)
        self.assertNotIn("some-mcp exec", out)
        self.assertNotIn("npm exec some-mcp", out)                           # control: pid 13

    def test_every_listed_process_is_one_line(self):
        _, out, _ = self.run_ps("--json")
        n = len(json.loads(out))
        _, out, _ = self.run_ps()
        self.assertEqual(len(out.splitlines()), n)

    def test_json_says_when_ports_are_unknown(self):
        row, fleet = runner.engine.collect()
        fleet = dict(fleet, ports_ok=False)
        runner.engine.collect = lambda cache=None, status=None: (row, fleet)
        _, out, _ = self.run_ps("--json")
        self.assertTrue(all(p["ports"] is None for p in json.loads(out)))

    def test_the_full_command_only_when_asked(self):
        _, out, _ = self.run_ps()
        self.assertNotIn("node_modules", out)
        _, out, _ = self.run_ps("--full")
        self.assertIn("node_modules/.bin/vite", out)

    def test_json_has_the_full_command_only_when_asked(self):
        _, out, _ = self.run_ps("--json")
        self.assertNotIn("node_modules", out)
        _, out, _ = self.run_ps("--json", "--full")
        self.assertIn("node_modules/.bin/vite", out)

    def test_json_for_scripts_and_agents(self):
        rc, out, _ = self.run_ps("--json")
        procs = json.loads(out)
        self.assertEqual(sorted(p["pid"] for p in procs), [11, 20, 30])
        self.assertEqual({p["group"] for p in procs}, {"session", "left behind", "codex"})


class TestReapNeverPrintsASecret(unittest.TestCase):
    """reap prints the commands it would kill; a command line can carry a token
    (a node process can spill its environment into it). eng D5."""

    def test_the_token_is_masked(self):
        real = runner.engine.ps_snapshot_elapsed
        runner.engine.ps_snapshot_elapsed = lambda: (
            f"  4242     1 2-00:00:00 node leak.js --token={TOKEN_R}\n")
        self.addCleanup(setattr, runner.engine, "ps_snapshot_elapsed", real)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.main(["reap", "leak.js", "--older-than", "1h"])
        self.assertIn("leak.js", out.getvalue())
        self.assertNotIn(TOKEN_R, out.getvalue())

    def test_a_token_with_no_known_shape_is_not_printed(self):
        real = runner.engine.ps_snapshot_elapsed
        runner.engine.ps_snapshot_elapsed = lambda: (
            "  4242     1 2-00:00:00 node leak.js k8Hq2vX9pLm3nR7tW1yZ4bC6dF0gJ5sA\n")
        self.addCleanup(setattr, runner.engine, "ps_snapshot_elapsed", real)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.main(["reap", "leak.js", "--older-than", "1h"])
        self.assertIn("leak.js", out.getvalue())                            # control
        self.assertNotIn("k8Hq2vX9pLm3nR7tW1yZ4bC6dF0gJ5sA", out.getvalue())


class TestStatusline(unittest.TestCase):
    """`ccwho statusline` runs inside every session, on every status update. It
    records what it is handed, prints nothing, and never fails the session's
    status bar - whatever arrives on stdin."""

    TOKEN = "sk-ant-oat01-FAKE-runner-0000-never-out"
    SID = "2f25ae12-9c12-4461-adf5-6000f566876d"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        with open(os.path.join(self.home, ".claude.json"), "w") as fh:
            json.dump({"oauthAccount": {"emailAddress": "a@example.com",
                                        "accountUuid": "uuid-a"}}, fh)
        self.saved = {k: os.environ.get(k) for k in
                      ("CCWHO_DIR", runner.TEST_HOME_VAR, "CLAUDE_CODE_OAUTH_TOKEN",
                       "CLAUDE_CONFIG_DIR")}
        os.environ["CCWHO_DIR"] = os.path.join(self.tmp, "ccwho")
        os.environ[runner.TEST_HOME_VAR] = self.home
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_with(self, stdin):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = runner.main(["statusline"])
        finally:
            sys.stdin = old
        return rc, out.getvalue(), err.getvalue()

    def payload(self, **extra):
        # resets relative to the clock: a fixed epoch expires and the window
        # then reads "expired", which is right, and not what these tests are about
        now = int(time.time())
        p = {"session_id": self.SID, "transcript_path": None,
             "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": now + 3600},
                             "seven_day": {"used_percentage": 67, "resets_at": now + 3 * 86400}}}
        p.update(extra)
        return json.dumps(p)

    def usage_file(self):
        return os.path.join(self.tmp, "ccwho", "usage", self.SID + ".json")

    def test_a_reading_is_written_and_its_entry_printed(self):
        # D10 (design review 2026-09-25): the session's own entry in its status bar
        rc, out, err = self.run_with(self.payload())
        self.assertEqual((rc, err), (0, ""))
        plain = runner.usage.ANSI_RE.sub("", out)
        self.assertTrue(plain.startswith("a 5h 5%"), plain)
        self.assertIn("7d 67%", plain)
        self.assertNotIn("\n\n", out)
        with open(self.usage_file()) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["rate_limits"]["seven_day"]["used_percentage"], 67)
        self.assertEqual(rec["account"]["id"], "login:uuid-a")

    def test_junk_on_stdin_is_exit_zero_silent_and_writes_nothing(self):
        for junk in ("", "{nope", "[]", "null", json.dumps({"session_id": "../../x"})):
            rc, out, err = self.run_with(junk)
            self.assertEqual((rc, out, err), (0, "", ""), junk)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ccwho", "usage")))

    def test_the_token_is_in_no_file_and_no_output(self):
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = self.TOKEN
        rc, out, err = self.run_with(self.payload())
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(self.usage_file()))                  # control
        for root, _, files in os.walk(self.tmp):
            for name in files:
                with open(os.path.join(root, name), errors="replace") as fh:
                    self.assertNotIn(self.TOKEN, fh.read(), name)
        self.assertNotIn(self.TOKEN, out + err)

    def test_a_second_reading_keeps_the_first_account(self):
        self.run_with(self.payload())
        with open(os.path.join(self.home, ".claude.json"), "w") as fh:
            json.dump({"oauthAccount": {"emailAddress": "b@example.com",
                                        "accountUuid": "uuid-b"}}, fh)
        self.run_with(self.payload())
        with open(self.usage_file()) as fh:
            rec = json.load(fh)
        self.assertTrue(rec["unsure"])
        self.assertEqual(rec["first_account"]["id"], "login:uuid-a")

    # #28: Claude Code gives the statusLine no CLAUDE_CODE_OAUTH_TOKEN; the claude
    # that runs it - its parent, named by CLAUDE_PID - still has it
    def claude_parent(self, env, pid=None):
        """Make this process's parent a claude whose environment is `env`."""
        import ccwho_procs
        import struct
        pid = os.getppid() if pid is None else pid
        buf = struct.pack("i", 1) + b"/usr/local/bin/claude\0\0\0claude\0" + \
            b"".join(e.encode() + b"\0" for e in env) + b"\0"
        asked = []
        real = runner.engine.procargs_buffer
        runner.engine.procargs_buffer = lambda p: asked.append(p) or buf
        self.addCleanup(setattr, runner.engine, "procargs_buffer", real)
        old = os.environ.get("CLAUDE_PID")
        os.environ["CLAUDE_PID"] = str(pid)
        self.addCleanup(lambda: os.environ.pop("CLAUDE_PID", None) if old is None
                        else os.environ.__setitem__("CLAUDE_PID", old))
        return asked, ccwho_procs

    def test_the_parent_claudes_token_names_the_account_and_is_kept_nowhere(self):
        import hashlib
        asked, _ = self.claude_parent([f"CLAUDE_CODE_OAUTH_TOKEN={self.TOKEN}", "HOME=/x"])
        rc, out, err = self.run_with(self.payload())
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(asked, [os.getppid()])
        with open(self.usage_file()) as fh:
            rec = json.load(fh)
        fp = hashlib.sha256(self.TOKEN.encode()).hexdigest()[:16]
        self.assertEqual(rec["account"], {"kind": "token", "id": "token:" + fp})
        self.assertEqual(rec["claude_pid"], os.getppid())
        for root, _, files in os.walk(self.tmp):
            for name in files:
                with open(os.path.join(root, name), errors="replace") as fh:
                    self.assertNotIn(self.TOKEN, fh.read(), name)
        self.assertNotIn(self.TOKEN, out + err)

    def test_a_claude_pid_that_is_not_the_parent_is_not_read(self):          # control
        asked, _ = self.claude_parent([f"CLAUDE_CODE_OAUTH_TOKEN={self.TOKEN}"],
                                      pid=os.getppid() + 100000)
        self.run_with(self.payload())
        self.assertEqual(asked, [])
        with open(self.usage_file()) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["account"]["id"], "login:uuid-a")
        self.assertIsNone(rec["claude_pid"])

    def test_a_claude_pid_that_is_not_a_pid_is_not_read(self):
        for junk in ("", "abc", "1", "0", "-5", "٣", " 12", "9" * 12):
            asked, _ = self.claude_parent([f"CLAUDE_CODE_OAUTH_TOKEN={self.TOKEN}"])
            os.environ["CLAUDE_PID"] = junk
            self.run_with(self.payload())
            self.assertEqual(asked, [], repr(junk))

    def test_a_parent_whose_environment_cannot_be_read_falls_back_to_the_login(self):
        asked, _ = self.claude_parent([])
        runner.engine.procargs_buffer = lambda p: asked.append(p) or None
        rc, out, err = self.run_with(self.payload())
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(asked, [os.getppid()])                             # control
        with open(self.usage_file()) as fh:
            self.assertEqual(json.load(fh)["account"]["id"], "login:uuid-a")

    def test_a_parent_that_cannot_be_read_again_keeps_the_sessions_account(self):
        import hashlib
        fp = hashlib.sha256(self.TOKEN.encode()).hexdigest()[:16]
        tok = {"kind": "token", "id": "token:" + fp}
        os.makedirs(os.path.dirname(self.usage_file()))
        with open(self.usage_file(), "w") as fh:
            json.dump({"v": 1, "session_id": self.SID, "received_at": time.time() - 60,
                       "measured_at": None, "rate_limits": {}, "account": tok,
                       "first_account": tok, "first_seen": time.time() - 600,
                       "login_at": None, "unsure": False, "claude_pid": os.getppid()}, fh)
        asked, _ = self.claude_parent([])
        runner.engine.procargs_buffer = lambda p: asked.append(p) or None
        rc, out, err = self.run_with(self.payload())
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(asked, [os.getppid()])
        with open(self.usage_file()) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["account"], {"kind": "unknown", "id": None})
        self.assertEqual(rec["first_account"], tok)
        self.assertTrue(rec["unsure"])              # a gap joins no account
        self.assertEqual(rec["claude_pid"], os.getppid())

    def test_a_parent_read_that_raises_still_records_and_exits_zero(self):
        asked, _ = self.claude_parent([])

        def boom(p):
            raise RuntimeError("sysctl went away")
        runner.engine.procargs_buffer = boom
        rc, out, err = self.run_with(self.payload())
        self.assertEqual((rc, err), (0, ""))
        with open(self.usage_file()) as fh:
            self.assertEqual(json.load(fh)["account"]["id"], "login:uuid-a")

    def test_a_write_that_fails_still_prints_and_exits_zero(self):
        os.makedirs(os.path.join(self.tmp, "ccwho"))
        with open(os.path.join(self.tmp, "ccwho", "usage"), "w") as fh:
            fh.write("a file where the directory should be")
        rc, out, err = self.run_with(self.payload())
        self.assertEqual((rc, err), (0, ""))
        self.assertIn("5h 5%", runner.usage.ANSI_RE.sub("", out))

    def test_before_the_first_reply_nothing_is_printed(self):
        p = json.loads(self.payload())
        del p["rate_limits"]
        self.assertEqual(self.run_with(json.dumps(p)), (0, "", ""))

    def test_accounts_lists_what_was_recorded(self):
        self.run_with(self.payload())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = runner.main(["accounts"])
        self.assertEqual(rc, 0)
        self.assertIn("a@example.com", out.getvalue())
        self.assertIn("7d", out.getvalue())

    def test_accounts_json(self):
        self.run_with(self.payload())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.main(["accounts", "--json"])
        rows = json.loads(out.getvalue())
        self.assertEqual(rows[0]["id"], "login:uuid-a")

    def test_accounts_with_nothing_recorded_says_how_it_gets_data(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = runner.main(["accounts"])
        self.assertEqual(rc, 0)
        self.assertIn("statusline", out.getvalue())

    def test_accounts_name_sets_a_label(self):
        self.run_with(self.payload())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(runner.main(["accounts", "name", "login:uuid", "work"]), 0)
            runner.main(["accounts"])
        self.assertIn("work", out.getvalue())

    def test_accounts_name_with_an_unknown_id_fails(self):
        self.run_with(self.payload())
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(["accounts", "name", "nope", "work"]), 2)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "ccwho", "accounts.json")))

    def test_housekeeping_prunes_old_readings(self):
        self.run_with(self.payload())
        old = self.usage_file()
        stamp = time.time() - 9 * 86400
        os.utime(old, (stamp, stamp))
        runner.prune_usage()
        self.assertFalse(os.path.exists(old))


class TestUsageSetup(unittest.TestCase):
    """The first time ccwho writes another tool's file. Every guard is here:
    only where no statusLine is set, the diff shown, y/N asked, a backup kept,
    nothing written if the file moved under us, and a no remembered."""

    OURS = "/opt/homebrew/bin/ccwho statusline"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("CCWHO_DIR")
        os.environ["CCWHO_DIR"] = os.path.join(self.tmp, "ccwho")
        self.root = os.path.join(self.tmp, ".claude")
        os.makedirs(self.root)
        self.settings = os.path.join(self.root, "settings.json")
        self.asked = []

    def tearDown(self):
        if self.old is None:
            os.environ.pop("CCWHO_DIR", None)
        else:
            os.environ["CCWHO_DIR"] = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, obj):
        with open(self.settings, "w") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj, indent=2))

    def read(self):
        with open(self.settings) as fh:
            return json.load(fh)

    def answer(self, reply):
        def ask(prompt):
            self.asked.append(prompt)
            return reply
        return ask

    def run_setup(self, reply="y", yes=False, mode="", roots=None, skipped=()):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = runner.usage_setup(roots if roots is not None else [self.root],
                                    list(skipped), yes=yes, mode=mode,
                                    ask=self.answer(reply), ccwho="/opt/homebrew/bin/ccwho")
        return rc, out.getvalue()

    def backups(self):
        return [n for n in os.listdir(self.root) if n.startswith("settings.json.bak-ccwho")]

    def test_yes_adds_it_keeps_the_rest_and_a_backup(self):
        self.write({"hooks": {"Stop": [1]}, "model": "opus"})
        rc, out = self.run_setup("y")
        self.assertEqual(rc, 0)
        data = self.read()
        self.assertEqual(data["statusLine"], {"type": "command", "command": self.OURS})
        self.assertEqual(data["hooks"], {"Stop": [1]})
        self.assertEqual(len(self.backups()), 1)
        self.assertIn('+  "statusLine"', out)                 # the diff was shown
        self.assertIn("Without this, you won't get usage info.", self.asked[0])

    def test_no_writes_nothing_and_is_remembered(self):
        self.write({"model": "opus"})
        rc, out = self.run_setup("n")
        self.assertEqual(self.read(), {"model": "opus"})
        self.assertEqual(self.backups(), [])
        self.asked.clear()
        rc, out = self.run_setup("y")                          # a later setup
        self.assertEqual(self.asked, [])                       # does not ask again
        self.assertEqual(self.read(), {"model": "opus"})
        self.assertIn("your choice", out)

    def test_an_empty_answer_is_no(self):
        self.write({})
        self.run_setup("")
        self.assertNotIn("statusLine", self.read())

    def test_cannot_ask_writes_nothing_and_remembers_nothing(self):
        self.write({})
        rc, out = self.run_setup(None)
        self.assertNotIn("statusLine", self.read())
        self.run_setup("y")
        self.assertIn("statusLine", self.read())               # it was not an opt-out

    def test_yes_flag_skips_the_question(self):
        self.write({})
        self.run_setup(None, yes=True)
        self.assertEqual(self.asked, [])
        self.assertEqual(self.read()["statusLine"]["command"], self.OURS)

    def test_yes_flag_never_overrides_an_opt_out(self):
        self.write({})
        self.run_setup("n")
        self.run_setup(None, yes=True)
        self.assertNotIn("statusLine", self.read())

    def test_no_settings_file_is_created(self):
        self.run_setup("y")
        self.assertEqual(self.read()["statusLine"]["command"], self.OURS)

    def test_a_file_changed_while_asking_is_left_alone(self):
        self.write({"model": "opus"})
        def ask(prompt):
            self.write({"model": "sonnet"})                    # another tool wrote it
            return "y"
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = runner.usage_setup([self.root], [], ask=ask, ccwho="/opt/homebrew/bin/ccwho")
        self.assertEqual(self.read(), {"model": "sonnet"})
        self.assertIn("changed", out.getvalue())
        self.assertEqual(rc, 1)

    def test_another_statusline_is_never_touched(self):
        self.write({"statusLine": {"type": "command", "command": "~/sl.sh"}})
        rc, out = self.run_setup("y")
        self.assertEqual(self.asked, [])
        self.assertEqual(self.read()["statusLine"]["command"], "~/sl.sh")
        self.assertIn("another statusLine", out)

    def test_invalid_json_is_never_touched(self):
        self.write("{nope")
        rc, out = self.run_setup("y")
        with open(self.settings) as fh:
            self.assertEqual(fh.read(), "{nope")
        self.assertEqual(self.asked, [])

    def test_a_second_run_changes_nothing(self):
        self.write({})
        self.run_setup("y")
        with open(self.settings) as fh:
            before = fh.read()
        self.asked.clear()
        rc, out = self.run_setup("y")
        self.assertEqual(self.asked, [])
        with open(self.settings) as fh:
            self.assertEqual(fh.read(), before)
        self.assertEqual(len(self.backups()), 1)

    def test_a_symlinked_settings_file_is_written_through_the_link(self):
        real = os.path.join(self.tmp, "dotfiles-settings.json")
        with open(real, "w") as fh:
            fh.write("{}")
        os.symlink(real, self.settings)
        self.run_setup("y")
        self.assertTrue(os.path.islink(self.settings))
        with open(real) as fh:
            self.assertIn("statusLine", json.load(fh))

    def test_skipped_dirs_are_named_with_the_fix(self):
        rc, out = self.run_setup("y", roots=[], skipped=["/h/.liveapp"])
        self.assertIn("/h/.liveapp", out)
        self.assertIn("run ccwho setup again", out)

    def test_no_usage_removes_ours_and_opts_every_dir_out(self):
        self.write({"model": "opus"})
        self.run_setup("y")
        rc, out = self.run_setup("y", mode="off")
        self.assertEqual(self.read(), {"model": "opus"})
        self.asked.clear()
        self.run_setup("y")
        self.assertEqual(self.asked, [])
        self.assertNotIn("statusLine", self.read())

    def test_no_usage_never_removes_another_statusline(self):
        self.write({"statusLine": {"type": "command", "command": "~/sl.sh"}})
        self.run_setup("y", mode="off")
        self.assertEqual(self.read()["statusLine"]["command"], "~/sl.sh")

    def test_no_usage_declined_keeps_it_but_still_opts_out(self):
        self.write({})
        self.run_setup("y")
        self.run_setup("n", mode="off")
        self.assertIn("statusLine", self.read())

    def test_a_private_settings_file_stays_private_and_so_does_its_backup(self):
        self.write({"env": {"SECRET": "x"}})
        os.chmod(self.settings, 0o600)
        self.run_setup("y")
        self.assertEqual(os.stat(self.settings).st_mode & 0o777, 0o600)
        backup = os.path.join(self.root, self.backups()[0])
        self.assertEqual(os.stat(backup).st_mode & 0o777, 0o600)

    def test_two_writes_in_one_second_keep_two_backups(self):
        self.write({"model": "opus"})
        real = runner.time.strftime
        runner.time.strftime = lambda fmt, *a: "20260925-120000"
        try:
            self.run_setup("y")
            self.run_setup("y", mode="off")
        finally:
            runner.time.strftime = real
        self.assertEqual(len(self.backups()), 2)
        texts = []
        for name in self.backups():
            with open(os.path.join(self.root, name)) as fh:
                texts.append(json.load(fh))
        self.assertIn({"model": "opus"}, texts)             # the original survives

    def test_a_settings_file_that_is_not_utf8_is_never_touched(self):
        with open(self.settings, "wb") as fh:
            fh.write(b'{"note": "caf\xe9"}')
        rc, out = self.run_setup("y")
        with open(self.settings, "rb") as fh:
            self.assertEqual(fh.read(), b'{"note": "caf\xe9"}')
        self.assertIn("not valid", out)

    def test_an_empty_settings_file_is_treated_as_no_settings(self):
        self.write("  \n")
        self.run_setup("y")
        self.assertEqual(self.read()["statusLine"]["command"], self.OURS)

    def test_a_dangling_settings_link_is_never_written(self):
        gone = os.path.join(self.tmp, "moved", "settings.json")
        os.symlink(gone, self.settings)
        rc, out = self.run_setup("y")
        self.assertFalse(os.path.exists(os.path.dirname(gone)))
        self.assertIn("broken link", out)
        self.assertEqual(self.asked, [])

    def test_a_root_with_a_trailing_slash_is_the_same_dir(self):
        self.write({})
        self.run_setup("n", roots=[self.root + "/"])
        self.asked.clear()
        self.run_setup("y", roots=[self.root])
        self.assertEqual(self.asked, [])
        self.assertNotIn("statusLine", self.read())

    def test_a_no_for_a_dir_holds_when_it_comes_back_with_a_slash(self):
        self.write({})
        self.run_setup("n", roots=[self.root])
        self.asked.clear()
        self.run_setup("y", roots=[self.root + "/"])
        self.assertEqual(self.asked, [])
        self.assertNotIn("statusLine", self.read())

    def test_no_usage_without_a_terminal_does_not_claim_off(self):
        self.write({})
        self.run_setup("y")
        rc, out = self.run_setup(None, mode="off")
        self.assertTrue(runner.setup.is_ours(self.read()["statusLine"]["command"]))
        self.assertNotIn("usage info: off", out)
        self.assertIn("still on", out)

    def test_no_usage_declined_does_not_claim_off(self):
        self.write({})
        self.run_setup("y")
        rc, out = self.run_setup("n", mode="off")
        self.assertIn("still on", out)

    def test_a_new_settings_file_follows_the_umask(self):
        old = os.umask(0o077)
        try:
            self.run_setup("y")
        finally:
            os.umask(old)
        self.assertEqual(os.stat(self.settings).st_mode & 0o777, 0o600)

    def test_a_backup_that_fails_to_write_leaves_nothing(self):
        path = os.path.join(self.root, "settings.json")
        with self.assertRaises(ValueError):
            runner._backup(path, "\udc80", 0o600)       # cannot be encoded
        self.assertEqual(self.backups(), [])

    def open_fds(self):
        return len(os.listdir("/dev/fd"))

    def test_a_failing_chmod_leaks_no_file_descriptor(self):
        real = runner.os.fchmod
        def boom(fd, mode):
            raise PermissionError("EPERM")
        before = self.open_fds()
        runner.os.fchmod = boom
        try:
            with self.assertRaises(PermissionError):
                runner._backup(os.path.join(self.root, "settings.json"), "{}", 0o600)
            with self.assertRaises(PermissionError):
                runner.write_atomic(os.path.join(self.root, "x.json"), "{}", mode=0o600)
        finally:
            runner.os.fchmod = real
        self.assertEqual(self.open_fds(), before)

    def test_no_usage_names_a_file_it_could_not_read_and_does_not_claim_off(self):
        with open(self.settings, "wb") as fh:
            fh.write(b'{"statusLine": "caf\xe9"}')
        rc, out = self.run_setup("y", mode="off", yes=True)
        self.assertIn("not checked", out)
        self.assertIn(self.settings, out)
        self.assertNotIn("usage info: off", out)
        self.assertNotIn("--yes", out)            # --yes was given; it cannot help
        self.assertIn("not valid UTF-8", out.splitlines()[-1])
        self.assertEqual(rc, 1)

    def test_no_usage_that_removed_it_says_off(self):                  # control
        self.write({})
        self.run_setup("y")
        rc, out = self.run_setup("y", mode="off")
        self.assertIn("usage info: off", out)

    def test_usage_flag_clears_the_opt_out(self):
        self.write({})
        self.run_setup("n")
        self.run_setup("y", mode="on")
        self.assertIn("statusLine", self.read())


class TestSetupOffersUsage(SetupHarness):
    def call(self, argv):
        seen = []
        real = runner.usage_setup
        runner.usage_setup = lambda roots, skipped, **k: seen.append((roots, skipped, k)) or 0
        try:
            rc, out = self.run_setup(argv)
        finally:
            runner.usage_setup = real
        return rc, out, seen

    def test_setup_offers_it_for_the_interactive_dirs(self):
        self.usage_dirs = (["/h/.claude"], ["/h/.la"])
        rc, out, seen = self.call(["--yes", "--no-hotkey", "--no-list"])
        self.assertEqual(seen, [(["/h/.claude"], ["/h/.la"], {"yes": True, "mode": ""})])

    def test_no_usage_and_usage_are_modes(self):
        _, _, seen = self.call(["--no-usage", "--no-hotkey", "--no-list"])
        self.assertEqual(seen[0][2]["mode"], "off")
        _, _, seen = self.call(["--usage", "--no-hotkey", "--no-list"])
        self.assertEqual(seen[0][2]["mode"], "on")

    def test_both_at_once_is_refused(self):
        rc, out, seen = self.call(["--usage", "--no-usage", "--no-list"])
        self.assertEqual(rc, 2)
        self.assertEqual(seen, [])

    def test_the_real_thing_writes_into_a_dir_setup_found(self):
        root = os.path.join(self.home, ".claude")
        os.makedirs(root)
        self.usage_dirs = ([root], [])
        self.run_setup(["--yes", "--no-hotkey", "--no-list"])
        with open(os.path.join(root, "settings.json")) as fh:
            self.assertTrue(runner.setup.is_ours(json.load(fh)["statusLine"]["command"]))


class TestWhichDirsAreInteractive(unittest.TestCase):
    """Setup refreshes the saved index for the known roots, and scans the dirs
    running sessions name WITHOUT saving them - the index drops every path it
    was not given, so saving them would rescan them on every refresh."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.old = os.environ.get("CCWHO_DIR")
        os.environ["CCWHO_DIR"] = os.path.join(self.tmp, "ccwho")
        self.known = self.root("known", "cli")
        self.extra = self.root("extra", "cli")
        self.program = self.root("program", "sdk-cli")
        self.real = (runner.engine.config_dirs_now, runner.index.config_roots)
        runner.engine.config_dirs_now = lambda table=None: [self.known, self.extra,
                                                             self.program]
        runner.index.config_roots = lambda roots_file=None: [self.known]

    def tearDown(self):
        runner.engine.config_dirs_now, runner.index.config_roots = self.real
        if self.old is None:
            os.environ.pop("CCWHO_DIR", None)
        else:
            os.environ["CCWHO_DIR"] = self.old

    def root(self, name, entrypoint):
        d = os.path.join(self.tmp, name)
        p = os.path.join(d, "projects", "-x")
        os.makedirs(p)
        sid = {"known": "1", "extra": "2", "program": "3"}[name] * 8 + "-0000-0000-0000-000000000000"
        with open(os.path.join(p, sid + ".jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "user", "sessionId": sid, "entrypoint": entrypoint,
                                 "timestamp": "2026-09-25T10:00:00.000Z", "cwd": "/x",
                                 "message": {"role": "user", "content": "hello there"}}) + "\n")
        return d

    def test_interactive_dirs_found_program_dirs_skipped(self):
        with contextlib.redirect_stderr(io.StringIO()):
            yes, skipped = runner.usage_roots()
        self.assertEqual(yes, [self.known, self.extra])
        self.assertEqual(skipped, [self.program])

    def test_only_the_known_roots_are_saved_in_the_index(self):
        with contextlib.redirect_stderr(io.StringIO()):
            runner.usage_roots()
        paths = [e["path"] for e in runner.index.load(runner.index_path()).values()]
        self.assertTrue(paths)                                              # control
        self.assertTrue(all(p.startswith(self.known) for p in paths), paths)

    def test_a_process_table_that_cannot_be_read_falls_back_to_the_known_roots(self):
        def broken(table=None):
            raise OSError("ps")
        runner.engine.config_dirs_now = broken
        with contextlib.redirect_stderr(io.StringIO()):
            yes, skipped = runner.usage_roots()
        self.assertEqual((yes, skipped), ([self.known], []))


class TestDoctorGathersUsage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.old = os.environ.get("CCWHO_DIR")
        os.environ["CCWHO_DIR"] = os.path.join(self.tmp, "ccwho")
        os.makedirs(os.path.join(self.tmp, "ccwho", "usage"))
        self.root = os.path.join(self.tmp, ".claude")
        os.makedirs(self.root)
        self.real = runner.index.config_roots
        runner.index.config_roots = lambda roots_file=None: [self.root]

    def tearDown(self):
        runner.index.config_roots = self.real
        if self.old is None:
            os.environ.pop("CCWHO_DIR", None)
        else:
            os.environ["CCWHO_DIR"] = self.old

    def test_a_dir_with_ours_is_reported_even_without_the_index(self):
        with open(os.path.join(self.root, "settings.json"), "w") as fh:
            json.dump({"statusLine": {"type": "command", "command": "ccwho statusline"}}, fh)
        f = runner.usage_facts()
        self.assertEqual(f["usage_roots"], [{"root": self.root, "state": "ours",
                                             "opted_out": False}])
        self.assertIsNone(f["usage_newest_age"])

    def test_a_dir_with_nothing_and_no_interactive_session_is_not_reported(self):
        self.assertEqual(runner.usage_facts()["usage_roots"], [])

    def test_an_opted_out_dir_is_reported_as_such(self):
        with open(runner.opt_out_path(), "w") as fh:
            json.dump([self.root], fh)
        f = runner.usage_facts()
        self.assertEqual(f["usage_roots"][0]["opted_out"], True)

    def test_a_settings_file_that_is_not_utf8_is_invalid_not_a_crash(self):
        with open(os.path.join(self.root, "settings.json"), "wb") as fh:
            fh.write(b'{"statusLine": "caf\xe9"}')
        with open(runner.opt_out_path(), "w") as fh:
            json.dump([self.root], fh)
        f = runner.usage_facts()
        self.assertEqual(f["usage_roots"][0]["state"], "invalid")

    def test_a_trailing_slash_root_still_finds_its_opt_out(self):
        runner.index.config_roots = lambda roots_file=None: [self.root + "/"]
        with open(runner.opt_out_path(), "w") as fh:
            json.dump([self.root], fh)
        f = runner.usage_facts()
        self.assertEqual(f["usage_roots"], [{"root": self.root, "state": "missing",
                                             "opted_out": True}])

    def test_the_newest_reading_age(self):
        with open(os.path.join(self.tmp, "ccwho", "usage", "abc.json"), "w") as fh:
            json.dump({"received_at": time.time() - 120, "session_id": "abc",
                       "rate_limits": {"five_hour": {"used_percentage": 1, "resets_at": None}},
                       "first_account": {"kind": "login", "id": "login:a"}}, fh)
        self.assertAlmostEqual(runner.usage_facts()["usage_newest_age"], 120, delta=5)


class TestUsageInTheTable(unittest.TestCase):
    ROWS = [{"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef", "project": "liveapp",
             "title": "t", "tab_title": "liveapp-4e", "name": "n", "since": "1m",
             "tty": "", "attention": "asks", "status": "waiting", "ask": "q",
             "doing": "q", "orphans": 0, "topic": ""}]

    def setUp(self):
        self.real = (runner.engine.collect, runner.usage_snapshot)
        runner.engine.collect = lambda cache=None, status=None: (list(self.ROWS), {})
        self.snap = {"state": "waiting"}
        runner.usage_snapshot = lambda rows, now=None, record=True: self.snap

    def tearDown(self):
        runner.engine.collect, runner.usage_snapshot = self.real

    def test_the_table_has_the_usage_line(self):
        out, _ = runner.tick({"watch": False, "ticks": 0}, [], False)
        self.assertIn("usage  waiting", out)

    def test_a_usage_failure_is_unknown_and_the_rows_survive(self):
        def boom(rows, now=None, record=True):
            raise OSError("disk")
        runner.usage_snapshot = boom
        out, rows = runner.tick({"watch": False, "ticks": 0}, [], False)
        self.assertIn("usage  unknown", out)
        self.assertIn("liveapp", out)

    def test_filtered_ls_gets_the_fleet_and_the_usage(self):
        out = io.StringIO()
        real = runner.fresh_index
        runner.fresh_index = lambda quiet=False: {}
        try:
            with contextlib.redirect_stdout(out):
                runner.ls(["liveapp", "--no-color"])
        finally:
            runner.fresh_index = real
        self.assertIn("usage  waiting", out.getvalue())


class TestUsageSnapshotLogsWhatItShows(unittest.TestCase):
    def test_the_shown_log_gets_one_line_per_window(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        old = os.environ.get("CCWHO_DIR")
        os.environ["CCWHO_DIR"] = tmp
        self.addCleanup(lambda: os.environ.pop("CCWHO_DIR", None) if old is None
                        else os.environ.__setitem__("CCWHO_DIR", old))
        os.makedirs(os.path.join(tmp, "usage"))
        now = time.time()
        rec = {"v": 1, "session_id": "s1", "received_at": now, "measured_at": now,
               "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": now + 100}},
               "account": {"kind": "login", "id": "login:a", "email": "a@x.com"},
               "first_account": {"kind": "login", "id": "login:a", "email": "a@x.com"},
               "first_seen": now, "login_at": None, "unsure": False}
        with open(os.path.join(tmp, "usage", "s1.json"), "w") as fh:
            json.dump(rec, fh)
        real = runner.usage_facts
        runner.usage_facts = lambda now=None: {"usage_roots": []}
        try:
            snap = runner.usage_snapshot([{"sessionId": "s1"}], now)
        finally:
            runner.usage_facts = real
        self.assertEqual(snap["state"], "ok")
        with open(os.path.join(tmp, "usage-shown.jsonl")) as fh:
            lines = [json.loads(l) for l in fh]
        self.assertEqual([(l["account"], l["window"], l["used"]) for l in lines],
                         [("a", "5h", 5)])


class TestReloadRebindsUsage(unittest.TestCase):
    def tearDown(self):
        # a reload re-executes the engine and rebinds the real functions; left
        # unguarded, every later save test asked the real iTerm2 for its panes
        install_guards()

    def test_the_runner_uses_the_reloaded_usage_module(self):
        state = {"engine_error": ""}
        runner.reload_engine(state)
        self.assertEqual(state["engine_error"], "")
        self.assertIs(runner.usage, sys.modules["ccwho_usage"])
        self.assertIs(runner.engine.ccwho_usage, sys.modules["ccwho_usage"])


class TestUsageWords(unittest.TestCase):
    def test_help_has_the_legend(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.main(["--help"])
        self.assertIn("5h 42%↓/60%", out.getvalue())

    def test_help_names_every_command(self):
        # The commands come from main's own dispatch, so a new one that --help
        # leaves out fails here: kill, clean and reap shipped without a line.
        # hotkey is left out on purpose: only the hotkey window starts it.
        import inspect
        import re
        cmds = set(re.findall(r'argv\[0\] == "([a-z]+)"', inspect.getsource(runner.main)))
        self.assertGreaterEqual(len(cmds), 16)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.main(["--help"])
        missing = sorted(c for c in cmds - {"hotkey"}
                         if not re.search(rf"^\s*(usage: )?ccwho {c}\b", out.getvalue(), re.M))
        self.assertEqual(missing, [])

    def test_setup_says_when_sessions_report(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        old = os.environ.get("CCWHO_DIR")
        os.environ["CCWHO_DIR"] = os.path.join(tmp, "c")
        self.addCleanup(lambda: os.environ.pop("CCWHO_DIR", None) if old is None
                        else os.environ.__setitem__("CCWHO_DIR", old))
        root = os.path.join(tmp, ".claude")
        os.makedirs(root)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.usage_setup([root], [], yes=True, ccwho="/x/ccwho")
        # measured 2026-09-25 on the owner's setup run: 11 of 11 sessions that
        # were already running reported - running sessions reload settings.json
        self.assertIn("sessions report usage after their next reply", out.getvalue())
        self.assertNotIn("restart", out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.usage_setup([root], [], yes=True, ccwho="/x/ccwho")   # already on
        self.assertNotIn("next reply", out.getvalue())


class TestReviewRoundOneRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        old = os.environ.get("CCWHO_DIR")
        os.environ["CCWHO_DIR"] = self.tmp
        self.addCleanup(lambda: os.environ.pop("CCWHO_DIR", None) if old is None
                        else os.environ.__setitem__("CCWHO_DIR", old))
        os.makedirs(os.path.join(self.tmp, "usage"))
        self.now = time.time()

    def put(self, sid, aid, email):
        acct = {"kind": "login", "id": aid, "email": email}
        rec = {"v": 1, "session_id": sid, "received_at": self.now, "measured_at": self.now,
               "rate_limits": {"five_hour": {"used_percentage": 5, "resets_at": self.now + 99}},
               "account": acct, "first_account": acct, "first_seen": self.now,
               "login_at": None, "unsure": False}
        with open(os.path.join(self.tmp, "usage", sid + ".json"), "w") as fh:
            json.dump(rec, fh)

    def test_the_snapshot_uses_the_usage_module_in_sys_modules(self):
        """The live list imports the runner once; after a reload the module in
        sys.modules is the one to use (and after a failed reload, the old one
        is put back there)."""
        import types
        fake = types.ModuleType("ccwho_usage")
        for name in dir(runner.usage):
            setattr(fake, name, getattr(runner.usage, name))
        fake.snapshot = lambda *a, **k: {"state": "from-the-reloaded-module"}
        real = sys.modules["ccwho_usage"]
        sys.modules["ccwho_usage"] = fake
        try:
            snap = runner.usage_snapshot([], self.now)
        finally:
            sys.modules["ccwho_usage"] = real
        self.assertEqual(snap["state"], "from-the-reloaded-module")

    def test_a_terminal_that_parked_a_job_is_a_live_session(self):
        # ctrl+b: the terminal has no row, and its account is still in use
        import types
        fake = types.ModuleType("ccwho_usage")
        for name in dir(runner.usage):
            setattr(fake, name, getattr(runner.usage, name))
        fake.snapshot = lambda readings, now, live, *a, **k: {"state": sorted(live)}
        real = sys.modules["ccwho_usage"]
        sys.modules["ccwho_usage"] = fake
        try:
            snap = runner.usage_snapshot([{"sessionId": "j", "parked": ["p"]}], self.now,
                                         record=False)
        finally:
            sys.modules["ccwho_usage"] = real
        self.assertEqual(snap["state"], ["j", "p"])

    def test_one_read_of_the_readings_and_no_settings_when_an_account_is_live(self):
        self.put("s1", "login:a", "a@x.com")
        calls = {"load": 0, "facts": 0}
        # the module usage_snapshot uses: the one in sys.modules. An earlier
        # test's reload can leave runner.usage pointing at an older object
        mod = sys.modules["ccwho_usage"]
        real_load, real_facts = mod.load_readings, runner.usage_facts
        def load(*a, **k):
            calls["load"] += 1
            return real_load(*a, **k)
        def facts(*a, **k):
            calls["facts"] += 1
            return {"usage_roots": []}
        mod.load_readings, runner.usage_facts = load, facts
        try:
            snap = runner.usage_snapshot([{"sessionId": "s1"}], self.now)
        finally:
            mod.load_readings, runner.usage_facts = real_load, real_facts
        self.assertEqual(snap["state"], "ok")
        self.assertEqual(calls, {"load": 1, "facts": 0})

    def test_filtered_ls_shows_only_the_accounts_of_the_rows_shown(self):
        self.put("s1", "login:a", "ann@x.com")
        self.put("s2", "login:b", "bob@x.com")
        rows = [dict(TestUsageInTheTable.ROWS[0], sessionId="s1", project="alpha"),
                dict(TestUsageInTheTable.ROWS[0], sessionId="s2", project="beta")]
        real = (runner.engine.collect, runner.fresh_index, runner.usage_facts)
        runner.engine.collect = lambda cache=None, status=None: (list(rows), {})
        runner.fresh_index = lambda quiet=False: {}
        runner.usage_facts = lambda *a, **k: {"usage_roots": []}
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                runner.ls(["alpha", "--no-color"])
        finally:
            runner.engine.collect, runner.fresh_index, runner.usage_facts = real
        self.assertIn("ann", out.getvalue())
        self.assertNotIn("bob", out.getvalue())

    def test_the_status_bar_name_is_the_one_the_list_used(self):
        self.put("s1", "login:a", "lukaso@gmail.com")
        self.put("s2", "login:b", "lukaso@work.com")
        real = runner.usage_facts
        runner.usage_facts = lambda *a, **k: {"usage_roots": []}
        try:
            snap = runner.usage_snapshot([{"sessionId": "s1"}, {"sessionId": "s2"}], self.now)
        finally:
            runner.usage_facts = real
        self.assertEqual(snap["names"]["login:a"], "lukaso@gmail")
        rec = json.load(open(os.path.join(self.tmp, "usage", "s1.json")))
        out = runner.usage.status_text(rec, self.now, names=runner.read_names())
        self.assertTrue(runner.usage.ANSI_RE.sub("", out).startswith("lukaso@gmail "), out)


class TestFilteredLsLeavesTheSharedFilesAlone(TestReviewRoundOneRunner):
    def test_names_and_shown_log_are_untouched(self):
        self.put("s1", "login:a", "lukaso@gmail.com")
        self.put("s2", "login:b", "lukaso@work.com")
        rows = [dict(TestUsageInTheTable.ROWS[0], sessionId="s1", project="alpha"),
                dict(TestUsageInTheTable.ROWS[0], sessionId="s2", project="beta")]
        real = (runner.engine.collect, runner.fresh_index, runner.usage_facts)
        runner.engine.collect = lambda cache=None, status=None: (list(rows), {})
        runner.fresh_index = lambda quiet=False: {}
        runner.usage_facts = lambda *a, **k: {"usage_roots": []}
        try:
            runner.usage_snapshot(rows, self.now)              # the full list, first
            names = open(runner.names_path()).read()
            shown = open(os.path.join(self.tmp, "usage-shown.jsonl")).read()
            with contextlib.redirect_stdout(io.StringIO()):
                runner.ls(["alpha", "--no-color"])
        finally:
            runner.engine.collect, runner.fresh_index, runner.usage_facts = real
        self.assertEqual(open(runner.names_path()).read(), names)
        self.assertEqual(open(os.path.join(self.tmp, "usage-shown.jsonl")).read(), shown)


class TestEveryOsascriptCallHasADeadline(unittest.TestCase):
    """An osascript with no deadline waits as long as iTerm2 does - forever,
    when iTerm2 is stuck (2026-09-29). jump and restore had none. Checked over
    the source, so a new call cannot slip in without one."""

    def test_each_direct_osascript_call_passes_a_timeout(self):
        import ast
        here = os.path.dirname(os.path.abspath(__file__))
        missing, seen = [], 0
        for name in sorted(os.listdir(here)):
            if not name.startswith("ccwho") or not name.endswith(".py"):
                continue
            tree = ast.parse(open(os.path.join(here, name)).read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and node.args
                        and isinstance(node.args[0], ast.List) and node.args[0].elts
                        and isinstance(node.args[0].elts[0], ast.Constant)
                        and node.args[0].elts[0].value == "osascript"):
                    continue
                seen += 1
                if not any(k.arg == "timeout" for k in node.keywords):
                    missing.append(f"{name}:{node.lineno}")
                # its output is decoded after it ran: a byte the locale cannot
                # read must not raise out of a launch that went through
                if (any(k.arg == "text" for k in node.keywords)
                        and not any(k.arg == "errors" for k in node.keywords)):
                    missing.append(f"{name}:{node.lineno} decodes strictly")
        self.assertGreater(seen, 0, "the scan found no osascript call at all")   # control
        self.assertEqual(missing, [])


class TestAStuckITerm2IsSaidNotRaised(unittest.TestCase):
    """With a deadline, an osascript that iTerm2 never answers ends in
    TimeoutExpired. That must be a sentence, not a traceback."""

    # the open fixture, borrowed - not inherited, or its tests run here too
    SID, ENTRY = TestOpenNeverForksALiveSession.SID, TestOpenNeverForksALiveSession.ENTRY
    tearDown = TestOpenNeverForksALiveSession.tearDown
    _open = TestOpenNeverForksALiveSession._open

    def setUp(self):
        TestOpenNeverForksALiveSession.setUp(self)

        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        runner.subprocess.run = stuck

    def test_reopening_says_iterm2_did_not_answer(self):
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1)
        self.assertIn("did not answer", out)

    def test_attaching_says_iterm2_did_not_answer(self):
        self.live = [{"sessionId": self.SID, "tty": "", "pid": 90266, "kind": "background"}]
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1)
        self.assertIn("did not answer", out)

    def test_a_reopen_that_timed_out_keeps_its_claim(self):
        # the killed osascript's event can still run in iTerm2: a retry inside
        # the claim window would start the session twice - a forked conversation
        self._open(self.SID)
        dead = lambda pid: False                        # this CLI has exited
        self.assertFalse(runner.claim_launch(self.SID, alive=dead),
                         "a retry could launch it a second time")

    def test_a_reopen_that_worked_holds_until_the_session_shows_up(self):
        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda *a, **k: Done()      # osascript answers
        self._open(self.SID)
        self.assertFalse(runner.claim_launch(self.SID, alive=lambda pid: False))
        self.live = [{"sessionId": self.SID, "tty": "ttys032", "pid": 90266, "windowed": True}]
        self._open(self.SID)                                # seen running
        self.live = []
        self.assertTrue(runner.claim_launch(self.SID, alive=lambda pid: False))  # control

    def test_jumping_says_iterm2_did_not_answer(self):
        self.live = [{"sessionId": self.SID, "pid": 42, "tty": "ttys032",
                      "title": "t", "name": "n", "project": "liveapp"}]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.jump(["ttys032"])
        self.assertEqual(rc, 1)
        self.assertIn("did not answer", out.getvalue() + err.getvalue())


class TestWhatYouAskForSkipsTheGate(unittest.TestCase):
    """Owner, 2026-09-29: the gate limits BACKGROUND asks so they cannot freeze
    iTerm2; what you ask for goes ahead. A reopen while the gate would refuse
    still runs its osascript."""

    SID, ENTRY = TestOpenNeverForksALiveSession.SID, TestOpenNeverForksALiveSession.ENTRY
    tearDown = TestOpenNeverForksALiveSession.tearDown
    _open = TestOpenNeverForksALiveSession._open

    def setUp(self):
        TestOpenNeverForksALiveSession.setUp(self)
        real = runner.engine.terms.ITERM2.ask

        def refuses(*a, **k):
            raise AssertionError("a user action went through the background gate")
        runner.engine.terms.ITERM2.ask = refuses
        self.addCleanup(setattr, runner.engine.terms.ITERM2, "ask", real)

    def test_a_reopen_runs_while_the_gate_refuses(self):
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 0, out)
        self.assertIn("claude --resume", " ".join(" ".join(c) for c in self.runs))


class TestARestoreHasTimeForEveryWindow(unittest.TestCase):
    """One deadline for one window or forty: forty at a few seconds each would
    be killed half way, its output - which panes it filled - lost."""

    def test_the_deadline_grows_with_the_windows(self):
        self.assertGreaterEqual(runner.restore_deadline(1), 30)
        self.assertGreaterEqual(runner.restore_deadline(40), 40 * 5)
        self.assertGreater(runner.restore_deadline(40), runner.restore_deadline(1))


class TestAClaimOutlivesALaunchThatMayStillHappen(unittest.TestCase):
    """A claim is "I am opening this". After a timed-out launch the event may
    still run inside iTerm2 - so the claim holds while THAT iTerm2 lives, and
    goes when it does: its queue went with it."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        self.addCleanup(os.environ.pop, "CCWHO_DIR", None)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])
        self.addCleanup(setattr, runner.subprocess, "run", runner.subprocess.run)
        # a boot id of the test's own: not every host can read one
        self.addCleanup(setattr, runner, "_boot_id", runner._boot_id)
        runner._boot_id = lambda: "THIS-BOOT"
        self.T = time.time()

    def test_an_unresolved_claim_holds_while_its_iterm2_lives(self):
        runner.claim_launch(self.SID, now=self.T)
        runner.claim_unresolved(self.SID, now=self.T, iterm_pid=4242)
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        iterm_up = lambda pid: pid == 4242
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 3600, alive=iterm_up))

    def test_it_goes_when_that_iterm2_does(self):                      # control
        runner.claim_launch(self.SID, now=self.T)
        runner.claim_unresolved(self.SID, now=self.T, iterm_pid=4242)
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 30, alive=lambda pid: False))

    def test_one_sent_while_no_iterm2_ran_is_held_by_the_iterm2_it_started(self):
        # `tell application "iTerm2"` started it, and that iTerm2 got the event
        runner.claim_launch(self.SID, now=self.T)
        runner.claim_unresolved(self.SID, now=self.T, iterm_pid=None)
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 3600,
                                             alive=lambda pid: pid == 4242))
        # bound to that iTerm2 when first seen: restarting iTerm2 still clears it
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n5151 {os.getuid()} iTerm2\n"
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 3601,
                                            alive=lambda pid: pid == 5151))

    def test_one_sent_while_no_iterm2_ran_goes_when_none_runs(self):    # control
        runner.claim_launch(self.SID, now=self.T)
        runner.claim_unresolved(self.SID, now=self.T, iterm_pid=None)
        runner.engine.terms.app_snapshot = lambda: "  PID UID UCOMM\n1 0 launchd\n"
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 30, alive=lambda pid: False))

    def test_a_claim_from_before_the_mac_restarted_is_let_go(self):
        # the pid of the iTerm2 it went to can be an iTerm2 again after a boot
        runner.claim_launch(self.SID, now=self.T - 60)
        runner.claim_unresolved(self.SID, now=self.T - 60, iterm_pid=4242)
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        real = runner._boot_id
        self.addCleanup(setattr, runner, "_boot_id", real)
        runner._boot_id = lambda: "ANOTHER-BOOT"
        self.assertTrue(runner.claim_launch(self.SID, now=self.T, alive=lambda pid: pid == 4242))

    def test_a_claim_since_the_mac_started_is_not(self):                # control
        runner.claim_launch(self.SID, now=self.T - 60)
        runner.claim_unresolved(self.SID, now=self.T - 60, iterm_pid=4242)
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        self.assertFalse(runner.claim_launch(self.SID, now=self.T, alive=lambda pid: pid == 4242))

    def test_the_boot_is_told_by_its_id_not_the_clock(self):
        # a wall clock stepped forward makes a claim of this boot look older
        # than the boot: its id says it is not
        runner.claim_launch(self.SID, now=self.T - 30 * 86400)
        runner.claim_unresolved(self.SID, now=self.T - 30 * 86400, iterm_pid=4242)
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        self.assertFalse(runner.claim_launch(self.SID, now=self.T, alive=lambda pid: pid == 4242))

    def test_a_clock_stepped_back_does_not_void_a_claim(self):
        # written when the clock was 5 minutes ahead
        me = os.getpid()
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T + 300, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": me,
                                       "boot": runner._boot_id()})
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 1))


class TestTwoCcwhosNeverBothTakeAStaleClaim(unittest.TestCase):
    """A stale claim was judged, then removed, then made again: two ccwhos -
    a double click on a link - could both judge it free, and the second
    removed the first one's new claim. Both launched: a fork."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_exactly_one_gets_it(self):
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T - 3600, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": DEAD})
        got, other = [], []

        def judging(pid):
            # the first ccwho is between reading the stale claim and replacing
            # it: the second runs all the way through now, if nothing stops it
            if not other:
                t = threading.Thread(target=lambda: other.append(runner.claim_launch(self.SID)))
                other.append(t)
                t.start()
                t.join(1.0)
            return False
        got.append(runner.claim_launch(self.SID, alive=judging))
        other[0].join(10)
        self.assertEqual([got[0], other[1]].count(True), 1, (got, other[1:]))

    def test_a_claim_made_while_a_sighting_lets_go_survives_it(self):
        # the sighting read the old claim; another ccwho replaced it before the
        # sighting wrote over it - and the new claim was lost
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T - 100, "sessionId": self.SID})
        other, real = [], runner._read_claim

        def reading(sid):
            rec = real(sid)
            if not other:                     # the sighting, between read and write
                t = threading.Thread(target=lambda: other.append(runner.claim_launch(self.SID)))
                other.append(t)
                t.start()
                t.join(1.0)
            return rec
        runner._read_claim = reading
        self.addCleanup(setattr, runner, "_read_claim", real)
        runner.release_claims([self.SID], self.T)
        other[0].join(10)
        self.assertTrue(other[1])
        with open(runner._claim_path(self.SID)) as fh:
            self.assertEqual(json.load(fh).get("pid"), os.getpid(), "the new claim was lost")


class TestAClaimIsOwnedByTheCallThatMadeIt(unittest.TestCase):
    """The list runs each restore in a thread of one process: a claim one of
    them took over from another is not the first one's to send."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_claim_another_thread_took_over(self):
        self.assertTrue(runner.claim_launch(self.SID, now=self.T - 100))   # aged past its time
        other = []
        t = threading.Thread(target=lambda: other.append(runner.claim_launch(self.SID, now=self.T)))
        t.start()
        t.join(10)
        self.assertEqual(other, [True])
        self.assertFalse(runner.claim_unresolved(self.SID, 4242, now=self.T + 1))

    def test_its_own_claim(self):                                          # control
        self.assertTrue(runner.claim_launch(self.SID, now=self.T))
        self.assertTrue(runner.claim_unresolved(self.SID, 4242, now=self.T + 1))


class TestOnlyOurOwnClaimIsRewrittenOrDropped(unittest.TestCase):
    """A launcher marks and lets go of the claims IT made. One another ccwho
    took over in between - after ours outlived its time - is that one's."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def taken_over(self):
        runner.claim_launch(self.SID, now=self.T)
        other = {"pid": DEAD, "since": self.T + 1, "sessionId": self.SID}
        runner._write_claim(self.SID, other)
        return other

    def on_disk(self):
        with open(runner._claim_path(self.SID)) as fh:
            return unstamped(json.load(fh))

    def test_it_is_not_written_over(self):
        other = self.taken_over()
        self.assertFalse(runner.claim_unresolved(self.SID, 4242, now=self.T + 2))
        self.assertEqual(self.on_disk(), other)

    def test_it_is_not_dropped(self):
        other = self.taken_over()
        runner.drop_claims([self.SID])
        self.assertEqual(self.on_disk(), other)

    def test_our_own_is(self):                                            # control
        runner.claim_launch(self.SID, now=self.T)
        self.assertTrue(runner.claim_unresolved(self.SID, 4242, now=self.T + 2))
        runner.drop_claims([self.SID])
        self.assertFalse(os.path.exists(runner._claim_path(self.SID)))


class TestARestoreThatTimedOutKeepsItsClaims(unittest.TestCase):
    """The same as `ccwho open`: a restore killed at its deadline may still be
    opening windows. Each session it claimed stays claimed."""

    # the restore fixture, borrowed - not inherited, or its tests run here too
    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown = _F.setUp, _F.tearDown
    write_manifest, _restore_open = _F.write_manifest, _F._restore_open

    def run_stuck(self, fail):
        runner.subprocess.run = fail
        rc, out = self._restore_open()
        return rc, out

    def held(self, sid):
        return not runner.claim_launch(sid, alive=lambda pid: False)

    def test_a_timeout_keeps_every_claim_and_says_so_plainly(self):
        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        rc, out = self.run_stuck(stuck)
        self.assertEqual(rc, 1)
        self.assertIn("did not answer", out)
        self.assertNotIn("create window", out, "the whole script was printed")
        self.assertTrue(self.held(self.LIVE_SID) and self.held(self.DEAD_SID))

    def test_a_timeout_inside_iterm2_keeps_them_too(self):
        class TimedOut:
            returncode, stdout = 1, ""
            stderr = "execution error: AppleEvent timed out. (-1712)"
        rc, _ = self.run_stuck(lambda cmd, **kw: TimedOut())
        self.assertEqual(rc, 1)
        self.assertTrue(self.held(self.LIVE_SID) and self.held(self.DEAD_SID))

    def test_a_refusal_at_the_probe_lets_it_go(self):                    # control
        self.write_manifest([{"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"}])
        self.run_stuck(refused_at(runner.engine.terms.AE_PROBE))
        self.assertFalse(self.held(self.DEAD_SID))

    def test_the_deadline_passed_is_the_one_for_its_windows(self):
        seen = []

        class Done:
            returncode, stdout, stderr = 0, "", ""
        self.run_stuck(lambda cmd, **kw: seen.append(kw.get("timeout")) or Done())
        self.assertEqual(seen, [runner.restore_deadline(2)])



class TestAHotkeySetupWhileITerm2IsNotAnswering(unittest.TestCase):
    """`ccwho setup` with iTerm2 running but not answering Apple Events must not
    say "start iTerm2"."""

    def out(self, iterm_ok):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runner.install_hotkey(home, runner.setup.DEFAULT_HOTKEY, prove=False, iterm_ok=iterm_ok)
        return buf.getvalue()

    def test_a_process_list_that_could_not_be_read(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            runner.install_hotkey(home, runner.setup.DEFAULT_HOTKEY, prove=False, iterm_ok=None,
                                  iterm_why="no-table")
        self.assertIn("process list", buf.getvalue())
        self.assertNotIn("restart iTerm2", buf.getvalue())

    def test_not_answering_says_restart(self):
        text = self.out(None)
        self.assertNotIn("\nstart iTerm2", "\n" + text)   # "restart" contains it
        self.assertIn("restart iTerm2", text)

    def test_not_running_still_says_start(self):                          # control
        self.assertIn("start iTerm2", self.out(False))


ITERM_TABLE = f"  PID UID UCOMM\n4242 {os.getuid()} iTerm2\n"


def unresolved(sid):
    """The record on disk now: an earlier launch that did not finish."""
    try:
        return runner._reason(runner._read_claim(sid), time.time(), None)[0] == "unresolved"
    except (OSError, ValueError):
        return False                              # none, or none ccwho wrote


class TestALaunchIsClaimedAsUnresolvedBeforeItIsSent(unittest.TestCase):
    """The protection must exist before the event does: a timeout, Ctrl-C, a
    closed terminal or a full disk after sending must never leave a claim that
    dies with its launcher while iTerm2 may still run the launch."""

    SIDS = [f"4f2b91ac-1111-4222-8333-{i:012d}" for i in range(40)]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        self.addCleanup(os.environ.pop, "CCWHO_DIR", None)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tables = []
        terms = runner.engine.terms     # put back on the module it was taken from
        real = (terms.app_snapshot, runner.subprocess.run)
        self.addCleanup(lambda: (setattr(terms, "app_snapshot", real[0]),
                                 setattr(runner.subprocess, "run", real[1])))
        runner.engine.terms.app_snapshot = lambda: self.tables.append(1) or ITERM_TABLE
        self.addCleanup(setattr, runner, "_boot_id", runner._boot_id)
        runner._boot_id = lambda: "THIS-BOOT"
        for sid in self.SIDS:
            runner.claim_launch(sid)

    def held_while_iterm2_lives(self, sid):
        # the launcher (this process) counted as gone; iTerm2 4242 in the table
        return not runner.claim_launch(sid, now=time.time() + 3600, alive=lambda p: p == 4242)

    def test_a_timeout_leaves_every_claim_on_that_iterm2_after_one_lookup(self):
        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        runner.subprocess.run = stuck
        res, why = runner.launch_in_iterm("script", 1.0, self.SIDS)
        self.assertIsNone(res)
        # one, after an outcome that may still run - per batch, never per
        # session
        self.assertEqual(len(self.tables), 1, "one ps after the send, for the batch")
        self.assertTrue(all(self.held_while_iterm2_lives(s) for s in self.SIDS))

    def test_ctrl_c_during_the_launch_leaves_them_held(self):
        def interrupted(cmd, **kw):
            raise KeyboardInterrupt
        runner.subprocess.run = interrupted
        with self.assertRaises(KeyboardInterrupt):
            runner.launch_in_iterm("script", 1.0, self.SIDS[:2])
        self.assertTrue(self.held_while_iterm2_lives(self.SIDS[0]))

    def test_an_answer_holds_them_until_seen_or_their_time_is_up(self):
        # a click's ccwho exits at once, and a session takes seconds to show
        # up in `claude agents`: a second click must not start it again
        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda cmd, **kw: Done()
        res, why = runner.launch_in_iterm("script", 1.0, self.SIDS[:2])
        self.assertIsNotNone(res, why)
        self.assertFalse(runner.claim_launch(self.SIDS[0], alive=lambda p: False))
        self.assertTrue(runner.claim_launch(                                  # control
            self.SIDS[1], now=time.time() + runner.LAUNCH_CLAIM_SECONDS + 1, alive=lambda p: False))

    def outcome(self, returncode, stderr):
        class R:
            pass
        R.returncode, R.stdout, R.stderr = returncode, "", stderr
        runner.subprocess.run = lambda cmd, **kw: R()
        return runner.launch_in_iterm("script", 1.0, self.SIDS[:1])

    def test_a_sender_killed_by_a_signal_leaves_them_held(self):
        # `killall osascript` after a hang: the event may already be in iTerm2
        self.outcome(-15, "")
        self.assertTrue(self.held_while_iterm2_lives(self.SIDS[0]))

    def test_a_failure_with_no_code_leaves_them_held(self):
        self.outcome(1, "osascript: something odd")
        self.assertTrue(self.held_while_iterm2_lives(self.SIDS[0]))

    def test_a_refusal_with_a_code_gives_them_back(self):                     # control
        runner.subprocess.run = refused_at(runner.engine.terms.AE_PROBE)
        runner.launch_in_iterm(runner.engine.terms.ITERM2.run_script("cd /x && claude"), 1.0, self.SIDS[:1])
        self.assertFalse(self.held_while_iterm2_lives(self.SIDS[0]))

    def test_a_timeout_inside_iterm2_says_it_may_still_run(self):
        res, why = self.outcome(1, "execution error: AppleEvent timed out. (-1712)")
        self.assertIsNone(res)
        self.assertIn("did not answer", why)
        self.assertTrue(self.held_while_iterm2_lives(self.SIDS[0]))

    def test_osascript_that_cannot_start_gives_them_back(self):
        runner.subprocess.run = spawn_failing(FileNotFoundError(errno.ENOENT, "No such file or directory"))
        res, why = runner.launch_in_iterm("script", 1.0, self.SIDS[:1])
        self.assertIn("could not drive iTerm2 (FileNotFoundError)", why)
        self.assertFalse(self.held_while_iterm2_lives(self.SIDS[0]))

    def test_a_write_failing_part_way_leaves_none_held(self):
        writes, real = [], runner._write_claim

        def second_fails(sid, record):
            writes.append(sid)
            return False if len(writes) == 2 else real(sid, record)
        runner._write_claim = second_fails
        sent = []
        runner.subprocess.run = lambda cmd, **kw: sent.append(cmd)
        try:
            res, _ = runner.launch_in_iterm("script", 1.0, self.SIDS[:3])
        finally:
            runner._write_claim = real
        self.assertIsNone(res)
        self.assertEqual(sent, [])
        self.assertFalse(self.held_while_iterm2_lives(self.SIDS[0]), "marked, never sent, still held")

    def test_a_claim_that_cannot_be_recorded_sends_nothing(self):
        sent = []
        runner.subprocess.run = lambda cmd, **kw: sent.append(cmd)
        real = runner.claim_unresolved
        runner.claim_unresolved = lambda *a, **k: False          # a full disk, say
        try:
            res, why = runner.launch_in_iterm("script", 1.0, self.SIDS[:1])
        finally:
            runner.claim_unresolved = real
        self.assertIsNone(res)
        self.assertEqual(sent, [])


DEAD = 4_000_000        # above macOS's pid limit: never a live process


class TestAnUnresolvedClaimIsHeldUntilSeenOrITerm2Quits(unittest.TestCase):
    """Owner, 2026-09-29: a fork is worse than a block you can clear. An
    unresolved claim holds while it is being sent; after that while the iTerm2
    it was sent to runs (a ps that could not be read counts as running); it
    goes when the session is seen running, or when that iTerm2 is gone. No
    timing rule: every one tried let an early or late answer release a launch
    still queued in iTerm2."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        self.addCleanup(os.environ.pop, "CCWHO_DIR", None)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])
        self.T = time.time() - 60

    def record(self, launcher, iterm_pid, since=None, send=None):
        since = self.T if since is None else since
        rec = {"pid": launcher, "since": since, "sessionId": self.SID, "unresolved": True,
               "iterm_pid": iterm_pid}
        runner._write_claim(self.SID, dict(rec, send=send) if send else rec)

    def table(self, *rows):
        runner.engine.terms.app_snapshot = lambda: "  PID UID UCOMM\n" + "".join(
            f"{p} {u} {n}\n" for p, u, n in rows)

    def test_a_launch_still_being_sent_holds_it_whatever_else(self):
        # its launcher holds the send lock - for as long as the send takes,
        # suspended or not, and not a moment after it has gone
        held = runner._hold_send()
        self.record(DEAD, DEAD, send=held[0])
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 500))
        runner._let_go_send(held)
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 501))           # control

    def test_a_launcher_that_lives_on_after_sending_does_not_hold_it(self):
        # the list's `o` runs the restore inside the list, which runs for days:
        # its send over - no send lock held - a live launcher holds nothing
        self.record(os.getpid(), DEAD)
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 60))

    def test_after_the_send_that_iterm2_holds_it(self):
        self.record(DEAD, os.getpid())
        self.table((1, 0, "launchd"), (os.getpid(), os.getuid(), "iTerm2"))
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 86400))

    def test_that_iterm2_holds_it_beside_an_older_one(self):
        # two iTerm2s of ours (a beta beside the release): the one it was sent
        # to is not the lowest pid
        self.record(DEAD, os.getpid())
        self.table((1, 0, "launchd"), (2, os.getuid(), "iTerm2"),
                   (os.getpid(), os.getuid(), "iTerm2"))
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 60))

    def test_the_same_pid_as_another_users_iterm2_does_not(self):            # control
        self.record(DEAD, os.getpid())
        self.table((2, os.getuid(), "iTerm2"), (os.getpid(), os.getuid() + 1, "iTerm2"))
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 60))

    def test_a_ps_that_could_not_be_read_holds_it(self):
        # app_snapshot() is "" on a timeout: could not tell is not "gone"
        self.record(DEAD, os.getpid())
        runner.engine.terms.app_snapshot = lambda: ""
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 86400))

    def test_that_iterm2_gone_lets_it_go(self):                                # control
        self.record(DEAD, os.getpid())
        self.table((1, 0, "launchd"), (4242, os.getuid(), "iTerm2"))      # another iTerm2 now
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 60))

    def test_seeing_the_session_running_lets_it_go(self):
        self.record(DEAD, os.getpid())
        self.table((os.getpid(), os.getuid(), "iTerm2"))
        runner.release_claims([self.SID], seen_at=self.T + 1)
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 2))

    def test_a_sighting_older_than_the_claim_does_not(self):                   # control
        # a `claude agents` read can be 30 s old: it may predate this launch
        self.record(DEAD, os.getpid())
        self.table((os.getpid(), os.getuid(), "iTerm2"))
        runner.release_claims([self.SID], seen_at=self.T - 1)
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 2))


class TestOpenReleasesTheClaimOfASessionItSeesRunning(unittest.TestCase):
    SID, ENTRY = TestOpenNeverForksALiveSession.SID, TestOpenNeverForksALiveSession.ENTRY
    tearDown = TestOpenNeverForksALiveSession.tearDown
    _open = TestOpenNeverForksALiveSession._open

    def setUp(self):
        TestOpenNeverForksALiveSession.setUp(self)
        # an earlier launcher, gone; the iTerm2 it sent to (this live pid) still up
        me = os.getpid()
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])
        runner._write_claim(self.SID, {"pid": DEAD, "since": time.time() - 5, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": me})

    def test_a_running_session_releases_its_claim(self):
        self.live = [{"sessionId": self.SID, "tty": "ttys032", "pid": 90266, "windowed": True}]
        self._open(self.SID)
        self.live = []
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 0, out)
        self.assertIn("claude --resume", " ".join(" ".join(c) for c in self.runs))

    def test_a_held_claim_says_what_holds_it(self):                          # control
        # not "timed out": a signal, no code or a refusal part way hold it too
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1)
        self.assertIn("did not finish", out)
        self.assertNotIn("timed out", out)
        self.assertIn("restart iTerm2", out)

    def test_a_launch_that_timed_out_in_this_process_says_so(self):
        # the list's `o` runs the restore inside the list, which lives on: a
        # timed-out launch of it is not "already starting"
        runner._write_claim(self.SID, {"pid": os.getpid(), "since": time.time() - 30,
                                       "sessionId": self.SID, "unresolved": True,
                                       "iterm_pid": os.getpid()})
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1)
        self.assertIn("restart iTerm2", out)
        self.assertNotIn("already starting", out)

    def test_a_launch_in_progress_says_so(self):
        # nothing timed out yet: its launcher is still sending
        held = runner._hold_send()
        self.addCleanup(runner._let_go_send, held)
        runner._write_claim(self.SID, {"pid": os.getpid(), "since": time.time(), "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": os.getpid(), "send": held[0]})
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1)
        self.assertIn("already starting", out)
        self.assertNotIn("restart iTerm2", out)
        self.assertEqual(out.count("not launching"), 1, out)


class TestARestoreRetryThatTimesOutKeepsItsClaims(unittest.TestCase):
    """The second launch - new windows for panes that closed - is a launch too."""

    _F = TestRestoreOpenFillsRestoredPanes
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def test_a_retry_that_times_out_keeps_the_missed_claim(self):
        self.wrote = ""                         # the pane closed: DEAD_SID is missed
        calls = []

        class Done:
            returncode, stdout, stderr = 0, "", ""

        def run(cmd, **kw):
            calls.append(cmd)
            if len(calls) == 2:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return Done()
        runner.subprocess.run = run
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])
        rc, out = self._restore_open()
        self.assertEqual(rc, 1)
        self.assertIn("did not answer", out)
        self.assertFalse(runner.claim_launch(self.DEAD_SID, now=time.time() + 3600,
                                             alive=lambda p: p == 4242))

    def test_a_restore_into_panes_has_time_for_each_pane(self):
        seen = []

        class Done:
            returncode, stderr, stdout = 0, "", "G-B\n"
        runner.subprocess.run = lambda cmd, **kw: seen.append(kw.get("timeout")) or Done()
        self._restore_open()
        self.assertEqual(seen[0], runner.restore_deadline(2))        # 1 window + 1 pane


class TestASaveThatCouldNotAsk(unittest.TestCase):
    """At save level: iTerm2 not asked, the newest manifest's panes carry over -
    and a damaged newest manifest does not stop the save."""

    ROWS = TestSaveAndRestore.ROWS
    setUp, tearDown, _save = TestSaveAndRestore.setUp, TestSaveAndRestore.tearDown, TestSaveAndRestore._save

    def newest(self, sessions):
        d = runner.restore_dir()
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "2026-01-01T0000.json"), "w") as fh:
            json.dump({"version": 1, "savedAt": 1, "count": 1, "skipped": 0, "sessions": sessions}, fh)

    def setUp(self):
        TestSaveAndRestore.setUp(self)
        real = runner.engine.terms.ITERM2.panes
        self.addCleanup(setattr, runner.engine.terms.ITERM2, "panes", real)
        runner.engine.terms.ITERM2.panes = lambda **k: None

    def saved(self):
        d = runner.restore_dir()
        newest = sorted(n for n in os.listdir(d) if n.endswith(".json"))[-1]
        with open(os.path.join(d, newest)) as fh:
            return json.load(fh)

    def test_the_last_known_pane_is_kept(self):
        sid = self.ROWS[0]["sessionId"]
        self.newest([{"sessionId": sid, "pane": "G-1", "tabTitle": "t"}])
        rc, _ = self._save()
        self.assertEqual(rc, 0)
        self.assertEqual([e["pane"] for e in self.saved()["sessions"] if e["sessionId"] == sid], ["G-1"])

    def test_a_damaged_last_save_does_not_stop_it(self):
        self.newest(None)
        rc, _ = self._save()
        self.assertEqual(rc, 0)


class TestAClaimOnAPidThatIsNoLongerITerm2(unittest.TestCase):
    """pids are reused: a claim keyed to an iTerm2 that quit must not be held by
    whatever process got its number."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        self.addCleanup(os.environ.pop, "CCWHO_DIR", None)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])
        # its launcher gone; the pid it was sent to alive - but is it still iTerm2?
        runner._write_claim(self.SID, {"pid": DEAD, "since": time.time(), "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": os.getpid()})

    def test_a_reused_pid_does_not_hold_it(self):
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{os.getpid()} {os.getuid()} python3\n"
        self.assertTrue(runner.claim_launch(self.SID))

    def test_that_iterm2_still_running_does(self):                             # control
        me = os.getpid()
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.assertFalse(runner.claim_launch(self.SID))


class TestARestoreSaysWhatAnUnresolvedClaimMeans(unittest.TestCase):
    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def unresolved(self, sid):
        me = os.getpid()
        runner.engine.terms.app_snapshot = lambda: self.snapshots.append(1) or (
            f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n")
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])
        runner._write_claim(sid, {"pid": DEAD, "since": time.time() - 5, "sessionId": sid,
                                  "unresolved": True, "iterm_pid": me})

    def test_a_timed_out_launch_is_named_as_one_not_as_starting(self):
        self.snapshots = []
        self.unresolved(self.LIVE_SID)
        self.unresolved(self.DEAD_SID)
        rc, out = self._restore_open()
        self.assertNotEqual(rc, 0, "nothing was reopened")
        self.assertIn("restart iTerm2", out)
        self.assertNotIn("timed out", out)
        self.assertNotIn("another ccwho is opening it", out)
        self.assertNotIn("already running or starting", out)
        self.assertEqual(len(self.snapshots), 1, "one process table for the batch")

    def test_a_session_seen_running_releases_its_claim(self):
        self.snapshots = []
        self.unresolved(self.LIVE_SID)
        self.live = [{"sessionId": self.LIVE_SID, "tty": "ttys032", "pid": 90266, "windowed": True}]
        self._restore_open()
        self.assertFalse(unresolved(self.LIVE_SID))

    def test_a_refusal_is_said_by_its_code(self):
        class Refused:
            returncode, stdout = 1, ""
            stderr = "execution error: Can\u2019t get window of /Users/x/secret. (-1728)"
        runner.subprocess.run = lambda cmd, **kw: Refused()
        rc, out = self._restore_open()
        self.assertEqual(rc, 1)
        self.assertIn("(-1728)", out)
        self.assertNotIn("/Users/x/secret", out)


class TestALaunchLetsGoOnlyWhenNothingOfItCanRun(unittest.TestCase):
    """A claim is given back only when none of the launch ran and none can:
    iTerm2 refused Apple Events (-1743) at the script's first event, before
    anything was written. A refusal part way through - -1743 after that
    probe included - leaves the first windows running; a signal or a failure
    with no code may have sent the event."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp, held_while_iterm2_lives = _F.SIDS, _F.setUp, _F.held_while_iterm2_lives

    def launch(self, returncode, stderr, sids):
        class R:
            pass
        R.returncode, R.stdout, R.stderr = returncode, "", stderr
        runner.subprocess.run = lambda cmd, **kw: R()
        return runner.launch_in_iterm("script", 1.0, sids)

    def test_a_refusal_part_way_through_several_windows_keeps_them_all(self):
        # window 9 of 12 refused: 1 to 8 already run `claude --resume`
        res, why = self.launch(1, "execution error: Can’t get current window. (-1728)", self.SIDS[:3])
        self.assertIsNone(res)
        self.assertTrue(all(self.held_while_iterm2_lives(s) for s in self.SIDS[:3]))
        self.assertIn("may still run", why)
        self.assertNotIn("refused", why)

    def test_one_session_refused_keeps_it_too(self):
        # its resume line may be written already: the pane-fill script walks
        # every window after writing, and a window closing then fails it
        res, why = self.launch(1, "execution error: Can\u2019t get window 3. (-1728)", self.SIDS[:1])
        self.assertTrue(self.held_while_iterm2_lives(self.SIDS[0]))
        self.assertIn("may still run", why)

    def test_apple_events_refused_at_the_first_event_gives_them_back(self):  # control
        runner.subprocess.run = refused_at(runner.engine.terms.AE_PROBE)
        res, why = runner.launch_in_iterm(runner.engine.terms.ITERM2.run_script("cd /x && claude"), 1.0, self.SIDS[:1])
        self.assertFalse(self.held_while_iterm2_lives(self.SIDS[0]))
        self.assertIn("refused (-1743)", why)

    def test_an_outcome_it_cannot_read_says_the_launch_may_still_run(self):
        for rc, err in ((-15, ""), (1, "osascript: something odd")):
            res, why = self.launch(rc, err, self.SIDS[:1])
            self.assertIsNone(res)
            self.assertIn("may still run", why)
            self.assertNotIn("refused", why)
            self.assertNotIn("something odd", why)
            self.assertNotIn("-15", why, "a signal's number read as an exit code")

    def test_a_disk_that_fills_part_way_leaves_none_held(self):
        # the rollback must not need the disk space whose lack it rolls back
        writes, real = [], runner._write_claim

        def fills(sid, record):
            writes.append(sid)
            return real(sid, record) if len(writes) == 1 else False
        runner._write_claim = fills
        sent = []
        runner.subprocess.run = lambda cmd, **kw: sent.append(cmd)
        try:
            res, _ = runner.launch_in_iterm("script", 1.0, self.SIDS[:3])
        finally:
            runner._write_claim = real
        self.assertIsNone(res)
        self.assertEqual(sent, [])
        self.assertFalse(any(self.held_while_iterm2_lives(s) for s in self.SIDS[:3]))


class TestAnAttachRefusalIsSaidByItsCode(unittest.TestCase):
    SID, ENTRY = TestOpenNeverForksALiveSession.SID, TestOpenNeverForksALiveSession.ENTRY
    setUp, tearDown = TestOpenNeverForksALiveSession.setUp, TestOpenNeverForksALiveSession.tearDown
    _open = TestOpenNeverForksALiveSession._open

    def test_the_code_not_the_text(self):
        self.live = [{"sessionId": self.SID, "tty": "", "pid": 90266, "kind": "background"}]

        class Refused:
            returncode, stdout = 1, ""
            stderr = "execution error: Can\u2019t get window of /Users/x/secret. (-1728)"
        runner.subprocess.run = lambda cmd, **kw: Refused()
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1)
        self.assertIn("(-1728)", out)
        self.assertNotIn("/Users/x/secret", out)


class TestACallerSaysALaunchMayStillRun(unittest.TestCase):
    """What `ccwho open` and `restore --open` print for an outcome that may
    still run: not osascript's text, never a negative exit code (a signal's
    number), and not "refused" - but for -1743 anywhere but the script's
    first event: "refused" ("part way through" when osascript placed it),
    with MAY_STILL_RUN."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def fails(self, returncode, stderr=None, run=None):
        class R:
            pass
        R.returncode, R.stdout, R.stderr = returncode, "", stderr
        runner.subprocess.run = run or (lambda cmd, **kw: R())
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])

    def held(self, sid):
        return not runner.claim_launch(sid, now=time.time() + 3600, alive=lambda p: p == 4242)

    def test_a_restore_killed_by_a_signal(self):
        self.fails(-15, "")
        rc, out = self._restore_open()
        self.assertEqual(rc, 1)
        self.assertNotIn("refused", out)
        self.assertIn("may still run", out)
        self.assertTrue(self.held(self.LIVE_SID) and self.held(self.DEAD_SID))

    def test_an_open_that_failed_with_no_code(self):
        self.fails(1, "osascript: something odd at /Users/x/secret")
        entry = {"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"}
        self.write_manifest([entry])
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.open_session([self.DEAD_SID])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("may still run", text)
        self.assertNotIn("/Users/x/secret", text)
        self.assertTrue(self.held(self.DEAD_SID))

    def test_a_refusal_is_still_called_one(self):
        # no place given for it: what came before it may have been written
        self.fails(1, "Not authorized to send Apple events to iTerm2. (-1743)")
        rc, out = self._restore_open()
        self.assertEqual(rc, 1)
        self.assertIn("refused (-1743)", out)
        self.assertTrue(self.held(self.LIVE_SID) and self.held(self.DEAD_SID))

    def test_a_refusal_at_the_probe_gives_it_back(self):                    # control
        self.fails(1, run=refused_at(runner.engine.terms.AE_PROBE))
        self.write_manifest([{"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"}])
        rc, out = self._restore_open()
        self.assertEqual(rc, 1)
        self.assertIn("refused (-1743)", out)
        self.assertFalse(self.held(self.DEAD_SID))


class TestTheListSaysWhatARestoreLeftWaiting(unittest.TestCase):
    """The list's `o` shows one line for a whole restore. A session whose
    earlier launch timed out is not one that "could not resume" - that line
    loses the advice to restart iTerm2."""

    _F = TestARestoreSaysWhatAnUnresolvedClaimMeans
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, unresolved = _F.setUp, _F.tearDown, _F.write_manifest, _F.unresolved

    def reopen(self):
        self.snapshots = []
        return runner.reopen_saved()

    def test_one_opened_and_one_waiting(self):
        self.snapshots = []
        self.unresolved(self.LIVE_SID)
        said = self.reopen()
        self.assertNotIn("could not resume", said)
        self.assertIn("restart iTerm2", said)

    def test_one_that_cannot_resume_still_says_so(self):                  # control
        self.on_disk.discard(self.LIVE_SID)
        said = self.reopen()
        self.assertIn("could not resume", said)

    def test_one_running_and_one_waiting_is_not_all_running(self):
        self.snapshots = []
        self.unresolved(self.DEAD_SID)
        self.live = [{"sessionId": self.LIVE_SID, "tty": "ttys032", "pid": 90266, "windowed": True}]
        said = self.reopen()
        self.assertNotIn("already running", said)
        self.assertIn("restart iTerm2", said)

    def test_only_waiting_ones_end_on_the_advice(self):
        self.snapshots = []
        self.unresolved(self.LIVE_SID)
        self.unresolved(self.DEAD_SID)
        said = self.reopen()
        self.assertIn("restart iTerm2", said)
        self.assertNotIn("nothing in that manifest", said)


class TestARestoreRetryRefusalIsSaidByItsCode(unittest.TestCase):
    """The second launch - new windows for panes that closed - prints a
    refusal as its code too: osascript's message quotes paths."""

    _F = TestRestoreOpenFillsRestoredPanes
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def test_the_retry_refused(self):
        self.wrote = ""                         # the pane closed: DEAD_SID is missed
        calls = []

        class Done:
            returncode, stdout, stderr = 0, "", ""

        class Refused:
            returncode, stdout = 1, ""
            stderr = "execution error: Can’t get window of /Users/x/secret. (-1728)"
        runner.subprocess.run = lambda cmd, **kw: calls.append(cmd) or (Done() if len(calls) == 1 else Refused())
        rc, out = self._restore_open()
        self.assertEqual(len(calls), 2)
        self.assertEqual(rc, 1)
        self.assertIn("(-1728)", out)
        self.assertNotIn("/Users/x/secret", out)


class TestASightingIsTakenBeforeTheScan(unittest.TestCase):
    """release_claims lets go of claims older than the sighting. The sighting
    is the moment the scan STARTED: `claude agents` can take 30 s, and a claim
    made while it ran may be a new launch of a session that has since ended."""

    SID, ENTRY = TestOpenNeverForksALiveSession.SID, TestOpenNeverForksALiveSession.ENTRY
    tearDown = TestOpenNeverForksALiveSession.tearDown
    _open = TestOpenNeverForksALiveSession._open

    def setUp(self):
        TestOpenNeverForksALiveSession.setUp(self)
        me = os.getpid()
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])
        calls = []

        def slow_collect(cache=None, status=None):
            # seen by the first scan only: a later one (jump's) starts after
            # the claim, and lets go of it rightly if it sees the session
            rows = [] if calls else [{"sessionId": self.SID, "tty": "ttys032", "pid": 90266,
                                      "windowed": True}]
            if not calls:
                # its rows were read; now another ccwho launches the session
                time.sleep(0.02)
                runner._write_claim(self.SID, {"pid": DEAD, "since": time.time(), "sessionId": self.SID,
                                               "unresolved": True, "iterm_pid": me})
                time.sleep(0.02)
            calls.append(1)
            if status is not None:
                status["source_ok"] = True
            return rows, 0
        runner.engine.collect = slow_collect

    def test_open_keeps_a_claim_made_during_its_scan(self):
        self._open(self.SID)
        self.assertTrue(unresolved(self.SID))

    def test_restore_keeps_a_claim_made_during_its_scan(self):
        d = os.path.join(self.tmp, "restore")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            runner.restore(["--open", "--from", os.path.join(d, "2026-08-01T0001.json")])
        self.assertTrue(unresolved(self.SID))


class TestEveryScanLetsGoOfWhatItSeesRunning(unittest.TestCase):
    """A launch that timed out and then ran is seen by the 15-minute save and
    by the list all day. Released only by `open` and `restore --open`, its
    claim outlived the session: reopening it later said "restart iTerm2"."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    JOB = "99999999-9999-4999-8999-999999999999"

    def setUp(self):
        TestSaveAndRestore.setUp(self)
        self.addCleanup(TestSaveAndRestore.tearDown, self)
        me = os.getpid()
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])
        runner._write_claim(self.SID, {"pid": DEAD, "since": time.time() - 5, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": me})

    def rows(self, rows):
        def fake_collect(cache=None, status=None):
            if status is not None:
                status["source_ok"] = True
            return list(rows), 0
        runner.engine.collect = fake_collect

    def free(self):
        return runner.claim_launch(self.SID, now=time.time() + 3600, alive=lambda p: p == os.getpid())

    def test_a_save_that_sees_it(self):
        self.rows([dict(TestSaveAndRestore.ROWS[0], sessionId=self.SID)])
        TestSaveAndRestore._save(self)
        self.assertTrue(self.free())

    def test_a_save_that_does_not_see_it(self):                             # control
        self.rows([dict(TestSaveAndRestore.ROWS[0], sessionId=self.JOB)])
        TestSaveAndRestore._save(self)
        self.assertFalse(self.free())

    def test_a_terminal_parked_under_a_job_is_seen_running(self):
        # ctrl+b: the terminal has no row of its own, the job row names it
        self.rows([dict(TestSaveAndRestore.ROWS[0], sessionId=self.JOB, parked=[self.SID])])
        TestSaveAndRestore._save(self)
        self.assertTrue(self.free())

    def test_every_scan_in_ccwho_goes_through_the_one_that_lets_go(self):
        import ast
        here = os.path.dirname(os.path.abspath(__file__))
        tree = ast.parse(open(os.path.join(here, "ccwho.py")).read())
        outside, seen = [], 0
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            for node in ast.walk(fn):
                # any receiver: tick() scans with the hot-reloaded engine, `eng`
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "collect"):
                    seen += 1
                    if fn.name != "scan":
                        outside.append(f"{fn.name}:{node.lineno}")
        self.assertGreater(seen, 0, "the scan found no collect at all")        # control
        self.assertEqual(outside, [])


class TestTheWatchLoopLetsGoOfWhatItSeesRunning(unittest.TestCase):
    """`ccwho` and `ccwho --watch` scan through tick(), with the engine they
    reload - a scan like any other."""

    _F = TestEveryScanLetsGoOfWhatItSeesRunning
    SID, JOB, setUp, rows, free = _F.SID, _F.JOB, _F.setUp, _F.rows, _F.free

    def tick(self):
        real = runner.usage_snapshot
        runner.usage_snapshot = lambda rows, now=None, record=True: {"state": "unknown"}
        try:
            return runner.tick({"watch": False, "ticks": 0}, [], False)
        finally:
            runner.usage_snapshot = real

    def test_a_tick_that_sees_it(self):
        self.rows([dict(TestUsageInTheTable.ROWS[0], sessionId=self.SID)])
        self.tick()
        self.assertTrue(self.free())

    def test_a_tick_that_does_not(self):                                   # control
        self.rows([dict(TestUsageInTheTable.ROWS[0], sessionId=self.JOB)])
        self.tick()
        self.assertFalse(self.free())


class TestNoPsRunsWhileTheClaimsAreLocked(unittest.TestCase):
    """A launch waits up to CLAIMS_LOCK_SECONDS for the claims lock and a
    scan tries it once: a ps under it (up to 20 s on a loaded Mac) would
    stall a click, and make every scan skip its releases."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_the_table_is_taken_with_the_lock_free(self):
        me = os.getpid()
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T - 60, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": me})
        free = []

        def snapshot():
            fd = os.open(os.path.join(self.tmp, "launching", ".lock"), os.O_RDWR | os.O_CREAT)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                free.append(True)
            except OSError:
                free.append(False)
            finally:
                os.close(fd)
            return f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        runner.engine.terms.app_snapshot = snapshot
        self.assertFalse(runner.claim_launch(self.SID, now=self.T))
        self.assertTrue(free and all(free), free)


class TestAClaimWithTimesNoCcwhoWrites(unittest.TestCase):
    """A claim is read off disk: times no ccwho sets - not finite, more than
    FAR_FUTURE_SECONDS ahead, ending before they start - must not hold a
    session for good, where neither an iTerm2 restart nor a reboot clears
    it."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def claim(self, **rec):
        runner._write_claim(self.SID, dict({"pid": DEAD, "sessionId": self.SID}, **rec))

    def test_an_endless_send(self):
        self.claim(since=self.T, unresolved=True, iterm_pid=DEAD, until=float("inf"))
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 1))

    def test_an_until_far_off_is_no_send(self):
        # only a send lock held is "being sent": an until, however far off,
        # holds nothing - the claim's iTerm2 does
        self.claim(since=self.T, unresolved=True, iterm_pid=DEAD, until=self.T + 365 * 86400)
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 1))

    def test_an_until_far_off_on_a_live_iterm2(self):                          # control
        me = os.getpid()
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.claim(since=self.T, unresolved=True, iterm_pid=me, until=self.T + 365 * 86400)
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 1))

    def test_a_launch_from_the_day_after_tomorrow(self):
        self.claim(since=self.T + 2 * 86400, launched=True)
        self.assertTrue(runner.claim_launch(self.SID, now=self.T))

    def test_a_sighting_clears_a_launch_from_the_day_after_tomorrow(self):
        self.claim(since=self.T + 2 * 86400, launched=True)
        runner.release_claims([self.SID], self.T)
        self.assertFalse(os.path.exists(runner._claim_path(self.SID)))

    def test_a_send_that_ends_before_it_starts(self):
        me = os.getpid()
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.claim(since=self.T, unresolved=True, iterm_pid=me, until=self.T - 10)
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 1))

    def test_a_sighting_that_ends_next_year(self):
        # it would refuse every decision for a year
        self.claim(since=self.T, seen=True, until=self.T + 365 * 86400)
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 1, decided_at=self.T + 1))

    def test_a_claim_in_range_still_holds(self):                          # control
        me = os.getpid()
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.claim(since=self.T, unresolved=True, iterm_pid=me)
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 1))


class TestASightingOutranksAnOlderDecision(unittest.TestCase):
    """B reads "not running" and stalls; A launches the session; a scan sees
    it running and lets go of A's claim. B, deciding on its older read, must
    not take the free claim and launch it a second time."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_decision_older_than_the_sighting_is_refused(self):
        runner.claim_launch(self.SID, now=self.T + 1)                      # A
        runner.release_claims([self.SID], self.T + 2)                     # seen running
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 3, decided_at=self.T))

    def test_a_decision_newer_than_the_sighting_is_not(self):             # control
        runner.claim_launch(self.SID, now=self.T - 4)
        runner.release_claims([self.SID], self.T - 3)
        time.sleep(0.01)
        self.assertTrue(runner.claim_launch(self.SID, decided_at=time.time()))

    def test_a_sighting_goes_when_the_session_is_seen_long_after(self):
        runner.claim_launch(self.SID, now=self.T - 1)
        runner.release_claims([self.SID], self.T)
        runner.release_claims([self.SID], self.T + 10)
        self.assertTrue(os.path.exists(runner._claim_path(self.SID)))          # control
        runner.release_claims([self.SID], self.T + runner.SIGHTING_SECONDS + 1)
        self.assertFalse(os.path.exists(runner._claim_path(self.SID)))

    def test_a_full_disk_keeps_the_claim(self):
        # the sighting cannot be written: the claim stays until it can be -
        # removed, a decision older than the scan would take the session
        runner.claim_launch(self.SID, now=self.T - 1)
        real = runner._write_claim
        runner._write_claim = lambda sid, record: False
        self.addCleanup(setattr, runner, "_write_claim", real)
        runner.release_claims([self.SID], self.T)
        self.assertTrue(os.path.exists(runner._claim_path(self.SID)))


class TestOpenDecidesOnItsOwnScan(unittest.TestCase):
    SID, ENTRY = TestOpenNeverForksALiveSession.SID, TestOpenNeverForksALiveSession.ENTRY
    tearDown = TestOpenNeverForksALiveSession.tearDown
    _open = TestOpenNeverForksALiveSession._open

    def setUp(self):
        TestOpenNeverForksALiveSession.setUp(self)

        def another_ccwho_launches_it_meanwhile(entry):
            a = time.time()
            runner.claim_launch(self.SID, now=a, pid=DEAD)                 # A
            runner.release_claims([self.SID], a + 0.5)                    # a scan sees it
            return ""
        runner.resume_problem = another_ccwho_launches_it_meanwhile

    def test_open_does_not_launch_it_again(self):
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1, out)
        self.assertNotIn("claude --resume", " ".join(" ".join(c) for c in self.runs))

    def test_restore_does_not_launch_it_again(self):
        d = os.path.join(self.tmp, "restore")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            runner.restore(["--open", "--from", os.path.join(d, "2026-08-01T0001.json")])
        self.assertNotIn("claude --resume", " ".join(" ".join(c) for c in self.runs))


class TestALaunchIsHeldByTheITerm2ThatGotIt(unittest.TestCase):
    """The event goes to whatever iTerm2 runs when it is sent - one restarted
    since the launch began got it. After an outcome that may still run, the
    claim holds on the iTerm2 a table taken after the send shows."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def timed_out(self, after):
        runner.engine.terms.app_snapshot = lambda: after          # the table after the send

        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        runner.subprocess.run = stuck
        runner.launch_in_iterm("script", 1.0, self.SIDS[:1])

    def test_a_restarted_iterm2_holds_it(self):
        # 4242 ran when the launch began (the setUp's table); the one after the
        # send shows only 5151 - restarted meanwhile, and it got the event
        self.timed_out(f"  PID UID UCOMM\n5151 {os.getuid()} iTerm2\n")
        self.assertFalse(runner.claim_launch(self.SIDS[0], now=time.time() + 3600,
                                             alive=lambda p: p == 5151))

    def test_the_one_from_before_the_restart_does_not(self):                  # control
        self.timed_out(f"  PID UID UCOMM\n5151 {os.getuid()} iTerm2\n")
        self.assertTrue(runner.claim_launch(self.SIDS[0], now=time.time() + 3600,
                                            alive=lambda p: p == 4242))


class TestAStaleTableIsNotProofOfAGoneITerm2(unittest.TestCase):
    """A claim is judged gone only on a table taken after it was written - a
    live pid an older table does not show as iTerm2 proves nothing."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def claim(self, iterm_pid):
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T - 60, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": iterm_pid})

    def now_running(self, *pids):
        runner.engine.terms.app_snapshot = lambda: "  PID UID UCOMM\n1 0 launchd\n" + "".join(
            f"{p} {os.getuid()} iTerm2\n" for p in pids)

    def older(self):
        """A table taken before the claim was written, showing no iTerm2."""
        return lambda after=None: ("  PID UID UCOMM\n1 0 launchd\n", self.T - 120)

    def test_a_live_pid_missing_from_an_older_table(self):
        self.claim(os.getpid())
        self.assertFalse(runner.claim_launch(self.SID, now=self.T, refresh=self.older()))

    def test_an_unbound_claim_on_an_older_table(self):
        self.claim(None)
        self.assertFalse(runner.claim_launch(self.SID, now=self.T, refresh=self.older()))

    def test_a_new_table_that_agrees_lets_it_go(self):                    # control
        self.claim(os.getpid())
        self.now_running()
        self.assertTrue(runner.claim_launch(self.SID, now=self.T))

    def test_an_older_table_binds_nothing(self):
        # a batch's table taken before this claim: its iTerm2 may have died
        # before the one that got the launch started - bound to it, the claim
        # would be let go the moment it is seen dead. A new one is taken.
        tables = runner._tables()
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n4242 {os.getuid()} iTerm2\n"
        tables()                                            # taken now, before the claim
        time.sleep(0.01)
        runner._write_claim(self.SID, {"pid": DEAD, "since": time.time(), "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": None})
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n5151 {os.getuid()} iTerm2\n"
        self.assertFalse(runner.claim_launch(self.SID, now=time.time() + 1, refresh=tables,
                                             alive=lambda p: p == 5151))
        with open(runner._claim_path(self.SID)) as fh:
            self.assertEqual(json.load(fh)["iterm_pid"], [5151], "bound on a table older than the claim")


class TestALaunchNotOursAnyMoreIsNotSent(unittest.TestCase):
    """Between claim_launch and the send, a scan saw the session running, or
    another ccwho took the claim over: not a disk failure, and not sent."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_seen_running_in_between(self):
        runner.release_claims(self.SIDS[:1], time.time() + 1)
        sent = []
        runner.subprocess.run = lambda cmd, **kw: sent.append(cmd)
        res, why = runner.launch_in_iterm("script", 1.0, self.SIDS[:1])
        self.assertIsNone(res)
        self.assertEqual(sent, [])
        self.assertNotIn("could not record", why)
        self.assertIn("running", why)

    def test_a_dir_that_cannot_be_written_sends_nothing(self):           # control
        blocker = os.path.join(self.tmp, "file")
        open(blocker, "w").close()
        os.environ["CCWHO_DIR"] = blocker
        sent = []
        runner.subprocess.run = lambda cmd, **kw: sent.append(cmd)
        self.assertTrue(runner.claim_launch(self.SIDS[0]))       # not held by another...
        res, why = runner.launch_in_iterm("script", 1.0, self.SIDS[:1])
        self.assertEqual(sent, [])                                  # ...but not sent
        self.assertIn("could not record", why)


class TestTheListSaysALaunchThatMayStillRun(unittest.TestCase):
    """`o` puts "could not reopen" before a failed restore's last line. One
    that may still run did not fail: that prefix contradicts the line."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest = _F.setUp, _F.tearDown, _F.write_manifest

    def test_a_timed_out_reopen(self):
        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        runner.subprocess.run = stuck
        said = runner.reopen_saved()
        self.assertNotIn("could not reopen", said)
        self.assertIn("restart iTerm2", said)

    def test_a_refused_reopen_still_could_not(self):                      # control
        runner.subprocess.run = refused_at(runner.engine.terms.AE_PROBE)
        self.write_manifest([{"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"}])
        self.assertIn("could not reopen", runner.reopen_saved())


class TestJumpSaysARefusalByItsCode(unittest.TestCase):
    SID, ENTRY = TestOpenNeverForksALiveSession.SID, TestOpenNeverForksALiveSession.ENTRY
    setUp, tearDown = TestOpenNeverForksALiveSession.setUp, TestOpenNeverForksALiveSession.tearDown

    def test_the_code_not_the_text(self):
        self.live = [{"sessionId": self.SID, "pid": 42, "tty": "ttys032",
                      "title": "t", "name": "n", "project": "liveapp"}]

        class Refused:
            returncode, stdout = 1, ""
            stderr = "execution error: Can\u2019t get tab of /Users/x/secret. (-1728)"
        runner.subprocess.run = lambda cmd, **kw: Refused()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.jump(["ttys032"])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("(-1728)", text)
        self.assertNotIn("/Users/x/secret", text)


class TestASendThatHasEndedIsNotBeingSent(unittest.TestCase):
    """"Being sent" holds a claim while osascript runs - not until its deadline.
    A Ctrl-C, or a failure that came back early, ended the send: a retry after
    restarting iTerm2 must not be told it is "already starting"."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def ended(self, sid):
        # the iTerm2 it went to gone: only the send itself could hold it now
        return unresolved(sid) and runner.claim_launch(sid, alive=lambda p: False)

    def test_after_a_failure_that_came_back_early(self):
        class R:
            returncode, stdout, stderr = 1, "", "execution error: Can\u2019t get window 3. (-1728)"
        runner.subprocess.run = lambda cmd, **kw: R()
        res, _ = runner.launch_in_iterm("script", 60.0, self.SIDS[:2])
        self.assertIsNone(res)
        self.assertTrue(self.ended(self.SIDS[0]))

    def test_after_ctrl_c(self):
        # a Ctrl-C that shows no process of the send's ended (here none was
        # made) leaves the record made before it: once its lock is free, not
        # "starting" - held by an iTerm2 of ours as any unresolved launch, and
        # cleared by restarting that iTerm2
        def interrupted(cmd, **kw):
            raise KeyboardInterrupt
        runner.subprocess.run = interrupted
        with self.assertRaises(KeyboardInterrupt):
            runner.launch_in_iterm("script", 60.0, self.SIDS[:1])
        self.assertFalse(runner.claim_launch(self.SIDS[0], alive=lambda p: p == 4242))
        self.assertEqual(runner.why_held(self.SIDS[0]), ("unresolved", 0))
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n5151 {os.getuid()} iTerm2\n"
        self.assertTrue(runner.claim_launch(self.SIDS[0], alive=lambda p: p == 5151))

    def test_while_it_is_being_sent_it_holds(self):                         # control
        seen = []

        class Done:
            returncode, stdout, stderr = 0, "", ""

        def sending(cmd, **kw):
            seen.append((runner.claim_launch(self.SIDS[0], alive=lambda p: False),
                         unresolved(self.SIDS[0])))
            return Done()
        runner.subprocess.run = sending
        runner.launch_in_iterm("script", 60.0, self.SIDS[:1])
        self.assertEqual(seen, [(False, False)], "held, and said to be starting")

    def test_a_send_from_before_the_mac_started_is_over(self):
        runner.claim_unresolved(self.SIDS[0], 4242, now=time.time() - 30)
        real = runner._boot_id
        self.addCleanup(setattr, runner, "_boot_id", real)
        runner._boot_id = lambda: "ANOTHER-BOOT"
        self.assertTrue(runner.claim_launch(self.SIDS[0], alive=lambda p: True))


class TestARestoreAfterAnEndedSendIsNotStarting(unittest.TestCase):
    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def test_the_second_restore_says_they_are_waiting(self):
        me = os.getpid()
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])

        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        runner.subprocess.run = stuck
        self._restore_open()
        rc, out = self._restore_open()
        self.assertNotIn("already running or starting", out)
        self.assertIn("still waiting on", out)
        self.assertNotEqual(rc, 0)


class TestAClaimNoCcwhoWroteNeverCrashesAScan(unittest.TestCase):
    """A claim is read off disk. Whatever is in it - a number too big for a
    float, a pid no process can have, a sighting with no time - it is "no
    claim", taken over or removed: never an exception out of every scan."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def raw(self, text):
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, self.SID + ".json"), "w") as fh:
            fh.write(text)

    def survives(self):
        self.assertFalse(unresolved(self.SID))
        runner.drop_claims([self.SID])
        runner.release_claims([self.SID], time.time())
        self.assertFalse(os.path.exists(runner._claim_path(self.SID)), "a record no ccwho wrote stayed")
        self.assertTrue(runner.claim_launch(self.SID))

    def test_a_since_too_big_for_a_float(self):
        self.raw('{"pid": 1, "since": 1' + "0" * 400 + ', "sessionId": "x"}')
        self.survives()

    def test_an_until_too_big_for_a_float(self):
        self.raw('{"pid": 1, "since": 1, "unresolved": true, "until": 1' + "0" * 400 + "}")
        self.survives()

    def test_a_sighting_with_no_time(self):
        self.raw('{"seen": true, "sessionId": "x"}')
        self.survives()

    def test_pids_no_process_can_have(self):
        for rec in ({"pid": 2 ** 31, "since": self.T},
                    {"pid": 1e10, "since": self.T},
                    {"pid": 0, "since": self.T},           # os.kill(0, 0) is our process group
                    {"pid": -1, "since": self.T},
                    {"pid": DEAD, "since": self.T - 60, "unresolved": True,
                     "iterm_pid": 2 ** 31}):
            self.raw(json.dumps(rec))
            self.assertTrue(runner.claim_launch(self.SID), rec)
            with open(runner._claim_path(self.SID)) as fh:
                self.assertEqual(json.load(fh).get("pid"), os.getpid())

    def test_nesting_too_deep_to_parse(self):
        self.raw("[" * 100000)
        self.survives()

    def test_a_record_ccwho_wrote_still_holds(self):                         # control
        self.raw(json.dumps({"pid": os.getpid(), "owner": "x", "since": time.time(),
                             "sessionId": self.SID}))
        self.assertFalse(runner.claim_launch(self.SID))


class TestOldClaimFilesAreSwept(unittest.TestCase):
    """A sighting of a session that ends before the next one is never read
    again; neither is the claim of a launch that never showed up. Every scan
    lists the dir: without a sweep, each such launch leaves a file for good."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    OTHER = "99999999-9999-4999-8999-999999999999"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def sighted(self):
        runner.claim_launch(self.SID, now=self.T - 1)
        runner.release_claims([self.SID], self.T)
        self.assertTrue(os.path.exists(runner._claim_path(self.SID)))

    def test_a_sighting_no_scan_lists_again(self):
        self.sighted()
        runner.release_claims([self.OTHER], self.T + runner.SIGHTING_SECONDS + 1)
        self.assertFalse(os.path.exists(runner._claim_path(self.SID)))

    def test_one_still_young_stays(self):                                  # control
        self.sighted()
        runner.release_claims([self.OTHER], self.T + runner.SIGHTING_SECONDS - 1)
        self.assertTrue(os.path.exists(runner._claim_path(self.SID)))

    def test_a_launch_that_may_still_run_stays(self):                      # control
        runner.claim_launch(self.SID, now=self.T)
        runner.claim_unresolved(self.SID, os.getpid(), now=self.T)
        runner.release_claims([self.OTHER], self.T + 30 * 86400)
        self.assertTrue(os.path.exists(runner._claim_path(self.SID)))

    def test_an_old_record_no_ccwho_wrote_is_swept(self):
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, self.SID + ".json")
        with open(path, "w") as fh:
            fh.write("[" * 100000)
        os.utime(path, (self.T - 3600, self.T - 3600))
        runner.release_claims([], self.T)
        self.assertFalse(os.path.exists(path))

    def test_a_sweep_runs_once_in_its_time(self):
        # every scan calls release_claims: the dir is walked once per
        # SIGHTING_SECONDS, not once a scan
        walks, real = [], os.scandir
        runner.os.scandir = lambda d: walks.append(d) or real(d)
        self.addCleanup(setattr, runner.os, "scandir", real)
        runner.claim_launch(self.SID, now=self.T)
        for i in range(3):
            runner.release_claims([self.OTHER], self.T + i)
        self.assertEqual(len(walks), 1)
        runner.release_claims([self.OTHER], self.T + runner.SIGHTING_SECONDS + 1)
        self.assertEqual(len(walks), 2)                                       # control
        runner.release_claims([self.OTHER], self.T + runner.SIGHTING_SECONDS + 2)
        self.assertEqual(len(walks), 2, "the marker did not move: every scan walks")
        runner.release_claims([self.OTHER], self.T + 2 * runner.SIGHTING_SECONDS + 2)
        self.assertEqual(len(walks), 3)

    def test_a_scan_reads_no_fresh_claim_it_does_not_list(self):
        runner.claim_launch(self.SID, now=self.T)
        read, real = [], runner._read_claim
        runner._read_claim = lambda sid: read.append(sid) or real(sid)
        self.addCleanup(setattr, runner, "_read_claim", real)
        runner.release_claims([self.OTHER], self.T + 1)
        self.assertEqual(read, [])


class TestASightingCountsUntilItsScanEnded(unittest.TestCase):
    """Scans overlap: C starts, B starts and reads "not running", the session
    appears, C reads it. C's sighting is dated by C's start - earlier than
    B's - yet it saw what B missed. B must not launch it again."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_decision_made_while_the_sighting_scan_ran(self):
        runner.claim_launch(self.SID, now=self.T - 10)                      # A
        runner.release_claims([self.SID], self.T - 5)                      # C: started at T-5, ends now
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 1, decided_at=self.T - 3))  # B

    def test_a_decision_made_after_it_ended(self):                          # control
        runner.claim_launch(self.SID, now=self.T - 10)
        runner.release_claims([self.SID], self.T - 5)
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 2, decided_at=time.time() + 1))


class TestASendStaysProtectedWhileItsIterm2IsLookedUp(unittest.TestCase):
    """After a timeout _sent() takes a ps - up to 20 s - to see which iTerm2
    got the event. The send lock must still hold the claim meanwhile, or a
    retry takes it over from a restarted iTerm2 that holds the launch."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_a_retry_during_the_lookup(self):
        sid, start, other = self.SIDS[0], time.time(), []

        def snapshot():
            if not self.tables:                # _sent's lookup, 20 s into it
                t = threading.Thread(target=lambda: other.append(runner.claim_launch(
                    sid, now=start + 3600, alive=lambda p: False)))
                t.start()
                t.join(10)
            self.tables.append(1)
            return ITERM_TABLE
        runner.engine.terms.app_snapshot = snapshot

        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        runner.subprocess.run = stuck
        runner.launch_in_iterm("script", 1.0, [sid])
        self.assertEqual(other, [False])


class TestASendAcrossASleepIsStillAClaim(unittest.TestCase):
    """A Mac asleep for hours during a send: the record written after it must
    not read as "no claim ccwho wrote": its iTerm2 still holds it."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_two_hours_asleep(self):
        real = time.time
        self.addCleanup(setattr, time, "time", real)

        def slept(cmd, **kw):
            time.time = lambda: real() + 7200
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        runner.subprocess.run = slept
        runner.launch_in_iterm("script", 1.0, self.SIDS[:1])
        self.assertFalse(runner.claim_launch(self.SIDS[0], alive=lambda p: p == 4242))


class TestAnUnrecordedLaunchIsNotBlamedOnAnotherCcwho(unittest.TestCase):
    """Our claim could not be written over a stale one (a full disk): that is
    "could not record", not "another ccwho is opening it"."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_stale_claim_and_a_dir_that_cannot_be_written(self):
        runner.claim_launch("99999999-9999-4999-8999-999999999999")     # the lock file exists
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time() - 1000,
                                       "sessionId": self.SID, "launched": True})
        d = os.path.join(self.tmp, "launching")
        os.chmod(d, 0o555)
        self.addCleanup(os.chmod, d, 0o755)
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        self.assertTrue(runner.claim_launch(self.SID))
        res, why = runner.launch_in_iterm("script", 1.0, [self.SID])
        self.assertIsNone(res)
        self.assertIn("could not record", why)


class TestTheListSaysAWaitingLaunchAsOne(unittest.TestCase):
    """The second `o` after one that timed out ends on restore's own "still
    waiting on" line - that is not a failure either."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest = _F.setUp, _F.tearDown, _F.write_manifest

    def test_the_second_reopen(self):
        me = os.getpid()
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])

        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        runner.subprocess.run = stuck
        runner.reopen_saved()
        said = runner.reopen_saved()
        self.assertNotIn("could not reopen", said)
        self.assertIn("restart iTerm2", said)


class TestARestoreTakesOneNewTableForAllItsClaims(unittest.TestCase):
    """A claim its table cannot settle takes a new one - once for the batch,
    not once a session."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def test_two_claims_no_table_can_settle(self):
        snapshots, at_send = [], []
        runner.engine.terms.app_snapshot = lambda: snapshots.append(1) or "  PID UID UCOMM\n1 0 launchd\n"
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])
        for sid in (self.LIVE_SID, self.DEAD_SID):
            runner._write_claim(sid, {"pid": DEAD, "since": time.time() - 60, "sessionId": sid,
                                      "unresolved": True, "iterm_pid": None})

        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda *a, **k: at_send.append(len(snapshots)) or Done()
        self._restore_open()
        # one table for the claims, taken when the first needed it; none at the send
        self.assertEqual(at_send, [1])

    def test_claims_that_need_no_table(self):                                # control
        snapshots, at_send = [], []
        runner.engine.terms.app_snapshot = lambda: snapshots.append(1) or "  PID UID UCOMM\n1 0 launchd\n"
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])

        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda *a, **k: at_send.append(len(snapshots)) or Done()
        self._restore_open()
        self.assertEqual(at_send, [0])


class TestAnOsascriptFailureIsWordedTheSameEverywhere(unittest.TestCase):
    """-1712 is iTerm2 not answering - not a refusal; a negative exit status
    is a signal, not a number to show."""

    SID, ENTRY = TestOpenNeverForksALiveSession.SID, TestOpenNeverForksALiveSession.ENTRY

    def test_the_words(self):
        say = runner.engine.terms.ITERM2.trouble
        self.assertNotIn("refused", say(1, "execution error: AppleEvent timed out. (-1712)"))
        self.assertIn("did not answer", say(1, "execution error: AppleEvent timed out. (-1712)"))
        self.assertNotIn("-15", say(-15, ""))
        self.assertIn("exit 1", say(1, "osascript: something broke"))
        self.assertNotIn("signal", say(1, "osascript: something broke"))
        self.assertIn("(-1728)", say(1, "Can\u2019t get window of /Users/x/secret. (-1728)"))  # control
        self.assertNotIn("/Users/x/secret", say(1, "Can\u2019t get window of /Users/x/secret. (-1728)"))

    def test_jump_uses_them(self):
        f = TestOpenNeverForksALiveSession
        f.setUp(self)
        self.addCleanup(f.tearDown, self)
        self.live = [{"sessionId": f.SID, "pid": 42, "tty": "ttys032",
                      "title": "t", "name": "n", "project": "liveapp"}]

        class Stuck:
            returncode, stdout, stderr = 1, "", "execution error: AppleEvent timed out. (-1712)"
        runner.subprocess.run = lambda cmd, **kw: Stuck()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.jump(["ttys032"])
        self.assertEqual(rc, 1)
        self.assertNotIn("refused", out.getvalue() + err.getvalue())
        self.assertIn("did not answer", out.getvalue() + err.getvalue())


class TestALaunchOutranksAnOlderDecision(unittest.TestCase):
    """A launch made after a caller decided "not running" holds against that
    caller - however slow its scan was, not only for 90 s."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def launched(self):
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": self.T,
                                       "sessionId": self.SID, "launched": True})

    def test_a_scan_slower_than_the_claim_time(self):
        self.launched()
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 120, decided_at=self.T - 1))

    def test_a_decision_after_the_launch(self):                              # control
        self.launched()
        self.assertTrue(runner.claim_launch(self.SID, now=self.T + 120, decided_at=self.T + 1))


class TestAReceiverThatDiedTookItsEventWithIt(unittest.TestCase):
    """-609 and -600: the iTerm2 the event went to is gone, and the event with
    it. A new iTerm2 - the one you started - never had it: the claim stays on
    the dead one, and goes."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def after(self, outcome):
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n5151 {os.getuid()} iTerm2\n"
        runner.subprocess.run = outcome
        runner.launch_in_iterm("script", 1.0, self.SIDS[:1])
        return runner.claim_launch(self.SIDS[0], now=time.time() + 3600, alive=lambda p: p == 5151)

    def test_connection_invalid(self):
        class R:
            returncode, stdout, stderr = 1, "", "execution error: Connection is invalid. (-609)"
        self.assertTrue(self.after(lambda cmd, **kw: R()))

    def test_a_timeout_binds_it_to_the_iterm2_after_the_send(self):           # control
        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        self.assertFalse(self.after(stuck))


class TestAFailedWriteOverAnOldSightingIsADiskProblem(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_it_says_could_not_record(self):
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True, "since": self.T - 100,
                                       "until": self.T - 99})
        real = runner._write_claim
        runner._write_claim = lambda sid, record: False                   # the disk is full
        self.addCleanup(setattr, runner, "_write_claim", real)
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        self.assertTrue(runner.claim_launch(self.SID, decided_at=self.T))
        res, why = runner.launch_in_iterm("script", 1.0, [self.SID])
        self.assertIn("could not record", why)


class TestOneReopenAtATime(unittest.TestCase):
    """`o` runs the restore in a thread, and takes its output by swapping the
    process's stdout: two at once mixed their lines and left stdout on a
    buffer. A second `o` while one runs is told so."""

    def test_a_second_while_one_runs(self):
        started, go, said = threading.Event(), threading.Event(), []
        real = runner.restore

        def slow(argv):
            started.set()
            go.wait(10)
            print("opened 1 window(s). each is at its project, resuming its own session.")
            return 0
        runner.restore = slow
        self.addCleanup(setattr, runner, "restore", real)
        before = sys.stdout
        t = threading.Thread(target=lambda: said.append(runner.reopen_saved()))
        t.start()
        self.assertTrue(started.wait(10))
        second = runner.reopen_saved()
        go.set()
        t.join(10)
        self.assertIn("already", second)
        self.assertIn("reopened 1", said[0])
        self.assertIs(sys.stdout, before)
        self.assertIn("reopened", runner.reopen_saved())                     # control: free again


class TestDecidedAtIsTheScanStart(unittest.TestCase):
    """What a caller decides from its scan, it decided when the scan STARTED:
    a launch and a sighting while its `claude agents` read ran are newer."""

    _F = TestOpenNeverForksALiveSession
    SID, ENTRY, tearDown, _open = _F.SID, _F.ENTRY, _F.tearDown, _F._open

    def setUp(self):
        self._F.setUp(self)

        def slow_read(cache=None, status=None):
            # our read said not running; meanwhile another ccwho launched it
            # and a scan that started after ours saw it
            time.sleep(0.01)
            a = time.time()
            runner.claim_launch(self.SID, now=a, pid=DEAD)
            runner.release_claims([self.SID], a + 0.001)
            time.sleep(0.01)
            status["source_ok"] = True
            return [], 0
        runner.engine.collect = slow_read

    def test_open_does_not_launch_it_again(self):
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1, out)
        self.assertNotIn("claude --resume", " ".join(" ".join(c) for c in self.runs))

    def test_restore_does_not_launch_it_again(self):
        d = os.path.join(self.tmp, "restore")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            runner.restore(["--open", "--from", os.path.join(d, "2026-08-01T0001.json")])
        self.assertNotIn("claude --resume", " ".join(" ".join(c) for c in self.runs))


class TestA1712EndsTheSend(unittest.TestCase):
    """-1712 - the usual outcome of a big restore - ends the send like any
    other: a retry after restarting iTerm2 is not "already starting"."""

    _F = TestASendThatHasEndedIsNotBeingSent
    SIDS, setUp, ended = _F.SIDS, _F.setUp, _F.ended

    def test_after_iterm2_timed_out_inside(self):
        class R:
            returncode, stdout = 1, ""
            stderr = "execution error: iTerm2 got an error: AppleEvent timed out. (-1712)"
        runner.subprocess.run = lambda cmd, **kw: R()
        res, why = runner.launch_in_iterm("script", 60.0, self.SIDS[:1])
        self.assertIsNone(res)
        self.assertIn("may still run", why)
        self.assertTrue(self.ended(self.SIDS[0]))


class TestNoTestReachesTheRealCcwhoDir(unittest.TestCase):
    """A test that pops CCWHO_DIR - or never set it - lands in a temp dir,
    never in the user's own ~/.ccwho (a sweep there deleted real files)."""

    def test_without_ccwho_dir(self):
        old = os.environ.pop("CCWHO_DIR", None)
        try:
            d = runner.ccwho_dir()
        finally:
            if old is not None:
                os.environ["CCWHO_DIR"] = old
        self.assertNotEqual(os.path.realpath(d), os.path.realpath(os.path.expanduser("~/.ccwho")))

    def test_with_ccwho_dir(self):                                          # control
        os.environ["CCWHO_DIR"] = "/somewhere/else"
        try:
            self.assertEqual(runner.ccwho_dir(), "/somewhere/else")
        finally:
            os.environ.pop("CCWHO_DIR", None)


class TestASightingAcrossASleepIsStillOne(unittest.TestCase):
    """The list's scan runs across hours of sleep: its sighting spans them.
    That is no send, and no endless one - still a sighting."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_scan_that_started_two_hours_ago(self):
        now = time.time()
        runner._write_claim(self.SID, {"pid": DEAD, "since": now - 7300, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": DEAD})
        runner.release_claims([self.SID], now - 7205)
        self.assertFalse(runner.claim_launch(self.SID, now=now + 1, decided_at=now - 2))

    def test_a_decision_after_it(self):                                    # control
        now = time.time()
        runner._write_claim(self.SID, {"pid": DEAD, "since": now - 7300, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": DEAD})
        runner.release_claims([self.SID], now - 7205)
        self.assertTrue(runner.claim_launch(self.SID, now=now + 2, decided_at=now + 1))


class TestTheSweepRemovesClaimsThatCannotHold(unittest.TestCase):
    """An old unresolved claim from another boot, or on an iTerm2 that is
    gone, holds nothing: swept like a sighting, not kept for good."""

    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp
    IDS = [f"4f2b91ac-1111-4222-8333-{i:012d}" for i in range(5)]

    def old(self, sid, **rec):
        runner._write_claim(sid, dict({"pid": DEAD, "since": self.T - 7200, "sessionId": sid,
                                       "unresolved": True}, **rec))
        old = self.T - 7200
        os.utime(runner._claim_path(sid), (old, old))

    def test_what_the_sweep_takes_and_leaves(self):
        another, dead, sending, unbound, alive = self.IDS
        self.old(another, iterm_pid=os.getpid(), boot="ANOTHER-BOOT")
        self.old(dead, iterm_pid=DEAD)
        held = runner._hold_send()
        self.addCleanup(runner._let_go_send, held)
        self.old(sending, iterm_pid=DEAD, send=held[0])
        self.old(unbound, iterm_pid=None)
        self.old(alive, iterm_pid=os.getpid())
        runner.release_claims([], self.T)
        left = {sid for sid in self.IDS if os.path.exists(runner._claim_path(sid))}
        self.assertEqual(left, {sending, unbound, alive})


class TestTheSweepNeverTakesWhatItsScanSees(unittest.TestCase):
    """A scan that sees a session running puts a sighting in place of its
    claim - the sweep of that same scan must not remove the claim first."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_an_old_launch_seen_now(self):
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": self.T - 7300,
                                       "sessionId": self.SID, "launched": True})
        os.utime(runner._claim_path(self.SID), (self.T - 7300, self.T - 7300))
        runner.release_claims([self.SID], self.T)
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 1, decided_at=self.T - 1))

    def test_one_it_does_not_see_is_swept(self):                             # control
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": self.T - 7300,
                                       "sessionId": self.SID, "launched": True})
        os.utime(runner._claim_path(self.SID), (self.T - 7300, self.T - 7300))
        runner.release_claims(["99999999-9999-4999-8999-999999999999"], self.T)
        self.assertFalse(os.path.exists(runner._claim_path(self.SID)))


class TestASightingAgesFromItsScansEnd(unittest.TestCase):
    """A sighting whose scan crossed a sleep started hours ago and ended now:
    the next scan that sees the session must keep it."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_the_next_scan_keeps_it(self):
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True,
                                       "since": self.T - 7205, "until": self.T})
        runner.release_claims([self.SID], self.T + 1)
        # a decision made during that scan - recent enough to be judged
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 2, decided_at=self.T - 100))

    def test_one_that_ended_long_ago_goes(self):                              # control
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True,
                                       "since": self.T - 800, "until": self.T - 700})
        runner.release_claims([self.SID], self.T)
        self.assertFalse(os.path.exists(runner._claim_path(self.SID)))

    def test_a_decision_older_than_any_sighting_kept(self):
        # its sighting may be swept already: nothing can say it still holds
        self.assertFalse(runner.claim_launch(self.SID, now=self.T,
                                             decided_at=self.T - runner.SIGHTING_SECONDS - 1))
        self.assertTrue(runner.claim_launch(self.SID, now=self.T, decided_at=self.T - 1))   # control


class TestATableOlderThanAClaimProvesNothing(unittest.TestCase):
    """A batch shares one new table. A claim written after it - by a
    launcher on an iTerm2 started since - is not "gone" on that table."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    OTHER = "99999999-9999-4999-8999-999999999999"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_claim_newer_than_the_batch_table(self):
        me = os.getpid()
        tables = ["  PID UID UCOMM\n1 0 launchd\n", f"  PID UID UCOMM\n1 0 launchd\n{me} {os.getuid()} iTerm2\n"]
        runner.engine.terms.app_snapshot = lambda: tables.pop(0) if len(tables) > 1 else tables[0]
        refresh = runner._tables()
        runner._write_claim(self.OTHER, {"pid": DEAD, "since": self.T - 60, "sessionId": self.OTHER,
                                         "unresolved": True, "iterm_pid": None})
        self.assertTrue(runner.claim_launch(self.OTHER, now=self.T, refresh=refresh))  # the batch's table
        time.sleep(0.01)
        runner._write_claim(self.SID, {"pid": DEAD, "since": time.time(), "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": me})
        self.assertFalse(runner.claim_launch(self.SID, now=time.time() + 1, refresh=refresh))


class TestAReceiverThatDiedHoldsItsLaunchBriefly(unittest.TestCase):
    """-600/-609: the event died with its receiver. Windows opened before
    that may run, so the claims hold as launched - the usual short time -
    and never bind to an iTerm2 started since."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_sent_while_no_iterm2_ran(self):
        runner.engine.terms.app_snapshot = lambda: "  PID UID UCOMM\n1 0 launchd\n"

        class R:
            returncode, stdout, stderr = 1, "", "execution error: iTerm2 is not running. (-600)"
        runner.subprocess.run = lambda cmd, **kw: R()
        runner.launch_in_iterm("script", 1.0, self.SIDS[:1])
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n5151 {os.getuid()} iTerm2\n"
        self.assertFalse(runner.claim_launch(self.SIDS[0], alive=lambda p: p == 5151))   # briefly
        self.assertTrue(runner.claim_launch(self.SIDS[0], now=time.time() + runner.LAUNCH_CLAIM_SECONDS + 1,
                                            alive=lambda p: p == 5151))


class TestARebindThatCannotBeWrittenStaysSafe(unittest.TestCase):
    """_sent could not write the iTerm2 that got the event (a full disk): what
    the send left must hold on whatever iTerm2 runs - not on the dead one."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_the_rebind_write_fails(self):
        tables = [ITERM_TABLE, f"  PID UID UCOMM\n5151 {os.getuid()} iTerm2\n"]
        runner.engine.terms.app_snapshot = lambda: tables.pop(0) if len(tables) > 1 else tables[0]
        real = runner.claim_unresolved
        calls = []

        def second_fails(*a, **k):
            calls.append(1)
            return real(*a, **k) if len(calls) == 1 else False
        runner.claim_unresolved = second_fails
        self.addCleanup(setattr, runner, "claim_unresolved", real)

        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        runner.subprocess.run = stuck
        runner.launch_in_iterm("script", 1.0, self.SIDS[:1])
        self.assertFalse(runner.claim_launch(self.SIDS[0], now=time.time() + 3600,
                                             alive=lambda p: p == 5151))


class TestTheSweepTouchesOnlyItsOwnFiles(unittest.TestCase):
    """The sweep deletes: only files ccwho writes - <session id>.json and
    its temp files - and only regular files, never through a symlink. One
    entry it cannot read does not stop it."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def dir(self):
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d, exist_ok=True)
        return d

    def aged(self, path):
        old = self.T - 7200
        os.utime(path, (old, old), follow_symlinks=False)

    def file(self, name, text="{}"):
        path = os.path.join(self.dir(), name)
        with open(path, "w") as fh:
            fh.write(text)
        self.aged(path)
        return path

    def test_files_it_did_not_write_stay(self):
        outside = os.path.join(self.tmp, "mine.json")
        with open(outside, "w") as fh:
            fh.write(json.dumps({"seen": True, "since": self.T - 7200}))
        self.aged(outside)                  # old through the link as well
        link = os.path.join(self.dir(), "99999999-9999-4999-8999-999999999999.json")
        os.symlink(outside, link)
        self.aged(link)
        keep = [self.file("README.txt", "hello"), self.file("notes", "x"), link]
        sighting = self.file(self.SID + ".json", json.dumps(
            {"sessionId": self.SID, "seen": True, "since": self.T - 7200, "until": self.T - 7200}))
        tmp = self.file(self.SID + ".json.123.tmp")
        runner.release_claims([], self.T)
        self.assertEqual([p for p in keep if not os.path.lexists(p)], [])
        self.assertTrue(os.path.exists(outside), "followed a symlink out of the dir")
        self.assertFalse(os.path.exists(sighting) or os.path.exists(tmp))       # control

    def test_one_bad_entry_does_not_stop_it(self):
        os.symlink(os.path.join(self.tmp, "nowhere"), os.path.join(self.dir(), "00000000-dangling.json"))
        ids = [f"4f2b91ac-1111-4222-8333-{i:012d}" for i in range(20)]
        for sid in ids:
            self.file(sid + ".json", json.dumps({"sessionId": sid, "seen": True,
                                                 "since": self.T - 7200, "until": self.T - 7200}))
        runner.release_claims([], self.T)
        self.assertEqual([s for s in ids if os.path.exists(runner._claim_path(s))], [])


class TestAnInheritedCcwhoDirIsNotTheTests(unittest.TestCase):
    """CCWHO_DIR exported in the shell that runs ./test is the user's own data
    dir: a pinned module must not use it."""

    def test_a_scan_in_a_test_leaves_it_alone(self):
        x = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, x, True)
        d = os.path.join(x, "launching")
        os.makedirs(d)
        old = time.time() - 7200
        for name, text in (("4f2b91ac-1111-4222-8333-abcdefabcdef.json",
                            json.dumps({"seen": True, "since": old, "until": old})), (".swept", "")):
            with open(os.path.join(d, name), "w") as fh:
                fh.write(text)
            os.utime(os.path.join(d, name), (old, old))
        before = sorted((n, os.stat(os.path.join(d, n)).st_mtime) for n in os.listdir(d))
        here = os.path.dirname(os.path.abspath(__file__))
        r = subprocess.run([sys.executable, "-B", "-m", "unittest", "test_runner.TestUsageInTheTable"],
                           cwd=here, capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, CCWHO_DIR=x, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        after = sorted((n, os.stat(os.path.join(d, n)).st_mtime) for n in os.listdir(d))
        self.assertEqual(after, before)


class TestAJudgementThatBreaksHoldsTheClaim(unittest.TestCase):
    """Only a record that cannot be read is "no claim". An error while
    judging one - an engine the list reloaded, with a function renamed - must
    hold it, not free it: the guard fails closed."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_an_engine_that_raises(self):
        me = os.getpid()
        rec = {"pid": DEAD, "since": self.T - 60, "sessionId": self.SID,
               "unresolved": True, "iterm_pid": me}
        runner._write_claim(self.SID, rec)
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        def renamed(*a):
            raise AttributeError("iterm_app_pids")
        testkit.patch(self, runner.engine.terms.ITERM2, "pids", renamed)
        self.assertFalse(runner.claim_launch(self.SID, now=self.T))
        with open(runner._claim_path(self.SID)) as fh:
            self.assertEqual(unstamped(json.load(fh)), rec)


class TestALaunchThatNeverStartedLetsGo(unittest.TestCase):
    """osascript refused before it ran (an argument subprocess cannot pass -
    a NUL byte): nothing was sent. The claims go, and it says so by type."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp, held_while_iterm2_lives = _F.SIDS, _F.setUp, _F.held_while_iterm2_lives

    def test_an_argument_that_cannot_be_passed(self):
        # the real run, of a harmless program: its argument stops it before a process is made
        runner.subprocess.run = lambda cmd, **kw: REAL_RUN(["/usr/bin/true", "a\0b"], **kw)
        res, why = runner.launch_in_iterm("script", 1.0, self.SIDS[:1])
        self.assertIsNone(res)
        self.assertIn("(ValueError)", why)
        self.assertFalse(self.held_while_iterm2_lives(self.SIDS[0]))


class TestAFailedWriteKeepsWhatStillProtects(unittest.TestCase):
    """A sighting refuses decisions older than it. A newer decision whose
    write fails must not delete it - the older one would then launch."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_an_older_decision_is_still_refused(self):
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True,
                                       "since": self.T - 10, "until": self.T - 5})
        real = runner._write_claim
        self.addCleanup(setattr, runner, "_write_claim", real)
        runner._write_claim = lambda sid, record: False
        self.assertTrue(runner.claim_launch(self.SID, now=self.T, decided_at=self.T - 1))  # newer
        runner._write_claim = real
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 1, decided_at=self.T - 8))


class TestADecisionTooOldIsSaidAsOne(unittest.TestCase):
    """A restore whose scan is older than any sighting kept (the Mac slept
    between the scan and the claims) must not report "already running or
    starting" and succeed: nothing runs, nothing was claimed."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def test_restore(self):
        def slept(cache=None, status=None):
            status["source_ok"] = True
            status["seen_at"] = time.time() - runner.SIGHTING_SECONDS - 60
            status["seen_mono"] = runner._mono() - runner.SIGHTING_SECONDS - 60
            return [], 0
        runner.engine.collect = slept
        rc, out = self._restore_open()
        self.assertNotEqual(rc, 0)
        self.assertNotIn("already running or starting", out)
        self.assertIn("again", out)
        self.assertEqual(self.runs, [])


class TestAClaimLaunchTakesAtMostOneNewTable(unittest.TestCase):
    """A claim no table can settle must not keep claim_launch taking tables:
    it ends, and the claim holds. Run in a process of its own with a limit -
    a loop that does not end is killed, and fails the test."""

    def test_a_claim_no_table_can_settle(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        code = (
            "import os, time, ccwho as r\n"
            "T = time.time(); sid = '4f2b91ac-1111-4222-8333-abcdefabcdef'\n"
            "r._write_claim(sid, {'pid': 4000000, 'since': T - 60, 'sessionId': sid, 'unresolved': True,"
            " 'iterm_pid': os.getpid()})\n"
            "print(r.claim_launch(sid, now=T, refresh=lambda after=None: ('  PID UID UCOMM\\n1 0 launchd\\n', T - 120)))\n")
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            r = subprocess.run([sys.executable, "-B", "-c", code], cwd=here, capture_output=True, text=True,
                               timeout=20, env=dict(os.environ, CCWHO_DIR=tmp, PYTHONDONTWRITEBYTECODE="1"))
        except subprocess.TimeoutExpired:
            self.fail("claim_launch did not stop")
        self.assertEqual(r.stdout.strip(), "False", r.stderr[-400:])


class TestAScanWithNoClaimsTakesNoLock(unittest.TestCase):
    """Every tick of the list scans: one that has no claim to look at must
    not wait on the lock another ccwho holds."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def locks(self):
        taken, real = [], runner._claims_locked
        runner._claims_locked = lambda **kw: taken.append(1) or real(**kw)
        self.addCleanup(setattr, runner, "_claims_locked", real)
        return taken

    def test_nothing_it_sees_has_a_claim(self):
        runner.claim_launch("99999999-9999-4999-8999-999999999999", now=self.T)
        runner.release_claims([], self.T)                  # the sweep's first run: its marker
        taken = self.locks()
        runner.release_claims([self.SID], self.T + 1)
        self.assertEqual(taken, [])

    def test_one_it_sees_has_a_claim(self):                                   # control
        runner.claim_launch(self.SID, now=self.T - 1)
        runner.release_claims([], self.T)
        taken = self.locks()
        runner.release_claims([self.SID], self.T + 1)
        self.assertEqual(len(taken), 1)


class TestASightingThatCannotBeWrittenKeepsTheClaim(unittest.TestCase):
    """A full disk when a scan sees a launched session: the claim stays until
    its sighting can be written - removed, an older decision would take it."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_an_older_decision_is_still_refused(self):
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": self.T - 5,
                                       "sessionId": self.SID, "launched": True})
        real = runner._write_claim
        self.addCleanup(setattr, runner, "_write_claim", real)
        runner._write_claim = lambda sid, record: False
        runner.release_claims([self.SID], self.T)
        runner._write_claim = real
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 200, decided_at=self.T - 10))


class TestAnOutputThatCannotBeDecodedIsNotNothingSent(unittest.TestCase):
    """osascript ran; its output is decoded after. A byte the locale cannot
    decode is no reason to think nothing was sent."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp, held_while_iterm2_lives = _F.SIDS, _F.setUp, _F.held_while_iterm2_lives

    def test_a_timeout_said_in_bytes_the_locale_cannot_read(self):
        class R:
            returncode, stdout, stderr = 1, "", "execution error: \ufffd AppleEvent timed out. (-1712)"

        def run(cmd, **kw):
            if kw.get("errors") != "replace":
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            return R()
        runner.subprocess.run = run
        res, why = runner.launch_in_iterm("script", 1.0, self.SIDS[:1])
        self.assertIn("may still run", why)
        self.assertTrue(self.held_while_iterm2_lives(self.SIDS[0]))


class TestALaunchCutOffByAnITerm2ThatQuit(unittest.TestCase):
    """-600/-609: iTerm2 quit during the launch. What it opened may start; the
    rest never will. Held a minute - and said so: not "already starting in
    another window", not success, and not "restart iTerm2", which clears
    nothing here."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def quit(self):
        class R:
            returncode, stdout, stderr = 1, "", "execution error: Connection is invalid. (-609)"
        runner.subprocess.run = lambda cmd, **kw: self.runs.append(cmd) or R()

    def test_the_first_launch_names_the_way_out(self):
        self.quit()
        rc, out = self._restore_open()
        self.assertEqual(rc, 1)
        self.assertIn("try again in", out)
        self.assertNotIn("restart iTerm2", out)

    def test_a_retry_in_that_minute(self):
        self.quit()
        self._restore_open()
        runs = len(self.runs)
        rc, out = self._restore_open()
        self.assertEqual(len(self.runs), runs, "launched again")
        self.assertNotEqual(rc, 0)
        self.assertNotIn("already running or starting", out)
        self.assertNotIn("another ccwho", out)
        self.assertIn("try again in", out)

    def test_an_open_retry_in_that_minute(self):
        self.quit()
        self._restore_open()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.open_session([self.DEAD_SID])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(rc, 1)
        self.assertNotIn("another window", text)
        self.assertIn("try again in", text)


class TestALockThatCannotBeHadIsADiskProblem(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_it_says_could_not_record(self):
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True,
                                       "since": self.T - 100, "until": self.T - 99})

        def no_lock():
            raise OSError("no lock")
        real = runner._claims_locked
        runner._claims_locked = no_lock
        self.addCleanup(setattr, runner, "_claims_locked", real)
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        self.assertTrue(runner.claim_launch(self.SID, decided_at=self.T))
        res, why = runner.launch_in_iterm("script", 1.0, [self.SID])
        self.assertIn("could not record", why)


class TestARestoreThatTurnsStaleLetsGoOfWhatItClaimed(unittest.TestCase):
    """The claims loop crossed the sighting time part way (a sleep): it stops,
    and the claims it took already must not be left behind."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def test_the_first_one_claimed_is_let_go(self):
        seen = {}

        def collect(cache=None, status=None):
            seen["status"] = status
            status["source_ok"] = True
            return [], 0
        runner.engine.collect = collect
        real = runner.resume_problem
        self.addCleanup(setattr, runner, "resume_problem", real)

        def slept_before_the_second(entry):
            if entry.get("sessionId") == self.DEAD_SID:
                seen["status"]["seen_at"] = time.time() - runner.SIGHTING_SECONDS - 60
                seen["status"]["seen_mono"] = runner._mono() - runner.SIGHTING_SECONDS - 60
            return ""
        runner.resume_problem = slept_before_the_second
        rc, out = self._restore_open()
        self.assertNotEqual(rc, 0)
        self.assertNotIn("`ccwho restore --open`", out, "names a command that reopens another save")
        self.assertIn("run it again", out)
        other = []
        t = threading.Thread(target=lambda: other.append(runner.claim_launch(self.LIVE_SID)))
        t.start()
        t.join(10)
        self.assertEqual(other, [True])


class TestTheSweepMatchesTheTempNamesItsWriterUses(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_leftover_of_a_real_write(self):
        names, real = [], os.replace
        runner.os.replace = lambda src, dst: names.append(src) or real(src, dst)
        self.addCleanup(setattr, runner.os, "replace", real)
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True, "since": self.T, "until": self.T})
        runner.os.replace = real
        tmp = os.path.basename(names[0])
        self.assertTrue(runner._CLAIM_FILE.fullmatch(tmp), tmp)
        path = os.path.join(self.tmp, "launching", tmp)
        open(path, "w").close()
        os.utime(path, (self.T - 3600, self.T - 3600))
        runner.release_claims([], self.T)
        self.assertFalse(os.path.exists(path))


class TestAClaimThatCannotBeReadIsNotNoClaim(unittest.TestCase):
    """A claim file that exists and cannot be opened (a root-owned file, the
    list out of file descriptors) holds - only a missing one is no claim."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_permission_denied(self):
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": None})
        with open(runner._claim_path(self.SID), "rb") as fh:
            before = fh.read()
        real = runner._read_claim

        def denied(sid):
            raise PermissionError(13, "Permission denied")
        runner._read_claim = denied
        self.addCleanup(setattr, runner, "_read_claim", real)
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 1))
        with open(runner._claim_path(self.SID), "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_no_file_is_no_claim(self):                                        # control
        self.assertTrue(runner.claim_launch(self.SID, now=self.T))


class TestAClaimFileThatCannotBeReadIsSaidAsOne(unittest.TestCase):
    """A claim file there and unreadable (left by `sudo ccwho open`) holds -
    and says what it is and how to clear it. A scan that sees the session
    running puts a sighting in its place."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def unreadable(self, sid):
        runner._write_claim(sid, {"pid": DEAD, "since": time.time(), "sessionId": sid})
        os.chmod(runner._claim_path(sid), 0)
        self.addCleanup(lambda: os.path.exists(runner._claim_path(sid)) and os.chmod(runner._claim_path(sid), 0o600))

    def test_restore_names_it(self):
        self.unreadable(self.LIVE_SID)
        self.unreadable(self.DEAD_SID)
        rc, out = self._restore_open()
        self.assertNotEqual(rc, 0)
        self.assertNotIn("already running or starting", out)
        self.assertNotIn("another ccwho", out)
        self.assertIn("cannot be read", out)

    def test_open_names_it(self):
        self.unreadable(self.DEAD_SID)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.open_session([self.DEAD_SID])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(rc, 1)
        self.assertNotIn("another window", text)
        self.assertIn("cannot be read", text)

    def test_seen_running_it_becomes_a_sighting(self):
        self.unreadable(self.DEAD_SID)
        runner.release_claims([self.DEAD_SID], time.time() + 1)
        with open(runner._claim_path(self.DEAD_SID)) as fh:
            self.assertIs(json.load(fh).get("seen"), True)


class TestTheListSaysACutOffLaunchAsOne(unittest.TestCase):
    """`o` after -600/-609: not "could not reopen", and restore's name not
    repeated - what it opened may still start."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest = _F.setUp, _F.tearDown, _F.write_manifest

    def said(self, stderr):
        class R:
            returncode, stdout = 1, ""
        R.stderr = stderr
        runner.subprocess.run = lambda cmd, **kw: R()
        return runner.reopen_saved()

    def test_609_and_600(self):
        for code in ("-609", "-600"):
            said = self.said(f"execution error: gone. ({code})")
            self.assertFalse(said.startswith("could not reopen"), said)
            self.assertNotIn("ccwho restore:", said)
            runner.drop_claims([self.LIVE_SID, self.DEAD_SID])
            for sid in (self.LIVE_SID, self.DEAD_SID):
                with contextlib.suppress(OSError):
                    os.remove(runner._claim_path(sid))

    def test_the_list_on_a_retry(self):
        # its last line is what `o` shows: the wait, not "nothing can be reopened"
        self.said("execution error: gone. (-609)")
        again = runner.reopen_saved()
        self.assertIn("try again in", again)
        self.assertNotIn("nothing in that manifest", again)

    def test_a_real_failure_still_could_not(self):                          # control
        runner.subprocess.run = exec_failing()                                    # never ran
        self.assertTrue(runner.reopen_saved().startswith("could not reopen"))


class TestACutOffLaunchSaysHowLongToWait(unittest.TestCase):
    """The wait it names is the hold still left - a retry after it, as told,
    is not refused with the same words."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def test_a_retry_61_s_later(self):
        class R:
            returncode, stdout, stderr = 1, "", "execution error: gone. (-609)"
        runner.subprocess.run = lambda cmd, **kw: R()
        rc, first = self._restore_open()
        self.assertIn(f"try again in {int(runner.LAUNCH_CLAIM_SECONDS)} s", first)
        for sid in (self.LIVE_SID, self.DEAD_SID):          # 61 s have passed
            with open(runner._claim_path(sid)) as fh:
                rec = json.load(fh)
            rec["since"] -= 61
            rec.pop("clock")                                 # a new since: a new stamp
            runner._write_claim(sid, rec)
        rc, out = self._restore_open()
        waits = [int(n) for n in re.findall(r"try again in (\d+) s", out)]
        self.assertTrue(waits, out)
        self.assertTrue(all(w <= runner.LAUNCH_CLAIM_SECONDS - 61 + 1 for w in waits), waits)


class TestOpenSaysItsListIsTooOld(unittest.TestCase):
    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest = _F.setUp, _F.tearDown, _F.write_manifest

    def test_open(self):
        def slept(cache=None, status=None):
            status["source_ok"] = True
            status["seen_at"] = time.time() - runner.SIGHTING_SECONDS - 60
            status["seen_mono"] = runner._mono() - runner.SIGHTING_SECONDS - 60
            return [], 0
        runner.engine.collect = slept
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.open_session([self.DEAD_SID])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("too old", text)
        self.assertNotIn("already starting", text)
        self.assertEqual(self.runs, [])


class TestAFailedWriteKeepsALaunch(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_an_older_decision_is_still_refused(self):
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": self.T - 100,
                                       "sessionId": self.SID, "launched": True})
        real = runner._write_claim
        runner._write_claim = lambda sid, record: False
        self.addCleanup(setattr, runner, "_write_claim", real)
        self.assertTrue(runner.claim_launch(self.SID, now=self.T, decided_at=self.T - 50))
        runner._write_claim = real
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 1, decided_at=self.T - 150))


class TestTheSweepLeavesJsonThatIsNoSession(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_notes_json_stays(self):
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "notes.json")
        with open(p, "w") as fh:
            fh.write("{}")
        os.utime(p, (self.T - 7200, self.T - 7200))
        runner.release_claims([], self.T)
        self.assertTrue(os.path.exists(p))

    def test_one_entry_that_cannot_be_read_does_not_stop_the_rest(self):
        ids = [f"4f2b91ac-1111-4222-8333-{i:012d}" for i in range(20)]
        bad = "00000000-0000-4000-8000-000000000000"
        for sid in ids + [bad]:
            runner._write_claim(sid, {"sessionId": sid, "seen": True, "since": self.T - 7200,
                                      "until": self.T - 7200})
            os.utime(runner._claim_path(sid), (self.T - 7200, self.T - 7200))
        real_read, real_scan = runner._read_claim, os.scandir

        def read(sid):
            if sid == bad:
                raise PermissionError(13, "Permission denied")
            return real_read(sid)

        def scan_bad_first(d):
            return iter(sorted(real_scan(d), key=lambda e: not e.name.startswith(bad)))
        runner._read_claim, runner.os.scandir = read, scan_bad_first
        self.addCleanup(setattr, runner, "_read_claim", real_read)
        self.addCleanup(setattr, runner.os, "scandir", real_scan)
        runner.release_claims([], self.T)
        self.assertEqual([s for s in ids if os.path.exists(runner._claim_path(s))], [])


class TestARestoreThatFailsBeforeItSendsLetsGo(unittest.TestCase):
    """Claims taken, then an error before the send - the pane lookup raised:
    none of them may stay behind."""

    _F = TestRestoreOpenFillsRestoredPanes
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def test_the_pane_lookup_raised(self):
        def broken(**k):
            raise RuntimeError("panes")
        runner.engine.terms.ITERM2.panes = broken
        with self.assertRaises(RuntimeError):
            self._restore_open()
        other = []
        t = threading.Thread(target=lambda: other.extend(
            [runner.claim_launch(self.LIVE_SID), runner.claim_launch(self.DEAD_SID)]))
        t.start()
        t.join(10)
        self.assertEqual(other, [True, True])


class TestABootIdWithoutCtypesIsUnknown(unittest.TestCase):
    def test_no_ctypes(self):
        import builtins
        real = builtins.__import__

        def no_ctypes(name, *a, **k):
            if name.startswith("ctypes"):
                raise ImportError(name)
            return real(name, *a, **k)
        builtins.__import__ = no_ctypes
        try:
            self.assertIsNone(runner.engine.terms.boot_id.__wrapped__())
        finally:
            builtins.__import__ = real


class TestAnUnreadableClaimNewerThanTheScanStays(unittest.TestCase):
    """A scan replaces a claim it cannot read with a sighting only when the
    file is older than the scan - a newer one may be a launch made since."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_launch_made_during_the_scan(self):
        seen_at = self.T - 30
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": self.T,
                                       "sessionId": self.SID, "launched": True})
        path = runner._claim_path(self.SID)
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o600)
        before = os.stat(path).st_mtime
        runner.release_claims([self.SID], seen_at)
        self.assertEqual(os.stat(path).st_mtime, before, "replaced")
        os.chmod(path, 0o600)
        self.assertFalse(runner.claim_launch(self.SID, now=self.T + 1, decided_at=self.T - 20))


class TestAClaimIsOwnedByTheCallThatMadeIt2(unittest.TestCase):
    """The same thread's earlier launch is not this call's claim: a
    claim_launch that could not lock may not rewrite it and send again."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_an_earlier_launch_and_a_lock_that_failed(self):
        runner.claim_launch(self.SID)
        runner.claim_unresolved(self.SID, 4242)            # an earlier launch, held
        real, calls = runner._claims_locked, []

        def once():
            if not calls:
                calls.append(1)
                raise OSError("busy")
            return real()
        runner._claims_locked = once
        self.addCleanup(setattr, runner, "_claims_locked", real)
        self.assertTrue(runner.claim_launch(self.SID))     # could not lock
        sent = []

        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda cmd, **kw: sent.append(cmd) or Done()
        res, why = runner.launch_in_iterm("script", 1.0, [self.SID])
        self.assertEqual(sent, [], "sent again")
        self.assertIn("could not record", why)


class TestAClockSetBackKeepsALaunchThatMayStillRun(unittest.TestCase):
    """Written while the clock ran two hours fast: an unresolved claim is
    judged by what does not need the clock - its boot, its iTerm2."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_two_hours(self):
        me = os.getpid()
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T + 7200, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": me,
                                       "boot": "THIS-BOOT"})
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.assertFalse(runner.claim_launch(self.SID, now=self.T))

    def test_two_hours_on_an_iterm2_that_is_gone(self):                      # control
        # the iTerm2 it went to is gone, and no send lock is held: a restart
        # clears it, clock or not
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T + 7200, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": DEAD,
                                       "boot": "THIS-BOOT"})
        self.assertTrue(runner.claim_launch(self.SID, now=self.T))


class TestTheListNamesEveryReasonALaunchWasHeld(unittest.TestCase):
    """`o` summarises a restore that opened some: an unreadable claim file
    is not "could not resume", and the wait it names is the one left."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest = _F.setUp, _F.tearDown, _F.write_manifest

    def test_an_unreadable_claim_beside_one_that_opens(self):
        runner._write_claim(self.LIVE_SID, {"pid": DEAD, "since": time.time(), "sessionId": self.LIVE_SID})
        path = runner._claim_path(self.LIVE_SID)
        os.chmod(path, 0)
        self.addCleanup(lambda: os.path.exists(path) and os.chmod(path, 0o600))
        said = runner.reopen_saved()
        self.assertNotIn("could not resume", said)
        self.assertIn("cannot be read", said)

    def test_a_cut_off_launch_61_s_old_beside_one_that_opens(self):
        runner._write_claim(self.LIVE_SID, {"pid": DEAD, "owner": "x", "since": time.time() - 61,
                                            "sessionId": self.LIVE_SID, "launched": True, "cut_off": True})
        said = runner.reopen_saved()
        waits = [int(n) for n in re.findall(r"try again in (\d+) s", said)]
        self.assertTrue(waits and max(waits) <= 30, said)


class TestAnOldCutOffLaunchAgainstAnOlderListIsNotStarting(unittest.TestCase):
    """A cut-off launch past its 90 s still holds against a decision made
    before it: that list is older than the launch - "run it again", not
    "already starting", and never rc 0."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def setUp(self):
        self._F.setUp(self)
        for sid in (self.LIVE_SID, self.DEAD_SID):
            runner._write_claim(sid, {"pid": DEAD, "owner": "x", "since": time.time() - 100,
                                      "sessionId": sid, "launched": True, "cut_off": True})

        def older_list(cache=None, status=None):
            status["source_ok"] = True
            status["seen_at"] = time.time() - 120
            status["seen_mono"] = runner._mono() - 120
            return [], 0
        runner.engine.collect = older_list

    def test_open(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.open_session([self.DEAD_SID])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(rc, 1)
        self.assertNotIn("another window", text)
        self.assertIn("run it again", text)

    def test_restore(self):
        rc, out = self._restore_open()
        self.assertNotEqual(rc, 0)
        self.assertNotIn("already running or starting", out)
        self.assertIn("run it again", out)


class TestALockFailureDropsNothingOfAnEarlierLaunch(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_drop_after_a_lock_that_failed(self):
        runner.claim_launch(self.SID)
        runner.claim_unresolved(self.SID, 4242)            # an earlier launch, held
        with open(runner._claim_path(self.SID), "rb") as fh:
            before = fh.read()
        real, calls = runner._claims_locked, []

        def once():
            if not calls:
                calls.append(1)
                raise OSError("busy")
            return real()
        runner._claims_locked = once
        self.addCleanup(setattr, runner, "_claims_locked", real)
        runner.claim_launch(self.SID)                      # could not lock
        runner.drop_claims([self.SID])
        with open(runner._claim_path(self.SID), "rb") as fh:
            self.assertEqual(fh.read(), before)


class TestARestoreLetsGoFromItsFirstClaim(unittest.TestCase):
    """An error while it sorts the entries - after the first is claimed - lets
    go of that claim too; a cwd that is not a path is said, not raised."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open
    _script = _F._script

    def test_an_error_on_the_second_entry(self):
        real = runner.resume_problem
        self.addCleanup(setattr, runner, "resume_problem", real)

        def second_breaks(entry):
            if entry.get("sessionId") == self.DEAD_SID:
                raise RuntimeError("broken entry")
            return ""
        runner.resume_problem = second_breaks
        with self.assertRaises(RuntimeError):
            self._restore_open()
        other = []
        t = threading.Thread(target=lambda: other.append(runner.claim_launch(self.LIVE_SID)))
        t.start()
        t.join(10)
        self.assertEqual(other, [True])

    def test_a_cwd_that_is_not_a_path(self):
        self.write_manifest([{"sessionId": self.LIVE_SID, "cwd": self.cwd("a"), "project": "a"},
                             {"sessionId": self.DEAD_SID, "cwd": ["x"], "project": "b"}])
        rc, out = self._restore_open()
        self.assertNotIn("Traceback", out)
        self.assertIn("claude --resume " + self.LIVE_SID, self._script())


class TestTheClaimDirsOwnFilesThatCannotBeOpened(unittest.TestCase):
    """.swept and .lock left unopenable (a root-run ccwho): the sweep still
    runs, and a launch that cannot lock says which file."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_marker_that_cannot_be_opened(self):
        runner.claim_launch("99999999-9999-4999-8999-999999999999", now=self.T)
        d = os.path.join(self.tmp, "launching")
        mark = os.path.join(d, ".swept")
        open(mark, "w").close()
        os.utime(mark, (self.T - 7200, self.T - 7200))
        os.chmod(mark, 0)
        self.addCleanup(lambda: os.path.exists(mark) and os.chmod(mark, 0o600))
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True, "since": self.T - 7200,
                                       "until": self.T - 7200})
        os.utime(runner._claim_path(self.SID), (self.T - 7200, self.T - 7200))
        runner.release_claims([], self.T)
        self.assertFalse(os.path.exists(runner._claim_path(self.SID)))

    def test_a_lock_that_cannot_be_opened(self):
        runner.claim_launch("99999999-9999-4999-8999-999999999999", now=self.T)
        lock = os.path.join(self.tmp, "launching", ".lock")
        os.chmod(lock, 0)
        self.addCleanup(lambda: os.path.exists(lock) and os.chmod(lock, 0o600))
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        runner.claim_launch(self.SID)
        res, why = runner.launch_in_iterm("script", 1.0, [self.SID])
        self.assertIn(".lock", why)


class TestASessionLaunchedSinceTheListLeavesTheRestAlone(unittest.TestCase):
    """One session launched elsewhere while restore read the list: that one
    says "run it again" - the others still open."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open
    _script = _F._script

    def test_one_launched_during_the_scan(self):
        def scan(cache=None, status=None):
            status["source_ok"] = True
            runner._write_claim(self.DEAD_SID, {"pid": DEAD, "owner": "x", "since": time.time() + 1,
                                                "sessionId": self.DEAD_SID, "launched": True})
            return [], 0
        runner.engine.collect = scan
        rc, out = self._restore_open()
        self.assertIn("claude --resume " + self.LIVE_SID, self._script())
        self.assertNotIn("claude --resume " + self.DEAD_SID, self._script())
        self.assertIn("run it again", out)
        self.assertNotIn("already running or starting", out)


class TestTheListCountsASessionThatChanged(unittest.TestCase):
    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest = _F.setUp, _F.tearDown, _F.write_manifest

    def test_one_launched_during_the_scan(self):
        def scan(cache=None, status=None):
            status["source_ok"] = True
            runner._write_claim(self.DEAD_SID, {"pid": DEAD, "owner": "x", "since": time.time() + 1,
                                                "sessionId": self.DEAD_SID, "launched": True})
            return [], 0
        runner.engine.collect = scan
        said = runner.reopen_saved()
        self.assertIn("changed since the list was read", said)
        self.assertNotIn("could not resume", said)


class TestARestoreInterruptedInItsSendKeepsItsClaims(unittest.TestCase):
    _F = TestACallerSaysALaunchMayStillRun
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open, held = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open, _F.held

    def test_ctrl_c_in_the_send(self):
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])

        def interrupted(cmd, **kw):
            raise KeyboardInterrupt
        runner.subprocess.run = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self._restore_open()
        self.assertTrue(self.held(self.LIVE_SID) and self.held(self.DEAD_SID))

    def test_before_the_send_lets_go(self):                                     # control
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])
        self.addCleanup(setattr, runner.engine, "iterm_open_script", runner.engine.iterm_open_script)

        def broken(*a, **k):
            raise KeyboardInterrupt
        runner.engine.iterm_open_script = broken
        with self.assertRaises(KeyboardInterrupt):
            self._restore_open()
        self.assertFalse(self.held(self.LIVE_SID) or self.held(self.DEAD_SID))


class TestAHeldSessionBesideARunningOneIsNotSuccess(unittest.TestCase):
    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def test_cut_off_while_another_runs(self):
        class R:
            returncode, stdout, stderr = 1, "", "execution error: gone. (-609)"
        runner.subprocess.run = lambda cmd, **kw: self.runs.append(cmd) or R()
        self._restore_open()
        self.live = [{"sessionId": self.LIVE_SID, "tty": "ttys009", "pid": 7}]
        runs = len(self.runs)
        rc, out = self._restore_open()
        self.assertEqual(len(self.runs), runs)
        self.assertNotEqual(rc, 0)
        self.assertNotIn("already running or starting", out)
        self.assertIn("try again in", out)

    def unreadable(self, sid):
        runner._write_claim(sid, {"pid": DEAD, "since": time.time(), "sessionId": sid})
        path = runner._claim_path(sid)
        os.chmod(path, 0)
        self.addCleanup(lambda: os.path.exists(path) and os.chmod(path, 0o600))

    def test_unreadable_while_another_runs(self):
        self.unreadable(self.DEAD_SID)
        self.live = [{"sessionId": self.LIVE_SID, "tty": "ttys009", "pid": 7}]
        rc, out = self._restore_open()
        self.assertNotEqual(rc, 0)
        self.assertNotIn("already running or starting", out)
        self.assertIn("cannot be read", out)

    def test_the_list_names_the_unreadable_file(self):
        self.unreadable(self.DEAD_SID)
        self.write_manifest([{"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"}])
        said = runner.reopen_saved()
        self.assertIn("cannot be read", said)
        self.assertNotIn("nothing in that manifest", said)

    def test_open_says_the_wait_left(self):
        runner._write_claim(self.DEAD_SID, {"pid": DEAD, "owner": "x", "since": time.time() - 61,
                                            "sessionId": self.DEAD_SID, "launched": True, "cut_off": True})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            runner.open_session([self.DEAD_SID])
        waits = [int(n) for n in re.findall(r"try again in (\d+) s", out.getvalue() + err.getvalue())]
        self.assertTrue(waits and max(waits) <= runner.LAUNCH_CLAIM_SECONDS - 60, waits)


class TestASuspendedLauncherStillHoldsItsSend(unittest.TestCase):
    """A launcher stopped in the middle of its send (Ctrl-Z) holds its send
    lock: however long it sleeps, whatever iTerm2 did meanwhile, no one else
    launches the session - it would send when it wakes."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_another_waits_for_it(self):
        sid, sending, go, other = self.SIDS[0], threading.Event(), threading.Event(), []

        class Done:
            returncode, stdout, stderr = 0, "", ""

        def stopped(cmd, **kw):
            sending.set()
            go.wait(10)
            return Done()
        runner.subprocess.run = stopped
        # claimed and sent from one thread, as open and restore do: the claim
        # is that call's
        t = threading.Thread(target=lambda: (runner.claim_launch(sid, now=time.time() + 100),
                                             runner.launch_in_iterm("script", 1.0, [sid])))
        t.start()
        self.assertTrue(sending.wait(10))
        runner.engine.terms.app_snapshot = lambda: "  PID UID UCOMM\n1 0 launchd\n"   # iTerm2 quit
        b = threading.Thread(target=lambda: other.append(runner.claim_launch(
            sid, now=time.time() + 3600, alive=lambda p: False)))
        b.start()
        b.join(10)
        go.set()
        t.join(10)
        self.assertEqual(other, [False])

    def test_no_send_lock_is_left_behind(self):                                # control
        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda cmd, **kw: Done()
        runner.launch_in_iterm("script", 1.0, self.SIDS[:2])
        left = [n for n in os.listdir(os.path.join(self.tmp, "launching")) if n.endswith(".send")]
        self.assertEqual(left, [])

    def test_no_send_lock_is_left_open(self):
        # the list runs the restores inside it, for days: a descriptor per
        # launch would run it out of them
        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda cmd, **kw: Done()
        before = len(os.listdir("/dev/fd"))
        runner.launch_in_iterm("script", 1.0, self.SIDS[:2])
        self.assertEqual(len(os.listdir("/dev/fd")), before)


class TestASendLockIsOnTheFileAtItsName(unittest.TestCase):
    """A send lock is a file of the launch's own: made new, never through a
    link, and only removed while it is held."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def held_now(self, path):
        # can another take it at once? not while one holds it
        fd = os.open(path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        finally:
            os.close(fd)
        return False

    def test_it_is_removed_only_while_held(self):
        # by the launcher letting go and by the sweep: whoever removes a send
        # file holds its lock at that moment
        claims = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent.SIDS[:2]
        for sid in claims:
            self.assertTrue(runner.claim_launch(sid))
        token, fd = runner._hold_send()
        os.close(fd)                                  # left behind by a holder that died
        old = self.T - 7200
        os.utime(runner._send_path(token), (old, old))
        removed, real_remove, real_unlink = [], os.remove, os.unlink

        def watching(real):
            def remove(path, *a, **kw):
                if str(path).endswith(".send"):
                    removed.append(self.held_now(path))
                return real(path, *a, **kw)
            return remove

        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda cmd, **kw: Done()
        os.remove, os.unlink = watching(real_remove), watching(real_unlink)
        try:
            runner.launch_in_iterm("script", 1.0, claims)
            runner.release_claims([], self.T)
        finally:
            os.remove, os.unlink = real_remove, real_unlink
        self.assertEqual(removed, [True, True])       # the launch's own, the one left behind

    def test_a_link_at_its_name_is_not_followed(self):
        # a root-run ccwho locks in the user's claim dir
        token = "cd" * 16
        self.addCleanup(setattr, runner.uuid, "uuid4", runner.uuid.uuid4)
        runner.uuid.uuid4 = lambda: type("U", (), {"hex": token})()
        os.makedirs(os.path.join(self.tmp, "launching"))
        target = os.path.join(self.tmp, "made-through-a-link")
        os.symlink(target, runner._send_path(token))
        with self.assertRaises(OSError):
            runner._hold_send()
        self.assertFalse(os.path.exists(target))

    def test_one_held_and_let_go(self):                                         # control
        held = runner._hold_send()
        rec = {"send": held[0]}
        self.assertTrue(runner._sending(rec))
        runner._let_go_send(held)
        self.assertFalse(runner._sending(rec))
        self.assertFalse(os.path.exists(runner._send_path(held[0])))


class TestTheSweepLeavesASendStillHeld(unittest.TestCase):
    """A launcher stopped longer than the sweep's age still holds its send
    lock: its file stays, and still says so."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def old_send(self):
        held = runner._hold_send()
        old = self.T - 7200
        os.utime(runner._send_path(held[0]), (old, old))
        return held

    def test_a_held_one_stays(self):
        held = self.old_send()
        self.addCleanup(runner._let_go_send, held)
        runner.release_claims([], self.T)
        self.assertTrue(runner._sending({"send": held[0]}))

    def test_one_no_one_holds_goes(self):                                     # control
        token, fd = self.old_send()
        os.close(fd)                                # its holder died: the file stays behind
        runner.release_claims([], self.T)
        self.assertFalse(os.path.exists(runner._send_path(token)))


class TestAStaleDecisionIsJudgedWhenTheLockIsHad(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_it_grew_old_waiting_for_the_lock(self):
        real = runner._claims_locked

        @contextlib.contextmanager
        def slow():
            time.sleep(1.0)
            with real():
                yield
        runner._claims_locked = slow
        self.addCleanup(setattr, runner, "_claims_locked", real)
        decided = time.time() - runner.SIGHTING_SECONDS + 0.5
        self.assertFalse(runner.claim_launch(self.SID, decided_at=decided))
        self.assertEqual(runner.why_held(self.SID)[0], "old list")


class TestAClaimDatedAheadOfTheClock(unittest.TestCase):
    """Written while the clock ran fast: judged as of when it is first read
    under the lock - so an iTerm2 restart, a sighting and "no iTerm2" all
    still work on it."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def ahead(self, **rec):
        runner._write_claim(self.SID, dict({"pid": DEAD, "since": self.T + 7200, "sessionId": self.SID,
                                            "unresolved": True, "iterm_pid": None, "boot": "THIS-BOOT"}, **rec))

    def test_no_iterm2_lets_it_go(self):
        self.ahead()
        runner.engine.terms.app_snapshot = lambda: "  PID UID UCOMM\n1 0 launchd\n"
        self.assertTrue(runner.claim_launch(self.SID))

    def test_it_binds_and_a_restart_clears_it(self):
        self.ahead()
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n5151 {os.getuid()} iTerm2\n"
        self.assertFalse(runner.claim_launch(self.SID, alive=lambda p: p == 5151))
        with open(runner._claim_path(self.SID)) as fh:
            self.assertEqual(json.load(fh)["iterm_pid"], [5151])
        self.assertTrue(runner.claim_launch(self.SID, alive=lambda p: False))

    def test_a_sighting_replaces_it(self):
        self.ahead()
        runner.release_claims([self.SID], self.T)
        with open(runner._claim_path(self.SID)) as fh:
            self.assertIs(json.load(fh).get("seen"), True)

    def test_it_reads_as_unresolved(self):
        me = os.getpid()
        self.ahead(iterm_pid=me)
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.assertFalse(runner.claim_launch(self.SID))
        self.assertEqual(runner.why_held(self.SID)[0], "unresolved")

    def test_a_claim_of_now_is_as_before(self):                               # control
        me = os.getpid()
        self.ahead(since=self.T - 5, iterm_pid=me)
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.assertFalse(runner.claim_launch(self.SID))


class TestTempFilesInTheClaimDirFollowNoLink(unittest.TestCase):
    """A root-run ccwho writes in the user's claim dir: a temp file there is
    made new and exclusively, never through a link planted at its name."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_link_at_the_names_the_code_used(self):
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d, exist_ok=True)
        victim = os.path.join(self.tmp, "victim")
        with open(victim, "w") as fh:
            fh.write("keep me")
        for name in (f"{self.SID}.json.{os.getpid()}.tmp", f".swept.{os.getpid()}.tmp"):
            os.symlink(victim, os.path.join(d, name))
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True, "since": self.T, "until": self.T})
        runner.release_claims([], self.T + 7200)
        with open(victim) as fh:
            self.assertEqual(fh.read(), "keep me")
        self.assertTrue(os.path.exists(os.path.join(d, ".swept")))                # control: it ran


class TestSentBindsOnlyAReceiverItSaw(unittest.TestCase):
    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def after_a_timeout(self):
        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        runner.subprocess.run = stuck
        runner.launch_in_iterm("script", 1.0, self.SIDS[:1])
        with open(runner._claim_path(self.SIDS[0])) as fh:
            return json.load(fh)

    def test_a_table_it_could_not_read(self):
        runner.engine.terms.app_snapshot = lambda: ""
        self.assertIsNone(self.after_a_timeout()["iterm_pid"])

    def test_a_table_that_shows_one(self):                                       # control
        self.assertEqual(self.after_a_timeout()["iterm_pid"], [4242])


class TestTheReasonIsTheRefusals(unittest.TestCase):
    """Why a launch was held comes from the read that held it - a claim gone
    a moment later does not turn it into "starting"."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_removed_after_the_refusal(self):
        me = os.getpid()
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T - 60, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": me})
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.assertFalse(runner.claim_launch(self.SID))
        os.remove(runner._claim_path(self.SID))
        self.assertEqual(runner.why_held(self.SID)[0], "unresolved")


class TestMoreHeldSessionsSaidRight(unittest.TestCase):
    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def test_open_after_a_newer_sighting(self):
        def scan(cache=None, status=None):
            status["source_ok"] = True
            t = time.time() + 1
            runner._write_claim(self.DEAD_SID, {"sessionId": self.DEAD_SID, "seen": True, "since": t, "until": t})
            return [], 0
        runner.engine.collect = scan
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = runner.open_session([self.DEAD_SID])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(rc, 1)
        self.assertNotIn("another window", text)
        self.assertIn("run it again", text)

    def changed_during_the_scan(self, rows):
        def scan(cache=None, status=None):
            status["source_ok"] = True
            runner._write_claim(self.DEAD_SID, {"pid": DEAD, "owner": "x", "since": time.time() + 1,
                                                "sessionId": self.DEAD_SID, "launched": True})
            return list(rows), 0
        runner.engine.collect = scan

    def test_changed_beside_running(self):
        self.changed_during_the_scan([{"sessionId": self.LIVE_SID, "tty": "ttys009", "pid": 7}])
        rc, out = self._restore_open()
        self.assertNotEqual(rc, 0, out)
        self.assertNotIn("already running or starting", out)

    def test_only_a_changed_one_through_the_list(self):
        self.write_manifest([{"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"}])
        self.changed_during_the_scan([])
        said = runner.reopen_saved()
        self.assertIn("run it again", said)
        self.assertNotIn("nothing in that manifest", said)

    def test_two_cut_off_the_list_names_the_longer_wait(self):
        for sid, age in ((self.LIVE_SID, 10), (self.DEAD_SID, 80)):
            runner._write_claim(sid, {"pid": DEAD, "owner": "x", "since": time.time() - age,
                                      "sessionId": sid, "launched": True, "cut_off": True})
        self.write_manifest([{"sessionId": self.LIVE_SID, "cwd": self.cwd("a"), "project": "a"},
                             {"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"},
                             {"sessionId": "33333333-3333-4333-8333-333333333333", "cwd": self.cwd("c"),
                              "project": "c"}])
        self.on_disk.add("33333333-3333-4333-8333-333333333333")
        said = runner.reopen_saved()
        waits = [int(n) for n in re.findall(r"try again in (\d+) s", said)]
        self.assertTrue(waits and max(waits) >= 70, said)

    def test_a_fast_clock_claim_says_restart_beside_a_running_one(self):
        me = os.getpid()
        self.addCleanup(setattr, runner, "_boot_id", runner._boot_id)
        runner._boot_id = lambda: "THIS-BOOT"
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{me} {os.getuid()} iTerm2\n"
        self.addCleanup(setattr, runner.engine.terms, "app_snapshot", GUARDS["app_snapshot"])
        runner._write_claim(self.DEAD_SID, {"pid": DEAD, "owner": "x", "since": time.time() + 7200,
                                            "sessionId": self.DEAD_SID, "unresolved": True, "iterm_pid": me,
                                            "boot": "THIS-BOOT"})
        self.live = [{"sessionId": self.LIVE_SID, "tty": "ttys009", "pid": 7}]
        rc, out = self._restore_open()
        self.assertNotEqual(rc, 0, out)
        self.assertNotIn("already running or starting", out)
        self.assertIn("restart iTerm2", out)


class TestOurOwnMarkThatCannotBeWritten(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_it_says_could_not_record(self):
        self.assertTrue(runner.claim_launch(self.SID))
        self.addCleanup(setattr, runner, "_write_claim", runner._write_claim)
        runner._write_claim = lambda sid, record: False
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        res, why = runner.launch_in_iterm("script", 1.0, [self.SID])
        self.assertIn("could not record", why)
        self.assertNotIn("another ccwho", why)

    def test_a_failed_take_leaves_an_earlier_launch_alone(self):
        self.assertTrue(runner.claim_launch(self.SID, now=self.T - 120))
        runner.claim_launched([self.SID])
        rec = json.load(open(runner._claim_path(self.SID)))
        rec["since"] = self.T - 120
        rec.pop("clock")                                     # a new since: a new stamp
        runner._write_claim(self.SID, rec)
        with open(runner._claim_path(self.SID), "rb") as fh:
            before = fh.read()
        real, calls = runner._write_claim, []

        def once(sid, record):
            calls.append(1)
            return False if len(calls) == 1 else real(sid, record)
        runner._write_claim = once
        self.addCleanup(setattr, runner, "_write_claim", real)
        self.assertTrue(runner.claim_launch(self.SID, now=self.T))
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        res, why = runner.launch_in_iterm("script", 1.0, [self.SID])
        self.assertIn("could not record", why)
        with open(runner._claim_path(self.SID), "rb") as fh:
            self.assertEqual(fh.read(), before)


NO_ITERM = "  PID UID UCOMM\n1 0 launchd\n"


def unstamped(rec):
    """A record as written, without the "clock" _write_claim stamps on it."""
    return {k: v for k, v in rec.items() if k != "clock"}


def send_lock_fds():
    """This process's descriptors on send lock files."""
    d = os.path.join(runner.ccwho_dir(), "launching")
    files = set()
    for name in os.listdir(d):
        if name.endswith(".send"):
            st = os.lstat(os.path.join(d, name))
            files.add((st.st_dev, st.st_ino))
    fds = []
    for n in os.listdir("/dev/fd"):
        try:
            st = os.fstat(int(n))
        except OSError:
            continue
        if (st.st_dev, st.st_ino) in files:
            fds.append(int(n))
    return fds


class TestALaunchHoldsOneSendLock(unittest.TestCase):
    """One send lock per launch, not one per session: a restore of many
    windows holds one descriptor, and each launch locks a file of its own -
    one no one else ever locks, so no removal of it can hide a send."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    class Done:
        returncode, stdout, stderr = 0, "", ""

    def test_many_windows_one_descriptor(self):
        during = []
        runner.subprocess.run = lambda cmd, **kw: during.append(len(send_lock_fds())) or self.Done()
        runner.launch_in_iterm("script", 1.0, self.SIDS[:20])
        self.assertEqual(during, [1])

    def test_each_launch_locks_a_file_of_its_own(self):
        sid, names = self.SIDS[0], []

        def run(cmd, **kw):
            names.append(sorted(n for n in os.listdir(os.path.join(self.tmp, "launching"))
                                if n.endswith(".send")))
            return self.Done()
        runner.subprocess.run = run
        runner.launch_in_iterm("script", 1.0, [sid])
        os.remove(runner._claim_path(sid))              # seen and ended: it may start again
        self.assertTrue(runner.claim_launch(sid))
        runner.launch_in_iterm("script", 1.0, [sid])
        self.assertEqual([len(n) for n in names], [1, 1])
        self.assertNotEqual(names[0], names[1])


class TestTheOsascriptItRunsHoldsTheSend(unittest.TestCase):
    """The one that sends is osascript: a launcher that dies mid-send - killed,
    its terminal closed - leaves it running, and it may still start iTerm2 and
    deliver the event. The send lock lives while either of them does."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_a_launcher_that_dies_mid_send(self):
        sid, other = self.SIDS[0], []
        runner.engine.terms.app_snapshot = lambda: NO_ITERM            # iTerm2 not started yet

        def another():
            t = threading.Thread(target=lambda: other.append(runner.claim_launch(sid, alive=lambda p: False)))
            t.start()
            t.join(10)

        def run(cmd, **kw):
            child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                     stdin=subprocess.PIPE, pass_fds=kw.get("pass_fds", ()))
            null = os.open(os.devnull, os.O_RDONLY)
            try:
                for fd in send_lock_fds():
                    os.dup2(null, fd)                   # the launcher dies: its own hold goes
            finally:
                os.close(null)
            another()                                   # its osascript still runs
            child.stdin.close()
            child.wait(10)
            another()                                   # control: it has ended too
            raise subprocess.TimeoutExpired(cmd, 1)
        runner.subprocess.run = run
        runner.launch_in_iterm("script", 1.0, [sid])
        self.assertEqual(other, [False, True])


class TestATableFromDuringASendReleasesNothing(unittest.TestCase):
    """A table taken while a launch was being sent - before its osascript had
    started iTerm2 - must never release that launch once the send is over:
    it is judged on a table taken after the send was seen to end, which
    shows the iTerm2 the osascript started."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def timed_out(self, after):
        # another restore takes its batch table during this send, when no
        # iTerm2 runs; after the send the table is `after`
        sid, batch, sending = self.SIDS[0], runner._tables(), []
        runner.engine.terms.app_snapshot = lambda: NO_ITERM if sending else after

        def run(cmd, **kw):
            sending.append(1)
            batch()
            time.sleep(0.01)
            sending.clear()
            raise subprocess.TimeoutExpired(cmd, 1)
        runner.subprocess.run = run
        runner.launch_in_iterm("script", 1.0, [sid])
        return sid, batch

    def test_bound_to_the_iterm2_its_osascript_started(self):
        sid, batch = self.timed_out(ITERM_TABLE)
        self.assertFalse(runner.claim_launch(sid, alive=lambda p: p == 4242, refresh=batch))

    def test_after_a_send_whose_ps_could_not_be_read(self):
        sid, batch = self.timed_out("")
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE        # the one its osascript started
        self.assertFalse(runner.claim_launch(sid, alive=lambda p: p == 4242, refresh=batch))

    def test_after_a_launcher_that_died_mid_send(self):
        # killed before it could record what the send did: its record names
        # the send lock and has the time from before the send
        sid, batch = self.SIDS[0], runner._tables()
        token, fd = runner._hold_send()
        self.assertTrue(runner.claim_unresolved(sid, None, send=token))
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        batch()                                         # taken while it was being sent
        os.close(fd)                                    # it dies: the lock goes, its file stays
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        self.assertFalse(runner.claim_launch(sid, alive=lambda p: p == 4242, refresh=batch))

    def test_a_table_after_the_send_with_no_iterm2(self):                       # control
        sid, batch = self.timed_out(ITERM_TABLE)
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        self.assertTrue(runner.claim_launch(sid, alive=lambda p: p == 4242))


class TestARefusalWithNoPlaceKeepsTheClaims(unittest.TestCase):
    """-1743 gives the claims back only when osascript places it in the
    script's first event (engine.terms.AE_PROBE), however many windows: this one's
    stderr places it nowhere, so what came before it may have been written -
    they stay."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp, held_while_iterm2_lives = _F.SIDS, _F.setUp, _F.held_while_iterm2_lives

    def test_several_windows_stay_held(self):
        class R:
            returncode, stdout = 1, ""
            stderr = "execution error: Not authorized to send Apple events to iTerm2. (-1743)"
        runner.subprocess.run = lambda cmd, **kw: R()
        res, why = runner.launch_in_iterm("script", 1.0, self.SIDS[:2])
        self.assertIsNone(res)
        self.assertIn("-1743", why)
        self.assertIn(runner.MAY_STILL_RUN, why)
        self.assertNotIn("part way", why)
        self.assertTrue(all(self.held_while_iterm2_lives(s) for s in self.SIDS[:2]))
        # control: TestAnAutomationRefusalIsPlacedByItsStatement.test_at_the_probe_nothing_was_sent


class TestNoFileInTheClaimDirHangsOrMisleadsIt(unittest.TestCase):
    """A launch waits for the claims lock and a scan tries it: a FIFO planted
    at a name read under it must not block, a link must not be followed, and
    a send lock that cannot be read holds - it never reads as free."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    TOKEN = "ab" * 16
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def in_a_process(self, setup):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        code = ("import os, time, ccwho as r\n"
                f"sid, token = {self.SID!r}, {self.TOKEN!r}\n"
                "r._boot_id = lambda: 'THIS-BOOT'\n"
                "os.makedirs(os.path.join(os.environ['CCWHO_DIR'], 'launching'))\n"
                + setup +
                f"r.engine.terms.app_snapshot = lambda: {NO_ITERM!r}\n"
                "print(r.claim_launch(sid, alive=lambda p: False))\n")
        here = os.path.dirname(os.path.abspath(__file__))
        try:
            r = subprocess.run([sys.executable, "-B", "-c", code], cwd=here, capture_output=True, text=True,
                               timeout=20, env=dict(os.environ, CCWHO_DIR=tmp, PYTHONDONTWRITEBYTECODE="1"))
        except subprocess.TimeoutExpired:
            self.fail("claim_launch hung")
        return r.stdout.strip(), r.stderr[-400:]

    def test_a_fifo_at_a_claim_name(self):
        # no claim ccwho wrote: taken, and replaced
        self.assertEqual(self.in_a_process("os.mkfifo(r._claim_path(sid))\n"), ("True", ""))

    def test_a_fifo_at_a_send_lock(self):
        # the send lock of a launch that may still run: it holds
        out = self.in_a_process(
            "r._write_claim(sid, {'pid': 4000000, 'since': time.time() - 60, 'sessionId': sid,"
            " 'unresolved': True, 'iterm_pid': None, 'boot': 'THIS-BOOT', 'send': token})\n"
            "os.mkfifo(r._send_path(token))\n")
        self.assertEqual(out, ("False", ""))

    def test_a_send_lock_that_cannot_be_read_holds(self):
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T - 60, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": None, "boot": "THIS-BOOT",
                                       "send": self.TOKEN})
        path = runner._send_path(self.TOKEN)
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o000))
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        self.assertFalse(runner.claim_launch(self.SID, alive=lambda p: False))
        os.chmod(path, 0o600)
        self.assertTrue(runner.claim_launch(self.SID, alive=lambda p: False))   # control: free

    def test_a_link_at_the_lock(self):
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d)
        target = os.path.join(self.tmp, "made-through-a-link")
        os.symlink(target, os.path.join(d, ".lock"))
        runner.claim_launch(self.SID)
        self.assertFalse(os.path.lexists(target))

    def test_a_launching_dir_that_is_a_link(self):
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        kept = os.path.join(other, f"{self.SID}.json")
        with open(kept, "w") as fh:
            json.dump({"sessionId": self.SID, "seen": True, "since": self.T - 7200, "until": self.T - 7200}, fh)
        os.utime(kept, (self.T - 7200, self.T - 7200))
        os.symlink(other, os.path.join(self.tmp, "launching"))
        runner.release_claims([], self.T)
        runner.claim_launch("4f2b91ac-1111-4222-8333-000000000077")
        self.assertEqual(sorted(os.listdir(other)), [f"{self.SID}.json"])


class TestTheSweepMarkerLeavesNoTempFile(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_marker_that_cannot_be_replaced(self):
        d = os.path.join(self.tmp, "launching")
        os.makedirs(os.path.join(d, ".swept", "x"))       # a dir, not empty: never replaced
        for k in range(3):
            runner.release_claims([], self.T + 7200 * (k + 1))
        self.assertEqual([n for n in os.listdir(d) if n.startswith(".swept.")], [])

    def test_a_marker_put_in_place(self):                                      # control
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d)
        runner.release_claims([], self.T)
        self.assertTrue(os.path.isfile(os.path.join(d, ".swept")))
        self.assertEqual([n for n in os.listdir(d) if n.startswith(".swept.")], [])


class TestWhyHeldIsWhatTheRefusalSaid(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_after_two_tables_that_could_not_tell(self):
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T - 60, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": None, "boot": "THIS-BOOT"})
        older = lambda after=None: (NO_ITERM, self.T - 120)   # from before the claim: tells nothing
        self.assertFalse(runner.claim_launch(self.SID, refresh=older))
        os.remove(runner._claim_path(self.SID))
        self.assertEqual(runner.why_held(self.SID), ("unresolved", 0))


class TestASendOfOtherWindowsBlocksNoNewLaunch(unittest.TestCase):
    """A long restore still sending other windows must not stop a session it
    already launched - seen running, and ended since - from opening again."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp
    X, Y = "4f2b91ac-2222-4222-8333-000000000001", "4f2b91ac-2222-4222-8333-000000000002"

    def test_open_it_again_meanwhile(self):
        sending, go, done = threading.Event(), threading.Event(), []

        class Done:
            returncode, stdout, stderr = 0, "", ""

        def run(cmd, **kw):
            if threading.current_thread() is not threading.main_thread():     # the long restore
                sending.set()
                go.wait(10)
            return Done()
        runner.subprocess.run = run
        t = threading.Thread(target=lambda: (runner.claim_launch(self.X), runner.claim_launch(self.Y),
                                             done.append(runner.launch_in_iterm("script", 60.0, [self.Y, self.X]))))
        t.start()
        self.addCleanup(t.join, 10)
        self.addCleanup(go.set)
        self.assertTrue(sending.wait(10))
        runner.release_claims([self.X], time.time())              # X seen running; it ends since
        self.assertTrue(runner.claim_launch(self.X, decided_at=time.time() + 0.001))
        res, why = runner.launch_in_iterm("script", 1.0, [self.X])
        self.assertIsNotNone(res, why)


class TestASendLockIsLetGoOfWhenNothingIsSent(unittest.TestCase):
    """The list runs for days: a launch it did not send must not keep its
    send lock - nor its file - for the life of the list."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_mark_that_cannot_be_written(self):
        self.assertTrue(runner.claim_launch(self.SID))
        before = len(os.listdir("/dev/fd"))
        self.addCleanup(setattr, runner, "_write_claim", runner._write_claim)
        runner._write_claim = lambda sid, record: False
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        self.assertIsNone(runner.launch_in_iterm("script", 1.0, [self.SID])[0])
        self.assertEqual(len(os.listdir("/dev/fd")), before)
        d = os.path.join(self.tmp, "launching")
        self.assertEqual([n for n in os.listdir(d) if n.endswith(".send")], [])


class TestEveryMarkIsWrittenUnderItsSendLock(unittest.TestCase):
    """The send lock is had BEFORE a claim is marked unresolved: a launcher
    stopped between the two must not leave a mark nothing holds."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_each_unresolved_mark(self):
        seen, real = [], runner._write_claim

        def watching(sid, record):
            if record.get("unresolved") is True and record.get("send"):
                seen.append((sid, runner._sending(record)))
            return real(sid, record)
        runner._write_claim = watching
        self.addCleanup(setattr, runner, "_write_claim", real)

        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda *a, **k: Done()
        runner.launch_in_iterm("script", 1.0, self.SIDS[:2])
        self.assertEqual(seen, [(s, True) for s in self.SIDS[:2]])


class TestWhatAClaimNamesIsReadWithCare(unittest.TestCase):
    """A claim's own fields, and the files at its names, are data another
    process wrote: never followed out of the claim dir, never trusted to be
    what ccwho writes."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def unresolved(self, **rec):
        runner._write_claim(self.SID, dict({"pid": DEAD, "since": self.T - 60, "sessionId": self.SID,
                                            "unresolved": True, "iterm_pid": None, "boot": "THIS-BOOT"},
                                           **rec))

    def test_a_fast_clock_claim_still_being_sent_holds(self):
        held = runner._hold_send()
        self.addCleanup(runner._let_go_send, held)
        self.unresolved(since=self.T + 7200, send=held[0])
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        self.assertFalse(runner.claim_launch(self.SID, alive=lambda p: False))
        runner._let_go_send(held)
        self.assertTrue(runner.claim_launch(self.SID, alive=lambda p: False))   # control: over

    def test_a_dir_at_a_send_lock_holds(self):
        token = "ef" * 16
        os.makedirs(runner._send_path(token))
        self.unresolved(send=token)
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        self.assertFalse(runner.claim_launch(self.SID, alive=lambda p: False))

    def test_a_send_that_is_not_a_token(self):
        # a path in its place: no claim ccwho wrote, never opened
        outside = os.path.join(self.tmp, "outside.send")
        with open(outside, "w"):
            pass
        fd = os.open(outside, os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)                  # would read as "being sent"
        self.unresolved(send="../outside")
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        self.assertTrue(runner.claim_launch(self.SID, alive=lambda p: False))

    def test_a_link_at_a_claim_name(self):
        # no claim ccwho wrote: taken over, and the file it points at is left
        target = os.path.join(self.tmp, "a-claim-elsewhere.json")
        with open(target, "w") as fh:
            json.dump({"pid": DEAD, "since": time.time(), "sessionId": self.SID, "launched": True}, fh)
        os.makedirs(os.path.join(self.tmp, "launching"))
        os.symlink(target, runner._claim_path(self.SID))
        self.assertTrue(runner.claim_launch(self.SID))
        self.assertFalse(os.path.islink(runner._claim_path(self.SID)))
        with open(target) as fh:
            self.assertIs(json.load(fh).get("launched"), True)


class TestABatchJudgesEachClaimOnATableAfterIt(unittest.TestCase):
    """A restore shares one table for its claims. A claim re-dated when its
    send is seen over is newer than that table: it gets one taken after it -
    never the older one, and never "held" for want of one."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def sent_and_died(self, sids):
        # a restore of these whose launcher died mid-send: one send lock,
        # let go of, named by all of them
        token, fd = runner._hold_send()
        for sid in sids:
            self.assertTrue(runner.claim_unresolved(sid, None, send=token))
        os.close(fd)

    def test_all_of_them_reopen_with_no_iterm2(self):
        sids = self.SIDS[:3]
        self.sent_and_died(sids)
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        tables = runner._tables()
        self.assertEqual([runner.claim_launch(s, alive=lambda p: False, refresh=tables) for s in sids],
                         [True, True, True])

    def test_all_of_them_held_by_a_running_iterm2(self):                        # control
        sids = self.SIDS[:3]
        self.sent_and_died(sids)
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        tables = runner._tables()
        self.assertEqual([runner.claim_launch(s, alive=lambda p: p == 4242, refresh=tables) for s in sids],
                         [False, False, False])


class TestABatchTableIsTakenAgainOnlyForAClaimNewerThanIt(unittest.TestCase):
    """_tables keeps a batch's table by its monotonic time: a wall clock step
    neither takes it again nor ages it. A claim newer than it (`after`, on
    the monotonic clock) takes a new one; one dated ahead of now cannot."""

    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def counted(self):
        calls = []
        runner.engine.terms.app_snapshot = lambda: calls.append(1) or NO_ITERM
        return calls, runner._tables()

    def test_a_step_before_a_claim_older_than_it(self):
        calls, tables = self.counted()
        first = tables()
        real = time.time
        for step in (7200, -7200):
            with mock.patch.object(runner.time, "time", lambda: real() + step):
                again = tables(first[2] - 1)
            self.assertEqual((len(calls), again[2]), (1, first[2]), step)

    def test_a_claim_newer_than_it(self):                                        # control
        calls, tables = self.counted()
        first = tables()
        time.sleep(0.01)
        again = tables(runner._mono())
        self.assertEqual(len(calls), 2)
        self.assertGreater(again[2], first[2])

    def test_a_claim_dated_ahead_of_now(self):
        calls, tables = self.counted()
        tables()
        tables(runner._mono() + 60)
        self.assertEqual(len(calls), 1)


class TestNothingFromTheFutureIsTrusted(unittest.TestCase):
    """The wall clock can be set back: a decision stamped later than now was
    made before the step - its order against claims and sightings written
    since is unknown. A table's time is on the monotonic clock in use
    (_tables), never ahead; one given on the wall clock alone - a
    (table, taken) refresh, tests' - dated ahead is not trusted either."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_decision_from_the_future(self):
        # scan at 10000, clock set back to 6400, a sighting at 6402: the
        # decision at "10000" must not take the session
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True, "since": self.T - 5, "until": self.T})
        self.assertFalse(runner.claim_launch(self.SID, decided_at=time.time() + 3600))
        self.assertEqual(runner.why_held(self.SID)[0], "old list")

    def test_a_decision_just_made(self):                                        # control
        self.assertTrue(runner.claim_launch(self.SID, decided_at=time.time() - 1))

    def test_a_table_from_the_future(self):
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T - 60, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": None, "boot": "THIS-BOOT"})
        future = lambda after=None: (NO_ITERM, time.time() + 7200)
        self.assertFalse(runner.claim_launch(self.SID, alive=lambda p: False, refresh=future))


class TestTheClaimDirIsPrivate(unittest.TestCase):
    """Another user who can write in launching/ can remove a send lock's file
    mid-send - and the claim reads as not being sent."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_made_private_whatever_the_umask(self):
        old = os.umask(0)
        try:
            runner._claims_dir()
        finally:
            os.umask(old)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.tmp, "launching")).st_mode), 0o700)

    def test_one_open_to_others_is_made_private(self):
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d)
        os.chmod(d, 0o777)
        runner._claims_dir()
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)

    def test_one_of_another_user_is_refused(self):
        os.makedirs(os.path.join(self.tmp, "launching"))
        sent = []
        runner.subprocess.run = lambda *a, **k: sent.append(a)
        with mock.patch.object(runner.os, "geteuid", return_value=os.getuid() + 1):
            self.assertTrue(runner.claim_launch(self.SID))
            self.assertIn(self.SID, runner._unrecorded())
            res, why = runner.launch_in_iterm("script", 1.0, [self.SID])
        self.assertFalse(os.path.lexists(runner._claim_path(self.SID)))
        self.assertIsNone(res)
        self.assertIn("could not record", why)
        self.assertEqual(sent, [])

    def test_one_of_ours(self):                                                 # control
        self.assertTrue(runner.claim_launch(self.SID))
        self.assertTrue(os.path.exists(runner._claim_path(self.SID)))

    def test_a_link_is_said_as_one(self):
        other = os.path.join(self.tmp, "elsewhere")
        os.makedirs(other)
        os.symlink(other, os.path.join(self.tmp, "launching"))
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        self.assertTrue(runner.claim_launch(self.SID))
        res, why = runner.launch_in_iterm("script", 1.0, [self.SID])
        self.assertIn("not a link", why)


class TestAClaimsLockHeldForGoodStopsNoScan(unittest.TestCase):
    """A ccwho stopped (Ctrl-Z) while it holds the claims lock must not
    freeze every scan and every list: a scan does not wait for it, and a
    launch waits a while, then refuses."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def hold_the_lock(self):
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d, exist_ok=True)
        fd = os.open(os.path.join(d, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        # a wait given up marks this thread (the next ones are short): not
        # the next test's
        self.addCleanup(runner._LOCAL.__dict__.pop, "lock_gave_up", None)

    def within(self, seconds, call):
        out = []
        t = threading.Thread(target=lambda: out.append(call()), daemon=True)
        t.start()
        t.join(seconds)
        return out

    def test_a_scan_does_not_wait(self):
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T - 60, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": None})
        self.hold_the_lock()
        self.assertEqual(self.within(3, lambda: runner.release_claims([self.SID], self.T)), [None])

    def test_a_launch_waits_a_while_then_refuses(self):
        self.addCleanup(setattr, runner, "CLAIMS_LOCK_SECONDS", runner.CLAIMS_LOCK_SECONDS)
        runner.CLAIMS_LOCK_SECONDS = 0.2
        self.hold_the_lock()
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        self.assertEqual(self.within(3, lambda: runner.claim_launch(self.SID)), [True])
        res, why = runner.launch_in_iterm("script", 1.0, [self.SID])
        self.assertIsNone(res)
        self.assertIn("stopped", why)

    def test_a_scan_with_the_lock_free(self):                                   # control
        runner._write_claim(self.SID, {"pid": DEAD, "since": self.T - 60, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": None})
        runner.release_claims([self.SID], self.T)
        with open(runner._claim_path(self.SID)) as fh:
            self.assertIs(json.load(fh).get("seen"), True)


class TestTheSweepKnowsASendLockByItsToken(unittest.TestCase):
    """A send lock is named by a token, not a session id: a narrower session
    id pattern must not leave its files behind for good."""

    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_an_old_one_with_a_session_id_pattern_of_uuids(self):
        token, fd = runner._hold_send()
        os.close(fd)
        os.utime(runner._send_path(token), (self.T - 7200, self.T - 7200))
        self.addCleanup(setattr, runner.engine, "_SESSION_ID", runner.engine._SESSION_ID)
        runner.engine._SESSION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
        runner.release_claims([], self.T)
        self.assertFalse(os.path.exists(runner._send_path(token)))


class TestASendValueNoCcwhoWrote(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_values_that_are_no_token(self):
        for send in (5, ["ab" * 16], {"x": 1}, True):
            rec = {"pid": DEAD, "since": time.time() - 60, "sessionId": self.SID, "unresolved": True,
                   "iterm_pid": None, "send": send}
            runner._write_claim(self.SID, rec)
            runner.release_claims([self.SID], time.time())
            runner._write_claim(self.SID, rec)
            self.assertTrue(runner.claim_launch(self.SID), send)

    def test_a_token_held(self):                                                # control
        held = runner._hold_send()
        self.addCleanup(runner._let_go_send, held)
        runner._write_claim(self.SID, {"pid": DEAD, "since": time.time() - 60, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": None, "send": held[0]})
        self.assertFalse(runner.claim_launch(self.SID))


def refused_at(where):
    """osascript as iTerm2 refusing Apple Events (-1743) at the statement of
    the script it runs that `where` starts - its range, as osascript gives it."""
    def run(cmd, **kw):
        at = cmd[-1].index(where)

        class R:
            returncode, stdout = 1, ""
            stderr = (f"{at}:{at + len(where)}: execution error: Not authorized to send"
                      " Apple events to iTerm2. (-1743)")
        return R()
    return run


class TestAnAutomationRefusalIsPlacedByItsStatement(unittest.TestCase):
    """-1743 is Automation refused. At the script's first event - a probe
    that writes nothing - nothing was sent: the claims go, and the line says
    how to allow it. Anywhere after it a window may have been written, even
    in a script of one window (the pane fill walks on after its write): the
    claims stay."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp, held_while_iterm2_lives = _F.SIDS, _F.setUp, _F.held_while_iterm2_lives

    def script(self, sids):
        return runner.engine.iterm_open_script([{"sessionId": s, "cwd": "/x", "project": "x"} for s in sids])

    def test_every_script_starts_with_the_probe(self):
        for script in (self.script(self.SIDS[:2]), runner.engine.terms.ITERM2.run_script("cd /x && claude")):
            first = script.splitlines()[1].strip()
            self.assertEqual(first, runner.engine.terms.AE_PROBE, script)

    def test_at_the_probe_nothing_was_sent(self):
        runner.subprocess.run = refused_at(runner.engine.terms.AE_PROBE)
        res, why = runner.launch_in_iterm(self.script(self.SIDS[:3]), 1.0, self.SIDS[:3])
        self.assertIsNone(res)
        self.assertIn("refused (-1743)", why)
        self.assertIn("Automation", why)
        self.assertNotIn(runner.MAY_STILL_RUN, why)
        self.assertFalse(any(self.held_while_iterm2_lives(s) for s in self.SIDS[:3]))

    def test_after_it_they_stay(self):                                          # control
        runner.subprocess.run = refused_at("create window")
        res, why = runner.launch_in_iterm(self.script(self.SIDS[:3]), 1.0, self.SIDS[:3])
        self.assertIn("Automation", why)
        self.assertIn(runner.MAY_STILL_RUN, why)
        self.assertTrue(all(self.held_while_iterm2_lives(s) for s in self.SIDS[:3]))

    def test_after_a_write_in_one_window(self):
        sid = self.SIDS[0]
        script = runner.engine.iterm_open_script([{"sessionId": sid, "cwd": "/x", "project": "x"}],
                                                 fill={sid: "U-1"})
        runner.subprocess.run = refused_at("end repeat")          # walking on, after the write
        runner.launch_in_iterm(script, 1.0, [sid])
        self.assertTrue(self.held_while_iterm2_lives(sid))

    def test_one_with_no_place(self):
        # a -1743 osascript gives no range for: nothing says nothing was sent
        class R:
            returncode, stdout, stderr = 1, "", "Not authorized to send Apple events to iTerm2. (-1743)"
        runner.subprocess.run = lambda cmd, **kw: R()
        runner.launch_in_iterm(self.script(self.SIDS[:1]), 1.0, self.SIDS[:1])
        self.assertTrue(self.held_while_iterm2_lives(self.SIDS[0]))


class TestAStoppedLockHolderCostsABatchOneWait(unittest.TestCase):
    """After one wait for the claims lock gives up, the rest of that batch -
    its other claims, the marks, the drop - does not wait the whole time
    again; and a restore stops before it asks iTerm2 for its panes."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp
    hold_the_lock = TestAClaimsLockHeldForGoodStopsNoScan.hold_the_lock

    def test_claims_and_a_launch_of_five(self):
        self.addCleanup(setattr, runner, "CLAIMS_LOCK_SECONDS", runner.CLAIMS_LOCK_SECONDS)
        runner.CLAIMS_LOCK_SECONDS = 0.5
        self.hold_the_lock()
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        sids = [f"4f2b91ac-1111-4222-8333-{i:012d}" for i in range(5)]
        t, tables = time.monotonic(), runner._tables()
        self.assertTrue(all([runner.claim_launch(s, refresh=tables) for s in sids]))
        res, why = runner.launch_in_iterm("script", 1.0, sids)
        self.assertIsNone(res)
        self.assertIn("stopped", why)
        self.assertLess(time.monotonic() - t, 2 * runner.CLAIMS_LOCK_SECONDS)

    def test_a_launch_it_could_not_record_takes_no_send_lock(self):
        self.addCleanup(setattr, runner, "CLAIMS_LOCK_SECONDS", runner.CLAIMS_LOCK_SECONDS)
        runner.CLAIMS_LOCK_SECONDS = 0.2
        self.hold_the_lock()
        self.assertTrue(runner.claim_launch(self.SID))
        self.addCleanup(setattr, runner, "_hold_send", runner._hold_send)
        runner._hold_send = lambda: self.fail("took a send lock")
        res, why = runner.launch_in_iterm("script", 1.0, [self.SID])
        self.assertIn("could not record", why)

    def test_a_give_up_long_past_waits_again(self):
        # a click a minute after the batch that gave up waits in full
        self.addCleanup(setattr, runner, "CLAIMS_LOCK_SECONDS", runner.CLAIMS_LOCK_SECONDS)
        runner.CLAIMS_LOCK_SECONDS = 2.0
        runner._LOCAL.lock_gave_up = time.monotonic() - runner.GAVE_UP_SECONDS - 1
        self.addCleanup(runner._LOCAL.__dict__.pop, "lock_gave_up", None)
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d, exist_ok=True)
        fd = os.open(os.path.join(d, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        threading.Timer(0.1, os.close, [fd]).start()
        self.assertTrue(runner.claim_launch(self.SID))
        self.assertNotIn(self.SID, runner._unrecorded())

    def test_a_holder_that_lets_go_in_time(self):                              # control
        self.addCleanup(setattr, runner, "CLAIMS_LOCK_SECONDS", runner.CLAIMS_LOCK_SECONDS)
        runner.CLAIMS_LOCK_SECONDS = 2.0
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d, exist_ok=True)
        fd = os.open(os.path.join(d, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        threading.Timer(0.1, os.close, [fd]).start()
        self.assertTrue(runner.claim_launch(self.SID))
        self.assertNotIn(self.SID, runner._unrecorded())


class TestARestoreThatCannotRecordAsksITerm2Nothing(unittest.TestCase):
    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def test_no_pane_lookup_and_no_send(self):
        self.addCleanup(setattr, runner, "CLAIMS_LOCK_SECONDS", runner.CLAIMS_LOCK_SECONDS)
        runner.CLAIMS_LOCK_SECONDS = 0.2
        d = os.path.join(runner.ccwho_dir(), "launching")
        os.makedirs(d, exist_ok=True)
        fd = os.open(os.path.join(d, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        self.addCleanup(runner._LOCAL.__dict__.pop, "lock_gave_up", None)
        asked = []
        self.addCleanup(setattr, runner.engine.terms.ITERM2, "panes", runner.engine.terms.ITERM2.panes)
        runner.engine.terms.ITERM2.panes = lambda *a, **k: asked.append(1) or {}
        self.write_manifest([{"sessionId": s, "cwd": self.cwd(c), "project": c, "pane": "U-" + c}
                             for s, c in ((self.LIVE_SID, "a"), (self.DEAD_SID, "b"))])
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        rc, out = self._restore_open()
        self.assertEqual(rc, 1)
        self.assertIn("could not record", out)
        self.assertEqual(asked, [])


class TestOneTableForTheClaimsOfOneSend(unittest.TestCase):
    """The claims an interrupted launch left are re-dated together when its
    send lock is first seen let go of: a restore of them takes one table,
    not one a claim."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp
    sent_and_died = TestABatchJudgesEachClaimOnATableAfterIt.sent_and_died

    def counting(self, table):
        taken = []
        runner.engine.terms.app_snapshot = lambda: taken.append(1) or table
        return taken

    def test_one_send_one_table(self):
        sids = self.SIDS[:4]
        self.sent_and_died(sids)
        taken = self.counting(NO_ITERM)
        tables = runner._tables()
        self.assertEqual([runner.claim_launch(s, alive=lambda p: False, refresh=tables) for s in sids],
                         [True] * 4)
        self.assertEqual(len(taken), 1)

    def test_claims_already_bound(self):                                        # control
        sids = self.SIDS[:4]
        for s in sids:
            runner.claim_unresolved(s, 4242, now=time.time() - 60)
        taken = self.counting(NO_ITERM)                     # 4242 lives, but is no iTerm2 any more
        tables = runner._tables()
        self.assertEqual([runner.claim_launch(s, alive=lambda p: p == 4242, refresh=tables) for s in sids],
                         [True] * 4)
        self.assertEqual(len(taken), 1)

    def test_claims_dated_ahead_take_no_new_table(self):
        # after the clock was set back, less than a clock step: no table can
        # be newer than them yet - they hold, and cost no ps each
        sids = self.SIDS[:4]
        for s in sids:
            runner.claim_unresolved(s, None, now=time.time() + 600)
        taken = self.counting(NO_ITERM)
        tables = runner._tables()
        self.assertEqual([runner.claim_launch(s, alive=lambda p: False, refresh=tables) for s in sids],
                         [False] * 4)
        self.assertLessEqual(len(taken), 1)


class TestARecordFromBeforeAClockStepStillRefuses(unittest.TestCase):
    """A sighting or a launch written before the clock was set back keeps its
    times: a decision whose scan began before it must still be refused -
    re-dating it would let that decision through (a fork). It holds until
    the clock catches up: a block, never a fork."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_decision_from_before_the_sighting(self):
        real, t = time.time, time.time()
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True, "since": t - 5, "until": t})
        self.addCleanup(setattr, runner.time, "time", real)
        runner.time.time = lambda: real() - 20                     # set back 20 s
        runner.claim_launch(self.SID, decided_at=runner.time.time() - 1)   # another reads it
        runner.time.time = lambda: real() - 2                      # 18 s on
        self.assertFalse(runner.claim_launch(self.SID, decided_at=t - 3))

    def test_one_reader_inside_the_slack(self):
        ahead = time.time() + 1.8
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True, "since": ahead, "until": ahead})
        self.assertFalse(runner.claim_launch(self.SID, decided_at=time.time() + 0.5))

    def test_a_launch_two_hours_ahead_is_kept(self):
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time() + 7200,
                                       "sessionId": self.SID, "launched": True})
        self.assertFalse(runner.claim_launch(self.SID))

    def test_a_decision_after_the_sighting(self):                               # control
        t = time.time() - 10
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True, "since": t - 5, "until": t})
        self.assertTrue(runner.claim_launch(self.SID, decided_at=t + 1))


class TestTheClaimDirAndAllAboveItAreOurs(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_one_open_to_its_group_is_made_private(self):
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d)
        os.chmod(d, 0o775)
        runner._claims_dir()
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)

    def test_the_ccwho_dir_open_to_its_group_is_made_private(self):
        os.chmod(self.tmp, 0o775)
        runner._claims_dir()
        self.assertEqual(stat.S_IMODE(os.stat(self.tmp).st_mode), 0o700)

    def test_a_private_ccwho_dir_is_left_as_it_is(self):                        # control
        os.chmod(self.tmp, 0o755)
        runner._claims_dir()
        self.assertEqual(stat.S_IMODE(os.stat(self.tmp).st_mode), 0o755)

    def test_a_lock_file_with_another_name_is_not_ours(self):
        # hard-linked from elsewhere while the dir was open: another may hold it
        self.assertTrue(runner.claim_launch("99999999-9999-4999-8999-999999999999"))
        lock = os.path.join(self.tmp, "launching", ".lock")
        os.link(lock, os.path.join(self.tmp, "elsewhere.lock"))
        self.addCleanup(setattr, runner, "CLAIMS_LOCK_SECONDS", runner.CLAIMS_LOCK_SECONDS)
        runner.CLAIMS_LOCK_SECONDS = 30.0
        t = time.monotonic()
        self.assertTrue(runner.claim_launch(self.SID))
        self.assertIn(self.SID, runner._unrecorded())
        self.assertLess(time.monotonic() - t, 5)

    def test_a_claim_file_not_ours_is_no_claim(self):
        runner._write_claim(self.SID, {"pid": DEAD, "since": time.time(), "sessionId": self.SID, "launched": True})
        with mock.patch.object(runner.os, "geteuid", return_value=os.getuid() + 1):
            with self.assertRaises(ValueError):
                runner._read_claim(self.SID)
        self.assertIs(runner._read_claim(self.SID).get("launched"), True)       # control: ours


class TestTwoITerm2sBindTheClaimToBoth(unittest.TestCase):
    """With two iTerm2s of ours, the one a script's event went to is not
    known: the claim is bound to both, held while either runs - and let go
    when both were restarted."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def timed_out(self, table):
        runner.engine.terms.app_snapshot = lambda: table

        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)
        runner.subprocess.run = stuck
        runner.launch_in_iterm("script", 1.0, self.SIDS[:1])

    def test_two_after_the_send(self):
        self.timed_out(f"  PID UID UCOMM\n1111 {os.getuid()} iTerm2\n4242 {os.getuid()} iTerm2\n")
        # the lower one quits: the event may be queued in the other
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        self.assertFalse(runner.claim_launch(self.SIDS[0], now=time.time() + 3600,
                                             alive=lambda p: p == 4242))

    def test_two_when_first_judged(self):
        runner.claim_unresolved(self.SIDS[0], None, now=time.time() - 60)
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n1111 {os.getuid()} iTerm2\n4242 {os.getuid()} iTerm2\n"
        self.assertFalse(runner.claim_launch(self.SIDS[0], alive=lambda p: True))
        with open(runner._claim_path(self.SIDS[0])) as fh:
            self.assertEqual(json.load(fh)["iterm_pid"], [1111, 4242])

    def table(self, *pids):
        return "  PID UID UCOMM\n1 0 launchd\n" + "".join(f"{p} {os.getuid()} iTerm2\n" for p in pids)

    def after_two(self, *now):
        self.timed_out(self.table(1111, 2222))
        runner.engine.terms.app_snapshot = lambda: self.table(*now)
        return runner.claim_launch(self.SIDS[0], now=time.time() + 3600, alive=lambda p: p in now)

    def test_both_restarted_clears_it(self):
        self.assertTrue(self.after_two(5555, 6666))

    def test_one_of_them_still_running_holds_it(self):
        self.assertFalse(self.after_two(2222, 5555))

    def test_the_other_still_running_holds_it(self):
        self.assertFalse(self.after_two(1111))

    def test_one_at_the_send_restarted(self):                                  # control
        self.timed_out(self.table(4242))
        runner.engine.terms.app_snapshot = lambda: self.table(5555)
        self.assertTrue(runner.claim_launch(self.SIDS[0], now=time.time() + 3600, alive=lambda p: p == 5555))

    def test_one_after_the_send(self):                                          # control
        self.timed_out(ITERM_TABLE)
        with open(runner._claim_path(self.SIDS[0])) as fh:
            self.assertEqual(json.load(fh)["iterm_pid"], [4242])


class TestAnInterruptWhileMarkingSendsAndHoldsNothing(unittest.TestCase):
    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp, held_while_iterm2_lives = _F.SIDS, _F.setUp, _F.held_while_iterm2_lives

    def test_ctrl_c_on_the_second_mark(self):
        real, calls = runner.claim_unresolved, []

        def second_interrupted(*a, **k):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(*a, **k)
        runner.claim_unresolved = second_interrupted
        self.addCleanup(setattr, runner, "claim_unresolved", real)
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        with self.assertRaises(KeyboardInterrupt):
            runner.launch_in_iterm("script", 1.0, self.SIDS[:3])
        runner.claim_unresolved = real
        self.assertFalse(any(self.held_while_iterm2_lives(s) for s in self.SIDS[:3]))
        self.assertEqual([n for n in os.listdir(os.path.join(self.tmp, "launching")) if n.endswith(".send")], [])


class TestTheWordsForAClockAndARefusal(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_the_old_list_text(self):
        self.assertIn("too old", runner.OLD_LIST)
        self.assertNotIn("clock", runner.OLD_LIST)

    def test_a_refusal_with_no_place_is_not_part_way(self):
        class R:
            returncode, stdout, stderr = 1, "", "Not authorized to send Apple events to iTerm2. (-1743)"
        self.assertTrue(runner.claim_launch(self.SID))
        runner.subprocess.run = lambda cmd, **kw: R()
        res, why = runner.launch_in_iterm(runner.engine.terms.ITERM2.run_script("cd /x && claude"), 1.0, [self.SID])
        self.assertNotIn("part way", why)
        self.assertIn(runner.MAY_STILL_RUN, why)

    def test_a_refusal_placed_after_the_probe_is(self):                         # control
        self.assertTrue(runner.claim_launch(self.SID))
        runner.subprocess.run = refused_at("create window")
        res, why = runner.launch_in_iterm(runner.engine.terms.ITERM2.run_script("cd /x && claude"), 1.0, [self.SID])
        self.assertIn("part way", why)

    def test_a_decision_a_moment_ahead(self):
        self.assertTrue(runner.claim_launch(self.SID, decided_at=time.time() + runner.CLOCK_SLACK_SECONDS / 2))

    def test_a_decision_further_ahead(self):                                    # control
        self.assertFalse(runner.claim_launch(self.SID, decided_at=time.time() + runner.CLOCK_SLACK_SECONDS * 2))


class TestABusyLockDefersTheSweepToo(unittest.TestCase):
    """A scan that could not write its sightings must not sweep: the claims
    of sessions it saw running would go without them."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_the_first_try_busy(self):
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": self.T - 7300,
                                       "sessionId": self.SID, "launched": True})
        os.utime(runner._claim_path(self.SID), (self.T - 7300, self.T - 7300))
        real, tries = runner._claims_locked, []

        def first_busy(**kw):
            tries.append(1)
            if len(tries) == 1:
                raise BlockingIOError(errno.EWOULDBLOCK, "busy")
            return real(**kw)
        runner._claims_locked = first_busy
        self.addCleanup(setattr, runner, "_claims_locked", real)
        runner.release_claims([self.SID], self.T)
        self.assertTrue(os.path.exists(runner._claim_path(self.SID)))


class TestThePlaceOfARealOsascriptError(unittest.TestCase):
    """osascript's own range for an error at the probe: run on the script's
    head, aimed at osascript itself - no event goes to iTerm2."""

    @unittest.skipUnless(shutil.which("osascript"), "macOS only")
    def test_the_probe_is_placed(self):
        # the head of a launch script, to its first statement after the probe
        head = runner.engine.terms.ITERM2.run_script("cd /x && claude").split("  activate\n")[0]
        script = head.replace('application "iTerm2"', "current application") + "  activate\nend tell\n"
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertTrue(runner.engine.terms.ae_error_at(r.stderr, script, runner.engine.terms.AE_PROBE), r.stderr)

    @unittest.skipUnless(shutil.which("osascript"), "macOS only")
    def test_a_probe_last_in_its_block_is_not(self):                           # control
        script = "tell current application\n  %s\nend tell\n" % runner.engine.terms.AE_PROBE
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertFalse(runner.engine.terms.ae_error_at(r.stderr, script, runner.engine.terms.AE_PROBE), r.stderr)

    def test_a_range_before_the_probe_keeps_them(self):
        script = runner.engine.terms.ITERM2.run_script("cd /x && claude")
        err = f"0:{len(script)}: execution error: Not authorized to send Apple events to iTerm2. (-1743)"
        self.assertFalse(runner.engine.terms.ae_error_at(err, script, runner.engine.terms.AE_PROBE))


class TestTheClockUnderTheLockJudgesADecision(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_set_back_while_waiting_for_the_lock(self):
        # the call began at T+3; while it waited the clock went back 100 s:
        # its decision at T is from after now
        real, t = time.time, time.time()
        self.addCleanup(setattr, runner.time, "time", real)
        runner.time.time = lambda: real() - 100
        self.assertFalse(runner.claim_launch(self.SID, now=t + 3, decided_at=t))
        self.assertEqual(runner.why_held(self.SID)[0], "old list")


class TestARollbackWaitsForTheLock(unittest.TestCase):
    """A launch that was not sent lets go of its claims even after a wait
    for the lock gave up in its thread - or a session never sent stays
    "unresolved" behind a restart of iTerm2."""

    SIDS = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent.SIDS
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_holder_that_lets_go_during_it(self):
        self.addCleanup(setattr, runner, "CLAIMS_LOCK_SECONDS", runner.CLAIMS_LOCK_SECONDS)
        runner.CLAIMS_LOCK_SECONDS = 2.0
        a, b = self.SIDS[:2]
        self.assertTrue(runner.claim_launch(a))
        self.assertTrue(runner.claim_launch(b))
        runner._LOCAL.lock_gave_up = time.monotonic()               # a wait just gave up
        self.addCleanup(runner._LOCAL.__dict__.pop, "lock_gave_up", None)
        fd = os.open(os.path.join(self.tmp, "launching", ".lock"), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        threading.Timer(0.2, os.close, [fd]).start()
        runner.drop_claims([a, b])
        self.assertEqual([os.path.exists(runner._claim_path(s)) for s in (a, b)], [False, False])

    def test_nothing_of_ours_takes_no_lock(self):
        TestAClaimsLockHeldForGoodStopsNoScan.hold_the_lock(self)
        t = time.monotonic()
        runner.drop_claims(["4f2b91ac-7777-4777-8777-777777777777"])        # never claimed
        self.assertLess(time.monotonic() - t, 1.0)


class TestALockThatCannotWorkIsNoWait(unittest.TestCase):
    """flock failing for a reason other than another holder - a file system
    without it - is no lock to wait for, and no stopped ccwho."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_not_supported(self):
        real = runner.fcntl

        class NoFlock:
            LOCK_EX, LOCK_NB, LOCK_SH = fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_SH

            def flock(self, fd, op):
                raise OSError(errno.ENOTSUP, "not supported")
        self.addCleanup(runner._LOCAL.__dict__.pop, "lock_gave_up", None)
        self.addCleanup(setattr, runner, "fcntl", real)
        runner.fcntl = NoFlock()        # ccwho's own lock: the engine's is not used
        t = time.monotonic()
        self.assertTrue(runner.claim_launch(self.SID))
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertIsNone(runner._LOCAL.__dict__.get("lock_gave_up"))


class TestARefusalSaysItsReason(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def why(self):
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        self.assertTrue(runner.claim_launch(self.SID))
        return runner.launch_in_iterm("script", 1.0, [self.SID])[1]

    def test_a_lock_file_with_another_name(self):
        self.assertTrue(runner.claim_launch("99999999-9999-4999-8999-999999999999"))
        lock = os.path.join(self.tmp, "launching", ".lock")
        os.link(lock, os.path.join(self.tmp, "elsewhere.lock"))
        why = self.why()
        self.assertIn(lock, why)
        self.assertIn("remove it", why)

    def test_a_ccwho_dir_not_ours(self):
        with mock.patch.object(runner.os, "geteuid", return_value=os.getuid() + 1):
            why = self.why()
        self.assertIn("not a dir of this user's own", why)

    def test_a_lock_only_busy(self):                                            # control
        self.addCleanup(setattr, runner, "CLAIMS_LOCK_SECONDS", runner.CLAIMS_LOCK_SECONDS)
        runner.CLAIMS_LOCK_SECONDS = 0.2
        TestAClaimsLockHeldForGoodStopsNoScan.hold_the_lock(self)
        why = self.why()
        self.assertIn("stopped", why)
        self.assertNotIn("remove it", why)


class TestARedateLeavesAnotherSendAlone(unittest.TestCase):
    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_a_send_still_under_way_holds(self):
        a, b = self.SIDS[0], self.SIDS[1]
        done, fd = runner._hold_send()
        self.assertTrue(runner.claim_unresolved(a, None, send=done))
        os.close(fd)                                          # its launcher died
        busy, fd2 = runner._hold_send()                       # another launch, being sent now
        self.addCleanup(os.close, fd2)
        self.assertTrue(runner.claim_unresolved(b, None, send=busy))
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        tables = runner._tables()
        self.assertTrue(runner.claim_launch(a, alive=lambda p: False, refresh=tables))
        with open(runner._claim_path(b)) as fh:
            self.assertEqual(json.load(fh).get("send"), busy)
        self.assertFalse(runner.claim_launch(b, alive=lambda p: False, refresh=tables))


class TestEachOwnerCheckOnItsOwn(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_the_dir(self):
        with mock.patch.object(runner.os, "geteuid", return_value=os.getuid() + 1):
            with self.assertRaises(OSError):
                runner._claims_dir()

    def test_the_lock_file(self):
        d = runner._claims_dir()
        open(os.path.join(d, ".lock"), "w").close()
        self.addCleanup(setattr, runner, "_claims_dir", runner._claims_dir)
        runner._claims_dir = lambda: d
        with mock.patch.object(runner.os, "geteuid", return_value=os.getuid() + 1):
            with self.assertRaises(OSError):
                with runner._claims_locked():
                    pass

    def test_ours(self):                                                        # control
        with runner._claims_locked():
            pass

    def test_a_ccwho_dir_that_is_a_link_of_ours(self):
        real = os.path.join(self.tmp, "real")
        os.mkdir(real, 0o700)
        link = os.path.join(self.tmp, "link")
        os.symlink(real, link)
        os.environ["CCWHO_DIR"] = link
        self.assertTrue(runner.claim_launch(self.SID))
        self.assertNotIn(self.SID, runner._unrecorded())
        self.assertTrue(os.path.exists(os.path.join(real, "launching", self.SID + ".json")))


class TheClocks:
    """Move this process's clocks: the wall clock, the one that counts sleep
    (monotonic) and the one that does not (uptime), each by its own amount."""

    def move(self, wall=0.0, mono=0.0, up=0.0):
        real = (time.time, runner._mono, runner._uptime)
        if not hasattr(self, "_clocks"):
            self._clocks = real
            self.addCleanup(lambda: (setattr(runner.time, "time", self._clocks[0]),
                                     setattr(runner, "_mono", self._clocks[1]),
                                     setattr(runner, "_uptime", self._clocks[2])))
        w, m, u = self._clocks
        runner.time.time = lambda: w() + wall
        runner._mono = lambda: m() + mono
        runner._uptime = lambda: u() + up


class TestAClockStepBetweenAScanAndItsClaim(TheClocks, unittest.TestCase):
    """A scan stamped by a clock later set back, judged after the clock caught
    up with its stamp: against a sighting or a launch written after the step,
    its order is kept by a clock that never steps."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_decision_from_before_the_step(self):
        decided_at, decided_mono = time.time() + 20, runner._mono()   # its clock 20 s fast
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time(),
                                       "sessionId": self.SID, "launched": True})
        runner.release_claims([self.SID], time.time())               # seen running
        self.move(wall=25, mono=25, up=25)
        self.assertFalse(runner.claim_launch(self.SID, decided_at=decided_at, decided_mono=decided_mono))

    def test_a_decision_after_the_sighting(self):                               # control
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time(),
                                       "sessionId": self.SID, "launched": True})
        runner.release_claims([self.SID], time.time())
        self.move(wall=25, mono=25, up=25)
        self.assertTrue(runner.claim_launch(self.SID, decided_at=time.time(), decided_mono=runner._mono()))

    def test_a_launch_across_a_step_back_longer_than_its_hold(self):
        t, m = time.time(), runner._mono()
        decided = (t - 3, m - 3)                                             # a scan just before it
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": t, "sessionId": self.SID,
                                       "launched": True})
        self.move(wall=-200)
        runner.claim_launch(self.SID, decided_at=time.time() - 1, decided_mono=runner._mono() - 1)
        self.move(wall=-2, mono=198, up=198)                                 # 198 s on, set back 200
        self.assertFalse(runner.claim_launch(self.SID, decided_at=decided[0], decided_mono=decided[1]))

    def test_a_launch_stamped_by_a_slow_clock(self):
        # launched at t by a clock 200 s slow, later set right: a scan from
        # just before the launch must still be older than it
        t, m, u = time.time(), runner._mono(), runner._uptime()
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": t - 200, "sessionId": self.SID,
                                       "launched": True, "clock": [runner._boot_id(), m, u]})
        self.move(wall=100, mono=100, up=100)
        self.assertFalse(runner.claim_launch(self.SID, decided_at=t - 3, decided_mono=m - 3))

    def test_a_scan_after_that_launch(self):                                   # control
        t, m, u = time.time(), runner._mono(), runner._uptime()
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": t - 200, "sessionId": self.SID,
                                       "launched": True, "clock": [runner._boot_id(), m, u]})
        self.move(wall=100, mono=100, up=100)
        self.assertTrue(runner.claim_launch(self.SID, decided_at=t + 3, decided_mono=m + 3))

    def test_a_sweep_after_a_step_forward_keeps_a_new_sighting(self):
        t = time.time()
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True, "since": t - 1, "until": t})
        os.utime(runner._claim_path(self.SID), (t - 7200, t - 7200))    # its file looks old
        runner.release_claims([], t)
        self.assertTrue(os.path.exists(runner._claim_path(self.SID)))

    def test_a_launch_written_while_the_clock_ran_two_hours_fast(self):
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time() + 7200,
                                       "sessionId": self.SID, "launched": True,
                                       "clock": [runner._boot_id(), runner._mono(), runner._uptime()]})
        self.move(wall=91, mono=91, up=91)
        self.assertTrue(runner.claim_launch(self.SID))


class TestALaunchIsHeldForItsTimeAwake(TheClocks, unittest.TestCase):
    """The lid closed a moment after a launch: its sessions start when the Mac
    wakes. The hold counts time awake, not the hour asleep."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def launched(self):
        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda *a, **k: Done()
        runner.launch_in_iterm("script", 1.0, self.SIDS[:1])

    def test_an_hour_asleep(self):
        self.launched()
        self.move(wall=3600, mono=3600, up=10)
        self.assertFalse(runner.claim_launch(self.SIDS[0], alive=lambda p: False, decided_at=time.time() - 2,
                                             decided_mono=runner._mono() - 2))

    def test_its_time_awake_over(self):                                         # control
        self.launched()
        self.move(wall=3600, mono=3600, up=91)
        self.assertTrue(runner.claim_launch(self.SIDS[0], alive=lambda p: False, decided_at=time.time() - 2,
                                            decided_mono=runner._mono() - 2))


class TestASightingKeepsALaunchFromBeforeABigStep(TheClocks, unittest.TestCase):
    """A launch dated hours ahead that no clock of this boot can place (from
    another boot, or none stamped) - written before the clock was set back -
    seen running: it keeps refusing decisions older than it, sweep or not."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_seen_then_swept(self):
        t, step = time.time(), 7200.0
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": t + step, "sessionId": self.SID,
                                       "launched": True, "clock": ["ANOTHER-BOOT", 0.0, 0.0]})
        os.utime(runner._claim_path(self.SID), (t + step, t + step))     # the pre-step clock stamped it
        runner.release_claims([self.SID], t)                  # the list sees it running
        self.move(wall=step - 2, mono=step - 2, up=step - 2)  # the clock caught up
        runner.release_claims([], time.time())                # a later sweep
        self.assertFalse(runner.claim_launch(self.SID, decided_at=t + step - 5))


class TestAReDateIsStampedAnew(unittest.TestCase):
    """A claim re-dated when its send is seen over is stamped at its new time:
    read again, it is not put back to the time before the send - where a
    table taken during the send would count as newer than it."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_read_again_after_the_re_date(self):
        sid, batch = self.SIDS[0], runner._tables()
        token, fd = runner._hold_send()
        self.assertTrue(runner.claim_unresolved(sid, None, send=token))
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        batch()                                               # taken during the send
        os.close(fd)                                          # the launcher died
        time.sleep(0.01)
        unreadable = lambda after=None: ("", time.time())
        self.assertFalse(runner.claim_launch(sid, alive=lambda p: False, refresh=unreadable))  # re-dated
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE      # the iTerm2 its osascript started
        self.assertFalse(runner.claim_launch(sid, alive=lambda p: p == 4242, refresh=batch))


class TestAnITermPidListIsReadWithCare(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def claim(self, iterm, age=60):
        runner._write_claim(self.SID, {"pid": DEAD, "since": time.time() - age, "sessionId": self.SID,
                                       "unresolved": True, "iterm_pid": iterm, "boot": "THIS-BOOT"})
        if age > runner.SIGHTING_SECONDS:
            os.utime(runner._claim_path(self.SID), (time.time() - age, time.time() - age))

    def test_a_pid_no_process_can_have(self):
        for bad in ([2 ** 40, 2 ** 41], [True], ["1"], [4242, 0]):
            self.claim(bad)
            self.assertTrue(runner.claim_launch(self.SID), bad)

    def test_a_list_with_a_live_iterm2_holds(self):                              # control
        self.claim([4242, os.getpid()])
        runner.engine.terms.app_snapshot = lambda: f"  PID UID UCOMM\n{os.getpid()} {os.getuid()} iTerm2\n"
        self.assertFalse(runner.claim_launch(self.SID))

    def test_the_sweep_lets_one_of_dead_pids_go(self):
        self.claim([DEAD, DEAD - 1], age=7200)
        runner.release_claims([], time.time())
        self.assertFalse(os.path.exists(runner._claim_path(self.SID)))

    def test_the_sweep_keeps_one_with_a_live_pid(self):                         # control
        self.claim([DEAD, os.getpid()], age=7200)
        runner.release_claims([], time.time())
        self.assertTrue(os.path.exists(runner._claim_path(self.SID)))


class TestAReasonIsTheOneOfItsFailure(unittest.TestCase):
    """A reason kept from an earlier refusal must not be given for a later,
    other one in the same thread."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_busy_lock_then_a_failed_write(self):
        self.addCleanup(setattr, runner, "CLAIMS_LOCK_SECONDS", runner.CLAIMS_LOCK_SECONDS)
        runner.CLAIMS_LOCK_SECONDS = 0.2
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d, exist_ok=True)
        fd = os.open(os.path.join(d, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(runner._LOCAL.__dict__.pop, "lock_gave_up", None)
        self.assertTrue(runner.claim_launch(self.SID))                    # busy: "stopped"
        os.close(fd)
        runner._LOCAL.__dict__.pop("lock_gave_up", None)
        self.addCleanup(setattr, runner, "_write_claim", runner._write_claim)
        runner._write_claim = lambda sid, record: False                   # now the disk is full
        self.assertTrue(runner.claim_launch(self.SID))
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        why = runner.launch_in_iterm("script", 1.0, [self.SID])[1]
        self.assertNotIn("stopped", why)


class TestAFlockErrorNamesTheLock(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_not_supported(self):
        real = runner.fcntl

        class NoFlock:
            LOCK_EX, LOCK_NB, LOCK_SH = fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_SH

            def flock(self, fd, op):
                raise OSError(errno.ENOTSUP, "Operation not supported")
        self.addCleanup(setattr, runner, "fcntl", real)
        runner.fcntl = NoFlock()        # ccwho's own: the list reloads the engine, never ccwho
        self.assertTrue(runner.claim_launch(self.SID))
        runner.fcntl = real
        runner.subprocess.run = lambda *a, **k: self.fail("sent")
        why = runner.launch_in_iterm("script", 1.0, [self.SID])[1]
        self.assertIn(os.path.join(self.tmp, "launching", ".lock"), why)


class TestTheSweepKeepsALaunchStillHeldAwake(TheClocks, unittest.TestCase):
    """A launch held for its time awake is kept by the sweep too: the first
    scan after an hour asleep must not remove what the judge still holds."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def launched(self, cut_off=False):
        sid = self.SIDS[0]
        if cut_off:
            class Gone:
                returncode, stdout, stderr = 1, "", "execution error: gone. (-609)"
            runner.subprocess.run = lambda *a, **k: Gone()
        else:
            class Done:
                returncode, stdout, stderr = 0, "", ""
            runner.subprocess.run = lambda *a, **k: Done()
        runner.launch_in_iterm("script", 1.0, [sid])
        return sid

    def woke(self, sid, awake):
        self.move(wall=3600, mono=3600, up=awake)
        runner.release_claims([], time.time(), runner._mono())       # the scan after waking, swept
        return runner.claim_launch(sid, alive=lambda p: False, decided_at=time.time() - 2,
                                   decided_mono=runner._mono() - 2)

    def test_an_hour_asleep_ten_seconds_awake(self):
        sid = self.launched()
        self.assertFalse(self.woke(sid, 10))
        self.assertEqual(runner.why_held(sid), ("starting", 0))

    def test_cut_off_and_asleep(self):
        sid = self.launched(cut_off=True)
        self.assertFalse(self.woke(sid, 10))
        reason, left = runner.why_held(sid)
        self.assertEqual(reason, "cut off")
        self.assertTrue(75 <= left <= 81, left)

    def test_its_time_awake_over(self):                                         # control
        sid = self.launched()
        self.assertTrue(self.woke(sid, 91))


class TestNoWallClockAgesALaunch(TheClocks, unittest.TestCase):
    """A launch's age is its time awake: a wall clock set back while a claim
    waited for the lock does not make it old."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_set_back_while_it_waited(self):
        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda *a, **k: Done()
        sid = self.SIDS[0]
        runner.launch_in_iterm("script", 1.0, [sid])
        real = runner._claims_locked

        @contextlib.contextmanager
        def stepped(**kw):
            self.move(wall=-200)                                  # while it waited
            with real(**kw):
                yield
        runner._claims_locked = stepped
        self.addCleanup(setattr, runner, "_claims_locked", real)
        self.assertFalse(runner.claim_launch(sid, alive=lambda p: False))


class TestOneJudgementReadsOneClock(TheClocks, unittest.TestCase):
    """A decision and the record it is judged against are put on the wall
    clock by the same reading: a step between them misorders nothing."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_step_inside_the_judgement(self):
        m = runner._mono()
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True, "since": time.time() - 1,
                                       "until": time.time()})
        real = runner._read_claim

        def read_after_a_step(sid):
            self.move(wall=-200)                   # after the decision was put on the clock
            return real(sid)
        runner._read_claim = read_after_a_step
        self.addCleanup(setattr, runner, "_read_claim", real)
        self.assertFalse(runner.claim_launch(self.SID, decided_at=time.time() - 20, decided_mono=m - 20))


class TestAStampIsTheRecordsOwnTime(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_slow_boot_id_does_not_move_it(self):
        runner._boot_id = lambda: (time.sleep(0.003), "THIS-BOOT")[1]    # a cold first call
        t = time.time()
        runner._write_claim(self.SID, {"pid": 1, "owner": "x", "since": t, "sessionId": self.SID})
        self.assertAlmostEqual(runner._read_claim(self.SID)["since"], t, delta=0.0005)


class TestAClockNoCcwhoWroteIsNoClaim(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def raw(self, clock, age=10):
        path = runner._claim_path(self.SID)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write('{"pid": 4000000, "owner": "x", "since": %r, "sessionId": "%s", "launched": true,'
                     ' "clock": %s}' % (time.time() - age, self.SID, clock))

    BAD = ("[]", "{}", '["THIS-BOOT"]', '["THIS-BOOT", "x", 0]', '["THIS-BOOT", 0, "x"]',
           '["THIS-BOOT", ' + "1" + "0" * 400 + ', 0]', '["x", 0, -' + "1" + "0" * 400 + "]",
           '["THIS-BOOT", true, 0]', "[1, 0, 0]")

    def test_each_of_them(self):
        for bad in self.BAD:
            self.raw(bad)
            with self.assertRaises(ValueError, msg=bad):
                runner._read_claim(self.SID)
            runner.release_claims([self.SID], time.time())
            self.assertFalse(os.path.exists(runner._claim_path(self.SID)), bad)
            self.raw(bad)
            self.assertTrue(runner.claim_launch(self.SID), bad)

    def test_swept_too(self):
        self.raw('["x", 0, ' + "1" + "0" * 400 + "]", age=7200)
        os.utime(runner._claim_path(self.SID), (time.time() - 7200, time.time() - 7200))
        runner.release_claims([], time.time())
        self.assertFalse(os.path.exists(runner._claim_path(self.SID)))

    def test_a_clock_of_floats(self):                                           # control
        self.raw('["x", 1e300, 0]')
        self.assertIs(runner._read_claim(self.SID).get("launched"), True)


class TestTheScansClocksReachTheJudgement(TheClocks, unittest.TestCase):
    """open and restore order their scan by the clock that never steps -
    through scan(), release_claims and claim_launch as they run."""

    _F = TestOpenNeverForksALiveSession
    SID, ENTRY, tearDown, _open = _F.SID, _F.ENTRY, _F.tearDown, _F._open

    def setUp(self):
        self._F.setUp(self)

        def stepped_scan(cache=None, status=None):
            status["seen_at"] += 20                      # stamped by a clock 20 s fast
            time.sleep(0.01)
            runner.release_claims([self.SID], time.time(), runner._mono())   # another scan sees it
            self.move(wall=25, mono=25, up=25)
            status["source_ok"] = True
            return [], 0
        runner.engine.collect = stepped_scan
        os.makedirs(os.path.join(runner.ccwho_dir(), "launching"), exist_ok=True)
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time() - 1,
                                       "sessionId": self.SID, "launched": True})

    def test_open(self):
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1, out)
        self.assertNotIn("claude --resume", " ".join(" ".join(c) for c in self.runs))

    def test_a_release_by_its_start(self):
        # a launch 5 s after a scan whose wall stamp ran 20 s fast: the scan
        # started before it - it stays a launch
        m = runner._mono()
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time() + 5,
                                       "sessionId": self.SID, "launched": True,
                                       "clock": [runner._boot_id(), m + 5, runner._uptime() + 5]})
        runner.release_claims([self.SID], time.time() + 20, m)
        with open(runner._claim_path(self.SID)) as fh:
            self.assertIs(json.load(fh).get("launched"), True)


class TestTheRestoresScanReachesTheJudgement(TheClocks, unittest.TestCase):
    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    tearDown, write_manifest, _restore_open = _F.tearDown, _F.write_manifest, _F._restore_open

    def setUp(self):
        self._F.setUp(self)
        self.write_manifest([{"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"}])

        def stepped_scan(cache=None, status=None):
            status["seen_at"] += 20
            time.sleep(0.01)
            runner.release_claims([self.DEAD_SID], time.time(), runner._mono())
            self.move(wall=25, mono=25, up=25)
            status["source_ok"] = True
            return [], 0
        runner.engine.collect = stepped_scan
        runner._write_claim(self.DEAD_SID, {"pid": DEAD, "owner": "x", "since": time.time() - 1,
                                            "sessionId": self.DEAD_SID, "launched": True})

    def test_restore(self):
        rc, out = self._restore_open()
        self.assertNotEqual(rc, 0, out)
        self.assertNotIn("claude --resume", " ".join(" ".join(c) for c in self.runs))


class TestEveryClaimOfASendIsStampedAnew(unittest.TestCase):
    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_the_other_one_read_again(self):
        a, b, batch = self.SIDS[0], self.SIDS[1], runner._tables()
        token, fd = runner._hold_send()
        for sid in (a, b):
            self.assertTrue(runner.claim_unresolved(sid, None, send=token))
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        batch()                                               # during the send
        os.close(fd)
        time.sleep(0.01)
        unreadable = lambda after=None: ("", time.time())
        self.assertFalse(runner.claim_launch(a, alive=lambda p: False, refresh=unreadable))   # re-dates both
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        self.assertFalse(runner.claim_launch(b, alive=lambda p: p == 4242, refresh=batch))


class TestACutOffWaitIsItsTimeAwake(TheClocks, unittest.TestCase):
    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_an_hour_asleep(self):
        class Gone:
            returncode, stdout, stderr = 1, "", "execution error: gone. (-609)"
        runner.subprocess.run = lambda *a, **k: Gone()
        runner.launch_in_iterm("script", 1.0, self.SIDS[:1])
        self.move(wall=3600, mono=3600, up=10)
        self.assertFalse(runner.claim_launch(self.SIDS[0], alive=lambda p: False))
        reason, left = runner.why_held(self.SIDS[0])
        self.assertEqual(reason, "cut off")
        self.assertTrue(75 <= left <= 81, left)


class TestABatchTablesTimeFollowsTheClock(TheClocks, unittest.TestCase):
    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def test_a_claim_newer_than_the_table_after_a_step(self):
        sid, batch = self.SIDS[0], runner._tables()
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        batch()
        time.sleep(0.01)
        runner.claim_unresolved(sid, None)                     # written after the table
        self.move(wall=5, mono=10, up=10)                      # 10 s on, the clock set back 5
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        self.assertFalse(runner.claim_launch(sid, alive=lambda p: p == 4242, refresh=batch))


class TestAMonotonicDecisionIsNotOldForAClockStep(TheClocks, unittest.TestCase):
    """A decision stamped on the monotonic clock is as old as it is: a wall
    clock set back since does not make it "from the future", nor "old"."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_set_back_by_more_than_the_slack(self):
        decided = (time.time(), runner._mono())
        self.move(wall=-700)
        self.assertTrue(runner.claim_launch(self.SID, decided_at=decided[0], decided_mono=decided[1]))

    def test_one_that_is_old(self):                                              # control
        decided = (time.time(), runner._mono())
        self.move(wall=700, mono=700, up=700)
        self.assertFalse(runner.claim_launch(self.SID, decided_at=decided[0], decided_mono=decided[1]))
        self.assertEqual(runner.why_held(self.SID)[0], "old list")


class TestSetupPassesWhyITerm2WasNotAsked(SetupHarness):
    """setup hands the gate's reason to the hotkey install - fresh or not."""

    def installed_with(self, argv, installed):
        self.facts.update(iterm_ok=None, iterm_why="no-table")
        seen = []
        self.addCleanup(setattr, runner, "install_hotkey", runner.install_hotkey)
        runner.install_hotkey = lambda *a, **k: seen.append(k.get("iterm_why")) or 0
        self.addCleanup(setattr, runner.setup, "hotkey_installed", runner.setup.hotkey_installed)
        runner.setup.hotkey_installed = lambda *a, **k: installed
        self.run_setup(argv)
        return seen

    def test_a_fresh_install(self):
        self.assertEqual(self.installed_with(["--yes", "--no-list"], False), ["no-table"])

    def test_a_profile_rewritten(self):
        self.assertEqual(self.installed_with(["--yes", "--no-list"], True), ["no-table"])


class TestAStampIsReadWithItsTime(TheClocks, unittest.TestCase):
    """A record's time and its clocks are read at one instant, inside the lock
    where it is written: a clock step or a sleep while its writer waited for
    the lock neither ages nor back-dates it."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def moved_in_the_wait(self, **move):
        real = runner._claims_locked

        @contextlib.contextmanager
        def waited(**kw):
            self.move(**move)
            with real(**kw):
                yield
        runner._claims_locked = waited
        self.addCleanup(setattr, runner, "_claims_locked", real)    # a test that fails too
        return lambda: setattr(runner, "_claims_locked", real)

    def launched_then_asked(self, **move):
        sid = self.SIDS[0]
        undo = self.moved_in_the_wait(**move)
        runner.claim_launched([sid])
        undo()
        return sid, runner.claim_launch(sid, alive=lambda p: False, decided_at=time.time(),
                                        decided_mono=runner._mono())

    def test_a_step_forward_in_the_wait(self):
        sid, took = self.launched_then_asked(wall=120)
        self.assertFalse(took)
        self.assertEqual(runner.why_held(sid), ("starting", 0))

    def test_a_sleep_in_the_wait(self):
        sid, took = self.launched_then_asked(wall=3600, mono=3600)
        self.assertFalse(took)

    def test_no_move(self):                                                     # control
        sid, took = self.launched_then_asked()
        self.assertFalse(took)

    def test_awake_time_after_the_mark(self):                                  # control
        sid = self.SIDS[0]
        runner.claim_launched([sid])
        up = runner.LAUNCH_CLAIM_SECONDS + 30
        self.move(wall=up, mono=up, up=up)
        self.assertTrue(runner.claim_launch(sid, alive=lambda p: False, decided_at=time.time(),
                                            decided_mono=runner._mono()))

    def test_an_unresolved_mark(self):
        sid = self.SIDS[0]
        undo = self.moved_in_the_wait(wall=120)
        runner.claim_unresolved(sid, None)
        undo()
        self.assertAlmostEqual(runner._read_claim(sid)["since"], time.time(), delta=1.0)

    def test_a_new_claim(self):
        sid = "4f2b91ac-3333-4333-8333-000000000001"
        undo = self.moved_in_the_wait(wall=200)
        self.assertTrue(runner.claim_launch(sid))
        undo()
        self.assertLess(runner._age(runner._read_claim(sid), time.time()), 1.0)


class TestAnOldListIsOldByTheMonotonicClock(TheClocks, unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_set_back_in_the_wait(self):
        decided = (time.time() - 2, runner._mono() - 2)
        real = runner._claims_locked

        @contextlib.contextmanager
        def waited(**kw):
            self.move(wall=-700)
            with real(**kw):
                yield
        runner._claims_locked = waited
        self.addCleanup(setattr, runner, "_claims_locked", real)
        self.assertTrue(runner.claim_launch(self.SID, decided_at=decided[0], decided_mono=decided[1]))

    def test_one_that_is_old(self):                                              # control
        self.assertFalse(runner.claim_launch(self.SID, decided_at=time.time() - 601,
                                             decided_mono=runner._mono() - 601))
        self.assertEqual(runner.why_held(self.SID)[0], "old list")


class TestAStampFarAheadIsNoClaim(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def write(self, clock):
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time() - 7200,
                                       "sessionId": self.SID, "launched": True, "clock": clock})
        os.utime(runner._claim_path(self.SID), (time.time() - 7200, time.time() - 7200))

    def test_its_uptime_or_its_monotonic_time(self):
        for clock in (["THIS-BOOT", runner._mono() - 7200, runner._uptime() + 1e7],
                      ["THIS-BOOT", runner._mono() + 1e7, runner._uptime() - 7200]):
            self.write(clock)
            with self.assertRaises(ValueError, msg=clock):
                runner._read_claim(self.SID)
            with contextlib.suppress(FileNotFoundError):         # a sweep is due each time
                os.remove(os.path.join(self.tmp, "launching", ".swept"))
            runner.release_claims([], time.time(), runner._mono())
            self.assertFalse(os.path.exists(runner._claim_path(self.SID)), clock)

    def test_an_unresolved_launch_too(self):
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time(),
                                       "sessionId": self.SID, "unresolved": True, "iterm_pid": None,
                                       "clock": ["THIS-BOOT", runner._mono() + 1e7, runner._uptime()]})
        with self.assertRaises(ValueError):
            runner._read_claim(self.SID)

    def test_a_stamp_it_wrote(self):                                             # control
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time(),
                                       "sessionId": self.SID, "launched": True})
        self.assertLess(abs(runner._age(runner._read_claim(self.SID), time.time())), 1.0)


class TestATablesTimeIsPutOnTheClockUnderTheLock(TheClocks, unittest.TestCase):
    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS, _F.setUp

    def judged(self, step):
        sid = self.SIDS[0]
        runner.claim_unresolved(sid, [4242], now=time.time() - 60)
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        real, entries = runner._claims_locked, []

        @contextlib.contextmanager
        def second_moves(**kw):
            entries.append(1)
            if len(entries) == 2:
                self.move(wall=step)
            with real(**kw):
                yield
        runner._claims_locked = second_moves
        self.addCleanup(setattr, runner, "_claims_locked", real)
        return runner.claim_launch(sid, alive=lambda p: p == 4242)

    def test_set_back(self):
        self.assertTrue(self.judged(-200))

    def test_set_forward(self):
        self.assertTrue(self.judged(200))

    def test_no_step(self):                                                     # control
        self.assertTrue(self.judged(0))

    def test_a_claim_no_table_can_be_after(self):                              # control
        sid = self.SIDS[0]
        runner.claim_unresolved(sid, [4242], now=time.time() + 30)
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        self.assertFalse(runner.claim_launch(sid, alive=lambda p: p == 4242))
        self.assertEqual(runner.why_held(sid), ("unresolved", 0))


class TestTheSweepPutsItsScanOnTheClockUnderTheLock(TheClocks, unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_scan_before_a_step_back(self):
        seen_at, seen_mono = time.time(), runner._mono()
        self.move(wall=-700)
        t = time.time()
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True, "since": t - 1, "until": t})
        os.utime(runner._claim_path(self.SID), (t - 7200, t - 7200))
        runner.release_claims([], seen_at, seen_mono)
        self.assertTrue(os.path.exists(runner._claim_path(self.SID)))

    def test_an_old_sighting(self):                                              # control
        t = time.time()
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True, "since": t - 1, "until": t})
        os.utime(runner._claim_path(self.SID), (t - 7200, t - 7200))
        self.move(wall=700, mono=700, up=700)
        runner.release_claims([], time.time(), runner._mono())
        self.assertFalse(os.path.exists(runner._claim_path(self.SID)))


class TestScanHandsItsStartToTheRelease(TheClocks, unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def scan(self, write_during):
        test = self

        class Eng:
            @staticmethod
            def collect(cache=None, status=None):
                if write_during:
                    test.move(wall=-25)                 # set back while `claude agents` ran
                    runner._write_claim(test.SID, {"pid": DEAD, "owner": "x", "since": time.time(),
                                                   "sessionId": test.SID, "launched": True})
                return [{"sessionId": test.SID}], 0

            @staticmethod
            def live_ids(rows):
                return [test.SID]
        runner.scan(cache={}, status={}, eng=Eng)
        with open(runner._claim_path(self.SID)) as fh:
            return json.load(fh)

    def test_a_launch_written_during_it(self):
        self.assertIs(self.scan(True).get("launched"), True)

    def test_a_launch_written_before_it(self):                                  # control
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time() - 5,
                                       "sessionId": self.SID, "launched": True})
        self.assertIs(self.scan(False).get("seen"), True)


class TestOnlyAnErrorBeforeOsascriptRanGivesTheClaimsBack(unittest.TestCase):
    """subprocess.run raises in Popen's constructor before any process was
    made (its pipes, fork, an argument refused), or after exec reported the
    program could not run: nothing ran, and the claims go. Raised after a
    process was made and before exec's report, or while its output is read,
    its event may already be in iTerm2 (a killed osascript does not cancel
    it): the claims hold. Told by where it was raised, never by its errno:
    fork's EAGAIN is poll(2)'s too."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp, held_while_iterm2_lives = _F.SIDS, _F.setUp, _F.held_while_iterm2_lives

    def sent(self, sid, run):
        runner.subprocess.run = run
        return runner.launch_in_iterm("script", 1.0, [sid])[1]

    def test_before_it_ran(self):
        cells = [spawn_failing(ex) for ex in (
            FileNotFoundError(errno.ENOENT, "No such file or directory"),
            PermissionError(errno.EACCES, "Permission denied"),
            BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable"),    # no process left
            OSError(errno.ENOMEM, "Cannot allocate memory"),
            OSError(errno.EIO, "Input/output error"),
            ValueError("embedded null byte"), TypeError("bad argument"))] + [exec_failing()]
        for i, run in enumerate(cells):
            with self.subTest(i=i):
                why = self.sent(self.SIDS[i], run)
                self.assertRegex(why, r"^could not drive iTerm2 \(\w+\)$")
                self.assertFalse(self.held_while_iterm2_lives(self.SIDS[i]))

    def test_after_it_started(self):
        cells = [failing_after_start(self, ex) for ex in (
            OSError(errno.EIO, "Input/output error"),
            BlockingIOError(errno.EAGAIN, "Resource temporarily unavailable"),    # poll(2)
            FileNotFoundError(errno.ENOENT, "No such file or directory"),
            PermissionError(errno.EPERM, "Operation not permitted"),
            OSError("no errno"), ValueError("I/O operation on closed file"))]
        # made, and exec not yet heard from: it may be running
        cells.append(failing_after_start(self, OSError(errno.EBADF, "Bad file descriptor"),
                                         "_close_pipe_fds"))
        for i, run in enumerate(cells):
            with self.subTest(i=i):
                sid = self.SIDS[20 + i]
                why = self.sent(sid, run)
                self.assertTrue(why.startswith("could not drive iTerm2 ("), why)
                self.assertIn(runner.MAY_STILL_RUN, why)
                # bound to the iTerm2 a table after it shows (_sent)
                self.assertEqual(runner._read_claim(sid)["iterm_pid"], [4242])
                self.assertTrue(self.held_while_iterm2_lives(sid))

    def test_ctrl_c_as_its_process_is_made(self):
        # Ctrl-C lands in Popen's constructor after the process was made: it
        # is ended as on any other error, before the send is recorded as over
        sid = self.SIDS[31]
        run = failing_after_start(self, KeyboardInterrupt(), "_close_pipe_fds", ("/bin/sleep", "30"))
        # _sent's table is taken only once it has ended
        at_the_table = []
        runner.engine.terms.app_snapshot = lambda: at_the_table.append(self.children[-1].returncode) or ITERM_TABLE
        with self.assertRaises(KeyboardInterrupt):
            self.sent(sid, run)
        self.assertEqual(self.children[-1].returncode, -signal.SIGKILL, "left running, or waited for")
        self.assertEqual(at_the_table, [-signal.SIGKILL])
        self.assertTrue(self.held_while_iterm2_lives(sid))

    def test_one_that_cannot_be_ended_keeps_its_send_lock(self):
        # it may still send: the claim stays on its send lock, which it holds
        # - never bound to an iTerm2 that may quit while it still runs
        sid = self.SIDS[32]
        run = failing_after_start(self, OSError(errno.EBADF, "Bad file descriptor"), "_close_pipe_fds",
                                  ("/bin/sleep", "30"))
        with mock.patch.object(subprocess.Popen, "kill", side_effect=OSError(errno.EPERM, "no")):
            why = self.sent(sid, run)
        self.assertIn(runner.MAY_STILL_RUN, why)
        self.assertIsNone(self.children[-1].returncode)
        rec = runner._read_claim(sid)
        self.assertIsNotNone(rec.get("send"))
        self.assertTrue(runner._sending(rec))
        self.assertFalse(runner.claim_launch(sid, alive=lambda p: False))
        self.assertEqual(runner.why_held(sid), ("starting", 0))

    def test_a_process_made_and_not_heard_from_is_ended(self):
        # as subprocess.run ends one it gives up on: no osascript is left
        # running on its own, holding the send lock after the send is recorded
        sid = self.SIDS[30]
        why = self.sent(sid, failing_after_start(self, OSError(errno.EBADF, "Bad file descriptor"),
                                                 "_close_pipe_fds", ("/bin/sleep", "30")))
        self.assertIn(runner.MAY_STILL_RUN, why)
        self.assertEqual(self.children[-1].returncode, -signal.SIGKILL, "left running, or waited for")
        self.assertTrue(self.held_while_iterm2_lives(sid))


class TestOnlyNoSuchProcessIsAProcessGone(unittest.TestCase):
    """kill(pid, 0) that fails for another reason than "no such process" -
    a process of another user (EPERM), a sandbox's refusal - does not say
    the process is gone: the claim holds, and the table decides."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def probe_says(self, ex):
        real = os.kill
        self.addCleanup(setattr, os, "kill", real)

        def kill(pid, sig):
            if pid == 4242:
                raise ex
            return real(pid, sig)
        os.kill = kill

    def test_what_it_answers(self):
        for ex, alive in ((ProcessLookupError(errno.ESRCH, "No such process"), False),
                          (PermissionError(errno.EPERM, "Operation not permitted"), True),
                          (OSError(errno.EINVAL, "Invalid argument"), True)):
            with self.subTest(ex=ex):
                self.probe_says(ex)
                self.assertIs(runner._pid_alive(4242), alive)
        for pid in (None, "x", "4242", 4242.0, True, 0, -1, 2 ** 70):
            with self.subTest(pid=pid):
                self.assertIs(runner._pid_alive(pid), False)

    def unresolved_on_4242(self):
        runner.claim_launch(self.SID, now=self.T)
        runner.claim_unresolved(self.SID, [4242], now=self.T)
        runner.engine.terms.app_snapshot = lambda: ITERM_TABLE
        return runner.claim_launch(self.SID, now=self.T + 30)

    def test_a_denied_probe_holds_the_claim(self):
        self.probe_says(PermissionError(errno.EPERM, "Operation not permitted"))
        self.assertFalse(self.unresolved_on_4242())
        self.assertEqual(runner.why_held(self.SID), ("unresolved", 0))

    def test_a_process_gone(self):                                              # control
        self.probe_says(ProcessLookupError(errno.ESRCH, "No such process"))
        self.assertTrue(self.unresolved_on_4242())


class TestOneReadingOfTheClocksInsideALockedSection(TheClocks, unittest.TestCase):
    """Inside a claims-lock section every time is on the one reading of the
    clocks taken when the lock was had (_skew): a wall clock step while the
    section runs moves nothing it writes, reads or compares - nor what it
    hands to the next section (_tables)."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def inside(self, name, step, when=lambda *a: True):
        """The wall clock steps by `step`, and stays so, as ccwho.<name> is
        entered - inside the section that calls it."""
        real = getattr(runner, name)

        def stepped(*a, **kw):
            if when(*a):
                self.move(wall=step)
            return real(*a, **kw)
        setattr(runner, name, stepped)
        self.addCleanup(setattr, runner, name, real)

    def launched(self, ago=5.0):
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time() - ago,
                                       "sessionId": self.SID, "launched": True})

    # a sighting written in a scan's section (_see)

    def seen_across(self, step):
        self.launched()
        seen = (time.time(), runner._mono())
        self.inside("_see", step)
        runner.release_claims([self.SID], *seen)
        return seen

    def before(self, seen):
        return runner.claim_launch(self.SID, decided_at=seen[0] - 1, decided_mono=seen[1] - 1)

    def after(self):
        return runner.claim_launch(self.SID, decided_at=time.time(), decided_mono=runner._mono())

    def test_set_back_an_hour_a_scan_after_it(self):
        self.seen_across(-3600)
        self.assertTrue(self.after())

    def test_set_back_an_hour_its_stamp(self):
        self.seen_across(-3600)
        with open(runner._claim_path(self.SID)) as fh:
            rec = json.load(fh)
        self.assertIs(rec.get("seen"), True)
        self.assertLessEqual(rec["clock"][1], runner._mono())

    def test_set_back_an_hour_a_scan_before_it(self):                           # control
        seen = self.seen_across(-3600)
        self.assertFalse(self.before(seen))

    def test_set_back_two_days_a_scan_before_it(self):
        seen = self.seen_across(-2 * 86400)
        self.assertFalse(self.before(seen))
        self.assertEqual(runner.why_held(self.SID)[0], "newer")

    def test_set_back_two_days_a_scan_after_it(self):                           # control
        self.seen_across(-2 * 86400)
        self.assertTrue(self.after())

    def test_no_step_in_the_scan(self):                                         # control
        seen = self.seen_across(0)
        self.assertFalse(self.before(seen))
        self.assertTrue(self.after())

    def test_a_launch_made_during_the_scan_stays(self):
        seen = (time.time() - 5, runner._mono() - 5)            # it started 5 s ago
        runner.claim_launch(self.SID)
        runner.claim_unresolved(self.SID, None)                 # made since: may be a new launch
        self.inside("_see", -7200)
        runner.release_claims([self.SID], *seen)
        self.assertIs(runner._read_claim(self.SID).get("unresolved"), True)

    def test_a_launch_made_before_the_scan_is_seen(self):                       # control
        runner.claim_launch(self.SID)
        runner.claim_unresolved(self.SID, None)
        time.sleep(0.01)
        seen = (time.time(), runner._mono())
        self.inside("_see", -7200)
        runner.release_claims([self.SID], *seen)
        self.assertIs(runner._read_claim(self.SID).get("seen"), True)

    # a claim read in a launch's section (_take)

    def test_a_launch_read_across_a_step_back_of_two_days(self):
        decided = (time.time() - 3, runner._mono() - 3)
        self.launched(ago=0)
        self.inside("_take", -2 * 86400)
        self.assertFalse(runner.claim_launch(self.SID, decided_at=decided[0], decided_mono=decided[1]))

    def test_a_launch_whose_time_is_up_across_that_step(self):                 # control
        self.launched(ago=runner.LAUNCH_CLAIM_SECONDS + 30)
        self.inside("_take", -2 * 86400)
        self.assertTrue(runner.claim_launch(self.SID, decided_at=time.time(), decided_mono=runner._mono()))

    def test_a_decision_on_the_wall_clock_alone(self):
        decided_at = time.time() - 2
        self.inside("_take", -700)
        self.assertTrue(runner.claim_launch(self.SID, decided_at=decided_at))

    def test_one_on_the_wall_clock_alone_that_is_old(self):                    # control
        decided_at = time.time() - 601
        self.inside("_take", -700)
        self.assertFalse(runner.claim_launch(self.SID, decided_at=decided_at))
        self.assertEqual(runner.why_held(self.SID)[0], "old list")

    # a claim judged on a table (_iterm_holds)

    def judged_on(self, table, step):
        runner.claim_launch(self.SID)
        runner.claim_unresolved(self.SID, [4242], now=time.time() - 60)
        runner.engine.terms.app_snapshot = lambda: table
        self.inside("_iterm_holds", step, when=lambda rec, alive, tbl, taken: tbl is not None)
        return runner.claim_launch(self.SID, alive=lambda p: p == 4242)

    def test_set_back_while_it_is_judged_on_a_table(self):
        self.assertTrue(self.judged_on(NO_ITERM, -3600))

    def test_that_table_with_no_step(self):                                     # control
        self.assertTrue(self.judged_on(NO_ITERM, 0))

    def test_its_iterm2_still_runs(self):                                      # control
        self.assertFalse(self.judged_on(ITERM_TABLE, -3600))
        self.assertEqual(runner.why_held(self.SID), ("unresolved", 0))

    # a claim's time handed from one section to the batch's table (_tables)

    def redated_in_a_batch(self, table, step):
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time() - 60,
                                       "sessionId": self.SID, "unresolved": True, "iterm_pid": None,
                                       "boot": "THIS-BOOT", "send": "0" * 32})     # its send is over
        taken = []
        runner.engine.terms.app_snapshot = lambda: taken.append(1) or (NO_ITERM if len(taken) == 1 else table)
        batch = runner._tables()
        batch()                                     # the batch's table: from before the re-date

        def refresh(after=None):
            self.move(wall=step)                    # between the judgement and its table
            return batch(after)
        return runner.claim_launch(self.SID, alive=lambda p: False, refresh=refresh), len(taken)

    def test_set_forward_before_its_table(self):
        self.assertEqual(self.redated_in_a_batch(NO_ITERM, 7200), (True, 2))

    def test_set_back_before_its_table(self):
        self.assertEqual(self.redated_in_a_batch(NO_ITERM, -7200), (True, 2))

    def test_no_step_before_its_table(self):                                    # control
        self.assertEqual(self.redated_in_a_batch(NO_ITERM, 0), (True, 2))

    def test_its_iterm2_runs_on_that_table(self):                              # control
        self.assertEqual(self.redated_in_a_batch(ITERM_TABLE, 7200), (False, 2))


class TestTheSweepIsNotStoppedByAClockThatRanAhead(unittest.TestCase):
    """A sweep run while the wall clock ran ahead leaves its marker - and the
    files written then - dated in the future. After the clock is set right,
    neither may stop the sweep for as long as it ran ahead: a file is judged
    by its own times."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def sighting(self, age, mtime):
        t = time.time()
        runner._write_claim(self.SID, {"sessionId": self.SID, "seen": True, "since": t - age - 1,
                                       "until": t - age})
        os.utime(runner._claim_path(self.SID), (mtime, mtime))

    def marker(self, mtime):
        mark = os.path.join(self.tmp, "launching", ".swept")
        open(mark, "w").close()
        os.utime(mark, (mtime, mtime))

    def swept(self):
        runner.release_claims([], time.time(), runner._mono())
        return not os.path.exists(runner._claim_path(self.SID))

    def test_a_marker_left_in_the_future(self):
        self.sighting(7200, time.time() - 7200)
        self.marker(time.time() + 2 * 86400)
        self.assertTrue(self.swept())

    def test_an_old_sighting_written_in_the_future(self):
        self.sighting(7200, time.time() + 2 * 86400)
        self.assertTrue(self.swept())

    def test_a_marker_of_a_minute_ago(self):                                   # control
        self.sighting(7200, time.time() - 7200)
        self.marker(time.time() - 60)
        self.assertFalse(self.swept())

    def test_a_new_sighting_written_in_the_future(self):                       # control
        self.sighting(60, time.time() + 2 * 86400)
        self.assertFalse(self.swept())


class TestATryOnceThatFailsKeepsItsWindow(unittest.TestCase):
    """For GAVE_UP_SECONDS after a launch's full wait for the claims lock gave
    up, its thread tries once - and a try that fails does not start the
    window again: a click a minute on waits in full."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def hold(self):
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d, exist_ok=True)
        fd = os.open(os.path.join(d, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(runner._LOCAL.__dict__.pop, "lock_gave_up", None)

    def test_a_failed_single_try(self):
        self.hold()
        mark = time.monotonic() - 50                    # gave up 50 s ago: in the window
        runner._LOCAL.lock_gave_up = mark
        t = time.monotonic()
        self.assertTrue(runner.claim_launch(self.SID))  # tries once: busy
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual(runner._LOCAL.lock_gave_up, mark)

    def test_a_full_wait_that_gives_up(self):                                   # control
        self.addCleanup(setattr, runner, "CLAIMS_LOCK_SECONDS", runner.CLAIMS_LOCK_SECONDS)
        runner.CLAIMS_LOCK_SECONDS = 0.2
        self.hold()
        runner._LOCAL.__dict__.pop("lock_gave_up", None)
        t = time.monotonic()
        self.assertTrue(runner.claim_launch(self.SID))
        self.assertGreater(runner._LOCAL.lock_gave_up, t)


class TestTheSweepNeverTakesAFileMadeAfterItsScan(unittest.TestCase):
    """A file is recent from SIGHTING_SECONDS before its scan's start to
    SIGHTING_SECONDS past the clock now: a scan that took longer than that
    (the Mac slept while `claude agents` ran) must not take a send lock made
    since, before its launcher locks it. A start on the monotonic clock after
    now is none a scan gives: no sweep runs on it (_sweep returns before its
    lock)."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def send_file(self, mtime):
        d = os.path.join(self.tmp, "launching")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "ab" * 16 + ".send")
        open(path, "w").close()                         # made, not yet locked
        os.utime(path, (mtime, mtime))
        return d, path

    def test_one_made_after_a_long_scan_started(self):
        for start in (-700, -3600, 3600):
            with self.subTest(start=start):
                d, path = self.send_file(time.time())
                runner._sweep(d, time.time() + start, runner._mono() + start)
                self.assertTrue(os.path.exists(path))
                with contextlib.suppress(FileNotFoundError):
                    os.remove(os.path.join(d, ".swept"))

    def test_a_marker_made_after_it_started(self):
        d, path = self.send_file(time.time() - 7200)    # one the sweep would take
        mark = os.path.join(d, ".swept")
        open(mark, "w").close()                         # a sweep 1 s ago, by a later scan
        os.utime(mark, (time.time() - 1, time.time() - 1))
        runner._sweep(d, time.time() - 700, runner._mono() - 700)
        self.assertTrue(os.path.exists(path))

    def test_an_old_one_no_one_holds(self):                                     # control
        d, path = self.send_file(time.time() - 3600)
        runner._sweep(d, time.time() - 700, runner._mono() - 700)
        self.assertFalse(os.path.exists(path))


class TestASendLockRemovedAsItIsMadeIsNone(unittest.TestCase):
    """_hold_send makes its file, then locks it. A file removed in between
    would leave a lock on no name: every claim naming it would read "the
    send is over". It is refused - never a lock on a file that is gone."""

    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def locked_after(self, between):
        real = runner.fcntl

        class Fcntl:
            LOCK_EX, LOCK_NB, LOCK_SH = fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_SH

            def flock(self, fd, op):
                between()
                return real.flock(fd, op)
        self.addCleanup(setattr, runner, "fcntl", real)
        runner.fcntl = Fcntl()

    def sends(self):
        d = os.path.join(self.tmp, "launching")
        return [os.path.join(d, n) for n in os.listdir(d) if n.endswith(".send")]

    def test_removed_before_its_lock(self):
        self.locked_after(lambda: [os.unlink(p) for p in self.sends()])
        with self.assertRaises(OSError):
            runner._hold_send()

    def test_replaced_before_its_lock(self):
        def replaced():
            for p in self.sends():
                os.unlink(p)
                open(p, "w").close()
        self.locked_after(replaced)
        with self.assertRaises(OSError):
            runner._hold_send()

    def test_left_as_it_was_made(self):                                         # control
        self.locked_after(lambda: None)
        held = runner._hold_send()
        self.addCleanup(runner._let_go_send, held)
        self.assertTrue(runner._sending({"send": held[0]}))


class TestNoClaimStateCrossesTests(unittest.TestCase):
    """A claim_launch that failed, or a send lock launch_in_iterm could not
    have, leaves why in ccwho's thread-local store (_LOCAL), and the next
    launch_in_iterm on the thread reads it: every test here starts with none
    (setUpModule)."""

    def test_a_refused_dir_then_a_busy_lock(self):
        for first in (TestARefusalSaysItsReason("test_a_ccwho_dir_not_ours"),
                      TestTheClaimDirIsPrivate("test_one_of_another_user_is_refused")):
            r = unittest.TestResult()
            first.run(r)
            TestAClaimsLockHeldForGoodStopsNoScan("test_a_launch_waits_a_while_then_refuses").run(r)
            self.assertEqual((r.failures, r.errors), ([], []), first)


class TestASendLockStaysNamedWhileAnyoneHoldsIt(unittest.TestCase):
    """_let_go_send removes the send lock's file only when no one holds the
    lock then - as the sweep does: a process that inherited it and still runs
    keeps it named, and every claim naming it held, until it ends."""

    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_a_process_that_still_holds_it(self):
        held = runner._hold_send()
        child = subprocess.Popen(["/bin/sleep", "30"], pass_fds=(held[1],))
        self.addCleanup(lambda: (child.kill(), child.wait()))
        runner._let_go_send(held)
        self.assertTrue(os.path.exists(runner._send_path(held[0])))
        self.assertTrue(runner._sending({"send": held[0]}))
        child.kill()
        child.wait()
        self.assertFalse(runner._sending({"send": held[0]}))       # free: the sweep's to remove

    def test_no_one_else(self):                                                 # control
        held = runner._hold_send()
        runner._let_go_send(held)
        self.assertFalse(os.path.exists(runner._send_path(held[0])))


class TestALaunchWhoseSendLockWasRemovedAsItWasMadeSendsNothing(unittest.TestCase):
    """_hold_send refuses a lock whose file was removed or replaced before its
    flock: the launch is not sent, its claims go, and it says why - a race to
    try again, not a dir that cannot be written."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS[:1], _F.setUp

    def launch(self, between):
        real = runner.fcntl

        class Fcntl:
            LOCK_EX, LOCK_NB, LOCK_SH = fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_SH

            def flock(self, fd, op):
                between()
                return real.flock(fd, op)
        sent = []

        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda cmd, **kw: sent.append(cmd) or Done()
        runner.fcntl = Fcntl()
        try:
            res, why = runner.launch_in_iterm("script", 1.0, self.SIDS)
        finally:
            runner.fcntl = real
        return sent, res, why

    def sends(self):
        d = os.path.join(self.tmp, "launching")
        return [os.path.join(d, n) for n in os.listdir(d) if n.endswith(".send")]

    def replaced(self):
        for p in self.sends():
            os.unlink(p)
            open(p, "w").close()

    def test_removed_or_replaced_before_its_lock(self):
        for between in (lambda: [os.unlink(p) for p in self.sends()], self.replaced):
            with self.subTest(between=between):
                if not os.path.exists(runner._claim_path(self.SIDS[0])):   # dropped by the last
                    self.assertTrue(runner.claim_launch(self.SIDS[0]))
                sent, res, why = self.launch(between)
                self.assertEqual(sent, [])
                self.assertIsNone(res)
                self.assertIn("not sending it", why)
                self.assertIn("try again", why)
                self.assertNotIn("can be written", why)
                self.assertFalse(os.path.exists(runner._claim_path(self.SIDS[0])))

    def test_left_as_it_was_made(self):                                         # control
        sent, res, why = self.launch(lambda: None)
        self.assertEqual(len(sent), 1)
        self.assertIsNotNone(res, why)


class TestAScanStartAfterNowReleasesNothing(unittest.TestCase):
    """A scan's start on the monotonic clock is never after now (scan() reads
    it first): one that is was given by no scan - no claim becomes a sighting
    on it, and no sweep runs on it."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def launched(self):
        runner._write_claim(self.SID, {"pid": DEAD, "owner": "x", "since": time.time() - 5,
                                       "sessionId": self.SID, "launched": True})

    def test_a_start_ahead(self):
        self.launched()
        runner.release_claims([self.SID], time.time() + 3600, runner._mono() + 3600)
        self.assertIs(runner._read_claim(self.SID).get("launched"), True)

    def test_a_start_of_now(self):                                              # control
        self.launched()
        runner.release_claims([self.SID], time.time(), runner._mono())
        self.assertIs(runner._read_claim(self.SID).get("seen"), True)


class TestTheSpawnRuleOnEachPythonThatRunsCcwho(unittest.TestCase):
    """_send sorts a spawn error by CPython's own frames and fields
    (_being_made, _never_ran, _ended, _popen_in, _interrupted): checked on
    this Python and on the system's
    (/usr/bin/python3 - `ccwho` runs on whichever python3 is first on PATH).
    The list's (uv's) is checked by test_ui."""

    def sorts(self, python):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        r = REAL_RUN([python, "-c", testkit.SPAWN_RULE_CHECK, os.path.dirname(os.path.abspath(runner.__file__))],
                     capture_output=True, text=True, timeout=120,
                     env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CCWHO_DIR=tmp))
        return r.stdout.split(), r.stderr[-800:]

    def test_this_python(self):
        got, err = self.sorts(sys.executable)
        self.assertEqual(got, testkit.SPAWN_RULE_SORTS, err)

    @unittest.skipUnless(os.path.exists("/usr/bin/python3"), "no system Python")
    def test_the_systems(self):
        got, err = self.sorts("/usr/bin/python3")
        self.assertEqual(got, testkit.SPAWN_RULE_SORTS, err)


class TestAPopenWithoutTheFieldsTheSpawnRuleReads(unittest.TestCase):
    """_never_ran and _ended read CPython's own Popen fields (_child_created,
    returncode). On a Python without them the rule fails closed: a child may
    exist - the claims are neither dropped nor bound while it may run."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS[:1], _F.setUp

    class Bare:
        """A Popen with none of the fields: kill and wait do nothing."""
        def kill(self):
            pass

        def wait(self):
            pass

    def test_the_rule(self):
        self.assertFalse(runner._never_ran(self.Bare()))
        self.assertFalse(runner._ended(self.Bare()))

    def test_a_send_that_raised_in_one(self):
        bound = []
        self.addCleanup(setattr, runner, "_being_made", runner._being_made)
        self.addCleanup(setattr, runner, "_sent", runner._sent)
        runner._being_made = lambda ex: self.Bare()
        runner._sent = lambda sids: bound.append(sids)

        def run(cmd, **kw):
            raise OSError(errno.EBADF, "Bad file descriptor")
        runner.subprocess.run = run
        why = runner.launch_in_iterm("script", 1.0, self.SIDS)[1]
        self.assertIn(runner.MAY_STILL_RUN, why)
        self.assertEqual(bound, [])                             # not bound while it may run
        rec = runner._read_claim(self.SIDS[0])                  # nor dropped
        self.assertIs(rec.get("unresolved"), True)
        self.assertIsNone(rec.get("iterm_pid"))

    def test_one_that_can_be_ended(self):                                       # control
        class Ends(self.Bare):
            _child_created, returncode = True, None

            def wait(self):
                self.returncode = -9
        bound = []
        self.addCleanup(setattr, runner, "_being_made", runner._being_made)
        self.addCleanup(setattr, runner, "_sent", runner._sent)
        runner._being_made = lambda ex: Ends()
        runner._sent = lambda sids: bound.append(list(sids))

        def run(cmd, **kw):
            raise OSError(errno.EBADF, "Bad file descriptor")
        runner.subprocess.run = run
        runner.launch_in_iterm("script", 1.0, self.SIDS)
        self.assertEqual(bound, [list(self.SIDS)])              # ended: bound, as the send is over


class TestACtrlCJustAfterTheFork(unittest.TestCase):
    """Ctrl-C can land in Popen's constructor after its fork and before it
    notes the child (pid, _child_created): a process of ours may run
    osascript with the send lock that nothing names. The claim stays on that
    lock - never bound while it may run."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS[:1], _F.setUp

    def launched(self, fork):
        real, forked = getattr(*testkit.FORK_POINT), []

        def fork_then_ctrl_c(*a, **k):
            if fork:
                forked.append(real(*a, **k))
            raise KeyboardInterrupt
        self.addCleanup(lambda: [(os.kill(pid, 9), os.waitpid(pid, 0)) for pid in forked])

        def run(cmd, **kw):
            with mock.patch.object(*testkit.FORK_POINT, side_effect=fork_then_ctrl_c):
                return REAL_RUN(["/bin/sleep", "30"], **kw)
        runner.subprocess.run = run
        with self.assertRaises(KeyboardInterrupt):
            runner.launch_in_iterm("script", 1.0, self.SIDS)
        return forked

    def test_a_child_nothing_names(self):
        forked = self.launched(fork=True)
        self.assertEqual(len(forked), 1)
        rec = runner._read_claim(self.SIDS[0])
        self.assertIsNotNone(rec.get("send"))
        self.assertIsNone(rec.get("iterm_pid"))
        # held while it runs - whatever the table shows
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        self.assertFalse(runner.claim_launch(self.SIDS[0], alive=lambda p: False))
        self.assertEqual(runner.why_held(self.SIDS[0]), ("starting", 0))
        # and judged on a table once it has ended: no iTerm2 - it goes
        os.kill(forked[0], 9)
        os.waitpid(forked[0], 0)
        forked.clear()
        self.assertTrue(runner.claim_launch(self.SIDS[0], alive=lambda p: False))

    def test_one_made_as_run_enters_it(self):
        # Ctrl-C after Popen returned, before subprocess.run's `with` - its
        # clean-up never runs, and no constructor frame names the process
        made = []

        def enter(p):
            made.append(p)
            raise KeyboardInterrupt
        self.addCleanup(lambda: [(p.kill(), p.wait()) for p in made])

        def run(cmd, **kw):
            with mock.patch.object(subprocess.Popen, "__enter__", enter):
                return REAL_RUN(["/bin/sleep", "30"], **kw)
        runner.subprocess.run = run
        with self.assertRaises(KeyboardInterrupt):
            runner.launch_in_iterm("script", 1.0, self.SIDS)
        self.assertIsNone(made[0].poll(), "the test needs it running")
        rec = runner._read_claim(self.SIDS[0])
        self.assertIsNotNone(rec.get("send"))
        self.assertIsNone(rec.get("iterm_pid"))
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        self.assertFalse(runner.claim_launch(self.SIDS[0], alive=lambda p: False))
        self.assertEqual(runner.why_held(self.SIDS[0]), ("starting", 0))
        made[0].kill()
        made[0].wait()
        self.assertTrue(runner.claim_launch(self.SIDS[0], alive=lambda p: False))

    def test_none_made(self):                                                   # control
        self.assertEqual(self.launched(fork=False), [])
        # no process holds the lock: its file goes, the send is over - judged
        # on a table after it, never held for good
        self.assertEqual([n for n in os.listdir(os.path.join(self.tmp, "launching"))
                          if n.endswith(".send")], [])
        self.assertFalse(runner._sending(runner._read_claim(self.SIDS[0])))
        self.assertFalse(runner.claim_launch(self.SIDS[0], alive=lambda p: False))
        self.assertEqual(runner.why_held(self.SIDS[0]), ("unresolved", 0))


class TestACtrlCInTheCleanUpOfTheSend(unittest.TestCase):
    """A second Ctrl-C in subprocess.run's clean-up (its kill): _popen_in finds
    run's process, and _ended kills it - bound once it is shown ended. When
    every kill is interrupted - _ended's too - it may run on: the claim stays
    on its send lock."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS[:1], _F.setUp

    def test_runs_kill_interrupted_once(self):
        run = failing_after_start(self, KeyboardInterrupt(), "communicate", ("/bin/sleep", "30"))
        real, calls = subprocess.Popen.kill, []

        def kill(p):
            calls.append(p)
            if len(calls) == 1:
                raise KeyboardInterrupt             # run's
            return real(p)
        with mock.patch.object(subprocess.Popen, "kill", kill):
            with self.assertRaises(KeyboardInterrupt):
                runner.subprocess.run = run
                runner.launch_in_iterm("script", 1.0, self.SIDS)
        self.assertEqual(self.children[-1].returncode, -signal.SIGKILL)   # _ended's
        rec = runner._read_claim(self.SIDS[0])
        self.assertNotIn("send", rec)
        self.assertEqual(rec["iterm_pid"], [4242])

    def test_every_kill_interrupted(self):
        run = failing_after_start(self, KeyboardInterrupt(), "communicate", ("/bin/sleep", "30"))
        with mock.patch.object(subprocess.Popen, "kill", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                runner.subprocess.run = run
                runner.launch_in_iterm("script", 1.0, self.SIDS)
        self.assertIsNone(self.children[-1].poll(), "the test needs it running")
        rec = runner._read_claim(self.SIDS[0])
        self.assertIsNotNone(rec.get("send"))
        self.assertTrue(runner._sending(rec))
        self.assertFalse(runner.claim_launch(self.SIDS[0], alive=lambda p: False))
        self.assertEqual(runner.why_held(self.SIDS[0]), ("starting", 0))

    def test_one_that_ended(self):                                              # control
        run = failing_after_start(self, KeyboardInterrupt(), "communicate", ("/bin/sleep", "30"))
        with self.assertRaises(KeyboardInterrupt):
            runner.subprocess.run = run
            runner.launch_in_iterm("script", 1.0, self.SIDS)
        # subprocess.run killed it, and it is found and shown ended: the send
        # is over - bound to the iTerm2 a table after it shows
        self.assertEqual(self.children[-1].returncode, -signal.SIGKILL)
        rec = runner._read_claim(self.SIDS[0])
        self.assertNotIn("send", rec)
        self.assertEqual(rec["iterm_pid"], [4242])


class TestACtrlCWithAProcessThatCannotBeEnded(unittest.TestCase):
    """Ctrl-C while Popen waits for exec's report, and the process it made
    cannot be ended: the claim stays on its send lock, never bound while that
    process may run."""

    _A = TestOnlyAnErrorBeforeOsascriptRanGivesTheClaimsBack
    SIDS, setUp, sent = _A.SIDS, _A.setUp, _A.sent

    def test_it_keeps_its_send_lock(self):
        sid = self.SIDS[33]
        run = failing_after_start(self, KeyboardInterrupt(), "_close_pipe_fds", ("/bin/sleep", "30"))
        with mock.patch.object(subprocess.Popen, "kill", side_effect=OSError(errno.EPERM, "no")):
            with self.assertRaises(KeyboardInterrupt):
                self.sent(sid, run)
        self.assertIsNone(self.children[-1].returncode)
        rec = runner._read_claim(sid)
        self.assertIsNotNone(rec.get("send"))
        self.assertTrue(runner._sending(rec))
        self.assertFalse(runner.claim_launch(sid, alive=lambda p: False))
        self.assertEqual(runner.why_held(sid), ("starting", 0))


class TestAnErrorThatHidesACtrlCJustAfterTheFork(unittest.TestCase):
    """A Ctrl-C just after Popen's fork, and an error raised as it unwinds (a
    pipe closed) that takes its place: "no child noted" is then no proof that
    none was made - the claims are not dropped, and stay on the send lock."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS[:1], _F.setUp

    def launched(self, interrupted):
        real, forked = getattr(*testkit.FORK_POINT), []

        def fork(*a, **k):
            if not interrupted:
                raise OSError(errno.EBADF, "Bad file descriptor")       # a fork that failed
            forked.append(real(*a, **k))
            try:
                raise KeyboardInterrupt
            except KeyboardInterrupt:
                raise OSError(errno.EBADF, "Bad file descriptor")
        self.addCleanup(lambda: [(os.kill(pid, 9), os.waitpid(pid, 0)) for pid in forked])
        self.forked = forked

        def run(cmd, **kw):
            with mock.patch.object(*testkit.FORK_POINT, side_effect=fork):
                return REAL_RUN(["/bin/sleep", "30"], **kw)
        runner.subprocess.run = run
        return runner.launch_in_iterm("script", 1.0, self.SIDS)[1], forked

    def test_it_is_no_proof(self):
        with self.assertRaises(KeyboardInterrupt):      # the Ctrl-C it hid: the command stops
            self.launched(interrupted=True)
        forked = self.forked
        rec = runner._read_claim(self.SIDS[0])
        self.assertIsNotNone(rec.get("send"))
        runner.engine.terms.app_snapshot = lambda: NO_ITERM
        self.assertFalse(runner.claim_launch(self.SIDS[0], alive=lambda p: False))
        self.assertEqual(runner.why_held(self.SIDS[0]), ("starting", 0))
        os.kill(forked[0], 9)
        os.waitpid(forked[0], 0)
        forked.clear()
        self.assertTrue(runner.claim_launch(self.SIDS[0], alive=lambda p: False))

    def test_the_same_error_alone(self):                                       # control
        why, forked = self.launched(interrupted=False)
        self.assertEqual(forked, [])
        self.assertNotIn(runner.MAY_STILL_RUN, why)
        self.assertFalse(os.path.exists(runner._claim_path(self.SIDS[0])))    # dropped: nothing ran


class TestACtrlCSaysWhatItStopped(unittest.TestCase):
    """open and restore end on Ctrl-C with exit 130 and a line, no traceback:
    one that came during a send says the launch may still run - a user who
    read only a traceback would take it as cancelled and start the session
    by hand."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS[:1], _F.setUp

    def stopped(self, command):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = runner._interruptible(command, [])
        return rc, err.getvalue()

    def test_during_a_send(self):
        def interrupted(cmd, **kw):
            raise KeyboardInterrupt
        runner.subprocess.run = interrupted
        rc, said = self.stopped(lambda args: runner.launch_in_iterm("script", 1.0, self.SIDS))
        self.assertEqual(rc, 130)
        self.assertIn(runner.MAY_STILL_RUN, said)
        self.assertNotIn("Traceback", said)

    def test_in_the_ps_after_a_timeout(self):
        def stuck(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1.0)
        runner.subprocess.run = stuck

        def ps():
            raise KeyboardInterrupt
        runner.engine.terms.app_snapshot = ps
        rc, said = self.stopped(lambda args: runner.launch_in_iterm("script", 1.0, self.SIDS))
        self.assertEqual(rc, 130)
        self.assertIn(runner.MAY_STILL_RUN, said)
        self.assertNotIn("nothing was sent", said)

    def test_in_its_record_after_it_ran(self):
        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda cmd, **kw: Done()
        with mock.patch.object(runner, "claim_launched", side_effect=KeyboardInterrupt):
            rc, said = self.stopped(lambda args: runner.launch_in_iterm("script", 1.0, self.SIDS))
        self.assertIn(runner.MAY_STILL_RUN, said)

    def test_after_a_send_that_finished(self):
        # restore: its send done, a Ctrl-C before the next
        class Done:
            returncode, stdout, stderr = 0, "", ""
        runner.subprocess.run = lambda cmd, **kw: Done()

        def then_stopped(args):
            runner.launch_in_iterm("script", 1.0, self.SIDS)
            raise KeyboardInterrupt
        rc, said = self.stopped(then_stopped)
        self.assertIn(runner.MAY_STILL_RUN, said)

    def test_while_marking(self):                                               # control
        with mock.patch.object(runner, "claim_unresolved", side_effect=KeyboardInterrupt):
            rc, said = self.stopped(lambda args: runner.launch_in_iterm("script", 1.0, self.SIDS))
        self.assertEqual(rc, 130)
        self.assertIn("nothing was sent", said)

    def test_before_any_send(self):                                             # control
        def before(args):
            raise KeyboardInterrupt
        rc, said = self.stopped(before)
        self.assertEqual(rc, 130)
        self.assertNotIn(runner.MAY_STILL_RUN, said)
        self.assertIn("nothing was sent", said)

    def test_open_and_restore_go_through_it(self):
        for name, attr in (("open", "open_session"), ("restore", "restore")):
            with self.subTest(name=name):
                with mock.patch.object(runner, attr, side_effect=KeyboardInterrupt):
                    with contextlib.redirect_stderr(io.StringIO()):
                        try:
                            rc = runner.main([name, "x"])
                        except KeyboardInterrupt:
                            self.fail(f"{name}: a Ctrl-C left main - a traceback")
                self.assertEqual(rc, 130)


class TestASendLockItsLauncherLeftSaysSo(unittest.TestCase):
    """A send lock held by an osascript whose ccwho is gone is not "another
    ccwho opening it": the reason names what holds it, and the way out. The
    hold is the same."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS[:1], _F.setUp

    def held_on_a_lock(self, launcher_alive):
        held = runner._hold_send()
        self.addCleanup(runner._let_go_send, held)
        runner.claim_unresolved(self.SIDS[0], None, send=held[0])
        alive = (lambda p: True) if launcher_alive else (lambda p: False)
        with mock.patch.object(runner, "_pid_alive", alive):
            took = runner.claim_launch(self.SIDS[0], alive=lambda p: False)
            return took, runner.why_held(self.SIDS[0])

    def test_its_launcher_gone(self):
        took, why = self.held_on_a_lock(launcher_alive=False)
        self.assertFalse(took)
        self.assertEqual(why, ("sending", 0))
        text = runner._why_text("sending", self.SIDS[0], 0)
        self.assertIn("osascript", text)
        self.assertNotIn("another", text)

    def test_its_launcher_still_sending(self):                                  # control
        took, why = self.held_on_a_lock(launcher_alive=True)
        self.assertFalse(took)
        self.assertEqual(why, ("starting", 0))


# a ccwho started with stdin, stdout and stderr closed: its locks, and a child
# that must hold them. argv: repo, result file, mode - "open", "closed", or
# "closed-unguarded" (closed, and ccwho's and ccwho_terms' above_std made
# identity: the locks left low). Writes: high, sending, freed, gate_high,
# gate_freed. Only harmless programs run.
STD_CLOSED_PROBE = r"""
import os, subprocess, sys
repo, result, mode = sys.argv[1:4]
out = os.open(result, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
if mode.startswith("closed"):
    for fd in (0, 1, 2):
        os.close(fd)
sys.path.insert(0, repo)
import ccwho as r
import ccwho_engine as e
if mode == "closed-unguarded":          # the locks opened as they come
    r._above_std = e.terms.above_std = lambda fd: fd
import fcntl
held = r._hold_send()
# as _send runs osascript: its output piped, the lock passed
child = subprocess.Popen(["/bin/sleep", "30"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, pass_fds=(held[1],))
high = held[1] > 2
r._let_go_send(held)                    # the launcher lets go: only the child may hold it
sending = r._sending({"send": held[0]})
child.kill()
child.wait()
freed = not r._sending({"send": held[0]})       # and once it ends, no one
os.makedirs(e.terms.STATE_DIR, exist_ok=True)
gate = e.terms._gate_lock(e.terms.ITERM2)
gate_high = gate > 2
fcntl.flock(gate, fcntl.LOCK_EX)
os.close(gate)                          # let go: another can have it
again = os.open(e.terms._gate_path(e.terms.ITERM2, "lock"), os.O_RDWR)
try:
    fcntl.flock(again, fcntl.LOCK_EX | fcntl.LOCK_NB)
    gate_freed = True
except BlockingIOError:
    gate_freed = False
os.write(out, f"{high} {sending} {freed} {gate_high} {gate_freed}".encode())
"""


class TestALockPassedToAChildIsAboveStdio(unittest.TestCase):
    """A ccwho started with stdin, stdout or stderr closed gets that number
    for its next file: a lock with it, passed to a child (pass_fds), is
    replaced there by the child's pipes or DEVNULL - osascript would hold no
    send lock, the gate's wrapper no gate lock. Both locks are above them."""

    def probe(self, mode):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        result = os.path.join(tmp, "result")
        REAL_RUN([sys.executable, "-c", STD_CLOSED_PROBE, os.path.dirname(os.path.abspath(runner.__file__)),
                  result, mode], capture_output=True, timeout=120,
                 env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", HOME=tmp, CCWHO_DIR=os.path.join(tmp, "c")))
        with open(result) as fh:
            return fh.read().split()

    def test_started_with_them_closed(self):
        self.assertEqual(self.probe("closed"), ["True"] * 5)

    def test_started_with_them_open(self):
        self.assertEqual(self.probe("open"), ["True"] * 5)

    def test_closed_and_the_locks_left_low(self):                               # control
        # the probe sees the defect: a low lock, replaced in the child
        self.assertEqual(self.probe("closed-unguarded"), ["False", "False", "True", "False", "True"])


class TestARestoreHeldOnALauncherlessSendSaysSo(unittest.TestCase):
    """restore --open, a session held by a send lock whose ccwho is gone (its
    osascript runs on): it is still waited on, and the line says what holds
    it - not "another ccwho is opening it"."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def test_it_is_waited_on(self):
        self.write_manifest([{"sessionId": self.DEAD_SID, "cwd": self.cwd("b"), "project": "b"}])
        held = runner._hold_send()
        self.addCleanup(runner._let_go_send, held)
        runner._write_claim(self.DEAD_SID, {"pid": DEAD, "owner": "x", "since": time.time(),
                                            "sessionId": self.DEAD_SID, "unresolved": True,
                                            "iterm_pid": None, "send": held[0]})
        rc, out = self._restore_open()
        self.assertIn("still waiting on b - an earlier launch is still being sent", out)
        self.assertNotIn("another ccwho is opening it", out)
        self.assertEqual(self.runs, [])
        self.assertNotEqual(rc, 0)
        said = runner.reopen_saved(self.man)             # the list's `o`: not a failure
        self.assertFalse(said.startswith("could not reopen"), said)
        self.assertIn(runner.MAY_STILL_RUN, said)


class TestAHiddenCtrlCEndsTheCommand(unittest.TestCase):
    """The Ctrl-C an error hid still stops open or restore: exit 130 and the
    launch may still run - not a normal refusal that goes on."""

    _A = TestAnErrorThatHidesACtrlCJustAfterTheFork
    SIDS, setUp, launched = _A.SIDS, _A.setUp, _A.launched

    def test_it(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = runner._interruptible(lambda args: self.launched(interrupted=True), [])
        self.assertEqual(rc, 130)
        self.assertIn(runner.MAY_STILL_RUN, err.getvalue())


class TestAStoppedCommandDiesOfSigint(unittest.TestCase):
    """A command stopped by Ctrl-C ends the process by SIGINT, as an uncaught
    Ctrl-C does: bash ends a loop around `ccwho open` only when its child died
    of SIGINT - an exit 130 lets it launch the next."""

    def ended(self, flagged):
        """(its wait status, what it printed to stdout - a file: buffered)"""
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import ccwho; "
                "print('already open: a'); "
                + ("ccwho._LOCAL.interrupted = True; " if flagged else "")
                + "ccwho._exit(130)")
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "out"), "w") as out:
            rc = REAL_RUN([sys.executable, "-c", code, os.path.dirname(os.path.abspath(runner.__file__))],
                          stdout=out, stderr=subprocess.DEVNULL, timeout=60,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", HOME=tmp,
                                   CCWHO_DIR=tmp)).returncode
        with open(os.path.join(tmp, "out")) as fh:
            return rc, fh.read()

    def test_after_a_ctrl_c(self):
        # dies of SIGINT - and what it printed reaches its file first
        self.assertEqual(self.ended(True), (-signal.SIGINT, "already open: a\n"))

    def test_otherwise(self):                                                   # control
        self.assertEqual(self.ended(False), (130, "already open: a\n"))

    def test_interruptible_marks_it(self):
        def before(args):
            raise KeyboardInterrupt
        with contextlib.redirect_stderr(io.StringIO()):
            runner._interruptible(before, [])
        self.assertIs(runner._LOCAL.__dict__.get("interrupted"), True)


class TestACtrlCInOpensAttach(unittest.TestCase):
    """open's attach runs its own osascript: a Ctrl-C in it may still leave a
    window opening - not "nothing was sent"."""

    setUp = TestAClaimOutlivesALaunchThatMayStillHappen.setUp

    def test_it(self):
        self.addCleanup(setattr, runner.engine, "resolve_open", runner.engine.resolve_open)
        runner.engine.resolve_open = lambda *a, **k: ("attach", "4f2b91ac-1111-4222-8333-abcdefabcdef")

        class Nothing:
            returncode, stdout, stderr = 0, "", ""

        def interrupted(cmd, **kw):
            if cmd and cmd[0] == "osascript":           # the attach's own run: Ctrl-C there
                raise KeyboardInterrupt
            return Nothing()                            # the scan before it: nothing runs
        runner.subprocess.run = interrupted
        err = io.StringIO()
        with mock.patch.object(runner, "scan", return_value=([], 0)), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = runner._interruptible(runner.open_session, ["4f2b91ac-1111-4222-8333-abcdefabcdef"])
        self.assertEqual(rc, 130)
        self.assertIn(runner.MAY_STILL_RUN, err.getvalue())


class TestARestoreWithALauncherlessSendBesideAnOpenOne(unittest.TestCase):
    """A session held "sending" beside one that is open: still waited on -
    not "all of them are already running"."""

    _F = TestRestoreOpenSkipsWhatIsAlreadyRunning
    LIVE_SID, DEAD_SID = _F.LIVE_SID, _F.DEAD_SID
    setUp, tearDown, write_manifest, _restore_open = _F.setUp, _F.tearDown, _F.write_manifest, _F._restore_open

    def test_it(self):
        self.live = [{"sessionId": self.LIVE_SID, "tty": "ttys009", "pid": 7}]
        held = runner._hold_send()
        self.addCleanup(runner._let_go_send, held)
        runner._write_claim(self.DEAD_SID, {"pid": DEAD, "owner": "x", "since": time.time(),
                                            "sessionId": self.DEAD_SID, "unresolved": True,
                                            "iterm_pid": None, "send": held[0]})
        rc, out = self._restore_open()
        self.assertIn("still waiting on b", out)
        self.assertNotIn("already running or starting", out)
        self.assertEqual(rc, 1)


class TestAnErrorThatHidesACtrlCInRunsCleanUp(unittest.TestCase):
    """A Ctrl-C while osascript's output is read, a second in run's kill, and
    an error from its clean-up that takes its place: no constructor frame,
    so the error branch finds run's own process (_popen_in), ends it, and
    only then binds the claims - the Ctrl-C still stops the command. One that
    cannot be ended leaves the claim on its send lock."""

    _A = TestOnlyAnErrorBeforeOsascriptRanGivesTheClaimsBack
    SIDS, setUp, sent = _A.SIDS, _A.setUp, _A.sent

    def launched(self, kill_fails):
        sid = self.SIDS[35]
        run = failing_after_start(self, KeyboardInterrupt(), "communicate", ("/bin/sleep", "30"))
        real_kill, calls = subprocess.Popen.kill, []

        def kill(p):
            calls.append(p)
            if len(calls) == 1:
                raise KeyboardInterrupt                         # run's kill, interrupted
            if kill_fails:
                raise OSError(errno.EPERM, "Operation not permitted")
            return real_kill(p)

        def exit_fails(p, *exc):
            raise OSError(errno.EBADF, "Bad file descriptor")   # its pipes, closed as it unwound
        at_the_table = []
        runner.engine.terms.app_snapshot = lambda: at_the_table.append(self.children[-1].returncode) or ITERM_TABLE
        with mock.patch.object(subprocess.Popen, "kill", kill), \
                mock.patch.object(subprocess.Popen, "__exit__", exit_fails):
            with self.assertRaises(KeyboardInterrupt):
                self.sent(sid, run)
        return sid, at_the_table

    def test_it_is_ended_before_it_is_bound(self):
        sid, at_the_table = self.launched(kill_fails=False)
        self.assertEqual(self.children[-1].returncode, -signal.SIGKILL)
        self.assertEqual(at_the_table, [-signal.SIGKILL])       # its table taken once it ended
        rec = runner._read_claim(sid)
        self.assertNotIn("send", rec)
        self.assertEqual(rec["iterm_pid"], [4242])

    def test_one_that_cannot_be_ended_stays_on_its_lock(self):                 # control
        sid, _ = self.launched(kill_fails=True)
        self.assertIsNone(self.children[-1].returncode)
        rec = runner._read_claim(sid)
        self.assertIsNotNone(rec.get("send"))
        self.assertTrue(runner._sending(rec))


class TestAHiddenCtrlCWithNoProcessFound(unittest.TestCase):
    """An error that hides a Ctrl-C where no Popen can be found - not one being
    made, not run's own: no process can be shown ended, so the claims stay on
    the send lock, not bound - and the Ctrl-C stops the command."""

    _F = TestALaunchIsClaimedAsUnresolvedBeforeItIsSent
    SIDS, setUp = _F.SIDS[:1], _F.setUp

    def run_raising(self, hidden):
        def run(cmd, **kw):
            if not hidden:
                raise OSError(errno.EIO, "Input/output error")
            try:
                raise KeyboardInterrupt
            except KeyboardInterrupt:
                raise OSError(errno.EBADF, "Bad file descriptor")
        runner.subprocess.run = run

    def test_it_hid_one(self):
        self.run_raising(hidden=True)
        with self.assertRaises(KeyboardInterrupt):
            runner.launch_in_iterm("script", 1.0, self.SIDS)
        rec = runner._read_claim(self.SIDS[0])
        self.assertIsNotNone(rec.get("send"))
        self.assertIsNone(rec.get("iterm_pid"))

    def test_it_hid_none(self):                                                 # control
        self.run_raising(hidden=False)
        why = runner.launch_in_iterm("script", 1.0, self.SIDS)[1]
        self.assertIn(runner.MAY_STILL_RUN, why)
        rec = runner._read_claim(self.SIDS[0])
        self.assertNotIn("send", rec)
        self.assertEqual(rec["iterm_pid"], [4242])
