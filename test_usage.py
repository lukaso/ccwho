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


def no_age(w):
    """A window without its age: the tests below are about the value; age has
    its own tests (TestWindowAge)."""
    return {k: v for k, v in w.items() if k != "age"}


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
        self.assertEqual(no_age(row["five_hour"]), {"state": "ok", "pct": 60, "resets_at": FIVE_RESET})
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
        self.assertEqual(no_age(row["five_hour"]), {"state": "ok", "pct": 2, "resets_at": FIVE_RESET})

    def test_windows_are_chosen_separately(self):
        a = reading("login:a", five=(10, FIVE_RESET), measured=NOW - 10)
        b = reading("login:a", week=(40, WEEK_RESET), measured=NOW - 5000, sid="b")
        row, = usage.accounts([a, b], NOW)
        self.assertEqual(row["five_hour"]["pct"], 10)
        self.assertEqual(row["seven_day"]["pct"], 40)

    def test_a_window_past_its_reset_is_expired_never_a_number(self):
        r = reading("login:a", five=(80, NOW - 60), week=(30, WEEK_RESET), measured=NOW - 4000)
        row, = usage.accounts([r], NOW)
        self.assertEqual(no_age(row["five_hour"]), {"state": "expired", "resets_at": NOW - 60})
        self.assertNotIn("pct", row["five_hour"])
        self.assertEqual(row["seven_day"]["state"], "ok")

    def test_an_idle_reading_measured_after_the_reset_is_the_new_window(self):
        old = reading("login:a", five=(60, NOW - 1000), measured=NOW - 2000)
        idle = reading("login:a", five=(0, None), measured=NOW - 500, sid="i")
        row, = usage.accounts([old, idle], NOW)
        self.assertEqual(no_age(row["five_hour"]), {"state": "ok", "pct": 0, "resets_at": None})

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
        self.assertEqual(no_age(row["five_hour"]), {"state": "ok", "pct": 0, "resets_at": None})

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


# ------------------------------------------------------------ slice C: display
H5, D7 = 18000, 604800


def acct_row(aid, five=None, week=None, kind="login", email="a@example.com",
             age=10, five_age=None, week_age=None, sessions=1):
    def win(w, a):
        if w is None:
            return None
        if w == "expired":
            return {"state": "expired", "resets_at": NOW - 60, "age": a}
        pct, reset = w
        return {"state": "ok", "pct": pct, "resets_at": reset, "age": a}
    return {"id": aid, "kind": kind, "email": email if kind == "login" else "",
            "label": "", "brand": "ant", "sessions": sessions, "age": age,
            "five_hour": win(five, age if five_age is None else five_age),
            "seven_day": win(week, age if week_age is None else week_age)}


def snap(rows, sessions=None, state="ok", labels=None):
    names = usage.short_names(rows, labels or {})
    return {"state": state, "accounts": usage.ordered(rows, names), "names": names,
            "sessions": sessions or {}, "now": NOW}


def text(lines):
    return usage.plain(lines)


class TestPace(unittest.TestCase):
    def test_elapsed_is_the_share_of_the_window_gone(self):
        self.assertEqual(usage.elapsed_pct("five_hour", NOW + 0.4 * H5, NOW), 60)
        self.assertEqual(usage.elapsed_pct("seven_day", NOW + 0.7 * D7, NOW), 30)

    def test_elapsed_is_clamped_and_none_without_a_reset(self):
        self.assertEqual(usage.elapsed_pct("five_hour", NOW - 10, NOW), 100)
        self.assertEqual(usage.elapsed_pct("five_hour", NOW + 3 * H5, NOW), 0)
        self.assertIsNone(usage.elapsed_pct("five_hour", None, NOW))
        self.assertIsNone(usage.elapsed_pct("nope", NOW + 10, NOW))

    def test_arrow_up_red_when_faster_than_time_down_green_otherwise(self):
        self.assertEqual(usage.pace_arrow(67, 40), ("↑", "red"))
        self.assertEqual(usage.pace_arrow(40, 40), ("↓", "green"))
        self.assertEqual(usage.pace_arrow(10, 60), ("↓", "green"))
        self.assertIsNone(usage.pace_arrow(0, None))

    def test_the_used_number_warms_at_80_and_95(self):
        self.assertEqual(usage.used_style(79.9, "dim"), "dim")
        self.assertEqual(usage.used_style(80, "dim"), "yellow")
        self.assertEqual(usage.used_style(95, "plain"), "byellow")


