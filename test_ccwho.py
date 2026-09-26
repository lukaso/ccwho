"""Tests for ccwho. Stdlib only: python3 -m unittest -v"""
import json
import re
import os
import shutil
import shlex
import subprocess
import tempfile
import unittest

import ccwho_brief as brief
import ccwho_engine as ccwho

# Reading this machine's processes, ports and session files is machine state: a
# test that reaches it answers differently on every laptop and every minute. Every
# test in this module runs with guards that fail loudly instead; a test that means
# to exercise a real function takes it from REAL and puts the guard back.
REAL = {name: getattr(ccwho, name) for name in ("live_file_sessions", "ps_table",
                                                "listen_ports")}
REAL_LIVE_FILE_SESSIONS = REAL["live_file_sessions"]


def _guard(name):
    def unpinned(*a, **k):
        raise AssertionError(f"a test reached this machine through ccwho.{name} - "
                             "pin it (see MachinelessCollect)")
    return unpinned


GUARDS = {name: _guard(name) for name in REAL}
_unpinned_live_file_sessions = GUARDS["live_file_sessions"]


def install_guards():
    for name, guard in GUARDS.items():
        setattr(ccwho, name, guard)


def setUpModule():
    install_guards()


def tearDownModule():
    for name, real in REAL.items():
        setattr(ccwho, name, real)


class MachinelessCollect(unittest.TestCase):
    """collect() reaches the machine: ps, the tty map, and an AppleScript round
    trip to iTerm2. A test that leaves those live measures this laptop, takes a
    second each, and answers differently on someone else's. Pin them."""

    def setUp(self):
        self._saved = (ccwho.ps_snapshot, ccwho.tty_snapshot, ccwho.titles_snapshot,
                       ccwho.live_file_sessions, ccwho.ps_table, ccwho.listen_ports)
        ccwho.ps_snapshot = lambda: ""
        ccwho.tty_snapshot = lambda: ""
        ccwho.titles_snapshot = lambda timeout=5.0: {}
        # the session files, the start times and the ports of THIS machine are
        # machine state too
        ccwho.live_file_sessions = lambda *a, **k: ([], 0)
        ccwho.ps_table = lambda: {}
        ccwho.listen_ports = lambda: {}

    def tearDown(self):
        (ccwho.ps_snapshot, ccwho.tty_snapshot, ccwho.titles_snapshot,
         ccwho.live_file_sessions, ccwho.ps_table, ccwho.listen_ports) = self._saved


class TestParseSessions(unittest.TestCase):
    def test_parses_agents_json(self):
        raw = json.dumps([
            {"pid": 1, "cwd": "/Users/x/projects/liveapp", "kind": "interactive",
             "startedAt": 1000, "sessionId": "aaa", "name": "liveapp-4e", "status": "busy"},
        ])
        got = ccwho.parse_sessions(raw)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "liveapp-4e")

    def test_empty_input_is_not_an_error(self):
        self.assertEqual(ccwho.parse_sessions("[]"), [])

    def test_malformed_json_returns_empty(self):
        # None, not []: output we cannot parse is "we do not know", and a caller
        # that reads it as "nothing is running" reopens sessions that are alive.
        self.assertIsNone(ccwho.parse_sessions("not json"))


class TestProject(unittest.TestCase):
    def test_project_is_last_path_component(self):
        self.assertEqual(ccwho.project_of("/Users/x/projects/liveapp"), "liveapp")

    def test_worktree_keeps_parent_repo_name(self):
        cwd = "/Users/x/projects/football/.claude/worktrees/fix-nav"
        self.assertEqual(ccwho.project_of(cwd), "football")

    def test_missing_cwd(self):
        self.assertEqual(ccwho.project_of(""), "?")


class TestTopic(unittest.TestCase):
    def _lines(self, *msgs):
        out = []
        for m in msgs:
            out.append(json.dumps({"type": "user", "message": {"content": m}}))
        return out

    def test_last_user_prompt_is_the_topic(self):
        got = ccwho.extract_topic(self._lines("first thing", "second thing"))
        self.assertEqual(got["last"], "second thing")
        self.assertEqual(got["first"], "first thing")

    def test_skips_system_reminders_and_tool_noise(self):
        got = ccwho.extract_topic(self._lines("real prompt", "<system-reminder>noise</>"))
        self.assertEqual(got["last"], "real prompt")

    def test_handles_content_as_block_list(self):
        line = json.dumps({"type": "user", "message": {"content": [
            {"type": "text", "text": "block prompt"},
            {"type": "tool_result", "content": "ignored"},
        ]}})
        self.assertEqual(ccwho.extract_topic([line])["last"], "block prompt")

    def test_ignores_assistant_turns(self):
        lines = self._lines("mine") + [json.dumps({"type": "assistant", "message": {"content": "theirs"}})]
        self.assertEqual(ccwho.extract_topic(lines)["last"], "mine")

    def test_collapses_whitespace_and_newlines(self):
        self.assertEqual(ccwho.extract_topic(self._lines("a\n\n  b   c"))["last"], "a b c")

    def test_no_user_messages(self):
        got = ccwho.extract_topic([])
        self.assertEqual(got["last"], "")
        self.assertEqual(got["first"], "")

    def test_bad_lines_do_not_crash(self):
        self.assertEqual(ccwho.extract_topic(["{bad", ""] + self._lines("ok"))["last"], "ok")


class TestBoilerplate(unittest.TestCase):
    def _lines(self, *msgs):
        return [json.dumps({"type": "user", "message": {"content": m}}) for m in msgs]

    def test_skill_preamble_is_not_a_topic(self):
        got = ccwho.extract_topic(self._lines(
            "fix the flaky test",
            "Base directory for this skill: /Users/x/.claude/skills/review",
        ))
        self.assertEqual(got["last"], "fix the flaky test")

    def test_compaction_notice_is_not_a_topic(self):
        got = ccwho.extract_topic(self._lines(
            "port the metrics row",
            "This session is being continued from a previous conversation that ran out of context",
        ))
        self.assertEqual(got["last"], "port the metrics row")

    def test_local_command_caveat_is_not_a_topic(self):
        got = ccwho.extract_topic(self._lines(
            "land it",
            "Caveat: The messages below were generated by the user while running local commands",
        ))
        self.assertEqual(got["last"], "land it")

    def test_all_boilerplate_falls_back_to_boilerplate_not_empty(self):
        got = ccwho.extract_topic(self._lines("Base directory for this skill: /x"))
        self.assertTrue(got["last"].startswith("Base directory"))

    def test_strict_mode_returns_empty_rather_than_boilerplate(self):
        got = ccwho.extract_topic(self._lines("<task-notification>x</>"), strict=True)
        self.assertEqual(got["last"], "")

    def test_strict_mode_still_returns_real_prompts(self):
        got = ccwho.extract_topic(self._lines("real one", "<task-notification>x</>"), strict=True)
        self.assertEqual(got["last"], "real one")

    def test_nonstrict_still_falls_back(self):
        got = ccwho.extract_topic(self._lines("Base directory for this skill: /x"), strict=False)
        self.assertTrue(got["last"].startswith("Base directory"))

    def test_is_boilerplate_predicate(self):
        self.assertTrue(ccwho.is_boilerplate("Base directory for this skill: /x"))
        self.assertFalse(ccwho.is_boilerplate("run the gate"))

    def test_the_engine_and_the_brief_share_one_rule(self):
        # Three places ask "did a human type this?": the topic column, the
        # brief's "you said", and search. Two lists drift, and then the list and
        # the brief disagree about the same session.
        self.assertIs(ccwho.is_boilerplate("x"), not brief.is_human_prompt("x"))
        for machine in ("Another Claude session sent a message: hi",
                        "Your claude.ai usage limit has reset. Continue",
                        "[Subagent report] done"):
            self.assertTrue(ccwho.is_boilerplate(machine), machine[:30])

    def test_the_topic_column_walks_back_past_machine_text(self):
        lines = [json.dumps({"type": "user", "timestamp": "2026-09-18T10:00:00.000Z",
                             "message": {"role": "user", "content": "rebase and land"}}),
                 json.dumps({"type": "user", "timestamp": "2026-09-18T11:00:00.000Z",
                             "message": {"role": "user",
                                         "content": "Your claude.ai usage limit has"
                                                    " reset. Continue the task"}})]
        self.assertEqual(ccwho.extract_topic(lines, strict=True)["last"], "rebase and land")


class TestTitle(unittest.TestCase):
    def test_reads_ai_title(self):
        lines = [json.dumps({"type": "ai-title", "aiTitle": "PR review workflow redesign"})]
        self.assertEqual(ccwho.extract_title(lines), "PR review workflow redesign")

    def test_last_ai_title_wins(self):
        lines = [json.dumps({"type": "ai-title", "aiTitle": "old"}),
                 json.dumps({"type": "ai-title", "aiTitle": "new"})]
        self.assertEqual(ccwho.extract_title(lines), "new")

    def test_no_title_is_empty(self):
        self.assertEqual(ccwho.extract_title([json.dumps({"type": "user"})]), "")

    def test_bad_lines_do_not_crash(self):
        self.assertEqual(ccwho.extract_title(["{oops"]), "")


class TestDoing(unittest.TestCase):
    def _tool(self, name, **inp):
        return json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": inp}]}})

    def test_prefers_bash_description_over_raw_command(self):
        got = ccwho.extract_doing([self._tool("Bash", command="rm -rf /x", description="Record evidence")])
        self.assertEqual(got, "Bash: Record evidence")

    def test_falls_back_to_command_when_no_description(self):
        got = ccwho.extract_doing([self._tool("Bash", command="pytest -x")])
        self.assertEqual(got, "Bash: pytest -x")

    def test_file_tools_show_the_file(self):
        got = ccwho.extract_doing([self._tool("Edit", file_path="/a/b/ccwho.py")])
        self.assertEqual(got, "Edit: ccwho.py")

    def test_last_tool_wins(self):
        got = ccwho.extract_doing([self._tool("Read", file_path="/a.py"), self._tool("Grep", pattern="foo")])
        self.assertTrue(got.startswith("Grep"))

    def test_collapses_multiline_commands(self):
        got = ccwho.extract_doing([self._tool("Bash", command="line one\nline two")])
        self.assertEqual(got, "Bash: line one line two")

    def test_no_tools_is_empty(self):
        self.assertEqual(ccwho.extract_doing([]), "")


class TestSince(unittest.TestCase):
    def test_seconds_then_minutes(self):
        self.assertEqual(ccwho.since(100.0, now=105.0), "5s")
        self.assertEqual(ccwho.since(0.0, now=300.0), "5m")

    def test_hours_and_days(self):
        self.assertEqual(ccwho.since(0.0, now=7200.0), "2h")
        self.assertEqual(ccwho.since(0.0, now=172800.0), "2d")

    def test_missing_is_question_mark(self):
        self.assertEqual(ccwho.since(None, now=1.0), "?")


class TestOrphansNamedOnTheCommandLine(unittest.TestCase):
    """The fallback for a process whose environment cannot be read: an orphan
    whose command line names the session (a scratchpad path does) still counts
    for it (eng D8). The PID-1 total that used to sit in the header is gone -
    it counted every daemon under your home, agent or not."""

    PS = "\n".join([
        "  PID  PPID COMMAND",
        " 1000     1 bash -c scripts/check.sh > /tmp/claude-501/proj/aaa/scratchpad/g.log",
        " 1001     1 node /some/other/thing.ts",
        " 1002   999 bash -c owned-by-a-live-parent /tmp/.../aaa/...",
    ])

    def test_an_orphan_naming_the_session_is_its(self):
        got = ccwho._cmdline_orphans(self.PS, ["aaa", "bbb"])
        self.assertEqual(got, {"aaa": {1000}, "bbb": set()})

    def test_a_process_with_a_live_parent_is_not_an_orphan(self):     # control
        self.assertNotIn(1002, ccwho._cmdline_orphans(self.PS, ["aaa"])["aaa"])

    def test_empty_ps_output(self):
        self.assertEqual(ccwho._cmdline_orphans("", ["aaa"]), {"aaa": set()})


class TestSorting(unittest.TestCase):
    def test_needs_you_sorts_first(self):
        rows = [
            {"status": "idle", "project": "a", "name": "i"},
            {"status": "waiting", "project": "z", "name": "w"},
            {"status": "busy", "project": "a", "name": "b"},
        ]
        got = [r["name"] for r in sorted(rows, key=ccwho.sort_key)]
        self.assertEqual(got[0], "w")

    def test_busy_before_idle(self):
        rows = [{"status": "idle", "project": "a", "name": "i"},
                {"status": "busy", "project": "a", "name": "b"}]
        got = [r["name"] for r in sorted(rows, key=ccwho.sort_key)]
        self.assertEqual(got, ["b", "i"])

    def test_unknown_status_does_not_crash(self):
        rows = [{"status": "weird", "project": "a", "name": "x"}]
        self.assertEqual(sorted(rows, key=ccwho.sort_key)[0]["name"], "x")


class TestAge(unittest.TestCase):
    def test_formats_minutes_hours_days(self):
        self.assertEqual(ccwho.age(1000 * 1000, now=1000 * 1000 + 5 * 60_000), "5m")
        self.assertEqual(ccwho.age(0, now=3 * 3600_000), "3h")
        self.assertEqual(ccwho.age(0, now=2 * 86400_000), "2d")

    def test_missing_timestamp(self):
        self.assertEqual(ccwho.age(None, now=1000), "?")


class TestTruncate(unittest.TestCase):
    def test_truncates_with_ellipsis(self):
        self.assertEqual(ccwho.truncate("abcdefghij", 5), "abcd…")

    def test_short_string_untouched(self):
        self.assertEqual(ccwho.truncate("abc", 5), "abc")


if __name__ == "__main__":
    unittest.main()


class TestTheHeaderHasNoGateLine(unittest.TestCase):
    """ccgate was removed: never used outside the session that built it. The
    header must not consult a gate module even when one is importable and holds
    a slot - a stale copy on sys.path would otherwise put the line back."""

    def test_a_held_slot_is_not_reported(self):
        import sys
        import types
        fake = types.ModuleType("ccgate")
        fake.GATE_DIR = "/nonexistent"
        fake.gate_status = lambda d: [{"label": "liveapp gate", "since": 0, "alive": True}]
        saved = sys.modules.get("ccgate")
        sys.modules["ccgate"] = fake
        try:
            out = ccwho.render([self.row()], 0, color=False, width=200)
        finally:
            if saved is None:
                sys.modules.pop("ccgate", None)
            else:
                sys.modules["ccgate"] = saved
        self.assertNotIn("gate", out)

    def test_the_header_is_still_drawn(self):                          # control
        # with no rows render() returns before the header, which made the
        # assertion above pass without reaching the code it is about
        self.assertIn("1 sessions", ccwho.render([self.row()], 0, color=False,
                                                 width=200))

    def row(self):
        session = {"pid": 4242, "cwd": "/Users/x/projects/app", "sessionId": "abc",
                   "name": "app-4e", "status": "idle", "startedAt": 1788200000000}
        return ccwho.build_row(session, [], [], mtime=1788203600, tty="")


class TestWaitingKind(unittest.TestCase):
    """A session Claude Code calls `waiting` may be blocked on a prompt, or may
    simply have finished its turn. Only the first deserves NEEDS YOU."""

    def _assistant(self, stop_reason, tool_id=None):
        content = [{"type": "text", "text": "hi"}]
        if tool_id:
            content = [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {}}]
        return json.dumps({"type": "assistant",
                           "message": {"stop_reason": stop_reason, "content": content}})

    def _result(self, tool_id):
        return json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": "ok"}]}})

    def test_end_turn_with_nothing_pending_is_ready(self):
        self.assertEqual(ccwho.waiting_kind([self._assistant("end_turn")]), "ready")

    def test_unanswered_tool_use_is_blocked(self):
        lines = [self._assistant("tool_use", tool_id="t1")]
        self.assertEqual(ccwho.waiting_kind(lines), "blocked")

    def test_answered_tool_use_is_ready(self):
        lines = [self._assistant("tool_use", tool_id="t1"), self._result("t1")]
        self.assertEqual(ccwho.waiting_kind(lines), "ready")

    def test_one_of_two_answered_is_still_blocked(self):
        lines = [self._assistant("tool_use", tool_id="t1"),
                 self._assistant("tool_use", tool_id="t2"), self._result("t1")]
        self.assertEqual(ccwho.waiting_kind(lines), "blocked")

    def test_empty_transcript_is_ready_not_blocked(self):
        self.assertEqual(ccwho.waiting_kind([]), "ready")

    def test_bad_lines_do_not_crash(self):
        self.assertEqual(ccwho.waiting_kind(["{bad"]), "ready")


class TestAttentionSort(unittest.TestCase):
    def test_blocked_outranks_busy(self):
        rows = [{"attention": "busy", "status": "busy", "project": "a", "name": "b"},
                {"attention": "blocked", "status": "waiting", "project": "z", "name": "w"}]
        self.assertEqual(sorted(rows, key=ccwho.sort_key)[0]["name"], "w")

    def test_ready_sorts_below_busy(self):
        rows = [{"attention": "ready", "status": "waiting", "project": "a", "name": "r"},
                {"attention": "busy", "status": "busy", "project": "a", "name": "b"}]
        self.assertEqual([r["name"] for r in sorted(rows, key=ccwho.sort_key)], ["b", "r"])

    def test_ready_sorts_above_idle(self):
        rows = [{"attention": "idle", "status": "idle", "project": "a", "name": "i"},
                {"attention": "ready", "status": "waiting", "project": "a", "name": "r"}]
        self.assertEqual([r["name"] for r in sorted(rows, key=ccwho.sort_key)], ["r", "i"])

    def test_falls_back_to_status_when_attention_absent(self):
        rows = [{"status": "idle", "project": "a", "name": "i"},
                {"status": "busy", "project": "a", "name": "b"}]
        self.assertEqual([r["name"] for r in sorted(rows, key=ccwho.sort_key)], ["b", "i"])


class TestAsksUser(unittest.TestCase):
    """The harness reports `idle` for a session that ended its turn with a
    question. Whether it needs you is in the closing line, not the status field."""

    def test_closing_question_mark(self):
        self.assertTrue(ccwho.asks_user("Did the work.\n\nWant me to build S2a?"))

    def test_markdown_is_stripped_before_matching(self):
        self.assertTrue(ccwho.asks_user("**Want me to build S2a?**"))

    def test_imperative_request_without_a_question_mark(self):
        self.assertTrue(ccwho.asks_user("Tell me which and I'll finish it and land."))

    def test_say_the_word(self):
        self.assertTrue(ccwho.asks_user("Outside this change's scope - say the word and it's a one-liner."))

    def test_your_call(self):
        self.assertTrue(ccwho.asks_user("It's your machine and your call; I'm not killing anything."))

    def test_the_decision_is_yours(self):
        # a real closing line, left in BUSY for 26 hours
        self.assertTrue(ccwho.asks_user("I'm stopping here rather than starting another round. "
                                        "The decision is yours: land the R1 fix, re-cut W1 "
                                        "STATE-only, or park both."))

    def test_plain_statement_is_not_an_ask(self):
        self.assertFalse(ccwho.asks_user("Idle and ready."))

    def test_report_of_results_is_not_an_ask(self):
        self.assertFalse(ccwho.asks_user("234 tests green, typecheck clean."))

    def test_the_word_question_alone_is_not_an_ask(self):
        self.assertFalse(ccwho.asks_user("Still open, not blocking: the spend labelling question."))

    def test_only_the_closing_line_counts(self):
        self.assertFalse(ccwho.asks_user("Should I do X?\n\nDone, all landed."))

    def test_a_quoted_question_is_not_an_ask(self):
        # real closing lines: the question is one put to a reviewer, not to you
        self.assertFalse(ccwho.asks_user(
            'Both reviewers are still running: one mutation audit asking only "can each '
            'of these assertions actually fail?". I will fix whatever they find.'))
        self.assertFalse(ccwho.asks_user(
            "I'll keep watching r/gimp - user-crowd questions (“does it work for X?”) "
            "are the likely first comments."))

    def test_an_ask_phrase_in_quotes_is_not_an_ask(self):
        self.assertFalse(ccwho.asks_user('The reviewer\'s brief says "tell me what breaks". It runs now.'))

    def test_a_question_outside_the_quotes_still_asks(self):     # control
        self.assertTrue(ccwho.asks_user('The round asked "is it pending?" - rename it to "ready"?'))
        self.assertTrue(ccwho.asks_user('I asked it "what breaks?" - say the word and I land it.'))

    def test_an_ask_between_two_quotes_still_asks(self):         # control
        # a quote runs to the NEXT mark, not the last: the ask between is kept
        self.assertTrue(ccwho.asks_user('Kept "a" - want me to rename it to "b"'))

    def test_inch_marks_are_not_quotes(self):
        self.assertTrue(ccwho.asks_user('The 13" build passes - want me to ship it? The 15" one too.'))

    def test_an_unclosed_quote_hides_nothing(self):              # control
        self.assertTrue(ccwho.asks_user('The label reads "ready. Want me to fix it?'))

    def test_empty_text(self):
        self.assertFalse(ccwho.asks_user(""))


class TestExtractAsk(unittest.TestCase):
    def _msg(self, text):
        return json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": text}]}})

    def test_returns_the_closing_line(self):
        got = ccwho.extract_ask([self._msg("Did work.\n\nWant me to build S2a?")])
        self.assertEqual(got, "Want me to build S2a?")

    def test_empty_when_not_asking(self):
        self.assertEqual(ccwho.extract_ask([self._msg("All done.")]), "")

    def test_uses_the_last_assistant_message(self):
        lines = [self._msg("Want me to do X?"), self._msg("Never mind, finished.")]
        self.assertEqual(ccwho.extract_ask(lines), "")

    def test_no_messages(self):
        self.assertEqual(ccwho.extract_ask([]), "")


class TestAsksRanking(unittest.TestCase):
    def test_asks_outranks_busy(self):
        rows = [{"attention": "busy", "project": "a", "name": "b"},
                {"attention": "asks", "project": "z", "name": "q"}]
        self.assertEqual(sorted(rows, key=ccwho.sort_key)[0]["name"], "q")

    def test_blocked_still_outranks_asks(self):
        rows = [{"attention": "asks", "project": "a", "name": "q"},
                {"attention": "blocked", "project": "z", "name": "x"}]
        self.assertEqual(sorted(rows, key=ccwho.sort_key)[0]["name"], "x")

    def test_asks_outranks_idle(self):
        rows = [{"attention": "idle", "project": "a", "name": "i"},
                {"attention": "asks", "project": "a", "name": "q"}]
        self.assertEqual([r["name"] for r in sorted(rows, key=ccwho.sort_key)], ["q", "i"])


class TestLastTurnTs(unittest.TestCase):
    """File mtime is not activity: idle transcripts get metadata writes every few
    minutes, so mtime reported 4m for a session last worked on six days ago."""

    def _entry(self, typ, ts):
        return json.dumps({"type": typ, "timestamp": ts,
                           "message": {"content": [{"type": "text", "text": "x"}]}})

    def test_parses_iso_z(self):
        got = ccwho.last_turn_ts([self._entry("assistant", "2026-08-31T12:00:00.000Z")])
        self.assertAlmostEqual(got, 1788177600.0, places=0)

    def test_last_turn_wins(self):
        lines = [self._entry("assistant", "2026-08-30T12:00:00.000Z"),
                 self._entry("user", "2026-08-31T12:00:00.000Z")]
        self.assertAlmostEqual(ccwho.last_turn_ts(lines), 1788177600.0, places=0)

    def test_ignores_metadata_entries(self):
        lines = [self._entry("assistant", "2026-08-30T12:00:00.000Z"),
                 json.dumps({"type": "atis-latch", "timestamp": "2026-08-31T12:00:00.000Z"}),
                 json.dumps({"type": "bridge-session", "timestamp": "2026-08-31T12:00:00.000Z"})]
        self.assertAlmostEqual(ccwho.last_turn_ts(lines), 1788091200.0, places=0)

    def test_none_when_no_timestamps(self):
        self.assertIsNone(ccwho.last_turn_ts([json.dumps({"type": "assistant"})]))

    def test_bad_timestamp_is_skipped(self):
        lines = [self._entry("assistant", "2026-08-30T12:00:00.000Z"),
                 self._entry("assistant", "not-a-date")]
        self.assertAlmostEqual(ccwho.last_turn_ts(lines), 1788091200.0, places=0)

    def test_empty(self):
        self.assertIsNone(ccwho.last_turn_ts([]))


class TestRecencySort(unittest.TestCase):
    def test_newer_ask_sorts_above_older(self):
        # names chosen so alphabetical order gives the WRONG answer
        rows = [{"attention": "asks", "ts": 100, "project": "a", "name": "aaa-old"},
                {"attention": "asks", "ts": 900, "project": "a", "name": "zzz-new"}]
        self.assertEqual([r["name"] for r in sorted(rows, key=ccwho.sort_key)],
                         ["zzz-new", "aaa-old"])

    def test_rank_still_beats_recency(self):
        rows = [{"attention": "asks", "ts": 100, "project": "a", "name": "ask"},
                {"attention": "idle", "ts": 900, "project": "a", "name": "idle"}]
        self.assertEqual([r["name"] for r in sorted(rows, key=ccwho.sort_key)], ["ask", "idle"])

    def test_missing_ts_sorts_last_within_rank(self):
        rows = [{"attention": "asks", "project": "a", "name": "aaa-nots"},
                {"attention": "asks", "ts": 900, "project": "a", "name": "zzz-has"}]
        self.assertEqual([r["name"] for r in sorted(rows, key=ccwho.sort_key)],
                         ["zzz-has", "aaa-nots"])


class TestBuildRow(unittest.TestCase):
    """Covers the wiring collect() used to hide: which clock `since` reads from."""

    SESSION = {"sessionId": "s1", "cwd": "/Users/x/projects/liveapp",
               "status": "idle", "name": "liveapp-4e", "startedAt": 1788000000000}

    def _turn(self, ts, text="all done."):
        return json.dumps({"type": "assistant", "timestamp": ts,
                           "message": {"content": [{"type": "text", "text": text}]}})

    def test_since_uses_the_last_turn_not_file_mtime(self):
        # transcript last touched 6 days ago; file mtime is 60s old from metadata writes
        old = 1788177600.0 - 6 * 86400
        row = ccwho.build_row(self.SESSION, [], [self._turn("2026-08-25T12:00:00.000Z")],
                              mtime=1788177600.0 - 60, now=1788177600.0)
        self.assertEqual(row["since"], "6d")
        self.assertAlmostEqual(row["ts"], old, places=0)

    def test_falls_back_to_mtime_when_no_timestamps(self):
        row = ccwho.build_row(self.SESSION, [], [json.dumps({"type": "assistant"})],
                              mtime=1788177600.0 - 300, now=1788177600.0)
        self.assertEqual(row["since"], "5m")

    def test_ask_promotes_an_idle_session(self):
        tail = [self._turn("2026-08-31T12:00:00.000Z", "Want me to build S2a?")]
        row = ccwho.build_row(self.SESSION, [], tail, mtime=0, now=1788177600.0)
        self.assertEqual(row["attention"], "asks")
        self.assertEqual(row["ask"], "Want me to build S2a?")

    def test_plain_finish_with_no_background_work_wants_reviewing(self):
        # It reads as "stopped" only once you have looked at it: finishing a
        # turn is the commonest thing a session ever needs you for.
        tail = [self._turn("2026-08-31T12:00:00.000Z", "234 tests green.")]
        row = ccwho.build_row(self.SESSION, [], tail, mtime=0, now=1788177600.0, work=0)
        self.assertEqual(row["attention"], "review")
        self.assertEqual(row["ask"], "")
        seen = {self.SESSION["sessionId"]: row["ts"]}
        self.assertEqual(ccwho.build_row(self.SESSION, [], tail, mtime=0,
                                         now=1788177600.0, work=0,
                                         reviewed=seen)["attention"], "stopped")

    def test_busy_is_never_reclassified_as_asks(self):
        s = dict(self.SESSION, status="busy")
        tail = [self._turn("2026-08-31T12:00:00.000Z", "Want me to build S2a?")]
        self.assertEqual(ccwho.build_row(s, [], tail, mtime=0, now=1788177600.0)["attention"], "busy")

    def test_project_and_title_are_carried(self):
        tail = [json.dumps({"type": "ai-title", "aiTitle": "Gate red triage"})]
        row = ccwho.build_row(self.SESSION, [], tail, mtime=0, now=1788177600.0)
        self.assertEqual(row["project"], "liveapp")
        self.assertEqual(row["title"], "Gate red triage")


