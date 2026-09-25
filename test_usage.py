"""Tests for ccwho_usage: subscription usage, read from each session's statusLine.

Claude Code hands a statusLine command `rate_limits` for the account the session
spends. ccwho records what it is given and never logs in, never calls the API and
never stores a credential. The fake token below must never come back out.
"""
import hashlib
import json
import os
import sys
import tempfile
import unittest

import ccwho_usage as usage

TOKEN = "sk-ant-oat01-FAKE-0000-must-never-leave-the-read"
SID = "b70ab2bf-3e41-460e-abbe-7cb44448725b"
NOW = 1790340000.0
FIVE_RESET = 1790346600
WEEK_RESET = 1790830800


def iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%S.000Z")


def limits(five=5, week=1, five_reset=FIVE_RESET, week_reset=WEEK_RESET):
    return {"five_hour": {"used_percentage": five, "resets_at": five_reset},
            "seven_day": {"used_percentage": week, "resets_at": week_reset}}


class Home:
    """A fake $HOME with a machine login in ~/.claude.json, and a transcript."""

    def __init__(self, email="a@example.com", uuid="uuid-a"):
        self.dir = tempfile.mkdtemp()
        self.login(email, uuid)
        self.transcript = os.path.join(self.dir, "t.jsonl")
        open(self.transcript, "w").close()

    def login(self, email, uuid, config_dir=None):
        path = os.path.join(config_dir or self.dir, ".claude.json")
        with open(path, "w") as fh:
            json.dump({"oauthAccount": {"emailAddress": email, "accountUuid": uuid,
                                        "organizationName": "Org"},
                       "numStartups": 3}, fh)

    def add(self, *records):
        with open(self.transcript, "a") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")

    def payload(self, rate_limits=None, sid=SID):
        p = {"session_id": sid, "transcript_path": self.transcript,
             "cwd": "/x", "model": {"id": "claude-haiku"}}
        if rate_limits is not None:
            p["rate_limits"] = rate_limits
        return p