class TestShortNames(unittest.TestCase):
    def test_email_local_part_and_four_hex(self):
        rows = [acct_row("login:u1", email="lukaso@gmail.com"),
                acct_row("token:0123456789abcdef", kind="token")]
        self.assertEqual(usage.short_names(rows, {}),
                         {"login:u1": "lukaso", "token:0123456789abcdef": "0123"})

    def test_clashing_emails_grow_to_the_domain_then_the_whole_address(self):
        rows = [acct_row("login:u1", email="lukaso@gmail.com"),
                acct_row("login:u2", email="lukaso@work.com"),
                acct_row("login:u3", email="ann@x.com")]
        names = usage.short_names(rows, {})
        self.assertEqual(names["login:u1"], "lukaso@gmail")
        self.assertEqual(names["login:u2"], "lukaso@work")
        self.assertEqual(names["login:u3"], "ann")
        rows = [acct_row("login:u1", email="a@x.com"), acct_row("login:u2", email="a@x.org")]
        names = usage.short_names(rows, {})
        self.assertEqual(sorted(names.values()), ["a@x.com", "a@x.org"])

    def test_clashing_tokens_grow_by_two_hex(self):
        rows = [acct_row("token:1a2b3c4d", kind="token"), acct_row("token:1a2b9f00", kind="token")]
        self.assertEqual(sorted(usage.short_names(rows, {}).values()), ["1a2b3c", "1a2b9f"])

    def test_a_label_wins_and_equal_labels_get_the_short_id(self):
        rows = [acct_row("login:u1", email="lukaso@gmail.com"),
                acct_row("token:1a2b3c4d", kind="token")]
        names = usage.short_names(rows, {"login:u1": "work", "token:1a2b3c4d": "work"})
        self.assertEqual(names, {"login:u1": "work lukaso", "token:1a2b3c4d": "work 1a2b"})
        self.assertEqual(usage.short_names(rows, {"login:u1": "me"})["login:u1"], "me")

    def test_every_name_is_unique(self):
        rows = [acct_row(f"token:{i:04x}{i:04x}", kind="token") for i in range(40)]
        names = usage.short_names(rows, {})
        self.assertEqual(len(set(names.values())), 40)


class TestOrder(unittest.TestCase):
    def test_brand_then_login_before_token_then_name(self):
        rows = [acct_row("token:ffff", kind="token"), acct_row("login:b", email="zed@x.com"),
                acct_row("token:aaaa", kind="token"), acct_row("login:a", email="ann@x.com")]
        names = usage.short_names(rows, {})
        self.assertEqual([r["id"] for r in usage.ordered(rows, names)],
                         ["login:a", "login:b", "token:aaaa", "token:ffff"])

    def test_order_does_not_follow_activity(self):
        a = acct_row("login:a", email="ann@x.com", age=5000)
        b = acct_row("login:b", email="bob@x.com", age=1)
        names = usage.short_names([a, b], {})
        self.assertEqual([r["id"] for r in usage.ordered([b, a], names)], ["login:a", "login:b"])


class TestWindowAge(unittest.TestCase):
    def test_accounts_keep_an_age_per_window(self):
        fresh = reading("login:a", five=(10, FIVE_RESET), measured=NOW - 10)
        old = reading("login:a", week=(40, WEEK_RESET), measured=NOW - 3 * 3600, sid="o")
        row, = usage.accounts([fresh, old], NOW)
        self.assertEqual(row["five_hour"]["age"], 10)
        self.assertEqual(row["seven_day"]["age"], 3 * 3600)
        self.assertEqual(row["brand"], "ant")


