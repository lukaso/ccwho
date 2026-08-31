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
        runner.engine.collect = lambda cache=None: (list(self.ROWS), 0)

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
