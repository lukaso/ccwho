"""Tests for ccgate - admission control so N sessions don't all gate at once."""
import json
import os
import tempfile
import unittest

import ccgate


class TestLoadCeiling(unittest.TestCase):
    def test_under_ceiling_is_ok(self):
        self.assertTrue(ccgate.load_ok(4.0, cores=16, factor=0.75))

    def test_over_ceiling_is_not_ok(self):
        self.assertFalse(ccgate.load_ok(18.5, cores=16, factor=0.75))

    def test_boundary_is_admitted(self):
        self.assertTrue(ccgate.load_ok(12.0, cores=16, factor=0.75))

    def test_zero_cores_never_divides_by_zero(self):
        self.assertTrue(ccgate.load_ok(1.0, cores=0, factor=0.75))

    def test_factor_zero_disables_the_check(self):
        self.assertTrue(ccgate.load_ok(99.0, cores=16, factor=0))


class TestSlots(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_claims_a_free_slot(self):
        self.assertIsNotNone(ccgate.claim_slot(self.dir, 2, pid=111, label="a"))

    def test_second_claim_takes_the_other_slot(self):
        a = ccgate.claim_slot(self.dir, 2, pid=111, label="a")
        b = ccgate.claim_slot(self.dir, 2, pid=222, label="b")
        self.assertNotEqual(a, b)
        self.assertIsNotNone(b)

    def test_refuses_when_all_slots_held(self):
        ccgate.claim_slot(self.dir, 1, pid=111, label="a")
        self.assertIsNone(ccgate.claim_slot(self.dir, 1, pid=222, label="b"))

    def test_release_frees_the_slot(self):
        s = ccgate.claim_slot(self.dir, 1, pid=111, label="a")
        ccgate.release_slot(s)
        self.assertIsNotNone(ccgate.claim_slot(self.dir, 1, pid=222, label="b"))

    def test_release_of_missing_slot_is_not_an_error(self):
        ccgate.release_slot(os.path.join(self.dir, "nope"))

    def test_slot_file_records_pid_and_label(self):
        s = ccgate.claim_slot(self.dir, 1, pid=111, label="the gate")
        rec = json.load(open(s))
        self.assertEqual(rec["pid"], 111)
        self.assertEqual(rec["label"], "the gate")


class TestStaleReaping(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_dead_holder_is_reclaimed(self):
        ccgate.claim_slot(self.dir, 1, pid=999999, label="dead")
        reaped = ccgate.reap_stale(self.dir, alive=lambda p: False)
        self.assertEqual(reaped, 1)
        self.assertIsNotNone(ccgate.claim_slot(self.dir, 1, pid=1, label="new"))

    def test_live_holder_is_kept(self):
        ccgate.claim_slot(self.dir, 1, pid=111, label="live")
        self.assertEqual(ccgate.reap_stale(self.dir, alive=lambda p: True), 0)
        self.assertIsNone(ccgate.claim_slot(self.dir, 1, pid=222, label="b"))

    def test_corrupt_slot_file_is_reclaimed(self):
        p = os.path.join(self.dir, "slot-0.json")
        open(p, "w").write("{not json")
        self.assertEqual(ccgate.reap_stale(self.dir, alive=lambda p: True), 1)

    def test_empty_dir_reaps_nothing(self):
        self.assertEqual(ccgate.reap_stale(self.dir, alive=lambda p: True), 0)


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_reports_holders(self):
        ccgate.claim_slot(self.dir, 2, pid=111, label="gate A")
        got = ccgate.gate_status(self.dir)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["label"], "gate A")

    def test_empty_when_nothing_held(self):
        self.assertEqual(ccgate.gate_status(self.dir), [])


if __name__ == "__main__":
    unittest.main()