class TestLoadReadingsValidates(unittest.TestCase):
    def test_malformed_windows_are_dropped_and_good_files_kept(self):
        d = tempfile.mkdtemp()
        good = reading("login:a", five=(5, FIVE_RESET))
        bad = [dict(reading("login:b", five=(5, FIVE_RESET), sid="b1"),
                    rate_limits={"five_hour": {}}),
               dict(reading("login:c", five=(5, FIVE_RESET), sid="c1"),
                    rate_limits={"five_hour": {"used_percentage": "5", "resets_at": 1}}),
               dict(reading("login:d", five=(5, FIVE_RESET), sid="d1"), rate_limits=[]),
               dict(reading("login:e", five=(5, FIVE_RESET), sid="e1"), first_account="x")]
        for i, r in enumerate([good] + bad):
            with open(os.path.join(d, f"r{i}.json"), "w") as fh:
                json.dump(r, fh)
        got = usage.load_readings(d, NOW)
        self.assertEqual([r["session_id"] for r in got], [SID])
        rows = usage.accounts(got, NOW)                  # and nothing downstream raises
        self.assertEqual(len(rows), 1)

    def test_a_reading_with_one_bad_window_keeps_the_good_one(self):
        d = tempfile.mkdtemp()
        r = reading("login:a", five=(5, FIVE_RESET), week=(9, WEEK_RESET))
        r["rate_limits"]["five_hour"] = {}
        with open(os.path.join(d, "r.json"), "w") as fh:
            json.dump(r, fh)
        got, = usage.load_readings(d, NOW)
        self.assertEqual(set(got["rate_limits"]), {"seven_day"})


class TestSnapshot(unittest.TestCase):
    FACTS_OURS = {"usage_roots": [{"root": "/h/.claude", "state": "ours", "opted_out": False}]}

    def test_only_accounts_a_live_session_spends(self):
        a = reading("login:a", five=(5, FIVE_RESET), sid="s1")
        b = reading("login:b", five=(5, FIVE_RESET), sid="s2", email="b@x.com")
        s = usage.snapshot([a, b], NOW, live_ids={"s1"}, facts=self.FACTS_OURS)
        self.assertEqual([r["id"] for r in s["accounts"]], ["login:a"])
        self.assertEqual(s["sessions"], {"s1": "login:a"})
        self.assertEqual(s["state"], "ok")

    def test_empty_state_precedence(self):
        live = {"s1"}
        facts = lambda *states: {"usage_roots": [
            {"root": f"/r{i}", "state": st, "opted_out": oo} for i, (st, oo) in enumerate(states)]}
        r = reading("login:a", five=(5, FIVE_RESET), sid="s1")
        self.assertEqual(usage.snapshot([r], NOW, live, facts(("missing", True)))["state"], "ok")
        self.assertEqual(usage.snapshot([], NOW, live, facts(("ours", False), ("missing", False)))
                         ["state"], "waiting")
        self.assertEqual(usage.snapshot([], NOW, live, facts(("missing", True), ("other", True)))
                         ["state"], "off")
        self.assertEqual(usage.snapshot([], NOW, live, facts(("missing", False), ("missing", True)))
                         ["state"], "not_set_up")
        self.assertEqual(usage.snapshot([], NOW, live, {"usage_roots": []})["state"], "not_set_up")

    def test_unsure_readings_map_to_no_account(self):
        r = reading("login:a", five=(5, FIVE_RESET), sid="s1", unsure=True)
        s = usage.snapshot([r], NOW, {"s1"}, self.FACTS_OURS)
        self.assertEqual(s["accounts"], [])
        self.assertEqual(s["state"], "waiting")