class TestRecordsAcceptBoth(unittest.TestCase):
    """Extractors must accept pre-parsed dicts, so a tail is parsed once per tick
    instead of once per extractor. They took 5.2 passes over every line."""

    def test_as_records_parses_strings(self):
        got = ccwho.as_records([json.dumps({"type": "user"})])
        self.assertEqual(got[0]["type"], "user")

    def test_as_records_passes_dicts_through(self):
        d = {"type": "user"}
        self.assertIs(ccwho.as_records([d])[0], d)

    def test_as_records_skips_junk(self):
        self.assertEqual(ccwho.as_records(["{bad", None, 7]), [])

    def test_as_records_skips_valid_json_that_is_not_an_object(self):
        # "123" and "[1,2]" parse fine but are not records; without the dict
        # guard they reach the extractors and blow up on .get()
        self.assertEqual(ccwho.as_records(["123", "[1,2]", '"str"', "null"]), [])

    def test_title_from_dicts(self):
        self.assertEqual(ccwho.extract_title([{"type": "ai-title", "aiTitle": "T"}]), "T")

    def test_doing_from_dicts(self):
        recs = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"description": "go"}}]}}]
        self.assertEqual(ccwho.extract_doing(recs), "Bash: go")

    def test_topic_from_dicts(self):
        recs = [{"type": "user", "message": {"content": "hello there"}}]
        self.assertEqual(ccwho.extract_topic(recs)["last"], "hello there")

    def test_last_turn_ts_from_dicts(self):
        recs = [{"type": "assistant", "timestamp": "2026-08-31T12:00:00.000Z"}]
        self.assertAlmostEqual(ccwho.last_turn_ts(recs), 1788177600.0, places=0)

    def test_waiting_kind_from_dicts(self):
        recs = [{"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}}]
        self.assertEqual(ccwho.waiting_kind(recs), "blocked")


class TestWindowCache(unittest.TestCase):
    def test_fresh_entry_is_valid(self):
        self.assertTrue(ccwho.cache_valid({"mtime": 5.0, "size": 100}, 5.0, 100))

    def test_changed_mtime_invalidates(self):
        self.assertFalse(ccwho.cache_valid({"mtime": 5.0, "size": 100}, 6.0, 100))

    def test_changed_size_invalidates(self):
        self.assertFalse(ccwho.cache_valid({"mtime": 5.0, "size": 100}, 5.0, 101))

    def test_missing_entry_invalidates(self):
        self.assertFalse(ccwho.cache_valid(None, 5.0, 100))

    def test_incomplete_entry_invalidates(self):
        self.assertFalse(ccwho.cache_valid({"mtime": 5.0}, 5.0, 100))


class TestWorkDescendants(unittest.TestCase):
    """A session that is not busy has either stopped, or is waiting on background
    work. Only the first needs you. MCP servers are infrastructure, not work."""

    PS = "\n".join([
        "  PID  PPID COMMAND",
        " 100     1 claude",
        " 101   100 npm exec chrome-devtools-mcp@latest --isolated",
        " 102   101 chrome-devtools-mcp",
        " 103   102 /Users/me/.npm/_npx/15c6/node/mcp.js",
        " 200     1 claude",
        " 201   200 /bin/zsh -c source /Users/me/.claude/shell-snapshots/x",
        " 202   201 node vitest",
    ])

    def test_session_with_only_mcp_has_no_work(self):
        self.assertEqual(ccwho.work_descendants(self.PS, 100), 0)

    def test_session_running_a_command_has_work(self):
        self.assertEqual(ccwho.work_descendants(self.PS, 200), 2)

    def test_grandchildren_count(self):
        ps = "\n".join(["  PID  PPID COMMAND", " 300     1 claude",
                        " 301   300 bash x", " 302   301 node y"])
        self.assertEqual(ccwho.work_descendants(ps, 300), 2)

    def test_unknown_pid_is_zero(self):
        self.assertEqual(ccwho.work_descendants(self.PS, 999), 0)

    def test_none_pid_is_zero(self):
        self.assertEqual(ccwho.work_descendants(self.PS, None), 0)

    def test_a_parent_cycle_terminates(self):
        ps = "\n".join(["  PID  PPID COMMAND", " 400   401 a", " 401   400 b"])
        self.assertEqual(ccwho.work_descendants(ps, 400), 1)


class TestBusyWithTheTurnOver(unittest.TestCase):
    """The feed says `busy` while any background task runs, even after the turn
    has ended. Found 2026-09-24: two sessions ended their turns with a question
    and sat in BUSY - one for 26 hours - behind a wait loop that could never
    stop. A finished turn is still NOT news on its own: the task will wake the
    session, so only a question may pull the row out of BUSY.
    """
    SESSION = {"sessionId": "s1", "cwd": "/Users/x/projects/liveapp",
               "status": "busy", "name": "n", "startedAt": 1788000000000}
    T = "2026-09-24T16:33:40.601Z"

    # the records Claude Code writes when a turn ends, as measured: 2235 of 2251
    # ended turns carry turn_duration, and 0 of 44860 mid-turn steps do
    END = [{"type": "system", "subtype": "stop_hook_summary", "timestamp": T},
           {"type": "system", "subtype": "turn_duration", "timestamp": T},
           {"type": "system", "subtype": "away_summary", "timestamp": T}]

    def _said(self, text):
        return {"type": "assistant", "timestamp": self.T,
                "message": {"content": [{"type": "text", "text": text}]}}

    def _attention(self, tail, work=1):
        return ccwho.build_row(self.SESSION, [], tail, mtime=0,
                               now=1790267620.0, work=work)["attention"]

    def test_a_question_at_the_end_of_the_turn_asks(self):
        tail = [self._said("Want me to run the full gate, or stop here?")] + self.END
        self.assertEqual(self._attention(tail), "asks")

    def test_a_handback_without_a_question_mark_asks(self):
        tail = [self._said("The decision is yours: land the R1 fix, or park both.")] + self.END
        self.assertEqual(self._attention(tail), "asks")

    def test_a_turn_that_ended_without_asking_is_running_not_news(self):
        tail = [self._said("Suite is running in the background; I'll report.")] + self.END
        self.assertEqual(self._attention(tail), "running")

    def test_no_process_to_see_is_still_running(self):
        # a background subagent is no child process, but the feed still knows
        tail = [self._said("Two agents are reviewing it.")] + self.END
        self.assertEqual(self._attention(tail, work=0), "running")

    def test_a_question_mid_turn_is_busy(self):                        # control
        tail = [self._said("Why does this fail?"),
                {"type": "assistant", "timestamp": self.T, "message": {"content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}},
                {"type": "user", "timestamp": self.T, "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}]
        self.assertEqual(self._attention(tail), "busy")

    def test_a_question_with_no_end_marker_is_busy(self):              # control
        self.assertEqual(self._attention([self._said("Why does this fail?")]), "busy")

    def test_you_answered_so_it_is_busy_again(self):                   # control
        tail = ([self._said("Want me to run the full gate?")] + self.END
                + [{"type": "user", "timestamp": self.T, "message": {"content": "yes"}}])
        self.assertEqual(self._attention(tail), "busy")

    def test_the_task_woke_it_so_it_is_busy_again(self):               # control
        tail = ([self._said("Want me to run the full gate?")] + self.END
                + [self._said("The suite finished; reading it.")])
        self.assertEqual(self._attention(tail), "busy")


class TestStuckOnALoopThatCannotEnd(unittest.TestCase):
    """The case 8cc8093 left in BUSY: the turn ended without a question, and the
    only thing keeping the feed on `busy` is a wait loop that can never stop.
    Nothing will wake that session, so it is not running - it is stuck."""
    SESSION, T, END = (TestBusyWithTheTurnOver.SESSION, TestBusyWithTheTurnOver.T,
                       TestBusyWithTheTurnOver.END)
    _said = TestBusyWithTheTurnOver._said

    LOOP = {"pid": 86246, "tasks": ["bscl8fc6k"]}

    def _stuck(self, tail, dead=None):
        return ccwho.build_row(self.SESSION, [], tail, mtime=0, now=1790267620.0,
                               work=2, dead_loops=[self.LOOP] if dead is None else dead)

    def test_ended_turn_and_a_dead_loop_is_stuck(self):
        tail = [self._said("Three things remain stated-but-unclosed.")] + self.END
        self.assertEqual(self._stuck(tail)["attention"], "stuck")

    def test_no_dead_loop_is_still_running(self):                     # control
        tail = [self._said("Three things remain stated-but-unclosed.")] + self.END
        self.assertEqual(self._stuck(tail, dead=[])["attention"], "running")

    def test_a_question_still_asks(self):
        # a dead loop - or a watcher on it - never hides a question
        tail = [self._said("Want me to run the full gate?")] + self.END
        self.assertEqual(self._stuck(tail)["attention"], "asks")

    def test_mid_turn_it_is_busy(self):                                # control
        self.assertEqual(self._stuck([self._said("Checking.")])["attention"], "busy")

    def test_the_row_says_which(self):
        tail = [self._said("Want me to run the full gate?")] + self.END
        self.assertEqual(self._stuck(tail)["dead_loops"], [self.LOOP])


class TestStuckOnScreen(unittest.TestCase):
    """STUCK is its own group, after NEEDS YOU: the session needs you - to kill
    the loop - but less urgently than a question. Its second line says which
    loop, and carries the one part of any row that can be clicked on its own."""
    LOOP = {"pid": 86246, "tasks": ["bscl8fc6k"]}

    def _row(self, attention="stuck", loops=None):
        return {"sessionId": "e0e2a7c1-c7c4-499c-a72d-27d9c11b3211", "project": "liveapp",
                "attention": attention, "since": "3h", "title": "Blabberate",
                "recap": "Goal was moving files into STATE", "recap_age": "3h",
                "dead_loops": [self.LOOP] if loops is None else loops}

    def test_its_own_group_after_needs_you(self):
        rows = [self._row("stopped"), self._row(), self._row("asks")]
        self.assertEqual([g["heading"] for g in ccwho.ui_groups(rows)],
                         ["NEEDS YOU", "STUCK", "STOPPED"])

    def test_it_sorts_after_a_question_and_before_stopped(self):
        rank = ccwho._RANK
        self.assertLess(rank["review"], rank["stuck"])
        self.assertLess(rank["stuck"], rank["stopped"])

    def test_the_second_line_names_the_loop_and_offers_the_kill(self):
        _, second = ccwho.ui_row_cells(self._row(), width=100)
        text = "".join(t for t, _ in second)
        self.assertIn("loop 86246", text)
        self.assertIn("bscl8fc6k", text)
        self.assertEqual([t for t, r in second if r == "action"], ["[kill loop]"])

    def test_the_kill_survives_a_narrow_window(self):
        _, second = ccwho.ui_row_cells(self._row(), width=40)
        self.assertEqual([t for t, r in second if r == "action"], ["[kill loop]"])

    def test_no_other_row_has_anything_to_click(self):                   # control
        for att in ("asks", "busy", "running", "stopped"):
            with self.subTest(att=att):
                first, second = ccwho.ui_row_cells(self._row(att, loops=[]), width=100)
                self.assertNotIn("action", [r for _, r in first + second])

    def test_a_busy_row_with_a_dead_loop_shows_it_but_offers_nothing(self):
        # mid-turn the agent may be about to deal with it: said, not offered
        _, second = ccwho.ui_row_cells(self._row("busy"), width=100)
        self.assertNotIn("action", [r for _, r in second])

    def test_the_click_lands_on_the_kill(self):
        _, second = ccwho.ui_row_cells(self._row(), width=100)
        col = 0
        for text, role in second:
            if role == "action":
                break
            col += len(text)
        self.assertEqual(ccwho.ui_action_at(self._row(), 100, 1, col), "kill")
        self.assertEqual(ccwho.ui_action_at(self._row(), 100, 1, col + 10), "kill")

    def test_a_wide_recap_keeps_the_kill_where_it_is_drawn(self):
        # the recap was cut in characters and the line in cells: a CJK recap ran
        # twice as wide, the cut dropped the kill, and the click zone was counted
        # in characters while the screen counts cells
        row = dict(self._row(), recap="把持久化文件移到状态目录并验证每一个游标的恢复路径都能工作" * 3)
        for width in (80, 100, 140):
            with self.subTest(width=width):
                _, second = ccwho.ui_row_cells(row, width=width)
                self.assertIn("action", [r for _, r in second])
                col = 0
                for text, role in second:
                    if role == "action":
                        break
                    col += ccwho._cells(text)
                self.assertEqual(ccwho.ui_action_at(row, width, 1, col), "kill")
                self.assertIsNone(ccwho.ui_action_at(row, width, 1, col - 3))

    def test_a_click_anywhere_else_is_no_action(self):                   # control
        self.assertIsNone(ccwho.ui_action_at(self._row(), 100, 0, 30))
        self.assertIsNone(ccwho.ui_action_at(self._row(), 100, 1, 3))
        self.assertIsNone(ccwho.ui_action_at(self._row("asks", loops=[]), 100, 1, 90))


class TestTheDetailIsOneClickAway(unittest.TestCase):
    """Reported: "there's no good way to get the detail screen with the mouse".
    A click on a row goes to the session, so each row ends with an arrow that
    opens its detail instead: \\ over /, one big > the height of the row, at the
    right edge where it is in the same place on every row. (Right click and
    ctrl-click are iTerm2's own menu: they never reach the list.)"""

    LOOP = {"pid": 86246, "tasks": ["bscl8fc6k"]}

    def _row(self, name="fix the login bug in auth", **kw):
        return dict({"sessionId": "e0e2a7c1-c7c4-499c-a72d-27d9c11b3211",
                     "project": "liveapp", "attention": "stopped", "since": "3h",
                     "title": name, "tty": "ttys022", "recap": "the recap " * 20,
                     "recap_age": "3h", "doing": ""}, **kw)

    def rows(self):
        stuck = self._row(attention="stuck", dead_loops=[self.LOOP])
        return [self._row("short"), self._row("a very long name " * 8),
                self._row("把持久化文件移到状态目录" * 4), self._row(recap=""), stuck]

    def _col(self, cells, role="detail"):
        col = 0
        for text, r in cells:
            if r == role:
                return col, text
            col += ccwho._cells(text)
        return None, None

    def test_both_lines_end_with_half_the_arrow_at_the_right_edge(self):
        for row in self.rows():
            for width in (40, 80, 100, 140):
                with self.subTest(name=row["title"][:10], stuck=bool(row.get("dead_loops")),
                                  width=width):
                    first, second = ccwho.ui_row_cells(row, width=width)
                    self.assertEqual(first[-1], (ccwho.UI_DETAIL[0], "detail"))
                    self.assertEqual(second[-1], (ccwho.UI_DETAIL[1], "detail"))
                    for line in (first, second):
                        self.assertEqual(sum(ccwho._cells(t) for t, _ in line), width,
                                         "the same place on every row: the edge")

    def test_it_is_one_arrow(self):
        top, bottom = ccwho.UI_DETAIL
        self.assertEqual(top.strip(), "\\")
        self.assertEqual(bottom.strip(), "/")
        self.assertEqual(ccwho._cells(top), ccwho._cells(bottom))

    def test_a_click_on_either_half_opens_the_detail(self):
        for row in self.rows():
            for width in (40, 100):
                for line in (0, 1):
                    with self.subTest(width=width, line=line):
                        cells = ccwho.ui_row_cells(row, width=width)[line]
                        col, text = self._col(cells)
                        for c in range(col, col + ccwho._cells(text)):
                            self.assertEqual(ccwho.ui_action_at(row, width, line, c),
                                             "detail")
                        self.assertNotEqual(ccwho.ui_action_at(row, width, line, col - 1),
                                            "detail")

    def test_the_kill_stays_whole_and_before_the_arrow(self):
        stuck = self.rows()[-1]
        for width in (40, 80, 140):
            with self.subTest(width=width):
                _, second = ccwho.ui_row_cells(stuck, width=width)
                roles = [r for _, r in second]
                self.assertEqual([t for t, r in second if r == "action"], ["[kill loop]"])
                self.assertLess(roles.index("action"), roles.index("detail"))
                col, _ = self._col(second, "action")
                self.assertEqual(ccwho.ui_action_at(stuck, width, 1, col), "kill")

    # pad, the gap before the kill, the kill, one cell, the arrow's half
    KILL_FITS = 8 + 2 + len("[kill loop]") + 1 + 3

    def test_the_kill_stays_whole_down_to_its_own_width(self):
        # the loop's words give way, down to nothing - never the kill
        stuck = dict(self.rows()[-1], dead_loops=[
            {"pid": 86246, "tasks": ["bscl8fc6k", "b2", "b3"]}, self.LOOP])
        for width in range(self.KILL_FITS, 41):
            with self.subTest(width=width):
                _, second = ccwho.ui_row_cells(stuck, width=width)
                self.assertIn(("[kill loop]", "action"), second)
                self.assertEqual(second[-1], (ccwho.UI_DETAIL[1], "detail"))
                self.assertEqual(sum(ccwho._cells(t) for t, _ in second), width)

    def test_below_that_there_is_no_room_for_it(self):                 # control
        stuck = self.rows()[-1]
        _, second = ccwho.ui_row_cells(stuck, width=self.KILL_FITS - 1)
        self.assertNotIn(("[kill loop]", "action"), second)

    def test_the_zones_are_where_a_tagged_row_is_drawn(self):
        for row in self.rows():
            for tag in ("", "1a2b", "lukaso@gmail"):
                for width in (30, 40, 60, 100):
                    for line in (0, 1):
                        with self.subTest(tag=tag, width=width, line=line):
                            cells = ccwho.ui_row_cells(row, width=width, tag=tag)[line]
                            want, at = [], 0
                            for text, role in cells:
                                kind = {"action": "kill", "detail": "detail"}.get(role)
                                want += [kind] * ccwho._cells(text)
                            got = [ccwho.ui_action_at(row, width, line, c, tag=tag)
                                   for c in range(len(want))]
                            self.assertEqual(got, want)

    def test_the_words_never_run_into_it(self):
        for row in self.rows():
            first, second = ccwho.ui_row_cells(row, width=80)
            for line in (first, second):
                self.assertEqual(line[-2][1], "pad", "a gap between the words and the arrow")
                self.assertGreaterEqual(ccwho._cells(line[-2][0]), 1)

    def test_the_rest_of_the_row_is_no_action(self):                    # control
        row = self._row()
        self.assertIsNone(ccwho.ui_action_at(row, 100, 0, 30))
        self.assertIsNone(ccwho.ui_action_at(row, 100, 1, 30))


class TestKillDeadLoops(unittest.TestCase):
    """The kill looks again first. Between the scan and the key press a loop can
    end, and its pid can go to something else: only a pid that is STILL a dead
    loop of that session gets the signal."""
    TASKS = "/private/tmp/claude-501/p/s/tasks/"
    LOOP = "until grep -q 'Test Files' " + TASKS + "bscl8fc6k.output; do sleep 5; done"
    ENDED = ("x\n[exited with code 0]\n", 600)

    def _kill(self, ps, pids, fact=None):
        sent = []
        got = ccwho.kill_dead_loops(73787, pids, ps=lambda: ps,
                                    read=lambda path: fact or self.ENDED, matches=lambda argv, files: False,
                                    kill=lambda pid, sig: sent.append((pid, sig)))
        return got, sent

    def test_it_kills_the_loop(self):
        got, sent = self._kill(f"73787 1 claude\n86246 73787 {self.LOOP}\n", [86246])
        self.assertEqual(got, [86246])
        self.assertEqual(sent, [(86246, ccwho.signal.SIGTERM)])

    def test_a_reused_pid_is_left_alone(self):                           # control
        got, sent = self._kill("73787 1 claude\n86246 73787 vim notes.md\n", [86246])
        self.assertEqual((got, sent), ([], []))

    def test_a_loop_that_is_live_again_is_left_alone(self):              # control
        got, sent = self._kill(f"73787 1 claude\n86246 73787 {self.LOOP}\n", [86246],
                               fact=("still running\n", 600))
        self.assertEqual(sent, [])

    def test_only_what_was_asked(self):                                  # control
        ps = (f"73787 1 claude\n86246 73787 {self.LOOP}\n"
              f"86247 73787 {self.LOOP}\n")
        _, sent = self._kill(ps, [86246])
        self.assertEqual([pid for pid, _ in sent], [86246])

    def test_another_sessions_loop_is_left_alone(self):                  # control
        got, sent = self._kill(f"73787 1 claude\n86246 555 {self.LOOP}\n", [86246])
        self.assertEqual(sent, [])

    def test_a_loop_whose_line_came_since_is_left_alone(self):          # control
        sent = []
        got = ccwho.kill_dead_loops(73787, [86246],
                                    ps=lambda: f"73787 1 claude\n86246 73787 {self.LOOP}\n",
                                    read=lambda path: self.ENDED,
                                    kill=lambda pid, sig: sent.append(pid),
                                    matches=lambda argv, files: True)
        self.assertEqual((got, sent), ([], []))

    def test_a_loop_already_gone_is_not_an_error(self):
        sent = []

        def kill(pid, sig):
            raise ProcessLookupError
        got = ccwho.kill_dead_loops(73787, [86246],
                                    ps=lambda: f"73787 1 claude\n86246 73787 {self.LOOP}\n",
                                    read=lambda path: self.ENDED, kill=kill,
                                    matches=lambda argv, files: False)
        self.assertEqual(got, [])


class TestTaskFile(unittest.TestCase):
    """The two facts the dead-loop rule needs about a task output: how it ends,
    and how long ago it last changed. Read from the end, from a regular file
    only: a subagent's output is a symlink to its whole transcript."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "b1.output")
        with open(self.path, "w") as f:
            f.write("x" * 5000 + "\n[exited with code 0]\n")
        os.utime(self.path, (1000, 1000))

    def test_the_end_and_the_age(self):
        tail, age = ccwho.task_file(self.path, now=1600)
        self.assertTrue(tail.endswith("[exited with code 0]\n"))
        self.assertEqual(age, 600)

    def test_only_the_end_is_read(self):
        tail, _ = ccwho.task_file(self.path, now=1600)
        self.assertLess(len(tail), 1000)

    def test_a_symlink_is_not_followed(self):
        link = os.path.join(self.dir, "a1.output")
        os.symlink(self.path, link)
        self.assertIsNone(ccwho.task_file(link, now=1600))

    def test_a_missing_file_is_unknown(self):
        self.assertIsNone(ccwho.task_file(self.path + ".gone", now=1600))

    def test_a_fifo_is_not_waited_on(self):
        # swapped in after a check, opening one for reading would hang the scan
        import threading
        fifo = os.path.join(self.dir, "b2.output")
        os.mkfifo(fifo)
        # the race: any check by path saw the regular file that was there before
        real_lstat, regular = os.lstat, os.lstat(self.path)
        os.lstat = lambda p, *a, **k: regular if p == fifo else real_lstat(p, *a, **k)
        got = []
        t = threading.Thread(target=lambda: got.append(ccwho.task_file(fifo, now=1600)),
                             daemon=True)
        try:
            t.start()
            t.join(2)
        finally:
            os.lstat = real_lstat
        blocked = t.is_alive()
        if blocked:                     # unblock the reader so the suite can end
            with open(fifo, "w"):
                pass
        self.assertFalse(blocked, "task_file blocked on a FIFO")
        self.assertEqual(got, [None])

    def test_a_fifo_with_something_in_it_is_not_read(self):
        fifo = os.path.join(self.dir, "b3.output")
        os.mkfifo(fifo)
        keep = os.open(fifo, os.O_RDWR)          # a writer, so there is data
        self.addCleanup(os.close, keep)
        os.write(keep, b"[exited with code 0]\n")
        self.assertIsNone(ccwho.task_file(fifo, now=1600))


class TestGrepMatches(unittest.TestCase):
    """Asking the loop's grep again, on the file it polls: True, False, or None
    when grep could not answer."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "b1.output")
        with open(self.path, "w") as f:
            f.write(" Test Files  3 passed (3)\n[exited with code 0]\n")

    def test_a_line_that_is_there(self):
        self.assertIs(ccwho.grep_matches(["-q", "-E", "-e", "^ *(Test Files|Tests) "],
                                         [self.path]), True)

    def test_a_line_that_is_not(self):                                 # control
        self.assertIs(ccwho.grep_matches(["-q", "-e", "never printed"], [self.path]), False)

    def test_a_pattern_that_looks_like_an_option_stays_a_pattern(self):
        self.assertIs(ccwho.grep_matches(["-q", "-e", "--version"], [self.path]), False)

    def test_a_file_that_is_not_there_is_unknown(self):
        self.assertIsNone(ccwho.grep_matches(["-q", "-e", "x"], [self.path + ".gone"]))

    def test_a_match_in_either_locale_is_a_match(self):
        # review round 4: the loop's shell ran in UTF-8, where "." is one "·";
        # in the C locale it is one byte of two, and the line is not found
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("Tests·passed\n[exited with code 0]\n")
        self.assertIs(ccwho.grep_matches(["-q", "-e", "^Tests.passed$"], [self.path]), True)

    def test_no_match_in_both_is_no_match(self):                         # control
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("Tests·failed\n[exited with code 0]\n")
        self.assertIs(ccwho.grep_matches(["-q", "-e", "^Tests.passed$"], [self.path]), False)

    def test_one_locale_that_cannot_answer_is_unknown(self):
        answers = iter([1, 2])
        run = lambda argv, **kw: subprocess.CompletedProcess(argv, next(answers))
        self.assertIsNone(ccwho.grep_matches(["-q", "-e", "x"], [self.path], run=run))

    def test_the_same_grep_every_time(self):
        # review round 3: not whatever grep, locale or PATH ccwho was started with
        seen = {}

        def run(argv, **kw):
            seen.update(argv=argv, env=kw.get("env"))
            return subprocess.CompletedProcess(argv, 1)
        ccwho.grep_matches(["-q", "-e", "x"], [self.path], run=run)
        self.assertEqual(seen["argv"][0], "/usr/bin/grep")
        self.assertIn(seen["env"]["LC_ALL"], ("C", "en_US.UTF-8"))
        self.assertNotIn("GREP_OPTIONS", seen["env"])

    def test_a_file_that_looks_like_an_option_is_a_file(self):
        # without `--`, "-v" would turn "never printed" into "any other line"
        self.assertIsNone(ccwho.grep_matches(["-q", "-e", "never printed"],
                                             ["-v", self.path]))


class TestFindDeadLoops(unittest.TestCase):
    """A session's wait loops that cannot end. Found by walking the session's own
    process tree: a Bash tool shell carries no session mark (measured), so the
    list procs.attribute builds never has it."""
    TASKS = "/private/tmp/claude-501/p/s/tasks/"
    LOOP = ("until grep -q 'Test Files' " + TASKS + "bf6yb2pu5.output; "
            "do sleep 5; done")
    ENDED = ("partial\n[exited with code 0]\n", 600)

    def _find(self, cmd, fact, ppid=55625, session=55625):
        ptable = {55625: (1, "", "claude"), 27737: (ppid, "", cmd),
                  42386: (27737, "", "sleep 5")}
        seen = []

        def read(path):
            seen.append(path)
            return fact
        return ccwho.find_dead_loops(session, ptable, read, matches=lambda argv, files: False), seen

    def test_a_loop_on_a_finished_task_is_found(self):
        found, seen = self._find(self.LOOP, self.ENDED)
        self.assertEqual(found, [{"pid": 27737, "tasks": ["bf6yb2pu5"]}])
        self.assertEqual(seen, [self.TASKS + "bf6yb2pu5.output"])

    def test_deeper_in_the_tree_too(self):
        ptable = {55625: (1, "", "claude"), 60: (55625, "", "bash"),
                  27737: (60, "", self.LOOP)}
        self.assertEqual([d["pid"] for d in ccwho.find_dead_loops(
            55625, ptable, lambda path: self.ENDED, matches=lambda argv, files: False)], [27737])

    def test_another_sessions_loop_is_not_this_ones(self):             # control
        found, seen = self._find(self.LOOP, self.ENDED, ppid=999)
        self.assertEqual(found, [])
        self.assertEqual(seen, [])

    def test_a_loop_on_a_live_task_is_not(self):                      # control
        found, _ = self._find(self.LOOP, ("running\n", 600))
        self.assertEqual(found, [])

    def test_an_unreadable_file_is_not(self):                         # control
        found, _ = self._find(self.LOOP, None)
        self.assertEqual(found, [])

    def test_anything_that_is_not_a_wait_loop_is_never_read(self):    # control
        found, seen = self._find("vitest run", self.ENDED)
        self.assertEqual((found, seen), ([], []))

    def test_no_session_pid_finds_nothing(self):
        self.assertEqual(self._find(self.LOOP, self.ENDED, session=None)[0], [])

    def _tree(self, *kids, loop=None, age=600, matches=lambda argv, files: False):
        ptable = {55625: (1, "", "claude"), 27737: (55625, "", loop or self.LOOP)}
        for i, (ppid, cmd) in enumerate(kids):
            ptable[90000 + i] = (ppid, "", cmd)
        fact = ("partial\n[exited with code 0]\n", age)
        return [d["pid"] for d in ccwho.find_dead_loops(55625, ptable, lambda p: fact,
                                                        matches=matches)]

    def test_between_looks_it_is_found(self):                         # control
        self.assertEqual(self._tree(), [27737])

    def test_mid_look_it_is_found(self):                              # control
        self.assertEqual(self._tree((27737, "grep -q Test Files x")), [27737])

    def test_a_loop_with_other_work_under_it_is_not(self):
        # the loop ended and the shell went on to the rest of its command
        self.assertEqual(self._tree((27737, "npm run e2e")), [])

    def test_a_forked_copy_is_the_same_loop(self):
        # zsh forks for a pipeline stage or $(...), with the same argv
        self.assertEqual(self._tree((27737, self.LOOP), (90000, "sleep 5")), [27737])

    def test_a_long_sleep_gets_its_look_first(self):
        loop = "until grep -q DONE " + self.TASKS + "b1.output; do sleep 600; done"
        self.assertEqual(self._tree(loop=loop, age=150), [])

    def test_a_long_sleep_long_after_is_found(self):                  # control
        loop = "until grep -q DONE " + self.TASKS + "b1.output; do sleep 600; done"
        self.assertEqual(self._tree(loop=loop, age=700), [27737])

    # review round 2: a loop that ended, in a shell that went on to more
    AFTER = ("until grep -q ok /private/tmp/claude-501/p/s/tasks/b1.output; "
             "do sleep 5; done; sleep 3600; ./deploy")

    def test_a_loop_whose_line_is_there_has_ended(self):
        self.assertEqual(self._tree((27737, "sleep 3600"), loop=self.AFTER,
                                    matches=lambda argv, files: True), [])

    def test_a_loop_whose_line_is_not_there_cannot_end(self):         # control
        self.assertEqual(self._tree((27737, "sleep 5"), loop=self.AFTER), [27737])

    def test_a_grep_that_could_not_answer_is_unknown(self):
        self.assertEqual(self._tree(matches=lambda argv, files: None), [])

    def test_the_question_is_the_loops_own(self):
        asked = []
        self._tree(loop=self.AFTER,
                   matches=lambda argv, files: asked.append((argv, files)) or False)
        self.assertEqual(asked, [(["-q", "-e", "ok"],
                                  ["/private/tmp/claude-501/p/s/tasks/b1.output"])])

    def test_a_finished_file_is_asked_once(self):
        # review round 3: a finished file does not change, nor does its answer
        asked, cache = [], {}
        ptable = {55625: (1, "", "claude"), 27737: (55625, "", self.LOOP)}
        fact = ("partial\n[exited with code 0]\n", 600)
        for _ in range(3):
            ccwho.find_dead_loops(55625, ptable, lambda p: fact, cache=cache,
                                  matches=lambda a, f: asked.append(1) or False)
        self.assertEqual(len(asked), 1)

    def test_a_file_that_changed_is_asked_again(self):                # control
        asked, cache = [], {}
        ptable = {55625: (1, "", "claude"), 27737: (55625, "", self.LOOP)}
        for tail in ("a\n[exited with code 0]\n", "b\n[exited with code 0]\n"):
            ccwho.find_dead_loops(55625, ptable, lambda p: (tail, 600), cache=cache,
                                  matches=lambda a, f: asked.append(1) or False)
        self.assertEqual(len(asked), 2)

    def test_the_answer_kept_is_the_answer(self):
        cache = {}
        ptable = {55625: (1, "", "claude"), 27737: (55625, "", self.LOOP)}
        fact = ("partial\n[exited with code 0]\n", 600)
        first = ccwho.find_dead_loops(55625, ptable, lambda p: fact, cache=cache,
                                      matches=lambda a, f: True)
        again = ccwho.find_dead_loops(55625, ptable, lambda p: fact, cache=cache,
                                      matches=lambda a, f: False)
        self.assertEqual((first, again), ([], []))

    def test_grep_that_could_not_answer_is_asked_again(self):
        # review round 4: a fork that failed once must not hide the loop for good
        answers, cache = iter([None, False]), {}
        ptable = {55625: (1, "", "claude"), 27737: (55625, "", self.LOOP)}
        fact = ("partial\n[exited with code 0]\n", 600)
        got = [ccwho.find_dead_loops(55625, ptable, lambda p: fact, cache=cache,
                                     matches=lambda a, f: next(answers))
               for _ in range(2)]
        self.assertEqual([[d["pid"] for d in g] for g in got], [[], [27737]])

    def test_a_short_sleep_needs_only_the_grace(self):                # control
        self.assertEqual(self._tree(age=150), [27737])

    def test_a_parent_cycle_terminates(self):
        ptable = {1: (3, "", "claude"), 2: (1, "", self.LOOP), 3: (2, "", "sleep 5")}
        self.assertEqual([d["pid"] for d in ccwho.find_dead_loops(
            1, ptable, lambda path: self.ENDED, matches=lambda argv, files: False)], [2])


class TestTurnEnded(unittest.TestCase):
    def test_turn_duration_after_the_last_message_ends_it(self):
        self.assertTrue(ccwho.turn_ended([
            {"type": "assistant", "message": {"content": []}},
            {"type": "system", "subtype": "turn_duration"}]))

    def test_a_stop_hook_summary_alone_ends_it(self):
        self.assertTrue(ccwho.turn_ended([
            {"type": "assistant", "message": {"content": []}},
            {"type": "system", "subtype": "stop_hook_summary"}]))

    def test_other_system_records_do_not(self):                        # control
        self.assertFalse(ccwho.turn_ended([
            {"type": "assistant", "message": {"content": []}},
            {"type": "system", "subtype": "away_summary"}]))

    def test_a_message_after_the_marker_reopens_it(self):              # control
        self.assertFalse(ccwho.turn_ended([
            {"type": "system", "subtype": "turn_duration"},
            {"type": "user", "message": {"content": "go"}}]))

    def test_an_empty_tail_has_not_ended(self):
        self.assertFalse(ccwho.turn_ended([]))


class TestStoppedVsRunning(unittest.TestCase):
    SESSION = {"sessionId": "s1", "cwd": "/Users/x/projects/liveapp",
               "status": "idle", "name": "n", "startedAt": 1788000000000}

    def _turn(self, text):
        return {"type": "assistant", "timestamp": "2026-08-31T12:00:00.000Z",
                "message": {"content": [{"type": "text", "text": text}]}}

    def test_not_busy_with_no_work_wants_reviewing(self):
        row = ccwho.build_row(self.SESSION, [], [self._turn("All done.")],
                              mtime=0, now=1788177600.0, work=0)
        self.assertEqual(row["attention"], "review")

    def test_not_busy_with_background_work_is_running(self):
        row = ccwho.build_row(self.SESSION, [], [self._turn("All done.")],
                              mtime=0, now=1788177600.0, work=3)
        self.assertEqual(row["attention"], "running")

    def test_an_ask_still_wins_over_stopped(self):
        row = ccwho.build_row(self.SESSION, [], [self._turn("Want me to land it?")],
                              mtime=0, now=1788177600.0, work=0)
        self.assertEqual(row["attention"], "asks")

    def test_an_ask_while_background_work_runs_still_asks(self):
        row = ccwho.build_row(self.SESSION, [], [self._turn("Want me to land it?")],
                              mtime=0, now=1788177600.0, work=5)
        self.assertEqual(row["attention"], "asks")

    def test_busy_is_untouched_by_work_count(self):
        s = dict(self.SESSION, status="busy")
        row = ccwho.build_row(s, [], [self._turn("x")], mtime=0, now=1788177600.0, work=0)
        self.assertEqual(row["attention"], "busy")

    def test_stopped_outranks_busy(self):
        rows = [{"attention": "busy", "ts": 9, "project": "a", "name": "b"},
                {"attention": "stopped", "ts": 1, "project": "a", "name": "s"}]
        self.assertEqual([r["name"] for r in sorted(rows, key=ccwho.sort_key)], ["s", "b"])

    def test_running_sorts_last(self):
        rows = [{"attention": "running", "ts": 9, "project": "a", "name": "r"},
                {"attention": "busy", "ts": 1, "project": "a", "name": "b"}]
        self.assertEqual([r["name"] for r in sorted(rows, key=ccwho.sort_key)], ["b", "r"])


class TestTtyMap(unittest.TestCase):
    PS = "\n".join([
        "  PID TTY",
        "19576 ttys032",
        "26581 ttys062",
        " 1234 ??",
    ])

    def test_maps_pid_to_tty(self):
        self.assertEqual(ccwho.parse_tty_map(self.PS)[19576], "ttys032")

    def test_no_controlling_terminal_is_omitted(self):
        self.assertNotIn(1234, ccwho.parse_tty_map(self.PS))

    def test_header_is_skipped(self):
        self.assertNotIn("PID", ccwho.parse_tty_map(self.PS))

    def test_empty_input(self):
        self.assertEqual(ccwho.parse_tty_map(""), {})


class TestShortTty(unittest.TestCase):
    def test_strips_dev_and_tty(self):
        self.assertEqual(ccwho.short_tty("/dev/ttys032"), "s032")

    def test_bare_form(self):
        self.assertEqual(ccwho.short_tty("ttys032"), "s032")

    def test_empty(self):
        self.assertEqual(ccwho.short_tty(""), "")

    def test_unknown_shape_passes_through(self):
        self.assertEqual(ccwho.short_tty("console"), "console")


class TestMatchRows(unittest.TestCase):
    ROWS = [
        {"pid": 19576, "tty": "ttys032", "title": "Liveapp temp directory leak", "project": "liveapp"},
        {"pid": 26581, "tty": "ttys062", "title": "Managing multiple Claude Code shells", "project": "liveapp"},
        {"pid": 73336, "tty": "ttys147", "title": "Vitest cleanup", "project": "liveapp"},
    ]

    def test_matches_by_pid(self):
        self.assertEqual(ccwho.match_rows(self.ROWS, "19576")[0]["pid"], 19576)

    def test_matches_by_tty_short_form(self):
        self.assertEqual(ccwho.match_rows(self.ROWS, "s062")[0]["pid"], 26581)

    def test_matches_by_title_substring_case_insensitive(self):
        self.assertEqual(ccwho.match_rows(self.ROWS, "vitest")[0]["pid"], 73336)

    def test_returns_all_matches_when_ambiguous(self):
        self.assertEqual(len(ccwho.match_rows(self.ROWS, "liveapp")), 3)

    def test_no_match_is_empty(self):
        self.assertEqual(ccwho.match_rows(self.ROWS, "zzzz"), [])

    def test_empty_query_matches_nothing(self):
        self.assertEqual(ccwho.match_rows(self.ROWS, ""), [])


class TestReadyCollapsesIntoStopped(unittest.TestCase):
    """`waiting` with nothing pending is not a separate state: it is stopped or
    running like any other non-busy session, decided by what is in flight."""

    SESSION = {"sessionId": "s1", "cwd": "/Users/x/projects/liveapp",
               "status": "waiting", "name": "n", "startedAt": 1788000000000}

    def _turn(self, text="done."):
        return {"type": "assistant", "timestamp": "2026-08-31T12:00:00.000Z",
                "message": {"content": [{"type": "text", "text": text}]}}

    def test_waiting_with_nothing_pending_and_no_work_wants_reviewing(self):
        row = ccwho.build_row(self.SESSION, [], [self._turn()], mtime=0,
                              now=1788177600.0, work=0)
        self.assertEqual(row["attention"], "review")
        seen = {self.SESSION["sessionId"]: row["ts"]}
        self.assertEqual(ccwho.build_row(self.SESSION, [], [self._turn()], mtime=0,
                                         now=1788177600.0, work=0,
                                         reviewed=seen)["attention"], "stopped")

    def test_waiting_with_nothing_pending_but_work_is_running(self):
        row = ccwho.build_row(self.SESSION, [], [self._turn()], mtime=0,
                              now=1788177600.0, work=2)
        self.assertEqual(row["attention"], "running")

    def test_waiting_with_a_pending_tool_is_still_blocked(self):
        tail = [{"type": "assistant", "timestamp": "2026-08-31T12:00:00.000Z",
                 "message": {"content": [{"type": "tool_use", "id": "t1",
                                          "name": "Bash", "input": {}}]}}]
        row = ccwho.build_row(self.SESSION, [], tail, mtime=0, now=1788177600.0, work=0)
        self.assertEqual(row["attention"], "blocked")

    def test_ready_is_no_longer_produced(self):
        row = ccwho.build_row(self.SESSION, [], [self._turn()], mtime=0,
                              now=1788177600.0, work=0)
        self.assertNotEqual(row["attention"], "ready")


class TestOsc8(unittest.TestCase):
    """OSC 8 makes the tty column a real hyperlink. iTerm2 renders it; clicking
    hands the URL to LaunchServices, where a tiny applet turns it into a jump."""

    def test_wraps_label_in_an_osc8_sequence(self):
        got = ccwho.osc8("s032", "ccwho://jump/s032", enabled=True)
        self.assertEqual(got, "\033]8;;ccwho://jump/s032\033\\s032\033]8;;\033\\")

    def test_disabled_returns_the_bare_label(self):
        self.assertEqual(ccwho.osc8("s032", "ccwho://jump/s032", enabled=False), "s032")

    def test_empty_url_returns_the_bare_label(self):
        self.assertEqual(ccwho.osc8("s032", "", enabled=True), "s032")

    def test_label_length_is_what_gets_padded(self):
        # the escape must not count toward column width
        self.assertEqual(ccwho.visible_len(ccwho.osc8("s032", "ccwho://jump/s032", True)), 4)

    def test_visible_len_of_plain_text(self):
        self.assertEqual(ccwho.visible_len("s032"), 4)

    def test_visible_len_ignores_colour_codes(self):
        self.assertEqual(ccwho.visible_len("\033[2ms032\033[0m"), 4)


class TestJumpUrl(unittest.TestCase):
    def test_prefers_tty(self):
        self.assertEqual(ccwho.jump_url({"tty": "ttys032", "pid": 19576}),
                         "ccwho://jump/s032")

    def test_falls_back_to_pid(self):
        self.assertEqual(ccwho.jump_url({"tty": "", "pid": 19576}),
                         "ccwho://jump/19576")

    def test_empty_when_neither(self):
        self.assertEqual(ccwho.jump_url({"tty": "", "pid": None}), "")

    def test_parse_round_trip(self):
        self.assertEqual(ccwho.parse_jump_url("ccwho://jump/s032"), "s032")

    def test_parse_tolerates_trailing_slash(self):
        self.assertEqual(ccwho.parse_jump_url("ccwho://jump/s032/"), "s032")

    def test_parse_rejects_other_schemes(self):
        self.assertEqual(ccwho.parse_jump_url("http://evil/jump/s032"), "")

    def test_parse_rejects_a_scheme_crafted_to_survive_the_slice(self):
        # "https://evil/" is exactly as long as "ccwho://jump/", so without the
        # scheme check this yields a perfectly valid-looking target
        self.assertEqual(ccwho.parse_jump_url("https://evil/s032"), "")

    def test_parse_rejects_shell_metacharacters(self):
        self.assertEqual(ccwho.parse_jump_url("ccwho://jump/s032;rm -rf /"), "")

    def test_parse_empty(self):
        self.assertEqual(ccwho.parse_jump_url(""), "")


class TestEtimeSeconds(unittest.TestCase):
    """ps etime has four shapes and getting it wrong silently kills live work."""

    def test_seconds_only(self):
        self.assertEqual(ccwho.etime_seconds("39"), 39)

    def test_mm_ss(self):
        self.assertEqual(ccwho.etime_seconds("02:30"), 150)

    def test_hh_mm_ss(self):
        self.assertEqual(ccwho.etime_seconds("01:00:00"), 3600)

    def test_days_hh_mm_ss(self):
        self.assertEqual(ccwho.etime_seconds("01-20:48:59"), 161339)

    def test_multi_digit_days(self):
        self.assertEqual(ccwho.etime_seconds("11-00:00:00"), 950400)

    def test_garbage_is_zero_so_nothing_is_reaped_by_accident(self):
        self.assertEqual(ccwho.etime_seconds("nonsense"), 0)
        self.assertEqual(ccwho.etime_seconds(""), 0)


class TestReapCandidates(unittest.TestCase):
    PS = "\n".join([
        "  PID  PPID ELAPSED COMMAND",
        " 100     1 01-20:48:59 script -q /dev/null /tmp/vr-A/liveapp-pty-guards-x/ptyrun.1",
        " 101   100 01-20:48:59 bash /tmp/vr-A/liveapp-pty-guards-x/ptyrun.1",
        " 200     1 00:30 script -q /dev/null /tmp/vr-B/liveapp-pty-guards-y/ptyrun.2",
        " 300   999 05:00:00 node vitest",
        " 400     1 03:00:00 claude",
    ])

    def test_matches_only_the_pattern(self):
        got = ccwho.reap_candidates(self.PS, "liveapp-pty-guards", min_age=0)
        self.assertEqual({r["pid"] for r in got}, {100, 101, 200})

    def test_age_filter_spares_the_recent_one(self):
        got = ccwho.reap_candidates(self.PS, "liveapp-pty-guards", min_age=3600)
        self.assertEqual({r["pid"] for r in got}, {100, 101})

    def test_age_filter_can_spare_everything(self):
        self.assertEqual(ccwho.reap_candidates(self.PS, "liveapp-pty-guards", min_age=10**9), [])

    def test_never_matches_unrelated_work(self):
        got = ccwho.reap_candidates(self.PS, "liveapp-pty-guards", min_age=0)
        self.assertNotIn(300, {r["pid"] for r in got})
        self.assertNotIn(400, {r["pid"] for r in got})

    def test_empty_pattern_matches_nothing(self):
        self.assertEqual(ccwho.reap_candidates(self.PS, "", min_age=0), [])

    def test_candidates_carry_age_and_command(self):
        got = ccwho.reap_candidates(self.PS, "liveapp-pty-guards", min_age=3600)
        r = [x for x in got if x["pid"] == 100][0]
        self.assertEqual(r["age"], 161339)
        self.assertIn("ptyrun", r["command"])


# ------------------------------------------------------------ restore manifest
# A reboot is a one-way door today: the sessions survive on disk but nothing says
# which was which. These cover the capture, the resume line, and the read-back.

class InTempDir(unittest.TestCase):
    ROOTS = ["/private/var/folders", "/private/tmp"]

    def test_a_test_run_dir_is_in_a_temp_dir(self):
        self.assertTrue(ccwho.in_temp_dir(
            "/private/var/folders/vx/_rt/T/vr-lrgkV6/liveapp-d1-BKYLPk/app", self.ROOTS))

    def test_the_root_itself_is_in_it(self):
        self.assertTrue(ccwho.in_temp_dir("/private/tmp", self.ROOTS))

    def test_a_project_is_not(self):                                    # control
        self.assertFalse(ccwho.in_temp_dir("/Users/x/projects/liveapp", self.ROOTS))

    def test_a_sibling_that_shares_the_prefix_is_not(self):
        self.assertFalse(ccwho.in_temp_dir("/private/tmpfoo/app", self.ROOTS))

    def test_no_path_is_not(self):
        self.assertFalse(ccwho.in_temp_dir("", self.ROOTS))


class ManifestFromRows(unittest.TestCase):
    def row(self, **kw):
        base = dict(sessionId="4f2b91ac-1111-4222-8333-abcdefabcdef",
                    cwd="/Users/x/projects/liveapp", project="liveapp",
                    topic="the reaper", first="reap leaked ptys", ask="",
                    attention="stopped", tty="s032", pid=4242, since="2h",
                    status="waiting", work=0)
        base.update(kw)
        return base

    def test_carries_what_a_restore_needs(self):
        m = ccwho.manifest_from_rows([self.row()], now=1788196525)
        s = m["sessions"][0]
        for k in ("sessionId", "cwd", "project", "topic", "attention", "tty", "pid", "since"):
            self.assertIn(k, s, f"{k} is needed to identify or reopen the session")
        self.assertEqual(s["cwd"], "/Users/x/projects/liveapp")

    def test_stamps_version_and_time_and_count(self):
        m = ccwho.manifest_from_rows([self.row(), self.row(sessionId="a" * 8 + "-b" * 4)], now=1788196525)
        self.assertEqual(m["version"], ccwho.MANIFEST_VERSION)
        self.assertEqual(m["savedAt"], 1788196525)
        self.assertEqual(m["count"], len(m["sessions"]))

    def test_drops_a_session_with_no_id_because_it_cannot_be_resumed(self):
        m = ccwho.manifest_from_rows([self.row(sessionId=""), self.row()], now=1)
        self.assertEqual(len(m["sessions"]), 1)
        self.assertEqual(m["skipped"], 1)

    def test_drops_a_session_with_no_cwd_because_resume_must_cd_first(self):
        m = ccwho.manifest_from_rows([self.row(cwd=""), self.row()], now=1)
        self.assertEqual(len(m["sessions"]), 1)
        self.assertEqual(m["skipped"], 1)

    # A restore opened eight windows onto errors: liveapp test runs whose
    # $TMPDIR was deleted, and headless workers with no transcript. A session
    # that can never resume is not part of the fleet to save.
    def test_a_session_the_caller_rules_out_is_skipped_with_its_reason(self):
        gone = self.row(sessionId="1" * 8 + "-2222-4333-8444-" + "5" * 12, project="gone")
        why = lambda r: "cwd is in a temp dir" if r["project"] == "gone" else ""
        m = ccwho.manifest_from_rows([gone, self.row()], now=1, why_not=why)
        self.assertEqual([s["project"] for s in m["sessions"]], ["liveapp"])
        self.assertEqual(m["skipped"], 1)
        self.assertEqual(m["skippedWhy"], {"cwd is in a temp dir": 1})

    def test_no_reason_keeps_every_session(self):                       # control
        m = ccwho.manifest_from_rows([self.row(), self.row(project="b")], now=1,
                                     why_not=lambda r: "")
        self.assertEqual(m["count"], 2)
        self.assertEqual(m["skipped"], 0)

    def test_the_reason_for_no_id_is_recorded_too(self):
        m = ccwho.manifest_from_rows([self.row(sessionId="")], now=1)
        self.assertEqual(sum(m["skippedWhy"].values()), m["skipped"])

    def test_keeps_the_order_it_was_given(self):
        rows = [self.row(project="a", sessionId="1" * 8 + "-2222-4333-8444-" + "5" * 12),
                self.row(project="b")]
        m = ccwho.manifest_from_rows(rows, now=1)
        self.assertEqual([s["project"] for s in m["sessions"]], ["a", "b"])


class RestoreCommand(unittest.TestCase):
    def test_cds_then_resumes_by_id(self):
        c = ccwho.restore_command({"cwd": "/Users/x/p", "sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef"})
        self.assertEqual(c, "cd /Users/x/p && claude --resume 4f2b91ac-1111-4222-8333-abcdefabcdef")

    def test_quotes_a_path_with_a_space(self):
        c = ccwho.restore_command({"cwd": "/Users/x/my code", "sessionId": "a1b2c3d4-1111-4222-8333-abcdefabcdef"})
        self.assertIn("'/Users/x/my code'", c)

    def test_a_cwd_carrying_shell_metacharacters_cannot_execute(self):
        # cwd comes from `claude agents --json`, i.ccwho. off the machine, not from us.
        evil = "/tmp/x'; touch /tmp/pwned; echo '"
        sid = "a1b2c3d4-1111-4222-8333-abcdefabcdef"
        c = ccwho.restore_command({"cwd": evil, "sessionId": sid})
        # The only assertion that means anything: the shell parses it as ONE argument
        # to cd, so the payload is data. A substring check would pass vacuously.
        self.assertEqual(shlex.split(c), ["cd", evil, "&&", "claude", "--resume", sid])

    def test_a_session_id_that_is_not_id_shaped_is_refused(self):
        for bad in ("", "a b", "id;rm -rf /", "../../etc/passwd", "$(whoami)", "-"):
            self.assertIsNone(ccwho.restore_command({"cwd": "/tmp", "sessionId": bad}), bad)

    def test_missing_cwd_is_refused(self):
        self.assertIsNone(ccwho.restore_command({"cwd": "", "sessionId": "a1b2c3d4-1111-4222-8333-abcdefabcdef"}))


class ManifestReadBack(unittest.TestCase):
    def test_entries_of_a_well_formed_manifest(self):
        m = {"version": 1, "sessions": [{"sessionId": "x"}, {"sessionId": "y"}]}
        self.assertEqual([s["sessionId"] for s in ccwho.manifest_entries(m)], ["x", "y"])

    def test_a_manifest_with_no_sessions_key_is_empty_not_a_crash(self):
        self.assertEqual(ccwho.manifest_entries({"version": 1}), [])

    def test_junk_is_empty_not_a_crash(self):
        for junk in (None, [], "nope", 7, {"sessions": "nope"}, {"sessions": [1, 2]}):
            self.assertEqual(ccwho.manifest_entries(junk), [], repr(junk))


class NewestManifest(unittest.TestCase):
    def test_iso_names_sort_chronologically(self):
        names = ["2026-08-30T0900.json", "2026-08-31T2230.json", "2026-08-31T0100.json"]
        self.assertEqual(ccwho.newest_manifest(names), "2026-08-31T2230.json")

    def test_ignores_anything_not_a_json_manifest(self):
        self.assertEqual(ccwho.newest_manifest(["notes.md", "2026-08-30T0900.json", "zzz.txt"]),
                         "2026-08-30T0900.json")

    def test_nothing_saved_yet_is_none(self):
        self.assertIsNone(ccwho.newest_manifest([]))
        self.assertIsNone(ccwho.newest_manifest(["readme.md"]))


def _local(y, mo, d, h, mi):
    """An epoch for a wall-clock time here, so the tests pass in any timezone."""
    import time
    return int(time.mktime((y, mo, d, h, mi, 0, 0, 0, -1)))


def _man(at, *sids):
    return {"version": 1, "savedAt": at, "count": len(sids), "skipped": 0,
            "sessions": [{"sessionId": s, "cwd": "/p", "project": "p"} for s in sids]}


class BootTime(unittest.TestCase):
    """When the Mac last started: `sysctl -n kern.boottime`."""

    def test_reads_the_seconds(self):
        self.assertEqual(ccwho.boot_time(
            "{ sec = 1790423298, usec = 678588 } Sat Sep 26 12:48:18 2026\n"), 1790423298)

    def test_junk_is_none_not_a_crash(self):
        for junk in ("", None, "nope", "{ usec = 5 }"):
            self.assertIsNone(ccwho.boot_time(junk), repr(junk))


class SavePoints(unittest.TestCase):
    """What `o` offers: every save, newest first, with what it holds and what
    reopening it would do - and which one was the last before the restart."""

    A, B, C = "a" * 8, "b" * 8, "c" * 8

    def saves(self):
        return [("2026-09-26T1020.json", _man(_local(2026, 9, 26, 10, 20), self.A, self.B, self.C)),
                ("2026-09-26T1222.json", _man(_local(2026, 9, 26, 12, 22), self.A, self.B, self.C)),
                ("2026-09-26T1310.json", _man(_local(2026, 9, 26, 13, 10), self.A))]

    def test_newest_first(self):
        names = [p["name"] for p in ccwho.save_points(self.saves())]
        self.assertEqual(names, ["2026-09-26T1310.json", "2026-09-26T1222.json",
                                 "2026-09-26T1020.json"])

    def test_counts_what_it_holds_and_what_is_running_now(self):
        p = ccwho.save_points(self.saves(), live_ids={self.A, "zzzz"})[1]
        self.assertEqual((p["count"], p["running"], p["to_open"]), (3, 1, 2))

    def test_one_session_saved_twice_counts_once(self):
        p = ccwho.save_points([("2026-09-26T1020.json",
                                _man(_local(2026, 9, 26, 10, 20), self.A, self.A))])[0]
        self.assertEqual(p["count"], 1, "restore opens one process per transcript")

    def test_the_last_save_before_the_restart_is_marked(self):
        points = ccwho.save_points(self.saves(), booted=_local(2026, 9, 26, 12, 48))
        self.assertEqual([p["name"] for p in points if p["before_reboot"]],
                         ["2026-09-26T1222.json"])

    def test_the_mark_skips_an_unreadable_save(self):
        saves = self.saves() + [("2026-09-26T1230.json", None)]
        points = ccwho.save_points(saves, booted=_local(2026, 9, 26, 12, 48))
        self.assertEqual([p["name"] for p in points if p["before_reboot"]],
                         ["2026-09-26T1222.json"])

    def test_no_boot_time_marks_nothing(self):                            # control
        self.assertFalse(any(p["before_reboot"] for p in ccwho.save_points(self.saves())))

    def test_a_restart_before_every_save_marks_nothing(self):             # control
        points = ccwho.save_points(self.saves(), booted=_local(2026, 9, 1, 0, 0))
        self.assertFalse(any(p["before_reboot"] for p in points))

    def test_no_savedAt_falls_back_to_the_name(self):
        man = _man(None, self.A)
        del man["savedAt"]
        p = ccwho.save_points([("2026-09-26T1020.json", man)])[0]
        self.assertEqual(p["at"], _local(2026, 9, 26, 10, 20))

    def test_an_unreadable_save_is_listed_not_dropped(self):
        points = ccwho.save_points([("2026-09-26T1020.json", None)])
        self.assertEqual(len(points), 1)
        self.assertIsNone(points[0]["count"])

    def test_a_damaged_session_id_does_not_stop_the_menu(self):
        bad = _man(_local(2026, 9, 26, 10, 20), self.A)
        bad["sessions"].append({"sessionId": [1], "cwd": "/p"})
        points = ccwho.save_points(self.saves()[1:2] + [("2026-09-26T1020.json", bad)])
        self.assertEqual([p["count"] for p in points], [3, 1])

    def test_anything_not_a_manifest_name_is_left_out(self):
        self.assertEqual(ccwho.save_points([("notes.md", _man(1, self.A))]), [])


class SavePointLine(unittest.TestCase):
    NOW = _local(2026, 9, 26, 13, 15)
    BOOT = _local(2026, 9, 26, 12, 48)

    def line(self, **kw):
        p = {"name": "2026-09-26T1222.json", "at": _local(2026, 9, 26, 12, 22),
             "count": 12, "running": 0, "to_open": 12, "before_reboot": False}
        p.update(kw)
        return ccwho.save_point_line(p, now=self.NOW, booted=self.BOOT)

    def test_says_when_and_how_many(self):
        text = self.line()
        self.assertIn("today 12:22", text)
        self.assertIn("12 sessions", text)

    def test_yesterday_and_older_say_so(self):
        self.assertIn("yesterday 18:04", self.line(at=_local(2026, 9, 25, 18, 4)))
        self.assertIn("Thu 24 Sep 09:10", self.line(at=_local(2026, 9, 24, 9, 10)))

    def test_says_what_reopening_it_would_do(self):
        self.assertIn("1 running, 11 to reopen", self.line(running=1, to_open=11))
        self.assertIn("all running", self.line(running=12, to_open=0))

    def test_marks_the_last_save_before_the_restart_with_its_time(self):
        text = self.line(before_reboot=True)
        self.assertIn("last save before the restart", text)
        self.assertIn("12:48", text)
        self.assertNotIn("restart", self.line())                        # control

    def test_one_session_is_singular(self):
        self.assertIn("1 session ", self.line(count=1, to_open=1) + " ")

    def test_an_unreadable_save_says_so(self):
        self.assertIn("unreadable", self.line(count=None))


class RenderRestore(unittest.TestCase):
    def man(self, **kw):
        base = dict(version=1, savedAt=1788196525, count=2, skipped=1, sessions=[
            {"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef", "cwd": "/Users/x/p/liveapp",
             "project": "liveapp", "topic": "the worktree reaper", "ask": "Want me to land it?",
             "attention": "asks", "tty": "s032", "since": "2h"},
            {"sessionId": "a1b2c3d4-1111-4222-8333-abcdefabcdef", "cwd": "/Users/x/p/football",
             "project": "football", "topic": "fix the probe", "ask": "",
             "attention": "stopped", "tty": "s041", "since": "20m"},
        ])
        base.update(kw)
        return base

    def test_shows_each_session_with_its_resume_line(self):
        out = ccwho.render_restore(self.man(), color=False)
        self.assertIn("liveapp", out)
        self.assertIn("the worktree reaper", out)
        self.assertIn("claude --resume 4f2b91ac-1111-4222-8333-abcdefabcdef", out)
        self.assertIn("claude --resume a1b2c3d4-1111-4222-8333-abcdefabcdef", out)

    def test_surfaces_what_was_waiting_on_you(self):
        out = ccwho.render_restore(self.man(), color=False)
        self.assertIn("Want me to land it?", out)

    def test_reports_sessions_it_could_not_capture(self):
        self.assertIn("1", ccwho.render_restore(self.man(skipped=1), color=False))
        self.assertNotIn("skipped", ccwho.render_restore(self.man(skipped=0), color=False).lower())

    def test_says_why_each_skipped_session_was_not_saved(self):
        m = dict(self.man(skipped=5), skippedWhy={"cwd is in a temp dir": 5})
        out = ccwho.render_restore(m, color=False)
        self.assertIn("5", out)
        self.assertIn("cwd is in a temp dir", out)
        self.assertNotIn("no id or no cwd", out, "that is not why these were left out")

    def test_an_empty_manifest_says_so_rather_than_printing_nothing(self):
        out = ccwho.render_restore({"version": 1, "savedAt": 1, "count": 0,
                                    "skipped": 0, "sessions": []}, color=False)
        self.assertTrue(out.strip(), "an empty restore must still say something")
        self.assertIn("no sessions", out.lower())

    def test_junk_does_not_crash(self):
        for junk in (None, [], "nope", {"sessions": "nope"}):
            self.assertTrue(ccwho.render_restore(junk, color=False).strip(), repr(junk))


class AppleScriptQuoting(unittest.TestCase):
    def test_a_plain_string_round_trips(self):
        self.assertEqual(ccwho.applescript_str("cd /tmp"), '"cd /tmp"')

    def test_a_double_quote_cannot_close_the_literal(self):
        # AppleScript has no \' escape; an unescaped " ends the string and the rest
        # becomes code. This is the whole reason the helper exists.
        self.assertEqual(ccwho.applescript_str('say "hi"'), '"say \\"hi\\""')

    def test_a_backslash_is_escaped_first_so_it_cannot_eat_the_quote_escape(self):
        self.assertEqual(ccwho.applescript_str('a\\"b'), '"a\\\\\\"b"')

    def test_a_newline_cannot_inject_a_second_statement(self):
        self.assertNotIn("\n", ccwho.applescript_str("a\nb"))


class ItermOpenScript(unittest.TestCase):
    entries = [{"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef", "cwd": "/Users/x/p/liveapp"},
               {"sessionId": "a1b2c3d4-1111-4222-8333-abcdefabcdef", "cwd": "/Users/x/p/football"}]

    def test_one_window_per_session(self):
        s = ccwho.iterm_open_script(self.entries)
        self.assertEqual(s.count("create window with default profile"), 2)

    def test_each_window_runs_that_session_s_resume_line(self):
        s = ccwho.iterm_open_script(self.entries)
        self.assertIn("claude --resume 4f2b91ac-1111-4222-8333-abcdefabcdef", s)
        self.assertIn("claude --resume a1b2c3d4-1111-4222-8333-abcdefabcdef", s)

    def test_a_cwd_with_a_quote_is_escaped_into_the_applescript_literal(self):
        s = ccwho.iterm_open_script([{"sessionId": "a1b2c3d4-1111-4222-8333-abcdefabcdef",
                                      "cwd": '/tmp/a"; do shell script "touch /tmp/pwned'}])
        for line in s.splitlines():
            if "write text" in line:
                self.assertEqual(line.count('"') % 2, 0, "unbalanced quotes: literal escaped early")
                self.assertNotIn('do shell script "touch', line.replace('\\"', "'"))

    def test_a_session_that_cannot_build_a_command_is_dropped_not_emitted_broken(self):
        s = ccwho.iterm_open_script([{"sessionId": "", "cwd": "/tmp"}] + self.entries)
        self.assertEqual(s.count("create window with default profile"), 2)

    def test_nothing_to_open_yields_no_script(self):
        self.assertEqual(ccwho.iterm_open_script([]), "")


# A restore used to open one new window per session, and the person then moved
# each one by hand into the panes iTerm2 had restored. iTerm2 keeps a pane's
# `unique id` when it restores its windows (PTYSession.m adopts the saved
# "Session GUID" on window restoration and on the startup arrangement), so a
# save can name the pane and a restore can fill it.
class ItermPanes(unittest.TestCase):
    def test_the_script_asks_every_pane_for_its_tty_id_and_name(self):
        s = ccwho.iterm_panes_script()
        for word in ("tty", "unique id", "name", "every tab"):
            self.assertIn(word, s)

    def test_a_line_is_tty_id_and_name(self):
        got = ccwho.parse_panes("/dev/ttys045\tGUID-1\t✳ the reaper\n")
        self.assertEqual(got, {"ttys045": {"pane": "GUID-1", "name": "✳ the reaper"}})

    def test_a_pane_with_no_name_is_still_a_pane(self):
        got = ccwho.parse_panes("/dev/ttys045\tGUID-1\t\n")
        self.assertEqual(got["ttys045"]["pane"], "GUID-1")

    def test_junk_and_a_line_with_no_id_are_skipped(self):
        got = ccwho.parse_panes("garbage\n/dev/ttys001\t\tname\n/dev/ttys002\tG2\tn\n")
        self.assertEqual(list(got), ["ttys002"])


class IdleTtys(unittest.TestCase):
    LOGIN = ("/usr/bin/login -fpl someone /Applications/iTerm.app/Contents/MacOS/"
             "ShellLauncher --launch_shell")

    def ps(self, *rows):
        return "  PID TTY      COMMAND\n" + "".join(
            "%d %s %s\n" % (100 + i, tty, cmd) for i, (tty, cmd) in enumerate(rows))

    def test_a_login_and_its_shell_is_idle(self):
        out = self.ps(("ttys045", self.LOGIN), ("ttys045", "-zsh"))
        self.assertEqual(ccwho.idle_ttys(out), {"ttys045"})

    def test_a_pane_running_anything_else_is_not(self):
        out = self.ps(("ttys045", self.LOGIN), ("ttys045", "-zsh"),
                      ("ttys045", "claude --resume x"),
                      ("ttys001", self.LOGIN), ("ttys001", "-zsh"),
                      ("ttys001", "caffeinate -s"),
                      ("ttys002", self.LOGIN), ("ttys002", "-bash"))      # control
        self.assertEqual(ccwho.idle_ttys(out), {"ttys002"})

    def test_a_shell_running_a_script_is_not_idle(self):
        out = self.ps(("ttys003", "/bin/zsh ./deploy.sh"), ("ttys004", "fish"))
        self.assertEqual(ccwho.idle_ttys(out), {"ttys004"})

    def test_no_terminal_is_no_pane(self):
        self.assertEqual(ccwho.idle_ttys(self.ps(("??", "-zsh"))), set())


class ManifestRecordsThePane(unittest.TestCase):
    def row(self, **kw):
        base = dict(sessionId="4f2b91ac-1111-4222-8333-abcdefabcdef",
                    cwd="/Users/x/projects/liveapp", project="liveapp",
                    tty="ttys032", tab_title="✳ the reaper")
        base.update(kw)
        return base

    def test_the_pane_showing_a_session_is_saved_with_it(self):
        panes = {"ttys032": {"pane": "GUID-1", "name": "✳ the reaper"}}
        s = ccwho.manifest_from_rows([self.row()], now=1, panes=panes)["sessions"][0]
        self.assertEqual(s["pane"], "GUID-1")
        self.assertEqual(s["tabTitle"], "✳ the reaper")

    def test_no_pane_known_saves_an_empty_one(self):                     # control
        s = ccwho.manifest_from_rows([self.row(tty="ttys099")], now=1,
                                     panes={"ttys032": {"pane": "G", "name": ""}})
        self.assertEqual(s["sessions"][0]["pane"], "")

    def test_a_session_with_no_tty_takes_no_pane(self):
        s = ccwho.manifest_from_rows([self.row(tty="")], now=1,
                                     panes={"": {"pane": "G", "name": ""}})
        self.assertEqual(s["sessions"][0]["pane"], "")


class MatchPanes(unittest.TestCase):
    A = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    B = "a1b2c3d4-1111-4222-8333-abcdefabcdef"

    def e(self, sid, pane="", title=""):
        return {"sessionId": sid, "cwd": "/x", "pane": pane, "tabTitle": title}

    def test_a_restored_idle_pane_is_matched_by_its_id(self):
        panes = {"ttys050": {"pane": "G-A", "name": "whatever it says now"}}
        got = ccwho.match_panes([self.e(self.A, "G-A")], panes, {"ttys050"})
        self.assertEqual(got, {self.A: "G-A"})

    def test_a_busy_pane_is_never_written_into(self):
        panes = {"ttys050": {"pane": "G-A", "name": "t"}}
        got = ccwho.match_panes([self.e(self.A, "G-A", "t")], panes, set())
        self.assertEqual(got, {})

    def test_a_new_id_falls_back_to_the_one_pane_with_that_title(self):
        panes = {"ttys050": {"pane": "NEW", "name": "✳ reaper"},
                 "ttys051": {"pane": "OTHER", "name": "zsh"}}
        got = ccwho.match_panes([self.e(self.A, "OLD", "✳ reaper")], panes,
                                {"ttys050", "ttys051"})
        self.assertEqual(got, {self.A: "NEW"})

    def test_the_status_mark_claude_puts_before_its_title_is_ignored(self):
        # measured: "✳ topic" at rest, "◐ topic" / "◑ topic" while it works - the
        # mark at the moment of the save need not be the one iTerm2 restored
        panes = {"ttys050": {"pane": "NEW", "name": "✳ Laptop crash (claude)"}}
        got = ccwho.match_panes([self.e(self.A, "OLD", "◐ Laptop crash (claude)")], panes,
                                {"ttys050"})
        self.assertEqual(got, {self.A: "NEW"})

    def test_a_different_title_after_the_mark_is_no_match(self):        # control
        panes = {"ttys050": {"pane": "NEW", "name": "✳ Laptop crash (claude)"}}
        got = ccwho.match_panes([self.e(self.A, "OLD", "◐ Laptop fan (claude)")], panes,
                                {"ttys050"})
        self.assertEqual(got, {})

    def test_a_bare_mark_is_no_title(self):
        panes = {"ttys050": {"pane": "NEW", "name": "✳ "}}
        got = ccwho.match_panes([self.e(self.A, "OLD", "◐")], panes, {"ttys050"})
        self.assertEqual(got, {})

    def test_a_busy_pane_with_that_title_makes_it_ambiguous(self):
        panes = {"ttys050": {"pane": "P1", "name": "X"}, "ttys051": {"pane": "P2", "name": "X"}}
        got = ccwho.match_panes([self.e(self.A, "OLD", "X")], panes, {"ttys051"})
        self.assertEqual(got, {})

    def test_a_session_matched_by_id_still_counts_for_its_title(self):
        panes = {"ttys050": {"pane": "P1", "name": "X"}, "ttys051": {"pane": "P2", "name": "X"}}
        got = ccwho.match_panes([self.e(self.A, "P1", "X"), self.e(self.B, "OLD", "X")],
                                panes, {"ttys050", "ttys051"})
        self.assertEqual(got, {self.A: "P1"})

    def test_a_saved_session_not_being_opened_counts_for_its_title(self):
        # B is running already, so only A is opened - but "X" was B's title too
        panes = {"ttys050": {"pane": "P2", "name": "X"}}
        got = ccwho.match_panes([self.e(self.A, "OLD", "X")], panes, {"ttys050"},
                                saved=[self.e(self.A, "OLD", "X"), self.e(self.B, "PB", "X")])
        self.assertEqual(got, {})

    def test_one_pane_id_on_two_ttys_is_never_matched_by_id(self):
        # two panes given one saved id would both resume the session: a fork
        panes = {"ttys050": {"pane": "G-A", "name": "a"}, "ttys051": {"pane": "G-A", "name": "b"}}
        got = ccwho.match_panes([self.e(self.A, "G-A", "a")], panes, {"ttys050", "ttys051"})
        self.assertEqual(got, {})

    def test_one_pane_id_on_two_ttys_is_never_matched_by_title(self):
        panes = {"ttys050": {"pane": "NEW", "name": "X"}, "ttys051": {"pane": "NEW", "name": "Y"}}
        got = ccwho.match_panes([self.e(self.A, "OLD", "X")], panes, {"ttys050", "ttys051"})
        self.assertEqual(got, {})

    def test_two_panes_with_that_title_is_no_match(self):
        panes = {"ttys050": {"pane": "N1", "name": "claude"},
                 "ttys051": {"pane": "N2", "name": "claude"}}
        got = ccwho.match_panes([self.e(self.A, "", "claude")], panes, {"ttys050", "ttys051"})
        self.assertEqual(got, {})

    def test_two_sessions_with_one_title_is_no_match(self):
        panes = {"ttys050": {"pane": "N1", "name": "claude"}}
        got = ccwho.match_panes([self.e(self.A, "", "claude"), self.e(self.B, "", "claude")],
                                panes, {"ttys050"})
        self.assertEqual(got, {})

    def test_a_pane_matched_by_id_is_not_given_away_by_title(self):
        panes = {"ttys050": {"pane": "G-A", "name": "same"}}
        got = ccwho.match_panes([self.e(self.A, "G-A", "x"), self.e(self.B, "", "same")],
                                panes, {"ttys050"})
        self.assertEqual(got, {self.A: "G-A"})

    def test_an_empty_title_matches_nothing(self):
        panes = {"ttys050": {"pane": "N1", "name": ""}}
        self.assertEqual(ccwho.match_panes([self.e(self.A)], panes, {"ttys050"}), {})

    def test_an_old_manifest_matches_nothing_and_opens_windows(self):
        panes = {"ttys050": {"pane": "N1", "name": "t"}}
        old = {"sessionId": self.A, "cwd": "/x"}
        self.assertEqual(ccwho.match_panes([old], panes, {"ttys050"}), {})


class ItermOpenScriptFillsPanes(unittest.TestCase):
    A = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    B = "a1b2c3d4-1111-4222-8333-abcdefabcdef"
    entries = [{"sessionId": A, "cwd": "/Users/x/p/liveapp"},
               {"sessionId": B, "cwd": "/Users/x/p/football"}]

    def test_a_filled_session_opens_no_window_and_the_other_does(self):
        s = ccwho.iterm_open_script(self.entries, fill={self.A: "G-A"})
        self.assertEqual(s.count("create window with default profile"), 1)
        self.assertIn('if u is "G-A"', s)
        self.assertIn("claude --resume " + self.A, s)
        self.assertIn("claude --resume " + self.B, s)

    def test_the_script_says_which_panes_it_wrote_into(self):
        s = ccwho.iterm_open_script(self.entries, fill={self.A: "G-A"})
        self.assertIn("return done", s)

    def test_a_pane_id_cannot_break_out_of_its_literal(self):
        s = ccwho.iterm_open_script(self.entries, fill={self.A: 'x" then do shell script "id'})
        checked = [l for l in s.splitlines() if " u is " in l]
        self.assertEqual(len(checked), 1)
        for line in checked:
            self.assertEqual(line.count('"') - line.count('\\"'), 2)

    def test_a_half_typed_line_is_cleared_before_the_resume_line(self):
        # `ps` cannot see "rm -rf " typed at a prompt; the resume line must not
        # be appended to it
        s = ccwho.iterm_open_script(self.entries, fill={self.A: "G-A", self.B: "G-B"})
        for sid in (self.A, self.B):
            at = s.index("claude --resume " + sid)
            branch = s[s.rindex("unique id", 0, at):at]
            # ^E first: ^U clears only left of the cursor in bash, fish and vi-insert
            self.assertIn("write text ((ASCII character 5) & (ASCII character 21)) newline no",
                          branch)

    def test_a_pane_id_is_written_at_most_once(self):
        s = ccwho.iterm_open_script(self.entries, fill={self.A: "G-A"})
        self.assertIn("if done does not contain u then", s)
        self.assertLess(s.index("if done does not contain u then"), s.index('if u is "G-A"'))

    def test_one_pane_failing_does_not_lose_what_was_written(self):
        # a pane that closes mid-loop must not abort the script: the ids already
        # written are only reported if the script reaches `return done`
        s = ccwho.iterm_open_script(self.entries, fill={self.A: "G-A"})
        start, end = s.index("      repeat with s in sessions of t"), s.index("    end repeat")
        loop = s[start:end]
        self.assertLess(loop.index("try"), loop.index("set u to unique id of s"))
        self.assertGreater(loop.rindex("end try"), loop.rindex("set done to done"))

    def test_each_pane_id_is_read_once(self):
        s = ccwho.iterm_open_script(self.entries, fill={self.A: "G-A", self.B: "G-B"})
        self.assertEqual(s.count("unique id of s"), 1)

    def test_the_filled_ids_are_returned_after_the_windows_open(self):
        s = ccwho.iterm_open_script(self.entries, fill={self.A: "G-A"})
        self.assertGreater(s.index("return done"), s.rindex("create window"))

    @unittest.skipUnless(shutil.which("osacompile"), "macOS only")
    def test_the_script_compiles(self):
        for fill in ({self.A: "G-A", self.B: "G-B"}, {self.A: "G-A"}, {}):
            s = ccwho.iterm_open_script(self.entries, fill=fill)
            r = subprocess.run(["osacompile", "-o", os.devnull, "-e", s],
                               capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_only_filled_sessions_still_make_a_script(self):
        s = ccwho.iterm_open_script(self.entries[:1], fill={self.A: "G-A"})
        self.assertNotIn("create window", s)
        self.assertIn("claude --resume " + self.A, s)


class RestoreLabelling(unittest.TestCase):
    """A restore list whose rows read '/compact' and 'go ahead' identifies nothing.
    Measured on the real fleet: 3 of 17 rows were useless with `topic` alone, and 1
    of 17 would be useless with `first` alone ('restart from disk'). So: both."""

    def man(self, first, topic):
        return {"version": 1, "savedAt": 1, "count": 1, "skipped": 0, "sessions": [
            {"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef", "cwd": "/p",
             "project": "p", "first": first, "topic": topic, "ask": ""}]}

    def test_shows_both_when_they_differ_and_says_which_is_which(self):
        out = ccwho.render_restore(self.man("something is wrong with docker", "/compact"),
                                   color=False)
        self.assertIn("something is wrong with docker", out)
        self.assertIn("/compact", out)
        self.assertIn("opened", out.lower())
        self.assertIn("latest", out.lower())

    def test_the_useful_half_survives_when_the_opening_line_is_junk(self):
        out = ccwho.render_restore(self.man("restart from disk", "Daily engagement scan"),
                                   color=False)
        self.assertIn("Daily engagement scan", out)

    def test_one_line_when_they_are_the_same(self):
        out = ccwho.render_restore(self.man("same thing", "same thing"), color=False)
        self.assertEqual(out.count("same thing"), 1)

    def test_a_session_with_neither_half_prints_no_stray_blank_line(self):
        """Found by mutation, not by review: `elif opened or latest:` -> `elif True:`
        stayed green, because nothing asserted the both-empty case."""
        out = ccwho.render_restore(self.man("", ""), color=False)
        body = [ln for ln in out.splitlines() if ln.startswith("      ")]
        self.assertEqual([ln for ln in body if not ln.strip()], [],
                         "a session with no topic must not emit an empty indented line")

    def test_an_empty_half_is_not_printed_as_a_blank_label(self):
        out = ccwho.render_restore(self.man("", "only this"), color=False)
        self.assertIn("only this", out)
        self.assertNotIn("opened:", out)
        out2 = ccwho.render_restore(self.man("only that", ""), color=False)
        self.assertIn("only that", out2)
        self.assertNotIn("latest:", out2)


class CheckManifest(unittest.TestCase):
    """The point of a restore manifest is that you find out it works BEFORE the
    reboot, not after. --check answers that without opening anything."""

    GOOD = {"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef", "cwd": "/p/liveapp",
            "project": "liveapp", "first": "f", "topic": "t", "ask": ""}

    def check(self, sessions, dirs=("/p/liveapp",), transcripts=("4f2b91ac-1111-4222-8333-abcdefabcdef",)):
        return ccwho.check_manifest({"version": 1, "sessions": list(sessions)},
                                    cwd_exists=lambda p: p in dirs,
                                    transcript_for=lambda sid: "/tx" if sid in transcripts else None)

    def test_a_healthy_manifest_has_no_problems(self):
        ok, problems = self.check([self.GOOD])
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    def test_a_cwd_that_no_longer_exists_is_reported(self):
        # a reaped worktree is the realistic case: 56 of them under ~/.liveapp-wt
        gone = dict(self.GOOD, cwd="/p/reaped-worktree", project="wt")
        ok, problems = self.check([gone])
        self.assertFalse(ok)
        self.assertEqual(len(problems), 1)
        self.assertIn("wt", problems[0][0])
        self.assertIn("cwd", problems[0][1].lower())

    def test_a_transcript_that_is_gone_is_reported(self):
        orphan = dict(self.GOOD, sessionId="ffffffff-1111-4222-8333-abcdefabcdef", project="ghost")
        ok, problems = self.check([orphan], dirs=("/p/liveapp",))
        self.assertFalse(ok)
        self.assertIn("transcript", problems[0][1].lower())

    def test_an_unbuildable_resume_line_is_reported(self):
        """The cwd exists AND the transcript resolves, so ONLY the resume-line check
        can fire. The first version let the cwd and transcript checks catch this one
        instead, and matched on the word "resume" - which the TRANSCRIPT message also
        contains ("nothing left to resume"), so deleting the guard stayed green.
        Found by mutation."""
        bad = dict(self.GOOD, sessionId="not a valid id", project="bad")
        ok, problems = self.check([bad], transcripts=("not a valid id",))
        self.assertFalse(ok)
        self.assertEqual(len(problems), 1)
        self.assertIn("no resume line", problems[0][1].lower())

    def test_it_reports_every_problem_not_just_the_first(self):
        a = dict(self.GOOD, cwd="/gone", project="a")
        b = dict(self.GOOD, sessionId="zzzzzzzz-1111-4222-8333-abcdefabcdef", project="b")
        ok, problems = self.check([a, b, self.GOOD])
        self.assertFalse(ok)
        self.assertEqual(len(problems), 2, "a partial report hides the session you would lose")

    def test_an_empty_manifest_is_not_a_pass(self):
        ok, problems = self.check([])
        self.assertFalse(ok, "nothing to restore is a failed check, not a clean bill of health")

    def test_junk_does_not_crash(self):
        for junk in (None, [], "nope", {"sessions": "nope"}):
            ok, problems = ccwho.check_manifest(junk, cwd_exists=lambda p: True,
                                                transcript_for=lambda s: "/tx")
            self.assertFalse(ok, repr(junk))


class TestSessionSourceAvailability(MachinelessCollect):
    """`[]` and "could not ask" are DIFFERENT answers.

    Conflating them is how a scheduled save writes "you had nothing open" over the
    record of what you did have open. agents_json must say which one it is.
    """

    def setUp(self):
        super().setUp()
        self.real_which = ccwho.shutil.which
        self.real_run = ccwho.subprocess.run
        self.real_exists = ccwho.os.path.exists

    def tearDown(self):
        super().tearDown()
        ccwho.shutil.which = self.real_which
        ccwho.subprocess.run = self.real_run
        ccwho.os.path.exists = self.real_exists

    def nowhere(self):
        """claude is not on PATH AND not where it installs itself. Stubbing
        only which() stopped being the whole failure when ccwho started looking
        in ~/.local/bin - a window with no login shell has no PATH worth the
        name, and that is exactly where claude lives."""
        ccwho.shutil.which = lambda name: None
        ccwho.os.path.exists = lambda p: False

    class _Done:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    def test_binary_missing_is_none_not_empty_list(self):
        self.nowhere()
        self.assertIsNone(ccwho.agents_json())

    def test_nonzero_exit_is_none(self):
        ccwho.shutil.which = lambda name: "/bin/claude"
        ccwho.subprocess.run = lambda *a, **k: self._Done(1, "")
        self.assertIsNone(ccwho.agents_json())

    def test_launch_failure_is_none(self):
        ccwho.shutil.which = lambda name: "/bin/claude"

        def boom(*a, **k):
            raise OSError("no exec")

        ccwho.subprocess.run = boom
        self.assertIsNone(ccwho.agents_json())

    def test_a_genuine_empty_answer_is_still_empty_list(self):
        ccwho.shutil.which = lambda name: "/bin/claude"
        ccwho.subprocess.run = lambda *a, **k: self._Done(0, "[]")
        self.assertEqual(ccwho.agents_json(), "[]")

    def test_collect_reports_the_source_to_its_caller(self):
        self.nowhere()
        st = {}
        ccwho.collect(cache={}, status=st)
        self.assertIs(st.get("source_ok"), False)

    def test_collect_reports_a_working_source(self):
        ccwho.shutil.which = lambda name: "/bin/claude"
        ccwho.subprocess.run = lambda *a, **k: self._Done(0, "[]")
        st = {}
        ccwho.collect(cache={}, status=st)
        self.assertIs(st.get("source_ok"), True)


class TestOpenUrls(unittest.TestCase):
    """A link in the restore list has to survive the session it points at.

    The tty link answers "where is this window". This one answers "get me to this
    session", which is a different question the moment the window is gone - and
    after a reboot every window is gone, which is exactly when the restore list is
    the thing you are reading.
    """

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"

    def test_builds_an_open_url_for_a_session(self):
        self.assertEqual(ccwho.open_url({"sessionId": self.SID}),
                         "ccwho://open/" + self.SID)

    def test_no_url_without_a_session_id(self):
        self.assertEqual(ccwho.open_url({"sessionId": ""}), "")
        self.assertEqual(ccwho.open_url({}), "")

    def test_a_bogus_session_id_gets_no_url(self):
        for junk in ("../../etc/passwd", "a b", "'; rm -rf /", "short"):
            self.assertEqual(ccwho.open_url({"sessionId": junk}), "", junk)

    def test_parses_both_verbs(self):
        self.assertEqual(ccwho.parse_ccwho_url("ccwho://open/" + self.SID),
                         ("open", self.SID))
        self.assertEqual(ccwho.parse_ccwho_url("ccwho://jump/s032"), ("jump", "s032"))

    def test_rejects_anything_else(self):
        for bad in ("https://example.com", "ccwho://delete/all", "ccwho://open/nope",
                    "", None, "ccwho://open/", "file:///etc/passwd"):
            self.assertEqual(ccwho.parse_ccwho_url(bad), ("", ""), repr(bad))


class TestResolveOpen(unittest.TestCase):
    """Live -> focus the window. Dead -> reopen it. Unknown -> say so."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    LIVE = [{"sessionId": SID, "tty": "ttys032", "pid": 4242}]
    ENTRY = {"sessionId": SID, "cwd": "/Users/x/p/liveapp", "project": "liveapp"}

    def test_a_live_session_is_a_jump(self):
        action, value = ccwho.resolve_open(self.SID, self.LIVE, [])
        self.assertEqual(action, "jump")
        self.assertEqual(value, "s032")

    def test_a_dead_session_in_the_manifest_is_a_resume(self):
        action, value = ccwho.resolve_open(self.SID, [], [self.ENTRY])
        self.assertEqual(action, "resume")
        self.assertIn("claude --resume " + self.SID, value)
        self.assertTrue(value.startswith("cd /Users/x/p/liveapp"))

    def test_live_beats_the_manifest(self):
        # Reopening a session that is already running would fork the conversation.
        action, _ = ccwho.resolve_open(self.SID, self.LIVE, [self.ENTRY])
        self.assertEqual(action, "jump")

    def test_a_session_nobody_knows_is_missing(self):
        action, value = ccwho.resolve_open("deadbeef-0000-0000-0000-000000000000",
                                           self.LIVE, [self.ENTRY])
        self.assertEqual(action, "missing")

    def test_a_live_session_with_no_tty_is_not_reopened(self):
        # It used to fall back to "resume", which forks a session that is running.
        # No window to focus is not the same fact as no session to open.
        live = [{"sessionId": self.SID, "tty": "", "pid": 91}]
        action, value = ccwho.resolve_open(self.SID, live, [self.ENTRY])
        self.assertEqual(action, "attach", "a running session is opened, not reopened")
        self.assertIn("claude attach", value)
        self.assertNotIn("--resume", value)


class TestRestoreListLinks(unittest.TestCase):
    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    MAN = {"version": 1, "savedAt": 1788213090, "count": 1, "skipped": 0,
           "sessions": [{"sessionId": SID, "cwd": "/Users/x/p/liveapp",
                         "project": "liveapp", "topic": "the reaper", "first": "",
                         "ask": "", "tty": "s032", "since": "2h",
                         "status": "waiting", "attention": "asks", "pid": 1}]}

    def test_links_off_emits_no_escape(self):
        out = ccwho.render_restore(self.MAN, color=False, links=False)
        self.assertNotIn("\033]8;;", out)
        self.assertNotIn("ccwho://open/", out)

    def test_links_on_emits_a_clickable_open_url(self):
        out = ccwho.render_restore(self.MAN, color=False, links=True)
        self.assertIn("\033]8;;ccwho://open/" + self.SID, out)
        self.assertIn("liveapp", out)

    def test_links_default_off_so_piped_output_stays_plain(self):
        self.assertNotIn("\033]8;;", ccwho.render_restore(self.MAN, color=False))


class TestParseSessionsSaysWhenItCouldNotParse(unittest.TestCase):
    """A feed we could not parse is NOT "you have nothing open".

    `agents_json()` already draws the line between "claude said []" and "we could
    not ask". That line was undone one layer down: parse_sessions() turned every
    malformed answer into [], so garbage on stdout - a partial write, a `{}`, a
    prompt leaking into the output - reached the callers as an empty fleet. The
    callers act on an empty fleet: `save` records it, `open` reopens what it finds
    in a manifest. Reopening a session that is running forks the conversation.
    """

    def test_a_valid_list_parses(self):
        raw = json.dumps([{"sessionId": "a-1", "pid": 1}])
        self.assertEqual(ccwho.parse_sessions(raw), [{"sessionId": "a-1", "pid": 1}])

    def test_empty_list_is_a_real_answer(self):
        self.assertEqual(ccwho.parse_sessions("[]"), [])   # control: genuinely nothing open

    def test_not_json_is_unparseable(self):
        self.assertIsNone(ccwho.parse_sessions("not json"))

    def test_an_object_is_not_a_session_list(self):
        self.assertIsNone(ccwho.parse_sessions("{}"))

    def test_a_feed_of_nothing_usable_is_unparseable(self):
        self.assertIsNone(ccwho.parse_sessions("[1]"))
        self.assertIsNone(ccwho.parse_sessions(json.dumps([{"pid": 1}])))

    def test_one_unreadable_record_does_not_hide_the_readable_ones(self):
        # A future claude may add a record shape we do not know. Blanking the
        # dashboard over it is the wrong trade: SHOW what parsed. Acting on a
        # partial picture is the part that is unsafe, and that is a second fact.
        raw = json.dumps([{"sessionId": "a-1", "pid": 1}, {"pid": 2}])
        self.assertEqual(ccwho.parse_sessions(raw), [{"sessionId": "a-1", "pid": 1}])
        self.assertFalse(ccwho.read_is_complete(raw), "we did not understand all of it")

    def test_a_feed_we_understood_entirely_is_complete(self):
        raw = json.dumps([{"sessionId": "a-1", "pid": 1}])
        self.assertTrue(ccwho.read_is_complete(raw))          # control

    def test_none_in_none_out(self):
        self.assertIsNone(ccwho.parse_sessions(None))


class TestTranscriptsAreFoundUnderEveryConfigRoot(unittest.TestCase):
    """Sessions do not all run under the same login. Some use ~/.claude, some a
    CLAUDE_CODE_OAUTH_TOKEN, and CLAUDE_CONFIG_DIR moves a session's whole
    directory. ccwho reads files and never logs in - so the only thing that could
    tie it to one login is a hard-coded path, and this is where that was."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sid = "abcd1234-0000-4000-8000-00000000abcd"
        d = os.path.join(self.tmp, "other-config", "projects", "-Users-x-football")
        os.makedirs(d)
        self.path = os.path.join(d, self.sid + ".jsonl")
        with open(self.path, "w") as fh:
            fh.write('{"type":"user","message":{"role":"user","content":"hi"}}\n')
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, "other-config")

    def tearDown(self):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_transcript_in_another_config_dir_is_found(self):
        self.assertEqual(ccwho.transcript_path(self.sid), self.path)

    def test_a_session_nobody_has_a_transcript_for_is_nothing(self):   # control
        self.assertIsNone(
            ccwho.transcript_path("0000dead-0000-4000-8000-000000000000"))

    def test_reading_its_windows_works_the_same(self):
        head, tail, mtime = ccwho.read_windows(self.sid)
        self.assertTrue(head or tail)
        self.assertGreater(mtime, 0)


class TestTerminalNames(unittest.TestCase):
    """The name you can SEE is the one on the tab, and it is not the ai-title:
    Claude Code puts a status glyph in front of it and iTerm2 adds "(claude)".
    Matching by eye needs the same string, glyph and all.

    Measured 2026-09-21: iTerm2's AppleScript cannot report a tab's own title
    per session - both `current session of tab` and the `tab.title` variable
    resolve to the WINDOW's current tab (10 sessions in 10 different tabs all
    reported one title). A session's own `name` is correct, costs 0.18s in bulk
    against 1.6s per-session, and is what the tab displays whenever that session
    is the tab's active pane.
    """

    DUMP = ("/dev/ttys000\t\u25d1 Session discovery and management UX (claude)\n"
            "/dev/ttys012\tChat (claude)\n"
            "/dev/ttys017\t-zsh\n"
            "\n"
            "/dev/ttys099\n")           # a line with no name at all

    def test_it_maps_a_tty_to_the_name_on_the_tab(self):
        got = ccwho.parse_titles(self.DUMP)
        self.assertEqual(got["ttys000"], "\u25d1 Session discovery and management UX (claude)")
        self.assertEqual(got["ttys012"], "Chat (claude)")

    def test_a_shell_tab_is_named_too(self):
        self.assertEqual(ccwho.parse_titles(self.DUMP)["ttys017"], "-zsh")

    def test_junk_lines_are_skipped_not_fatal(self):
        got = ccwho.parse_titles(self.DUMP)
        self.assertNotIn("ttys099", got)
        self.assertEqual(len(got), 3)

    def test_nothing_from_iterm_is_an_empty_map(self):
        self.assertEqual(ccwho.parse_titles(""), {})
        self.assertEqual(ccwho.parse_titles(None), {})

    def test_the_script_asks_for_names_in_bulk(self):
        # one round trip for every session: the per-session form takes 1.6s
        script = ccwho.iterm_titles_script()
        self.assertIn("name of sessions", script)
        self.assertNotIn("variable named", script)


class TestTitlesAreCachedBetweenTicks(unittest.TestCase):
    """Asking iTerm2 costs ~0.5s of a 1.2s run, and tab names change far more
    slowly than a 5s watch tick. `--watch` once burned a full core by re-doing
    per-tick work; this is the same mistake waiting to happen."""

    def setUp(self):
        self.calls = []
        self.real = ccwho.titles_snapshot

        def counted(timeout=5.0):
            self.calls.append(1)
            return {"ttys022": "\u2733 Issue 362 (claude)"}

        ccwho.titles_snapshot = counted

    def tearDown(self):
        ccwho.titles_snapshot = self.real

    def test_a_second_call_inside_the_window_reuses_the_answer(self):
        cache = {}
        a = ccwho.titles_cached(cache, now=1000.0)
        b = ccwho.titles_cached(cache, now=1000.0 + 5)
        self.assertEqual(a, b)
        self.assertEqual(len(self.calls), 1)

    def test_it_asks_again_once_the_window_has_passed(self):
        cache = {}
        ccwho.titles_cached(cache, now=1000.0)
        ccwho.titles_cached(cache, now=1000.0 + ccwho.TITLES_TTL + 0.1)
        self.assertEqual(len(self.calls), 2)

    def test_without_a_cache_it_always_asks(self):          # control
        ccwho.titles_cached(None, now=1000.0)
        ccwho.titles_cached(None, now=1000.0)
        self.assertEqual(len(self.calls), 2)

    def test_collect_does_not_evict_the_titles_while_tidying_up(self):
        # collect() drops cache entries for sessions that ended. The titles live
        # in the same dict and are not a session id, so a careless sweep deletes
        # them every tick and the cache never hits once.
        real_agents, real_ps, real_ttys, real_files = (
            ccwho.agents_json, ccwho.ps_snapshot, ccwho.tty_snapshot,
            ccwho.live_file_sessions)
        ccwho.agents_json = lambda: json.dumps(
            [{"sessionId": "aaa", "pid": 1, "cwd": "/x", "status": "idle"}])
        ccwho.ps_snapshot = lambda: ""
        ccwho.tty_snapshot = lambda: ""
        ccwho.live_file_sessions = lambda *a, **k: ([], 0)     # this machine's sessions stay out
        real_table, real_ports = ccwho.ps_table, ccwho.listen_ports
        ccwho.ps_table, ccwho.listen_ports = (lambda: {}), (lambda: {})
        self.addCleanup(setattr, ccwho, "ps_table", real_table)
        self.addCleanup(setattr, ccwho, "listen_ports", real_ports)
        try:
            cache = {}
            ccwho.collect(cache=cache)
            ccwho.collect(cache=cache)
        finally:
            (ccwho.agents_json, ccwho.ps_snapshot, ccwho.tty_snapshot,
             ccwho.live_file_sessions) = real_agents, real_ps, real_ttys, real_files
        self.assertEqual(len(self.calls), 1, "iTerm2 was asked twice in two ticks")

    def test_a_terminal_reused_by_another_session_is_not_given_the_old_name(self):
        # close a tab, open another on the same tty inside the cache window: the
        # new session would wear the old session's name for 15 seconds
        cache = {}
        ccwho.titles_cached(cache, ttys={"ttys022"}, now=1000.0)
        ccwho.titles_cached(cache, ttys={"ttys022", "ttys044"}, now=1000.0 + 1)
        self.assertEqual(len(self.calls), 2, "the set of terminals changed")

    def test_the_same_terminals_still_reuse_the_answer(self):       # control
        cache = {}
        ccwho.titles_cached(cache, ttys={"ttys022"}, now=1000.0)
        ccwho.titles_cached(cache, ttys={"ttys022"}, now=1000.0 + 1)
        self.assertEqual(len(self.calls), 1)

    def test_an_empty_answer_is_not_cached_as_the_truth(self):
        # iTerm2 starting up, or a timeout: retry on the next tick rather than
        # showing "~" fallbacks for the next quarter minute
        ccwho.titles_snapshot = lambda timeout=5.0: self.calls.append(1) or {}
        cache = {}
        ccwho.titles_cached(cache, now=1000.0)
        ccwho.titles_cached(cache, now=1000.0 + 1)
        self.assertEqual(len(self.calls), 2)


class TestRowCarriesTheNameYouSee(unittest.TestCase):
    SESSION = {"pid": 4242, "cwd": "/Users/x/projects/liveapp", "sessionId": "abc",
               "name": "liveapp-4e", "status": "idle", "startedAt": 1788200000000}
    TAIL = [json.dumps({"type": "ai-title", "aiTitle": "Issue 362"})]

    def test_the_terminal_name_wins_over_the_ai_title(self):
        row = ccwho.build_row(self.SESSION, [], self.TAIL, mtime=1788203600,
                              tty="ttys022", tab_title="\u2733 Issue 362 (claude)")
        self.assertEqual(row["tab_title"], "\u2733 Issue 362 (claude)")
        self.assertEqual(row["title"], "Issue 362", "the ai-title is still there")

    def test_without_a_terminal_name_the_row_says_so(self):
        row = ccwho.build_row(self.SESSION, [], self.TAIL, mtime=1788203600, tty="")
        self.assertEqual(row["tab_title"], "")

    def test_the_list_shows_the_name_you_see_and_marks_a_fallback(self):
        seen = ccwho.build_row(self.SESSION, [], self.TAIL, mtime=1788203600,
                               tty="ttys022", tab_title="\u2733 Issue 362 (claude)")
        out = ccwho.render([seen], 0, color=False, width=200)
        self.assertIn("\u2733 Issue 362 (claude)", out)
        self.assertNotIn("~Issue 362", out)
        unseen = ccwho.build_row(self.SESSION, [], self.TAIL, mtime=1788203600, tty="")
        out2 = ccwho.render([unseen], 0, color=False, width=200)
        self.assertIn("~Issue 362", out2,
                      "a title we could not read off the tab is marked, not faked")


class TestMatchRowsFindsEveryNameASessionHas(unittest.TestCase):
    """A session answers to five names: the tab title, a short id, the full
    session id, the message name and a tty/pid. Any of them has to find it, or
    the one you remember is the one that does not work - and `show` prints short
    ids in its ambiguity list, which were not searchable at all."""

    ROWS = [{"sessionId": "6c4c20f6-c6fb-46e5-bc8a-699f013cbe69", "tty": "ttys022",
             "pid": 90266, "name": "liveapp-f0", "title": "Issue 362",
             "project": "liveapp"},
            {"sessionId": "87f12e97-36d3-45be-87af-1ff776601651", "tty": "ttys016",
             "pid": 92723, "name": "ccwho-81", "title": "Session discovery",
             "project": "ccwho"}]

    def one(self, q):
        hits = ccwho.match_rows(self.ROWS, q)
        self.assertEqual(len(hits), 1, f"{q!r} -> {len(hits)} hits")
        return hits[0]

    def test_the_short_id_finds_it(self):
        self.assertEqual(self.one("6c4c")["project"], "liveapp")

    def test_the_full_session_id_finds_it(self):
        self.assertEqual(self.one("87f12e97-36d3-45be-87af-1ff776601651")["project"],
                         "ccwho")

    def test_the_message_name_finds_it(self):
        self.assertEqual(self.one("ccwho-81")["project"], "ccwho")

    def test_the_tty_and_the_pid_still_find_it(self):      # control
        self.assertEqual(self.one("s022")["project"], "liveapp")
        self.assertEqual(self.one("92723")["project"], "ccwho")

    def test_the_name_on_the_tab_finds_it(self):
        # the list shows the tab name, so that is the string you will type back
        rows = [dict(self.ROWS[0], tab_title="\u2733 Release triage (claude)")]
        self.assertEqual(len(ccwho.match_rows(rows, "release triage")), 1)

    def test_a_title_substring_still_finds_it(self):       # control
        self.assertEqual(self.one("362")["project"], "liveapp")

    def test_a_prefix_two_sessions_share_stays_ambiguous(self):
        rows = self.ROWS + [dict(self.ROWS[0],
                                 sessionId="6c4cffff-0000-4000-8000-000000000000",
                                 tty="ttys044", pid=1, name="liveapp-zz",
                                 title="something else")]
        self.assertEqual(len(ccwho.match_rows(rows, "6c4c")), 2,
                         "never guess between two sessions")


class TestResolveOpenNeverForksALiveSession(unittest.TestCase):
    """Reopening a session that is alive forks the conversation into two processes.

    Two ways in. A live session with no tty used to fall through to "resume" - but
    "I cannot see a window" is not "it is not running". And a caller whose source
    was unreachable sees an empty live list, which looks exactly like "nothing is
    running" unless it is told otherwise.
    """

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"
    LIVE = [{"sessionId": SID, "tty": "ttys032", "pid": 4242}]
    LIVE_NO_TTY = [{"sessionId": SID, "tty": "", "pid": 4242}]
    ENTRY = {"sessionId": SID, "cwd": "/Users/x/p/liveapp", "project": "liveapp"}

    def test_live_with_a_window_is_still_a_jump(self):
        self.assertEqual(ccwho.resolve_open(self.SID, self.LIVE, []),
                         ("jump", "s032"))

    def test_dead_and_in_a_manifest_is_still_a_resume(self):
        # control: the one case where reopening IS right must stay green
        action, value = ccwho.resolve_open(self.SID, [], [self.ENTRY])
        self.assertEqual(action, "resume")
        self.assertIn("claude --resume " + self.SID, value)

    def test_live_without_a_window_is_not_a_resume(self):
        action, value = ccwho.resolve_open(self.SID, self.LIVE_NO_TTY, [self.ENTRY])
        self.assertEqual(action, "attach")
        self.assertEqual(value, "claude attach " + self.SID[:8],
                         "the short id - the only one `claude attach` knows")
        self.assertNotIn("--resume", value)

    def test_an_unreadable_source_is_unknown_not_dead(self):
        action, why = ccwho.resolve_open(self.SID, [], [self.ENTRY], source_ok=False)
        self.assertEqual(action, "unknown")
        self.assertTrue(why, "the caller has to be able to say WHY it did nothing")

    def test_an_unreadable_source_does_not_even_jump(self):
        # rows from a failed read are not evidence of anything, in either direction
        action, _ = ccwho.resolve_open(self.SID, self.LIVE, [], source_ok=False)
        self.assertEqual(action, "unknown")

    def test_unknown_session_with_a_good_source_is_still_missing(self):
        action, _ = ccwho.resolve_open("deadbeef-0000-0000-0000-000000000000",
                                       self.LIVE, [self.ENTRY])
        self.assertEqual(action, "missing")


class TestCollectReportsAnUnparseableSource(MachinelessCollect):
    def test_a_partly_understood_feed_still_renders_but_blocks_launching(self):
        real = ccwho.agents_json
        ccwho.agents_json = lambda: json.dumps(
            [{"sessionId": "aaa", "pid": 1, "cwd": "/x", "status": "idle"}, {"pid": 2}])
        try:
            status = {}
            rows, _ = ccwho.collect(cache={}, status=status)
        finally:
            ccwho.agents_json = real
        self.assertEqual(len(rows), 1, "the session we understood is still shown")
        self.assertIs(status["source_ok"], False,
                      "an incomplete picture must not authorise opening anything")

    def test_garbage_from_the_feed_is_not_an_empty_fleet(self):
        real = ccwho.agents_json
        ccwho.agents_json = lambda: "{}"
        try:
            status = {}
            rows, _ = ccwho.collect(cache={}, status=status)
        finally:
            ccwho.agents_json = real
        self.assertEqual(rows, [])
        self.assertIs(status["source_ok"], False)

    def test_an_empty_feed_is_a_reachable_source(self):
        real = ccwho.agents_json
        ccwho.agents_json = lambda: "[]"
        try:
            status = {}
            rows, _ = ccwho.collect(cache={}, status=status)
        finally:
            ccwho.agents_json = real
        self.assertEqual(rows, [])
        self.assertIs(status["source_ok"], True)      # control


class TestCollectShowsNoDaemonRows(MachinelessCollect):
    """The spare and the parked terminal come from session files only; the
    list, the TUI and `ccwho save` all read collect's rows, so none may hold them."""

    JOB = "4e3efc1d-3639-4af3-91e9-6d6373c1cf94"
    SPARE = "49dc1790-be8a-4c68-98de-153189ebc981"
    PARKED = "fc509261-e383-4ed4-aacc-44087dc5a599"

    def collect(self, files):
        real = ccwho.agents_json
        ccwho.agents_json = lambda: json.dumps([
            {"pid": 3, "id": "4e3efc1d", "sessionId": self.JOB, "cwd": "/x",
             "kind": "background", "status": "idle"}])
        ccwho.live_file_sessions = lambda *a, **k: (files, 0)
        try:
            return ccwho.collect(cache={}, status={})[0]
        finally:
            ccwho.agents_json = real

    PARKED_ROW = {"pid": 2, "sessionId": PARKED, "cwd": "/x", "kind": "interactive",
                  "status": "busy", "parkedJobId": "4e3efc1d"}

    def test_a_parked_terminal_that_runs_is_never_resumed(self):
        # hidden is not closed: `claude --resume` on it would be a second
        # process on a live transcript. Its window shows the job: open that
        rows = self.collect([self.PARKED_ROW])
        action, value = ccwho.resolve_open(self.PARKED, rows,
                                           [{"sessionId": self.PARKED, "cwd": "/x"}])
        self.assertEqual((action, value), ("attach", "claude attach 4e3efc1d"))

    def test_a_parked_terminal_that_ended_is_resumed(self):          # control
        rows = self.collect([])
        action, _ = ccwho.resolve_open(self.PARKED, rows,
                                       [{"sessionId": self.PARKED, "cwd": "/x"}])
        self.assertEqual(action, "resume")

    def test_the_job_row_answers_to_the_parked_id(self):
        rows = self.collect([self.PARKED_ROW])
        self.assertEqual(rows[0]["parked"], [self.PARKED])
        self.assertEqual([r["sessionId"] for r in ccwho.match_rows(rows, "fc509261")],
                         [self.JOB])

    def test_what_the_parked_terminal_started_names_the_job(self):
        rows = self.collect([self.PARKED_ROW])
        rows[0].update(project="ccwho", title="TUI copy-paste")
        fleet = {"by_session": {self.PARKED: [{"pid": 9, "helper": False, "ports": [3000]}]}}
        listed = ccwho.ps_listing(rows, fleet)
        self.assertEqual(listed[0]["who"], "ccwho · TUI copy-paste")
        att = {"sessions": fleet["by_session"], "left_behind": [], "codex": []}
        self.assertEqual(ccwho._fleet(att, rows, True)["agent_ports"][0]["who"], "ccwho")

    def test_the_parked_terminal_counts_as_running(self):
        rows = self.collect([self.PARKED_ROW])
        self.assertEqual(ccwho.live_ids(rows), {self.JOB, self.PARKED})
        self.assertEqual(ccwho.live_ids(self.collect([])), {self.JOB})  # control

    def test_a_terminal_behind_a_chain_of_parked_jobs_is_never_resumed(self):
        mid = "11111111-2222-4333-8444-555555555555"
        # PARKED parked job aaaaaaaa (mid), and mid was parked again as 4e3efc1d
        rows = self.collect([
            dict(self.PARKED_ROW, parkedJobId="aaaaaaaa"),
            {"pid": 4, "sessionId": mid, "cwd": "/x", "kind": "background",
             "status": "idle", "id": "aaaaaaaa", "parkedJobId": "4e3efc1d"}])
        self.assertEqual([(r["sessionId"], r["parked"]) for r in rows],
                         [(self.JOB, sorted([self.PARKED, mid]))])
        action, _ = ccwho.resolve_open(self.PARKED, rows,
                                       [{"sessionId": self.PARKED, "cwd": "/x"}])
        self.assertNotEqual(action, "resume")

    def test_only_the_job_is_a_row(self):
        real = ccwho.agents_json
        ccwho.agents_json = lambda: json.dumps([
            {"pid": 3, "id": "4e3efc1d", "sessionId": self.JOB, "cwd": "/x",
             "kind": "background", "status": "idle"}])
        ccwho.live_file_sessions = lambda *a, **k: ([
            {"pid": 1, "sessionId": self.SPARE, "cwd": "/x", "kind": "background",
             "status": "idle", "id": "49dc1790", "spare": True},
            {"pid": 2, "sessionId": self.PARKED, "cwd": "/x", "kind": "interactive",
             "status": "busy", "parkedJobId": "4e3efc1d"}], 0)
        try:
            rows, _ = ccwho.collect(cache={}, status={})
        finally:
            ccwho.agents_json = real
        self.assertEqual([r["sessionId"] for r in rows], [self.JOB])


class TestRowsCarryTheRecap(unittest.TestCase):
    """The list's second line is the recap, so the row has to have one. It comes
    from the windows the engine already read for that session - no extra work."""

    SESSION = {"pid": 4242, "cwd": "/Users/x/projects/liveapp", "sessionId": "abc",
               "name": "liveapp-4e", "status": "idle", "startedAt": 1788200000000}

    def rows(self, tail):
        return ccwho.build_row(self.SESSION, [], tail, mtime=1788203600,
                               now=1788207200, tty="ttys022")

    def test_the_recap_and_its_age_are_on_the_row(self):
        tail = [json.dumps({"type": "system", "subtype": "away_summary",
                            "content": "Goal: fix the gate flake.",
                            "timestamp": "2026-09-18T10:00:00.000Z"}),
                json.dumps({"type": "user", "timestamp": "2026-09-18T12:00:00.000Z",
                            "message": {"role": "user", "content": "land it"}})]
        row = self.rows(tail)
        self.assertIn("gate flake", row["recap"])
        self.assertEqual(row["recap_ts"], "2026-09-18T10:00:00.000Z")
        self.assertTrue(row["recap_age"], "an age is not optional")
        self.assertEqual(row["turns_since_recap"], 1)

    def test_no_recap_is_empty_not_missing(self):          # control
        row = self.rows([])
        self.assertEqual(row["recap"], "")
        self.assertEqual(row["turns_since_recap"], 0)


class TestTheViewModelBehindTheUi(unittest.TestCase):
    """The TUI is a thin shell. What belongs on the screen - which group a session
    is in, what its two lines say, what a search matches - is decided here, where
    it can be tested without a terminal."""

    def rows(self):
        return [
            {"sessionId": "aaaa1111-0000-4000-8000-000000000001", "project": "liveapp",
             "attention": "busy", "title": "Release queue", "tab_title": "✳ Release queue",
             "tty": "ttys007", "since": "2m", "doing": "Bash: run the gate",
             "recap": "", "name": "liveapp-a1", "pid": 1, "ask": "", "topic": ""},
            {"sessionId": "bbbb2222-0000-4000-8000-000000000002", "project": "liveapp",
             "attention": "asks", "title": "Issue 362", "tab_title": "✳ Issue 362",
             "tty": "ttys022", "since": "5h", "doing": "Bash: read the verdict",
             "recap": "", "name": "liveapp-b2", "pid": 2,
             "ask": "Shall I land it?", "topic": ""},
            {"sessionId": "cccc3333-0000-4000-8000-000000000003", "project": "ccwho",
             "attention": "stopped", "title": "Session discovery", "tab_title": "",
             "tty": "", "since": "1d", "doing": "Edit: engine.py", "recap": "",
             "name": "ccwho-81", "pid": 3, "ask": "", "topic": ""},
            {"sessionId": "dddd4444-0000-4000-8000-000000000004", "project": "liveapp",
             "attention": "blocked", "title": "Docker", "tab_title": "✳ Docker",
             "tty": "ttys009", "since": "10m", "doing": "AskUserQuestion", "recap": "",
             "name": "liveapp-d4", "pid": 4, "ask": "", "topic": ""},
        ]

    def test_groups_are_what_each_one_needs_from_you(self):
        groups = ccwho.ui_groups(self.rows())
        self.assertEqual([g["heading"] for g in groups],
                         ["NEEDS YOU", "STOPPED", "BUSY"])
        self.assertEqual(len(groups[0]["rows"]), 2, "blocked and asks belong together")
        self.assertEqual(len(groups[1]["rows"]), 1)
        self.assertEqual(len(groups[2]["rows"]), 1)

    def test_an_empty_group_is_not_a_heading_over_nothing(self):
        groups = ccwho.ui_groups([r for r in self.rows() if r["attention"] == "busy"])
        self.assertEqual([g["heading"] for g in groups], ["BUSY"])

    def test_no_sessions_at_all_is_no_groups(self):
        self.assertEqual(ccwho.ui_groups([]), [])

    def test_the_first_line_is_what_you_see_on_the_tab(self):
        line = ccwho.ui_row_lines(self.rows()[1], width=100)[0]
        self.assertIn("bbbb", line)              # short id
        self.assertIn("✳ Issue 362", line)       # the name on the tab
        self.assertIn("5h", line)
        self.assertIn("s022", line)

    def test_the_second_line_is_the_recap_when_there_is_one(self):
        row = dict(self.rows()[1], recap="the gate flake, finally understood",
                   recap_age="2d", turns_since_recap=23)
        second = ccwho.ui_row_lines(row, width=100)[1]
        self.assertIn("gate flake", second)
        self.assertIn("2d", second, "a recap without its age hides how stale it is")

    def test_the_age_comes_before_the_recap_not_after_it(self):
        # at the end of a long recap the age is the first thing truncation eats,
        # and a recap whose age you cannot see reads as the current state
        row = dict(self.rows()[1], recap="x" * 400, recap_age="2d",
                   turns_since_recap=23)
        second = ccwho.ui_row_lines(row, width=90)[1]
        self.assertIn("2d", second)
        self.assertIn("23 turns", second)
        self.assertLess(second.index("2d"), second.index("xxx"))

    def test_without_a_recap_it_says_so_and_shows_the_last_action(self):
        second = ccwho.ui_row_lines(self.rows()[1], width=100)[1]
        self.assertIn("no recap yet", second)
        self.assertIn("read the verdict", second)

    def test_lines_never_wrap(self):
        row = dict(self.rows()[1], recap="x" * 500)
        for line in ccwho.ui_row_lines(row, width=60):
            self.assertLessEqual(ccwho.visible_len(line), 60, repr(line))

    # The third part is the session's name: the address another session sends a
    # message to. A cut name is no address at all.
    def _parts(self, row, width=100):
        return [text.strip() for text, _ in ccwho.ui_row_cells(row, width=width)[0]]

    def test_the_third_part_is_the_name_you_message_it_by(self):
        parts = self._parts(self.rows()[1])
        self.assertEqual(parts[2], "liveapp-b2")

    def test_a_long_name_is_never_cut(self):
        long = "update-landing-page-whatsapp-faq"
        row = dict(self.rows()[1], name=long)
        for width in (100, 70):
            with self.subTest(width=width):
                self.assertEqual(self._parts(row, width)[2], long)

    def test_a_long_name_takes_its_room_from_the_title_not_the_age(self):
        row = dict(self.rows()[1], name="update-landing-page-whatsapp-faq",
                   tab_title="✳ " + "a long title " * 8)
        line = ccwho.ui_row_lines(row, width=80)[0]
        self.assertIn("· 5h · s022", line)
        self.assertIn("…", line, "the title is what gives way")
        # all the way down to where the name and the age fit and nothing else:
        # the title gives way to nothing before the age or the account is cut
        for width in range(58, 81):
            with self.subTest(width=width):
                self.assertIn("· 5h · s022", ccwho.ui_row_lines(row, width=width)[0])
                first = ccwho.ui_row_cells(row, width=width, tag="1a2b")[0]
                self.assertIn("· 5h · s022", "".join(t for t, _ in first))
                # the account whole or not at all: "· 1…" is no account
                shown = [t for t, role in first if role == "account"]
                self.assertIn(shown, ([], [" · 1a2b"]))
                if width >= 65:             # where it fits, it is there
                    self.assertEqual(shown, [" · 1a2b"])
        for width in range(52, 58):
            with self.subTest(width=width):
                self.assertIn("· 5h", ccwho.ui_row_lines(row, width=width)[0])

    def test_a_name_too_long_for_the_line_is_not_followed_by_a_stray_title(self):
        for name in ("update-landing-page-whatsapp-faq", "把持久化文件移到状态目录把持久化"):
            with self.subTest(name=name):
                row = dict(self.rows()[1], name=name)
                first = ccwho.ui_row_cells(row, width=40)[0]
                self.assertNotIn(("…", "name"), first, first)
                self.assertTrue(all(ccwho.visible_len(t) or not t for t, _ in first))

    def test_a_tab_title_that_repeats_the_name_gives_way_to_the_real_title(self):
        row = dict(self.rows()[1], tab_title="liveapp-b2", title="Laptop crash")
        self.assertIn("~Laptop crash", ccwho.ui_row_lines(row, width=100)[0])
        row = dict(self.rows()[1], tab_title="✳ liveapp-b2", title="")
        line = ccwho.ui_row_lines(row, width=100)[0]
        self.assertEqual(line.count("liveapp-b2"), 1, line)
        row = dict(self.rows()[1], tab_title="✳ Issue 362")                 # control
        self.assertIn("✳ Issue 362", ccwho.ui_row_lines(row, width=100)[0])

    def test_the_project_is_only_the_start_of_a_name_up_to_a_dash(self):
        row = dict(self.rows()[1], project="app", name="apple-12")
        self.assertIn("· app ·", ccwho.ui_row_lines(row, width=100)[0])
        for name in ("app-12", "app"):                                      # control
            with self.subTest(name=name):
                line = ccwho.ui_row_lines(dict(row, name=name), width=100)[0]
                self.assertEqual(line.count("app"), 1, line)

    def test_no_project_is_not_a_question_mark(self):
        for project in ("", None, "?"):
            with self.subTest(project=project):
                row = dict(self.rows()[1], project=project, name="foo-1")
                self.assertNotIn("?", ccwho.ui_row_lines(row, width=100)[0])

    def test_a_name_cannot_break_the_row(self):
        for field in ("name", "tab_title", "title", "project"):
            with self.subTest(field=field):
                row = dict(self.rows()[1], tab_title="", name="x-1")
                row[field] = "a\nb\tc\x1b[31md"
                for text, _ in ccwho.ui_row_cells(row, width=100)[0]:
                    self.assertFalse(any(not c.isprintable() for c in text), repr(text))
        row = dict(self.rows()[1], name="a\nb")                             # control
        self.assertEqual(self._parts(row)[2], "a b")

    def test_a_blank_name_shows_the_project(self):
        for name in (" ", "\n", "\t "):
            with self.subTest(name=name):
                row = dict(self.rows()[1], name=name)
                self.assertEqual(self._parts(row)[2], "liveapp")
                self.assertNotIn("· liveapp", ccwho.ui_row_lines(row, width=100)[0])

    def test_the_meta_is_drawn_in_whole_pieces_after_a_whole_name(self):
        long = "update-landing-page-whatsapp-faq"
        row = dict(self.rows()[1], name=long)
        for width in range(40, 61):
            with self.subTest(width=width):
                first = ccwho.ui_row_cells(row, width=width)[0]
                meta = "".join(t for t, role in first if role == "meta")
                self.assertNotIn("…", meta, first)
                self.assertIn(meta, ("", " · 5h", " · 5h · s022"), first)
                for text, role in first:
                    if role != "project":
                        self.assertFalse(text.endswith("…"), first)
        line = ccwho.ui_row_lines(row, width=58)[0]                         # control
        self.assertIn("· 5h · s022", line)

    def test_with_no_title_a_name_that_just_fits_is_whole(self):
        for width in (40, 60):
            with self.subTest(width=width):
                row = dict(sessionId="abcd1234", attention="idle", since="5h",
                           project="liveapp", name="n" * (width - 12))
                line = ccwho.ui_row_lines(row, width=width)[0]
                self.assertIn("n" * (width - 12), line)
                self.assertNotIn("…", line)
                row["name"] = "n" * (width - 11)                            # control
                self.assertIn("…", ccwho.ui_row_lines(row, width=width)[0])

    def test_with_no_title_the_account_and_ports_take_the_room_there_is(self):
        row = dict(sessionId="abcd1234", attention="idle", name="?", project="liveapp",
                   tab_title="Short", tty="/dev/ttys012", since="5h")
        # "· abcd  liveapp · 5h · s012 · acct2" and a cell before the arrow: 40
        first = ccwho.ui_row_cells(row, width=40, tag="acct2")[0]
        self.assertIn((" · acct2", "account"), first)
        first = ccwho.ui_row_cells(row, width=39, tag="acct2")[0]          # control
        self.assertEqual([t for t, role in first if role == "account"], [])
        held = dict(row, ports=[3000])
        self.assertIn(" · :3000", ccwho.ui_row_lines(held, width=40)[0])
        self.assertNotIn(":3000", ccwho.ui_row_lines(held, width=39)[0])  # control

    def test_an_account_that_does_not_fit_leaves_its_room_to_the_ports(self):
        row = dict(sessionId="abcd1234", attention="idle", since="5h", project="",
                   name="update-landing-page-whatsapp-faq", tab_title="", title="",
                   tty="/dev/ttys012", ports=[3000])
        first = ccwho.ui_row_cells(row, width=65, tag="长账户")[0]
        self.assertIn(" · :3000", "".join(t for t, _ in first), first)
        self.assertEqual([t for t, role in first if role == "account"], [])
        first = ccwho.ui_row_cells(row, width=64, tag="长账户")[0]          # control
        self.assertNotIn(":3000", "".join(t for t, _ in first))
        self.assertIn(" · :3000", ccwho.ui_row_lines(row, width=65)[0])  # control

    def test_no_title_leaves_two_spaces_before_the_meta(self):
        row = dict(self.rows()[1], tab_title="", title="")
        self.assertIn("liveapp-b2  · 5h", ccwho.ui_row_lines(row, width=100)[0])

    def test_a_renamed_session_still_says_its_project(self):
        row = dict(self.rows()[1], name="update-landing-page-whatsapp-faq")
        line = ccwho.ui_row_lines(row, width=100)[0]
        self.assertIn("· liveapp", line)

    def test_an_auto_name_does_not_say_the_project_twice(self):
        line = ccwho.ui_row_lines(self.rows()[1], width=100)[0]
        self.assertEqual(line.count("liveapp"), 1, line)

    def test_the_name_is_not_said_twice_when_there_is_no_title(self):
        for tab_title, title in (("", ""), ("liveapp-b2", ""), ("", "liveapp-b2")):
            with self.subTest(tab_title=tab_title, title=title):
                row = dict(self.rows()[1], tab_title=tab_title, title=title)
                line = ccwho.ui_row_lines(row, width=100)[0]
                self.assertEqual(line.count("liveapp-b2"), 1, line)
        row = dict(self.rows()[1], tab_title="", title="Issue 362")          # control
        self.assertIn("~Issue 362", ccwho.ui_row_lines(row, width=100)[0])

    def test_no_name_shows_the_project(self):
        for name in ("", "?", None):
            with self.subTest(name=name):
                row = dict(self.rows()[1], name=name)
                self.assertEqual(self._parts(row)[2], "liveapp")

    def test_search_matches_any_of_the_names_and_the_recap(self):
        rows = self.rows()
        rows[1]["recap"] = "the gate flake"
        for q in ("bbbb", "issue 362", "s022", "liveapp-b2", "flake", "gate flake"):
            got = ccwho.ui_filter(rows, q)
            self.assertEqual([r["sessionId"] for r in got],
                             [rows[1]["sessionId"]], q)

    def test_every_word_has_to_match(self):
        rows = self.rows()
        self.assertEqual(ccwho.ui_filter(rows, "issue docker"), [])

    def test_an_empty_search_is_everything(self):          # control
        self.assertEqual(len(ccwho.ui_filter(self.rows(), "")), 4)


class TestFindingTheToolsWhenThereIsNoPath(unittest.TestCase):
    """Three times now the same fault: a process with no login shell has
    PATH=/usr/bin:/bin:/usr/sbin:/sbin. A launchd job gets that, and so does a
    window opened by a global hotkey, because iTerm2 was started by the Dock.
    `claude` and `uv` live in none of those directories, and the failure looks
    like an empty fleet or a window that closes on its own."""

    def setUp(self):
        self.real_which = ccwho.shutil.which
        self.real_exists = ccwho.os.path.exists
        self.addCleanup(setattr, ccwho.shutil, "which", self.real_which)
        self.addCleanup(setattr, ccwho.os.path, "exists", self.real_exists)

    def only(self, *paths):
        ccwho.shutil.which = lambda n: ""
        ccwho.os.path.exists = lambda p: p in paths

    def test_path_is_still_asked_first(self):                        # control
        ccwho.shutil.which = lambda n: "/somewhere/odd/" + n
        self.assertEqual(ccwho.find_tool("claude"), "/somewhere/odd/claude")

    def test_claude_is_found_with_no_path_at_all(self):
        self.only(os.path.expanduser("~/.local/bin/claude"))
        self.assertEqual(ccwho.find_tool("claude"),
                         os.path.expanduser("~/.local/bin/claude"))

    def test_homebrew_counts_too(self):
        self.only("/opt/homebrew/bin/claude")
        self.assertEqual(ccwho.find_tool("claude"), "/opt/homebrew/bin/claude")

    def test_a_tool_that_is_not_here_is_still_nothing(self):
        self.only()
        self.assertEqual(ccwho.find_tool("claude"), "")

    def test_the_session_list_is_read_from_a_window_with_no_path(self):
        self.only(os.path.expanduser("~/.local/bin/claude"))
        ran = []

        class Done:
            returncode = 0
            stdout = "[]"

        real_run = ccwho.subprocess.run
        ccwho.subprocess.run = lambda cmd, **k: ran.append(cmd) or Done()
        try:
            self.assertEqual(ccwho.agents_json(), "[]")
        finally:
            ccwho.subprocess.run = real_run
        self.assertEqual(ran[0][0], os.path.expanduser("~/.local/bin/claude"),
                         "called by its full path, not by a name PATH cannot resolve")

    def test_no_claude_anywhere_is_still_None_not_an_empty_fleet(self):
        self.only()
        self.assertIsNone(ccwho.agents_json(),
                          "nothing to ask is not the same as nothing open")


class TestWhatIsColouredAndWhatIsNot(unittest.TestCase):
    """The first screen was a wall of orange: state was painted over whole rows,
    and three of the four states used the same colour, so the colour told you
    nothing and the text was harder to read than plain text would have been.

    A row is now plain text with ONE coloured mark, and the recap under it is
    subordinate. The decision lives here, where it can be tested without a
    terminal; the UI only maps a role to a style.
    """

    def cells(self, **row):
        base = {"sessionId": "abcd1234", "project": "liveapp", "tab_title": "fix the queue",
                "attention": "stopped", "since": "6m", "tty": "ttys024",
                "recap": "the admission queue is released", "recap_age": "3h",
                "turns_since_recap": 4}
        base.update(row)
        return ccwho.ui_row_cells(base, width=100)

    def roles(self, line):
        return [role for _, role in line]

    def test_a_row_is_two_lines_of_parts(self):
        first, second = self.cells()
        self.assertTrue(first and second)
        for text, role in list(first) + list(second):
            self.assertIsInstance(text, str)
            self.assertIn(role, ccwho.UI_ROLES, role)

    def test_the_words_are_exactly_the_line_they_replace(self):       # control
        row = {"sessionId": "abcd1234", "project": "liveapp",
               "tab_title": "fix the queue", "attention": "stopped",
               "since": "6m", "tty": "ttys024", "recap": "released", "recap_age": "3h"}
        first, second = ccwho.ui_row_cells(row, width=100)
        self.assertEqual(("".join(t for t, _ in first),
                          "".join(t for t, _ in second)),
                         ccwho.ui_row_lines(row, width=100))

    def test_only_the_mark_carries_the_state(self):
        first, _ = self.cells(attention="waiting")
        marks = [t for t, role in first if role == "mark"]
        self.assertEqual(len(marks), 1)
        # everything else is ordinary text, not another colour to decode
        self.assertNotIn("state", self.roles(first))

    def test_the_whole_second_line_is_subordinate(self):
        _, second = self.cells()
        # the half of the detail arrow is a control, not words: it is not read
        self.assertEqual(set(self.roles(second)) - {"pad", "detail"}, {"age", "recap"},
                         "the recap reads as context, and its age stands out in it")

    def test_a_session_that_needs_you_is_marked_differently_from_one_that_does_not(self):
        need = [t for t, r in self.cells(attention="waiting")[0] if r == "mark"]
        idle = [t for t, r in self.cells(attention="stopped")[0] if r == "mark"]
        self.assertNotEqual(need, idle)

    def test_every_state_has_a_mark_and_a_style(self):
        for _, states in ccwho.UI_GROUPS:
            for state in states:
                self.assertIn(state, ccwho.UI_STATE_MARK, state)
                self.assertIn(state, ccwho.UI_STATE_STYLE, state)

    def test_a_state_nobody_planned_for_still_draws(self):
        first, _ = self.cells(attention="something-new")
        self.assertEqual(len([t for t, r in first if r == "mark"]), 1)


class TestTheCheapQuestion(unittest.TestCase):
    """"Does anything need me?" must be answerable far more often than "tell me
    everything about every session".

    Measured on a live fleet of sixteen: a full scan is 0.57s of CPU. Asking
    claude for the session list is 0.23s of CPU - it starts a Node process, and
    that is nearly all of it - so a check built on it could not run often
    without costing more than the scans it saved. Stat-ing every transcript and
    every project directory is 0.3ms and starts nothing at all.

    A session that needs you has just written to its transcript: the question is
    IN the transcript. A session that has only just started appears as a new
    file, which moves the mtime of the directory holding it.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.projects = os.path.join(self.tmp, "projects", "liveapp")
        os.makedirs(self.projects)
        self.real_roots = ccwho.ccwho_index.config_roots
        ccwho.ccwho_index.config_roots = lambda *a, **k: [self.tmp]
        self.addCleanup(setattr, ccwho.ccwho_index, "config_roots", self.real_roots)

    def transcript(self, sid, text="{}\n", when=None):
        path = os.path.join(self.projects, f"{sid}.jsonl")
        with open(path, "a") as fh:
            fh.write(text)
        if when:
            os.utime(path, (when, when))
        return path

    def test_the_same_files_give_the_same_answer(self):               # control
        self.transcript("a", when=1000.0)
        first = ccwho.watch_digest(["a"])
        self.assertEqual(first, ccwho.watch_digest(["a"]))

    def test_a_session_writing_something_changes_it(self):
        self.transcript("a", when=1000.0)
        before = ccwho.watch_digest(["a"])
        self.transcript("a", text="{\"q\": 1}\n", when=1001.0)
        self.assertNotEqual(before, ccwho.watch_digest(["a"]),
                            "a question it just asked you is a write")

    def test_a_session_that_did_not_exist_yet_changes_it(self):
        self.transcript("a", when=1000.0)
        before = ccwho.watch_digest(["a"])
        os.utime(self.projects, (2000.0, 2000.0))   # a new file landed here
        self.assertNotEqual(before, ccwho.watch_digest(["a"]),
                            "a session that has only just started is not in the"
                            " list of ids we were given")

    def test_it_asks_nothing_of_any_program(self):
        called = []
        real = ccwho.agents_json
        ccwho.agents_json = lambda: called.append(1) or "[]"
        try:
            self.transcript("a")
            ccwho.watch_digest(["a"])
        finally:
            ccwho.agents_json = real
        self.assertEqual(called, [], "starting Node to ask what changed is the"
                                     " cost this exists to avoid")

    def test_a_session_with_no_transcript_is_not_a_crash(self):
        self.assertIsInstance(ccwho.watch_digest(["nothing-here"]), tuple)

    def test_no_sessions_at_all_still_watches_the_directories(self):
        digest = ccwho.watch_digest([])
        self.assertTrue(digest, "a new session appearing must still show up")

    def test_a_full_scan_hands_back_the_same_answer(self):
        # or the cheap check fires again the moment a scan finishes
        real, real_files = ccwho.agents_json, ccwho.live_file_sessions
        ccwho.agents_json = lambda: '[]'
        ccwho.live_file_sessions = lambda *a, **k: ([], 0)     # this machine's sessions stay out
        real_table, real_ports = ccwho.ps_table, ccwho.listen_ports
        ccwho.ps_table, ccwho.listen_ports = (lambda: {}), (lambda: {})
        self.addCleanup(setattr, ccwho, "ps_table", real_table)
        self.addCleanup(setattr, ccwho, "listen_ports", real_ports)
        try:
            status = {}
            ccwho.collect(cache={}, status=status)
            self.assertEqual(status.get("watch"), ccwho.watch_digest([]))
        finally:
            ccwho.agents_json, ccwho.live_file_sessions = real, real_files


class TestAQuestionIsAQuestionWhateverTheFeedSays(unittest.TestCase):
    """Reported: a session asked a question and the list did not say so for a
    long time.

    ccwho asked `claude agents` what state each session was in, and only looked
    at the transcript when that answer was already "waiting". While the feed
    still said "busy", a question sitting unanswered in the transcript counted
    for nothing.

    Measured on a live fleet: a session sitting on AskUserQuestion, and this
    session, busy, sitting on a running Bash. Those must not read the same -
    which is why "a pending tool call" is not the signal and "a pending
    QUESTION" is. Across the newest 200 transcripts on this machine, the only
    tool ever left unanswered at the end of one was AskUserQuestion.
    """

    def records(self, *blocks):
        # a LIST of lines: as_records iterates what it is given, and a single
        # joined string iterates into characters and yields nothing
        return [json.dumps(b) for b in blocks]

    def asking(self, tool="AskUserQuestion"):
        return self.records(
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": tool, "input": {}}]}})

    def answered(self, tool="AskUserQuestion"):
        return self.asking(tool) + self.records(
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})

    def row(self, status, tail):
        return ccwho.build_row({"sessionId": "a", "status": status, "pid": 1},
                               "", tail, None)

    def test_a_question_while_the_feed_still_says_busy_needs_you(self):
        self.assertEqual(self.row("busy", self.asking())["attention"], "blocked")

    def test_a_question_while_the_feed_says_idle_needs_you(self):
        self.assertEqual(self.row("idle", self.asking())["attention"], "blocked")

    def test_a_running_tool_is_not_a_question(self):                  # control
        self.assertEqual(self.row("busy", self.asking("Bash"))["attention"], "busy")

    def test_a_plan_waiting_to_be_approved_needs_you(self):
        self.assertEqual(self.row("busy", self.asking("ExitPlanMode"))["attention"],
                         "blocked")

    def test_answering_it_clears_it_before_the_feed_catches_up(self):
        # The other half of the report: it has to STOP saying needs you the
        # moment you answer, not when the feed notices.
        self.assertNotEqual(self.row("waiting", self.answered())["attention"],
                            "blocked")

    def test_a_session_with_nothing_pending_is_unchanged(self):       # control
        quiet = self.records({"type": "assistant",
                              "message": {"role": "assistant",
                                          "content": [{"type": "text",
                                                       "text": "done."}]}})
        self.assertEqual(self.row("busy", quiet)["attention"], "busy")

    def test_the_tools_that_mean_a_human_is_needed_are_named(self):
        self.assertIn("AskUserQuestion", ccwho.HUMAN_TOOLS)
        self.assertIn("ExitPlanMode", ccwho.HUMAN_TOOLS)
        self.assertNotIn("Bash", ccwho.HUMAN_TOOLS)


class TestEverythingThatFinishedIsWorthReviewing(unittest.TestCase):
    """"Once an item finishes, I want to review it every time. The questions
    are just more urgent."

    So NEEDS YOU is not "sessions that asked something" - it is "sessions that
    have done something since you last looked". A pending question is the
    urgent kind and sorts first; a session that simply finished its turn is the
    same list, a tier down.
    """

    def row(self, status="idle", tail=(), ts="2026-09-22T10:00:00.000Z",
            reviewed=None):
        turn = json.dumps({"type": "assistant", "timestamp": ts,
                           "message": {"role": "assistant",
                                       "content": [{"type": "text",
                                                    "text": "done."}]}})
        return ccwho.build_row({"sessionId": "a", "status": status, "pid": 1},
                               [], [turn] + list(tail), None,
                               reviewed=reviewed or {})

    def finished(self, **kw):
        return self.row(**kw)

    def test_a_session_that_finished_wants_reviewing(self):
        self.assertEqual(self.finished()["attention"], "review")

    def test_looking_at_it_drops_it_to_stopped(self):
        row = self.finished()
        seen = {"a": row["ts"]}
        self.assertEqual(self.finished(reviewed=seen)["attention"], "stopped")

    def test_it_comes_back_when_the_session_does_more(self):
        row = self.finished()
        seen = {"a": row["ts"]}
        self.assertEqual(self.row(ts="2026-09-22T11:00:00.000Z",
                                  reviewed=seen)["attention"], "review",
                         "it picked up again and finished again")

    def test_a_busy_session_is_not_waiting_to_be_reviewed(self):      # control
        self.assertEqual(self.row(status="busy")["attention"], "busy")

    def test_a_question_is_still_the_urgent_kind(self):
        asking = [json.dumps({"type": "assistant", "message": {
            "role": "assistant", "content": [
                {"type": "tool_use", "id": "t", "name": "AskUserQuestion"}]}})]
        self.assertEqual(self.row(tail=asking)["attention"], "blocked")

    def test_dismissing_a_question_does_not_hide_it(self):
        # you cannot dismiss something that is still actually waiting on you
        asking = [json.dumps({"type": "assistant", "message": {
            "role": "assistant", "content": [
                {"type": "tool_use", "id": "t", "name": "AskUserQuestion"}]}})]
        row = self.row(tail=asking)
        self.assertEqual(self.row(tail=asking,
                                  reviewed={"a": row["ts"]})["attention"],
                         "blocked")


class TestTheUrgentOnesComeFirst(unittest.TestCase):
    def rows(self, *states):
        return [{"sessionId": s, "attention": s, "ts": ""} for s in states]

    def test_one_group_holds_them_all(self):
        groups = ccwho.ui_groups(self.rows("review", "blocked", "asks"))
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["heading"].split()[0], "NEEDS")

    def test_a_pending_question_sorts_above_one_that_just_finished(self):
        groups = ccwho.ui_groups(self.rows("review", "blocked"))
        self.assertEqual([r["attention"] for r in groups[0]["rows"]],
                         ["blocked", "review"])

    def test_a_phrase_match_sorts_above_one_that_just_finished(self):
        groups = ccwho.ui_groups(self.rows("review", "asks"))
        self.assertEqual([r["attention"] for r in groups[0]["rows"]],
                         ["asks", "review"])

    def test_the_two_tiers_do_not_look_the_same(self):
        self.assertNotEqual(ccwho.UI_STATE_MARK["review"],
                            ccwho.UI_STATE_MARK["blocked"])
        self.assertNotEqual(ccwho.UI_STATE_STYLE["review"],
                            ccwho.UI_STATE_STYLE["blocked"])

    def test_stopped_is_still_its_own_group(self):                    # control
        groups = ccwho.ui_groups(self.rows("stopped", "busy"))
        self.assertEqual([g["heading"].split()[0] for g in groups],
                         ["STOPPED", "BUSY"])


class TestWhatYouHaveLookedAtSurvives(unittest.TestCase):
    """Dismissals live on disk, not in the window. Pressing R restarts the
    screen, and a reboot takes it away entirely; neither should hand you back
    fifteen sessions you already reviewed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "reviewed.json")

    def test_nothing_reviewed_yet_is_an_empty_answer(self):
        self.assertEqual(ccwho.load_reviewed(self.path), {})

    def test_what_you_looked_at_comes_back(self):
        ccwho.mark_reviewed("a", "2026-09-22T10:00:00.000Z", path=self.path)
        self.assertEqual(ccwho.load_reviewed(self.path),
                         {"a": "2026-09-22T10:00:00.000Z"})

    def test_looking_again_moves_it_forward(self):
        ccwho.mark_reviewed("a", "2026-09-22T10:00:00.000Z", path=self.path)
        ccwho.mark_reviewed("a", "2026-09-22T11:00:00.000Z", path=self.path)
        self.assertEqual(ccwho.load_reviewed(self.path)["a"],
                         "2026-09-22T11:00:00.000Z")

    def test_two_sessions_do_not_tread_on_each_other(self):
        ccwho.mark_reviewed("a", "t1", path=self.path)
        ccwho.mark_reviewed("b", "t2", path=self.path)
        self.assertEqual(ccwho.load_reviewed(self.path), {"a": "t1", "b": "t2"})

    def test_a_file_full_of_nonsense_is_not_a_crash(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(ccwho.load_reviewed(self.path), {})

    def test_a_half_written_file_never_reaches_a_reader(self):
        # temp + replace, like everything else this repo writes
        ccwho.mark_reviewed("a", "t1", path=self.path)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["reviewed.json"])

    def test_it_does_not_grow_for_ever(self):
        for i in range(ccwho.REVIEWED_CAP + 50):
            ccwho.mark_reviewed(f"s{i}", f"t{i}", path=self.path)
        kept = ccwho.load_reviewed(self.path)
        self.assertLessEqual(len(kept), ccwho.REVIEWED_CAP)
        self.assertIn(f"s{ccwho.REVIEWED_CAP + 49}", kept, "the newest are kept")

    def test_a_scan_uses_them(self):
        ccwho.mark_reviewed("a", "t1", path=self.path)
        real = ccwho.reviewed_path
        ccwho.reviewed_path = lambda: self.path
        real_agents, real_files = ccwho.agents_json, ccwho.live_file_sessions
        ccwho.agents_json = lambda: "[]"
        ccwho.live_file_sessions = lambda *a, **k: ([], 0)     # this machine's sessions stay out
        real_table, real_ports = ccwho.ps_table, ccwho.listen_ports
        ccwho.ps_table, ccwho.listen_ports = (lambda: {}), (lambda: {})
        self.addCleanup(setattr, ccwho, "ps_table", real_table)
        self.addCleanup(setattr, ccwho, "listen_ports", real_ports)
        try:
            status = {}
            ccwho.collect(cache={}, status=status)
        finally:
            ccwho.reviewed_path = real
            ccwho.agents_json, ccwho.live_file_sessions = real_agents, real_files
        self.assertEqual(status.get("reviewed"), {"a": "t1"})


class TestOneTableSaysWhatAStateIsCalled(unittest.TestCase):
    """The table had its own copy of the label map, so a state added to the
    engine printed as its internal name - "review" instead of FINISHED. One
    table, and a test that every state the screen groups has an entry in it."""

    def test_every_grouped_state_has_a_label(self):
        for _, states in ccwho.UI_GROUPS:
            for state in states:
                self.assertIn(state, ccwho._LABEL, state)

    def test_every_grouped_state_has_a_colour(self):
        for _, states in ccwho.UI_GROUPS:
            for state in states:
                self.assertIn(state, ccwho._C, state)

    def test_the_table_prints_the_label_not_the_state(self):
        rows = [{"sessionId": "a", "attention": "review", "status": "idle",
                 "project": "liveapp", "title": "x", "tab_title": "", "name": "n",
                 "doing": "", "ask": "", "since": "1m", "ts": "", "topic": "",
                 "first": "", "age": "", "orphans": 0, "work": 0, "tty": "",
                 "pid": 1, "cwd": "/x", "recap": "", "recap_ts": "",
                 "recap_age": "", "turns_since_recap": 0}]
        out = ccwho.render(rows, 0, color=False)
        self.assertIn("FINISHED", out)
        self.assertNotIn(" review ", out)


class TestASessionWithNoWindowSaysSo(unittest.TestCase):
    """Clicking a session did nothing, and the reason was invisible: it runs in
    the background - `claude bg-spare` on a tty iTerm2 has never heard of - so
    there was no window to raise. ccwho had the evidence already (it asked
    iTerm2 for that tty's name and got nothing back) and said none of it.

    Three states, not two. iTerm2 knows the tty; iTerm2 does not know it; we
    could not ask iTerm2 at all - and the last of those must never be reported
    as the second, or every row says "no window" the moment iTerm2 is shut.
    """

    def test_a_tty_iterm_knows_is_windowed(self):                    # control
        # the titles map is keyed the way iTerm2 reports a tty
        self.assertIs(ccwho.windowed(tty="ttys024", titles={"ttys024": "a tab"}), True)

    def test_a_tty_iterm_does_not_know_is_not(self):
        self.assertIs(ccwho.windowed(tty="ttys042", titles={"ttys024": "a tab"}), False)

    def test_no_titles_at_all_is_unknown_not_missing(self):
        self.assertIsNone(ccwho.windowed(tty="ttys042", titles={}),
                          "iTerm2 shut is not every session losing its window")

    def test_no_tty_at_all_has_no_window(self):
        self.assertIs(ccwho.windowed(tty="", titles={"ttys024": "a tab"}), False)

    def test_the_row_carries_it(self):
        row = ccwho.build_row({"sessionId": "a", "status": "busy", "pid": 1},
                              [], [], None, tty="ttys042", tab_title="",
                              windowed=False)
        self.assertIs(row["windowed"], False)

    def test_a_row_says_it_where_you_can_see_it(self):
        row = ccwho.build_row({"sessionId": "a", "status": "busy", "pid": 1},
                              [], [], None, tty="ttys042", tab_title="",
                              windowed=False)
        first, _ = ccwho.ui_row_cells(row, width=100)
        self.assertIn("no window", "".join(t for t, _ in first))

    def test_a_windowed_row_says_nothing_of_the_kind(self):           # control
        row = ccwho.build_row({"sessionId": "a", "status": "busy", "pid": 1},
                              [], [], None, tty="ttys024", tab_title="a tab",
                              windowed=True)
        first, _ = ccwho.ui_row_cells(row, width=100)
        self.assertNotIn("no window", "".join(t for t, _ in first))

    def test_not_knowing_says_nothing_either(self):                   # control
        row = ccwho.build_row({"sessionId": "a", "status": "busy", "pid": 1},
                              [], [], None, tty="ttys042", tab_title="",
                              windowed=None)
        first, _ = ccwho.ui_row_cells(row, width=100)
        self.assertNotIn("no window", "".join(t for t, _ in first))


class TestASessionAProgramStartedNeverNeedsYou(unittest.TestCase):
    """NEEDS YOU and "~app · no window", now and then, for a few seconds. It was a
    session the liveapp eval harness started with the SDK in a folder named
    `app`: mid tool call, so its tool_use had no answer yet - which is what a
    permission prompt looks like. But the program answers its own prompts, and
    it has no window because it never had one. ccwho's own index already says
    such sessions are not yours (AGENT_ENTRYPOINTS); the live list did not ask.
    """

    PENDING = [json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]}})]

    def row(self, entrypoint="", tail_entrypoint=None, windowed=False):
        session = {"sessionId": "a", "status": "waiting", "pid": 1,
                   "cwd": "/private/var/folders/x/T/la-eval-quiet-abc/app", "name": "app-4e"}
        if entrypoint:
            session["entrypoint"] = entrypoint
        tail = list(self.PENDING)
        if tail_entrypoint is not None:
            rec = json.loads(tail[0])
            rec["entrypoint"] = tail_entrypoint
            tail = [json.dumps(rec)]
        return ccwho.build_row(session, [], tail, None, tty="", windowed=windowed)

    def test_you_at_a_terminal_blocked_needs_you(self):                 # control
        self.assertEqual(self.row("cli")["attention"], "blocked")

    def test_a_program_blocked_does_not(self):
        self.assertEqual(self.row("sdk-cli")["attention"], "program")

    def test_every_program_entrypoint_counts(self):
        for ep in ccwho.ccwho_index.AGENT_ENTRYPOINTS:
            self.assertEqual(self.row(ep)["attention"], "program", ep)

    def test_the_transcript_says_so_when_the_feed_does_not(self):
        self.assertEqual(self.row(tail_entrypoint="sdk-py")["attention"], "program")

    def test_a_transcript_from_a_terminal_still_needs_you(self):        # control
        self.assertEqual(self.row(tail_entrypoint="cli")["attention"], "blocked")

    def test_it_is_never_under_needs_you(self):
        groups = ccwho.ui_groups([self.row("cli"), self.row("sdk-cli")])
        self.assertEqual([g["heading"] for g in groups], ["NEEDS YOU", "PROGRAMS"])
        self.assertEqual([r["attention"] for r in groups[0]["rows"]], ["blocked"])

    def test_programs_come_last(self):
        busy = dict(self.row("cli"), attention="running")
        groups = ccwho.ui_groups([self.row("sdk-cli"), busy])
        self.assertEqual([g["heading"] for g in groups], ["BUSY", "PROGRAMS"])

    def test_it_does_not_say_no_window(self):
        first, _ = ccwho.ui_row_cells(self.row("sdk-cli"), width=100)
        text = "".join(t for t, _ in first)
        self.assertNotIn("no window", text)
        self.assertIn("program", text)

    def test_yours_with_no_window_still_says_so(self):                   # control
        first, _ = ccwho.ui_row_cells(self.row("cli"), width=100)
        self.assertIn("no window", "".join(t for t, _ in first))

    def test_the_table_does_not_say_needs_you(self):
        out = ccwho.render([self.row("sdk-cli")], {}, color=False, width=200)
        self.assertNotIn("NEEDS YOU", out)

    def test_the_table_still_says_it_for_yours(self):                   # control
        out = ccwho.render([self.row("cli")], {}, color=False, width=200)
        self.assertIn("NEEDS YOU", out)

    def test_the_newest_entrypoint_wins(self):
        # started by a program, then resumed by you at a terminal: the question
        # at the end is yours
        head = [json.dumps({"type": "user", "entrypoint": "sdk-cli",
                            "message": {"content": "go"}})]
        tail = [json.dumps(dict(json.loads(self.PENDING[0]), entrypoint="cli"))]
        row = ccwho.build_row({"sessionId": "a", "status": "waiting", "pid": 1},
                              head, tail, None, tty="", windowed=False)
        self.assertEqual(row["attention"], "blocked")

    def test_and_the_other_way_round(self):
        head = [json.dumps({"type": "user", "entrypoint": "cli",
                            "message": {"content": "go"}})]
        tail = [json.dumps(dict(json.loads(self.PENDING[0]), entrypoint="sdk-cli"))]
        row = ccwho.build_row({"sessionId": "a", "status": "waiting", "pid": 1},
                              head, tail, None, tty="", windowed=False)
        self.assertEqual(row["attention"], "program")

    def test_a_loop_that_cannot_end_is_still_stuck(self):
        # the program waits on the loop too: nobody but you will kill it
        ended = [json.dumps({"type": "system", "subtype": "turn_duration"})]
        session = {"sessionId": "a", "status": "busy", "pid": 1, "entrypoint": "sdk-cli"}
        stuck = ccwho.build_row(session, [], ended, None, dead_loops=[{"pid": 9}])
        self.assertEqual(stuck["attention"], "stuck")
        self.assertEqual(ccwho.build_row(session, [], ended, None)["attention"],
                         "program", "control: no dead loop, a program")

    def test_a_program_with_a_window_shows_its_tty(self):
        row = ccwho.build_row({"sessionId": "a", "status": "busy", "pid": 1,
                               "entrypoint": "sdk-py"}, [], [], None,
                              tty="/dev/ttys003", windowed=True)
        first, _ = ccwho.ui_row_cells(row, width=100)
        self.assertIn(ccwho.short_tty("/dev/ttys003"), "".join(t for t, _ in first))

    def test_not_knowing_about_windows_still_shows_its_tty(self):
        # iTerm2 shut: every other row shows its tty, and so does this one
        row = ccwho.build_row({"sessionId": "a", "status": "busy", "pid": 1,
                               "entrypoint": "sdk-py"}, [], [], None,
                              tty="/dev/ttys012", windowed=None)
        first, _ = ccwho.ui_row_cells(row, width=100)
        text = "".join(t for t, _ in first)
        self.assertIn(ccwho.short_tty("/dev/ttys012"), text)
        self.assertNotIn("program", text.split(ccwho.short_tty("/dev/ttys012"))[-1])

    def loops_row(self, entrypoint, ended):
        tail = [json.dumps({"type": "system", "subtype": "turn_duration"})] if ended else []
        return ccwho.build_row({"sessionId": "a", "status": "busy", "pid": 1,
                                "entrypoint": entrypoint}, [], tail, None,
                               dead_loops=[{"pid": 123, "tasks": ["t1"]}])

    def actions(self, row):
        _, second = ccwho.ui_row_cells(row, width=120)
        return [t for t, r in second if r == "action"]

    def test_mid_turn_a_program_is_not_offered_a_kill(self):
        # mid-turn its agent may be about to deal with the loop, as with yours
        row = self.loops_row("sdk-cli", ended=False)
        self.assertEqual(row["attention"], "program")
        self.assertFalse(ccwho.loop_kill_offered(row))
        self.assertEqual(self.actions(row), [])

    def test_nor_is_yours_mid_turn(self):                                # control
        row = self.loops_row("cli", ended=False)
        self.assertEqual(row["attention"], "busy")
        self.assertFalse(ccwho.loop_kill_offered(row))

    def test_a_stuck_program_is(self):                                   # control
        row = self.loops_row("sdk-cli", ended=True)
        self.assertEqual(row["attention"], "stuck")
        self.assertTrue(ccwho.loop_kill_offered(row))
        self.assertEqual(self.actions(row), [ccwho.UI_KILL])

    def test_a_stuck_program_says_program_not_no_window(self):
        row = self.loops_row("sdk-cli", ended=True)
        first, _ = ccwho.ui_row_cells(row, width=120)
        text = "".join(t for t, _ in first)
        self.assertIn("program", text)
        self.assertNotIn("no window", text)

    def test_the_session_file_beats_the_transcript(self):
        # the file belongs to the process running now: you resumed it at a
        # terminal, and it wrote "cli", whatever the transcript began as
        tail = [json.dumps(dict(json.loads(self.PENDING[0]), entrypoint="sdk-cli"))]
        row = ccwho.build_row({"sessionId": "a", "status": "waiting", "pid": 1,
                               "entrypoint": "cli"}, [], tail, None, tty="")
        self.assertEqual(row["attention"], "blocked")

    def test_enter_does_not_say_resume_a_program(self):
        note = ccwho.no_window_note(self.row("sdk-cli"))
        self.assertNotIn("claude --resume", note)
        self.assertIn("program", note)

    def test_enter_on_yours_still_says_resume(self):                      # control
        self.assertIn("claude --resume a", ccwho.no_window_note(self.row("cli")))

    def test_the_engine_main_blocked_leaves_it_out_too(self):
        import contextlib, io
        rows = [self.row("cli"), dict(self.row("sdk-cli"), title="PROGRAMROW")]
        for r in rows:
            r["title"] = r["title"] or r["name"]
        saved = ccwho.collect
        ccwho.collect = lambda *a, **k: (rows, {})
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                ccwho.main(["--blocked", "--no-color"])
        finally:
            ccwho.collect = saved
        self.assertNotIn("PROGRAMROW", buf.getvalue())
        self.assertIn("1 sessions", buf.getvalue(), "control: yours is listed")

    def test_blocked_leaves_it_out(self):
        rows = [self.row("cli"), self.row("sdk-cli")]
        self.assertEqual([r["attention"] for r in ccwho.only_blocked(rows)], ["blocked"])


class TestOpeningASessionAProgramRuns(unittest.TestCase):
    """A program's session with no tty was "attached": `claude attach` in a new
    window, on a session that is no daemon job - it fails, or it takes the
    session from the program. Nothing opens it; ccwho says who runs it."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"

    def test_it_is_not_attached(self):
        live = [{"sessionId": self.SID, "tty": "", "pid": 91, "attention": "program"}]
        action, value = ccwho.resolve_open(self.SID, live, [])
        self.assertEqual(action, "program")
        self.assertEqual(value, ccwho.no_window_note(live[0]))

    def test_a_background_session_still_is(self):                        # control
        live = [{"sessionId": self.SID, "tty": "", "pid": 91, "kind": "background",
                 "attention": "busy"}]
        self.assertEqual(ccwho.resolve_open(self.SID, live, [])[0], "attach")

    def test_a_stuck_one_is_not_attached_either(self):
        # a loop that cannot end keeps it in STUCK - it is still a program's
        live = [{"sessionId": self.SID, "tty": "", "pid": 91, "attention": "stuck",
                 "entrypoint": "sdk-py"}]
        self.assertEqual(ccwho.resolve_open(self.SID, live, [])[0], "program")
        self.assertNotIn("claude --resume", ccwho.no_window_note(live[0]))

    def test_a_stuck_one_of_yours_still_is(self):                         # control
        live = [{"sessionId": self.SID, "tty": "", "pid": 91, "attention": "stuck",
                 "entrypoint": "cli"}]
        self.assertEqual(ccwho.resolve_open(self.SID, live, [])[0], "attach")

    def test_one_with_a_window_is_still_a_jump(self):                     # control
        live = [{"sessionId": self.SID, "tty": "ttys032", "pid": 91,
                 "attention": "program"}]
        self.assertEqual(ccwho.resolve_open(self.SID, live, []), ("jump", "s032"))


class TestFindingTheWindowASessionIsShownIn(unittest.TestCase):
    """Measured on this machine, on a session running under the Claude Code
    daemon:

        28087  claude bg-spare      ttys042   <- what `claude agents` reports
        27890  claude bg-pty-host
        27713  claude daemon run
         1378  claude --resume      ttys000   <- the window you are looking at

    The feed names a background process whose tty no window owns, while the
    session is plainly on screen. Its ANCESTOR is the terminal it is displayed
    in, so that is what to walk to.
    """

    PARENTS = {28087: 27890, 27890: 27713, 27713: 1378, 1378: 98607}
    TTYS = {28087: "ttys042", 27890: "", 27713: "", 1378: "ttys000",
            98607: "ttys000"}
    TITLES = {"ttys000": "◑ Session discovery and management UX (claude)"}

    def test_it_walks_up_to_the_terminal_that_has_a_window(self):
        self.assertEqual(ccwho.owning_tty(28087, self.PARENTS, self.TTYS,
                                          self.TITLES), "ttys000")

    def test_a_session_in_its_own_window_is_left_alone(self):         # control
        titles = dict(self.TITLES, ttys042="some other tab")
        self.assertEqual(ccwho.owning_tty(28087, self.PARENTS, self.TTYS,
                                          titles), "ttys042")

    def test_no_ancestor_with_a_window_keeps_its_own_tty(self):
        self.assertEqual(ccwho.owning_tty(28087, {28087: 27890}, self.TTYS,
                                          self.TITLES), "ttys042",
                         "say where it is, even when nothing can show it")

    def test_iterm_unavailable_changes_nothing(self):                 # control
        self.assertEqual(ccwho.owning_tty(28087, self.PARENTS, self.TTYS, {}),
                         "ttys042")

    def test_a_loop_in_the_tree_does_not_hang(self):
        self.assertEqual(ccwho.owning_tty(1, {1: 2, 2: 1}, {1: "", 2: ""},
                                          self.TITLES), "")

    def test_no_pid_is_no_terminal(self):
        self.assertEqual(ccwho.owning_tty(0, self.PARENTS, self.TTYS,
                                          self.TITLES), "")

    def test_the_parent_map_comes_out_of_ps(self):
        out = ("  PID  PPID     ELAPSED COMMAND\n"
               "28087 27890    01:00:00 claude bg-spare\n"
               "27890 27713    01:00:00 claude bg-pty-host\n")
        self.assertEqual(ccwho.parent_map(out), {28087: 27890, 27890: 27713})

    def test_a_windowed_session_found_this_way_can_be_jumped_to(self):
        row = ccwho.build_row({"sessionId": "a", "status": "busy", "pid": 28087},
                              [], [], None, tty="ttys000",
                              tab_title=self.TITLES["ttys000"], windowed=True)
        self.assertIs(row["windowed"], True)
        self.assertEqual(row["tty"], "ttys000")


class TestABackgroundSessionCanBeGivenAWindow(unittest.TestCase):
    """The third kind, in the user's words: "it's running in the background,
    doesn't have a visible window and we want to get a visible window."

    Claude Code has had the answer all along and ccwho ignored it. The session
    feed carries a `kind` - on this fleet, fourteen interactive and one
    background - and the CLI has `claude attach <id>`: "Open the background
    session in this terminal."

    Resuming it would be the wrong answer and a dangerous one: the session is
    RUNNING, and `claude --resume` on a live session forks the conversation
    into two processes writing one transcript.
    """

    def live(self, **over):
        row = {"sessionId": "abc", "tty": "", "pid": 42, "kind": "background"}
        row.update(over)
        return [row]

    def test_a_live_session_with_no_window_is_attached_to(self):
        action, cmd = ccwho.resolve_open("abc", self.live(), [], source_ok=True)
        self.assertEqual(action, "attach")
        self.assertIn("claude attach abc", cmd)

    def test_it_is_attached_by_its_short_id(self):
        # `claude attach` knows a job by its daemon short id, the first eight
        # hex of the session id. Given the full id it says "No job matching"
        # (claude 2.1.280) - and the session looks lost.
        sid = "51fddd61-822b-49e0-9aeb-2145e91e1244"
        action, cmd = ccwho.resolve_open(sid, self.live(sessionId=sid), [],
                                         source_ok=True)
        self.assertEqual(action, "attach")
        self.assertEqual(cmd, "claude attach 51fddd61")

    def test_it_is_never_resumed(self):
        action, cmd = ccwho.resolve_open("abc", self.live(), [], source_ok=True)
        self.assertNotEqual(action, "resume")
        self.assertNotIn("--resume", cmd)

    def test_a_session_with_a_window_is_still_jumped_to(self):        # control
        action, where = ccwho.resolve_open("abc", self.live(tty="ttys024"), [],
                                           source_ok=True)
        self.assertEqual(action, "jump")
        self.assertEqual(where, "s024")

    def test_a_session_that_is_not_running_is_still_resumed(self):    # control
        gone = "aaaa1111-0000-4000-8000-000000000001"
        action, cmd = ccwho.resolve_open(
            "gone" if False else gone, [],
            [{"sessionId": gone, "cwd": "/tmp"}], source_ok=True)
        self.assertEqual(action, "resume")
        self.assertIn("--resume", cmd)

    def test_an_unreadable_fleet_still_decides_nothing(self):         # control
        action, _ = ccwho.resolve_open("abc", self.live(), [], source_ok=False)
        self.assertEqual(action, "unknown")

    def test_the_row_carries_what_kind_it_is(self):
        row = ccwho.build_row({"sessionId": "a", "status": "busy", "pid": 1,
                               "kind": "background"}, [], [], None)
        self.assertEqual(row["kind"], "background")

    def test_a_background_row_says_so_where_you_can_see_it(self):
        row = ccwho.build_row({"sessionId": "a", "status": "busy", "pid": 1,
                               "kind": "background"}, [], [], None,
                              tty="", windowed=False)
        first, _ = ccwho.ui_row_cells(row, width=100)
        line = "".join(t for t, _ in first)
        self.assertIn("background", line)
        self.assertNotIn("no window", line,
                         "'no window' reads as broken; 'background' reads as"
                         " a session you can open")


class TestOnlyAWindowThatIsSHOWINGItCounts(unittest.TestCase):
    """A fourth kind: background AND already attached - running under the
    daemon with a terminal displaying it.

    Measured on this machine: no process carries the session id and nothing
    runs `claude attach`; the window at ttys000 runs `claude --resume`, and
    that process IS the viewer - the daemon it spawned runs the session.

        28087  claude bg-spare      ttys042
        27890  claude bg-pty-host
        27713  claude daemon run
         1378  claude --resume      ttys000   <- the viewer

    So walking up to "the first ancestor with a tty" is not enough. A session
    started with --bg from a shell has that shell as an ancestor, and its
    window is NOT showing the session: jumping there would put you in front of
    a terminal that knows nothing about it.
    """

    SID = "51fddd61-822b-49e0-9aeb-2145e91e1244"
    TITLES = {"ttys000": "a tab", "ttys015": "another tab"}

    def test_a_viewer_ancestor_is_the_window(self):
        parents = {28087: 27890, 27890: 27713, 27713: 1378, 1378: 98607}
        ttys = {28087: "ttys042", 1378: "ttys000", 98607: "ttys000"}
        cmds = {28087: "claude bg-spare --bg-spare /tmp/x.sock",
                27890: "claude bg-pty-host /tmp/x.sock",
                27713: "claude daemon run --origin transient",
                1378: "claude --resume", 98607: "-zsh"}
        self.assertEqual(ccwho.owning_tty(28087, parents, ttys, self.TITLES,
                                          commands=cmds), "ttys000")

    def test_the_daemons_own_terminal_is_not_a_window_showing_it(self):
        # Start a session with `claude --bg` from a prompt and the daemon
        # inherits THAT terminal - which still has a window, showing your
        # shell. Without excluding the daemon's own processes the walk stops
        # there and jumps you to a terminal that knows nothing about it.
        parents = {28087: 27890, 27890: 1378}
        ttys = {28087: "ttys015", 27890: "ttys015", 1378: "ttys000"}
        cmds = {28087: "claude bg-spare --bg-spare /tmp/x.sock",
                27890: "claude daemon run --origin transient",
                1378: "claude --resume"}
        self.assertEqual(ccwho.owning_tty(28087, parents, ttys, self.TITLES,
                                          commands=cmds), "ttys000",
                         "the daemon's terminal shows a shell, not the session")

    def test_a_shell_ancestor_is_not_a_window_showing_it(self):
        # `claude --bg` from a prompt: the shell is still there, on a tty with
        # a window, and that window is showing a shell - not this session.
        parents = {5000: 4000, 4000: 3000}
        ttys = {5000: "", 4000: "", 3000: "ttys015"}
        cmds = {5000: "claude bg-spare", 4000: "claude daemon run", 3000: "-zsh"}
        self.assertEqual(ccwho.owning_tty(5000, parents, ttys, self.TITLES,
                                          commands=cmds), "",
                         "no window is showing it: it wants attaching, not a jump")

    def test_an_attach_process_is_the_window_wherever_it_is(self):
        # `claude attach <id>` run in some other terminal: that IS the viewer,
        # and it is not an ancestor of anything.
        ttys = {7000: "ttys015"}
        cmds = {7000: f"claude attach {self.SID}"}
        self.assertEqual(ccwho.owning_tty(5000, {}, ttys, self.TITLES,
                                          commands=cmds, session_id=self.SID),
                         "ttys015")

    def test_an_attach_by_short_id_is_the_window(self):
        # the form that works, and the one ccwho itself now runs
        ttys = {7000: "ttys015"}
        cmds = {7000: f"claude attach {self.SID[:8]}"}
        self.assertEqual(ccwho.owning_tty(5000, {}, ttys, self.TITLES,
                                          commands=cmds, session_id=self.SID),
                         "ttys015")
        self.assertTrue(ccwho.is_viewer(cmds[7000], self.SID))

    def test_an_attach_to_a_longer_id_with_the_same_prefix_is_not_it(self):  # control
        # "attach 51fddd61" must not match inside "attach 51fddd61ff"
        ttys = {7000: "ttys015"}
        cmds = {7000: f"claude attach {self.SID[:8]}ff"}
        self.assertEqual(ccwho.owning_tty(5000, {}, ttys, self.TITLES,
                                          commands=cmds, session_id=self.SID), "")

    def test_an_attach_to_a_different_session_is_not_it(self):        # control
        ttys = {7000: "ttys015"}
        cmds = {7000: "claude attach 9999aaaa-0000-4000-8000-000000000000"}
        self.assertEqual(ccwho.owning_tty(5000, {}, ttys, self.TITLES,
                                          commands=cmds, session_id=self.SID), "")

    def test_an_ordinary_session_is_unaffected(self):                 # control
        # its own process is on the tty, and is a viewer
        self.assertEqual(ccwho.owning_tty(
            42, {}, {42: "ttys000"}, self.TITLES,
            commands={42: "claude --resume abc"}), "ttys000")

    def test_without_commands_it_still_answers(self):                 # control
        # ps without a command column, or a caller that has none: fall back to
        # the old rule rather than reporting no window at all
        self.assertEqual(ccwho.owning_tty(42, {}, {42: "ttys000"}, self.TITLES),
                         "ttys000")


class TestCollectFindsEveryConfigDir(MachinelessCollect):
    """`claude agents --json` lists the sessions of ONE config dir. A session run
    with its own CLAUDE_CONFIG_DIR never appeared - not even when it needed you.
    Measured: 2 of 17 running sessions were invisible."""

    A = {"pid": 11, "sessionId": "aaaa1111-0000-4000-8000-000000000001",
         "cwd": "/Users/x/p/app", "kind": "interactive", "name": "app", "status": "idle",
         "startedAt": 1000}
    B = {"pid": 22, "sessionId": "bbbb2222-0000-4000-8000-000000000002",
         "cwd": "/Users/x/p/work", "kind": "interactive", "name": "isolated",
         "status": "busy", "startedAt": 2000}

    def setUp(self):
        super().setUp()
        self._agents = ccwho.agents_json
        ccwho.agents_json = lambda: json.dumps([self.A])

    def tearDown(self):
        ccwho.agents_json = self._agents
        super().tearDown()

    def ids(self, rows):
        return sorted(r["sessionId"][:4] for r in rows)

    def test_a_session_in_another_config_dir_is_listed(self):
        ccwho.live_file_sessions = lambda *a, **k: ([self.B], 0)
        rows, _ = ccwho.collect(cache={})
        self.assertEqual(self.ids(rows), ["aaaa", "bbbb"])

    def test_no_other_dirs_changes_nothing(self):                    # regression
        rows, _ = ccwho.collect(cache={})
        self.assertEqual(self.ids(rows), ["aaaa"])

    def test_a_session_in_both_is_listed_once(self):
        ccwho.live_file_sessions = lambda *a, **k: ([dict(self.A, name="from-file")], 0)
        rows, _ = ccwho.collect(cache={})
        self.assertEqual(self.ids(rows), ["aaaa"])

    def test_unreachable_claude_still_shows_what_the_files_know(self):
        # shown, but never trusted to launch: source_ok stays False
        ccwho.agents_json = lambda: None
        ccwho.live_file_sessions = lambda *a, **k: ([self.B], 0)
        status = {}
        rows, _ = ccwho.collect(cache={}, status=status)
        self.assertEqual(self.ids(rows), ["bbbb"])
        self.assertFalse(status["source_ok"])

    def test_unreadable_session_files_are_counted_for_doctor(self):
        ccwho.live_file_sessions = lambda *a, **k: ([], 3)
        status = {}
        ccwho.collect(cache={}, status=status)
        self.assertEqual(status["session_files_bad"], 3)


class TestLiveFileSessionsLooksWhereSessionsSayTheyAre(unittest.TestCase):
    """The glue: a running claude names its own config dir, and that dir's
    session files are read. Nothing here touches this machine's real dirs."""

    START = "Tue Sep 22 13:45:15 2026"

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "sessions"))
        with open(os.path.join(self.dir, "sessions", "7.json"), "w") as fh:
            json.dump({"pid": 7, "sessionId": "cccc3333-0000-4000-8000-000000000003",
                       "procStart": self.START, "kind": "interactive",
                       "cwd": "/Users/x/p/work", "name": "isolated", "status": "idle"}, fh)
        self._saved = (ccwho.ps_table, ccwho.read_procargs, ccwho.ccwho_index.config_roots)
        ccwho.ps_table = lambda: {7: (self.START, "/Users/x/.local/bin/claude"),
                                  8: (self.START, "/bin/zsh")}
        ccwho.ccwho_index.config_roots = lambda roots_file=None: []

    def tearDown(self):
        (ccwho.ps_table, ccwho.read_procargs, ccwho.ccwho_index.config_roots) = self._saved
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_running_sessions_own_config_dir_is_read(self):
        ccwho.read_procargs = lambda pid: {"CLAUDE_CONFIG_DIR": self.dir} if pid == 7 else {}
        rows, bad = REAL_LIVE_FILE_SESSIONS()
        self.assertEqual([r["name"] for r in rows], ["isolated"])
        self.assertEqual(bad, 0)

    def test_only_claude_processes_are_asked(self):                  # control
        asked = []
        ccwho.read_procargs = lambda pid: asked.append(pid) or {}
        REAL_LIVE_FILE_SESSIONS()
        self.assertEqual(asked, [7], "a shell's environment is none of our business")

    def test_without_that_dir_named_nothing_is_found(self):           # control
        ccwho.read_procargs = lambda pid: {}
        rows, _ = REAL_LIVE_FILE_SESSIONS()
        self.assertEqual(rows, [])