def reply(epoch):
    return {"type": "assistant", "timestamp": iso(epoch),
            "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}


def login_success(epoch):
    return {"type": "user", "timestamp": iso(epoch),
            "message": {"role": "user",
                        "content": "<local-command-stdout>Login successful</local-command-stdout>"}}


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.h = Home()

    def rec(self, payload, env=None, previous=None, now=NOW):
        return usage.record(payload, env or {}, self.h.dir, now, previous)

    def test_a_reading_is_recorded_with_exact_fields(self):
        self.h.add(reply(NOW - 30))
        r = self.rec(self.h.payload(limits(5, 1)))
        self.assertEqual(r["session_id"], SID)
        self.assertEqual(r["received_at"], NOW)
        self.assertEqual(r["measured_at"], NOW - 30)
        self.assertEqual(r["rate_limits"], limits(5, 1))
        self.assertEqual(r["account"], {"kind": "login", "id": "login:uuid-a",
                                        "email": "a@example.com"})
        self.assertFalse(r["unsure"])

    def test_no_rate_limits_before_the_first_reply_records_nothing(self):
        self.assertIsNone(self.rec(self.h.payload()))

    def test_rate_limits_that_are_not_windows_record_nothing(self):
        for bad in ({}, [], "x", {"five_hour": "x"}, {"five_hour": {"used_percentage": "5"}},
                    {"five_hour": {"used_percentage": True, "resets_at": 1}}):
            self.assertIsNone(self.rec(self.h.payload(bad)), bad)

    def test_only_known_numeric_fields_are_kept(self):
        rl = limits()
        rl["five_hour"]["extra"] = "x"
        rl["surprise"] = {"used_percentage": 1, "resets_at": 2}
        r = self.rec(self.h.payload(rl))
        self.assertEqual(r["rate_limits"], limits())

    def test_one_window_alone_is_a_reading(self):
        r = self.rec(self.h.payload({"seven_day": {"used_percentage": 3, "resets_at": 9}}))
        self.assertEqual(r["rate_limits"], {"seven_day": {"used_percentage": 3, "resets_at": 9}})

    def test_idle_window_is_a_reading_not_missing(self):
        rl = {"five_hour": {"used_percentage": 0, "resets_at": None},
              "seven_day": {"used_percentage": 1, "resets_at": WEEK_RESET}}
        r = self.rec(self.h.payload(rl))
        self.assertEqual(r["rate_limits"]["five_hour"], {"used_percentage": 0, "resets_at": None})

    def test_bad_session_ids_are_refused(self):
        for sid in ("", "../x", "a/b", SID + "\n", "x" * 200, None, 5):
            self.assertIsNone(self.rec(self.h.payload(limits(), sid=sid)), repr(sid))

    def test_a_payload_that_is_not_a_dict_records_nothing(self):
        for p in (None, [], "x", 3):
            self.assertIsNone(self.rec(p))

    def test_token_session_is_its_fingerprint_and_the_token_is_nowhere(self):
        r = self.rec(self.h.payload(limits()), env={"CLAUDE_CODE_OAUTH_TOKEN": TOKEN})
        fp = hashlib.sha256(TOKEN.encode()).hexdigest()[:16]
        self.assertEqual(r["account"], {"kind": "token", "id": "token:" + fp})
        self.assertNotIn(TOKEN, json.dumps(r))
        self.assertNotIn(TOKEN[10:30], json.dumps(r))

    def test_token_wins_over_the_machine_login(self):
        """The harness spends the env token before the login - so must we."""
        r = self.rec(self.h.payload(limits()), env={"CLAUDE_CODE_OAUTH_TOKEN": TOKEN})
        self.assertEqual(r["account"]["kind"], "token")
        self.assertNotIn("a@example.com", json.dumps(r))

    def test_config_dir_login_is_read_from_that_dir(self):
        other = tempfile.mkdtemp()
        self.h.login("b@example.com", "uuid-b", config_dir=other)
        r = self.rec(self.h.payload(limits()), env={"CLAUDE_CONFIG_DIR": other})
        self.assertEqual(r["account"]["id"], "login:uuid-b")

    def test_missing_or_broken_login_is_unknown(self):
        os.remove(os.path.join(self.h.dir, ".claude.json"))
        r = self.rec(self.h.payload(limits()))
        self.assertEqual(r["account"], {"kind": "unknown", "id": None})
        with open(os.path.join(self.h.dir, ".claude.json"), "w") as fh:
            fh.write("{not json")
        r = self.rec(self.h.payload(limits()))
        self.assertEqual(r["account"], {"kind": "unknown", "id": None})

    def test_unreadable_transcript_gives_no_measured_time(self):
        p = self.h.payload(limits())
        p["transcript_path"] = os.path.join(self.h.dir, "gone.jsonl")
        self.assertIsNone(self.rec(p)["measured_at"])
        p["transcript_path"] = None
        self.assertIsNone(self.rec(p)["measured_at"])

    def test_measured_time_is_the_last_assistant_record(self):
        self.h.add(reply(NOW - 500), {"type": "user", "timestamp": iso(NOW - 10),
                                      "message": {"role": "user", "content": "hi"}},
                   reply(NOW - 100), {"type": "system", "timestamp": iso(NOW - 5)},
                   "not json at all")
        with open(self.h.transcript, "a") as fh:
            fh.write("{broken\n")
        self.assertEqual(self.rec(self.h.payload(limits()))["measured_at"], NOW - 100)

    def test_first_account_is_kept_across_readings(self):
        first = self.rec(self.h.payload(limits()))
        again = self.rec(self.h.payload(limits(6)), previous=first, now=NOW + 60)
        self.assertEqual(again["first_account"], first["account"])
        self.assertEqual(again["first_seen"], NOW)
        self.assertFalse(again["unsure"])

    def test_machine_login_switch_makes_an_open_session_unsure(self):
        first = self.rec(self.h.payload(limits()))
        self.h.login("b@example.com", "uuid-b")
        again = self.rec(self.h.payload(limits(6)), previous=first, now=NOW + 60)
        self.assertTrue(again["unsure"])
        self.assertEqual(again["first_account"]["id"], "login:uuid-a")

    def test_login_inside_the_session_after_its_first_reading_adopts_the_new_account(self):
        first = self.rec(self.h.payload(limits()))
        self.h.login("b@example.com", "uuid-b")
        self.h.add(login_success(NOW + 30))
        again = self.rec(self.h.payload(limits(6)), previous=first, now=NOW + 60)
        self.assertFalse(again["unsure"])
        self.assertEqual(again["first_account"]["id"], "login:uuid-b")

    def test_a_login_before_the_first_reading_does_not_excuse_a_later_switch(self):
        self.h.add(login_success(NOW - 300))
        first = self.rec(self.h.payload(limits()))
        self.h.login("b@example.com", "uuid-b")
        again = self.rec(self.h.payload(limits(6)), previous=first, now=NOW + 60)
        self.assertTrue(again["unsure"])

    def test_login_text_inside_a_tool_call_is_not_a_login(self):
        """This very session's transcript held '/login' in a grep command."""
        first = self.rec(self.h.payload(limits()))
        self.h.login("b@example.com", "uuid-b")
        self.h.add({"type": "assistant", "timestamp": iso(NOW + 20), "message": {
            "role": "assistant", "content": [{"type": "tool_use", "input": {
                "command": "grep '<local-command-stdout>Login successful'"}}]}},
            {"type": "user", "timestamp": iso(NOW + 21), "message": {
                "role": "user", "content": [{"type": "tool_result",
                                             "content": "<local-command-stdout>Login successful"}]}})
        again = self.rec(self.h.payload(limits(6)), previous=first, now=NOW + 60)
        self.assertTrue(again["unsure"])

    def test_switching_back_clears_unsure(self):
        first = self.rec(self.h.payload(limits()))
        self.h.login("b@example.com", "uuid-b")
        mid = self.rec(self.h.payload(limits()), previous=first, now=NOW + 60)
        self.h.login("a@example.com", "uuid-a")
        back = self.rec(self.h.payload(limits()), previous=mid, now=NOW + 120)
        self.assertFalse(back["unsure"])

    def test_an_unknown_first_account_is_not_kept(self):
        """.claude.json mid-write on the first call must not make the session
        unsure for life."""
        os.remove(os.path.join(self.h.dir, ".claude.json"))
        first = self.rec(self.h.payload(limits()))
        self.h.login("a@example.com", "uuid-a")
        again = self.rec(self.h.payload(limits(6)), previous=first, now=NOW + 60)
        self.assertFalse(again["unsure"])
        self.assertEqual(again["first_account"]["id"], "login:uuid-a")

    def test_an_unknown_later_reading_keeps_the_first_account_and_is_not_a_switch(self):
        first = self.rec(self.h.payload(limits()))
        os.remove(os.path.join(self.h.dir, ".claude.json"))
        again = self.rec(self.h.payload(limits(6)), previous=first, now=NOW + 60)
        self.assertFalse(again["unsure"])
        self.assertEqual(again["first_account"]["id"], "login:uuid-a")

    def test_a_previous_record_that_is_junk_is_ignored(self):
        for junk in ({"first_account": "x"}, {"first_seen": "x"}, [], "x"):
            r = self.rec(self.h.payload(limits()), previous=junk)
            self.assertEqual(r["first_account"], r["account"])


def reading(account_id, five=None, week=None, measured=None, received=NOW, unsure=False,
            sid=SID, kind="login", email="a@example.com"):
    rl = {}
    if five is not None:
        rl["five_hour"] = {"used_percentage": five[0], "resets_at": five[1]}
    if week is not None:
        rl["seven_day"] = {"used_percentage": week[0], "resets_at": week[1]}
    acct = {"kind": kind, "id": account_id}
    if kind == "login":
        acct["email"] = email
    return {"v": 1, "session_id": sid, "received_at": received, "measured_at": measured,
            "rate_limits": rl, "account": acct, "first_account": acct,
            "first_seen": received, "login_at": None, "unsure": unsure}


class TestAccounts(unittest.TestCase):
    def test_one_row_per_account_id(self):
        rows = usage.accounts([reading("login:a", five=(5, FIVE_RESET)),
                               reading("token:1", five=(9, FIVE_RESET), kind="token")], NOW)
        self.assertEqual(sorted(r["id"] for r in rows), ["login:a", "token:1"])

    def test_newest_measurement_wins_not_newest_receipt(self):
        busy = reading("login:a", five=(60, FIVE_RESET), measured=NOW - 10, received=NOW - 10)
        idle = reading("login:a", five=(5, FIVE_RESET), measured=NOW - 3000, received=NOW,
                       sid="idle")
        row, = usage.accounts([busy, idle], NOW)
        self.assertEqual(row["five_hour"], {"state": "ok", "pct": 60, "resets_at": FIVE_RESET})
        self.assertEqual(row["age"], 10)

    def test_without_measurements_the_highest_in_a_window_wins(self):
        a = reading("login:a", five=(60, FIVE_RESET), received=NOW - 100)
        b = reading("login:a", five=(5, FIVE_RESET), received=NOW, sid="b")
        row, = usage.accounts([a, b], NOW)
        self.assertEqual(row["five_hour"]["pct"], 60)

    def test_a_later_window_beats_a_higher_number_in_an_older_one(self):
        old = reading("login:a", five=(90, FIVE_RESET - 18000), measured=NOW - 20000)
        new = reading("login:a", five=(2, FIVE_RESET), measured=NOW - 100, sid="n")
        row, = usage.accounts([old, new], NOW)
        self.assertEqual(row["five_hour"], {"state": "ok", "pct": 2, "resets_at": FIVE_RESET})

    def test_windows_are_chosen_separately(self):
        a = reading("login:a", five=(10, FIVE_RESET), measured=NOW - 10)
        b = reading("login:a", week=(40, WEEK_RESET), measured=NOW - 5000, sid="b")
        row, = usage.accounts([a, b], NOW)
        self.assertEqual(row["five_hour"]["pct"], 10)
        self.assertEqual(row["seven_day"]["pct"], 40)

    def test_a_window_past_its_reset_is_expired_never_a_number(self):
        r = reading("login:a", five=(80, NOW - 60), week=(30, WEEK_RESET), measured=NOW - 4000)
        row, = usage.accounts([r], NOW)
        self.assertEqual(row["five_hour"], {"state": "expired", "resets_at": NOW - 60})
        self.assertNotIn("pct", row["five_hour"])
        self.assertEqual(row["seven_day"]["state"], "ok")

    def test_an_idle_reading_measured_after_the_reset_is_the_new_window(self):
        old = reading("login:a", five=(60, NOW - 1000), measured=NOW - 2000)
        idle = reading("login:a", five=(0, None), measured=NOW - 500, sid="i")
        row, = usage.accounts([old, idle], NOW)
        self.assertEqual(row["five_hour"], {"state": "ok", "pct": 0, "resets_at": None})

    def test_an_idle_reading_measured_before_the_reset_leaves_it_expired(self):
        old = reading("login:a", five=(60, NOW - 1000), measured=NOW - 2000)
        idle = reading("login:a", five=(0, None), measured=NOW - 1500, sid="i")
        row, = usage.accounts([old, idle], NOW)
        self.assertEqual(row["five_hour"]["state"], "expired")

    def test_reset_times_a_few_seconds_apart_are_one_window(self):
        old = reading("login:a", five=(5, FIVE_RESET + 1), measured=NOW - 3000)
        new = reading("login:a", five=(60, FIVE_RESET), measured=NOW - 10, sid="n")
        row, = usage.accounts([old, new], NOW)
        self.assertEqual(row["five_hour"]["pct"], 60)

    def test_reset_times_hours_apart_are_different_windows(self):
        old = reading("login:a", five=(90, FIVE_RESET - 5 * 3600), measured=NOW - 10)
        new = reading("login:a", five=(2, FIVE_RESET), measured=NOW - 3000, sid="n")
        row, = usage.accounts([old, new], NOW)
        self.assertEqual(row["five_hour"]["pct"], 2)

    def test_a_measured_reading_beats_an_unmeasured_one_in_the_same_window(self):
        """Without a transcript time a reading cannot be placed; it only counts
        when no reading of that window can be."""
        timed = reading("login:a", five=(10, FIVE_RESET), measured=NOW - 3000)
        untimed = reading("login:a", five=(50, FIVE_RESET), received=NOW, sid="u")
        row, = usage.accounts([timed, untimed], NOW)
        self.assertEqual(row["five_hour"]["pct"], 10)

    def test_idle_window_reads_as_zero_with_no_reset(self):
        r = reading("login:a", five=(0, None), measured=NOW - 10)
        row, = usage.accounts([r], NOW)
        self.assertEqual(row["five_hour"], {"state": "ok", "pct": 0, "resets_at": None})

    def test_unsure_and_unknown_readings_join_no_account(self):
        rows = usage.accounts([reading("login:a", five=(5, FIVE_RESET), unsure=True),
                               reading(None, five=(5, FIVE_RESET), kind="unknown")], NOW)
        self.assertEqual(rows, [])

    def test_the_session_counts_its_account_by_first_account(self):
        """After an in-session /login the record's first_account is the new one."""
        r = reading("login:a", five=(5, FIVE_RESET))
        r["first_account"] = {"kind": "login", "id": "login:b", "email": "b@example.com"}
        row, = usage.accounts([r], NOW)
        self.assertEqual(row["id"], "login:b")
        self.assertEqual(row["email"], "b@example.com")

    def test_sessions_are_counted(self):
        rows = usage.accounts([reading("login:a", five=(5, FIVE_RESET), sid=s)
                               for s in ("x", "y", "z")], NOW)
        self.assertEqual(rows[0]["sessions"], 3)

    def test_labels_are_display_only(self):
        rows = usage.accounts([reading("login:a", five=(5, FIVE_RESET)),
                               reading("token:1", five=(9, FIVE_RESET), kind="token")],
                              NOW, labels={"login:a": "work", "token:1": "work"})
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["label"] for r in rows}, {"work"})

    def test_default_label_is_email_or_short_token(self):
        rows = {r["id"]: r for r in usage.accounts(
            [reading("login:a", five=(5, FIVE_RESET)),
             reading("token:0123456789abcdef", five=(9, FIVE_RESET), kind="token")], NOW)}
        self.assertEqual(rows["login:a"]["label"], "a@example.com")
        self.assertEqual(rows["token:0123456789abcdef"]["label"], "token 01234567")


