"""Tests for the ccwho runner's argument handling.

A flag that is silently ignored is the failure mode these cover: --watch=3 and -w
both used to fall through to one-shot with no complaint.
"""
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