class TestUsageLines(unittest.TestCase):
    L = acct_row("login:a", email="lukaso@gmail.com",
                 five=(42, NOW + 0.4 * H5), week=(2, NOW + 0.7 * D7))
    T = acct_row("token:1a2b3c4d", kind="token", five=(0, None), week=(67, NOW + 0.6 * D7))

    def test_one_account_one_line(self):
        line, = text(usage.usage_lines(snap([self.L]), 160))
        self.assertTrue(line.startswith("usage  ant lukaso 5h 42%↓/60% ↻"), line)
        self.assertIn("· 7d 2%↓/30% ↻", line)

    def test_two_accounts_fit_on_one_line_separated(self):
        line, = text(usage.usage_lines(snap([self.L, self.T]), 200))
        self.assertIn("  │  ant 1a2b 5h 0% · 7d 67%↑/40%", line)

    def test_they_stack_when_they_do_not_fit_one_per_account(self):
        lines = text(usage.usage_lines(snap([self.L, self.T]), 90))
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("usage  ant lukaso "))
        self.assertTrue(lines[1].startswith("       ant 1a2b "))

    def test_all_live_accounts_are_shown_no_cap(self):
        rows = [acct_row(f"token:{i:04x}0000", kind="token", five=(1, None)) for i in range(7)]
        self.assertEqual(len(usage.usage_lines(snap(rows), 60)), 7)

    def test_narrow_drops_resets_then_age_then_brand_then_cuts(self):
        old = acct_row("login:a", email="lukaso@gmail.com", five=(42, NOW + 0.4 * H5),
                       week=(2, NOW + 0.7 * D7), week_age=7200)
        full = text(usage.usage_lines(snap([old]), 200))[0]
        self.assertIn("↻", full)
        self.assertIn("(2h ago)", full)
        no_reset = text(usage.usage_lines(snap([old]), len(full) - 1))[0]
        self.assertNotIn("↻", no_reset)
        self.assertIn("(2h ago)", no_reset)
        no_age = text(usage.usage_lines(snap([old]), len(no_reset) - 1))[0]
        self.assertNotIn("ago", no_age)
        self.assertIn("ant ", no_age)
        no_brand = text(usage.usage_lines(snap([old]), len(no_age) - 1))[0]
        self.assertTrue(no_brand.startswith("usage  lukaso 5h 42%↓/60%"), no_brand)
        cut = text(usage.usage_lines(snap([old]), 20))[0]
        self.assertTrue(cut.endswith("…"))
        self.assertLessEqual(len(cut), 20)

    def test_age_only_on_a_window_older_than_15_minutes(self):
        r = acct_row("login:a", email="lukaso@gmail.com", five=(42, NOW + 0.4 * H5),
                     week=(2, NOW + 0.7 * D7), five_age=60, week_age=16 * 60)
        line = text(usage.usage_lines(snap([r]), 200))[0]
        five, week = line.split(" · 7d ")
        self.assertNotIn("ago", five)
        self.assertIn("(16m ago)", week)

    def test_expired_window_has_no_number(self):
        r = acct_row("login:a", email="lukaso@gmail.com", five="expired", week=(2, NOW + 0.7 * D7))
        line = text(usage.usage_lines(snap([r]), 200))[0]
        self.assertIn("5h expired ↻", line)
        self.assertNotIn("5h expired ↻" + "x", line)

    def test_empty_states(self):
        self.assertEqual(text(usage.usage_lines(snap([], state="not_set_up"), 100)),
                         ["usage  not set up - ccwho setup adds it"])
        self.assertEqual(text(usage.usage_lines(snap([], state="waiting"), 100)),
                         ["usage  waiting - sessions report after their next reply"])
        self.assertEqual(usage.usage_lines(snap([], state="off"), 100), [])
        self.assertEqual(text(usage.usage_lines(snap([], state="unknown"), 100)),
                         ["usage  unknown"])
        self.assertEqual(text(usage.usage_lines(None, 100)), ["usage  unknown"])

    def styles(self, lines):
        return [(t, s) for line in lines for t, s in line]

    def test_everything_is_dim_except_the_selected_account_and_the_warnings(self):
        spans = self.styles(usage.usage_lines(snap([self.L, self.T]), 200))
        plain_text = [t for t, s in spans if s not in ("dim",) and t.strip()]
        self.assertEqual(sorted(plain_text), ["↑", "↓", "↓"])

    def test_the_selected_account_is_bright(self):
        spans = self.styles(usage.usage_lines(snap([self.L, self.T]), 200, selected="token:1a2b3c4d"))
        bright = "".join(t for t, s in spans if s == "plain")
        self.assertIn("ant 1a2b", bright)
        self.assertNotIn("lukaso", bright)

    def test_arrows_are_never_dim(self):
        for sel in (None, "login:a"):
            for t, s in self.styles(usage.usage_lines(snap([self.L, self.T]), 200, selected=sel)):
                if t in ("↑", "↓"):
                    self.assertIn(s, ("red", "green"))

    def test_warm_numbers(self):
        hot = acct_row("login:a", email="a@x.com", five=(96, NOW + 0.05 * H5), week=(81, NOW + 0.5 * D7))
        spans = self.styles(usage.usage_lines(snap([hot]), 200))
        self.assertIn(("96%", "byellow"), spans)
        self.assertIn(("81%", "yellow"), spans)

    def test_a_cut_never_splits_a_span_style(self):
        spans = self.styles(usage.usage_lines(snap([self.L]), 25))
        self.assertLessEqual(sum(len(t) for t, _ in spans), 25)
        for t, s in spans:
            self.assertIn(s, usage.STYLES)

    def test_ansi_closes_every_span(self):
        out = usage.to_ansi(usage.usage_lines(snap([self.L, self.T]), 200)[0])
        self.assertTrue(out.endswith("\033[0m"))
        self.assertEqual(out.count("\033[0m"), len([p for p in out.split("\033[0m") if p]))