SID_ISO = "dddd4444-0000-4000-8000-000000000004"


class TestAnIsolatedSessionIsReadLikeAnyOther(MachinelessCollect):
    """Finding a session in another config dir is half the job: its transcript
    lives under THAT dir, and without it the row has no title, no recap and no
    way to say it needs you. Measured before this fix: a live isolated session
    listed with an empty title and topic."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        proj = os.path.join(self.dir, "projects", "-Users-x-p-work")
        os.makedirs(proj)
        with open(os.path.join(proj, f"{SID_ISO}.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "ai-title", "aiTitle": "Isolated work"}) + "\n")
        self._agents = ccwho.agents_json
        ccwho.agents_json = lambda: "[]"
        self.row = {"pid": 44, "sessionId": SID_ISO, "cwd": "/Users/x/p/work",
                    "kind": "interactive", "status": "idle", "configDir": self.dir}
        ccwho.live_file_sessions = lambda *a, **k: ([self.row], 0)

    def tearDown(self):
        ccwho.agents_json = self._agents
        super().tearDown()

    def test_its_transcript_is_found_under_its_own_dir(self):
        rows, _ = ccwho.collect(cache={})
        self.assertEqual(rows[0]["title"], "Isolated work")

    def test_the_one_second_check_watches_that_dir_too(self):
        # a session that has only just started has no id we know: its new file
        # moves the mtime of the project dir, which must be one we watch
        cache = {}
        ccwho.collect(cache=cache)
        proj = os.path.join(self.dir, "projects", "-Users-x-p-work")
        os.utime(proj, (1000.0, 1000.0))
        before = ccwho.watch_digest([SID_ISO], cache=cache)
        os.utime(proj, (5000.0, 5000.0))
        self.assertNotEqual(before, ccwho.watch_digest([SID_ISO], cache=cache),
                            "a new session in the isolated dir went unnoticed")

    def test_found_without_a_cache_too(self):
        # the engine's own one-shot main() calls collect() with no cache
        rows, _ = ccwho.collect()
        self.assertEqual(rows[0]["title"], "Isolated work")

    def test_restore_check_finds_a_saved_isolated_transcript(self):
        man = {"sessions": [{"sessionId": SID_ISO, "cwd": self.dir, "configDir": self.dir}]}
        ok, problems = ccwho.check_manifest(man, cwd_exists=os.path.isdir,
                                            transcript_for=ccwho.manifest_transcript_finder(man))
        self.assertTrue(ok, problems)

    def test_restore_check_still_fails_a_gone_transcript(self):       # control
        man = {"sessions": [{"sessionId": "eeee5555-0000-4000-8000-000000000005",
                             "cwd": self.dir, "configDir": self.dir}]}
        ok, _ = ccwho.check_manifest(man, cwd_exists=os.path.isdir,
                                     transcript_for=ccwho.manifest_transcript_finder(man))
        self.assertFalse(ok)

    def test_without_a_config_dir_nothing_is_invented(self):         # control
        self.row.pop("configDir")
        rows, _ = ccwho.collect(cache={})
        self.assertEqual(rows[0]["title"], "")


class TestSessionFilesAreReadCarefully(unittest.TestCase):
    """The dirs are named by other processes. Whatever is in them, a scan must
    finish, and what cannot be used is counted for `ccwho doctor`."""

    GOOD = {"pid": 7, "sessionId": "eeee5555-0000-4000-8000-000000000005",
            "procStart": "Tue Sep 22 13:45:15 2026"}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.sessions = os.path.join(self.dir, "sessions")
        os.makedirs(self.sessions)

    def write(self, name, body):
        with open(os.path.join(self.sessions, name), "w") as fh:
            fh.write(body)

    def test_unusable_files_are_counted_and_the_good_one_kept(self):
        self.write("7.json", json.dumps(self.GOOD))
        self.write("8.json", "{not json")
        self.write("9.json", "[1, 2]")
        self.write("10.json", json.dumps(dict(self.GOOD, sessionId="*")))
        entries, bad = ccwho.read_session_files([self.dir])
        self.assertEqual(([e["pid"] for e in entries], bad), ([7], 3))

    def test_each_entry_remembers_its_dir(self):
        self.write("7.json", json.dumps(self.GOOD))
        entries, _ = ccwho.read_session_files([self.dir])
        self.assertEqual(entries[0]["configDir"], self.dir)

    def test_no_sessions_dir_is_nothing_wrong(self):                  # control
        self.assertEqual(ccwho.read_session_files([tempfile.mkdtemp()]), ([], 0))

    def test_a_fifo_does_not_hang_the_scan(self):
        os.mkfifo(os.path.join(self.sessions, "x.json"))
        entries, bad = ccwho.read_session_files([self.dir])       # must return
        self.assertEqual((entries, bad), ([], 1))

    def test_an_oversized_file_is_not_read(self):
        self.write("7.json", json.dumps(dict(self.GOOD, name="x" * 200_000)))
        self.assertEqual(ccwho.read_session_files([self.dir]), ([], 1))

    def test_a_deeply_nested_file_is_counted_not_fatal(self):
        # small enough to pass the size cap, deep enough to exhaust the parser
        self.write("7.json", "[" * 60000)
        self.assertEqual(ccwho.read_session_files([self.dir]), ([], 1))

    def test_a_symlink_is_not_followed(self):
        target = os.path.join(self.dir, "elsewhere.json")
        with open(target, "w") as fh:
            json.dump(self.GOOD, fh)
        os.symlink(target, os.path.join(self.sessions, "7.json"))
        self.assertEqual(ccwho.read_session_files([self.dir]), ([], 1))

    def test_glob_characters_in_a_dir_name_are_literal(self):
        odd = os.path.join(self.dir, "we[i]rd*")
        os.makedirs(os.path.join(odd, "sessions"))
        with open(os.path.join(odd, "sessions", "7.json"), "w") as fh:
            json.dump(self.GOOD, fh)
        entries, _ = ccwho.read_session_files([odd])
        self.assertEqual([e["pid"] for e in entries], [7])


class TestPsSpeaksTheSessionFilesLanguage(unittest.TestCase):
    """A session file stores its start time in the C locale, in UTC. `ps` has
    to print it the same way, or on any machine not in UTC every isolated
    session fails the comparison and silently vanishes."""

    def test_ps_runs_in_the_c_locale_and_utc(self):
        seen = {}

        class Done:
            stdout = ""

        real = ccwho.subprocess.run
        ccwho.subprocess.run = lambda argv, **kw: seen.update(kw, argv=argv) or Done()
        try:
            REAL["ps_table"]()
        finally:
            ccwho.subprocess.run = real
        self.assertEqual((seen["env"]["LC_ALL"], seen["env"]["TZ"]), ("C", "UTC"))
        self.assertIn("pid=,lstart=,comm=", seen["argv"])

    def test_a_ps_that_cannot_run_is_an_empty_table(self):           # control
        def boom(*a, **k):
            raise OSError("no ps")
        real = ccwho.subprocess.run
        ccwho.subprocess.run = boom
        try:
            self.assertEqual(REAL["ps_table"](), {})
        finally:
            ccwho.subprocess.run = real


@unittest.skipUnless(os.uname().sysname == "Darwin", "KERN_PROCARGS2 is macOS")
class TestReadProcargsOnARealProcess(unittest.TestCase):
    """The only code that holds a real environment buffer. A child with a
    known, fake environment: the allowlisted key comes back, the token never."""

    TOKEN = "sk-ant-FAKE-real-process-must-not-leak"

    def test_only_the_allowlist_comes_back(self):
        # Not /bin/sleep: macOS hides the environment of its own system binaries
        # (measured: 33 bytes, argv only), so a system binary an agent starts
        # carries no mark we can read. claude, node and this Python are not.
        import subprocess
        import sys
        import time
        if sys.executable.startswith(("/usr/bin/", "/bin/", "/System/")):
            self.skipTest("the test's own Python is a system binary: its env is hidden")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                                 env={"CLAUDE_CONFIG_DIR": "/fake/dir",
                                      "ANTHROPIC_API_KEY": self.TOKEN, "PATH": "/bin"})
        try:
            got = {}
            for _ in range(50):             # until it has exec'd
                got = ccwho.read_procargs(child.pid) or {}
                if got:
                    break
                time.sleep(0.05)
        finally:
            child.kill()
            child.wait()
        self.assertEqual(got, {"CLAUDE_CONFIG_DIR": "/fake/dir"})
        self.assertNotIn(self.TOKEN, repr(got))

    def test_a_pid_that_does_not_exist_is_none(self):                # control
        self.assertIsNone(ccwho.read_procargs(99999999))


class TestReloadPutsEarlierModulesBackWhenALaterOneFails(unittest.TestCase):
    """brief is re-read first. If procs - the LAST rule module - raises, brief
    and index were already swapped; the rollback is what stops the window
    running a mix. Breaking brief alone never reaches the rollback."""

    def test_a_late_failure_restores_the_early_modules(self):
        import sys
        *early, last = ccwho.RELOAD_FIRST       # break whichever is re-read last
        before = {n: sys.modules[n] for n in early}
        path = sys.modules[last].__file__
        original = open(path).read()
        try:
            with open(path, "w") as fh:
                fh.write(original + '\nraise RuntimeError("boom")\n')
            with self.assertRaises(RuntimeError):
                ccwho.reload_all(ccwho)
            for name, module in before.items():
                self.assertIs(sys.modules[name], module, f"{name} was left swapped")
        finally:
            with open(path, "w") as fh:
                fh.write(original)
            ccwho.reload_all(ccwho)
            # a reload re-executes the engine, which rebinds the real function:
            # the module's guard has to go back, or every later test is unguarded
            install_guards()


class TestAttachUsesTheSessionsOwnConfigDir(unittest.TestCase):
    """`claude attach` looks in the config dir it runs under. A session found in
    another dir is not there, and the answer is `No job matching` - the very
    error this feature started from."""

    SID = "51fddd61-822b-49e0-9aeb-2145e91e1244"

    def live(self, **kw):
        return [dict({"sessionId": self.SID, "tty": "", "pid": 5, "kind": "background"}, **kw)]

    def test_a_session_from_another_dir_is_attached_there(self):
        action, cmd = ccwho.resolve_open(self.SID, self.live(configDir="/Users/x/my dir"),
                                         [], source_ok=True)
        self.assertEqual(action, "attach")
        self.assertEqual(cmd, "CLAUDE_CONFIG_DIR='/Users/x/my dir' claude attach 51fddd61")

    def test_a_session_from_the_agents_feed_is_attached_as_before(self):  # control
        _, cmd = ccwho.resolve_open(self.SID, self.live(), [], source_ok=True)
        self.assertEqual(cmd, "claude attach 51fddd61")

    def test_the_window_running_it_is_still_recognised(self):
        cmd = "CLAUDE_CONFIG_DIR=/x claude attach 51fddd61"
        self.assertTrue(ccwho.is_attach_to(cmd, self.SID))


class TestASessionFileCaughtMidWriteIsReadAgain(unittest.TestCase):
    """doctor says "if it persists, the format changed". A file caught while
    Claude Code writes it is not that: it is read once more before it counts."""

    GOOD = {"pid": 7, "sessionId": "eeee5555-0000-4000-8000-000000000005",
            "procStart": "Tue Sep 22 13:45:15 2026"}

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        os.makedirs(os.path.join(self.dir, "sessions"))
        with open(os.path.join(self.dir, "sessions", "7.json"), "w") as fh:
            json.dump(self.GOOD, fh)
        self.real = ccwho.json.loads
        self.addCleanup(setattr, ccwho.json, "loads", self.real)

    def test_one_failed_parse_is_retried(self):
        calls = []

        def flaky(data):
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("caught mid-write")
            return self.real(data)

        ccwho.json.loads = flaky
        entries, bad = ccwho.read_session_files([self.dir])
        self.assertEqual((len(entries), bad), (1, 0))

    def test_a_file_that_never_parses_still_counts(self):             # control
        def broken(data):
            raise ValueError("not json")
        ccwho.json.loads = broken
        self.assertEqual(ccwho.read_session_files([self.dir]), ([], 1))


class TestTheConfigDirTravelsWithTheRow(MachinelessCollect):
    """configDir is only useful if it reaches the commands. A row built by
    collect() - not by hand - has to carry it to attach, save and restore."""

    SID = "ffff6666-0000-4000-8000-000000000006"

    def setUp(self):
        super().setUp()
        self._agents = ccwho.agents_json
        ccwho.agents_json = lambda: "[]"
        ccwho.live_file_sessions = lambda *a, **k: ([{
            "pid": 66, "sessionId": self.SID, "cwd": "/Users/x/p/work",
            "kind": "background", "status": "idle", "configDir": "/Users/x/.claude-work"}], 0)

    def tearDown(self):
        ccwho.agents_json = self._agents
        super().tearDown()

    def test_attach_from_a_collected_row_uses_its_dir(self):
        rows, _ = ccwho.collect(cache={})
        action, cmd = ccwho.resolve_open(self.SID, rows, [], source_ok=True)
        self.assertEqual((action, cmd), ("attach",
                         "CLAUDE_CONFIG_DIR=/Users/x/.claude-work claude attach ffff6666"))

    def test_a_saved_session_resumes_in_its_dir(self):
        rows, _ = ccwho.collect(cache={})
        entry = ccwho.manifest_from_rows(rows)["sessions"][0]
        self.assertEqual(ccwho.restore_command(entry),
                         "cd /Users/x/p/work && CLAUDE_CONFIG_DIR=/Users/x/.claude-work"
                         " claude --resume " + self.SID)

    def test_a_default_dir_session_resumes_as_before(self):          # control
        entry = {"sessionId": self.SID, "cwd": "/Users/x/p/work", "configDir": ""}
        self.assertEqual(ccwho.restore_command(entry),
                         "cd /Users/x/p/work && claude --resume " + self.SID)


class TestIdsEndWhereTheyEnd(unittest.TestCase):
    """`$` matches before a trailing newline. Every pattern that guards an id or a
    file name that ends up in a command or a path uses \\Z instead."""

    SID = "51fddd61-822b-49e0-9aeb-2145e91e1244"

    def test_no_resume_line_for_an_id_with_a_newline(self):
        self.assertIsNone(ccwho.restore_command({"sessionId": self.SID + "\n", "cwd": "/x"}))
        self.assertIsNotNone(ccwho.restore_command({"sessionId": self.SID, "cwd": "/x"}))

    def test_no_link_for_an_id_with_a_newline(self):
        self.assertEqual(ccwho.open_url({"sessionId": self.SID + "\n"}), "")
        self.assertTrue(ccwho.open_url({"sessionId": self.SID}))      # control

    def test_a_manifest_name_with_a_newline_is_not_one(self):
        self.assertFalse(ccwho._MANIFEST_NAME.match("2026-09-24T1200.json\n"))
        self.assertTrue(ccwho._MANIFEST_NAME.match("2026-09-24T1200.json"))


class TestTheDefaultDirIsNotSpelledOut(unittest.TestCase):
    """A session in ~/.claude runs with CLAUDE_CONFIG_DIR unset. Setting it, even
    to the same path, can change where Claude Code looks for its login."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        os.makedirs(os.path.join(self.dir, "sessions"))
        with open(os.path.join(self.dir, "sessions", "7.json"), "w") as fh:
            json.dump({"pid": 7, "sessionId": "eeee5555-0000-4000-8000-000000000005",
                       "procStart": "Tue Sep 22 13:45:15 2026"}, fh)

    def test_the_default_root_leaves_configdir_empty(self):
        entries, _ = ccwho.read_session_files([self.dir], default=self.dir)
        self.assertEqual(entries[0]["configDir"], "")

    def test_another_root_is_named(self):                             # control
        entries, _ = ccwho.read_session_files([self.dir], default="/elsewhere")
        self.assertEqual(entries[0]["configDir"], self.dir)


