"""Tests for ccwho_usage: subscription usage, read from each session's statusLine.

Claude Code hands a statusLine command `rate_limits` for the account the session
spends. ccwho records what it is given and never logs in, never calls the API and
never stores a credential. The fake token below must never come back out.
"""
import glob
import hashlib
import json
import os
import shutil
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


FP = hashlib.sha256(TOKEN.encode()).hexdigest()[:16]
PID = 52715


class TestTheClaudeProcessNamesTheAccount(unittest.TestCase):
    """#28: Claude Code gives the statusLine no CLAUDE_CODE_OAUTH_TOKEN (measured
    2026-09-28, 2.1.283). The claude that runs it has one, and says which account
    the session spends; ccwho is handed that claude's pid and its auth facts."""

    def setUp(self):
        self.h = Home()

    def rec(self, env=None, previous=None, now=NOW, pid=None, auth=None, five=5):
        return usage.record(self.h.payload(limits(five)), env or {}, self.h.dir, now,
                            previous, claude_pid=pid, claude_auth=auth)

    def test_the_claudes_token_names_a_token_session(self):
        r = self.rec(pid=PID, auth={"token_fp": FP})
        self.assertEqual(r["account"], {"kind": "token", "id": "token:" + FP})
        self.assertNotIn("a@example.com", json.dumps(r))

    def test_without_the_claudes_auth_the_login_is_read_as_before(self):     # control
        self.assertEqual(self.rec(pid=PID, auth={})["account"]["id"], "login:uuid-a")
        self.assertEqual(self.rec()["account"]["id"], "login:uuid-a")

    def test_a_token_in_the_own_env_still_wins(self):
        r = self.rec(env={"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}, pid=PID,
                     auth={"token_fp": "0" * 16})
        self.assertEqual(r["account"]["id"], "token:" + FP)

    def test_the_claudes_config_dir_is_where_its_login_is_read(self):
        other = tempfile.mkdtemp()
        self.h.login("b@example.com", "uuid-b", config_dir=other)
        r = self.rec(pid=PID, auth={"config_dir": other})
        self.assertEqual(r["account"]["id"], "login:uuid-b")

    def test_the_own_config_dir_still_wins(self):
        other, third = tempfile.mkdtemp(), tempfile.mkdtemp()
        self.h.login("b@example.com", "uuid-b", config_dir=other)
        self.h.login("c@example.com", "uuid-c", config_dir=third)
        r = self.rec(env={"CLAUDE_CONFIG_DIR": other}, pid=PID, auth={"config_dir": third})
        self.assertEqual(r["account"]["id"], "login:uuid-b")

    def test_the_claude_pid_is_recorded(self):
        self.assertEqual(self.rec(pid=PID, auth={})["claude_pid"], PID)
        self.assertIsNone(self.rec()["claude_pid"])

    def test_the_same_claude_keeps_its_first_account(self):                  # control
        first = self.rec(pid=PID, auth={})
        self.h.login("b@example.com", "uuid-b")
        again = self.rec(previous=first, now=NOW + 60, pid=PID, auth={})
        self.assertTrue(again["unsure"])
        self.assertEqual(again["first_account"]["id"], "login:uuid-a")

    def test_a_new_claude_for_the_session_starts_its_account_again(self):
        """--resume in a new process with a token: the credential is read afresh."""
        first = self.rec(pid=PID, auth={})
        again = self.rec(previous=first, now=NOW + 60, pid=PID + 1, auth={"token_fp": FP})
        self.assertFalse(again["unsure"])
        self.assertEqual(again["first_account"]["id"], "token:" + FP)
        self.assertEqual(again["first_seen"], NOW + 60)

    def test_a_reading_from_before_the_fix_takes_the_claudes_account(self):
        """The readings on disk named a token session's first account from the
        login: they carry no claude_pid, and must not keep that account."""
        old = self.rec()
        del old["claude_pid"]
        again = self.rec(previous=old, now=NOW + 60, pid=PID, auth={"token_fp": FP})
        self.assertFalse(again["unsure"])
        self.assertEqual(again["first_account"]["id"], "token:" + FP)

    def test_a_login_reading_from_before_the_fix_keeps_its_account(self):
        """Only the token was missed before #28. A login session whose login was
        changed under it stays unsure: its claude still spends the old login."""
        old = self.rec()
        del old["claude_pid"]
        self.h.login("b@example.com", "uuid-b")
        again = self.rec(previous=old, now=NOW + 60, pid=PID, auth={})
        self.assertTrue(again["unsure"])
        self.assertEqual(again["first_account"]["id"], "login:uuid-a")
        self.assertEqual(again["claude_pid"], PID)
        # and from now on its pid is known: a later new claude starts again
        later = self.rec(previous=again, now=NOW + 120, pid=PID + 1, auth={})
        self.assertFalse(later["unsure"])
        self.assertEqual(later["first_account"]["id"], "login:uuid-b")

    def test_no_claude_before_and_none_now_is_the_same_session(self):        # control
        first = self.rec()
        self.h.login("b@example.com", "uuid-b")
        again = self.rec(previous=first, now=NOW + 60)
        self.assertTrue(again["unsure"])

    def test_a_claude_that_can_no_longer_be_confirmed_is_not_a_new_one(self):
        """A reading without a pid (the parent could not be confirmed this time)
        is no evidence of a new process, nor of a switch: the account is unknown
        for that reading, which joins no account (review 3: a lasting gap would put
        another account's numbers under this one). The first account is kept."""
        first = self.rec(pid=PID, auth={"token_fp": FP})
        again = self.rec(previous=first, now=NOW + 60)
        self.assertEqual(again["first_account"]["id"], "token:" + FP)
        self.assertEqual(again["claude_pid"], PID)
        self.assertTrue(again["unsure"])
        self.assertEqual(again["account"], {"kind": "unknown", "id": None})

    def test_the_same_claude_read_without_its_auth_keeps_its_account(self):
        first = self.rec(pid=PID, auth={"token_fp": FP})
        again = self.rec(previous=first, now=NOW + 60, pid=PID, auth=None)
        self.assertTrue(again["unsure"])
        self.assertEqual(again["first_account"]["id"], "token:" + FP)
        self.assertEqual(again["account"], {"kind": "unknown", "id": None})

    def test_the_same_claude_read_with_no_token_is_a_switch(self):           # control
        """A readable environment without the token is evidence, not a gap."""
        first = self.rec(pid=PID, auth={"token_fp": FP})
        again = self.rec(previous=first, now=NOW + 60, pid=PID, auth={})
        self.assertTrue(again["unsure"])
        self.assertEqual(again["account"]["id"], "login:uuid-a")

    def test_a_claude_counts_only_when_its_environment_was_read(self):
        """A pid whose environment could not be read confirms nothing: it is not
        stored, not a new process, and not a switch."""
        first = self.rec(pid=PID, auth={"token_fp": FP})
        again = self.rec(previous=first, now=NOW + 60, pid=PID + 1, auth=None)
        self.assertEqual(again["account"], {"kind": "unknown", "id": None})
        self.assertEqual(again["first_account"]["id"], "token:" + FP)
        self.assertEqual(again["claude_pid"], PID)
        self.assertTrue(again["unsure"])

    def test_a_first_read_that_fails_stores_no_pid(self):
        """Else the login becomes the first account of a token session's claude,
        and every later read that does see the token is unsure."""
        a = self.rec(pid=PID, auth=None)
        self.assertEqual(a["account"]["id"], "login:uuid-a")                # as before
        self.assertIsNone(a["claude_pid"])
        b = self.rec(previous=a, now=NOW + 60, pid=PID, auth={"token_fp": FP})
        self.assertFalse(b["unsure"])
        self.assertEqual(b["first_account"]["id"], "token:" + FP)

    def test_a_pid_that_is_not_a_pid_is_no_claude(self):
        first = self.rec(pid=PID, auth={"token_fp": FP})
        for bad in (0, -1, 1, True, "52715", 10 ** 8):
            again = self.rec(previous=first, now=NOW + 60, pid=bad, auth=None)
            self.assertEqual(again["account"], {"kind": "unknown", "id": None}, repr(bad))
            self.assertTrue(again["unsure"], repr(bad))
            self.assertEqual(again["claude_pid"], PID, repr(bad))
            fresh = self.rec(pid=bad, auth={"token_fp": FP})
            self.assertIsNone(fresh["claude_pid"], repr(bad))
            # the auth of an unconfirmed claude is never used
            self.assertEqual(fresh["account"]["id"], "login:uuid-a", repr(bad))

    def test_the_own_env_token_still_wins_in_a_gap(self):
        first = self.rec(pid=PID, auth={"token_fp": "0" * 16})
        again = self.rec(env={"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}, previous=first,
                         now=NOW + 60, pid=PID, auth=None)
        self.assertEqual(again["account"]["id"], "token:" + FP)

    def test_a_gap_joins_no_account_and_the_next_reading_joins_again(self):
        first = self.rec(pid=PID, auth={"token_fp": FP})
        gap = self.rec(previous=first, now=NOW + 60, pid=PID, auth=None, five=70)
        self.assertEqual(usage.accounts([gap], NOW + 60), [])
        self.assertEqual(usage.session_accounts([gap]), {})
        back = self.rec(previous=gap, now=NOW + 120, pid=PID, auth={"token_fp": FP})  # control
        self.assertFalse(back["unsure"])
        self.assertEqual(back["first_account"]["id"], "token:" + FP)
        self.assertEqual(back["first_seen"], NOW)
        self.assertEqual([r["id"] for r in usage.accounts([back], NOW + 120)], ["token:" + FP])

    def test_an_unsure_session_stays_out_through_a_gap(self):
        first = self.rec(pid=PID, auth={"token_fp": FP})
        mid = self.rec(previous=first, now=NOW + 60, pid=PID, auth={})
        self.assertTrue(mid["unsure"])
        gap = self.rec(previous=mid, now=NOW + 120, pid=PID, auth=None)
        self.assertTrue(gap["unsure"])
        self.assertEqual(usage.accounts([gap], NOW + 120), [])

    def test_a_stored_pid_that_is_not_a_pid_is_no_claude(self):
        for bad in ("52715", True, 0, -3, 10 ** 8, None):
            old = self.rec()
            old["claude_pid"] = bad
            again = self.rec(previous=old, now=NOW + 60)
            self.assertEqual(again["account"]["id"], "login:uuid-a", repr(bad))
            self.assertIsNone(again["claude_pid"], repr(bad))
            self.assertFalse(again["unsure"], repr(bad))

    def test_a_token_session_never_confirmed_keeps_its_first_seen(self):
        """The migration is for readings from before #28 only - those carry no
        claude_pid key. A token read from the own env, never confirmed, is not one."""
        env = {"CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
        a = self.rec(env=env)
        b = self.rec(env=env, previous=a, now=NOW + 60)
        self.assertEqual(b["first_seen"], NOW)
        self.assertEqual(b["first_account"]["id"], "token:" + FP)


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

    def test_older_than_two_weeks_is_ignored(self):
        # owner, 2026-10-03: an account is kept two weeks after its last
        # reading - past a weekly reset, and not forever
        self.put("old.json", reading("login:a", five=(5, FIVE_RESET),
                                     received=NOW - 14 * 86400 - 60))
        self.put("new.json", reading("login:b", five=(5, FIVE_RESET), sid="b",
                                     received=NOW - 14 * 86400 + 60))       # control
        self.assertEqual([r["session_id"] for r in usage.load_readings(self.dir, NOW)], ["b"])

    def test_temp_files_and_other_names_are_not_readings(self):
        self.put(SID + ".json.123.tmp", reading("login:a", five=(5, FIVE_RESET)))
        self.put("notes.txt", "x")
        self.assertEqual(usage.load_readings(self.dir, NOW), [])

    def test_missing_dir_is_no_readings(self):
        self.assertEqual(usage.load_readings(os.path.join(self.dir, "nope"), NOW), [])

    def test_prune_names_files_older_than_two_weeks_only(self):
        self.put("old.json", "{}", age=14 * 86400 + 60)
        self.put("new.json", "{}", age=60)
        self.put("week.json", "{}", age=13 * 86400)                       # control
        self.put("old.json.1.tmp", "{}", age=14 * 86400 + 60)
        self.assertEqual(sorted(usage.prune(self.dir, NOW)), ["old.json", "old.json.1.tmp"])


class TestEveryIdAQueryNames(unittest.TestCase):
    """`ccwho usage name` tells no account (1) from several (2): matching_ids
    gives them all (review 3 of the CLI revamp, slice 3b)."""

    def test_an_exact_id_is_that_one_alone(self):
        # though it is also the start of another
        self.assertEqual(usage.matching_ids("token:abc", ["token:abc", "token:abcd"]),
                         ["token:abc"])

    def test_no_query_names_none(self):
        self.assertEqual(usage.matching_ids("", ["login:a"]), [])

    def test_a_start_names_every_id_it_starts(self):                       # control
        self.assertEqual(usage.matching_ids("token:ab", ["token:abc", "token:abd", "login:x"]),
                         ["token:abc", "token:abd"])


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


class TestACopiedTagNamesTheAccount(unittest.TestCase):
    def test_the_brand_of_a_tag_is_no_part_of_the_id(self):
        # "ant:d0a0" is what a row says: `ccwho usage name ant:d0a0 work`
        ids = ["login:a", "token:d0a0beef"]
        self.assertEqual(usage.resolve_id("ant:d0a0", ids), "token:d0a0beef")
        self.assertEqual(usage.resolve_id("d0a0", ids), "token:d0a0beef")      # control
        self.assertIsNone(usage.resolve_id("ant:", ids))
        self.assertIsNone(usage.resolve_id("ant:", ["login:a"]))          # a brand is no id
        self.assertEqual(usage.resolve_id("token:d0a0beef", ids), "token:d0a0beef")


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

    def test_a_reset_more_than_a_day_ago_says_its_day(self):
        row = {"id": "x", "label": "x", "sessions": 1, "age": 4 * 86400,
               "five_hour": {"state": "expired", "resets_at": NOW - 3 * 86400},
               "seven_day": {"state": "expired", "resets_at": NOW - 60}}
        line = usage.format_row(row, NOW)
        day = _time.strftime("%a %H:%M", _time.localtime(NOW - 3 * 86400))
        self.assertIn(f"5h expired (reset {day})", line)
        self.assertIn("7d expired (reset " + _time.strftime("%H:%M", _time.localtime(NOW - 60))
                      + ")", line)                                         # control

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

    def test_every_account_with_a_reading_live_or_not(self):
        # owner, 2026-10-03: "I want to know what's being used on all my
        # accounts since I switch between them" - an account no live session
        # spends stays, with its age (it was dropped: E-D6)
        a = reading("login:a", five=(5, FIVE_RESET), sid="s1")
        b = reading("login:b", five=(5, FIVE_RESET), sid="s2", email="b@x.com",
                    measured=NOW - 2 * 86400)
        s = usage.snapshot([a, b], NOW, live_ids={"s1"}, facts=self.FACTS_OURS)
        self.assertEqual([r["id"] for r in s["accounts"]], ["login:a", "login:b"])
        self.assertEqual(s["sessions"], {"s1": "login:a"})   # a tag names a live row only
        self.assertEqual(s["state"], "ok")
        self.assertIn("(2d ago)", text(usage.usage_lines(s, 200))[0])

    def test_no_live_session_at_all_still_shows_them(self):
        a = reading("login:a", five=(5, FIVE_RESET), sid="s1")
        for facts in (self.FACTS_OURS, {"usage_roots": []}):
            s = usage.snapshot([a], NOW, live_ids=set(), facts=facts)
            self.assertEqual((s["state"], [r["id"] for r in s["accounts"]]), ("ok", ["login:a"]))

    def test_a_dir_that_kept_ccwhos_statusline_is_not_off(self):
        # "Remove ccwho's statusLine?" answered no: setup and doctor say
        # "still on", and so does the line
        b = reading("login:b", five=(5, FIVE_RESET), sid="s2", measured=NOW - 3 * 86400)
        kept = {"usage_roots": [{"root": "/h/.claude", "state": "ours", "opted_out": True}]}
        s = usage.snapshot([b], NOW, live_ids=set(), facts=kept)
        self.assertEqual(s["state"], "ok")
        self.assertTrue(usage.usage_lines(s, 200))
        gone = {"usage_roots": [{"root": "/h/.claude", "state": "missing", "opted_out": True}]}
        self.assertEqual(usage.snapshot([b], NOW, set(), gone)["state"], "off")    # control

    def test_opted_out_everywhere_is_no_line_kept_readings_or_not(self):
        # the owner's D3: "Opted out everywhere: no line at all" - a reading
        # kept two weeks must not outlive `ccwho setup --no-usage` (review
        # 2026-10-03); Codex's entry goes with it: usage info is off
        b = reading("login:b", five=(5, FIVE_RESET), sid="s2", measured=NOW - 3 * 86400)
        off = {"usage_roots": [{"root": "/h/.claude", "state": "missing", "opted_out": True}]}
        codex = [dict(acct_row("codex:codex", kind="codex", week=(65, NOW + D7)), brand="oai")]
        a = reading("login:a", five=(5, FIVE_RESET), sid="s1", measured=NOW - 86400)
        for kw in ({}, {"codex": codex}):
            s = usage.snapshot([a, b], NOW, live_ids=set(), facts=off, **kw)
            self.assertEqual(s["state"], "off", kw)
            self.assertEqual(usage.usage_lines(s, 200), [])
            # off carries no account: nothing to tag, log or name (review 2026-10-03)
            self.assertEqual((s["accounts"], s["names"]), ([], {}))
            self.assertEqual((usage.row_tag(s, "s2"), usage.row_tag(s, "s9")), ("", ""))
            self.assertEqual(usage.shown_records(s), [])
        s = usage.snapshot([b], NOW, live_ids=set(), facts=self.FACTS_OURS)     # control
        self.assertEqual(s["state"], "ok")
        # a live session's account is shown without a settings read (E-D8)
        s = usage.snapshot([b], NOW, live_ids={"s2"}, facts=lambda: self.fail("no facts"))
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
        self.assertTrue(line.startswith("usage  ant:lukaso 5h 42%↓/60% ↻"), line)
        self.assertIn("· 7d 2%↓/30% ↻", line)

    def test_two_accounts_fit_on_one_line_separated(self):
        line, = text(usage.usage_lines(snap([self.L, self.T]), 200))
        self.assertIn("  │  ant:1a2b 5h 0% · 7d 67%↑/40%", line)

    def test_they_stack_when_they_do_not_fit_one_per_account(self):
        lines = text(usage.usage_lines(snap([self.L, self.T]), 90))
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("usage  ant:lukaso "))
        self.assertTrue(lines[1].startswith("       ant:1a2b "))

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
        self.assertIn("ant:", no_age)
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

    def test_an_entry_whose_windows_all_expired_still_says_its_age(self):
        # what an old account most often reads like: both windows past their
        # reset - the age is the one thing left that says when it was used
        r = acct_row("token:1a2b3c4d", kind="token", five="expired", week="expired",
                     age=3 * 86400)
        line = text(usage.usage_lines(snap([r]), 200))[0]
        self.assertTrue(line.endswith(" (3d ago)"), line)
        self.assertEqual(line.count("ago"), 1)
        # two windows alike are still two names
        self.assertTrue(line.startswith("usage  ant:1a2b 5h expired ↻"), line)
        self.assertIn(" · 7d expired ↻", line)

    def test_the_newest_window_gives_an_all_expired_entry_its_age(self):
        r = acct_row("token:1a2b3c4d", kind="token", five="expired", week="expired",
                     five_age=2 * 3600, week_age=3 * 86400)
        self.assertTrue(text(usage.usage_lines(snap([r]), 200))[0].endswith(" (2h ago)"))

    def test_a_window_that_says_its_age_says_it_once(self):                 # control
        r = acct_row("login:a", email="lukaso@gmail.com", five="expired",
                     week=(2, NOW + 0.7 * D7), age=2 * 3600)
        line = text(usage.usage_lines(snap([r]), 200))[0]
        self.assertEqual(line.count("ago"), 1)
        self.assertTrue(line.endswith("(2h ago)"), line)
        fresh = acct_row("login:a", email="lukaso@gmail.com", five="expired",
                         week="expired", age=60)
        self.assertNotIn("ago", text(usage.usage_lines(snap([fresh]), 200))[0])

    def test_an_all_expired_entry_drops_its_age_where_the_others_do(self):
        r = acct_row("token:1a2b3c4d", kind="token", five="expired", week="expired",
                     age=3 * 86400)
        full = text(usage.usage_lines(snap([r]), 200))[0]
        no_reset = text(usage.usage_lines(snap([r]), len(full) - 1))[0]
        self.assertNotIn("↻", no_reset)
        self.assertIn("(3d ago)", no_reset)
        no_age = text(usage.usage_lines(snap([r]), len(no_reset) - 1))[0]
        self.assertNotIn("ago", no_age)
        self.assertIn("ant:", no_age)

    def test_a_reset_more_than_a_day_ago_says_its_day(self):
        # kept two weeks, a window can be days past its reset: "↻21:00" would
        # read as today's
        long_ago = NOW - 3 * 86400
        r = acct_row("token:1a2b3c4d", kind="token", five="expired", week="expired",
                     age=4 * 86400)
        r["seven_day"]["resets_at"] = long_ago
        line = text(usage.usage_lines(snap([r]), 200))[0]
        self.assertIn("7d expired ↻" + _time.strftime("%a", _time.localtime(long_ago)), line)
        self.assertIn("5h expired ↻" + _time.strftime("%H:%M", _time.localtime(NOW - 60)),
                      line)                                                # control

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
        self.assertIn("ant:1a2b", bright)
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
        # the name the usage line gives the account, brand and all (owner,
        # 2026-10-01): "· ant:lukaso" on the row is the "ant:lukaso" at the top
        self.assertEqual(usage.row_tag(s, "s1"), "ant:lukaso")
        self.assertEqual(usage.row_tag(s, "s2"), "ant:1a2b")
        self.assertEqual(usage.row_tag(s, "s3"), "?")

    def test_a_brand_with_one_account_tags_no_row(self):
        # the owner's D25: one Claude account and Codex on the line - "(codex)"
        # already tells the rows apart; a tag names one of a brand's accounts
        o = dict(acct_row("codex:codex", kind="codex", week=(65, NOW + D7)), brand="oai",
                 email="")
        s = snap([self.L, o], sessions={"s1": "login:a"})
        self.assertEqual(usage.row_tag(s, "s1"), "")
        self.assertEqual(usage.row_tag(s, "s3"), "")                 # no reading: no tag
        s = snap([self.L, self.T, o], sessions={"s1": "login:a", "s2": "token:1a2b3c4d"})
        self.assertEqual(usage.row_tag(s, "s1"), "ant:lukaso")             # control
        self.assertEqual(usage.row_tag(s, "s3"), "?")

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


# ------------------------------------------------- D20: Codex usage, passive
import time as _time

CODEX_RESET = NOW + 72 * 3600
T1 = "019a8f2c-0000-7000-8000-00000000000a"
T2 = "019a8f2c-0000-7000-8000-00000000000b"


def token_count(epoch, used=65.0, minutes=10080, resets=CODEX_RESET, limit="codex",
                secondary=None, rate_limits="default"):
    """A token_count event as Codex 0.154 writes it (measured 2026-10-01)."""
    rl = {"limit_id": limit, "limit_name": None, "plan_type": "prolite",
          "primary": ({"used_percent": used, "window_minutes": minutes, "resets_at": resets}
                      if used is not None else None),
          "secondary": secondary,
          "credits": {"balance": "7", "has_credits": True, "unlimited": False},
          "individual_limit": None, "rate_limit_reached_type": None,
          "spend_control_reached": None}
    return {"timestamp": iso(epoch), "type": "event_msg",
            "payload": {"type": "token_count", "info": None,
                        "rate_limits": rl if rate_limits == "default" else rate_limits}}


class CodexHome:
    """A fake $CODEX_HOME: rollouts under sessions/YYYY/MM/DD, as Codex writes them."""

    def __init__(self):
        self.dir = tempfile.mkdtemp()

    def rollout(self, events, when=NOW, thread=T1, pad=0, day=None):
        t = _time.localtime(when)
        d = os.path.join(self.dir, "sessions",
                         _time.strftime("%Y/%m/%d", _time.localtime(day if day else when)))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"rollout-{_time.strftime('%Y-%m-%dT%H-%M-%S', t)}-{thread}.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "session_meta",
                                 "payload": {"id": thread, "source": "vscode"}}) + "\n")
            for e in events:
                fh.write(json.dumps(e) + "\n")
            for _ in range(pad):          # what a turn writes after: no token_count
                fh.write(json.dumps({"type": "response_item", "payload": {
                    "type": "message", "content": "x" * 1000}}) + "\n")
        os.utime(path, (when, when))
        return path