class TestRowTag(unittest.TestCase):
    L = acct_row("login:a", email="lukaso@gmail.com", five=(1, None))
    T = acct_row("token:1a2b3c4d", kind="token", five=(1, None))

    def test_no_tag_with_one_account(self):
        s = snap([self.L], sessions={"s1": "login:a"})
        self.assertEqual(usage.row_tag(s, "s1"), "")
        self.assertEqual(usage.row_tag(s, "s2"), "")

    def test_tags_with_two_accounts_and_question_mark_without_reading(self):
        s = snap([self.L, self.T], sessions={"s1": "login:a", "s2": "token:1a2b3c4d"})
        self.assertEqual(usage.row_tag(s, "s1"), "lukaso")
        self.assertEqual(usage.row_tag(s, "s2"), "1a2b")
        self.assertEqual(usage.row_tag(s, "s3"), "?")

    def test_brand_on_the_tag_only_with_two_brands(self):
        o = dict(acct_row("oai:x", kind="token", five=(1, None)), brand="oai")
        s = snap([self.L, o], sessions={"s1": "login:a"})
        self.assertEqual(usage.row_tag(s, "s1"), "ant lukaso")

    def test_no_snapshot_no_tag(self):
        self.assertEqual(usage.row_tag(None, "s1"), "")
        self.assertEqual(usage.row_tag({"state": "unknown"}, "s1"), "")


class TestStatusBar(unittest.TestCase):
    def test_own_entry_in_ansi(self):
        rec = reading("login:a", five=(42, NOW + 0.4 * H5), week=(2, NOW + 0.7 * D7),
                      measured=NOW - 5, email="lukaso@gmail.com")
        out = usage.status_text(rec, NOW)
        plain = usage.ANSI_RE.sub("", out)
        # owner, design review of the built list: "ant is sort of assumed" in the
        # status bar - a brand shows there only when it is not Anthropic
        self.assertTrue(plain.startswith("lukaso 5h 42%↓/60% ↻"), plain)
        self.assertIn("\033[1;32m↓\033[0m", out)

    def test_unknown_or_unsure_account_says_question_mark(self):
        rec = reading(None, five=(5, None), kind="unknown")
        self.assertTrue(usage.ANSI_RE.sub("", usage.status_text(rec, NOW)).startswith("? 5h 5%"))
        rec = reading("login:a", five=(5, None), unsure=True)
        self.assertTrue(usage.ANSI_RE.sub("", usage.status_text(rec, NOW)).startswith("? "))

    def test_nothing_without_a_reading(self):
        self.assertEqual(usage.status_text(None, NOW), "")


class TestShownLog(unittest.TestCase):
    def test_one_line_per_change_and_bounded(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "usage-shown.jsonl")
        s = snap([acct_row("login:a", email="a@x.com", five=(5, NOW + 100))])
        key = usage.append_shown(path, usage.shown_records(s), None, now=NOW)
        key2 = usage.append_shown(path, usage.shown_records(s), key, now=NOW + 1)
        self.assertEqual(key, key2)
        with open(path) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        for k in ("t", "account", "window", "used", "elapsed", "age", "arrow"):
            self.assertIn(k, rec)
        for i in range(30):
            s = snap([acct_row("login:a", email="a@x.com", five=(i, NOW + 100))])
            key = usage.append_shown(path, usage.shown_records(s), key, now=NOW, keep=10)
        with open(path) as fh:
            self.assertLessEqual(len(fh.read().splitlines()), 20)

    def test_a_write_that_fails_is_silent(self):
        s = snap([acct_row("login:a", email="a@x.com", five=(5, NOW + 100))])
        self.assertIsNotNone(usage.append_shown("/nonexistent/dir/x.jsonl",
                                                usage.shown_records(s), None, now=NOW))