class TestAFailingFileSourceNeverBlanksTheList(MachinelessCollect):
    """The session files are a second source. If reading them fails outright,
    the list still shows what `claude agents` said."""

    def test_the_agents_rows_survive(self):
        real = ccwho.agents_json
        ccwho.agents_json = lambda: json.dumps([{"pid": 5, "sessionId":
            "aaaa1111-0000-4000-8000-000000000001", "cwd": "/x", "status": "idle"}])

        def boom(*a, **k):
            raise RuntimeError("anything at all")
        ccwho.live_file_sessions = boom
        try:
            status = {}
            rows, _ = ccwho.collect(cache={}, status=status)
        finally:
            ccwho.agents_json = real
        self.assertEqual(len(rows), 1)
        self.assertEqual(status["session_files_bad"], None)


class TestALinkNamesASessionByItsUuid(unittest.TestCase):
    """The README and parse_ccwho_url promise a UUID for ccwho://open/. A second,
    looser pattern of the same name further down the engine used to replace it,
    so any id-shaped string was accepted."""

    def test_a_non_uuid_is_refused(self):
        self.assertEqual(ccwho.parse_ccwho_url("ccwho://open/abcdefgh-not-a-uuid"), ("", ""))
        self.assertEqual(ccwho.open_url({"sessionId": "abcdefgh12"}), "")

    def test_a_uuid_is_accepted(self):                                # control
        sid = "51fddd61-822b-49e0-9aeb-2145e91e1244"
        self.assertEqual(ccwho.parse_ccwho_url("ccwho://open/" + sid), ("open", sid))
        self.assertTrue(ccwho.open_url({"sessionId": sid}))


