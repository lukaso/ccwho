"""Tests for the ccwho runner's argument handling.

A flag that is silently ignored is the failure mode these cover: --watch=3 and -w
both used to fall through to one-shot with no complaint.
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest

import ccwho as runner


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
        try:
            d = runner.restore_dir()
            self.assertTrue(d.startswith(os.path.expanduser("~")), d)
            self.assertNotIn("/tmp", d, "a temp dir does not survive the reboot it exists for")
        finally:
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
             "tty": "s032", "since": "2h", "status": "waiting", "first": "", "pid": 1},
            {"sessionId": "", "cwd": "/Users/x/p/nope", "project": "nope", "topic": "t",
             "ask": "", "attention": "stopped", "tty": "", "since": "1h", "status": "waiting",
             "first": "", "pid": 2}]

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["CCWHO_DIR"] = self.tmp
        self.real_collect = runner.engine.collect
        def fake_collect(cache=None, status=None):
            if status is not None:
                status["source_ok"] = True      # this class tests a REACHABLE source
            return (list(self.ROWS), 0)

        runner.engine.collect = fake_collect

    def tearDown(self):
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
        runner.engine.transcript_path = lambda sid: "/tx"
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
        with open(os.path.join(d, "2026-08-01T0001.json"), "w") as fh:
            json.dump({"version": 1, "savedAt": 1788213090, "count": 1,
                       "skipped": 0, "sessions": [self.ENTRY]}, fh)
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

    def tearDown(self):
        runner.engine.collect = self.real_collect
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

    def test_a_live_session_without_a_window_is_named_not_reopened(self):
        self.live = [{"sessionId": self.SID, "tty": "", "pid": 90266}]
        rc, out = self._open(self.SID)
        self.assertEqual(rc, 1)
        self.assertEqual(self.runs, [], "a running session must not be resumed")
        self.assertIn("90266", out)
        self.assertIn("not reopening", out)

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
        with open(self.man, "w") as fh:
            json.dump({"version": 1, "savedAt": 1788213090, "count": 2, "skipped": 0,
                       "sessions": [
                           {"sessionId": self.LIVE_SID, "cwd": "/Users/x/p/a",
                            "project": "a"},
                           {"sessionId": self.DEAD_SID, "cwd": "/Users/x/p/b",
                            "project": "b"}]}, fh)
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

    def tearDown(self):
        runner.engine.collect = self.real_collect
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

    def test_an_unreadable_fleet_opens_nothing_at_all(self):
        self.source_ok = False
        rc, out = self._restore_open()
        self.assertEqual(rc, 1)
        self.assertEqual(self.runs, [], "17 windows on a guess is the worst outcome")
        self.assertIn("not reopening", out)

    def test_the_same_session_twice_in_a_manifest_opens_once(self):
        # a hand-edited or double-written manifest must not start two processes
        # on one transcript - the exact harm this guard exists to prevent
        with open(self.man, "w") as fh:
            json.dump({"version": 1, "savedAt": 1788213090, "count": 2, "skipped": 0,
                       "sessions": [
                           {"sessionId": self.DEAD_SID, "cwd": "/Users/x/p/b",
                            "project": "b"},
                           {"sessionId": self.DEAD_SID, "cwd": "/Users/x/p/b",
                            "project": "b"}]}, fh)
        rc, out = self._restore_open()
        self.assertEqual(rc, 0)
        self.assertEqual(self._script().count("claude --resume " + self.DEAD_SID), 1)
        # and the duplicate is simply not a second entry: it is the same session,
        # not another process starting it, so it must not be reported as one
        self.assertNotIn("already starting", out)

    def test_an_entry_that_cannot_be_rebuilt_is_reported_not_swallowed(self):
        # one running + one unusable is not "all of them are already running"
        with open(self.man, "w") as fh:
            json.dump({"version": 1, "savedAt": 1788213090, "count": 2, "skipped": 0,
                       "sessions": [
                           {"sessionId": self.LIVE_SID, "cwd": "/Users/x/p/a",
                            "project": "a"},
                           {"sessionId": "not a session id", "cwd": "/Users/x/p/c",
                            "project": "c"}]}, fh)
        self.live = [{"sessionId": self.LIVE_SID, "tty": "ttys009", "pid": 7}]
        rc, out = self._restore_open()
        self.assertEqual(self.runs, [])
        self.assertEqual(rc, 1, "nothing opened and something was unusable")
        self.assertIn("cannot be reopened", out)

    def test_every_session_live_opens_nothing_and_says_so(self):
        self.live = [{"sessionId": self.LIVE_SID, "tty": "ttys009", "pid": 7},
                     {"sessionId": self.DEAD_SID, "tty": "ttys010", "pid": 8}]
        rc, out = self._restore_open()
        self.assertEqual(self.runs, [])
        self.assertIn("already open", out)


class TestLaunchClaim(unittest.TestCase):
    """The guard reads the world, then launches. Between those two moments another
    ccwho - a second click, a restore running in another window - reads the same
    world and launches too. A new session also takes a moment to show up in
    `claude agents`, so the window is real even for one caller in a hurry.

    Same idiom as ccgate's slots: O_CREAT|O_EXCL, and a claim whose holder is gone
    is reclaimed rather than blocking forever.
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
        # the engine imports both of them now; a module left out of the reload is
        # a module whose fix silently does not land in a running watch
        for mod in ("ccwho_brief", "ccwho_index"):
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
                              "ccwho_index": runner.index}[mod]
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

    def tearDown(self):
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


class TestWatchBannerIsCheap(unittest.TestCase):
    """The watch header should say when something drifted, but the checks shell
    out to launchctl, osascript and plutil. At a 5s tick that is three processes
    a second for an answer that changes about once a month."""

    def setUp(self):
        self.calls = []
        self.real = runner.setup.gather
        runner.setup.gather = lambda **kw: self.calls.append(1) or {
            "claude": "", "iterm_ok": True, "handler_registered": True,
            "launchd_loaded": True, "last_run_age": 10.0,
            "newest_manifest_age": 10.0, "cc_status_hook": True,
            "settings_path": "/x"}

    def tearDown(self):
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