class TestReviewRoundOne(unittest.TestCase):
    """Findings of the first review of slice C, each red before its fix."""

    def test_a_label_equal_to_another_accounts_name_is_made_unique(self):
        rows = [acct_row("login:u1", email="lukaso@gmail.com"),
                acct_row("token:1a2b3c4d", kind="token")]
        names = usage.short_names(rows, {"token:1a2b3c4d": "lukaso"})
        self.assertEqual(len(set(names.values())), 2, names)
        self.assertEqual(names["login:u1"], "lukaso")

    def test_the_arrow_compares_the_numbers_shown(self):
        self.assertEqual(usage.pace_arrow(60.4, 60), ("↓", "green"))
        self.assertEqual(usage.pace_arrow(60.6, 60), ("↑", "red"))

    def test_the_shown_log_does_not_repeat_across_processes(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "usage-shown.jsonl")
        s = snap([acct_row("login:a", email="a@x.com", five=(5, NOW + 100))])
        usage.append_shown(path, usage.shown_records(s), None, now=NOW)
        usage.append_shown(path, usage.shown_records(s), None, now=NOW + 5)  # a second process
        with open(path) as fh:
            self.assertEqual(len(fh.read().splitlines()), 1)

    def test_the_shown_log_does_not_read_the_file_to_append(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "usage-shown.jsonl")
        import builtins
        real, reads = builtins.open, []
        def spy(file, mode="r", *a, **k):
            if str(file) == path and "r" in mode and "+" not in mode:
                reads.append(1)
            return real(file, mode, *a, **k)
        builtins.open = spy
        try:
            for i in range(3):
                s = snap([acct_row("login:a", email="a@x.com", five=(i, NOW + 100))])
                usage.append_shown(path, usage.shown_records(s), None, now=NOW)
        finally:
            builtins.open = real
        self.assertEqual(reads, [])

    def test_the_status_bar_uses_the_names_the_list_used(self):
        rec = reading("login:a", five=(42, NOW + 0.4 * H5), measured=NOW - 5,
                      email="lukaso@gmail.com")
        out = usage.status_text(rec, NOW, names={"login:a": "lukaso@gmail"})
        self.assertTrue(usage.ANSI_RE.sub("", out).startswith("lukaso@gmail 5h"), out)

    def test_snapshot_takes_facts_lazily(self):
        called = []
        r = reading("login:a", five=(5, FIVE_RESET), sid="s1")
        usage.snapshot([r], NOW, {"s1"}, lambda: called.append(1) or {"usage_roots": []})
        self.assertEqual(called, [])
        usage.snapshot([], NOW, {"s1"}, lambda: called.append(1) or {"usage_roots": []})
        self.assertEqual(called, [1])


class TestShownLogAcrossLongRunningProcesses(unittest.TestCase):
    def test_each_caller_keeps_its_own_last_and_still_writes_once(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "usage-shown.jsonl")
        s1 = snap([acct_row("login:a", email="a@x.com", five=(1, NOW + 100))])
        s2 = snap([acct_row("login:a", email="a@x.com", five=(2, NOW + 100))])
        a = usage.append_shown(path, usage.shown_records(s1), None, now=NOW)   # list: K1
        b = usage.append_shown(path, usage.shown_records(s1), None, now=NOW)   # watch: K1
        b = usage.append_shown(path, usage.shown_records(s2), b, now=NOW)      # watch: K2
        a = usage.append_shown(path, usage.shown_records(s2), a, now=NOW)      # list: K2 again
        with open(path) as fh:
            self.assertEqual(len(fh.read().splitlines()), 2)