TOKEN_E = "sk-ant-FAKE-engine-must-never-print"
LIVE = "aaaa1111-0000-4000-8000-000000000001"
DEAD = "dddd4444-0000-4000-8000-000000000004"
START = "Tue Sep 22 13:45:15 2026"


class TestMarksAreReadOncePerProcess(unittest.TestCase):
    """A process's environment is fixed at exec: read it once per (pid, start,
    command), keep it in the caller's cache, forget it when the process is gone.
    The command is part of the key because an exec keeps the pid and the start."""

    def setUp(self):
        self.reads = []
        self.real = ccwho.read_procargs
        ccwho.read_procargs = lambda pid: self.reads.append(pid) or {
            "CLAUDE_CODE_SESSION_ID": LIVE}
        self.addCleanup(setattr, ccwho, "read_procargs", self.real)

    def test_a_second_scan_reads_nothing_new(self):
        cache, table = {}, {7: (START, "node"), 8: (START, "node")}
        first = ccwho.proc_marks(table, cache)
        ccwho.proc_marks(table, cache)
        self.assertEqual(sorted(self.reads), [7, 8])
        self.assertEqual(first[7], ("claude", LIVE))

    def test_a_reused_pid_is_read_again(self):
        cache = {}
        ccwho.proc_marks({7: (START, "node")}, cache)
        ccwho.proc_marks({7: ("Wed Sep 23 09:00:00 2026", "node")}, cache)
        self.assertEqual(self.reads, [7, 7])

    def test_an_exec_is_read_again(self):
        # same pid, same start, another program: the fork read the parent
        cache = {}
        ccwho.proc_marks({7: (START, "claude")}, cache)
        ccwho.proc_marks({7: (START, "node")}, cache)
        self.assertEqual(self.reads, [7, 7])

    def test_a_failed_read_is_not_remembered(self):
        ccwho.read_procargs = lambda pid: self.reads.append(pid) or None
        cache = {}
        ccwho.proc_marks({7: (START, "node")}, cache)
        ccwho.proc_marks({7: (START, "node")}, cache)
        self.assertEqual(self.reads, [7, 7])

    def test_an_unmarked_read_is_remembered(self):                    # control
        ccwho.read_procargs = lambda pid: self.reads.append(pid) or {}
        cache = {}
        ccwho.proc_marks({7: (START, "node")}, cache)
        ccwho.proc_marks({7: (START, "node")}, cache)
        self.assertEqual(self.reads, [7])

    def test_read_and_unmarked_is_not_unreadable(self):
        # None: the environment was read and names no session. Absent: it could
        # not be read. Only the second may fall back to the command line (#8)
        ccwho.read_procargs = lambda pid: {} if pid == 7 else None
        got = ccwho.proc_marks({7: (START, "node"), 8: (START, "bash")}, {})
        self.assertEqual(got, {7: None})

    def test_a_gone_process_is_forgotten(self):                       # control
        cache = {}
        ccwho.proc_marks({7: (START, "node"), 8: (START, "node")}, cache)
        ccwho.proc_marks({7: (START, "node")}, cache)
        self.assertEqual(sorted(cache["_marks"]), [(7, START, "node")])


