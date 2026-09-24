"""Tests for ccwho. Stdlib only: python3 -m unittest -v"""
import json
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

    def test_plain_statement_is_not_an_ask(self):
        self.assertFalse(ccwho.asks_user("Idle and ready."))

    def test_report_of_results_is_not_an_ask(self):
        self.assertFalse(ccwho.asks_user("234 tests green, typecheck clean."))

    def test_the_word_question_alone_is_not_an_ask(self):
        self.assertFalse(ccwho.asks_user("Still open, not blocking: the spend labelling question."))

    def test_only_the_closing_line_counts(self):
        self.assertFalse(ccwho.asks_user("Should I do X?\n\nDone, all landed."))

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
        self.assertEqual(set(self.roles(second)) - {"pad"}, {"age", "recap"},
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