class TestLoadReadings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def put(self, name, obj, age=0):
        path = os.path.join(self.dir, name)
        with open(path, "w") as fh:
            fh.write(obj if isinstance(obj, str) else json.dumps(obj))
        os.utime(path, (NOW - age, NOW - age))
        return path

    def test_reads_every_reading(self):
        self.put(SID + ".json", reading("login:a", five=(5, FIVE_RESET)))
        self.put("other.json", reading("login:b", five=(5, FIVE_RESET), sid="other"))
        self.assertEqual(len(usage.load_readings(self.dir, NOW)), 2)

    def test_corrupt_files_are_skipped_and_the_rest_kept(self):
        self.put("bad.json", "{nope")
        self.put("list.json", "[]")
        self.put("good.json", reading("login:a", five=(5, FIVE_RESET)))
        self.assertEqual(len(usage.load_readings(self.dir, NOW)), 1)

    def test_older_than_eight_days_is_ignored(self):
        self.put("old.json", reading("login:a", five=(5, FIVE_RESET),
                                     received=NOW - 9 * 86400))
        self.assertEqual(usage.load_readings(self.dir, NOW), [])

    def test_temp_files_and_other_names_are_not_readings(self):
        self.put(SID + ".json.123.tmp", reading("login:a", five=(5, FIVE_RESET)))
        self.put("notes.txt", "x")
        self.assertEqual(usage.load_readings(self.dir, NOW), [])

    def test_missing_dir_is_no_readings(self):
        self.assertEqual(usage.load_readings(os.path.join(self.dir, "nope"), NOW), [])

    def test_prune_names_files_older_than_eight_days_only(self):
        self.put("old.json", "{}", age=9 * 86400)
        self.put("new.json", "{}", age=60)
        self.put("old.json.1.tmp", "{}", age=9 * 86400)
        self.assertEqual(sorted(usage.prune(self.dir, NOW)), ["old.json", "old.json.1.tmp"])