class TestListenPorts(unittest.TestCase):
    def run_with(self, fake):
        real = ccwho.subprocess.run
        ccwho.subprocess.run = fake
        try:
            return REAL["listen_ports"]()
        finally:
            ccwho.subprocess.run = real

    def test_parsed(self):
        class Done:
            returncode, stdout, stderr = 0, "p5\nn*:3000\n", ""
        self.assertEqual(self.run_with(lambda *a, **k: Done()), {5: [3000]})

    def test_a_failed_lsof_is_unknown_not_empty(self):
        # "ports unknown" and "no ports" are different answers, like agents_json
        def boom(*a, **k):
            raise OSError("no lsof")
        self.assertIsNone(self.run_with(boom))

    def test_an_lsof_error_is_unknown(self):
        # lsof exits 1 for "nothing matched" AND for real errors: stderr tells
        class Done:
            returncode, stdout, stderr = 1, "", "lsof: WARNING: can't stat()"
        self.assertIsNone(self.run_with(lambda *a, **k: Done()))

    def test_nothing_listening_is_empty(self):                        # control
        class Done:
            returncode, stdout, stderr = 1, "", ""   # 1 with no error: nothing matched
        self.assertEqual(self.run_with(lambda *a, **k: Done()), {})


class TestCollectKnowsWhatEachSessionStarted(MachinelessCollect):
    """The processes a session started, their ports, and what was left behind -
    from the environment mark, which survives the process being orphaned."""

    PS = ("  10     1 claude\n"
          "  11    10 node server.js\n"
          "  12     1 vite --port 5173\n"
          "  13    10 npm exec chrome-devtools-mcp@latest\n"
          "  20     1 next-server (v15)\n"
          "  30     1 workerd serve\n"
          "  40     1 python3 -m http.server\n"
          f"  41     1 bash -c sleep 9 {LIVE}\n")

    def setUp(self):
        super().setUp()
        self._agents, self._read = ccwho.agents_json, ccwho.read_procargs
        ccwho.agents_json = lambda: json.dumps([{"pid": 10, "sessionId": LIVE,
                                                 "cwd": "/Users/x/p/app", "status": "idle"}])
        ccwho.ps_snapshot = lambda: self.PS
        ccwho.ps_table = lambda: {pid: (START, "claude" if pid == 10 else "node")
                                  for pid in (10, 11, 12, 13, 20, 30, 40, 41)}
        env = {11: {"CLAUDE_CODE_SESSION_ID": LIVE}, 12: {"CLAUDE_CODE_SESSION_ID": LIVE},
               13: {"CLAUDE_CODE_SESSION_ID": LIVE}, 20: {"CLAUDE_CODE_SESSION_ID": DEAD},
               30: {"CODEX_THREAD_ID": "01a0abc8-x"}}
        ccwho.read_procargs = lambda pid: env.get(pid, {})
        ccwho.listen_ports = lambda: {12: [5173], 13: [9222], 20: [3000], 30: [60805]}

    def tearDown(self):
        ccwho.agents_json, ccwho.read_procargs = self._agents, self._read
        super().tearDown()

    def collect(self):
        return ccwho.collect(cache={})

    def test_a_row_knows_its_work_and_ports(self):
        rows, _ = self.collect()
        r = rows[0]
        self.assertEqual((r["procs"], r["ports"]), (2, [5173]),
                         "the MCP helper's :9222 is not the session's work")

    def test_left_behind_and_codex_reach_the_fleet(self):
        _, fleet = self.collect()
        self.assertEqual([p["pid"] for p in fleet["left_behind"]], [20])
        self.assertEqual([p["pid"] for p in fleet["codex"]], [30])
        self.assertTrue(fleet["ports_ok"])

    def test_the_header_names_the_ports_agents_hold(self):
        _, fleet = self.collect()
        self.assertEqual([(a["port"], a["pid"]) for a in fleet["agent_ports"]],
                         [(3000, 20), (5173, 12), (60805, 30)])

    def test_ports_unknown_is_said_so(self):
        ccwho.listen_ports = lambda: None
        rows, fleet = self.collect()
        self.assertFalse(fleet["ports_ok"])
        # adversarial #5: null, not [] - a script must not read "holds nothing"
        self.assertIsNone(rows[0]["ports"])

    def _loop_on(self, text):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, "tasks", "bf6yb2pu5.output")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            f.write(text)
        os.utime(path, (1, 1))
        ccwho.ps_snapshot = lambda: (
            "  10     1 claude\n"
            f"  50    10 /bin/zsh -c until grep -q 'Test Files' {path}; do sleep 5; done\n")
        ccwho.ps_table = lambda: {10: (START, "claude"), 50: (START, "zsh")}
        # as measured 2026-09-24: a Bash tool shell is the claude's own child and
        # carries NO session mark, so procs.attribute never lists it
        ccwho.read_procargs = lambda pid: {}
        rows, _ = self.collect()
        return len(rows[0]["dead_loops"])

    def test_a_loop_on_a_finished_task_reaches_the_row(self):
        self.assertEqual(self._loop_on("partial\n[exited with code 0]\n"), 1)

    def test_a_loop_on_a_running_task_does_not(self):                 # control
        self.assertEqual(self._loop_on(" ✓ test/a.test.ts (3)\n"), 0)

    def test_the_scan_keeps_what_grep_said(self):
        real, calls = ccwho.grep_matches, []
        ccwho.grep_matches = lambda argv, files: calls.append(1) or False
        try:
            cache = {}
            self.collect = lambda: ccwho.collect(cache=cache)
            self._loop_on("partial\n[exited with code 0]\n")
            ccwho.collect(cache=cache)
        finally:
            ccwho.grep_matches = real
        self.assertEqual(len(calls), 1)

    # the regression contract for the old "detached" counts (eng D8)
    def test_d8_1_a_cmdline_named_orphan_still_counts(self):
        # 41's env is unreadable here (/bin/bash), but its command names the session
        ccwho.ps_snapshot = lambda: f"  10     1 claude\n  41     1 bash -c sleep 9 {LIVE}\n"
        ccwho.read_procargs = lambda pid: None if pid == 41 else {}
        rows, _ = self.collect()
        self.assertEqual(rows[0]["orphans"], 1)

    def test_d8_1_control_an_unrelated_orphan_does_not(self):
        ccwho.ps_snapshot = lambda: "  10     1 claude\n  40     1 python3 -m http.server\n"
        rows, _ = self.collect()
        self.assertEqual(rows[0]["orphans"], 0)

    def test_d8_2_an_env_marked_orphan_counts(self):
        # 12 names no session in its command line: today's count missed it
        ccwho.ps_snapshot = lambda: "  10     1 claude\n  12     1 vite --port 5173\n"
        rows, _ = self.collect()
        self.assertEqual(rows[0]["orphans"], 1)

    def test_d8_the_fallback_never_counts_someone_elses_process(self):
        # the command names LIVE, but the env says another session started it,
        # or it is a helper, or it is the session itself (adversarial F7)
        other = "eeee5555-0000-4000-8000-000000000005"
        ccwho.ps_snapshot = lambda: (f"  10     1 claude --resume {LIVE}\n"
                                     f"  42     1 node x {LIVE}\n"
                                     f"  43     1 npm exec some-mcp {LIVE}\n")
        ccwho.ps_table = lambda: {10: (START, "claude"), 42: (START, "node"),
                                  43: (START, "node")}
        ccwho.read_procargs = lambda pid: ({"CLAUDE_CODE_SESSION_ID": other}
                                           if pid == 42 else {})
        rows, _ = self.collect()
        self.assertEqual(rows[0]["orphans"], 0)

    def test_a_failed_process_scan_is_unknown_not_nothing(self):
        ccwho.ps_table = lambda: {}
        _, fleet = self.collect()
        self.assertFalse(fleet["procs_ok"])

    def test_a_failed_parent_scan_is_unknown_too(self):
        # adversarial #3: no ps snapshot means no parents and no commands
        ccwho.ps_snapshot = lambda: ""
        _, fleet = self.collect()
        self.assertFalse(fleet["procs_ok"])

    def test_both_scans_working_is_known(self):                      # control
        _, fleet = self.collect()
        self.assertTrue(fleet["procs_ok"])

    def test_with_the_agents_list_down_nothing_is_left_behind(self):
        # adversarial #1: the live sessions are not known, so a process whose
        # session is missing may belong to one that is running
        ccwho.agents_json = lambda: None
        _, fleet = self.collect()
        self.assertEqual(fleet["left_behind"], [])
        self.assertIn(20, [p["pid"] for p in fleet["unsure"]])
        self.assertFalse(fleet["sessions_ok"])
        self.assertNotIn("left behind", {a["who"] for a in fleet["agent_ports"]})
        # the port is still held, and the header still says so
        self.assertIn((3000, "not sure"),
                      {(a["port"], a["who"]) for a in fleet["agent_ports"]})

    def test_a_running_claude_that_is_not_listed_means_the_list_is_incomplete(self):
        # review 4 #4: pid 50 is a claude no source lists; 20's session may be it
        ccwho.ps_snapshot = lambda: self.PS + "  50     1 claude -p fix it\n"
        table = {**ccwho.ps_table(), 50: (START, "claude")}
        ccwho.ps_table = lambda: table
        _, fleet = self.collect()
        self.assertFalse(fleet["sessions_ok"])
        self.assertEqual(fleet["left_behind"], [])

    def test_with_a_session_file_unreadable_nothing_is_left_behind(self):
        # cycle 3 #2: one file that could not be read may be the session that
        # started 20
        ccwho.live_file_sessions = lambda *a, **k: ([], 1)
        _, fleet = self.collect()
        self.assertEqual(fleet["left_behind"], [])
        self.assertFalse(fleet["sessions_ok"])

    def test_with_a_claudes_env_unreadable_nothing_is_left_behind(self):
        # its CLAUDE_CONFIG_DIR is where its session file is: not read, not found
        real = ccwho.read_procargs
        ccwho.read_procargs = lambda pid: None if pid == 10 else real(pid)
        _, fleet = self.collect()
        self.assertEqual(fleet["left_behind"], [])
        self.assertFalse(fleet["sessions_ok"])

    def test_no_environment_readable_at_all_is_unknown(self):
        # cycle 3 #7: every read failed - "no processes" would be a lie
        ccwho.read_procargs = lambda pid: None
        _, fleet = self.collect()
        self.assertFalse(fleet["procs_ok"])

    def test_a_port_whose_holder_could_not_be_read_is_unknown(self):
        # lsof says 40 holds :8080; 40's environment could not be read
        real = ccwho.read_procargs
        ccwho.read_procargs = lambda pid: None if pid == 40 else real(pid)
        ccwho.listen_ports = lambda: {12: [5173], 40: [8080]}
        _, fleet = self.collect()
        self.assertEqual(fleet["unknown_ports"], [8080])

    def test_a_port_whose_holder_was_read_is_known(self):             # control
        ccwho.listen_ports = lambda: {12: [5173], 40: [8080]}
        _, fleet = self.collect()
        self.assertEqual(fleet["unknown_ports"], [])

    def test_with_the_file_scan_crashed_nothing_is_left_behind(self):
        def crash(*a, **k):
            raise OSError("scan failed")
        ccwho.live_file_sessions = crash
        _, fleet = self.collect()
        self.assertEqual(fleet["left_behind"], [])
        self.assertFalse(fleet["sessions_ok"])

    def test_with_the_agents_list_up_left_behind_is_left_behind(self):  # control
        _, fleet = self.collect()
        self.assertEqual([p["pid"] for p in fleet["left_behind"]], [20])
        self.assertTrue(fleet["sessions_ok"])

    def test_ccwho_is_not_its_own_callers_work(self):
        # adversarial #2: `ccwho ps` run from a session lists itself
        me = os.getpid()
        ccwho.ps_snapshot = lambda: self.PS + f"  {me}    10 python3 ccwho.py ps\n"
        table = {pid: (START, "claude" if pid == 10 else "node")
                 for pid in (10, 11, 12, 13, 20, 30, 40, 41, me)}
        ccwho.ps_table = lambda: table
        real = ccwho.read_procargs
        ccwho.read_procargs = lambda pid: ({"CLAUDE_CODE_SESSION_ID": LIVE} if pid == me
                                           else real(pid))
        _, fleet = self.collect()
        self.assertNotIn(me, [p["pid"] for p in fleet["by_session"][LIVE]])
        self.assertIn(11, [p["pid"] for p in fleet["by_session"][LIVE]])      # control

    def test_the_named_fallback_is_listed_where_it_is_counted(self):
        # adversarial #8: the row's "+1 detached" and `ccwho ps` must agree
        ccwho.ps_snapshot = lambda: f"  10     1 claude\n  41     1 bash -c sleep 9 {LIVE}\n"
        ccwho.read_procargs = lambda pid: None           # /bin/bash hides its env
        rows, fleet = self.collect()
        self.assertEqual(rows[0]["orphans"], 1)
        self.assertEqual([p["pid"] for p in fleet["by_session"][LIVE]], [41])

    def test_a_readable_unmarked_env_is_not_the_named_fallback(self):
        # its environment was read and names no session: nobody's, whatever its
        # command line says
        ccwho.ps_snapshot = lambda: f"  10     1 claude\n  41     1 node x {LIVE}\n"
        ccwho.read_procargs = lambda pid: {}
        rows, fleet = self.collect()
        self.assertEqual(rows[0]["orphans"], 0)

    def test_d8_4_the_header_no_longer_counts_pid1(self):
        rows, fleet = self.collect()
        out = ccwho.render(rows, fleet, color=False, width=200)
        self.assertNotIn("under your home on PID 1", out)

    def test_d9_process_data_changes_no_state_or_order(self):
        rows_with, _ = self.collect()
        ccwho.read_procargs = lambda pid: {}
        ccwho.listen_ports = lambda: {}
        rows_without, _ = self.collect()
        pick = lambda rows: [(r["sessionId"], r.get("attention"), r["status"]) for r in rows]
        self.assertEqual(pick(rows_with), pick(rows_without))