class TestCodexUsage(unittest.TestCase):
    """The owner's D20: Codex usage from the newest rate_limits its transcripts
    hold - no app-server, no credential. One row per limit (limit_id)."""

    def setUp(self):
        self.home = CodexHome()
        self.addCleanup(shutil.rmtree, self.home.dir, True)

    def rows(self, cache=None, now=NOW, threads=()):
        return usage.codex_rows(self.home.dir, now, cache=cache, threads=threads)

    def test_the_newest_reading_is_the_row(self):
        self.home.rollout([token_count(NOW - 3600, used=40.0)], when=NOW - 3600, thread=T1)
        self.home.rollout([token_count(NOW - 60, used=65.0)], when=NOW - 60, thread=T2)
        row, = self.rows()
        self.assertEqual((row["id"], row["kind"], row["brand"]), ("codex:codex", "codex", "oai"))
        self.assertEqual(row["seven_day"],
                         {"state": "ok", "pct": 65.0, "resets_at": CODEX_RESET, "age": 60})
        self.assertIsNone(row["five_hour"])
        self.assertEqual(row["age"], 60)

    def test_sessions_counts_the_rollouts_that_said_so(self):
        self.home.rollout([token_count(NOW - 600)], when=NOW - 600, thread=T1)
        self.home.rollout([token_count(NOW - 60)], when=NOW - 60, thread=T2)
        self.assertEqual(self.rows()[0]["sessions"], 2)

    def test_a_row_names_the_open_threads_that_spend_it(self):
        # the list brightens the entry a highlighted Codex row spends: an open
        # thread spends the limit its rollout's last reading names
        self.home.rollout([token_count(NOW - 120, limit="premium"), token_count(NOW - 60)],
                          when=NOW - 60, thread=T1)
        self.home.rollout([token_count(NOW - 120), token_count(NOW - 60, limit="premium")],
                          when=NOW - 60, thread=T2)
        threads = lambda rows: {r["id"]: r["threads"] for r in rows}
        self.assertEqual(threads(self.rows(threads=[T1, T2])),
                         {"codex:codex": [T1], "codex:premium": [T2]})
        # a thread not open spends nothing on the list (control: read all the same)
        self.assertEqual(threads(self.rows(threads=[T1])),
                         {"codex:codex": [T1], "codex:premium": []})

    def test_the_limit_written_last_is_the_one_spent(self):
        # a later line is a later reading - also at one time: a rollout that
        # starts with a copied history has all of it in one second (measured:
        # rollouts of 2026-09-03)
        threads = lambda: {r["id"]: r["threads"] for r in self.rows(threads=[T1])}
        self.home.rollout([token_count(NOW - 300), token_count(NOW - 200, limit="premium"),
                           token_count(NOW - 100)], when=NOW - 60, thread=T1)
        self.assertEqual(threads(), {"codex:codex": [T1], "codex:premium": []})
        for order in (["codex", "premium"], ["premium", "codex"],
                      ["codex", "premium", "codex"]):
            self.home.rollout([token_count(NOW - 60, limit=limit) for limit in order],
                              when=NOW - 60, thread=T1)
            last, other = order[-1], ({"codex", "premium"} - {order[-1]}).pop()
            self.assertEqual((threads()[f"codex:{last}"], threads()[f"codex:{other}"]),
                             ([T1], []), order)

    def test_a_limit_with_no_window_is_never_the_one_spent(self):
        # it has no entry to brighten: Codex writes "premium" with no window
        # after its own limit, in the same second (measured: rollouts of 2026-09-03)
        premium = lambda when: token_count(when, used=None, limit="premium")
        for events in ([token_count(NOW - 60), premium(NOW - 60)],
                       [premium(NOW - 60), token_count(NOW - 60)],
                       [token_count(NOW - 120), premium(NOW - 60)]):
            self.home.rollout(events, when=NOW - 60, thread=T1)
            self.assertEqual({r["id"]: r["threads"] for r in self.rows(threads=[T1])},
                             {"codex:codex": [T1]}, events)

    def test_the_open_threads_may_come_as_any_iterable(self):
        # read twice: to find an old folder's rollout, and to name who spends it
        self.home.rollout([token_count(NOW - 60)], when=NOW - 60, day=NOW - 20 * 86400)
        self.assertEqual([(r["id"], r["threads"]) for r in self.rows(threads=iter([T1]))],
                         [("codex:codex", [T1])])

    def test_the_last_reading_in_a_file_wins(self):
        self.home.rollout([token_count(NOW - 120, used=40.0), token_count(NOW - 60, used=65.0)],
                          when=NOW - 60)
        self.assertEqual(self.rows()[0]["seven_day"]["pct"], 65.0)

    def test_each_limit_is_its_own_row_and_one_without_a_window_is_none(self):
        self.home.rollout([token_count(NOW - 60)], when=NOW - 60, thread=T1)
        self.home.rollout([token_count(NOW - 30, used=None, limit="premium")], when=NOW - 30,
                          thread=T2)
        self.assertEqual([r["id"] for r in self.rows()], ["codex:codex"])
        self.home.rollout([token_count(NOW - 10, used=3.0, limit="premium")], when=NOW - 10,
                          thread="019a8f2c-0000-7000-8000-00000000000c")
        self.assertEqual(sorted(r["id"] for r in self.rows()), ["codex:codex", "codex:premium"])

    def test_a_window_is_named_by_its_length(self):
        self.home.rollout([token_count(NOW - 60, used=10.0, minutes=300, resets=NOW + 3600,
                                       secondary={"used_percent": 30.0, "window_minutes": 10080,
                                                  "resets_at": CODEX_RESET})], when=NOW - 60)
        row, = self.rows()
        self.assertEqual(row["five_hour"]["pct"], 10.0)
        self.assertEqual(row["seven_day"]["pct"], 30.0)
        # a window of a length ccwho has no name for is not shown as one it
        # has: the newest reading holds none it can name - no row
        self.home.rollout([token_count(NOW - 5, used=10.0, minutes=60)], when=NOW - 5, thread=T2)
        self.assertEqual(self.rows(), [])

    def test_a_window_past_its_reset_is_expired_never_a_number(self):
        self.home.rollout([token_count(NOW - 60, resets=NOW - 10)], when=NOW - 60)
        self.assertEqual(self.rows()[0]["seven_day"],
                         {"state": "expired", "resets_at": NOW - 10, "age": 60})

    def test_a_reading_with_no_limits_is_no_reading(self):
        self.home.rollout([token_count(NOW - 600, used=20.0)], when=NOW - 600, thread=T1)
        self.home.rollout([token_count(NOW - 60, rate_limits=None)], when=NOW - 60, thread=T2)
        self.assertEqual(self.rows()[0]["seven_day"]["pct"], 20.0)

    def test_only_token_count_lines_are_parsed_and_only_numbers_kept(self):
        # what was said is never parsed: a line is read only when it holds
        # "token_count" - and of that, the windows' numbers are kept
        self.home.rollout([{"type": "response_item", "payload": {"type": "message",
                                                                 "content": "rate_limits 99%"}},
                           token_count(NOW - 60)], when=NOW - 60)
        parsed = []
        real = usage.json.loads
        usage.json.loads = lambda text, *a, **k: parsed.append(text) or real(text, *a, **k)
        try:
            row, = self.rows()
        finally:
            usage.json.loads = real
        self.assertEqual(len(parsed), 1)
        self.assertIn(b"token_count" if isinstance(parsed[0], bytes) else "token_count", parsed[0])
        self.assertNotIn("credits", json.dumps(row))
        self.assertNotIn("balance", json.dumps(row))

    def test_a_message_that_says_token_count_is_no_reading(self):
        self.home.rollout([{"type": "response_item", "payload": {
            "type": "message", "content": '{"type": "token_count", "rate_limits": 1}'}}],
            when=NOW - 60)
        self.assertEqual(self.rows(), [])
        # nor is any other event that carries a rate_limits of the right shape
        real = token_count(NOW - 30)
        other = {"timestamp": real["timestamp"], "type": "response_item",
                 "payload": dict(real["payload"], type="message", content="token_count")}
        self.home.rollout([other], when=NOW - 30, thread=T2)
        self.assertEqual(self.rows(), [])

    def test_an_odd_limit_or_no_time_is_no_reading(self):
        odd = token_count(NOW - 60, limit="co dex\x1b[31m")
        timeless = token_count(NOW - 50)
        del timeless["timestamp"]
        self.home.rollout([odd, timeless], when=NOW - 50)
        self.assertEqual(self.rows(), [])

    def test_a_rollout_gone_leaves_the_cache(self):
        keep = self.home.rollout([token_count(NOW - 60)], when=NOW - 60, thread=T1)
        gone = self.home.rollout([token_count(NOW - 30)], when=NOW - 30, thread=T2)
        cache = {}
        self.rows(cache)
        os.remove(gone)
        self.rows(cache)
        self.assertEqual(sorted(k for k in cache if k.startswith("_codex_usage:")),
                         [f"_codex_usage:{keep}"])

    def test_an_open_threads_rollout_in_an_old_folder_is_read(self):
        # a resumed thread writes on in the rollout of the day it began: an open
        # thread's rollout is found by its id, wherever its folder is
        self.home.rollout([token_count(NOW - 60)], when=NOW - 60, day=NOW - 20 * 86400)
        self.assertEqual([r["id"] for r in self.rows(threads=[T1])], ["codex:codex"])
        self.assertEqual(self.rows(), [])            # control: old folders are not listed

    def test_a_thread_id_that_is_no_id_finds_nothing(self):
        # an id is a glob pattern's part: "*" would read the whole history
        self.home.rollout([token_count(NOW - 60)], when=NOW - 60, day=NOW - 20 * 86400)
        self.assertEqual(self.rows(threads=["*", "../x", 7, None]), [])

    def test_an_open_threads_rollout_is_found_again_when_it_moves(self):
        path = self.home.rollout([token_count(NOW - 600, used=40.0)], when=NOW - 600,
                                 day=NOW - 20 * 86400)
        cache = {}
        self.assertEqual(self.rows(cache, threads=[T1])[0]["seven_day"]["pct"], 40.0)
        os.remove(path)
        self.home.rollout([token_count(NOW - 60, used=65.0)], when=NOW - 60,
                          day=NOW - 19 * 86400)
        self.assertEqual(self.rows(cache, threads=[T1])[0]["seven_day"]["pct"], 65.0)
        os.remove(glob.glob(os.path.join(self.home.dir, "sessions", "*", "*", "*",
                                         f"*{T1}.jsonl"))[0])
        self.rows(cache, threads=[])                  # closed and gone: no key kept
        self.assertEqual([k for k in cache if k.startswith("_codex_path:")], [])

    def test_a_closed_threads_rollout_in_an_old_folder_keeps_its_reading(self):
        # a resumed thread writes on in its first day's folder, past the ones a
        # scan lists: closed, its newest reading must not give way to an older
        # one - while the list runs, its path is kept as long as a reading is
        # (review 2026-10-03)
        old = self.home.rollout([token_count(NOW - 60, used=65.0)], when=NOW - 60,
                                day=NOW - 20 * 86400)
        self.home.rollout([token_count(NOW - 3 * 86400, used=40.0)], when=NOW - 3 * 86400,
                          thread=T2)
        cache = {}
        pct_age = lambda rows: (rows[0]["seven_day"]["pct"], rows[0]["age"])
        self.assertEqual(pct_age(self.rows(cache, threads=[T1])), (65.0, 60))
        self.assertEqual(pct_age(self.rows(cache, threads=[])), (65.0, 60))
        stamp = NOW - 15 * 86400                      # past the keep: the key goes
        os.utime(old, (stamp, stamp))
        self.assertEqual(pct_age(self.rows(cache, threads=[])), (40.0, 3 * 86400))
        self.assertEqual([k for k in cache if k.startswith("_codex_path:")], [])

    def test_a_kept_path_of_another_codex_home_is_not_read(self):
        path = self.home.rollout([token_count(NOW - 60)], when=NOW - 60, day=NOW - 20 * 86400)
        for other in ("/elsewhere/.codex", self.home.dir + ":x"):     # a home that only starts alike
            cache = {f"_codex_path:{other}:{T1}": path}
            self.assertEqual(self.rows(cache, threads=[]), [], other)
            self.assertEqual([k for k in cache if k.startswith("_codex_path:")], [])
        cache = {f"_codex_path:{self.home.dir}:{T1}": path}                    # control
        self.assertEqual(len(self.rows(cache, threads=[])), 1)

    def test_a_rollout_begun_15_days_ago_and_written_13_days_ago_is_read(self):
        # its folder is its first day's: the scan lists folders a day past the keep
        self.home.rollout([token_count(NOW - 13 * 86400)], when=NOW - 13 * 86400,
                          day=NOW - 15 * 86400)
        self.assertEqual(len(self.rows()), 1)

    def test_an_old_folder_with_an_old_file_is_not_read(self):              # control
        self.home.rollout([token_count(NOW - 15 * 86400)], when=NOW - 15 * 86400,
                          day=NOW - 20 * 86400)
        self.assertEqual(self.rows(threads=[T1]), [])

    def test_a_clock_change_skips_no_folder(self):
        # 2026-03-29 is 23 hours long in London: 24-hour steps back from 00:30
        # on the 30th skip its folder (review 2026-10-01)
        old = os.environ.get("TZ")
        os.environ["TZ"] = "Europe/London"
        _time.tzset()
        try:
            now = _time.mktime((2026, 3, 30, 0, 30, 0, 0, 0, -1))
            self.home.rollout([token_count(now - 60)], when=now - 60,
                              day=_time.mktime((2026, 3, 29, 12, 0, 0, 0, 0, -1)))
            self.assertEqual(len(usage.codex_rows(self.home.dir, now)), 1)
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            _time.tzset()

    def test_a_scan_reads_recent_folders_and_open_threads_only(self):
        # the history grows by thousands a year: a scan every 2 s must not
        # stat it all (review 2026-10-01: 86 ms at 15,000 rollouts)
        for i in range(30):
            self.home.rollout([token_count(NOW - (20 + i) * 86400)],
                              when=NOW - (20 + i) * 86400,
                              thread=f"019a8f2c-0000-7000-8000-0000000001{i:02d}")
        self.home.rollout([token_count(NOW - 120)], when=NOW - 120, thread=T2)
        self.home.rollout([token_count(NOW - 60)], when=NOW - 60, day=NOW - 17 * 86400)
        stats = []
        real = usage.os.stat
        usage.os.stat = lambda p, *a, **k: (stats.append(p) if str(p).endswith(".jsonl")
                                             else None) or real(p, *a, **k)
        try:
            row, = self.rows(threads=[T1])
        finally:
            usage.os.stat = real
        self.assertEqual(row["sessions"], 2)
        self.assertEqual(len(stats), 2, stats)

    def test_a_turn_that_writes_past_64_kb_keeps_its_reading(self):
        path = self.home.rollout([token_count(NOW - 600, used=65.0)], when=NOW - 600)
        cache = {}
        self.assertEqual(self.rows(cache)[0]["seven_day"]["pct"], 65.0)
        with open(path, "a") as fh:                   # the same file, grown
            for _ in range(80):
                fh.write(json.dumps({"type": "response_item", "payload": {
                    "type": "message", "content": "x" * 1000}}) + "\n")
        os.utime(path, (NOW - 30, NOW - 30))
        row, = self.rows(cache)
        self.assertEqual((row["seven_day"]["pct"], row["age"]), (65.0, 600))

    def test_a_replaced_or_shorter_file_is_read_as_new(self):
        path = self.home.rollout([token_count(NOW - 600, used=65.0)], when=NOW - 600)
        cache = {}
        self.assertEqual(len(self.rows(cache)), 1)
        new = path + ".new"                           # a new inode, no reading
        with open(new, "w") as fh:
            fh.write(json.dumps({"type": "session_meta", "payload": {}}) + "\n" + "x" * 900)
        os.replace(new, path)
        os.utime(path, (NOW - 30, NOW - 30))
        self.assertEqual(self.rows(cache), [])
        path = self.home.rollout([token_count(NOW - 600, used=65.0)], when=NOW - 600,
                                 thread=T2)
        self.assertEqual(len(self.rows(cache)), 1)
        with open(path, "r+") as fh:                  # the same inode, shorter
            fh.truncate(10)
        os.utime(path, (NOW - 20, NOW - 20))
        self.assertEqual(self.rows(cache), [])

    def test_a_malformed_reading_is_no_reading_and_no_error(self):
        good = token_count(NOW - 30)
        bad = [["token_count"], {"type": "event_msg", "payload": "token_count"},
               {"timestamp": good["timestamp"], "type": "event_msg", "payload": {
                   "type": "token_count", "rate_limits": {"limit_id": "codex", "primary": {
                       "used_percent": 5, "window_minutes": [10080], "resets_at": 1}}}},
               {"timestamp": good["timestamp"], "type": "event_msg", "payload": {
                   "type": "token_count", "rate_limits": ["token_count"]}},
               {"timestamp": 12, "type": "event_msg", "payload": {
                   "type": "token_count", "rate_limits": good["payload"]["rate_limits"]}}]
        self.home.rollout(bad, when=NOW - 40, thread=T1)
        self.assertEqual(self.rows(), [])
        self.home.rollout(bad + [good], when=NOW - 30, thread=T2)
        self.assertEqual(self.rows()[0]["seven_day"]["pct"], 65.0)

    def test_older_than_two_weeks_is_not_read(self):
        self.home.rollout([token_count(NOW - 15 * 86400)], when=NOW - 15 * 86400)
        self.assertEqual(self.rows(), [])

    def test_a_rollout_from_last_week_is_read_with_no_thread_open(self):
        # owner, 2026-10-03: Codex stays on the line two weeks too - its
        # folders are read that far back, a thread open or not
        self.home.rollout([token_count(NOW - 13 * 86400, resets=NOW - 6 * 86400)],
                          when=NOW - 13 * 86400)
        row, = self.rows()
        self.assertEqual(row["seven_day"]["state"], "expired")
        self.assertEqual(row["age"], 13 * 86400)

    def test_a_reading_far_from_the_end_is_not_looked_for(self):
        # the last token_count is within 8 KB of a file's end (154 files,
        # measured 2026-10-01): the read stops at 64 KB
        self.home.rollout([token_count(NOW - 60)], when=NOW - 60, pad=80)
        self.assertEqual(self.rows(), [])
        self.home.rollout([token_count(NOW - 30)], when=NOW - 30, thread=T2, pad=40)  # control
        self.assertEqual(len(self.rows()), 1)

    def test_a_file_is_read_again_only_when_it_changed(self):
        path = self.home.rollout([token_count(NOW - 60, used=65.0)], when=NOW - 60)
        cache = {}
        self.assertEqual(self.rows(cache)[0]["seven_day"]["pct"], 65.0)
        with open(path, "r+") as fh:                  # the same size, the same inode
            text = fh.read()
            fh.seek(0)
            fh.write(text.replace("65.0", "30.0"))
        os.utime(path, (NOW - 60, NOW - 60))
        self.assertEqual(self.rows(cache)[0]["seven_day"]["pct"], 65.0)
        os.utime(path, (NOW - 59, NOW - 59))
        self.assertEqual(self.rows(cache)[0]["seven_day"]["pct"], 30.0)

    def test_no_codex_home_is_no_rows(self):
        self.assertEqual(usage.codex_rows(os.path.join(self.home.dir, "nope"), NOW), [])

    def test_a_broken_line_is_skipped(self):
        path = self.home.rollout([token_count(NOW - 60)], when=NOW - 60)
        with open(path, "a") as fh:
            fh.write('{"type": "event_msg", "payload": {"type": "token_count", "rate_l\n')
        os.utime(path, (NOW - 60, NOW - 60))
        self.assertEqual(self.rows()[0]["seven_day"]["pct"], 65.0)