class TestLabels(unittest.TestCase):
    def test_name_by_exact_id_or_unique_prefix(self):
        ids = ["token:0123456789abcdef", "login:uuid-a"]
        self.assertEqual(usage.resolve_id("token:0123", ids), "token:0123456789abcdef")
        self.assertEqual(usage.resolve_id("login:uuid-a", ids), "login:uuid-a")
        self.assertEqual(usage.resolve_id("0123", ids), "token:0123456789abcdef")

    def test_ambiguous_or_unknown_is_none(self):
        ids = ["token:01aa", "token:01bb"]
        self.assertIsNone(usage.resolve_id("token:01", ids))
        self.assertIsNone(usage.resolve_id("zz", ids))
        self.assertIsNone(usage.resolve_id("", ids))


class TestFormat(unittest.TestCase):
    def test_a_row_reads_label_windows_and_age(self):
        row = {"id": "login:a", "label": "a@example.com", "sessions": 2, "age": 180,
               "five_hour": {"state": "ok", "pct": 42, "resets_at": FIVE_RESET},
               "seven_day": {"state": "expired", "resets_at": NOW - 60}}
        line = usage.format_row(row, NOW)
        self.assertIn("a@example.com", line)
        self.assertIn("5h 42%", line)
        self.assertIn("7d expired", line)
        self.assertIn("3m ago", line)

    def test_a_missing_window_is_a_dash(self):
        row = {"id": "x", "label": "x", "sessions": 1, "age": 5,
               "five_hour": None, "seven_day": {"state": "ok", "pct": 1, "resets_at": None}}
        line = usage.format_row(row, NOW)
        self.assertIn("5h —", line)
        self.assertIn("7d 1%", line)


class TestStaysLight(unittest.TestCase):
    def test_the_module_imports_nothing_heavy(self):
        import subprocess
        out = subprocess.run([sys.executable, "-c",
                              "import sys, ccwho_usage; print('textual' in sys.modules)"],
                             capture_output=True, text=True,
                             cwd=os.path.dirname(os.path.abspath(__file__)))
        self.assertEqual(out.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