class TestTheTableSaysWhatAgentsHold(unittest.TestCase):
    """One dim header line for the ports agents hold, one line each at the bottom
    for Left behind and Codex - and nothing at all when there is nothing."""

    ROW = {"project": "app", "status": "idle", "attention": "stopped", "name": "n",
           "title": "t", "doing": "", "since": "1m", "tty": "", "orphans": 0,
           "sessionId": LIVE, "ports": [5173], "procs": 1}
    FLEET = {"ports_ok": True,
             "agent_ports": [{"port": 3000, "pid": 20, "who": "left behind"},
                             {"port": 5173, "pid": 12, "who": "app"}],
             "left_behind": [{"pid": 20, "ports": [3000], "command": "next-server"}],
             "codex": [{"pid": 30, "ports": [60805], "command": "workerd serve"}]}

    def out(self, fleet):
        return ccwho.render([dict(self.ROW)], fleet, color=False, width=200)

    def test_ports_left_behind_and_codex_are_each_one_line(self):
        out = self.out(self.FLEET)
        self.assertIn("agents hold :3000 left behind · :5173 app", out)
        self.assertIn("left behind: 1 process, 1 port", out)
        self.assertIn("codex: 1 process", out)

    def test_the_row_shows_its_own_ports(self):
        out = ccwho.render([dict(self.ROW)], {}, color=False, width=200)
        line = [l for l in out.splitlines() if "app" in l and "sessions" not in l][0]
        self.assertIn(":5173", line)

    def test_nothing_to_say_says_nothing(self):                       # control
        out = self.out({"ports_ok": True, "agent_ports": [], "left_behind": [],
                        "codex": []})
        for word in ("agents hold", "left behind", "codex:"):
            self.assertNotIn(word, out)

    def test_unsure_is_not_counted_as_left_behind(self):
        fleet = dict(self.FLEET, left_behind=[], agent_ports=[], unsure=[
            {"pid": 20, "ports": [3000], "command": "next-server", "why": "x"}])
        self.assertNotIn("left behind", self.out(fleet))

    def test_a_row_with_ports_unknown_still_renders(self):
        row = dict(self.ROW, ports=None)
        out = ccwho.render([row], {}, color=False, width=200)
        self.assertIn("app", out)

    def test_ports_unknown_is_not_ports_none(self):
        out = self.out({"ports_ok": False, "agent_ports": [], "left_behind": [],
                        "codex": []})
        self.assertIn("ports unknown", out)

    def test_the_old_integer_still_renders(self):                     # compat
        self.assertIn("1 sessions", ccwho.render([dict(self.ROW)], 0, color=False, width=200))