class TestCodexOnTheUsageLine(unittest.TestCase):
    """On the line two weeks after its last reading, as a Claude account (owner,
    2026-10-03 - it was only while a thread was open, D23); D24: named by its limit."""
    CODEX = {"id": "codex:codex", "kind": "codex", "brand": "oai", "email": "", "label": "",
             "sessions": 0, "age": 60, "five_hour": None,
             "seven_day": {"state": "ok", "pct": 65.0, "resets_at": CODEX_RESET, "age": 60}}
    FACTS = {"usage_roots": [{"root": "/h/.claude", "state": "ours", "opted_out": False}]}

    def test_it_follows_the_claude_accounts_as_oai_codex(self):
        r = reading("login:a", five=(5, FIVE_RESET), sid="s1", email="lukaso@gmail.com")
        s = usage.snapshot([r], NOW, {"s1"}, self.FACTS, codex=[self.CODEX])
        self.assertEqual([a["id"] for a in s["accounts"]], ["login:a", "codex:codex"])
        line = text(usage.usage_lines(s, 200))[0]
        self.assertIn("  │  oai:codex 7d 65%↑/57% ↻", line)
        self.assertTrue(line.startswith("usage  ant:lukaso 5h 5%"), line)

    def test_codex_alone_is_a_line(self):
        s = usage.snapshot([], NOW, set(), {"usage_roots": []}, codex=[self.CODEX])
        self.assertEqual(s["state"], "ok")
        self.assertTrue(text(usage.usage_lines(s, 200))[0].startswith("usage  oai:codex 7d 65%"))

    def test_an_open_thread_spends_its_entry(self):
        # a highlighted Codex row brightens its entry, as a Claude row its account
        r = reading("login:a", five=(5, FIVE_RESET), sid="s1", email="lukaso@gmail.com")
        s = usage.snapshot([r], NOW, {"s1"}, self.FACTS,
                           codex=[dict(self.CODEX, threads=[T1])])
        self.assertEqual(s["sessions"], {"s1": "login:a", T1: "codex:codex"})
        bright = lambda sel: "".join(t for line in usage.usage_lines(s, 200, selected=sel)
                                     for t, st in line if st == "plain")
        self.assertIn("oai:codex", bright(s["sessions"][T1]))
        self.assertNotIn("lukaso", bright(s["sessions"][T1]))
        self.assertNotIn("oai:codex", bright(s["sessions"]["s1"]))      # control
        # a row with no threads (CODEX has none): nothing spends it
        s = usage.snapshot([r], NOW, {"s1"}, self.FACTS, codex=[self.CODEX])
        self.assertEqual(s["sessions"], {"s1": "login:a"})

    def test_no_codex_no_entry(self):                                     # control
        r = reading("login:a", five=(5, FIVE_RESET), sid="s1")
        for codex in (None, []):
            s = usage.snapshot([r], NOW, {"s1"}, self.FACTS, codex=codex)
            self.assertNotIn("oai", text(usage.usage_lines(s, 200))[0])

    def test_a_copied_name_names_it(self):
        self.assertEqual(usage.resolve_id("oai:codex", ["login:a", "codex:codex"]),
                         "codex:codex")
        # the line's own name, whole: "codex" is a prefix of "codex:premium" too
        ids = ["login:a", "codex:codex", "codex:premium"]
        self.assertEqual(usage.resolve_id("oai:codex", ids), "codex:codex")
        self.assertEqual(usage.resolve_id("oai:premium", ids), "codex:premium")
        self.assertIsNone(usage.resolve_id("ant:codex", ids))      # not a Claude account
        self.assertIsNone(usage.resolve_id("ant:codex", ["login:a", "codex:codex"]))