class TestDoingNeverPrintsASecret(unittest.TestCase):
    """The `doing` column shows a Bash call's command when it has no description;
    that text reaches agents (eng D10)."""

    def lines(self, command, description=None):
        inp = {"command": command}
        if description:
            inp["description"] = description
        return [json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": inp}]}})]

    def test_a_token_in_the_command_is_masked(self):
        got = ccwho.extract_doing(self.lines(f"curl -H 'Authorization: Bearer {TOKEN_E}' x"))
        self.assertNotIn(TOKEN_E, got)

    def test_a_plain_command_is_shown(self):                          # control
        self.assertEqual(ccwho.extract_doing(self.lines("npm test")), "Bash: npm test")


class TestNonUtf8ProcessesDoNotCrash(unittest.TestCase):
    """cycle 3 #11: one process with a non-UTF-8 argv (any agent can start one)
    made `ps` output undecodable, and collect() raised - blanking `ccwho ps`."""

    def test_ps_output_with_a_bad_byte_is_text(self):
        probe = subprocess.Popen([b"ccprobe\xff", b"5"], executable="/bin/sleep")
        self.addCleanup(probe.wait)
        self.addCleanup(probe.kill)
        out = ccwho.ps_snapshot()
        self.assertIn(str(probe.pid), out)                                  # control
        self.assertIn(str(probe.pid), " ".join(map(str, REAL["ps_table"]())))


class TestNoRowCarriesAControlCharacter(unittest.TestCase):
    """A row's text comes from files and programs ccwho does not control: the
    title Claude Code generated, the agents feed, the tab title. An escape
    sequence in one retitles the window or repaints the list, and the JSON goes
    to agents. Each becomes `?` (review cycle 2, adversarial #9)."""

    ESC = "a\x1b]0;pwned\x07b\x9bc"

    def row(self, esc):
        session = {"sessionId": LIVE, "pid": 10, "status": "idle", "name": esc,
                   "cwd": f"/Users/x/{esc}", "waitingFor": esc}
        tail = [json.dumps({"type": "ai-title", "aiTitle": esc})]
        return ccwho.build_row(session, [], tail, mtime=1788203600, tty="",
                               tab_title=esc)

    def test_no_text_field_carries_one(self):
        row = self.row(self.ESC)
        for k in ("name", "title", "tab_title", "project", "cwd", "waitingFor"):
            with self.subTest(field=k):
                self.assertNotRegex(str(row[k]), r"[\x00-\x1f\x7f-\x9f]")
                self.assertIn("b", str(row[k]))

    def test_a_one_line_field_has_one_line(self):
        # cycle 3 #6: a newline in a title fakes a line in `ccwho ps` - such as a
        # "left behind" pid for an agent to kill
        row = self.row("x\n4242    :3000   left behind")
        for k in ("name", "title", "tab_title", "project", "cwd", "waitingFor"):
            with self.subTest(field=k):
                self.assertNotIn("\n", row[k])
                self.assertIn("left behind", row[k])

    def test_plain_text_is_untouched(self):                            # control
        row = self.row("fix the navbar")
        self.assertEqual((row["name"], row["title"], row["tab_title"]),
                         ("fix the navbar",) * 3)


class TestTheListSaysWhatAgentsHold(unittest.TestCase):
    """Slice 2b: the panel shows what the table shows, from the same functions.
    NEEDS YOU owns the screen (DX D9): process data is dim, one line each, and
    never changes a row's state, colour or order."""

    SID = "aaaa1111-0000-4000-8000-000000000001"
    ROW = {"sessionId": SID, "project": "app", "attention": "asks", "title": "fix nav",
           "tab_title": "", "since": "2m", "tty": "ttys007", "recap": "", "doing": "",
           "status": "idle", "ports": [3000, 5173], "procs": 2, "pid": 10, "name": "n"}
    FLEET = {"ports_ok": True, "procs_ok": True, "sessions_ok": True,
             "agent_ports": [{"port": 3000, "pid": 12, "who": "app"},
                             {"port": 8080, "pid": 20, "who": "left behind"}],
             "by_session": {SID: [
                 {"pid": 12, "ports": [3000], "command": "vite --port 3000", "helper": False,
                  "orphan": False, "harness": "claude", "session": SID},
                 {"pid": 13, "ports": [], "command": "npm exec some-mcp", "helper": True,
                  "orphan": False, "harness": "claude", "session": SID}]},
             "left_behind": [{"pid": 20, "ports": [8080], "command": "next-server",
                              "helper": False, "orphan": True, "harness": "claude",
                              "session": "dddd"}],
             "codex": [{"pid": 30, "ports": [], "command": "workerd serve", "helper": False,
                        "orphan": True, "harness": "codex", "session": "01a0"}],
             "unsure": [{"pid": 40, "ports": [], "command": "com.docker.backend",
                         "helper": False, "orphan": True, "harness": "codex",
                         "session": "01a0", "why": "it is an app"}]}

    # ---- one line under the header
    def test_the_ports_line(self):
        self.assertEqual(ccwho.ports_line(self.FLEET), "agents hold :3000 app · :8080 left behind")

    def test_no_ports_no_line(self):                                    # control
        self.assertEqual(ccwho.ports_line(dict(self.FLEET, agent_ports=[])), "")
        self.assertEqual(ccwho.ports_line({}), "")

    def test_ports_unknown_is_said_once(self):
        self.assertIn("ports unknown", ccwho.ports_line(dict(self.FLEET, ports_ok=False)))

    def test_the_ports_line_fits_its_width(self):
        many = [{"port": 3000 + i, "pid": i, "who": "liveapp"} for i in range(20)]
        line = ccwho.ports_line(dict(self.FLEET, agent_ports=many), width=60)
        self.assertLessEqual(len(line), 60)
        self.assertRegex(line, r"· \+\d+$")          # says how many it left out

    # ---- one collapsed line each at the bottom
    def test_the_bottom_lines_name_the_key(self):
        self.assertEqual(ccwho.bottom_lines(self.FLEET, hint="p"),
                         ["left behind: 1 process, 1 port · p to see",
                          "codex: 1 process · p to see"])

    def test_nothing_left_nothing_said(self):                           # control
        self.assertEqual(ccwho.bottom_lines(dict(self.FLEET, left_behind=[], codex=[])), [])

    # ---- the process screen and `ccwho ps`: one listing
    def test_the_listing_groups_every_process(self):
        got = [(p["pid"], p["group"]) for p in ccwho.ps_listing([self.ROW], self.FLEET)]
        self.assertEqual(got, [(12, "session"), (20, "left behind"), (30, "codex"),
                               (40, "unsure")])

    def test_the_process_screen_has_a_heading_per_group(self):
        text = ccwho.render_ps_screen(ccwho.ps_listing([self.ROW], self.FLEET), self.FLEET,
                                      width=100)
        for heading in ("app · fix nav", "LEFT BEHIND", "CODEX", "NOT SURE"):
            self.assertIn(heading, text)
        self.assertIn(":3000", text)
        self.assertIn("vite --port 3000", text)

    def test_the_process_screen_says_when_it_does_not_know(self):
        text = ccwho.render_ps_screen([], dict(self.FLEET, procs_ok=False), width=100)
        self.assertIn("unknown", text)
        self.assertNotIn("no processes", text)

    def test_a_sessions_processes_for_its_brief(self):
        lines = ccwho.session_procs_lines(self.FLEET, self.SID)
        self.assertEqual(len(lines), 1)                   # the helper is not work
        self.assertIn(":3000", lines[0])
        self.assertEqual(ccwho.session_procs_lines(self.FLEET, "nobody"), [])   # control

    # ---- the row
    def test_a_row_shows_its_ports_dim(self):
        first, _ = ccwho.ui_row_cells(self.ROW, width=120)
        meta = "".join(t for t, role in first if role == "meta")
        self.assertIn(":3000 :5173", meta)

    def test_a_row_with_work_but_no_ports_says_how_many(self):
        first, _ = ccwho.ui_row_cells(dict(self.ROW, ports=[], procs=3), width=120)
        self.assertIn("3 procs", "".join(t for t, role in first if role == "meta"))

    def test_a_row_with_nothing_says_nothing(self):                      # control
        first, _ = ccwho.ui_row_cells(dict(self.ROW, ports=[], procs=0), width=120)
        meta = "".join(t for t, role in first if role == "meta")
        self.assertNotIn("procs", meta)
        self.assertNotIn(":", meta.replace(" · ", ""))

    def test_ports_unknown_on_a_row_is_not_a_crash(self):
        ccwho.ui_row_cells(dict(self.ROW, ports=None), width=120)

    def test_process_data_never_moves_or_recolours_a_row(self):
        plain = dict(self.ROW, ports=[], procs=0)
        busy = dict(self.ROW, attention="busy", ports=[], procs=0)
        for r in (plain, busy):
            with self.subTest(attention=r["attention"]):
                loaded = dict(r, ports=[3000, 5173, 8080], procs=9, orphans=4)
                self.assertEqual(ccwho.ui_state_style(loaded), ccwho.ui_state_style(r))
                self.assertEqual([g["heading"] for g in ccwho.ui_groups([loaded])],
                                 [g["heading"] for g in ccwho.ui_groups([r])])

    # ---- search by port
    def test_a_port_finds_the_row_that_holds_it(self):
        other = dict(self.ROW, sessionId="bbbb", ports=[], title="other")
        for q in ("3000", ":3000"):
            with self.subTest(q=q):
                got = ccwho.ui_filter([self.ROW, other], q)
                self.assertEqual([r["sessionId"] for r in got], [self.SID])
        self.assertEqual(ccwho.ui_filter([self.ROW, other], ":4444"), [])      # control


class TestTheListSaysWhatAgentsHoldReview(unittest.TestCase):
    """Review of slice 2b: the name keeps its room, odd shapes do not crash, a
    title's escape codes do not reach the process screen."""

    ROW = TestTheListSaysWhatAgentsHold.ROW
    FLEET = TestTheListSaysWhatAgentsHold.FLEET

    def name_of(self, row, width):
        first, _ = ccwho.ui_row_cells(row, width=width)
        return "".join(t for t, role in first if role == "name")

    def test_ports_never_take_the_names_room(self):
        row = dict(self.ROW, title="fix the login bug in auth", tab_title="")
        loaded = dict(row, ports=[3000, 3001, 5173, 8080])
        for width in range(40, 101, 5):
            with self.subTest(width=width):
                self.assertEqual(self.name_of(loaded, width), self.name_of(row, width))
        for width in range(40, 101, 5):
            with self.subTest(width=width, part="meta"):
                first, _ = ccwho.ui_row_cells(loaded, width=width)
                meta = "".join(t for t, role in first if role == "meta")
                self.assertNotIn("…", meta, "a port is shown whole or not at all")
        first, _ = ccwho.ui_row_cells(loaded, width=160)                    # control
        self.assertIn(":3000", "".join(t for t, _ in first))

    def test_odd_shapes_do_not_crash(self):
        odd = {"ports_ok": True, "procs_ok": True,
               "agent_ports": [{"port": 3000, "pid": 1}],
               "left_behind": [{"pid": 2, "ports": None, "command": "x"}],
               "codex": [{"ports": [], "command": "y"}],
               "by_session": {self.ROW["sessionId"]: [{"pid": 3, "ports": None}]}}
        ccwho.ports_line(odd, 80)
        ccwho.bottom_lines(odd)
        ccwho.render_ps_screen(ccwho.ps_listing([self.ROW], odd), odd, width=80)
        ccwho.session_procs_lines(odd, self.ROW["sessionId"])

    def test_a_titles_escape_codes_do_not_reach_the_process_screen(self):
        row = dict(self.ROW, tab_title="evil\x1b]0;PWNED\x1b\\\x1b[31mRED\nnext")
        listing = ccwho.ps_listing([row], self.FLEET)
        for p in listing:
            self.assertNotRegex(p["who"], r"[\x00-\x1f\x7f-\x9f]")
        text = ccwho.render_ps_screen(listing, self.FLEET, width=100)
        self.assertNotRegex(text.replace("\n", ""), r"[\x00-\x1f\x7f-\x9f]")

    def test_a_bottom_line_fits_its_width(self):
        many = dict(self.FLEET, left_behind=self.FLEET["left_behind"] * 12)
        for line in ccwho.bottom_lines(many, hint="p", width=30):
            self.assertLessEqual(len(line), 30)

    def test_one_port_is_one_port(self):
        line = ccwho.ports_line(dict(self.FLEET, agent_ports=[
            {"port": 3000, "pid": 1, "who": "a-very-long-project-name-indeed"}]), width=20)
        self.assertNotIn("1 ports", line)


class TestTheListSaysWhatAgentsHoldVerify(unittest.TestCase):
    """Verification review of slice 2b's fixes."""

    ROW = TestTheListSaysWhatAgentsHold.ROW
    FLEET = TestTheListSaysWhatAgentsHold.FLEET

    def test_unknown_processes_are_said_on_the_ports_line(self):
        # #1: the line said "agents hold" while `p` said "processes unknown"
        line = ccwho.ports_line({"procs_ok": False, "ports_ok": True,
                                 "agent_ports": [{"port": 3000, "who": "x"}]}, 100)
        self.assertIn("unknown", line)
        self.assertNotIn("unknown", ccwho.ports_line(self.FLEET, 100))       # control

    def test_unknown_processes_are_said_in_the_brief(self):
        lines = ccwho.session_procs_lines(dict(self.FLEET, procs_ok=False),
                                          self.ROW["sessionId"])
        self.assertIn("unknown", " ".join(lines))
        self.assertNotIn("unknown", " ".join(                                # control
            ccwho.session_procs_lines(self.FLEET, self.ROW["sessionId"])))

    def test_a_wide_name_never_overflows_the_row(self):
        # #4: the room was counted in characters; a CJK character takes two cells
        import unicodedata
        cells = lambda s: sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)
        row = dict(self.ROW, tab_title="✳ 修复登录页面的错误并且部署",
                   ports=[3000, 5173, 8080, 9229])
        for width in range(30, 201):
            with self.subTest(width=width):
                first, _ = ccwho.ui_row_cells(row, width=width)
                self.assertLessEqual(cells("".join(t for t, _ in first)), width)

    def test_odd_ports_and_pids_do_not_crash(self):
        # #6
        for ports in (5, {"a": 1}, "3000", [None, "x"]):
            with self.subTest(ports=ports):
                ccwho.ui_row_cells(dict(self.ROW, ports=ports), 120)
                ccwho.ui_filter([dict(self.ROW, ports=ports)], ":3000")
        lines = ccwho.session_procs_lines(
            {"by_session": {"s": [{"pid": None, "ports": [1], "command": "x"}]}}, "s")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("?"))
        ccwho.ports_line({"agent_ports": ["not a dict", {"port": 1, "who": "a"}]}, 80)


class TestTheListSaysWhatAgentsHoldRound3(unittest.TestCase):
    """Third review of slice 2b's fixes."""

    FLEET = TestTheListSaysWhatAgentsHold.FLEET

    def test_not_collected_yet_says_nothing_on_the_list(self):
        # #1: "processes unknown" on every start was a false alarm
        self.assertEqual(ccwho.ports_line({"collected": False}, 100), "")
        self.assertEqual(ccwho.bottom_lines({"collected": False}), [])
        self.assertIn("not collected yet",
                      ccwho.render_ps_screen([], {"collected": False}, width=100))
        self.assertIn("unknown", ccwho.ports_line(dict(self.FLEET, procs_ok=False), 100))  # control

    def test_unknown_survives_a_narrow_line(self):
        # #2: the lead fell off and the line claimed a complete count
        unknown = dict(self.FLEET, procs_ok=False, agent_ports=[
            {"port": 3000, "pid": 1, "who": "liveapp-long-project"},
            {"port": 5173, "pid": 2, "who": "marketing-site"}])
        for width in range(10, 201):
            with self.subTest(width=width):
                line = ccwho.ports_line(unknown, width)
                self.assertLessEqual(len(line), width)
                self.assertTrue(line == "" or "unknown" in line, line)
                self.assertNotIn("unknown", ccwho.ports_line(self.FLEET, width))   # control

    def test_odd_ports_do_not_break_the_brief_or_the_screen(self):
        # #3
        for ports in (5, "3000", [None, "x", 3000]):
            with self.subTest(ports=ports):
                fleet = {"procs_ok": True, "by_session": {"s": [
                    {"pid": 1, "ports": ports, "command": "x"}]}, "left_behind": [
                    {"pid": 2, "ports": ports, "command": "y"}]}
                self.assertEqual(len(ccwho.session_procs_lines(fleet, "s")), 1)
                ccwho.render_ps_screen(ccwho.ps_listing([], fleet), fleet, width=80)
                ccwho.bottom_lines(fleet)


class TestRowsWithUsage(unittest.TestCase):
    """Usage adds a line under the header and, with 2+ accounts, a tag at the end
    of each row. A row with no tag must be EXACTLY what it was before usage
    existed: test_fixture_rows_before_usage.json was captured from the engine
    before this change (critical regression contract, eng review 2026-09-25)."""

    @classmethod
    def setUpClass(cls):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "test_fixture_rows_before_usage.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
        cls.rows, cls.expected = data["rows"], data["expected"]

    def as_json(self, value):
        return json.loads(json.dumps(value, ensure_ascii=False))

    def test_rows_without_a_tag_are_byte_identical_to_before(self):
        for w, want in self.expected["cells"].items():
            got = [ccwho.ui_row_cells(r, width=int(w)) for r in self.rows]
            self.assertEqual(self.as_json(got), want, f"width {w}")
            got = [ccwho.ui_row_cells(r, width=int(w), tag="") for r in self.rows]
            self.assertEqual(self.as_json(got), want, f"width {w}, empty tag")

    def test_the_table_without_usage_is_byte_identical_to_before(self):
        for key, want in self.expected["render"].items():
            w, color = int(key.rstrip("c")), key.endswith("c")
            self.assertEqual(ccwho.render(self.rows, {}, color=color, width=w), want, key)

    def test_a_tag_is_the_last_cell_and_the_name_gives_way(self):
        row = self.rows[0]
        for w in (60, 100, 160):
            first, _ = ccwho.ui_row_cells(row, width=w, tag="1a2b")
            # last of the words; only the › that opens the detail comes after it
            self.assertEqual(first[-3], (" · 1a2b", "account"))
            self.assertEqual([r for _, r in first[-2:]], ["pad", "detail"])
            self.assertEqual(first[0][1], "mark")
            self.assertLessEqual(sum(ccwho._cells(t) for t, _ in first), w)
            plain, _ = ccwho.ui_row_cells(row, width=w)
            self.assertEqual(first[:3], plain[:3])       # mark, id, project untouched

    def test_a_long_name_gives_way_and_the_tag_stays_whole(self):
        row = dict(self.rows[0], tab_title="a very long tab title " * 6)
        for w in (60, 100):
            first, _ = ccwho.ui_row_cells(row, width=w, tag="lukaso@gmail")
            self.assertEqual(first[-3], (" · lukaso@gmail", "account"))
            name = [t for t, role in first if role == "name"][0]
            self.assertTrue(name.endswith("…"), name)
            self.assertLessEqual(sum(ccwho._cells(t) for t, _ in first), w)

    def usage_snap(self):
        import ccwho_usage as usage
        now = 1790340000.0
        rows = [{"id": "login:a", "kind": "login", "email": "lukaso@gmail.com", "brand": "ant",
                 "label": "", "sessions": 1, "age": 5,
                 "five_hour": {"state": "ok", "pct": 42, "resets_at": now + 7200, "age": 5},
                 "seven_day": None},
                {"id": "token:1a2b3c4d", "kind": "token", "email": "", "brand": "ant",
                 "label": "", "sessions": 1, "age": 5,
                 "five_hour": {"state": "ok", "pct": 0, "resets_at": None, "age": 5},
                 "seven_day": None}]
        names = usage.short_names(rows, {})
        return {"state": "ok", "accounts": usage.ordered(rows, names), "names": names,
                "sessions": {self.rows[0]["sessionId"]: "login:a",
                             self.rows[2]["sessionId"]: "token:1a2b3c4d"}, "now": now}

    def test_the_table_shows_the_usage_line_under_the_header_and_tags(self):
        out = ccwho.render(self.rows, {"usage": self.usage_snap()}, color=False, width=150)
        lines = out.splitlines()
        self.assertTrue(lines[1].startswith("usage  ant lukaso 5h 42%"), lines[1])
        body = [l for l in lines if l.startswith(("liveapp", "ccwho", "marketing"))]
        before = [l for l in self.expected["render"]["150"].splitlines()
                  if l.startswith(("liveapp", "ccwho", "marketing"))]
        # the tag follows the age column; the doing column gives up its width,
        # so a row is exactly as wide as it was
        for row, line, old, tag in zip(self.rows, body, before, ("lukaso", "?", "1a2b")):
            self.assertIn(f"{row['since']:>5}  {tag}", line)
            self.assertEqual(ccwho._cells(line), ccwho._cells(old), line)

    def test_a_broken_usage_snapshot_never_breaks_the_table(self):
        out = ccwho.render(self.rows, {"usage": {"state": "ok", "accounts": [{"id": 3}]}},
                           color=False, width=150)
        self.assertIn("usage  unknown", out)
        rows_part = out.split("\n", 3)[3]
        self.assertIn("liveapp", rows_part)

    def test_usage_off_adds_nothing(self):
        out = ccwho.render(self.rows, {"usage": {"state": "off"}}, color=False, width=150)
        self.assertEqual(out, self.expected["render"]["150"])


class TestTableTagWidth(unittest.TestCase):
    ROWS = TestRowsWithUsage

    def setUp(self):
        TestRowsWithUsage.setUpClass()
        self.rows, self.expected = TestRowsWithUsage.rows, TestRowsWithUsage.expected

    def snap(self, names):
        import ccwho_usage as usage
        accts = [{"id": aid, "kind": "login", "email": "", "brand": "ant", "label": "",
                  "sessions": 1, "age": 1,
                  "five_hour": {"state": "ok", "pct": 1, "resets_at": None, "age": 1},
                  "seven_day": None} for aid in names]
        return {"state": "ok", "accounts": accts, "names": dict(names),
                "sessions": {self.rows[0]["sessionId"]: list(names)[0],
                             self.rows[2]["sessionId"]: list(names)[1]}, "now": 0}

    def body(self, out):
        return [l for l in out.splitlines() if l.startswith(("liveapp", "ccwho", "marketing"))]

    def test_a_wide_tag_takes_the_same_cells_on_every_row(self):
        """The tag column, measured in cells: a CJK tag is 2 cells per character.
        (A CJK title already put the old table out of line; that is not this.)"""
        out = ccwho.render(self.rows, {"usage": self.snap({"a": "日本語", "b": "bb"})},
                           color=False, width=150)
        cols = set()
        for row, line in zip(self.rows, self.body(out)):
            after = line.split(f"{row['since']:>5}", 1)[1]
            tag_part = re.split(r"  (?=[:+])", after)[0]
            cols.add(ccwho._cells(tag_part))
        self.assertEqual(len(cols), 1, self.body(out))
        self.assertIn("日本語", self.body(out)[0])      # whole, when there is room

    def test_a_long_tag_never_makes_a_row_wider_than_before(self):
        names = {"a": "someone.with.a.long.name@example-company.com", "b": "bb"}
        for w in (80, 150):
            out = ccwho.render(self.rows, {"usage": self.snap(names)}, color=False, width=w)
            old = [l for l in self.expected["render"][str(w)].splitlines()
                   if l.startswith(("liveapp", "ccwho", "marketing"))]
            for new, before in zip(self.body(out), old):
                self.assertLessEqual(ccwho._cells(new), ccwho._cells(before), new)


class TestShortTagsKeepTheirColumn(unittest.TestCase):
    def test_one_cell_tags_are_shown(self):
        TestRowsWithUsage.setUpClass()
        rows = TestRowsWithUsage.rows
        accts = [{"id": a, "kind": "login", "email": "", "brand": "ant", "label": "",
                  "sessions": 1, "age": 1,
                  "five_hour": {"state": "ok", "pct": 1, "resets_at": None, "age": 1},
                  "seven_day": None} for a in ("q", "r")]
        snap = {"state": "ok", "accounts": accts, "names": {"q": "q", "r": "r"},
                "sessions": {rows[0]["sessionId"]: "q", rows[2]["sessionId"]: "r"}, "now": 0}
        out = ccwho.render(rows, {"usage": snap}, color=False, width=150)
        self.assertIn(f"{rows[0]['since']:>5}  q", out)
        self.assertIn(f"{rows[2]['since']:>5}  r", out)


class TestTheBriefInParts(unittest.TestCase):
    """The detail pane copies what you click: each value of the brief is a part
    that knows its whole value, even where the screen shows it cut. The brief
    printed by `ccwho brief` must not change for it (the golden fixture was
    captured from render_brief before the parts existed)."""

    LONG = "a very long thing that goes on " * 6
    AKA = {"short_id": "4f2b", "tty": "/dev/ttys017", "pid": 4242, "name": "liveapp-40",
           "cwd": "/Users/u/projects/liveapp",
           "session_id": "4f2b91ac-1111-4222-8333-abcdefabcdef"}

    def brief(self, **over):
        b = dict(title="Laptop crash", recap="S4 is paused. Continue?", recap_age="2d",
                 turns_since_recap=23, you_said=self.LONG, goal="investigate the crash",
                 progress=["read logs", self.LONG], closing="Do you want me to continue?",
                 aka=dict(self.AKA))
        b.update(over)
        return b

    def copies(self, b, row=None):
        return [c for line in ccwho.brief_parts(b, row or {}) for _, _, c in line if c]

    def test_render_brief_is_byte_identical_to_before(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "test_fixture_render_brief.json"), encoding="utf-8") as fh:
            cases = json.load(fh)
        self.assertGreater(len(cases), 8)
        for i, case in enumerate(cases):
            with self.subTest(case=i, color=case["color"]):
                self.assertEqual(ccwho.render_brief(case["brief"], case["row"],
                                                    color=case["color"]), case["expected"])

    def test_the_parts_are_the_brief(self):
        b, row = self.brief(), {"attention": "asks", "project": "liveapp"}
        lines = ["".join(t for t, _, _ in line) for line in ccwho.brief_parts(b, row)]
        self.assertEqual("\n".join(lines), ccwho.render_brief(b, row, color=False))

    def test_every_value_can_be_copied_whole(self):
        b = self.brief()
        got = self.copies(b, {"project": "liveapp", "tty": "/dev/ttys017"})
        for want in ("4f2b", "liveapp", "Laptop crash", "S4 is paused. Continue?",
                     "investigate the crash", self.LONG.strip(),
                     "Do you want me to continue?", "/dev/ttys017", "4242", "liveapp-40",
                     "/Users/u/projects/liveapp", "4f2b91ac-1111-4222-8333-abcdefabcdef",
                     "claude --resume 4f2b91ac-1111-4222-8333-abcdefabcdef"):
            with self.subTest(want=want[:30]):
                self.assertIn(want, got)
        # the screen cuts the long ones; what you copy is never the cut
        shown = ccwho.render_brief(b, {}, color=False)
        self.assertNotIn(self.LONG, shown)                                  # control
        self.assertFalse(any(c.endswith("…") for c in got), got)

    def test_a_label_is_not_a_value(self):
        got = self.copies(self.brief(), {"attention": "asks", "project": "liveapp"})
        for label in ("tty", "pid", "resume", "opened", "you said", "progress", "it said",
                      "ASKED YOU", "recap 2d old · 23 turns since", " · "):
            with self.subTest(label=label):
                self.assertNotIn(label, got)
        self.assertFalse(any(c != c.strip() for c in got), got)

    def test_what_is_not_there_is_not_offered(self):
        bare = self.brief(title="", recap="", you_said="", goal="", progress=[], closing="",
                          aka=dict(self.AKA, tty="", pid=0, name="", cwd=""))
        got = self.copies(bare, {})
        self.assertNotIn("", got)
        self.assertNotIn("0", got)
        for project in ("?", ""):                   # no project is not a value
            with self.subTest(project=project):
                self.assertNotIn(project, self.copies(bare, {"project": project}))
        self.assertIn("app", self.copies(bare, {"project": "app"}))       # control
        self.assertEqual(got.count("4f2b91ac-1111-4222-8333-abcdefabcdef"), 1)

    def test_escape_codes_are_not_what_you_paste(self):
        b = self.brief(you_said="pasted \x1b[31mRED\x1b[0m done",
                       recap="a\x1b]52;c;aGk=\x07b", closing="c\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\d")
        got = self.copies(b)
        self.assertIn("pasted RED done", got)
        self.assertIn("ab", got)
        self.assertIn("clinkd", got)
        # a private CSI (cursor hide) and a bare ESC leave nothing behind
        # ESC d is a whole escape, as a terminal reads it: the d goes with it
        got2 = self.copies(self.brief(you_said="a\x1b[?25lb\x1b[?25h c\x1bd e\x1b"))
        self.assertIn("ab c e", got2)
        self.assertFalse(any("\x1b" in c or "\x07" in c for c in got), got)
        self.assertIn("Laptop crash", got)                                 # control

    def test_the_text_the_pane_draws_has_no_escape_left(self):
        for raw, want in (("c\x1bd e\x1b", "c e"), ("run \x1b[1mmake\x1b(B\x1b[m now", "run make now"),
                          ("a\x1b]0;title", "a"), ("  keep  my spaces ", "  keep  my spaces "),
                          ("x [1m] y\r\n", "x [1m] y\r\n")):
            with self.subTest(raw=raw):
                self.assertEqual(ccwho.plain_text(raw), want)

    def test_a_project_of_none_is_not_a_crash(self):
        shown = ccwho.render_brief(self.brief(), {"project": None}, color=False)
        self.assertEqual(shown.splitlines()[0], "4f2b  ?")
        self.assertNotIn(None, self.copies(self.brief(), {"project": None}))

    def test_what_you_paste_is_what_the_pane_shows(self):
        for said, want in (("run \x1b[1mmake\x1b(B\x1b[m now", "run make now"),   # tput sgr0
                           ("a\x1bMb", "ab"),
                           ("a\x1b]0;title", "a"),                 # an OSC never ended
                           ("x [1m] y", "x [1m] y"),               # control: no ESC at all
                           ("arr[0] (B) M", "arr[0] (B) M")):      # control
            with self.subTest(said=said):
                self.assertIn(want, self.copies(self.brief(you_said=said)))

    def test_the_cut_is_made_on_what_is_shown(self):
        said = "x" * 95 + "\x1b[31mRED\x1b[0m tail " + "y" * 20
        line = next(l for l in ccwho.render_brief(self.brief(you_said=said), {}, color=False)
                    .splitlines() if "you said" in l)
        self.assertNotIn("31", line)
        self.assertTrue(line.endswith("…"), line)
        self.assertIn("xRED", line, "the cut counts what is shown, not the codes")
        link = "x" * 97 + "\x1b]8;;http://example.com\x1b\\link\x1b]8;;\x1b\\ and more"
        line = next(l for l in ccwho.render_brief(self.brief(you_said=link), {}, color=False)
                    .splitlines() if "you said" in l)
        self.assertTrue(line.endswith("…"), line)

    def test_mojibake_is_text_not_escapes(self):
        for text in ("say “hi” ok and more text".encode().decode("latin-1"),
                     "Děkuji".encode().decode("latin-1")):
            with self.subTest(text=text):
                self.assertEqual(ccwho.plain_text(text), text)

    def test_an_osc_ends_where_a_terminal_ends_it(self):
        self.assertEqual(ccwho.plain_text("a\x1b]0;secret title\x1bXb rest"), "ab rest")
        self.assertEqual(ccwho.plain_text("a\x1b]0;title\x18b"), "ab")
        self.assertEqual(ccwho.plain_text("a\x1b]0;title\x1ab"), "ab")
        self.assertEqual(ccwho.plain_text("a\x1b]52;c;aGk=\x07b"), "ab")          # control

    def test_a_copy_has_the_line_ends_the_pane_shows(self):
        got = self.copies(self.brief(you_said="first\r\nsecond", closing="50%\r100% done"))
        self.assertIn("first\nsecond", got)
        self.assertIn("100% done", got)

    def test_a_cut_value_is_cut_last(self):
        for field in ("goal", "you_said", "closing"):
            with self.subTest(field=field):
                def shown_and_copied(raw):
                    b = self.brief(**{field: raw})
                    lines = ccwho.brief_parts(b, {})
                    for line in lines:
                        for t, _, v in line:
                            if v and v.startswith(("a" * 20, "rest", "short")) or v == "short":
                                return t, v
                    self.fail(f"no part for {raw!r}")
                t, v = shown_and_copied("a" * 98 + "\r\nrest of it")
                self.assertTrue(t.startswith("a" * 90), t)
                t, v = shown_and_copied("x" * 150 + "\rshort")
                self.assertEqual((t, v), ("short", "short"))

    def test_bel_backspace_vt_and_ff_are_not_pasted(self):
        for ch in ("\x07", "\x08", "\x0b", "\x0c"):
            for field in ("you_said", "title"):             # cut, and not cut
                with self.subTest(ch=repr(ch), field=field):
                    got = self.copies(self.brief(**{field: f"a{ch}b"}))
                    self.assertIn("ab", got)
                    self.assertNotIn(f"a{ch}b", got)

    def test_an_osc_that_never_ends_ends_with_its_line(self):
        self.assertEqual(ccwho.plain_text("a\x1b]0;t\nnext line"), "a\nnext line")
        self.assertEqual(ccwho.plain_text("a\x1b]0;title"), "a")                   # control

    def test_a_progress_step_is_shown_not_offered(self):
        b = self.brief(progress=["read logs", "ran the gate"])
        shown = ccwho.render_brief(b, {}, color=False)
        self.assertIn("read logs", shown)
        self.assertIn("ran the gate", shown)
        got = self.copies(b)
        self.assertNotIn("read logs", got)
        self.assertNotIn("ran the gate", got)
        self.assertIn("investigate the crash", got)                         # control

    def test_a_progress_step_is_cut_last_too(self):
        shown = ccwho.render_brief(self.brief(progress=["x" * 150 + "\rshort"]), {}, color=False)
        self.assertIn("    · short", shown.splitlines())
        shown = ccwho.render_brief(self.brief(progress=["a" * 98 + "\r\nrest of it"]), {},
                                   color=False)
        self.assertTrue(any(l.startswith("    · " + "a" * 90) for l in shown.splitlines()), shown)

    def test_every_value_says_which_field_it_is(self):
        b = self.brief()
        fields = [(f, v) for line in ccwho.brief_parts(b, {"project": "liveapp"}, fields=True)
                  for _, _, v, f in line if v or f]
        self.assertEqual([f for f, _ in fields],
                         ["id", "project", "title", "recap", "goal", "you_said", "closing",
                          "tty", "pid", "name", "cwd", "session_id", "resume"])
        self.assertEqual(tuple(f for f, _ in fields), ccwho.BRIEF_FIELDS,
                         "the order a screen follows is the order they come in")
        self.assertTrue(all(v for _, v in fields), "a field is a value; a label has none")
        # a value that is not there has no field either: no project, no "project"
        bare = [f for line in ccwho.brief_parts(b, {}, fields=True) for _, _, _, f in line if f]
        self.assertNotIn("project", bare)
        self.assertIn("id", bare)                                           # control

    def test_a_list_running_since_before_keeps_the_shape_it_reads(self):
        # the engine is hot-reloaded into a list that stays open for days; a
        # list from before fields reads three per part, and must not crash
        for line in ccwho.brief_parts(self.brief(), {"project": "liveapp"}):
            for part, style, value in line:
                self.assertIsInstance(part, str)
        with_fields = ccwho.brief_parts(self.brief(), {"project": "liveapp"}, fields=True)
        self.assertEqual({len(p) for line in with_fields for p in line}, {4})    # control
