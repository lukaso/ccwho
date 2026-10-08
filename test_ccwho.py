"""Tests for ccwho. Stdlib only: python3 -m unittest -v"""
import calendar
import errno
import fcntl
import json
import re
import os
import shutil
import shlex
import subprocess
import tempfile
import sys
import threading
import time
import unittest

import ccwho_brief as brief
import ccwho_engine as ccwho
import testkit

# Reading this machine's processes, ports and session files is machine state: a
# test that reaches it answers differently on every laptop and every minute. Every
# test in this module runs with guards that fail loudly instead; a test that means
# to exercise a real function takes it from REAL and puts the guard back.
# The terminal apps are in ccwho_terms, which a hot reload SWAPS rather than
# re-reads in place: its guards go on the live module (ccwho.terms), taken fresh
# from its file each time they go on (testkit.fresh_terms), and nothing of it
# is kept from an import.
TERMS_NAMES = {"iterm_ask": ("ITERM2", "ask")}


def _where(name):
    """(object, attribute) a guarded name lives at now."""
    if name in TERMS_NAMES:
        owner, attr = TERMS_NAMES[name]
        return getattr(ccwho.terms, owner), attr
    return ccwho, name


REAL = {name: getattr(ccwho, name) for name in ("live_file_sessions", "ps_table",
                                                "listen_ports", "read_codex_threads",
                                                # a sample stops a real process
                                                "stack_sample", "open_fds")}
REAL_LIVE_FILE_SESSIONS = REAL["live_file_sessions"]
REAL_TTY_SNAPSHOT = ccwho.tty_snapshot


def _guard(name):
    def unpinned(*a, **k):
        raise AssertionError(f"a test reached this machine through ccwho.{name} - "
                             "pin it (see MachinelessCollect)")
    return unpinned


GUARDS = {name: _guard(name) for name in REAL}
# an Apple Event to a real terminal app: "could not be asked" is an answer every
# caller handles, so the stand-in is that answer, not a failure. Both on
# iTerm2's own ask and on the gate every app's asks go through (terms.ask)
GUARDS["iterm_ask"] = lambda *a, **k: None
_unpinned_live_file_sessions = GUARDS["live_file_sessions"]
TERMS_GATE_GUARD = lambda *a, **k: None       # noqa: E731


def install_guards():
    for name, guard in GUARDS.items():
        if name not in TERMS_NAMES:
            setattr(ccwho, name, guard)
    live = testkit.fresh_terms(ccwho)
    # the real ones of the module now live, before its guards go on
    REAL["terms.ask"], REAL["terms.app_snapshot"] = live.ask, live.app_snapshot
    REAL["iterm_ask"] = lambda args, **k: REAL["terms.ask"](args, app=ccwho.terms.ITERM2, **k)
    for name in TERMS_NAMES:
        setattr(*_where(name), GUARDS[name])
    live.ask = TERMS_GATE_GUARD


_UNPIN = []
REAL_VERDICTS_PATH = ccwho.verdicts_path


def pin_verdicts():
    """The stuck-reader verdicts file, in a temp dir for the module: collect()
    writes it, and a test must never write the user's own (~/.cache/ccwho)."""
    tmp = tempfile.mkdtemp(prefix="ccwho-test-verdicts-")
    ccwho.verdicts_path = lambda: os.path.join(tmp, "readers.json")

    def undo():
        ccwho.verdicts_path = REAL_VERDICTS_PATH
        shutil.rmtree(tmp, True)
    return undo


def setUpModule():
    install_guards()
    _UNPIN.append(pin_verdicts())
    # a scan's digest stats the Codex lock folder: never the user's own
    _UNPIN.append(testkit.pin_codex_home())


def tearDownModule():
    for name, real in REAL.items():
        if name not in TERMS_NAMES and not name.startswith("terms."):
            setattr(ccwho, name, real)
    testkit.fresh_terms(ccwho)                  # no guard or fake of ours stays on it
    while _UNPIN:
        _UNPIN.pop()()


class MachinelessCollect(unittest.TestCase):
    """collect() reaches the machine: ps, the tty map, and an AppleScript round
    trip to iTerm2. A test that leaves those live measures this laptop, takes a
    second each, and answers differently on someone else's. Pin them."""

    def setUp(self):
        self._saved = (ccwho.ps_snapshot, ccwho.tty_snapshot,
                       ccwho.live_file_sessions, ccwho.ps_table, ccwho.listen_ports,
                       ccwho.stdin_sockets, ccwho.read_codex_threads)
        ccwho.ps_snapshot = lambda: ""
        ccwho.tty_snapshot = lambda: ""
        # on the app object of now, taken off that same object: a module swapped
        # in the meantime must not get this one's methods
        self._apps = ccwho.terms.APPS
        for app in self._apps:
            app.titles = lambda timeout=5.0, **k: {}
        # the session files, the start times and the ports of THIS machine are
        # machine state too
        ccwho.live_file_sessions = lambda *a, **k: ([], 0)
        ccwho.ps_table = lambda: {}
        ccwho.listen_ports = lambda: {}
        ccwho.stdin_sockets = lambda pids: {}
        ccwho.read_codex_threads = lambda env=None, cache=None: []  # Codex's locks: machine
        # what one scan proved reaches the next on disk: each test its own file
        tmp = tempfile.mkdtemp(prefix="ccwho-test-verdicts-")
        self.addCleanup(shutil.rmtree, tmp, True)
        testkit.patch(self, ccwho, "verdicts_path", lambda: os.path.join(tmp, "readers.json"))

    def tearDown(self):
        (ccwho.ps_snapshot, ccwho.tty_snapshot,
         ccwho.live_file_sessions, ccwho.ps_table, ccwho.listen_ports,
         ccwho.stdin_sockets, ccwho.read_codex_threads) = self._saved
        for app in self._apps:
            app.__dict__.pop("titles", None)


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


class TestAnAskPhraseAsksYou(unittest.TestCase):
    """A phrase is an ask only as whole words, and a phrase with "me" in it only
    when you are the one asked. Real closing lines (2026-10-08): a session that
    reported what its marker files would tell it sat in ASKED YOU for 17 hours."""

    def test_another_subject_reports_and_does_not_ask(self):
        for line in (
                "- The marker files tell me which leftovers come from old code, from kept "
                "RED gates, or from killed runs. With that I can say whether the "
                "follow-up step (S4) is needed: collecting finished leftovers after a day.",
                "That run will tell me which. Once it's green I'll dispatch the third review.",
                "If they edit agent-env.ts we'd collide - better they tell me before landing.",
                "The reviewers want me to split it, so it is split.",
                "The logs let me know when the gate ends.",
                "Your logs tell me which.",                         # "your" is not "you"
                "The Bayou logs tell me which.",
                "The tests then tell me whether it holds.",
                "When you are back, the logs tell me which.",
                "The two gates (ruff and mypy) tell me nothing new.",
                "The step-by-step and the how-to tell me nothing new.",
                # a quote is a thing said, not a clause of its own
                'Both "git status" and "git log" tell me the tree is clean.',
                "Rows 1-3 tell me nothing."):
            with self.subTest(line=line):
                self.assertFalse(ccwho.asks_user(line))

    def test_a_phrase_inside_a_longer_word_is_not_one(self):
        self.assertFalse(ccwho.asks_user(
            "This old CI event keeps starting new cycles, so the engine should ignore "
            "events it already handled."))
        self.assertFalse(ccwho.asks_user(
            "Nothing outstanding from you at this point; both your calls are applied."))
        self.assertFalse(ccwho.asks_user("That came from Marshall I think."))

    def test_you_in_its_clause_asks(self):                       # control
        # whatever stands between: you are the one asked
        for line in (
                "I do nothing further until you tell me to.",
                "You’ll tell me when it is done.",
                "You'll need to tell me which branch to keep.",
                "If you don't want me to land it, say so.",
                "You can rm it (or explicitly tell me to).",
                # a quote that does not end the sentence leaves "you" in it
                'If you see "Saved." in the footer please tell me.'):
            with self.subTest(line=line):
                self.assertTrue(ccwho.asks_user(line))

    def test_no_subject_still_asks(self):                        # control
        # one line for each way the clause can start: each would ask on its own
        for line in (
                "Tell me if it can go.",                            # the line starts
                "So **tell me** which.",
                # a word that joins it to what you were asked first
                "One thing for you: press ⌥/ and tell me what happens.",
                "I can leave the branch as it is, or want me to delete it.",
                "I can leave the branch (or want me to delete it).",
                "The framing is yours, but tell me where it drifts.",
                "So tell me which one.",
                # filler before it
                "Do steps 1-4, then tell me what it says.",
                "So please also tell me whether it appeared by itself.",
                "Just tell me which one.",
                "Now tell me what the screen shows.",
                "Do let me know if anything breaks.",
                "Feel free to let me know if it should change.",
                "If it's a different account: either tell me the handle or paste the URL.",
                "Otherwise just tell me.",
                "Next tell me which.",
                "Instead tell me which.",
                "Maybe tell me which.",
                "Perhaps tell me which.",
                "First tell me which.",
                # a symbol is no subject
                "3) Tell me whether to push.",
                "👉 Tell me which.",
                # a clause starts after these
                "The branch is merged. Tell me if it can go.",
                "Untick it anyway (the point is the toggle.) Tell me when you have.",
                "When it is done, tell me.",
                "The branch is merged; tell me if it can go.",
                "One thing: tell me which.",
                "It works! Tell me if it breaks.",
                "The branch is merged – tell me if it can go.",
                "The branch is merged - tell me if it can go.",
                "Your hotkey is still ⌥w — tell me and I will set it back.",
                "Happy to adjust—just let me know.",
                "Happy to adjust -- just let me know.",
                "Restart it → tell me what the row shows.",
                "It waits, and waits… tell me when to stop it.",
                "Ruff says 'ok.' Tell me which.",
                "- tell me whether to file the token-guidance issue.",
                "• tell me which one to keep.",
                "+ tell me which one to keep.",
                # and after a quote that ends a sentence
                'Ruff reports "All checks passed." Tell me if it can be pushed.',
                "The doctor now says “all good.” Let me know if the row clears.",
                'It printed "done!" Tell me if that is right.',
                'It said "wait…" Tell me when to go on.',
                'The reviewer asked "is it pending?" Tell me what to answer.',
                # the other phrases have no subject rule
                "Whether to land it is your call.",
                "Your call on whether to run it or stop here.",
                "Should I land it now"):
            with self.subTest(line=line):
                self.assertTrue(ccwho.asks_user(line))

    def test_any_phrase_that_asks_you_is_enough(self):           # control
        self.assertTrue(ccwho.asks_user("The logs tell me nothing, so tell me what you saw."))
        self.assertTrue(ccwho.asks_user("Tell me which; the logs tell me nothing."))


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

    # 2026-09-27: a task that cannot end needs a person whatever the feed says
    # between turns - `idle` or `waiting` at the prompt as much as `busy`
    def _as(self, status, tail, dead=None):
        return ccwho.build_row(dict(self.SESSION, status=status), [], tail, mtime=0,
                               now=1790267620.0, work=2,
                               dead_loops=[self.LOOP] if dead is None else dead)

    def test_idle_with_a_dead_wait_is_stuck(self):
        tail = [self._said("Started the runs in the background.")] + self.END
        self.assertEqual(self._as("idle", tail)["attention"], "stuck")
        self.assertEqual(self._as("idle", tail, dead=[])["attention"], "running")  # control

    def test_at_the_prompt_with_a_dead_wait_is_stuck(self):
        tail = [self._said("Started the runs in the background.")] + self.END
        self.assertEqual(self._as("waiting", tail)["attention"], "stuck")
        self.assertEqual(self._as("waiting", tail, dead=[])["attention"], "running")  # control

    def test_mid_turn_a_dead_reader_is_busy_and_not_offered(self):         # control
        # a foreground heredoc `cat`: the tool timeout ends it; the agent is at work
        reader = {"pid": 54329, "tasks": [], "kind": "reader", "program": "cat", "root": 1}
        row = self._as("busy", [self._said("Checking.")], dead=[reader])
        self.assertEqual(row["attention"], "busy")
        self.assertFalse(ccwho.loop_kill_offered(row))

    def test_a_question_outranks_a_dead_wait_in_every_state(self):          # control
        tail = [self._said("Want me to run the full gate?")] + self.END
        for status in ("idle", "waiting", "busy"):
            with self.subTest(status=status):
                self.assertEqual(self._as(status, tail)["attention"], "asks")


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
        self.assertEqual([t for t, r in second if r == "action"], [ccwho.UI_KILL])

    def test_the_button_says_it_opens_a_dialog_about_the_stuck_process(self):
        # the owner's words (design review 2026-09-29, decision 3): "…" - a box comes first
        self.assertEqual(ccwho.UI_KILL, "[kill stuck process…]")

    def test_the_count_of_more_survives_the_cut(self):
        # the wider button left no room for "(+1 more)": you saw one stuck item
        # of two until the box opened (review round 1, finding 1)
        row = dict(self._row(), dead_loops=[
            {"pid": 86246, "tasks": ["bscl8fc6k", "b2", "b3", "b4", "b5", "b6"]},
            {"pid": 1, "tasks": ["x"]}])
        for width in (60, 80, 100):
            with self.subTest(width=width):
                _, second = ccwho.ui_row_cells(row, width=width)
                text = "".join(t for t, _ in second)
                self.assertIn("(+1 more)", text)
                self.assertIn(ccwho.UI_KILL, text)
                self.assertEqual(sum(ccwho._cells(t) for t, _ in second), width)

    def test_one_stuck_item_says_no_count(self):                        # control
        _, second = ccwho.ui_row_cells(self._row(), width=80)
        self.assertNotIn("more)", "".join(t for t, _ in second))

    def test_the_kill_survives_a_narrow_window(self):
        _, second = ccwho.ui_row_cells(self._row(), width=40)
        self.assertEqual([t for t, r in second if r == "action"], [ccwho.UI_KILL])

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

    def test_what_was_said_does_not_cost_the_kill(self):
        # a stuck session found under SAID: what it said in place of the recap,
        # the loop and the kill where they are (review-plan1 F14)
        row = dict(self._row(), said={"text": "it hasn't woken the loop", "who": "you",
                                      "age": "2h"})
        _, second = ccwho.ui_row_cells(row, width=120)
        text = "".join(t for t, _ in second)
        self.assertIn("loop 86246", text)
        self.assertIn("it hasn't woken", text)
        self.assertNotIn("Goal was moving", text)
        col = 0
        for t, role in second:
            if role == "action":
                break
            col += ccwho._cells(t)
        self.assertEqual(ccwho.ui_action_at(row, 120, 1, col), "kill")


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
                self.assertEqual([t for t, r in second if r == "action"], [ccwho.UI_KILL])
                self.assertLess(roles.index("action"), roles.index("detail"))
                col, _ = self._col(second, "action")
                self.assertEqual(ccwho.ui_action_at(stuck, width, 1, col), "kill")

    # pad, the gap before the kill, the kill, one cell, the arrow's half
    KILL_FITS = 8 + 2 + ccwho._cells(ccwho.UI_KILL) + 1 + 3

    def test_the_kill_stays_whole_down_to_its_own_width(self):
        # the loop's words give way, down to nothing - never the kill
        stuck = dict(self.rows()[-1], dead_loops=[
            {"pid": 86246, "tasks": ["bscl8fc6k", "b2", "b3"]}, self.LOOP])
        for width in range(self.KILL_FITS, 41):
            with self.subTest(width=width):
                _, second = ccwho.ui_row_cells(stuck, width=width)
                self.assertIn((ccwho.UI_KILL, "action"), second)
                self.assertEqual(second[-1], (ccwho.UI_DETAIL[1], "detail"))
                self.assertEqual(sum(ccwho._cells(t) for t, _ in second), width)

    def test_below_that_there_is_no_room_for_it(self):                 # control
        stuck = self.rows()[-1]
        _, second = ccwho.ui_row_cells(stuck, width=self.KILL_FITS - 1)
        self.assertNotIn((ccwho.UI_KILL, "action"), second)

    def test_the_zones_are_where_a_tagged_row_is_drawn(self):
        for row in self.rows():
            for tag in ("", "ant:1a2b", "ant:lukaso@gmail"):
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


class TestTheRowSaysWhatIsStuck(unittest.TestCase):
    """The second line says what cannot end, before anything else: a loop on a
    finished task, or a reader waiting for input Claude Code never sends."""
    READER = {"pid": 54329, "kind": "reader", "program": "cat", "root": 86083}
    LOOP = {"pid": 86246, "tasks": ["bscl8fc6k"]}

    def second(self, dead, width=160):
        row = {"sessionId": "s", "attention": "stuck", "title": "t", "recap": "r",
               "dead_loops": dead}
        return "".join(t for t, _ in ccwho.ui_row_cells(row, width=width)[1])

    def test_a_reader_says_what_it_waits_for(self):
        self.assertIn("cat 54329 waits for input Claude Code never sends", self.second([self.READER]))

    def test_a_loop_still_says_its_task(self):                              # control
        self.assertIn("loop 86246 waits on bscl8fc6k, which has ended", self.second([self.LOOP]))

    def test_both_count_as_more(self):
        self.assertIn("(+1 more)", self.second([self.READER, self.LOOP]))


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

    def _report(self, ps, pids, matches=lambda argv, files: False):
        report = {}
        got = ccwho.kill_dead_loops(73787, pids, ps=lambda: ps, read=lambda path: self.ENDED,
                                    matches=matches, kill=lambda pid, sig: None,
                                    report=report)
        return got, report

    def test_a_loop_that_still_runs_but_did_not_look_stuck_is_named(self):
        # its line came since: it runs on, and "no loop is stuck" would be a guess
        got, report = self._report(f"73787 1 claude\n86246 73787 {self.LOOP}\n", [86246],
                                   matches=lambda argv, files: True)
        self.assertEqual(got, [])
        self.assertEqual(report.get("unconfirmed"), [86246])

    def test_a_killed_loop_is_not_unconfirmed(self):                   # control
        got, report = self._report(f"73787 1 claude\n86246 73787 {self.LOOP}\n", [86246])
        self.assertEqual(got, [86246])
        self.assertEqual(report.get("unconfirmed", []), [])

    def test_a_gone_or_reused_pid_is_not_unconfirmed(self):            # control
        _, report = self._report("73787 1 claude\n86246 73787 vim notes.md\n", [86246, 86247])
        self.assertEqual(report.get("unconfirmed", []), [])

    def test_another_sessions_process_on_that_pid_is_not_unconfirmed(self):
        # the pid went to a cat, or a loop, outside this session: "still runs"
        # would send you to try again for ever (review 1, finding 1)
        _, report = self._report("73787 1 claude\n86246 500 cat\n", [86246])
        self.assertEqual(report.get("unconfirmed"), [])
        _, report = self._report(f"73787 1 claude\n86246 555 {self.LOOP}\n", [86246],
                                 matches=lambda argv, files: True)
        self.assertEqual(report.get("unconfirmed"), [])

    def test_a_stuck_loop_that_could_not_be_signalled_is_named(self):
        report = {}

        def kill(pid, sig):
            raise PermissionError
        got = ccwho.kill_dead_loops(73787, [86246],
                                    ps=lambda: f"73787 1 claude\n86246 73787 {self.LOOP}\n",
                                    read=lambda path: self.ENDED, kill=kill,
                                    matches=lambda argv, files: False, report=report)
        self.assertEqual((got, report.get("failed")), ([], [86246]))

    def test_a_loop_that_is_gone_did_not_fail(self):                   # control
        report = {}

        def kill(pid, sig):
            raise ProcessLookupError
        ccwho.kill_dead_loops(73787, [86246],
                              ps=lambda: f"73787 1 claude\n86246 73787 {self.LOOP}\n",
                              read=lambda path: self.ENDED, kill=kill,
                              matches=lambda argv, files: False, report=report)
        self.assertEqual(report.get("failed"), [])

    def test_a_ps_that_said_nothing_is_unread(self):
        got, report = self._report("", [86246])
        self.assertEqual(got, [])
        self.assertTrue(report.get("unread"))

    def test_a_ps_that_answered_is_not_unread(self):                   # control
        _, report = self._report("73787 1 claude\n", [86246])
        self.assertFalse(report.get("unread"))

    def test_a_loop_already_gone_is_not_an_error(self):
        sent = []

        def kill(pid, sig):
            raise ProcessLookupError
        got = ccwho.kill_dead_loops(73787, [86246],
                                    ps=lambda: f"73787 1 claude\n86246 73787 {self.LOOP}\n",
                                    read=lambda path: self.ENDED, kill=kill,
                                    matches=lambda argv, files: False)
        self.assertEqual(got, [])


class TestFindDeadReaders(unittest.TestCase):
    """A `cat` with no file in a tool shell whose stdin is Claude Code's socket
    waits forever (2026-09-26: 20 hours, in a heredoc command moved to the
    background). Found in the session's own tree, proven by its fd 0."""
    # the tree measured on 2026-09-26, commands shortened
    START = "Sat Sep 26 15:12:13 2026"          # lstart, C locale, UTC
    NOW = 1790435533.0 + 3600                   # an hour later
    TOOL = "/bin/zsh -c source /Users/u/.claude/shell-snapshots/s.sh && eval 'x'"

    def ptable(self, reader="cat", ppid=54328, start=START):
        return {82755: (1, self.START, "claude --resume e78b"),
                86083: (82755, self.START, self.TOOL), 54328: (86083, self.START, self.TOOL),
                54329: (ppid, start, reader), 54330: (54328, self.START, "tr \\n ;"),
                54331: (54328, self.START, "cut -c1-300")}

    def unix(self, peer="0x4283419ab8f9493c", seen=None):
        def read(pids):
            if seen is not None:
                seen.append(sorted(pids))
            return {82755: {"16": ("0x4283419ab8f9493c", "->0xbbb423ce77188bdd")},
                    54329: {"0": ("0xbbb423ce77188bdd", "->" + peer)}}
        return read

    def find(self, ptable=None, unix=None, now=NOW, **kw):
        return ccwho.find_dead_readers(82755, ptable or self.ptable(), unix=unix or self.unix(),
                                       now=now, **kw)

    def test_the_20_hour_cat_is_found(self):
        # `tasks` too: a script reads every entry of dead_loops the same way;
        # `start`, so the kill knows it is still the cat you saw
        self.assertEqual(self.find(), [{"pid": 54329, "tasks": [], "kind": "reader",
                                        "program": "cat", "root": 86083,
                                        "start": self.START}])

    def test_under_an_mcp_server_it_is_not_dead(self):
        # an MCP server is the claude's child too, its stdin a claude socket
        # Claude Code DOES write to: a `cat` there carries the JSON-RPC
        t = self.ptable()
        t[86083] = (82755, self.START, "npm exec chrome-devtools-mcp@latest")
        self.assertEqual(self.find(t), [])

    def test_below_an_agent_it_is_not_this_sessions_reader(self):
        # a `claude -p` in the tool shell passes the socket on to its own work
        t = self.ptable()
        t[54340] = (54328, self.START, "claude -p hi")
        t[54329] = (54340, self.START, "cat")
        self.assertEqual(self.find(t), [])

    def test_under_any_other_child_of_the_claude_it_is_not_judged(self):   # control
        t = self.ptable()
        t[86083] = (82755, self.START, "/bin/sh -c cat | docker run -i srv")
        self.assertEqual(self.find(t), [])

    def test_lsof_failing_keeps_what_was_proven(self):
        # a new candidate and an lsof that fails: the cat proven before stays
        cache = {}
        self.find(cache=cache)
        t = self.ptable()
        t[54340] = (54328, self.START, "cat")
        self.assertEqual([d["pid"] for d in self.find(t, unix=lambda pids: None, cache=cache)],
                         [54329])

    def test_its_stdin_from_anyone_else_is_not_dead(self):                  # control
        self.assertEqual(self.find(unix=self.unix(peer="0x1")), [])

    def test_a_reader_younger_than_the_grace_is_not_judged(self):           # control
        self.assertEqual(self.find(now=1790435533.0 + 60), [])

    def test_an_unreadable_start_is_not_judged(self):                       # control
        self.assertEqual(self.find(self.ptable(start="")), [])

    def test_with_no_grace_its_age_is_not_asked(self):
        # the kill looks again on a ps with no start times: the list already
        # waited out the grace, and the proof is its fd 0
        self.assertEqual([d["pid"] for d in self.find(self.ptable(start=""), grace=0)], [54329])

    def test_a_file_to_read_is_not_a_dead_reader(self):                     # control
        # not by its name: since 2026-10-07 its stack says (TestFindAnyDeadReader)
        notes = lambda pids: {p: {"0": ("u", "unix", "->0x4283419ab8f9493c"),  # noqa: E731
                                  "3": ("r", "REG", "/Users/u/notes.md")} for p in pids}
        # not by its name, nor by its fds since review 3 (a read of a file never
        # waits for data): by its sample - reading notes.md uses CPU, and
        # stack_sample says no (TestTheRealStackAndFdPathsRun's busy file)
        self.assertEqual(self.find(self.ptable(reader="cat notes.md"), fds=notes,
                                   stack=lambda pid: False), [])

    def test_another_sessions_reader_is_not_this_ones(self):               # control
        t = self.ptable()
        t[54329] = (999, self.START, "cat")
        self.assertEqual(self.find(t), [])

    def test_lsof_is_asked_only_about_candidates(self):
        seen = []
        self.find(unix=self.unix(seen=seen))
        # tr and cut read stdin too - from the pipe, so they are not found
        self.assertEqual(seen, [[54329, 54330, 54331, 82755]])
        seen.clear()
        # since 2026-10-07 any program with no children is a candidate too
        # (TestFindAnyDeadReader) - once it has lived past the grace
        t = self.ptable(reader="sleep 100")
        t[54330] = t[54331] = (54328, self.START, "cargo test")
        self.find(t, unix=self.unix(seen=seen), now=1790435533.0 + 60)
        self.assertEqual(seen, [], "no candidate, no lsof")

    def test_the_real_lsof_path_runs(self):
        # every test above injects `unix`: this one takes the default, which
        # once was shadowed by another function of the same name (TypeError)
        t = {99999990: (1, self.START, "claude"), 99999991: (99999990, self.START, "cat")}
        self.assertEqual(ccwho.find_dead_readers(99999990, t, now=self.NOW), [])

    def test_lsof_unknown_finds_nothing(self):                             # control
        self.assertEqual(self.find(unix=lambda pids: None), [])

    def test_a_verdict_is_asked_once(self):
        # the fd 0 of a running cat does not change: not asked on every scan
        seen, cache = [], {}
        self.find(unix=self.unix(seen=seen), cache=cache)
        again = self.find(unix=self.unix(seen=seen), cache=cache)
        self.assertEqual((len(seen), again[0]["pid"]), (1, 54329))


class FakeKernel:
    """What `kill` does to a task, as the kernel does it. A process that got
    its TERM is gone, and its children go to launchd. A shell whose child dies
    while the shell still runs goes on to its next command: a new pid - the way
    a task outlives a leaves-first kill. `ps` answers from what is alive now."""

    def __init__(self, table, shells):
        self.table = dict(table)            # pid -> (ppid, start, cmd), alive
        self.shells = set(shells)
        self.sent, self.tried, self.forked, self.next = [], [], [], 70000

    def kill(self, pid, sig):
        self.tried.append(pid)              # every attempt, the failed ones too
        if pid not in self.table:
            raise ProcessLookupError
        self.sent.append((pid, sig))
        ppid = self.table.pop(pid)[0]
        for c, (pp, st, cmd) in list(self.table.items()):
            if pp == pid:
                self.table[c] = (1, st, cmd)
        if ppid in self.shells and ppid in self.table:
            self.next += 1
            self.table[self.next] = (ppid, "Sun Sep 27 12:00:00 2026", "rm -r x/")
            self.forked.append(self.next)

    def ps(self):
        return "".join(f"{p} {pp} {cmd}\n" for p, (pp, _s, cmd) in sorted(self.table.items()))

    def starts(self):
        return {p: (st, cmd.split()[0]) for p, (_pp, st, cmd) in self.table.items()}


class TestKillDeadReaders(unittest.TestCase):
    """`x` on a dead reader stops its whole task - the tool shell under the
    claude and all below it - as a person did by hand on 2026-09-27. Looked
    for again first: only a reader still proven dead, still the one listed."""
    TOOL = TestFindDeadReaders.TOOL
    START = TestFindDeadReaders.START

    def machine(self, extra=()):
        t = {82755: (1, self.START, "claude --resume e78b"),
             86083: (82755, self.START, self.TOOL), 54328: (86083, self.START, self.TOOL),
             54329: (54328, self.START, "cat"), 54330: (54328, self.START, "tr \\n ;"),
             54331: (54328, self.START, "cut -c1-300"),
             90000: (82755, self.START, self.TOOL), 90001: (90000, self.START, "cargo test")}
        t.update(dict(extra))
        return FakeKernel(t, shells={86083, 54328, 90000})

    def unix(self, peer="0xa"):
        return lambda pids: {82755: {"16": ("0xa", "->0xb")}, 54329: {"0": ("0xb", "->" + peer)}}

    def _kill(self, k=None, peer="0xa", own=None, pids=(54329,), started=None, report=None):
        k = k or self.machine()
        got = ccwho.kill_dead_loops(82755, list(pids), ps=k.ps, kill=k.kill, starts=k.starts,
                                    unix=self.unix(peer), own=own, report=report,
                                    started={54329: self.START} if started is None else started,
                                    read=lambda path: None, matches=lambda a, f: False)
        return got, k

    TASK = {86083, 54328, 54329, 54330, 54331}

    def test_the_whole_task_is_gone(self):
        got, k = self._kill()
        self.assertEqual(got, [54329])
        self.assertFalse(self.TASK & set(k.table), "nothing of the task still runs")

    def test_the_shell_never_goes_on_to_its_next_command(self):
        # leaves first, cat's death let zsh run the next line - a process the
        # kill never saw, left running under launchd (review 1, finding 2)
        _, k = self._kill()
        self.assertEqual(k.forked, [])
        self.assertTrue(all(sig == ccwho.signal.SIGTERM for _, sig in k.sent))

    def test_nothing_outside_the_task(self):                                # control
        _, k = self._kill()
        self.assertFalse({82755, 90000, 90001} & {p for p, _ in k.sent})

    def test_a_reader_no_longer_proven_is_left_alone(self):                 # control
        got, k = self._kill(peer="0x1")
        self.assertEqual((got, k.sent), ([], []))

    def test_only_what_was_asked(self):                                     # control
        got, k = self._kill(pids=(90001,))
        self.assertEqual((got, k.sent), ([], []))

    def test_a_reader_not_proven_now_is_unconfirmed(self):                # control
        report = {}
        self._kill(peer="0x1", report=report)
        self.assertEqual(report.get("unconfirmed"), [54329])

    def test_a_new_cat_on_that_pid_is_not_unconfirmed(self):
        # lsof no longer proves it, and it is not the cat you saw (review 1, finding 1)
        report = {}
        self._kill(peer="0x1", report=report, started={54329: "Sun Sep 27 09:00:00 2026"})
        self.assertEqual(report.get("unconfirmed"), [])

    def _two(self, pids, extra=()):
        """54329 proven and stopped with its task; the others asked for too."""
        k, report, calls = self.machine(extra), {}, []

        def starts():
            calls.append(1)
            return k.starts()
        got = ccwho.kill_dead_loops(82755, list(pids), ps=k.ps, kill=k.kill, starts=starts,
                                    unix=self.unix(), started={p: self.START for p in pids},
                                    read=lambda path: None, matches=lambda a, f: False,
                                    report=report)
        return got, report, calls

    def test_the_start_table_is_read_once_per_kill(self):
        # a cat in another task, not proven: its start is checked on the same table
        got, report, calls = self._two([54329, 90002], {90002: (90000, self.START, "cat")})
        self.assertEqual(got, [54329])
        self.assertEqual(report.get("unconfirmed"), [90002])
        self.assertEqual(len(calls), 1)

    def test_what_stopped_with_the_task_is_not_still_running(self):
        # 54330 (tr) went with 54329's task: it is not "still runs" - it was
        # signalled, and the kill says so (review round 1, finding 2)
        got, report, _ = self._two([54329, 54330])
        self.assertEqual(got, [54329, 54330])
        self.assertEqual(report.get("unconfirmed"), [])

    def test_a_listed_pid_not_in_the_task_is_not_said_killed(self):        # control
        got, _, _ = self._two([54329, 90002], {90002: (90000, self.START, "cat")})
        self.assertEqual(got, [54329])

    def test_an_empty_start_table_for_an_unproven_reader_is_unread(self):
        k, report = self.machine(), {}
        ccwho.kill_dead_loops(82755, [54329], ps=k.ps, kill=k.kill, starts=lambda: {},
                              unix=self.unix(peer="0x1"), started={54329: self.START},
                              read=lambda path: None, matches=lambda a, f: False,
                              report=report)
        self.assertTrue(report.get("unread"))

    def test_a_start_table_that_said_nothing_is_unread(self):
        # every reader skipped for want of its start is not "no reader is stuck"
        k, report = self.machine(), {}
        got = ccwho.kill_dead_loops(82755, [54329], ps=k.ps, kill=k.kill, starts=lambda: {},
                                    unix=self.unix(), started={54329: self.START},
                                    read=lambda path: None, matches=lambda a, f: False,
                                    report=report)
        self.assertEqual((got, report.get("unread")), ([], True))

    def test_a_reused_pid_is_left_alone(self):
        # the listed cat ended; a new cat got its pid: not the one you saw
        got, k = self._kill(started={54329: "Sun Sep 27 09:00:00 2026"})
        self.assertEqual((got, k.sent), ([], []))

    def test_ccwho_and_what_runs_it_are_never_signalled(self):
        # ccwho started inside that very task: its shells are not stopped under it
        _, k = self._kill(own=54331)
        self.assertFalse({54331, 54328, 86083} & {p for p, _ in k.sent})

    def test_an_agent_in_the_task_is_spared_with_its_tree(self):
        # a claude the task started is a session of its own - a hard guard;
        # the one under it is its business, not named again
        k = self.machine({54332: (54328, self.START, "claude -p hi"),
                          54333: (54332, self.START, "codex exec x"),
                          54334: (54333, self.START, "node x.js")})
        report = {}
        got, k = self._kill(k, report=report)
        self.assertEqual(got, [54329])
        self.assertFalse({54332, 54333, 54334} & {p for p, _ in k.sent})
        self.assertEqual(report.get("spared"), [54332])

    def test_a_reader_under_an_agent_is_not_reported_stopped(self):
        # `claude -p` in the task passes the socket on: its cat is spared, so
        # it must never be said to be stopped (round 2, M1)
        k = self.machine({54332: (54328, self.START, "claude -p hi"),
                          54335: (54332, self.START, "cat")})
        got = ccwho.kill_dead_loops(
            82755, [54335], ps=k.ps, kill=k.kill, starts=k.starts, started={54335: self.START},
            unix=lambda pids: {82755: {"16": ("0xa", "->0xb")}, 54335: {"0": ("0xb", "->0xa")}},
            read=lambda path: None, matches=lambda a, f: False)
        self.assertEqual(got, [])
        self.assertNotIn(54335, {p for p, _ in k.sent})

    def test_a_reader_that_ended_first_is_not_said_stopped(self):
        # it exited between the look and the signal: its task still goes, but
        # "stopped the task of cat" would be about a cat nobody stopped
        k = self.machine()
        real = k.kill

        def kill(pid, sig):
            if pid == 54329:
                k.table.pop(54329, None)
                raise ProcessLookupError
            real(pid, sig)
        got = ccwho.kill_dead_loops(82755, [54329], ps=k.ps, kill=kill, starts=k.starts,
                                    unix=self.unix(), started={54329: self.START},
                                    read=lambda path: None, matches=lambda a, f: False)
        self.assertEqual(got, [])
        self.assertIn(86083, {p for p, _ in k.sent})                        # control

    def test_two_readers_in_one_task_are_both_stopped_once(self):
        # `cat | cut` with both on the socket: one task, one pass (round 2, M2)
        k = self.machine({54336: (54328, self.START, "cut -c1-9")})
        report = {}
        got = ccwho.kill_dead_loops(
            82755, [54329, 54336], ps=k.ps, kill=k.kill, starts=k.starts, report=report,
            unix=lambda pids: {82755: {"16": ("0xa", "->0xb")},
                               54329: {"0": ("0xb", "->0xa")}, 54336: {"0": ("0xb", "->0xa")}},
            started={54329: self.START, 54336: self.START},
            read=lambda path: None, matches=lambda a, f: False)
        self.assertEqual(sorted(got), [54329, 54336])
        self.assertEqual(len(k.tried), len(set(k.tried)), "each pid tried once")
        self.assertEqual(report.get("spared", []), [])

    def test_in_one_task_only_the_listed_reader_is_said_stopped(self):
        # the cut's pid went to a new cut since the list: the task still goes
        # for the cat you saw, but the new cut is not "the one you stopped"
        k = self.machine({54336: (54328, "Sun Sep 27 09:00:00 2026", "cut -c1-9")})
        got = ccwho.kill_dead_loops(
            82755, [54329, 54336], ps=k.ps, kill=k.kill, starts=k.starts,
            unix=lambda pids: {82755: {"16": ("0xa", "->0xb")},
                               54329: {"0": ("0xb", "->0xa")}, 54336: {"0": ("0xb", "->0xa")}},
            started={54329: self.START, 54336: self.START},
            read=lambda path: None, matches=lambda a, f: False)
        self.assertEqual(got, [54329])
        self.assertTrue({54329, 54336, 86083} <= {p for p, _ in k.sent})   # control

    def test_a_loop_in_the_readers_task_lets_nothing_fork(self):
        # the loop killed first let its shell run the next line (round 3, #3)
        loop = ("until grep -q 'Test Files' /private/tmp/claude-501/p/s/tasks/b1.output; "
                "do sleep 5; done")
        k = self.machine({54337: (54328, self.START, loop)})
        got = ccwho.kill_dead_loops(
            82755, [54329, 54337], ps=k.ps, kill=k.kill, starts=k.starts, unix=self.unix(),
            started={54329: self.START}, read=lambda path: ("x\n[exited with code 0]\n", 600),
            matches=lambda a, f: False)
        self.assertEqual(sorted(got), [54329, 54337])
        self.assertEqual(k.forked, [])
        self.assertEqual(len(k.tried), len(set(k.tried)), "each pid tried once")

    def test_without_the_listed_starts_no_reader_is_signalled(self):
        # the start check fails closed: no caller may skip it by leaving it out
        k = self.machine()
        got = ccwho.kill_dead_loops(82755, [54329], ps=k.ps, kill=k.kill, starts=k.starts,
                                    unix=self.unix(), read=lambda path: None,
                                    matches=lambda a, f: False)
        self.assertEqual((got, k.sent), ([], []))
        self.assertEqual(self._kill(started={})[0], [])

    def test_under_an_mcp_server_nothing_is_signalled(self):
        # an MCP server's stdin is a claude socket too - a LIVE one (review 1, #1)
        k = self.machine()
        k.table[86083] = (82755, self.START, "npm exec some-mcp")
        got, k = self._kill(k)
        self.assertEqual((got, k.sent), ([], []))


class AnyReaderWorld:
    """The tree measured on 2026-10-07: a heredoc command ran `python3
    /tmp/label_chunk.py`, which read its stdin - Claude Code's socket - for 5.5
    hours. safe-chain, a wrapper, runs python3 as its child. Three fakes, each
    noting who it was asked about: lsof -U (fd 0's socket), lsof (every fd), and
    a sample of a stack (True: every thread in read; False: not; None: unknown)."""
    START = "Wed Oct  7 11:17:44 2026"          # lstart, C locale, UTC
    NOW = calendar.timegm(time.strptime(START, "%a %b %d %H:%M:%S %Y")) + 3600
    CLAUDE, SHELL, WRAP, PY = 56728, 99591, 99622, 99740
    TOOL = ("/bin/zsh -c source /Users/u/.claude/shell-snapshots/snapshot-zsh-1.sh 2>/dev/null"
            " || true && eval 'cat > /tmp/label_chunk.py << EOF'")
    PYCMD = "/Users/u/.pyenv/versions/3.13.5/bin/python3 /tmp/label_chunk.py"
    OUT = ("w", "REG", "/private/tmp/claude-501/x/tasks/bkdza8q8z.output")

    def ptable(self, cmd=PYCMD, start=START):
        return {self.CLAUDE: (1, self.START, "claude"),
                self.SHELL: (self.CLAUDE, self.START, self.TOOL),
                self.WRAP: (self.SHELL, self.START, "safe-chain python3 /tmp/label_chunk.py"),
                self.PY: (self.WRAP, start, cmd)}

    def unix(self, seen=None, peer="0x316f1225c8f0ee5a"):
        def read(pids):
            if seen is not None:
                seen.append(sorted(pids))
            out = {self.CLAUDE: {"25": ("0x316f1225c8f0ee5a", "->0xf32389e150b46276")}}
            for p in pids:
                if p != self.CLAUDE:
                    out[p] = {"0": ("0xf32389e150b46276", "->" + peer)}
            return out
        return read

    def fds(self, seen=None, extra=None):
        def read(pids):
            if seen is not None:
                seen.append(sorted(pids))
            return {p: dict({"cwd": (" ", "DIR", "/Users/u"),
                             "0": ("u", "unix", "->0x316f1225c8f0ee5a"),
                             "1": self.OUT, "2": self.OUT}, **(extra or {})) for p in pids}
        return read

    def stack(self, seen=None, says=True):
        def read(pid):
            if seen is not None:
                seen.append(pid)
            return says(pid) if callable(says) else says
        return read


class TestFindAnyDeadReader(AnyReaderWorld, unittest.TestCase):
    """Any program, not only the few named ones: proven by its fd 0 (Claude
    Code's socket), its other fds (none that a read could block on), and its
    stack (every thread in the kernel's read)."""

    def find(self, ptable=None, unix=None, fds=None, stack=None, now=None, **kw):
        return ccwho.find_dead_readers(self.CLAUDE, ptable or self.ptable(),
                                       unix=unix or self.unix(), fds=fds or self.fds(),
                                       stack=stack or self.stack(),
                                       now=self.NOW if now is None else now, **kw)

    def test_the_5_hour_python3_is_found(self):
        self.assertEqual(self.find(), [{"pid": self.PY, "tasks": [], "kind": "reader",
                                        "program": "python3", "root": self.SHELL,
                                        "start": self.START}])

    def test_only_a_process_with_no_children_is_sampled(self):
        # the tool shell and safe-chain hold the same socket, waiting on a child
        seen = []
        self.find(stack=self.stack(seen))
        self.assertEqual(seen, [self.PY])

    def test_asleep_is_not_stuck(self):                                     # control
        self.assertEqual(self.find(stack=self.stack(says=False)), [])

    def test_a_sample_that_failed_is_not_stuck(self):                       # control
        self.assertEqual(self.find(stack=self.stack(says=None)), [])

    def test_another_fd_that_could_block_is_not_sampled(self):              # control
        seen = []
        # a pipe as Apple's lsof prints it: no access mode (review 1)
        got = self.find(fds=self.fds(extra={"3": (" ", "PIPE", "->0x1")}), stack=self.stack(seen))
        self.assertEqual((got, seen), ([], []))

    def test_an_lsof_that_failed_is_not_sampled(self):                      # control
        seen = []
        got = self.find(fds=lambda pids: None, stack=self.stack(seen))
        self.assertEqual((got, seen), ([], []))

    def test_stdin_from_anyone_else_asks_nothing_more(self):                # control
        fds_seen, stack_seen = [], []
        got = self.find(unix=self.unix(peer="0x1"), fds=self.fds(fds_seen),
                        stack=self.stack(stack_seen))
        self.assertEqual((got, fds_seen, stack_seen), ([], [], []))

    def test_younger_than_the_grace_nothing_is_asked(self):                 # control
        u, f, s = [], [], []
        got = self.find(unix=self.unix(u), fds=self.fds(f), stack=self.stack(s),
                        now=self.NOW - 3600 + 60)
        self.assertEqual((got, u, f, s), ([], [], [], []))

    def test_below_an_agent_it_is_not_this_sessions_reader(self):           # control
        t = self.ptable()
        t[99700] = (self.SHELL, self.START, "claude -p hi")
        t[self.WRAP] = (99700, self.START, t[self.WRAP][2])
        self.assertEqual(self.find(t), [])

    def test_a_yes_is_not_asked_again_soon(self):
        f, s, cache = [], [], {}
        first = self.find(fds=self.fds(f), stack=self.stack(s), cache=cache)
        again = self.find(fds=self.fds(f), stack=self.stack(s), cache=cache, now=self.NOW + 60)
        self.assertEqual((first, again), (again, [dict(first[0])]))
        self.assertEqual((len(f), len(s)), (1, 1))

    def test_a_failed_sample_keeps_the_last_verdict(self):
        # "unknown, never no" (review 2): a sample too slow under load
        s, cache = [], {}
        self.find(stack=self.stack(s), cache=cache)
        later = self.find(stack=self.stack(s, says=None), cache=cache,
                          now=self.NOW + ccwho.STACK_RESAMPLE + 1)
        self.assertEqual(([d["pid"] for d in later], s), ([self.PY], [self.PY, self.PY]))

    def test_a_failed_lsof_keeps_the_last_verdict(self):
        # review 3: unknown, never no - in every place a verdict is written
        s, cache = [], {}
        self.find(stack=self.stack(s), cache=cache)
        later = self.find(fds=lambda pids: None, stack=self.stack(s), cache=cache,
                          now=self.NOW + ccwho.STACK_RESAMPLE + 1)
        self.assertEqual(([d["pid"] for d in later], s), ([self.PY], [self.PY]))

    def test_a_verdict_stamped_in_the_future_is_due(self):
        # a clock set back: the stamp is later than now (review 3)
        s, cache = [], {}
        self.find(stack=self.stack(s), cache=cache, now=self.NOW + 3600)
        self.find(stack=self.stack(s), cache=cache, now=self.NOW + ccwho.STACK_RESAMPLE + 1)
        self.assertEqual(s, [self.PY, self.PY])

    def test_stdin_on_another_socket_after_its_proof_is_not_judged(self):
        # dup2 of a socket to a live server: a unix socket still, not the
        # claude's - fd 0 is proven again before each new sample (review 4)
        s, cache = [], {}
        self.find(cache=cache)
        later = self.find(unix=self.unix(peer="0x1"), stack=self.stack(s), cache=cache,
                          now=self.NOW + ccwho.STACK_RESAMPLE + 1)
        self.assertEqual((later, s), ([], []))
        again = self.find(stack=self.stack(s), cache={}, now=self.NOW)          # control
        self.assertEqual(([d["pid"] for d in again], s), ([self.PY], [self.PY]))

    def test_a_failed_proof_keeps_the_last_verdict(self):
        # fd 0 asked again before a sample, and lsof could not say: unknown
        s, cache = [], {}
        self.find(stack=self.stack(s), cache=cache)
        later = self.find(unix=lambda pids: None, stack=self.stack(s), cache=cache,
                          now=self.NOW + ccwho.STACK_RESAMPLE + 1)
        self.assertEqual(([d["pid"] for d in later], s), ([self.PY], [self.PY]))

    def test_a_proof_made_in_this_scan_is_not_asked_again(self):
        u = []
        self.find(unix=self.unix(u))
        self.assertEqual(len(u), 1)

    def test_an_unreaped_child_does_not_hide_a_reader(self):
        # a Popen never waited for, then sys.stdin.read() (review 4)
        # macOS 27's ps prints a child left unreaped as "<defunct>" (review 5);
        # one that has only just exited as "(sleep)" (measured 2026-09-29)
        t = self.ptable()
        for zombie in ("<defunct>", "(sleep)"):
            with self.subTest(zombie=zombie):
                t[99800] = (self.PY, self.START, zombie)
                self.assertEqual([d["pid"] for d in self.find(t)], [self.PY])
        t[99800] = (self.PY, self.START, "sleep 100")                         # control
        s = []
        found = [d["pid"] for d in self.find(t, stack=self.stack(s))]
        # a live child: the python waits on it, so it is neither sampled nor found
        self.assertEqual((self.PY in found, self.PY in s), (False, False))

    def test_stdin_moved_after_its_proof_is_not_judged(self):
        # fd 0 proven the claude's socket once; then dup2 or `exec 0<fifo`
        cache = {}
        self.find(cache=cache)
        moved = self.fds(extra={"0": ("r", "FIFO", "/tmp/fifo")})
        self.assertEqual(self.find(fds=moved, cache=cache, now=self.NOW + ccwho.STACK_RESAMPLE + 1),
                         [])

    def test_no_sample_left_is_no_proof_asked_either(self):
        # the first proof of fd 0 is a sample's first step: a scan with nothing
        # left to sample (save, open, ps: samples=0) asks lsof nothing for a
        # program it cannot judge, and has nothing new to keep (review 5)
        u, cache = [], {"_sample_budget": 0}
        self.assertEqual((self.find(unix=self.unix(u), cache=cache), u), ([], []))
        self.assertNotIn("_verdicts_new", cache)
        named = {"_sample_budget": 0}                                          # control
        got = self.find(self.ptable(cmd="cat"), unix=self.unix(u), cache=named)
        self.assertEqual(([d["program"] for d in got], len(u)), (["cat"], 1))
        # and one proven before is still listed: from what the cache knows
        proven = {}
        self.find(cache=proven)
        proven["_sample_budget"] = 0
        self.assertEqual([d["pid"] for d in self.find(unix=lambda pids: self.fail("lsof"),
                                                      stack=lambda pid: self.fail("sampled"),
                                                      cache=proven, now=self.NOW + 60)], [self.PY])

    def test_an_empty_budget_asks_nothing(self):
        # one proven before and due again: no sample left, so no lsof either
        cache = {}
        self.find(cache=cache)
        u, f, s = [], [], []
        later = self.NOW + ccwho.STACK_RESAMPLE + 1
        cache["_sample_budget"] = 0
        self.find(unix=self.unix(u), fds=self.fds(f), stack=self.stack(s), cache=cache, now=later)
        self.assertEqual((u, f, s), ([], [], []))
        cache["_sample_budget"] = 1                                            # control
        self.find(unix=self.unix(u), fds=self.fds(f), stack=self.stack(s), cache=cache, now=later)
        self.assertEqual((len(u), len(f), len(s)), (1, 1, 1))

    def test_a_reader_that_stops_reading_leaves_within_a_while(self):
        # some reads do return - `read -t`, an alarm before input() (review 1)
        s, cache = [], {}
        self.find(stack=self.stack(s), cache=cache)
        later = self.find(stack=self.stack(s, says=False), cache=cache,
                          now=self.NOW + ccwho.STACK_RESAMPLE + 1)
        self.assertEqual((later, s), ([], [self.PY, self.PY]))

    def test_a_no_is_asked_again_only_after_a_while(self):
        s, cache = [], {}
        self.find(stack=self.stack(s, says=False), cache=cache)
        soon = self.find(stack=self.stack(s), cache=cache, now=self.NOW + 60)
        later = self.find(stack=self.stack(s), cache=cache,
                          now=self.NOW + ccwho.STACK_RESAMPLE + 1)
        self.assertEqual((soon, [d["pid"] for d in later], s), ([], [self.PY], [self.PY, self.PY]))

    def test_an_unknown_is_asked_again_only_after_a_while(self):
        # a sample that failed is not retried on every refresh either
        s, cache = [], {}
        self.find(stack=self.stack(s, says=None), cache=cache)
        self.find(stack=self.stack(s), cache=cache, now=self.NOW + 60)
        self.assertEqual(s, [self.PY])

    def test_at_most_a_few_samples_a_scan(self):
        t = self.ptable()
        more = [99801, 99802, 99803]
        for p in more:
            t[p] = (self.SHELL, self.START, f"python3 job{p}.py")
        s, cache, found = [], {}, set()
        for _ in range(3):
            n = len(s)
            found |= {d["pid"] for d in self.find(t, stack=self.stack(s), cache=cache)}
            self.assertLessEqual(len(s) - n, ccwho.STACK_PER_SCAN)
        self.assertEqual(found, {self.PY, *more})

    def test_a_named_reader_needs_no_sample(self):
        f, s = [], []
        got = self.find(self.ptable(cmd="cat"), fds=self.fds(f), stack=self.stack(s))
        self.assertEqual(([d["program"] for d in got], f, s), (["cat"], [], []))

    def test_only_the_asked_pids_are_sampled(self):
        t = self.ptable()
        t[99801] = (self.SHELL, self.START, "python3 other.py")
        s = []
        got = self.find(t, stack=self.stack(s), only={self.PY})
        self.assertEqual(([d["pid"] for d in got], s), ([self.PY], [self.PY]))

    def test_a_program_name_is_never_free_text(self):
        # it goes to a terminal, and to agents reading --json
        for cmd, said in (("/opt/x/node-20 job.js", "node-20"),
                          ("/tmp/\x1b]0;evil\x07 job", "a program"),
                          ("/tmp/" + "n" * 40, "a program")):
            with self.subTest(cmd=cmd):
                got = self.find(self.ptable(cmd=cmd))
                self.assertEqual([d["program"] for d in got], [said])

    def test_the_name_comes_from_the_executable(self):
        # ps -o comm, spaces and all: the command's first word would be "Application"
        app = "/Users/u/Library/Application Support/x/bin/python3"
        got = self.find(self.ptable(cmd=app + " job.py"), names={self.PY: app})
        self.assertEqual([d["program"] for d in got], ["python3"])


class TestReaderVerdictsAreShared(AnyReaderWorld, unittest.TestCase):
    """ccwho ls starts with an empty cache every time, and a sample takes about
    1.5 s: what one ccwho process proved, the next one reads from a file. Kept
    by a hash of (claude, pid, start, command): no command text on disk."""

    def setUp(self):
        folder = tempfile.mkdtemp(prefix="ccwho-test-v-")
        self.addCleanup(shutil.rmtree, folder, True)
        self.path = os.path.join(folder, "readers.json")

    def find(self, cache, ptable=None, now=None, **kw):
        for name, fake in (("unix", self.unix()), ("fds", self.fds()), ("stack", self.stack())):
            kw.setdefault(name, fake)
        return ccwho.find_dead_readers(self.CLAUDE, ptable or self.ptable(), cache=cache,
                                       now=self.NOW if now is None else now, **kw)

    def handed_on(self, cache):
        """What the next ccwho process starts from: the file this one wrote."""
        ccwho.save_verdicts(cache, path=self.path)
        later = {}
        ccwho.load_verdicts(later, path=self.path)
        return later

    def nothing(self, *a):
        self.fail("asked again")

    def test_a_yes_is_not_sampled_again_by_the_next_ccwho(self):
        first = {}
        self.find(first)
        got = self.find(self.handed_on(first), unix=self.nothing, fds=self.nothing,
                        stack=self.nothing)
        self.assertEqual([d["pid"] for d in got], [self.PY])

    def test_a_no_is_asked_again_after_a_while_by_the_next_ccwho(self):
        first, s = {}, []
        self.find(first, stack=self.stack(says=False))
        later = self.handed_on(first)
        self.find(later, stack=self.stack(s), now=self.NOW + 60)
        got = self.find(later, stack=self.stack(s), now=self.NOW + ccwho.STACK_RESAMPLE + 1)
        self.assertEqual((s, [d["pid"] for d in got]), ([self.PY], [self.PY]))

    def test_no_command_text_reaches_the_file(self):
        first = {}
        self.find(first)
        ccwho.save_verdicts(first, path=self.path)
        with open(self.path) as fh:
            text = fh.read()
        for word in ("label_chunk", "python3", str(self.PY), "safe-chain"):
            self.assertNotIn(word, text)

    def test_the_file_is_the_users_alone(self):
        ccwho.save_verdicts({"_fd0": {"k": [True, 1.0]}, "_stacks": {}}, path=self.path)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_an_unreadable_file_holds_no_verdict(self):
        for junk in ("not json", "[]", '{"fd0": [], "stacks": 3}',
                     '{"fd0": {"k": true}, "stacks": {"k": [true]}}',
                     '{"fd0": {"k": [true, "x"]}, "stacks": {"k": ["yes", 1]}}'):
            with self.subTest(junk=junk):
                with open(self.path, "w") as fh:
                    fh.write(junk)
                cache = {}
                ccwho.load_verdicts(cache, path=self.path)
                self.assertEqual((cache.get("_fd0"), cache.get("_stacks")), ({}, {}))

    def test_no_file_holds_no_verdict(self):
        cache = {}
        ccwho.load_verdicts(cache, path=self.path)
        self.assertEqual((cache.get("_fd0"), cache.get("_stacks")), ({}, {}))

    def test_a_file_that_cannot_be_written_is_no_error(self):
        blocked = os.path.join(self.path, "under-a-file", "readers.json")
        with open(self.path, "w") as fh:
            fh.write("{}")
        ccwho.save_verdicts({"_fd0": {"k": [True, 1.0]}, "_stacks": {}}, path=blocked)

    def test_two_ccwho_processes_keep_both_verdicts(self):
        t = self.ptable()
        t[99801] = (self.SHELL, self.START, "python3 other.py")
        a, b = {}, {}
        self.find(a)
        self.find(b, ptable=t, only={99801})
        ccwho.save_verdicts(a, path=self.path)
        ccwho.save_verdicts(b, path=self.path)
        later = {}
        ccwho.load_verdicts(later, path=self.path)
        got = self.find(later, ptable=t, unix=self.nothing, fds=self.nothing, stack=self.nothing)
        self.assertEqual([d["pid"] for d in got], [self.PY, 99801])

    def test_the_newer_verdict_of_a_process_wins(self):
        # another ccwho wrote since this one read: each process's newer verdict
        ccwho.save_verdicts({"_fd0": {}, "_stacks": {"a": [False, 100.0], "b": [True, 300.0]}},
                            path=self.path)
        ccwho.save_verdicts({"_fd0": {}, "_stacks": {"a": [True, 200.0], "b": [False, 200.0]}},
                            path=self.path)
        later = {}
        ccwho.load_verdicts(later, path=self.path)
        self.assertEqual(later["_stacks"], {"a": [True, 200.0], "b": [True, 300.0]})

    def test_a_new_process_on_that_pid_is_asked_again(self):
        # a pid is reused: its verdict belongs to the process that had it
        first, s = {}, []
        self.find(first, stack=self.stack(s))
        later = self.handed_on(first)
        self.find(later, ptable=self.ptable(start="Wed Oct  7 12:00:00 2026"), stack=self.stack(s))
        self.assertEqual(s, [self.PY, self.PY])

    def test_what_a_scan_learns_is_marked_for_the_file(self):
        # a socket proof alone, and later a sample alone: each is new to keep
        named = {}
        self.find(named, ptable=self.ptable(cmd="cat"))
        self.assertTrue(named.pop("_verdicts_new", False), "a socket proof")
        cache = {}
        self.find(cache, stack=self.stack(says=False))
        cache.pop("_verdicts_new")
        self.find(cache, now=self.NOW + ccwho.STACK_RESAMPLE + 1)
        self.assertTrue(cache.get("_verdicts_new"), "a sample, its socket known")

    def test_nothing_learned_is_nothing_to_keep(self):                      # control
        cache = {}
        self.find(cache)
        cache.pop("_verdicts_new")
        self.find(cache)
        self.assertNotIn("_verdicts_new", cache)

    def test_a_stamp_from_the_future_is_not_loaded(self):
        # it would come back through the cache that loaded it (review 4)
        with open(self.path, "w") as fh:
            json.dump({"fd0": {}, "stacks": {"k": [True, self.NOW + 3600], "j": [True, self.NOW]}}, fh)
        cache = {}
        ccwho.load_verdicts(cache, path=self.path, now=self.NOW)
        self.assertEqual(cache["_stacks"], {"j": [True, self.NOW]})

    def test_a_stamp_from_the_future_in_a_cache_is_not_written(self):
        ccwho.save_verdicts({"_fd0": {}, "_stacks": {"k": [True, self.NOW + 3600]}}, path=self.path,
                            now=self.NOW)
        later = {}
        ccwho.load_verdicts(later, path=self.path, now=self.NOW + 7200)
        self.assertEqual(later["_stacks"], {})

    def test_a_stamp_from_the_future_does_not_hold_the_file(self):
        # written while the clock ran ahead: it must not win every merge after
        ccwho.save_verdicts({"_fd0": {}, "_stacks": {"k": [True, self.NOW + 3600]}}, path=self.path,
                            now=self.NOW + 3600)
        ccwho.save_verdicts({"_fd0": {}, "_stacks": {"k": [False, self.NOW]}}, path=self.path,
                            now=self.NOW)
        later = {}
        ccwho.load_verdicts(later, path=self.path)
        self.assertEqual(later["_stacks"], {"k": [False, self.NOW]})

    def test_the_newest_verdicts_are_kept(self):
        many = {f"k{i}": [True, float(i)] for i in range(ccwho.VERDICTS_KEEP + 50)}
        ccwho.save_verdicts({"_fd0": many, "_stacks": {}}, path=self.path)
        later = {}
        ccwho.load_verdicts(later, path=self.path)
        self.assertEqual(len(later["_fd0"]), ccwho.VERDICTS_KEEP)
        self.assertIn(f"k{ccwho.VERDICTS_KEEP + 49}", later["_fd0"])
        self.assertNotIn("k0", later["_fd0"])

    def test_a_scan_keeps_its_memory_bounded(self):
        old = {f"k{i}": [False, 0.0] for i in range(ccwho.VERDICTS_KEEP + 10)}
        cache = {"_fd0": dict(old), "_stacks": dict(old)}
        got = self.find(cache)
        self.assertEqual([d["pid"] for d in got], [self.PY])
        self.assertLessEqual(max(len(cache["_fd0"]), len(cache["_stacks"])), ccwho.VERDICTS_KEEP)

    def test_the_file_lives_in_ccwhos_cache_folder(self):
        self.assertEqual(REAL_VERDICTS_PATH(),
                         os.path.expanduser("~/.cache/ccwho/readers.json"))


class RealChildren:
    """This test's own children, for the tests that run the real lsof and sample."""

    def children(self, codes, pass_fds=None):
        """This test's own children, each with a socket for its stdin that no
        one writes to - as Claude Code's is in a heredoc command. `pass_fds`:
        name -> the fds that child alone keeps."""
        import socket
        kids = {}
        for name, argv in codes.items():
            mine, theirs = socket.socketpair()
            kids[name] = subprocess.Popen(argv, stdin=theirs,
                                          pass_fds=(pass_fds or {}).get(name, ()))
            theirs.close()
            self.addCleanup(mine.close)
            self.addCleanup(kids[name].wait)
            self.addCleanup(kids[name].kill)
        time.sleep(0.5)                     # each at its read, its loop or its sleep
        return kids

    def py(self, code):
        return [sys.executable, "-c", code]


class TestTheRealStackAndFdPathsRun(RealChildren, unittest.TestCase):
    """Every test above injects lsof and sample: these take the defaults, on a
    pid no process has, so nothing real is sampled or listed."""

    def test_sampling_a_pid_that_does_not_run_is_unknown(self):
        self.assertIsNone(REAL["stack_sample"](99999991))

    def test_listing_a_pid_that_does_not_run_finds_nothing(self):
        self.assertEqual(REAL["open_fds"]([99999991]), {})

    def test_a_real_unreaped_child_is_a_zombie(self):
        # what ps prints for one is what procs._ZOMBIE must match (review 5: the
        # test of round 4 used a form this Mac does not print)
        zombie = subprocess.Popen(["/usr/bin/true"])          # exits; not waited for
        live = subprocess.Popen(["/bin/sleep", "20"])
        self.addCleanup(zombie.wait)
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        time.sleep(0.5)
        cmds = {int(p): c for p, _pp, c in ccwho.procs.ps_rows(ccwho.ps_snapshot()) if p.isdigit()}
        self.assertTrue(ccwho.procs._ZOMBIE.match(cmds[zombie.pid]), cmds.get(zombie.pid))
        self.assertFalse(ccwho.procs._ZOMBIE.match(cmds[live.pid]), cmds.get(live.pid))  # control

    def test_a_real_reader_is_seen_reading(self):
        # this test's own children, each with a socket for its stdin: one reads
        # it, one sleeps. The real sample and lsof, their flags and their text
        import socket
        kids = []
        for code in ("import sys; sys.stdin.read()", "import time; time.sleep(20)"):
            mine, theirs = socket.socketpair()
            kids.append(subprocess.Popen([sys.executable, "-c", code], stdin=theirs))
            theirs.close()
            self.addCleanup(mine.close)
        for k in kids:
            self.addCleanup(k.wait)
            self.addCleanup(k.kill)
        time.sleep(0.5)                     # each at its read or its sleep
        reader, sleeper = (k.pid for k in kids)
        fds = REAL["open_fds"]([reader, sleeper])
        self.assertTrue(ccwho.procs.only_stdin_can_block(fds, reader), fds)
        self.assertEqual((REAL["stack_sample"](reader), REAL["stack_sample"](sleeper)),
                         (True, False))

    def test_the_report_comes_on_stdout(self):
        # without -file, sample also leaves its report in /tmp, every time
        asked = []

        def run(argv, **kw):
            asked.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        testkit.patch(self, ccwho.subprocess, "run", run)
        testkit.patch(self, ccwho, "_cpu_seconds", lambda pid: 1.0)
        folder = tempfile.mkdtemp(prefix="ccwho-test-path-")
        self.addCleanup(shutil.rmtree, folder, True)
        fake = os.path.join(folder, "sample")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        os.chmod(fake, 0o755)
        testkit.patch(self, os, "environ", dict(os.environ, PATH=folder + os.pathsep +
                                                os.environ.get("PATH", "")))
        REAL["stack_sample"](12345)
        # Apple's own, never the first `sample` on PATH (review 3): its report is
        # the one stack_reads reads
        self.assertEqual(asked[0], ["/usr/bin/sample", "12345", "1", "10", "-mayDie",
                                    "-file", "/dev/stdout"])

    def test_a_process_using_cpu_while_sampled_is_not_stuck(self):
        # every thread in read at each look, and CPU spent between: a busy loop
        reads = ("Analysis of sampling x (pid 4242) every 10 milliseconds\nCall graph:\n"
                 "    90 Thread_1   DispatchQueue_1: com.apple.main-thread  (serial)\n\n"
                 "Sort by top of stack, same collapsed (when >= 5):\n"
                 "        read  (in libsystem_kernel.dylib)        90\n\n")
        testkit.patch(self, ccwho.subprocess, "run", lambda argv, **kw:
                      subprocess.CompletedProcess(argv, 0, stdout=reads, stderr=""))
        for cpu, said in (((1.0, 1.0), True), ((1.0, 1.8), False), ((None, 1.0), None),
                          ((1.0, None), None)):
            with self.subTest(cpu=cpu):
                times = list(cpu)
                testkit.patch(self, ccwho, "_cpu_seconds", lambda pid: times.pop(0))
                self.assertEqual(REAL["stack_sample"](4242), said)

    def test_a_real_shell_waiting_in_read_is_stuck(self):
        # the tool shell's own `read` (zsh holds /dev/null on fd 10), and a bash
        # script's (bash holds the script on fd 255): review 3
        folder = tempfile.mkdtemp(prefix="ccwho-test-readers-")
        self.addCleanup(shutil.rmtree, folder, True)
        script = os.path.join(folder, "ask.sh")
        with open(script, "w") as fh:
            fh.write("read -r answer\n")
        kids = self.children({"zsh": ["/bin/zsh", "-c", "read -r line"],
                              "bash": ["/bin/bash", script]})
        fds = REAL["open_fds"](sorted(k.pid for k in kids.values()))
        self.assertEqual({n: ccwho.procs.only_stdin_can_block(fds, k.pid) for n, k in kids.items()},
                         {"zsh": True, "bash": True}, fds)
        self.assertEqual({n: REAL["stack_sample"](k.pid) for n, k in kids.items()},
                         {"zsh": True, "bash": True})


class TestTheRealBusyReadersAreNotStuck(RealChildren, unittest.TestCase):
    """A read that can return - of a pipe, or one that loops on a file or a
    device - on this test's own children, with the real lsof and sample. A
    class of its own: each sample takes about 1.5 s, and a class runs under
    a 30 s kill."""

    def test_a_real_read_of_a_pipe_is_not_judged(self):
        # in the kernel's read on every sample, as a stdin reader is (review 1):
        # only its fds tell them apart
        r, w = os.pipe()
        self.addCleanup(os.close, w)
        kids = self.children({"pipe": self.py(f"import os; os.read({r}, 1)"),
                              "stdin": self.py("import sys; sys.stdin.read()")},
                             pass_fds={"pipe": (r,)})
        os.close(r)
        fds = REAL["open_fds"](sorted(k.pid for k in kids.values()))
        said = {n: ccwho.procs.only_stdin_can_block(fds, k.pid) for n, k in kids.items()}
        self.assertEqual(said, {"pipe": False, "stdin": True}, fds)

    def test_a_real_busy_read_of_a_device_is_not_stuck(self):
        # /dev/zero, and /dev/null read in a loop: their fds cannot wait for
        # data, and the CPU each uses through the sample says it is working
        # 16 MB a read: nearly every look finds the kernel's read, as a C
        # program copying /dev/urandom does - only the CPU tells it from a wait
        kids = self.children({
            "zero": self.py("import os\nfd = os.open('/dev/zero', os.O_RDONLY)\n"
                            "while True: os.read(fd, 1 << 24)"),
            "null": self.py("f = open('/dev/null', 'rb')\nwhile True: f.read(1)")})
        fds = REAL["open_fds"](sorted(k.pid for k in kids.values()))
        self.assertTrue(all(ccwho.procs.only_stdin_can_block(fds, k.pid) for k in kids.values()), fds)
        self.assertEqual({n: REAL["stack_sample"](k.pid) for n, k in kids.items()},
                         {"zero": False, "null": False})

    def test_a_real_busy_read_of_a_file_is_not_stuck(self):
        folder = tempfile.mkdtemp(prefix="ccwho-test-readers-")
        self.addCleanup(shutil.rmtree, folder, True)
        data = os.path.join(folder, "data")
        with open(data, "wb") as fh:
            fh.write(b"x" * (1 << 25))
        # 32 MB a read, from the page cache: nearly every look finds the read
        kids = self.children({"file": self.py(
            f"import os\nfd = os.open({data!r}, os.O_RDONLY)\n"
            "while True:\n os.lseek(fd, 0, 0); os.read(fd, 1 << 25)")})
        fds = REAL["open_fds"]([kids["file"].pid])
        self.assertTrue(ccwho.procs.only_stdin_can_block(fds, kids["file"].pid), fds)
        self.assertIs(REAL["stack_sample"](kids["file"].pid), False)


class TestKillAnyDeadReader(AnyReaderWorld, unittest.TestCase):
    """`x` on any stuck reader stops its task, as for a cat - looked for again
    first, with a new sample of only the pids asked for."""

    def _kill(self, says=True, pids=None, extra=(), report=None, seen=None):
        t = self.ptable()
        t.update(dict(extra))
        k = FakeKernel(t, shells={self.SHELL})
        got = ccwho.kill_dead_loops(self.CLAUDE, list(pids or [self.PY]), ps=k.ps, kill=k.kill,
                                    starts=k.starts, unix=self.unix(), fds=self.fds(),
                                    stack=self.stack(seen, says=says),
                                    started={self.PY: self.START}, report=report,
                                    read=lambda path: None, matches=lambda a, f: False)
        return got, k

    def test_its_whole_task_is_stopped_parents_first(self):
        got, k = self._kill()
        self.assertEqual(got, [self.PY])
        self.assertEqual([p for p, _ in k.sent], [self.SHELL, self.WRAP, self.PY])

    def test_no_longer_reading_it_is_left_alone_and_said_so(self):          # control
        report = {}
        got, k = self._kill(says=False, report=report)
        self.assertEqual((got, k.sent, report.get("unconfirmed")), ([], [], [self.PY]))

    def test_only_the_asked_pid_is_sampled(self):
        seen = []
        self._kill(extra={99801: (self.SHELL, self.START, "python3 other.py")}, seen=seen)
        self.assertEqual(seen, [self.PY])

    def listed_then_killed(self, says=True, fds=None, seen=None):
        """The list's cache after a scan found PY; what a kill then learned."""
        listed, learned, report = {}, {}, {}
        ccwho.find_dead_readers(self.CLAUDE, self.ptable(), unix=self.unix(), fds=self.fds(),
                                stack=self.stack(), now=self.NOW, cache=listed)
        k = FakeKernel(self.ptable(), shells={self.SHELL})
        ccwho.kill_dead_loops(self.CLAUDE, [self.PY], ps=k.ps, kill=k.kill, starts=k.starts,
                              unix=self.unix(), fds=fds or self.fds(),
                              stack=self.stack(seen, says=says), started={self.PY: self.START},
                              report=report, cache=learned, now=self.NOW + 30,
                              read=lambda path: None, matches=lambda a, f: False)
        return listed, learned, report

    def later(self, listed):
        return ccwho.find_dead_readers(self.CLAUDE, self.ptable(), unix=self.unix(),
                                       fds=self.fds(), stack=lambda pid: self.fail("sampled"),
                                       now=self.NOW + 60, cache=listed)

    def test_what_the_kill_saw_reaches_the_lists_cache(self):
        # the kill sampled again and saw no read: the list must not go on
        # offering the kill for five minutes on its older yes (review 2). It is
        # handed over, not written into the cache a scan may be walking (review 3)
        seen = []
        listed, learned, report = self.listed_then_killed(says=False, seen=seen)
        self.assertEqual((seen, report.get("unconfirmed")), ([self.PY], [self.PY]),
                         "the kill looks again, whatever the list's cache says")
        self.assertTrue(learned.get("_verdicts_new"), "and it is to be kept")
        ccwho.adopt_verdicts(listed, learned)
        self.assertTrue(listed.get("_verdicts_new"))
        self.assertEqual(self.later(listed), [])

    def test_an_older_verdict_never_overwrites_a_newer_one(self):
        # a scan sampled again after the kill did (review 4)
        cache = {"_stacks": {"k": [True, 200.0]}}
        ccwho.adopt_verdicts(cache, {"_stacks": {"k": [False, 100.0]}})
        self.assertEqual(cache["_stacks"]["k"], [True, 200.0])
        ccwho.adopt_verdicts(cache, {"_stacks": {"k": [False, 300.0]}})        # control
        self.assertEqual(cache["_stacks"]["k"], [False, 300.0])

    def test_an_unknown_from_the_kill_leaves_the_lists_yes(self):
        # a sample or an lsof that fails at the kill is no "not reading" (review 3)
        for case in ({"says": None}, {"fds": lambda pids: None}):
            with self.subTest(case=list(case)):
                listed, learned, _ = self.listed_then_killed(**case)
                ccwho.adopt_verdicts(listed, learned)
                self.assertEqual([d["pid"] for d in self.later(listed)], [self.PY])

    def test_the_kills_no_reaches_the_file(self):
        folder = tempfile.mkdtemp(prefix="ccwho-test-v-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "readers.json")
        listed, learned, _ = self.listed_then_killed(says=False)
        ccwho.adopt_verdicts(listed, learned)
        ccwho.save_verdicts(listed, path=path)
        on_disk = {}
        ccwho.load_verdicts(on_disk, path=path)
        key = ccwho._verdict_key(self.CLAUDE, self.PY, self.START, self.PYCMD)
        self.assertIs(on_disk["_stacks"][key][0], False)

    def test_every_reader_asked_about_is_sampled(self):
        # a scan takes a few; a kill looks at each one it was asked about
        more = {p: (self.SHELL, self.START, f"python3 job{p}.py") for p in (99801, 99802)}
        seen = []
        self._kill(pids=[self.PY, *more], extra=more, seen=seen)
        self.assertEqual(sorted(seen), [self.PY, *more])


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

    def test_a_sleep_that_just_ended_is_still_waiting(self):
        # macOS ps prints a child that has exited, not yet reaped, as "(sleep)":
        # a kill that looked in that moment said "no loop there is stuck any
        # more" about a loop that was (2026-09-29, 2 of 226 scans)
        self.assertEqual(self._tree((27737, "(sleep)")), [27737])

    def test_a_grep_that_just_ended_is_still_waiting(self):
        self.assertEqual(self._tree((27737, "(grep)")), [27737])

    def test_other_work_that_just_ended_is_not_waiting(self):         # control
        self.assertEqual(self._tree((27737, "(npm)")), [])

    ZLOOP = ("/bin/zsh -c 'until grep -q DONE /private/tmp/claude-501/p/s/tasks/b1.output;"
             " do sleep 5; done'")

    def test_the_loops_own_fork_that_just_ended_is_the_same_loop(self):
        # a $(...) or pipeline fork of the shell, exited, not yet reaped
        self.assertEqual(self._tree((27737, "(zsh)"), loop=self.ZLOOP), [27737])

    def test_another_shell_that_just_ended_is_not_the_loop(self):        # control
        self.assertEqual(self._tree((27737, "(bash)"), loop=self.ZLOOP), [])

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

    def test_matches_the_tty_as_tty_prints_it(self):
        # pasted from `tty` (review 2 of the CLI revamp, slice 2)
        self.assertEqual([r["pid"] for r in ccwho.match_rows(self.ROWS, "/dev/ttys062")], [26581])

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
        for ep in (None, "claude-desktop", "sdk-cli"):         # review 3: one a restore leaves
            for sid in ([1], {"a": 1}):
                bad = _man(_local(2026, 9, 26, 10, 20), self.A)
                bad["sessions"].append({"sessionId": sid, "cwd": "/p", "entrypoint": ep})
                points = ccwho.save_points(self.saves()[1:2] + [("2026-09-26T1020.json", bad)])
                self.assertEqual([(p["count"], p["to_open"], p["left"]) for p in points],
                                 [(3, 3, 0), (1, 1, 0)], (ep, sid))

    def test_a_session_saved_twice_counts_as_its_first_entry(self):
        # as restore opens it: the first entry of an id (review 3)
        man = _man(_local(2026, 9, 26, 10, 20), self.A, self.A)
        man["sessions"][1]["entrypoint"] = "claude-desktop"
        p = ccwho.save_points([("2026-09-26T1020.json", man)])[0]
        self.assertEqual((p["count"], p["to_open"], p["left"]), (1, 1, 0))
        man["sessions"][0]["entrypoint"] = "claude-desktop"
        man["sessions"][1]["entrypoint"] = "cli"
        p = ccwho.save_points([("2026-09-26T1020.json", man)])[0]
        self.assertEqual((p["count"], p["to_open"], p["left"]), (1, 0, 1))

    def test_anything_not_a_manifest_name_is_left_out(self):
        self.assertEqual(ccwho.save_points([("notes.md", _man(1, self.A))]), [])

    def with_desktop(self):
        """A, B and C saved; B ran in Claude Desktop, which a restore leaves."""
        man = _man(_local(2026, 9, 26, 10, 20), self.A, self.B, self.C)
        man["sessions"][1]["entrypoint"] = "claude-desktop"
        return [("2026-09-26T1020.json", man)]

    def test_one_a_restore_leaves_is_not_to_reopen(self):
        # review 2 of env-panes: the menu said "1 to reopen", and the restore
        # it chose opened nothing
        p = ccwho.save_points(self.with_desktop(), live_ids={self.A})[0]
        self.assertEqual((p["count"], p["running"], p["to_open"], p["left"]), (3, 1, 1, 1))

    def test_one_with_no_resume_line_is_not_left(self):
        # --open cannot reopen it at all - wherever it ran (left_by_restore)
        man = self.with_desktop()
        man[0][1]["sessions"][1]["cwd"] = ""
        p = ccwho.save_points(man)[0]
        self.assertEqual((p["left"], p["to_open"]), (0, 3))

    def test_a_running_one_is_running_wherever_it_ran(self):              # control
        p = ccwho.save_points(self.with_desktop(), live_ids={self.B})[0]
        self.assertEqual((p["running"], p["to_open"], p["left"]), (1, 2, 0))


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

    def test_what_a_restore_leaves_is_not_to_reopen(self):
        # review 2 of env-panes: it said "1 to reopen", and the restore opened
        # nothing - nor "all running" while one is not
        text = self.line(count=2, running=1, to_open=0, left=1)
        self.assertIn("2 sessions · 1 running, 0 to reopen", text)
        self.assertNotIn("all running", text)
        self.assertIn("2 sessions · 1 to reopen", self.line(count=2, running=0, to_open=1, left=1))
        self.assertIn("2 sessions · 0 to reopen", self.line(count=2, running=0, to_open=0, left=2))

    def test_the_line_is_no_longer_for_it(self):
        # review 3: the menu shows 94 cells, and the mark is how you choose the
        # save before the restart - words for what it leaves pushed it off
        line = self.line(at=_local(2026, 9, 24, 0, 21), count=14, running=1, to_open=11,
                         left=2, before_reboot=True)
        self.assertEqual(line, self.line(at=_local(2026, 9, 24, 0, 21), count=14, running=1,
                                         to_open=11, before_reboot=True))
        end = line.index("last save before the restart") + len("last save before the restart")
        self.assertLessEqual(ccwho.visible_len(line[:end]), 94)

    def test_nothing_left_says_what_it_said(self):                         # control
        self.assertNotIn("·", self.line())
        self.assertIn("all running", self.line(running=12, to_open=0, left=0))

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

    def test_a_damaged_value_renders_as_text(self):
        # a manifest is read off disk (review 9: a tty, a topic, an ask that is
        # not text, and a skipped count that is not a number, raised)
        for field in ("project", "tty", "first", "topic", "ask", "sessionId", "cwd",
                      "configDir", "entrypoint", "pane", "tabTitle", "terminal"):
            for bad in ([1], {"a": 1}, 5, True, None):
                m = self.man()
                m["sessions"][0][field] = bad
                with self.subTest(field=field, bad=bad):
                    out = ccwho.render_restore(m, color=False)
                    self.assertIn("2 sessions to restore", out)
                    if field == "project":                  # shown as no name, not as "[1]"
                        self.assertTrue(out.splitlines()[1].startswith(" 1. ?  "), out)
        for top in ("skipped", "skippedWhy", "savedAt"):
            for bad in ("x", [1], {"a": 1}, None):
                m = dict(self.man(), **{top: bad})
                with self.subTest(top=top, bad=bad):
                    self.assertIn("2 sessions to restore", ccwho.render_restore(m, color=False))

    def test_one_with_no_resume_line_is_not_marked_left(self):
        m = self.man()
        m["sessions"][1].update(tty="", entrypoint="claude-desktop", cwd="")
        line = next(l for l in ccwho.render_restore(m, color=False).splitlines() if "football" in l)
        self.assertNotIn("leaves it", line)

    def test_a_session_with_no_terminal_window_says_open_leaves_it(self):
        m = self.man()
        m["sessions"][1].update(tty="", entrypoint="claude-desktop")
        lines = ccwho.render_restore(m, color=False).splitlines()
        line = next(l for l in lines if "football" in l)
        self.assertIn("it ran in Claude Desktop - --open leaves it", line)
        self.assertNotIn("leaves it", next(l for l in lines if "liveapp" in l))   # control


class TestASessionWithNoTerminalWindowIsNotReopened(unittest.TestCase):
    """2026-10-05: after a reboot, `o` opened two Claude Desktop sessions, idle
    for days, in new iTerm2 windows - "closed windows popped to the fore". A
    restore puts back what was in a terminal; one that ran in no terminal
    window is left where it was, and `ccwho open <id>` opens it if you want it
    (the owner's choice A).

    Only what started it says so: Claude Desktop, or a program. A saved tty
    that is empty proves nothing - ps that could not be read saves every
    session with none (review 1 of env-panes) - and reopens as before."""

    def why(self, **entry):
        return ccwho.no_window(dict({"sessionId": "s", "cwd": "/x"}, **entry))

    def test_claude_desktop(self):
        self.assertEqual(self.why(tty="", entrypoint="claude-desktop"), "it ran in Claude Desktop")

    def test_a_program_even_in_a_terminal(self):
        self.assertEqual(self.why(tty="ttys001", entrypoint="sdk-cli"), "a program ran it")

    def test_no_tty_saved_is_no_proof(self):
        self.assertEqual(self.why(tty=""), "")
        self.assertEqual(self.why(tty="  ", entrypoint="cli"), "")

    def test_a_terminal_session(self):                                        # control
        self.assertEqual(self.why(tty="ttys005", entrypoint="cli"), "")

    def test_no_tty_key_is_not_known(self):                                   # control
        self.assertEqual(self.why(), "")

    def test_junk_is_not_known(self):
        self.assertEqual(self.why(tty=None), "")
        self.assertEqual(self.why(tty=5, entrypoint=["claude-desktop"]), "")
        self.assertEqual(ccwho.no_window("not a dict"), "")


class AppleScriptQuoting(unittest.TestCase):
    def test_a_plain_string_round_trips(self):
        self.assertEqual(ccwho.terms.applescript_str("cd /tmp"), '"cd /tmp"')

    def test_a_double_quote_cannot_close_the_literal(self):
        # AppleScript has no \' escape; an unescaped " ends the string and the rest
        # becomes code. This is the whole reason the helper exists.
        self.assertEqual(ccwho.terms.applescript_str('say "hi"'), '"say \\"hi\\""')

    def test_a_backslash_is_escaped_first_so_it_cannot_eat_the_quote_escape(self):
        self.assertEqual(ccwho.terms.applescript_str('a\\"b'), '"a\\\\\\"b"')

    def test_a_newline_cannot_inject_a_second_statement(self):
        self.assertNotIn("\n", ccwho.terms.applescript_str("a\nb"))


class ItermOpenScript(unittest.TestCase):
    entries = [{"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef", "cwd": "/Users/x/p/liveapp"},
               {"sessionId": "a1b2c3d4-1111-4222-8333-abcdefabcdef", "cwd": "/Users/x/p/football"}]

    def test_one_window_per_session(self):
        s = ccwho.open_script(ccwho.terms.ITERM2, self.entries)
        self.assertEqual(s.count("create window with default profile"), 2)

    def test_each_window_runs_that_session_s_resume_line(self):
        s = ccwho.open_script(ccwho.terms.ITERM2, self.entries)
        self.assertIn("claude --resume 4f2b91ac-1111-4222-8333-abcdefabcdef", s)
        self.assertIn("claude --resume a1b2c3d4-1111-4222-8333-abcdefabcdef", s)

    def test_a_cwd_with_a_quote_is_escaped_into_the_applescript_literal(self):
        s = ccwho.open_script(ccwho.terms.ITERM2, [{"sessionId": "a1b2c3d4-1111-4222-8333-abcdefabcdef",
                                      "cwd": '/tmp/a"; do shell script "touch /tmp/pwned'}])
        for line in s.splitlines():
            if "write text" in line:
                self.assertEqual(line.count('"') % 2, 0, "unbalanced quotes: literal escaped early")
                self.assertNotIn('do shell script "touch', line.replace('\\"', "'"))

    def test_a_session_that_cannot_build_a_command_is_dropped_not_emitted_broken(self):
        s = ccwho.open_script(ccwho.terms.ITERM2, [{"sessionId": "", "cwd": "/tmp"}] + self.entries)
        self.assertEqual(s.count("create window with default profile"), 2)

    def test_nothing_to_open_yields_no_script(self):
        self.assertEqual(ccwho.open_script(ccwho.terms.ITERM2, []), "")

    def test_each_window_is_written_to_by_name_not_as_the_current_one(self):
        # `current window` is asked for after `create window`, as its own
        # event: a click on another iTerm2 window in between - seconds, when
        # iTerm2 is slow - and the resume line goes into THAT window's pane
        for s in (ccwho.open_script(ccwho.terms.ITERM2, self.entries),
                  ccwho.terms.ITERM2.run_script("cd /x && claude --resume y")):
            self.assertNotIn("current window", s)
            self.assertEqual(s.count("set w to (create window with default profile)"),
                             s.count("create window with default profile"))
            self.assertEqual(s.count("tell current session of w"),
                             s.count("create window with default profile"))


# A restore used to open one new window per session, and the person then moved
# each one by hand into the panes iTerm2 had restored. iTerm2 keeps a pane's
# `unique id` when it restores its windows (PTYSession.m adopts the saved
# "Session GUID" on window restoration and on the startup arrangement), so a
# save can name the pane and a restore can fill it.
class ItermPanes(unittest.TestCase):
    def test_the_script_asks_every_pane_for_its_tty_id_and_name(self):
        s = ccwho.terms.ITERM2.panes_script()
        for word in ("tty", "unique id", "name", "every tab"):
            self.assertIn(word, s)

    def test_a_line_is_tty_id_and_name(self):
        got = ccwho.terms.parse_panes("/dev/ttys045\tGUID-1\t✳ the reaper\n")
        self.assertEqual(got, {"ttys045": {"pane": "GUID-1", "name": "✳ the reaper"}})

    def test_a_pane_with_no_name_is_still_a_pane(self):
        got = ccwho.terms.parse_panes("/dev/ttys045\tGUID-1\t\n")
        self.assertEqual(got["ttys045"]["pane"], "GUID-1")

    def test_junk_and_a_line_with_no_id_are_skipped(self):
        got = ccwho.terms.parse_panes("garbage\n/dev/ttys001\t\tname\n/dev/ttys002\tG2\tn\n")
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
        s = ccwho.open_script(ccwho.terms.ITERM2, self.entries, fill={self.A: "G-A"})
        self.assertEqual(s.count("create window with default profile"), 1)
        self.assertIn('if u is "G-A"', s)
        self.assertIn("claude --resume " + self.A, s)
        self.assertIn("claude --resume " + self.B, s)

    def test_the_script_says_which_panes_it_wrote_into(self):
        s = ccwho.open_script(ccwho.terms.ITERM2, self.entries, fill={self.A: "G-A"})
        self.assertIn("return done", s)

    def test_a_pane_id_cannot_break_out_of_its_literal(self):
        s = ccwho.open_script(ccwho.terms.ITERM2, self.entries, fill={self.A: 'x" then do shell script "id'})
        checked = [l for l in s.splitlines() if " u is " in l]
        self.assertEqual(len(checked), 1)
        for line in checked:
            self.assertEqual(line.count('"') - line.count('\\"'), 2)

    def test_a_half_typed_line_is_cleared_before_the_resume_line(self):
        # `ps` cannot see "rm -rf " typed at a prompt; the resume line must not
        # be appended to it
        s = ccwho.open_script(ccwho.terms.ITERM2, self.entries, fill={self.A: "G-A", self.B: "G-B"})
        for sid in (self.A, self.B):
            at = s.index("claude --resume " + sid)
            branch = s[s.rindex("unique id", 0, at):at]
            # ^E first: ^U clears only left of the cursor in bash, fish and vi-insert
            self.assertIn("write text ((ASCII character 5) & (ASCII character 21)) newline no",
                          branch)

    def test_a_pane_id_is_written_at_most_once(self):
        s = ccwho.open_script(ccwho.terms.ITERM2, self.entries, fill={self.A: "G-A"})
        self.assertIn("if done does not contain u then", s)
        self.assertLess(s.index("if done does not contain u then"), s.index('if u is "G-A"'))

    def test_one_pane_failing_does_not_lose_what_was_written(self):
        # a pane that closes mid-loop must not abort the script: the ids already
        # written are only reported if the script reaches `return done`
        s = ccwho.open_script(ccwho.terms.ITERM2, self.entries, fill={self.A: "G-A"})
        start, end = s.index("      repeat with s in sessions of t"), s.index("    end repeat")
        loop = s[start:end]
        self.assertLess(loop.index("try"), loop.index("set u to unique id of s"))
        self.assertGreater(loop.rindex("end try"), loop.rindex("set done to done"))

    def test_each_pane_id_is_read_once(self):
        s = ccwho.open_script(ccwho.terms.ITERM2, self.entries, fill={self.A: "G-A", self.B: "G-B"})
        self.assertEqual(s.count("unique id of s"), 1)

    def test_the_filled_ids_are_returned_after_the_windows_open(self):
        s = ccwho.open_script(ccwho.terms.ITERM2, self.entries, fill={self.A: "G-A"})
        self.assertGreater(s.index("return done"), s.rindex("create window"))

    @unittest.skipUnless(shutil.which("osacompile"), "macOS only")
    def test_the_script_compiles(self):
        for fill in ({self.A: "G-A", self.B: "G-B"}, {self.A: "G-A"}, {}):
            s = ccwho.open_script(ccwho.terms.ITERM2, self.entries, fill=fill)
            r = testkit.compiles(self, ccwho.terms.ITERM2, s)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_only_filled_sessions_still_make_a_script(self):
        s = ccwho.open_script(ccwho.terms.ITERM2, self.entries[:1], fill={self.A: "G-A"})
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


class TestDamagedSavedValues(unittest.TestCase):
    """A manifest is read off disk: a value of the wrong type is no session to
    reopen and no title to match, never a traceback (review 8 of env-panes)."""

    def test_an_id_that_is_a_number_builds_no_resume_line(self):
        # str(12345678) passes the id check, and transcript_path then raises
        self.assertIsNone(ccwho.restore_command({"sessionId": 12345678, "cwd": "/x"}))
        self.assertTrue(ccwho.restore_command({"sessionId": "12345678", "cwd": "/x"}))  # control

    def test_a_pane_that_is_not_text_fills_nothing(self):
        panes = {"ttys050": {"pane": "G-1", "name": "t"}}
        for pane in ([1], {"a": 1}, 5):
            self.assertEqual(ccwho.match_panes([{"sessionId": "s1", "pane": pane}], panes,
                                               {"ttys050"}), {}, pane)
        self.assertEqual(ccwho.match_panes([{"sessionId": "s1", "pane": "G-1"}], panes,
                                           {"ttys050"}), {"s1": "G-1"})                   # control

    def test_a_title_that_is_not_text_has_no_key(self):
        self.assertEqual(ccwho.title_key(5), "")
        self.assertEqual(ccwho.title_key(["t"]), "")
        self.assertEqual(ccwho.title_key("✳ topic"), "topic")                          # control


class CheckManifest(unittest.TestCase):
    """The point of a restore manifest is that you find out it works BEFORE the
    reboot, not after. --check answers that without opening anything."""

    GOOD = {"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef", "cwd": "/p/liveapp",
            "project": "liveapp", "first": "f", "topic": "t", "ask": ""}

    def check(self, sessions, dirs=("/p/liveapp",), transcripts=("4f2b91ac-1111-4222-8333-abcdefabcdef",)):
        ok, problems, _left, _ready = ccwho.check_manifest(
            {"version": 1, "sessions": list(sessions)},
            problem_of=lambda e: ccwho.entry_problem(
                e, lambda p: p in dirs, lambda sid: "/tx" if sid in transcripts else None))
        return ok, problems

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
            ok, problems, left, ready = ccwho.check_manifest(junk, problem_of=lambda e: "")
            self.assertFalse(ok, repr(junk))
            self.assertEqual((left, ready), ([], []), repr(junk))

    def test_it_sorts_each_session_as_restore_open_does(self):
        # the owner, 2026-10-06: --check says what --open does (engine.sort_saved)
        desk = dict(self.GOOD, sessionId="44444444-4444-4444-8444-444444444444", project="cofs",
                    entrypoint="claude-desktop")
        gone = dict(self.GOOD, sessionId="66666666-6666-4666-8666-666666666666", cwd="/gone",
                    project="wt")
        ok, problems, left, ready = ccwho.check_manifest(
            {"sessions": [self.GOOD, desk, gone, dict(self.GOOD, cwd="/gone")]},
            problem_of=lambda e: ccwho.entry_problem(e, lambda p: p == "/p/liveapp",
                                                     lambda sid: "/tx"))
        self.assertEqual((ok, [n for n, _ in problems], [e["project"] for e, _ in left],
                          [e["project"] for e in ready]), (False, ["wt"], ["cofs"], ["liveapp"]))

    def test_each_session_is_asked_about_on_its_own(self):
        # problem_of gets the entry - its own config dir, as --open asks (review 8)
        asked = []
        ccwho.check_manifest({"sessions": [self.GOOD, dict(self.GOOD, sessionId="5" * 8,
                                                           configDir="/alt")]},
                             problem_of=lambda e: asked.append(e.get("configDir")) or "")
        self.assertEqual(asked, [None, "/alt"])


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

    # each row's link in `ccwho ls` on a terminal (TestJumpUrl's, kept when
    # parse_jump_url went: review 1 of the CLI revamp, slice 1)
    def test_a_jump_link_prefers_the_tty(self):
        self.assertEqual(ccwho.jump_url({"tty": "ttys032", "pid": 19576}), "ccwho://jump/s032")

    def test_a_jump_link_falls_back_to_the_pid(self):
        self.assertEqual(ccwho.jump_url({"tty": "", "pid": 19576}), "ccwho://jump/19576")

    def test_no_jump_link_with_neither(self):
        self.assertEqual(ccwho.jump_url({"tty": "", "pid": None}), "")

    def test_a_jump_link_tolerates_a_trailing_slash(self):
        self.assertEqual(ccwho.parse_ccwho_url("ccwho://jump/s032/"), ("jump", "s032"))

    def test_a_jump_link_with_more_after_its_target_is_refused(self):
        # the URL comes from LaunchServices, which any app can call (review 7
        # of the CLI revamp, slice 1: this went with TestJumpUrl)
        for bad in ("ccwho://jump/s032;rm -rf /", "ccwho://jump/s032x"):
            self.assertEqual(ccwho.parse_ccwho_url(bad), ("", ""), bad)


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
        got = ccwho.terms.parse_titles(self.DUMP)
        self.assertEqual(got["ttys000"], "\u25d1 Session discovery and management UX (claude)")
        self.assertEqual(got["ttys012"], "Chat (claude)")

    def test_a_shell_tab_is_named_too(self):
        self.assertEqual(ccwho.terms.parse_titles(self.DUMP)["ttys017"], "-zsh")

    def test_junk_lines_are_skipped_not_fatal(self):
        got = ccwho.terms.parse_titles(self.DUMP)
        self.assertNotIn("ttys099", got)
        self.assertEqual(len(got), 3)

    def test_nothing_from_iterm_is_an_empty_map(self):
        self.assertEqual(ccwho.terms.parse_titles(""), {})
        self.assertEqual(ccwho.terms.parse_titles(None), {})

    def test_the_script_asks_for_names_in_bulk(self):
        # one round trip for every session: the per-session form takes 1.6s
        script = ccwho.terms.ITERM2.titles_script()
        self.assertIn("name of sessions", script)
        self.assertNotIn("variable named", script)


class TestTitlesAreCachedBetweenTicks(unittest.TestCase):
    """Asking iTerm2 costs ~0.5s of a 1.2s run, and tab names change far more
    slowly than a 5s watch tick. `--watch` once burned a full core by re-doing
    per-tick work; this is the same mistake waiting to happen."""

    def setUp(self):
        self.calls = []

        def counted(timeout=5.0, **k):
            self.calls.append(1)
            return {"ttys022": "\u2733 Issue 362 (claude)"}

        testkit.patch(self, ccwho.terms.ITERM2, "titles", counted)

    def test_a_second_call_inside_the_window_reuses_the_answer(self):
        cache = {}
        a = ccwho.terms.titles_cached(cache, now=1000.0)
        b = ccwho.terms.titles_cached(cache, now=1000.0 + 5)
        self.assertEqual(a, b)
        self.assertEqual(len(self.calls), 1)

    def test_it_asks_again_once_the_window_has_passed(self):
        cache = {}
        ccwho.terms.titles_cached(cache, now=1000.0)
        ccwho.terms.titles_cached(cache, now=1000.0 + ccwho.terms.TITLES_TTL + 0.1)
        self.assertEqual(len(self.calls), 2)

    def test_without_a_cache_it_always_asks(self):          # control
        ccwho.terms.titles_cached(None, now=1000.0)
        ccwho.terms.titles_cached(None, now=1000.0)
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
        self.addCleanup(setattr, ccwho, "read_codex_threads", ccwho.read_codex_threads)
        ccwho.read_codex_threads = lambda env=None, cache=None: []    # Codex's locks: machine
        try:
            cache = {}
            ccwho.collect(cache=cache)
            ccwho.collect(cache=cache)
        finally:
            (ccwho.agents_json, ccwho.ps_snapshot, ccwho.tty_snapshot,
             ccwho.live_file_sessions) = real_agents, real_ps, real_ttys, real_files
        self.assertEqual(len(self.calls), 1, "iTerm2 was asked twice in two ticks")

    def test_tabs_opening_and_closing_do_not_make_it_ask(self):
        # 2026-09-29: the names are a label now - which terminals have a window
        # comes from ps (ITERM2.ttys) - so a new tab may show its "~" fallback for
        # up to a minute rather than cost iTerm2 an Apple Event per tab change
        cache = {}
        for i in range(20):
            ccwho.terms.titles_cached(cache, now=1000.0 + i * 59 / 19)
        self.assertEqual(len(self.calls), 1)

    def test_a_cache_from_before_a_hot_reload_does_not_break_the_list(self):
        # a running list reloads the engine every tick; its cache still holds
        # the entry the old engine wrote, with the set of terminals in it
        cache = {"_titles": (1000.0, {"ttys022": "old"}, {"ttys022"})}
        self.assertEqual(ccwho.terms.titles_cached(cache, now=1001.0), {"ttys022": "\u2733 Issue 362 (claude)"})

    def test_a_minute_later_it_asks_again(self):                        # control
        cache = {}
        ccwho.terms.titles_cached(cache, now=1000.0)
        ccwho.terms.titles_cached(cache, now=1000.0 + 61)
        self.assertEqual(len(self.calls), 2)

    def test_no_answer_is_not_cached_as_the_truth(self):
        # a timeout, or a gate that would not ask (None): retry on the next tick
        # rather than showing "~" fallbacks for the next minute
        testkit.patch(self, ccwho.terms.ITERM2, "titles",
                      lambda timeout=5.0, **k: self.calls.append(1) or None)
        cache = {}
        ccwho.terms.titles_cached(cache, now=1000.0)
        ccwho.terms.titles_cached(cache, now=1000.0 + 1)
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


class TestCollectCarriesTheCodexThreads(MachinelessCollect):
    """Phase 3 (D15): the open Codex threads reach the fleet - their own list,
    never session rows (jump, show and --json read the rows as Claude's)."""

    T = {"thread": "01a0eca7-7b42-72f0-b19a-ff0ae32db6a3", "name": "fix the navbar",
         "cwd": "/x", "host_pid": 4215, "originator": "Codex Desktop"}

    def collect(self, threads):
        real = ccwho.agents_json
        ccwho.agents_json = lambda: "[]"
        ccwho.read_codex_threads = lambda env=None, cache=None: threads
        try:
            return ccwho.collect(cache={}, status={})
        finally:
            ccwho.agents_json = real

    def test_they_are_in_the_fleet_and_not_in_the_rows(self):
        rows, fleet = self.collect([dict(self.T)])
        self.assertEqual([t["thread"] for t in fleet["codex_threads"]], [self.T["thread"]])
        self.assertEqual(fleet["codex_threads"][0]["procs"], 0)
        self.assertEqual(rows, [])

    def test_not_known_is_none(self):
        rows, fleet = self.collect(None)
        self.assertIsNone(fleet["codex_threads"])

    def test_a_write_during_the_scan_is_the_next_checks_change(self):
        # the scan's digest is taken before its Codex read (review 2026-10-02):
        # a write the read could have missed must show in the next check - a
        # digest taken after the read hid it for up to 20 s
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "rollout.jsonl")
        open(path, "w").close()
        os.utime(path, (1000, 1000))
        home = ccwho.codex_home()
        when = {"t": None}
        def read(env=None, cache=None):
            cache[f"_codex_watch:{home}"] = [path]
            if when["t"]:
                os.utime(path, (when["t"], when["t"]))     # written as it is read
            return [dict(self.T)]
        real = ccwho.agents_json
        ccwho.agents_json = lambda: "[]"
        ccwho.read_codex_threads = read
        cache = {}
        try:
            ccwho.collect(cache=cache, status={})          # the thread is seen
            quiet = {}
            ccwho.collect(cache=cache, status=quiet)       # nothing written
            self.assertEqual(quiet["watch"], ccwho.watch_digest([], cache=cache))  # control
            when["t"] = 2000
            busy = {}
            ccwho.collect(cache=cache, status=busy)
        finally:
            ccwho.agents_json = real
        self.assertNotEqual(busy["watch"], ccwho.watch_digest([], cache=cache))

    def test_the_read_gets_the_scans_own_cache(self):
        # its lsof answer is kept in that cache: without it every tick pays 0.5 s
        seen = []
        real = ccwho.agents_json
        ccwho.agents_json = lambda: "[]"
        ccwho.read_codex_threads = lambda env=None, cache=None: seen.append(cache) or []
        try:
            cache = {}
            ccwho.collect(cache=cache, status={})
        finally:
            ccwho.agents_json = real
        self.assertIs(seen[0], cache)

    def test_the_turn_you_looked_at_reaches_the_threads(self):
        real = ccwho.load_reviewed
        ccwho.load_reviewed = lambda *a, **k: {self.T["thread"]: 42}
        self.addCleanup(setattr, ccwho, "load_reviewed", real)
        rows, fleet = self.collect([dict(self.T, turn="done", ask="", ts=42, mtime=1.0)])
        self.assertEqual(fleet["codex_threads"][0]["seen_ts"], 42)

    def test_ports_not_read_are_none(self):
        ccwho.listen_ports = lambda: None
        rows, fleet = self.collect([dict(self.T)])
        self.assertIsNone(fleet["codex_threads"][0]["ports"])


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

    def test_a_group_says_whether_it_needs_you(self):
        # STOPPED was drawn in the colour of NEEDS YOU, and a glance read it as
        # a question waiting. The screen draws the two kinds apart; which kind a
        # group is, is decided here.
        states = ("blocked", "review", "stuck", "stopped", "busy", "program")
        groups = ccwho.ui_groups([{"sessionId": s, "attention": s, "ts": ""}
                                  for s in states])
        self.assertEqual({g["heading"]: g["needs_you"] for g in groups},
                         {"NEEDS YOU": True, "STUCK": True, "STOPPED": False,
                          "BUSY": False, "PROGRAMS": False})

    def test_a_group_needs_you_when_its_marks_say_so(self):
        # the rule, not a list of headings: a heading is amber over amber marks
        self.addCleanup(ccwho.UI_STATE_STYLE.update, dict(ccwho.UI_STATE_STYLE))
        ccwho.UI_STATE_STYLE.update(stopped="needs", stuck="quiet")
        groups = ccwho.ui_groups([{"sessionId": s, "attention": s, "ts": ""}
                                  for s in ("stuck", "stopped")])
        self.assertEqual({g["heading"]: g["needs_you"] for g in groups},
                         {"STUCK": False, "STOPPED": True})

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
                first = ccwho.ui_row_cells(row, width=width, tag="ant:1a2b")[0]
                self.assertIn("· 5h · s022", "".join(t for t, _ in first))
                # the account whole or not at all: "· 1…" is no account - and
                # without its brand before not at all
                shown = [t for t, role in first if role == "account"]
                self.assertIn(shown, ([], [" · ant:1a2b"], [" · 1a2b"]))
                if width >= 65:             # where "1a2b" fits, the account is there
                    self.assertNotEqual(shown, [])
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
        self.addCleanup(setattr, ccwho, "read_codex_threads", ccwho.read_codex_threads)
        ccwho.read_codex_threads = lambda env=None, cache=None: []    # Codex's locks: machine
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

    def test_the_oldest_ask_comes_first(self):
        # you take the top one: newest-first means a fresh ask always jumps the
        # queue and the one that has waited longest never gets answered.
        # collect() hands them over newest first.
        rows = [{"sessionId": s, "attention": "asks", "ts": ts}
                for s, ts in (("new", 300), ("mid", 200), ("old", 100))]
        groups = ccwho.ui_groups(rows)
        self.assertEqual([r["sessionId"] for r in groups[0]["rows"]],
                         ["old", "mid", "new"])

    def test_the_urgent_kind_still_comes_before_an_older_ask(self):
        rows = [{"sessionId": "q", "attention": "blocked", "ts": 300},
                {"sessionId": "a", "attention": "asks", "ts": 100}]
        groups = ccwho.ui_groups(rows)
        self.assertEqual([r["sessionId"] for r in groups[0]["rows"]], ["q", "a"])

    def test_an_ask_with_no_time_waits_behind_the_known_ones(self):
        rows = [{"sessionId": "none", "attention": "asks", "ts": None},
                {"sessionId": "new", "attention": "asks", "ts": 300},
                {"sessionId": "old", "attention": "asks", "ts": 100}]
        groups = ccwho.ui_groups(rows)
        self.assertEqual([r["sessionId"] for r in groups[0]["rows"]],
                         ["old", "new", "none"])

    def test_the_other_groups_keep_newest_first(self):                 # control
        rows = [{"sessionId": s, "attention": "stopped", "ts": ts}
                for s, ts in (("new", 300), ("old", 100))]
        groups = ccwho.ui_groups(rows)
        self.assertEqual([r["sessionId"] for r in groups[0]["rows"]],
                         ["new", "old"])

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
        self.addCleanup(setattr, ccwho, "read_codex_threads", ccwho.read_codex_threads)
        ccwho.read_codex_threads = lambda env=None, cache=None: []    # Codex's locks: machine
        try:
            status = {}
            ccwho.collect(cache={}, status=status)
        finally:
            ccwho.reviewed_path = real
            ccwho.agents_json, ccwho.live_file_sessions = real_agents, real_files
        self.assertEqual(status.get("reviewed"), {"a": "t1"})


class TestParksAreKept(unittest.TestCase):
    """A session you keep but will not work on now is parked: out of NEEDS YOU,
    with a note that says why (owner, 2026-10-08). The note was text left unsent
    in the session's prompt box - in no file, and lost with the process."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "parked.json")

    def test_nothing_parked_yet_is_an_empty_answer(self):
        self.assertEqual(ccwho.load_parks(self.path), {})

    def test_a_park_comes_back_with_its_turn_and_note(self):
        self.assertTrue(ccwho.park("a", 1788177600.5, "temp folders checked later",
                                   path=self.path))
        self.assertEqual(ccwho.load_parks(self.path),
                         {"a": {"ts": 1788177600.5, "note": "temp folders checked later"}})

    def test_no_note_is_a_park_too(self):
        ccwho.park("a", "t1", path=self.path)
        self.assertEqual(ccwho.load_parks(self.path), {"a": {"ts": "t1", "note": ""}})

    def test_unpark_takes_it_all_away(self):
        ccwho.park("a", "t1", "why", path=self.path)
        ccwho.park("b", "t2", path=self.path)
        self.assertTrue(ccwho.unpark("a", path=self.path))
        self.assertEqual(ccwho.load_parks(self.path), {"b": {"ts": "t2", "note": ""}})

    def test_unparking_what_is_not_parked_is_no_error(self):
        self.assertTrue(ccwho.unpark("nobody", path=self.path))
        self.assertEqual(ccwho.load_parks(self.path), {})

    def test_the_note_is_one_printable_line(self):
        # it goes to a terminal and to agents (--json)
        ccwho.park("a", "t1", "two\nlines \x1b[31mred\x07", path=self.path)
        self.assertEqual(ccwho.load_parks(self.path)["a"]["note"], "two lines ?[31mred?")

    def test_a_long_note_is_cut(self):
        ccwho.park("a", "t1", "x" * 1000, path=self.path)
        self.assertEqual(len(ccwho.load_parks(self.path)["a"]["note"]), ccwho.PARK_NOTE_MAX)

    def test_a_file_full_of_nonsense_is_not_a_crash(self):
        with open(self.path, "w") as fh:
            fh.write("{not json")
        self.assertEqual(ccwho.load_parks(self.path), {})

    def test_an_entry_of_the_wrong_shape_is_left_out(self):
        with open(self.path, "w") as fh:
            json.dump({"a": "t1", "b": {"note": "no turn"}, "c": {"ts": True},
                       "d": {"ts": "t4", "note": 7}, "e": {"ts": "t5", "note": "kept"},
                       "f": {"ts": None, "note": ""}, "g": {"ts": ["t7"], "note": ""}}, fh)
        self.assertEqual(ccwho.load_parks(self.path), {"e": {"ts": "t5", "note": "kept"}})

    def test_a_park_after_a_bad_file_starts_again(self):
        with open(self.path, "w") as fh:
            fh.write("[1, 2]")
        self.assertTrue(ccwho.park("a", "t1", path=self.path))
        self.assertEqual(ccwho.load_parks(self.path), {"a": {"ts": "t1", "note": ""}})

    def test_a_half_written_file_never_reaches_a_reader(self):
        ccwho.park("a", "t1", path=self.path)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["parked.json"])

    def test_it_does_not_grow_for_ever(self):
        for i in range(ccwho.PARKS_CAP + 5):
            ccwho.park(f"s{i}", f"t{i}", path=self.path)
        kept = ccwho.load_parks(self.path)
        self.assertEqual(len(kept), ccwho.PARKS_CAP)
        self.assertIn(f"s{ccwho.PARKS_CAP + 4}", kept, "the newest are kept")
        self.assertNotIn("s0", kept)

    def test_parking_again_makes_it_the_newest(self):
        for i in range(ccwho.PARKS_CAP):
            ccwho.park(f"s{i}", f"t{i}", path=self.path)
        ccwho.park("s0", "t0b", path=self.path)
        ccwho.park("new", "t", path=self.path)
        kept = ccwho.load_parks(self.path)
        self.assertIn("s0", kept)
        self.assertNotIn("s1", kept, "the oldest went")

    def test_a_write_that_fails_half_way_leaves_the_parks_whole(self):
        ccwho.park("a", "t1", "kept", path=self.path)
        real = ccwho.json.dump

        def half(obj, fh, **kw):
            fh.write('{"b": {"ts"')
            raise OSError("disk full")
        testkit.patch(self, ccwho.json, "dump", half)
        self.assertFalse(ccwho.park("b", "t2", path=self.path))
        testkit.patch(self, ccwho.json, "dump", real)
        self.assertEqual(ccwho.load_parks(self.path), {"a": {"ts": "t1", "note": "kept"}})
        self.assertEqual(os.listdir(self.tmp), ["parked.json"], "no temp file left")

    def test_its_folder_is_made_when_there_is_none(self):
        path = os.path.join(self.tmp, "missing", "parked.json")
        self.assertTrue(ccwho.park("a", 1.0, path=path))
        self.assertEqual(ccwho.load_parks(path), {"a": {"ts": 1.0, "note": ""}})

    def test_a_park_that_cannot_be_saved_says_so(self):
        # the folder it goes in is a file: the write cannot happen
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w"):
            pass
        path = os.path.join(blocker, "parked.json")
        self.assertFalse(ccwho.park("a", "t1", path=path))
        self.assertFalse(ccwho.unpark("a", path=path))

    def test_it_lives_with_the_rest_of_ccwhos_files(self):
        had = os.environ.pop("CCWHO_DIR", None)
        self.addCleanup(lambda: os.environ.__setitem__("CCWHO_DIR", had) if had is not None
                        else os.environ.pop("CCWHO_DIR", None))
        os.environ["CCWHO_DIR"] = self.tmp
        self.assertEqual(ccwho.parks_path(), self.path)
        os.environ.pop("CCWHO_DIR")
        self.assertEqual(ccwho.parks_path(),
                         os.path.join(os.path.expanduser("~"), ".ccwho", "parked.json"))


class TestAParkedRow(unittest.TestCase):
    """Parked until the session writes a new turn, or you go to it (owner,
    2026-10-08): then it is as if it was never parked."""

    SESSION = TestBuildRow.SESSION

    def tail(self, ts="2026-08-31T12:00:00.000Z", text="Want me to build S2a?"):
        return [json.dumps({"type": "assistant", "timestamp": ts,
                            "message": {"content": [{"type": "text", "text": text}]}})]

    def build(self, parks=None, session=None, tail=None):
        return ccwho.build_row(session or self.SESSION, [], tail or self.tail(), mtime=0,
                               now=1788177600.0, parks=parks)

    def test_parked_at_its_last_turn_it_is_parked(self):
        ts = self.build()["ts"]
        row = self.build(parks={"s1": {"ts": ts, "note": "checked later"}})
        self.assertEqual(row["attention"], "parked")
        self.assertEqual(row["park_note"], "checked later")
        self.assertEqual(row["park_was"], "asks", "what it is when not parked")

    def test_a_new_turn_ends_the_park(self):
        old = self.build(tail=self.tail("2026-08-30T12:00:00.000Z"))["ts"]
        row = self.build(parks={"s1": {"ts": old, "note": "checked later"}})
        self.assertEqual(row["attention"], "asks")
        self.assertEqual(row["park_note"], "")
        self.assertEqual(row["park_was"], "")

    def test_a_finished_turn_can_be_parked(self):
        tail = self.tail(text="234 tests green.")
        ts = self.build(tail=tail)["ts"]
        row = self.build(parks={"s1": {"ts": ts, "note": ""}}, tail=tail)
        self.assertEqual((row["attention"], row["park_was"]), ("parked", "review"))

    def test_a_turn_with_no_time_is_never_parked(self):
        tail = [json.dumps({"type": "assistant",
                            "message": {"content": [{"type": "text", "text": "Shall I?"}]}})]
        row = self.build(parks={"s1": {"ts": None, "note": ""}}, tail=tail)
        self.assertEqual(row["attention"], "asks")

    def test_another_sessions_park_is_not_this_ones(self):           # control
        ts = self.build()["ts"]
        self.assertEqual(self.build(parks={"s2": {"ts": ts, "note": ""}})["attention"], "asks")

    def test_a_programs_session_stays_a_programs(self):
        session = dict(self.SESSION, entrypoint="sdk-py")
        ts = self.build(session=session)["ts"]
        row = self.build(parks={"s1": {"ts": ts, "note": ""}}, session=session)
        self.assertEqual(row["attention"], "program")

    def test_the_note_is_one_line_on_the_row(self):
        ts = self.build()["ts"]
        row = self.build(parks={"s1": {"ts": ts, "note": "two\nlines"}})
        self.assertEqual(row["park_note"], "two lines")

    def test_a_scan_reads_the_parks(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "parked.json")
        testkit.patch(self, ccwho, "parks_path", lambda: path)
        testkit.patch(self, ccwho, "agents_json", lambda: json.dumps([dict(
            self.SESSION, pid=11, kind="interactive")]))
        testkit.patch(self, ccwho, "read_windows",
                      lambda sid, cache=None: ([], self.tail(), 0))
        for name, fake in (("ps_snapshot", lambda: ""), ("tty_snapshot", lambda: ""),
                           ("live_file_sessions", lambda *a, **k: ([], 0)),
                           ("ps_table", lambda: {}), ("listen_ports", lambda: {}),
                           ("stdin_sockets", lambda pids: {}),
                           ("read_codex_threads", lambda env=None, cache=None: []),
                           ("verdicts_path", lambda: os.path.join(tmp, "readers.json"))):
            testkit.patch(self, ccwho, name, fake)
        for app in ccwho.terms.APPS:
            testkit.patch(self, app, "titles", lambda timeout=5.0, **k: {})
        rows, _ = ccwho.collect(cache={})
        self.assertEqual(rows[0]["attention"], "asks")                # control
        ccwho.park("s1", rows[0]["ts"], "later", path=path)
        rows, _ = ccwho.collect(cache={})
        self.assertEqual((rows[0]["attention"], rows[0]["park_note"]), ("parked", "later"))


class TestParkedInTheList(unittest.TestCase):
    """PARKED is its own quiet group, after BUSY: nothing in it needs you."""

    def rows(self):
        return [{"sessionId": s, "attention": s, "ts": "t"} for s in
                ("asks", "stopped", "busy", "parked", "program")]

    def test_parked_rows_have_their_own_group_after_busy(self):
        groups = ccwho.ui_groups(self.rows())
        self.assertEqual([g["heading"] for g in groups],
                         ["NEEDS YOU", "STOPPED", "BUSY", "PARKED", "PROGRAMS"])
        self.assertFalse(groups[3]["needs_you"])

    def test_needs_you_leaves_parked_rows_out(self):
        self.assertEqual([r["attention"] for r in ccwho.needs_you(self.rows())], ["asks"])

    def test_it_is_called_parked(self):
        self.assertEqual(ccwho._LABEL["parked"], "PARKED")

    def test_it_sorts_after_running_and_before_programs(self):
        rank = ccwho._RANK
        self.assertLess(rank["running"], rank["parked"])
        self.assertLess(rank["parked"], rank["program"])

    def test_the_table_shows_the_note(self):
        row = {"sessionId": "a", "attention": "parked", "status": "idle",
               "project": "liveapp", "title": "x", "tab_title": "", "name": "n",
               "doing": "Bash: run it", "ask": "Shall I land it?", "since": "1m", "ts": "",
               "topic": "", "first": "", "age": "", "orphans": 0, "work": 0, "tty": "",
               "pid": 1, "cwd": "/x", "recap": "", "recap_ts": "", "recap_age": "",
               "turns_since_recap": 0, "park_note": "temp folders checked later",
               "park_was": "asks"}
        out = ccwho.render([row], 0, color=False, width=160)
        self.assertIn("PARKED", out)
        self.assertIn("temp folders checked later", out)
        self.assertNotIn("Shall I land it?", out)
        out = ccwho.render([dict(row, park_note="")], 0, color=False, width=160)
        self.assertIn("Bash: run it", out)                            # control


class TestTheParkTarget(unittest.TestCase):
    """[park] on a row that needs you: a left click is the only click that
    reaches ccwho in iTerm2, so the action needs something visible to click."""

    def row(self, att="asks", **kw):
        return dict({"sessionId": "aaaa1111-0000-4000-8000-000000000001", "attention": att,
                     "ts": "2026-08-31T12:00:00.000Z", "name": "liveapp-de",
                     "project": "liveapp", "since": "17h", "tty": "ttys009",
                     "recap": "the temp folder cleanup", "recap_age": "17h",
                     "dead_loops": []}, **kw)

    def zone(self, row, width=100):
        _, second = ccwho.ui_row_cells(row, width=width)
        col = 0
        for text, role in second:
            if role == "park":
                return col
            col += ccwho._cells(text)
        return None

    def test_a_row_that_needs_you_offers_it(self):
        for att in ("asks", "review", "blocked", "waiting"):
            with self.subTest(att=att):
                _, second = ccwho.ui_row_cells(self.row(att), width=100)
                self.assertEqual([t for t, r in second if r == "park"], [ccwho.UI_PARK])

    def test_no_other_row_offers_it(self):                           # control
        for att in ("stopped", "busy", "running", "stuck", "parked", "program", "ended"):
            with self.subTest(att=att):
                _, second = ccwho.ui_row_cells(self.row(att), width=100)
                self.assertNotIn("park", [r for _, r in second])

    def test_a_codex_thread_is_not_parked(self):
        _, second = ccwho.ui_row_cells(self.row(kind="codex"), width=100)
        self.assertNotIn("park", [r for _, r in second])

    def test_a_row_with_no_turn_is_not_parked(self):
        _, second = ccwho.ui_row_cells(self.row(ts=None), width=100)
        self.assertNotIn("park", [r for _, r in second])

    def test_the_click_lands_on_it(self):
        row = self.row()
        col = self.zone(row)
        self.assertEqual(ccwho.ui_action_at(row, 100, 1, col), "park")
        self.assertEqual(ccwho.ui_action_at(row, 100, 1, col + len(ccwho.UI_PARK) - 1), "park")
        self.assertIsNone(ccwho.ui_action_at(row, 100, 1, col - 1))

    def test_it_survives_a_narrow_window(self):
        _, second = ccwho.ui_row_cells(self.row(), width=40)
        self.assertEqual([t for t, r in second if r == "park"], [ccwho.UI_PARK])

    def test_park_gives_way_before_the_kill_is_cut(self):
        # z still parks; a cut kill is no button (review 1 of slice 2)
        row = self.row(dead_loops=[{"pid": 86246, "tasks": ["bscl8fc6k"]}])
        for width in range(36, 61):
            with self.subTest(width=width):
                _, second = ccwho.ui_row_cells(row, width=width)
                self.assertIn((ccwho.UI_KILL, "action"), second)

    def test_park_is_whole_or_not_there(self):
        for row in (self.row(), self.row(dead_loops=[{"pid": 86246, "tasks": ["b"]}])):
            for width in range(24, 61):
                with self.subTest(width=width, loops=bool(row["dead_loops"])):
                    _, second = ccwho.ui_row_cells(row, width=width)
                    self.assertIn([t for t, r in second if r == "park"], ([], [ccwho.UI_PARK]))
        _, second = ccwho.ui_row_cells(self.row(), width=100)                # control
        self.assertEqual([t for t, r in second if r == "park"], [ccwho.UI_PARK])

    def test_with_a_stuck_loop_both_are_there(self):
        row = self.row(dead_loops=[{"pid": 86246, "tasks": ["bscl8fc6k"]}])
        _, second = ccwho.ui_row_cells(row, width=100)
        self.assertEqual([t for t, r in second if r in ("park", "action")],
                         [ccwho.UI_PARK, ccwho.UI_KILL])
        col = self.zone(row)
        self.assertEqual(ccwho.ui_action_at(row, 100, 1, col), "park")
        self.assertEqual(ccwho.ui_action_at(row, 100, 1, col + len(ccwho.UI_PARK) + 2), "kill")

    def test_the_key_parks_any_row_whose_turn_has_ended(self):
        for att in ("asks", "review", "blocked", "waiting", "stopped", "running", "stuck"):
            with self.subTest(att=att):
                self.assertTrue(ccwho.parkable(self.row(att)))

    def test_the_key_does_not_park_what_cannot_wait(self):           # control
        # busy: its next turn ends the park at once; a program's is not yours;
        # an ended one has no turn to come; a Codex thread is its app's
        for row in (self.row("busy"), self.row("program"), self.row("ended"),
                    self.row("parked"), self.row(kind="codex"), self.row(ts=None),
                    self.row(ts=""), self.row(sessionId="")):
            with self.subTest(row=(row["attention"], row.get("kind"), row["ts"])):
                self.assertFalse(ccwho.parkable(row))

    def test_a_parked_row_shows_its_note_in_place_of_the_recap(self):
        row = self.row("parked", park_note="checked later")
        _, second = ccwho.ui_row_cells(row, width=100)
        text = "".join(t for t, _ in second)
        self.assertIn("checked later", text)
        self.assertNotIn("the temp folder cleanup", text)

    def test_a_parked_row_with_no_note_shows_its_recap(self):          # control
        _, second = ccwho.ui_row_cells(self.row("parked", park_note=""), width=100)
        self.assertIn("the temp folder cleanup", "".join(t for t, _ in second))


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


class TestALabelLooksLikeNeedsYouOnlyWhenItDoes(unittest.TestCase):
    """ccwho ls printed STOPPED in the yellow of FINISHED, one bold away from
    NEEDS YOU, and at a glance it read as a question waiting. The list's
    headings were fixed first; this table follows the same rule, and so does the
    detail pane, which paints its label from it: a warm colour means it needs you.
    """

    # what each kind may be painted with, and nothing else: no bright, 256-colour
    # or background form of a warm colour gets past a list of what is allowed
    WARM = {"1", "31", "33", "35", "91", "93", "95"}     # bold; red, yellow, magenta
    CALM = {"1", "2", "32", "92"}                        # bold, dim; green

    def params(self, key):
        """The parameters of each escape in a paint: "\\033[33;1m" is 33 and 1."""
        # all of it is read: an escape this cannot parse (\033[38:5:214m, an
        # orange) must fail here, not pass as a paint of nothing
        self.assertRegex(ccwho._C[key], r"\A(?:\x1b\[[\d;]*m)*\Z")
        return [p for code in re.findall(r"\x1b\[([\d;]*)m", ccwho._C[key])
                for p in code.split(";")]

    def split(self):
        """Every grouped state, by whether its group needs you - the list's answer."""
        states = [s for _, group in ccwho.UI_GROUPS for s in group]
        groups = ccwho.ui_groups([{"sessionId": s, "attention": s, "ts": ""}
                                  for s in states])
        needs = {r["attention"] for g in groups if g["needs_you"] for r in g["rows"]}
        return needs, set(states) - needs

    def row(self, attention):
        return {"sessionId": attention, "attention": attention, "status": "idle",
                "project": "liveapp", "title": "x", "tab_title": "", "name": attention,
                "doing": "", "ask": "", "since": "1m", "ts": "", "topic": "",
                "first": "", "age": "", "orphans": 0, "work": 0, "tty": "",
                "pid": 1, "cwd": "/x", "recap": "", "recap_ts": "",
                "recap_age": "", "turns_since_recap": 0}

    def test_what_needs_you_is_warm(self):                              # control
        needs, _ = self.split()
        self.assertEqual(needs, {"blocked", "waiting", "asks", "review", "stuck"})
        for state in needs:
            with self.subTest(state=state):
                self.assertLessEqual(set(self.params(state)), self.WARM)
                self.assertTrue(set(self.params(state)) - {"1"}, "a colour, not only bold")

    def test_nothing_else_is_warm(self):
        _, calm = self.split()
        for state in calm:
            with self.subTest(state=state):
                self.assertLessEqual(set(self.params(state)), self.CALM,
                                     f"{state} looks like it needs you")

    def test_stopped_and_busy_are_green_as_in_the_list(self):
        for state in ("stopped", "busy"):
            self.assertEqual(self.params(state), ["32"], state)

    def test_ls_and_the_detail_pane_paint_each_state_from_the_table(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "test_fixture_render_brief.json"),
                  encoding="utf-8") as fh:
            brief = json.load(fh)[0]["brief"]
        reset = ccwho._C["reset"]
        for _, states in ccwho.UI_GROUPS:
            for state in states:
                with self.subTest(state=state):
                    label, paint = ccwho._LABEL[state], ccwho._C[state]
                    out = ccwho.render([self.row(state)], 0, color=True, width=150)
                    self.assertIn(f"{paint}{label:<10}{reset}", out)
                    row = {"attention": state, "project": "liveapp"}
                    # ccwho brief
                    self.assertIn(f"{paint}{label}{reset}",
                                  ccwho.render_brief(brief, row).splitlines()[0])
                    # the list's detail pane, painted part by part as ccwho_ui does
                    pane = "".join(ccwho.brief_ansi(text, style) for text, style, *_ in
                                   ccwho.brief_parts(brief, row, fields=True)[0])
                    self.assertIn(f"{paint}{label}{reset}", pane)


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

    def test_needs_you_leaves_it_out(self):
        rows = [self.row("cli"), self.row("sdk-cli")]
        self.assertEqual([r["attention"] for r in ccwho.needs_you(rows)], ["blocked"])


class TestNeedsYouIsWhatTheListPutsOnTop(unittest.TestCase):
    """`ccwho ls --needs-you`: the rows the live list shows in NEEDS YOU and
    STUCK - by ccwho's own state, as the list sorts them. It was `--blocked`,
    which kept Claude Code's raw `waiting` (a finished turn that needs nothing
    is `waiting` too) and missed ASKED YOU and FINISHED, which it reports `idle`
    (the owner, 2026-10-06)."""

    STATES = ("blocked", "waiting", "asks", "review", "stuck", "stopped", "ready", "shell",
              "idle", "busy", "running", "program", "ended")

    def kept(self, rows):
        return [r["attention"] for r in ccwho.needs_you(rows)]

    def test_the_list_s_top_groups(self):
        rows = [{"sessionId": a, "attention": a} for a in self.STATES]
        self.assertEqual(self.kept(rows), ["blocked", "waiting", "asks", "review", "stuck"])

    def test_by_ccwho_s_state_not_claude_code_s(self):
        # asks and review are reported idle; a turn that ended with nothing
        # pending, and you have seen, is reported waiting
        rows = [{"sessionId": "a", "attention": "asks", "status": "idle"},
                {"sessionId": "b", "attention": "review", "status": "idle"},
                {"sessionId": "c", "attention": "stopped", "status": "waiting"}]
        self.assertEqual(self.kept(rows), ["asks", "review"])

    def test_detached_work_alone_is_not_a_question(self):
        # `ccwho ps` and the list's "left behind" line show it
        self.assertEqual(self.kept([{"sessionId": "a", "attention": "stopped", "orphans": 2}]), [])

    def test_it_follows_the_list_s_groups(self):
        # one definition: a state the list moves into NEEDS YOU is kept here too
        real = ccwho.UI_GROUPS
        self.addCleanup(setattr, ccwho, "UI_GROUPS", real)
        ccwho.UI_GROUPS = (("NEEDS YOU", ("busy",)),) + tuple(g for g in real if g[0] != "NEEDS YOU")
        self.assertEqual(self.kept([{"sessionId": "a", "attention": "busy"},
                                    {"sessionId": "b", "attention": "asks"}]), ["busy"])


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

    def test_an_attach_in_a_window_wins_whatever_the_order(self):
        # two attaches, one on a tty no app shows (a shell iTerm2's daemon kept):
        # the one in a window, in whichever order ps lists them (review of slice 2)
        ttys = {7000: "ttys024", 7001: "ttys000"}
        for order in ((7000, 7001), (7001, 7000)):
            cmds = {pid: f"claude attach {self.SID}" for pid in order}
            self.assertEqual(ccwho.owning_tty(5000, {}, ttys, self.TITLES, commands=cmds,
                                              session_id=self.SID), "ttys000", order)

    def test_an_ancestor_in_a_window_wins_over_an_attach_in_none(self):
        parents = {28087: 27713, 27713: 1378}
        ttys = {28087: "ttys042", 1378: "ttys000", 7000: "ttys024"}
        cmds = {28087: "claude bg-spare", 27713: "claude daemon run",
                1378: "claude --resume", 7000: f"claude attach {self.SID}"}
        self.assertEqual(ccwho.owning_tty(28087, parents, ttys, self.TITLES, commands=cmds,
                                          session_id=self.SID), "ttys000")

    def test_with_no_viewer_in_a_window_an_attach_is_it(self):          # control
        # the old order stands when no window shows one: the attach first
        parents = {28087: 27713, 27713: 1378}
        ttys = {28087: "ttys042", 1378: "ttys031", 7000: "ttys024"}
        cmds = {28087: "claude bg-spare", 27713: "claude daemon run",
                1378: "claude --resume", 7000: f"claude attach {self.SID}"}
        self.assertEqual(ccwho.owning_tty(28087, parents, ttys, self.TITLES, commands=cmds,
                                          session_id=self.SID), "ttys024")
        del cmds[7000]
        self.assertEqual(ccwho.owning_tty(28087, parents, ttys, self.TITLES, commands=cmds,
                                          session_id=self.SID), "ttys031")


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
        ok, problems, _left, _ready = ccwho.check_manifest(
            man, problem_of=lambda e: ccwho.entry_problem(
                e, os.path.isdir, ccwho.manifest_transcript_finder({"sessions": [e]})))
        self.assertTrue(ok, problems)

    def test_restore_check_still_fails_a_gone_transcript(self):       # control
        man = {"sessions": [{"sessionId": "eeee5555-0000-4000-8000-000000000005",
                             "cwd": self.dir, "configDir": self.dir}]}
        ok, _p, _left, _ready = ccwho.check_manifest(
            man, problem_of=lambda e: ccwho.entry_problem(
                e, os.path.isdir, ccwho.manifest_transcript_finder({"sessions": [e]})))
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
            returncode, stdout = 0, ""

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
            for _ in range(50):             # until it has exec'd: before that the
                got = ccwho.read_procargs(child.pid) or {}   # fork holds OUR env
                if got.get("CLAUDE_CONFIG_DIR") == "/fake/dir":
                    break
                time.sleep(0.05)
        finally:
            child.kill()
            child.wait()
        self.assertEqual(got, {"CLAUDE_CONFIG_DIR": "/fake/dir"})
        self.assertNotIn(self.TOKEN, repr(got))

    def test_a_pid_that_does_not_exist_is_none(self):                # control
        self.assertIsNone(ccwho.read_procargs(99999999))

    def test_the_auth_of_a_real_process_is_its_token_fingerprint_only(self):
        # #28: the statusLine reads its claude's token this way - on the real
        # kernel, the fingerprint comes back and the token does not
        import hashlib
        import sys
        import time
        if sys.executable.startswith(("/usr/bin/", "/bin/", "/System/")):
            self.skipTest("the test's own Python is a system binary: its env is hidden")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                                 env={"CLAUDE_CODE_OAUTH_TOKEN": self.TOKEN,
                                      "CLAUDE_CONFIG_DIR": "/fake/dir", "PATH": "/bin"})
        try:
            got = {}
            for _ in range(50):             # until it has exec'd: before that the
                got = ccwho.read_auth(child.pid) or {}       # fork holds OUR env
                if got.get("config_dir") == "/fake/dir":
                    break
                time.sleep(0.05)
        finally:
            child.kill()
            child.wait()
        self.assertEqual(got, {"token_fp": hashlib.sha256(self.TOKEN.encode()).hexdigest()[:16],
                               "config_dir": "/fake/dir"})
        self.assertNotIn(self.TOKEN, repr(got))

    def test_the_auth_of_a_pid_that_does_not_exist_is_none(self):     # control
        self.assertIsNone(ccwho.read_auth(99999999))

    def test_the_iterm2_pane_of_a_real_process_is_its_unique_id(self):
        # a save reads each session's pane this way, with no Apple Event: on
        # the real kernel the id comes back, and nothing else of the environment
        import sys
        import time
        if sys.executable.startswith(("/usr/bin/", "/bin/", "/System/")):
            self.skipTest("the test's own Python is a system binary: its env is hidden")
        pane = "510D3545-DC2F-46FF-8669-B8CC8B209E6A"
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"],
                                 env={"ITERM_SESSION_ID": f"w6t0p4:{pane}",
                                      "ANTHROPIC_API_KEY": self.TOKEN, "PATH": "/bin"})
        try:
            got = None
            for _ in range(50):             # until it has exec'd: before that the
                got = ccwho.read_iterm_pane(child.pid)       # fork holds OUR env
                if got == pane:
                    break
                time.sleep(0.05)
        finally:
            child.kill()
            child.wait()
        self.assertEqual(got, pane)

    def test_the_iterm2_pane_of_a_pid_that_does_not_exist_is_none(self):   # control
        self.assertIsNone(ccwho.read_iterm_pane(99999999))

    def test_a_system_binary_is_unread_not_unmarked(self):
        # the environment macOS hides is unknown: "no session started it" would
        # drop every Bash tool shell (/bin/zsh) from its session
        import sys
        import time
        if sys.platform != "darwin":
            self.skipTest("macOS hides a system binary's environment; others do not")
        child = subprocess.Popen(["/bin/sleep", "5"],
                                 env={"CLAUDE_CODE_SESSION_ID": "x", "PATH": "/bin"})
        try:
            for _ in range(100):            # until it has exec'd: a fork is Python
                done = subprocess.run(["ps", "-o", "comm=", "-p", str(child.pid)],
                                      capture_output=True, text=True)
                if done.stdout.strip() == "/bin/sleep":
                    break
                time.sleep(0.02)
            else:
                self.fail("the child never exec'd /bin/sleep: nothing was measured")
            got = ccwho.read_procargs(child.pid)
        finally:
            child.kill()
            child.wait()
        self.assertIsNone(got)


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


class TestTheListsReloadCoversEveryRuleModule(unittest.TestCase):
    """The live list re-reads the engine before each scan, so an edit lands in a
    window that stays open for days. Half the rules live in modules the engine
    imports; if only the engine is re-read, a fix to those looks like it did
    nothing. (These were the --watch loop's tests; the list is the reloader now.)"""

    # the engine's own handle on each rule module: `ccwho ls` and the list call
    # through these, so a new module they do not point at is yesterday's code
    HANDLES = {"ccwho_text": "ccwho_text", "ccwho_procs": "procs", "ccwho_terms": "terms",
               "ccwho_brief": "brief", "ccwho_index": "ccwho_index",
               "ccwho_usage": "ccwho_usage"}

    def tearDown(self):
        # a reload re-executes the engine and rebinds the real functions
        install_guards()

    def edit(self, module, text):
        import sys
        path = sys.modules[module].__file__
        original = open(path).read()
        with open(path, "w") as fh:
            fh.write(text(original))
        self.addCleanup(self.put_back, path, original)

    def put_back(self, path, original):
        with open(path, "w") as fh:
            fh.write(original)
        ccwho.reload_all(ccwho)
        install_guards()

    def test_every_rule_module_is_re_read_not_just_the_engine(self):
        import sys
        for mod, handle in self.HANDLES.items():
            with self.subTest(module=mod):
                self.edit(mod, lambda s: s + "\n\ndef _hot_probe():\n    return 1\n")
                ccwho.reload_all(ccwho)
                self.assertTrue(hasattr(sys.modules[mod], "_hot_probe"), mod)
                self.assertTrue(hasattr(getattr(ccwho, handle), "_hot_probe"),
                                f"the engine still holds the old {mod}")

    def test_an_edit_that_blows_up_at_import_leaves_the_old_rules_working(self):
        """A syntax error is the easy case: nothing runs. The one that bites is an
        edit that RUNS and then raises - half the module's new definitions are
        already in place, and catching the error leaves that half live. The next
        scan then extracts with a module that is neither version."""
        self.edit("ccwho_brief", lambda s: s.replace(
            "MAX_PROGRESS = 5", 'MAX_PROGRESS = 5\nLOW_SIGNAL = set()\nraise RuntimeError("boom")'))
        with self.assertRaises(RuntimeError):
            ccwho.reload_all(ccwho)
        self.assertTrue(ccwho.brief.LOW_SIGNAL,
                        "the half-applied edit must not become the live rule set")
        self.assertFalse(ccwho.brief.is_substantive("continue"),
                         "extraction still works, with the last good rules")


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
        # named, so a person sees who holds it without a guess in the exit code
        self.assertEqual(fleet["unknown_holders"], {8080: [{"pid": 40, "name": "python3"}]})

    def test_a_dead_reader_makes_the_row_stuck(self):
        # the 2026-09-26 shape: a heredoc command's `cat` reads the claude's socket
        ccwho.ps_snapshot = lambda: self.PS + (
            "  50    10 /bin/zsh -c source /u/.claude/shell-snapshots/s.sh && eval 'x'\n"
            "  51    50 cat\n")
        # ps -o comm is the executable: the row's name since review 1 of 2026-10-07
        ccwho.ps_table = lambda: {pid: (START, {10: "claude", 51: "/bin/cat"}.get(pid, "node"))
                                  for pid in (10, 11, 12, 13, 20, 30, 40, 41, 50, 51)}
        ccwho.stdin_sockets = lambda pids: {10: {"16": ("0xa", "->0xb")},
                                           51: {"0": ("0xb", "->0xa")}}
        rows, _ = self.collect()
        self.assertEqual(rows[0]["dead_loops"], [{"pid": 51, "tasks": [], "kind": "reader",
                                                  "program": "cat", "root": 50,
                                                  "start": START}])
        self.assertEqual(rows[0]["attention"], "stuck")

    def test_a_stack_proven_once_is_not_sampled_by_the_next_scan(self):
        # ccwho ls is a new process each time: what one proved reaches the next
        ccwho.ps_snapshot = lambda: self.PS + (
            "  50    10 /bin/zsh -c source /u/.claude/shell-snapshots/s.sh && eval 'x'\n"
            "  51    50 python3 job.py\n")
        ccwho.ps_table = lambda: {pid: (START, "claude" if pid == 10 else "node")
                                  for pid in (10, 11, 12, 13, 20, 30, 40, 41, 50, 51)}
        ccwho.stdin_sockets = lambda pids: {10: {"16": ("0xa", "->0xb")},
                                           51: {"0": ("0xb", "->0xa")}}
        testkit.patch(self, ccwho, "open_fds", lambda pids: {51: {"0": ("u", "unix", "->0xa")}})
        samples = []
        testkit.patch(self, ccwho, "stack_sample", lambda pid: samples.append(pid) or True)
        first, _ = ccwho.collect(cache={})
        again, _ = ccwho.collect(cache={})
        self.assertEqual([[d["pid"] for d in rows[0]["dead_loops"]] for rows in (first, again)],
                         [[51], [51]])
        self.assertEqual(samples, [51])

    def test_the_file_is_read_once_for_a_cache(self):
        # the list's cache knows more than the file: a "no" a kill handed to it
        # is the newest word, and reading the file again would undo it (review 4)
        ccwho.ps_snapshot = lambda: self.PS + (
            "  50    10 /bin/zsh -c source /u/.claude/shell-snapshots/s.sh && eval 'x'\n"
            "  51    50 python3 job.py\n")
        ccwho.ps_table = lambda: {pid: (START, "claude" if pid == 10 else "node")
                                  for pid in (10, 11, 12, 13, 20, 30, 40, 41, 50, 51)}
        ccwho.stdin_sockets = lambda pids: {10: {"16": ("0xa", "->0xb")},
                                           51: {"0": ("0xb", "->0xa")}}
        testkit.patch(self, ccwho, "open_fds", lambda pids: {51: {"0": ("u", "unix", "->0xa")}})
        testkit.patch(self, ccwho, "stack_sample", lambda pid: True)
        cache = {}
        ccwho.collect(cache=cache)                     # found, and kept in the file as a yes
        key = ccwho._verdict_key(10, 51, START, "python3 job.py")
        cache["_stacks"][key] = [False, time.time()]   # what a kill then saw
        testkit.patch(self, ccwho, "stack_sample", lambda pid: self.fail("sampled"))
        rows, _ = ccwho.collect(cache=cache)
        self.assertEqual((rows[0]["dead_loops"], cache["_stacks"][key][0]), ([], False))

    def test_a_reader_on_a_pipe_leaves_the_row_alone(self):                # control
        ccwho.ps_snapshot = lambda: self.PS + "  50    10 /bin/zsh -c x\n  51    50 cat\n"
        ccwho.ps_table = lambda: {pid: (START, "claude" if pid == 10 else "node")
                                  for pid in (10, 11, 12, 13, 20, 30, 40, 41, 50, 51)}
        ccwho.stdin_sockets = lambda pids: {10: {"16": ("0xa", "->0xb")},
                                           51: {"0": ("0xc", "->0xd")}}
        rows, _ = self.collect()
        self.assertEqual((rows[0]["dead_loops"], rows[0]["attention"]), ([], "running"))

    def test_a_holder_the_tree_gives_a_session_is_not_unknown(self):
        # `/usr/bin/nc -l 8081` in a tool shell: unread, and still the session's
        # port - never also "unknown". 40, unread and nobody's, still is (control)
        real = ccwho.read_procargs
        ccwho.read_procargs = lambda pid: None if pid in (14, 40) else real(pid)
        ccwho.ps_snapshot = lambda: self.PS + "  14    10 /usr/bin/nc -l 8081\n"
        ccwho.listen_ports = lambda: {14: [8081], 40: [8080]}
        rows, fleet = self.collect()
        self.assertEqual(rows[0]["ports"], [8081])
        self.assertEqual(fleet["unknown_ports"], [8080])
        self.assertEqual(sorted(fleet["unknown_holders"]), [8080])

    def test_a_holder_in_an_app_is_named_by_the_app(self):
        # a path with spaces: its first word ("Visual") names nothing
        real = ccwho.read_procargs
        ccwho.read_procargs = lambda pid: None if pid == 42 else real(pid)
        ccwho.ps_snapshot = lambda: self.PS + (
            "  42     1 /Applications/Visual Studio Code.app/Contents/MacOS/Electron\n")
        ccwho.listen_ports = lambda: {42: [8082]}
        _, fleet = self.collect()
        self.assertEqual(fleet["unknown_holders"], {8082: [{"pid": 42,
                                                           "name": "Visual Studio Code"}]})

    def test_a_port_whose_holder_was_read_is_known(self):             # control
        ccwho.listen_ports = lambda: {12: [5173], 40: [8080]}
        _, fleet = self.collect()
        self.assertEqual(fleet["unknown_ports"], [])
        self.assertEqual(fleet["unknown_holders"], {})

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


class TestTheTableNamesEachSession(unittest.TestCase):
    """The name on a row is the one you message the session by (`ccwho-b5`), as
    the live list shows it. A table that shows only the project made a person
    ask which row `ccwho-b5` was - and made the agent they asked miss it."""

    def row(self, **kw):
        base = {"project": "ccwho", "status": "idle", "attention": "stopped",
                "name": "ccwho-b5", "title": "Session 51fd recovery", "doing": "",
                "since": "1m", "tty": "ttys039", "orphans": 0, "sessionId": LIVE}
        base.update(kw)
        return base

    def line(self, rows, width=200):
        out = ccwho.render(rows, {}, color=False, width=width)
        return [l for l in out.splitlines() if "s039" in l]

    def test_the_row_leads_with_its_name(self):
        self.assertTrue(self.line([self.row()])[0].startswith("ccwho-b5  "))

    def test_a_row_with_no_name_says_its_project(self):                # control
        self.assertTrue(self.line([self.row(name="")])[0].startswith("ccwho  "))

    def test_a_renamed_session_still_says_its_project(self):
        # "update-landing-page-faq" does not say it is marketing's
        line = self.line([self.row(name="update-landing-page-faq", project="marketing")])[0]
        self.assertIn("marketing", line)

    def test_an_auto_name_does_not_say_its_project_twice(self):        # control
        self.assertEqual(self.line([self.row()])[0].count("ccwho"), 1)

    def test_a_long_name_is_never_cut(self):
        # a cut name is no address: nothing can be sent to "update-landing-pa…"
        long = "update-landing-page-whatsapp-faq"
        lines = self.line([self.row(name=long, project="marketing"), self.row()], width=120)
        self.assertTrue(lines[0].startswith(long + "  "), lines)
        self.assertTrue(all(len(l) <= 120 for l in lines), lines)

    def test_short_names_keep_their_columns_in_line(self):
        lines = self.line([self.row(name="app-1", project="app"), self.row()])
        self.assertEqual(lines[0].index("s039"), lines[1].index("s039"))


class TestAPickListNamesEachSession(unittest.TestCase):
    """`ccwho show x` and `ccwho open x` list the sessions x matched. Each line
    says the name that matched, or nobody can see why a row is on the list."""

    def test_the_line_has_the_name_the_id_and_the_tty(self):
        row = {"name": "ccwho-b5", "title": "Session 51fd recovery", "tty": "ttys039",
               "sessionId": "cff47f0d-8106-4972-a5f8-5287cf808347", "project": "ccwho"}
        line = ccwho.pick_line(row)
        for part in ("ccwho-b5", "cff4", "s039", "Session 51fd recovery"):
            self.assertIn(part, line)

    def test_a_row_with_no_name_has_its_project(self):                 # control
        line = ccwho.pick_line({"name": "", "title": "t", "tty": "", "sessionId": "",
                                "project": "app"})
        self.assertIn("app", line)

    def test_the_lines_of_one_list_stay_in_line(self):
        rows = [{"name": "ccwho-b5", "tty": "ttys039", "sessionId": "cff4", "title": "a"},
                {"name": "", "project": "app", "tty": "ttys007", "sessionId": "7d19",
                 "title": "b"}]
        lines = ccwho.pick_lines(rows)
        self.assertEqual(lines[0].index("cff4"), lines[1].index("7d19"), lines)


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

    def test_not_sure_does_not_say_the_session_is_gone(self):
        # what runs under a `claude -p` in a live session is "not sure" too:
        # its session is not gone
        text = ccwho.render_ps_screen(ccwho.ps_listing([self.ROW], self.FLEET), self.FLEET,
                                      width=100)
        line = next(l for l in text.splitlines() if "NOT SURE" in l)
        self.assertNotIn("gone", line)

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
            first, _ = ccwho.ui_row_cells(row, width=w, tag="ant:1a2b")
            # last of the words; only the › that opens the detail comes after it
            self.assertEqual(first[-3], (" · ant:1a2b", "account"))
            self.assertEqual([r for _, r in first[-2:]], ["pad", "detail"])
            self.assertEqual(first[0][1], "mark")
            self.assertLessEqual(sum(ccwho._cells(t) for t, _ in first), w)
            plain, _ = ccwho.ui_row_cells(row, width=w)
            self.assertEqual(first[:3], plain[:3])       # mark, id, project untouched

    def test_a_row_too_narrow_for_the_brand_keeps_the_account(self):
        # where " · lukaso" fits, the row says it - " · ant:lukaso" or " · lukaso"
        has = (lambda w, tag: [t for t, role in ccwho.ui_row_cells(self.rows[0], width=w,
                                                                   tag=tag)[0]
                               if role == "account"])
        seen_short = False
        for w in range(30, 100):
            with self.subTest(width=w):
                short, full = has(w, "lukaso"), has(w, "ant:lukaso")
                self.assertIn(full, ([], [" · ant:lukaso"], [" · lukaso"]))
                if short:
                    self.assertNotEqual(full, [])
                seen_short |= full == [" · lukaso"]
        self.assertTrue(seen_short)

    def test_the_brand_gives_way_only_where_that_buys_something(self):
        # the short form only for a title of 8+ cells, or for the account
        # itself: never "· lukaso" where "· ant:lukaso" fits and no title shows
        account = (lambda w, row, tag: [t for t, role in ccwho.ui_row_cells(row, width=w,
                                                                            tag=tag)[0]
                                        if role == "account"])
        name = (lambda w, row, tag: "".join(t for t, role in ccwho.ui_row_cells(
            row, width=w, tag=tag)[0] if role == "name"))
        rows = list(self.rows) + [dict(r, kind="background", windowed=False)
                                  for r in self.rows]
        # no title (it only says the name again): a switch buys it nothing -
        # with ports or without: the ports give way to the account, not its brand
        rows += [dict(r, tab_title="", title="", ports=ports)
                 for r in self.rows for ports in (None, [3000])]
        rows += [dict(r, tab_title="", title="fix") for r in self.rows]    # control
        switched_for_fix = False
        for row in rows:
            for w in range(30, 121):
                if account(w, row, "ant:lukaso") != [" · lukaso"]:
                    continue
                with self.subTest(width=w, row=row.get("sessionId")):
                    # a tag as long as the full one, with no short form
                    full = "antXlukaso"
                    if name(w, row, "ant:lukaso"):
                        self.assertEqual(name(w, row, full), "")   # it bought the title
                        switched_for_fix |= row.get("title") == "fix"
                    else:
                        self.assertEqual(account(w, row, full), [])  # it kept the account
        self.assertTrue(switched_for_fix)

    def test_a_long_name_gives_way_and_the_tag_stays_whole(self):
        row = dict(self.rows[0], tab_title="a very long tab title " * 6)
        for w in (60, 100):
            first, _ = ccwho.ui_row_cells(row, width=w, tag="ant:lukaso@gmail")
            # whole - its brand gives way before the title is cut to nothing
            self.assertIn(first[-3], [(" · ant:lukaso@gmail", "account"),
                                      (" · lukaso@gmail", "account")])
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
        self.assertTrue(lines[1].startswith("usage  ant:lukaso 5h 42%"), lines[1])
        body = [l for l in lines if l.startswith(("liveapp", "ccwho", "marketing"))]
        before = [l for l in self.expected["render"]["150"].splitlines()
                  if l.startswith(("liveapp", "ccwho", "marketing"))]
        # the tag follows the age column; the doing column gives up its width,
        # so a row is exactly as wide as it was
        for row, line, old, tag in zip(self.rows, body, before, ("ant:lukaso", "?", "ant:1a2b")):
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
        self.assertIn(f"{rows[0]['since']:>5}  ant:q", out)
        self.assertIn(f"{rows[2]['since']:>5}  ant:r", out)
        self.assertIn(f"{rows[1]['since']:>5}  ?", out)           # no reading: one cell

    def test_a_narrow_column_drops_the_brand_before_the_name(self):
        # "ant:cli…" twice names no one: the brand goes first, as on the line
        TestRowsWithUsage.setUpClass()
        rows = TestRowsWithUsage.rows
        accts = [{"id": a, "kind": "login", "email": "", "brand": "ant", "label": "",
                  "sessions": 1, "age": 1,
                  "five_hour": {"state": "ok", "pct": 1, "resets_at": None, "age": 1},
                  "seven_day": None} for a in ("q", "r")]
        snap = {"state": "ok", "accounts": accts, "names": {"q": "client-a", "r": "client-b"},
                "sessions": {rows[0]["sessionId"]: "q", rows[2]["sessionId"]: "r"}, "now": 0}
        for width in range(70, 151, 4):
            out = ccwho.render(rows, {"usage": snap}, color=False, width=width)
            body = out.split("\n\n", 1)[1]
            with self.subTest(width=width):
                self.assertNotIn("ant:cli", body.replace("ant:client-a", "").replace(
                    "ant:client-b", ""))
                if "client-a" in body or "client-b" in body:
                    self.assertIn("client-a", body)
                    self.assertIn("client-b", body)

    def test_one_column_says_the_brand_on_every_tag_or_on_none(self):
        # "lukaso" over "ant:d0a0" reads as two kinds of thing
        TestRowsWithUsage.setUpClass()
        rows = TestRowsWithUsage.rows
        accts = [{"id": a, "kind": "login", "email": "", "brand": "ant", "label": "",
                  "sessions": 1, "age": 1,
                  "five_hour": {"state": "ok", "pct": 1, "resets_at": None, "age": 1},
                  "seven_day": None} for a in ("q", "r")]
        snap = {"state": "ok", "accounts": accts, "names": {"q": "lukaso", "r": "d0a0"},
                "sessions": {rows[0]["sessionId"]: "q", rows[2]["sessionId"]: "r"}, "now": 0}
        seen = set()
        for width in range(60, 151, 2):
            body = ccwho.render(rows, {"usage": snap}, color=False,
                                width=width).split("\n\n", 1)[1]
            with self.subTest(width=width):
                self.assertIn(body.count("ant:"), (0, 2), body)
                seen.add(body.count("ant:"))
        self.assertEqual(seen, {0, 2})          # both happen, never one of each

    def test_a_tag_fits_its_cells_brand_first(self):
        self.assertEqual(ccwho._fit_tag("ant:client-a", 12), "ant:client-a")
        self.assertEqual(ccwho._fit_tag("ant:client-a", 10), "client-a")
        self.assertEqual(ccwho._fit_tag("ant:client-a", 6), "clien\u2026")
        self.assertEqual(ccwho._fit_tag("?", 3), "?")


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



def procs(*rows):
    """`ps -eo pid,tty,uid,ucomm`: the kernel's name for the executable, which a
    process cannot set for itself (argv[0], `comm`, it can)."""
    return "  PID TTY        UID UCOMM\n" + "\n".join(
        f"{p} {t} {u} {c}" for p, t, u, c in rows) + "\n"


def apps(*rows):
    """`ps -eo pid,uid,ucomm`: the cheap table, no tty column (0.03 s, not 0.2)."""
    return "  PID   UID UCOMM\n" + "\n".join(f"{p} {u} {c}" for p, u, c in rows) + "\n"


def ps(*rows):
    """`ps -eo pid,ppid,command`."""
    return "  PID  PPID COMMAND\n" + "\n".join(f"{p} {pp} {c}" for p, pp, c in rows) + "\n"


ME = os.getuid()
APP = "/Applications/iTerm.app/Contents/MacOS/iTerm2"
IT = "iTerm2"                          # its ucomm
SERVER = "iTermServer-3.7."            # ucomm keeps 16 characters: iTermServer-3.7.2
DAEMON_CMD = ("/Users/x/Library/Application Support/iTerm2/iTermServer-3.7.2 "
              "/Users/x/Library/Application Support/iTerm2/iterm2-daemon-1.socket")
LOGIN = "/usr/bin/login -fpl x /Applications/iTerm.app/Contents/MacOS/ShellLauncher --launch_shell"


class TestTheTerminalsITerm2ShowsComeFromPs(unittest.TestCase):
    """Which terminals are iTerm2 windows used to be ASKED of iTerm2, several
    times a minute. On 2026-09-29 those asks, each leaving a handler thread stuck
    inside a wedged iTerm2, used up its 512 worker threads and froze it. ps
    already knows: every pane's first process is started by iTerm2's session
    daemon (iTermServer-*), or by iTerm2 itself without the daemon.

    Identified by the kernel's name for the executable (`ucomm`), never by a
    command line or argv[0]: `exec -a iTerm2 sleep` is sleep."""

    def test_a_pane_started_by_the_session_daemon_is_an_iterm2_terminal(self):
        p = procs((100, "??", ME, IT), (200, "??", ME, SERVER),
                  (300, "ttys024", 0, "login"), (301, "ttys024", ME, "zsh"))
        out = ps((100, 1, APP), (200, 1, DAEMON_CMD), (300, 200, LOGIN), (301, 300, "-zsh"))
        self.assertEqual(ccwho.terms.ITERM2.ttys(out, p), {"ttys024"})

    def test_a_pane_started_by_iterm2_itself_counts_too(self):
        # no daemon ("restore sessions" off): iTerm2 is the parent
        p = procs((100, "??", ME, IT), (300, "ttys007", 0, "login"))
        out = ps((100, 1, APP), (300, 100, "/usr/bin/login -fpq x /bin/bash -c make"))
        self.assertEqual(ccwho.terms.ITERM2.ttys(out, p), {"ttys007"})

    def test_the_bundle_name_does_not_matter(self):
        # iTerm2.app, iTerm.app: the executable is iTerm2 either way
        p = procs((100, "??", ME, IT), (300, "ttys007", 0, "login"))
        out = ps((100, 1, "/Applications/iTerm2.app/Contents/MacOS/iTerm2"), (300, 100, LOGIN))
        self.assertEqual(ccwho.terms.ITERM2.ttys(out, p), {"ttys007"})

    def test_a_beta_daemon_is_still_the_daemon(self):
        p = procs((100, "??", ME, IT), (200, "??", ME, "iTermServer-3.6."),
                  (300, "ttys024", 0, "login"))
        out = ps((100, 1, APP), (200, 1, "/x/iTermServer-3.6.0beta2 /sock"), (300, 200, LOGIN))
        self.assertEqual(ccwho.terms.ITERM2.ttys(out, p), {"ttys024"})

    def test_a_terminal_app_window_is_not_one(self):                    # control
        p = procs((100, "??", ME, IT), (200, "??", ME, SERVER),
                  (300, "ttys024", 0, "login"),
                  (400, "??", ME, "Terminal"), (401, "ttys050", 0, "login"))
        out = ps((100, 1, APP), (200, 1, DAEMON_CMD), (300, 200, LOGIN),
                 (400, 1, "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"),
                 (401, 400, "login -pf x"))
        self.assertEqual(ccwho.terms.ITERM2.ttys(out, p), {"ttys024"})

    def test_a_process_calling_itself_iterm2_is_not_iterm2(self):
        # `exec -a /x/MacOS/iTerm2 sleep 99`: argv[0] and comm say iTerm2, the
        # kernel says sleep. It must not become the app, nor its children panes
        p = procs((50, "??", ME, "sleep"), (51, "ttys009", ME, "cat"), (100, "??", ME, IT))
        out = ps((50, 1, APP), (51, 50, "cat"), (100, 1, APP))
        self.assertEqual(ccwho.terms.ITERM2.ttys(out, p), set())
        self.assertEqual(ccwho.terms.ITERM2.pid(p), 100)

    def test_another_users_iterm2_is_not_ours(self):
        self.assertIsNone(ccwho.terms.ITERM2.pid(procs((100, "??", ME + 1, IT))))

    def test_another_users_daemon_does_not_make_our_windows(self):
        p = procs((100, "??", ME, IT), (200, "??", ME + 1, SERVER),
                  (300, "ttys050", 0, "login"))
        out = ps((100, 1, APP), (200, 1, DAEMON_CMD), (300, 200, LOGIN))
        self.assertEqual(ccwho.terms.ITERM2.ttys(out, p), set())
        mine = procs((100, "??", ME, IT), (200, "??", ME, SERVER), (300, "ttys050", 0, "login"))
        self.assertEqual(ccwho.terms.ITERM2.ttys(out, mine), {"ttys050"})           # control

    def test_a_process_newer_than_the_table_is_skipped(self):
        # collect() takes the two ps snapshots one after the other: a pane
        # started in between is in one and not the other
        p = procs((100, "??", ME, IT), (200, "??", ME, SERVER), (300, "ttys024", 0, "login"))
        out = ps((100, 1, APP), (200, 1, DAEMON_CMD), (300, 200, LOGIN), (305, 200, LOGIN))
        self.assertEqual(ccwho.terms.ITERM2.ttys(out, p), {"ttys024"})

    def test_the_cheap_table_finds_the_app_too(self):
        self.assertEqual(ccwho.terms.ITERM2.pid(apps((1, 0, "launchd"), (100, ME, IT))), 100)

    def test_shells_kept_alive_after_iterm2_quit_are_not_windows(self):
        # measured 2026-09-29: after `kill -9` of iTerm2 the daemon kept all 28
        # shells, same parent - and not one window existed
        p = procs((200, "??", ME, SERVER), (300, "ttys024", 0, "login"))
        out = ps((200, 1, DAEMON_CMD), (300, 200, LOGIN))
        self.assertIsNone(ccwho.terms.ITERM2.ttys(out, p))

    def test_no_iterm2_at_all_is_not_knowing(self):
        self.assertIsNone(ccwho.terms.ITERM2.ttys(ps((1, 0, "/sbin/launchd")),
                                           procs((1, "??", 0, "launchd"))))

    def test_iterm2_running_with_no_panes_is_an_empty_answer(self):     # control
        self.assertEqual(ccwho.terms.ITERM2.ttys(ps((100, 1, APP)), procs((100, "??", ME, IT))), set())


class TestCollectTakesWindowsFromPsNotFromTabNames(MachinelessCollect):
    """The row's windowed flag must not depend on iTerm2 answering an Apple
    Event: that answer is what goes missing when iTerm2 is slow."""

    def setUp(self):
        super().setUp()
        real = ccwho.agents_json
        self.addCleanup(setattr, ccwho, "agents_json", real)
        ccwho.agents_json = lambda: json.dumps(
            [{"sessionId": "aaa", "pid": 301, "cwd": "/x", "status": "idle"}])

    def row(self, ps_rows, proc_rows):
        ccwho.ps_snapshot = lambda: ps(*ps_rows)
        ccwho.tty_snapshot = lambda: procs(*proc_rows)
        rows, _ = ccwho.collect(cache={})
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_a_session_in_an_iterm2_pane_has_a_window_with_no_tab_names(self):
        row = self.row([(100, 1, APP), (200, 1, DAEMON_CMD), (300, 200, LOGIN), (301, 300, "claude")],
                       [(100, "??", ME, IT), (200, "??", ME, SERVER),
                        (300, "ttys024", 0, "login"), (301, "ttys024", ME, "claude")])
        self.assertIs(row["windowed"], True)

    def test_a_session_on_a_terminal_iterm2_does_not_show_has_none(self):  # control
        row = self.row([(100, 1, APP), (200, 1, DAEMON_CMD), (300, 200, LOGIN), (301, 1, "claude")],
                       [(100, "??", ME, IT), (200, "??", ME, SERVER),
                        (300, "ttys024", 0, "login"), (301, "ttys042", ME, "claude")])
        self.assertIs(row["windowed"], False)

    def test_shells_the_daemon_kept_after_iterm2_quit_have_no_window(self):
        # the daemon keeps every shell, same parent, after iTerm2 quits
        row = self.row([(200, 1, DAEMON_CMD), (300, 200, LOGIN), (301, 300, "claude")],
                       [(200, "??", ME, SERVER), (300, "ttys024", 0, "login"),
                        (301, "ttys024", ME, "claude")])
        self.assertIsNot(row["windowed"], True)

    def test_without_iterm2_running_it_is_not_known(self):
        row = self.row([(301, 1, "claude")], [(301, "ttys042", ME, "claude")])
        self.assertIsNone(row["windowed"])

    def test_a_scan_runs_ps_once_even_when_iterm2_is_not_asked(self):
        # the gate reads the executable table collect() already took: a refused
        # ask used to fork a second ps on every tick
        seen = []
        ccwho.terms.ITERM2.ask = lambda args, timeout=5.0, **k: seen.append(k.get("procs")) or None
        self.addCleanup(setattr, ccwho.terms.ITERM2, "ask", GUARDS["iterm_ask"])
        ccwho.terms.ITERM2.__dict__.pop("titles", None)    # the class's own: the real one
        table = [(100, "??", ME, IT), (301, "ttys024", ME, "claude")]
        taken = []
        ccwho.tty_snapshot = lambda: taken.append(1) or procs(*table)
        ccwho.ps_snapshot = lambda: ps((100, 1, APP), (301, 1, "claude"))
        ccwho.collect(cache={})
        self.assertEqual(len(taken), 1)
        self.assertEqual(seen, [procs(*table)], "the gate got the table collect() took")


TERMINAL_APP = "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"
TAB = [(400, 1, TERMINAL_APP), (401, 400, "login -pf x")]            # a Terminal.app tab: ps
TAB_PROCS = [(400, "??", ME, "Terminal"), (401, "ttys050", 0, "login")]   # and its ucomm table
ITERM2_PANE = [(100, 1, APP), (200, 1, DAEMON_CMD), (300, 200, LOGIN)]
ITERM2_PANE_PROCS = [(100, "??", ME, IT), (200, "??", ME, SERVER), (300, "ttys024", 0, "login")]


class TestTerminalAppTabsHaveWindowsToo(MachinelessCollect):
    """A session in a Terminal.app tab has a window to go to, with iTerm2
    running or not - and an app that cannot tell never turns another app's
    rows into "no window" (review O3: availability is per app, never a union
    of what the apps show)."""

    def setUp(self):
        super().setUp()
        real = ccwho.agents_json
        self.addCleanup(setattr, ccwho, "agents_json", real)
        ccwho.agents_json = lambda: json.dumps(
            [{"sessionId": "aaa", "pid": 301, "cwd": "/x", "status": "idle"}])

    def row(self, ps_rows, proc_rows):
        ccwho.ps_snapshot = lambda: ps(*ps_rows)
        ccwho.tty_snapshot = lambda: procs(*proc_rows)
        rows, _ = ccwho.collect(cache={})
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_with_iterm2_closed(self):
        row = self.row(TAB + [(301, 401, "claude")], TAB_PROCS + [(301, "ttys050", ME, "claude")])
        self.assertIs(row["windowed"], True)
        self.assertEqual(row["tty"], "ttys050")

    def test_beside_iterm2(self):
        row = self.row(ITERM2_PANE + TAB + [(301, 401, "claude")],
                       ITERM2_PANE_PROCS + TAB_PROCS + [(301, "ttys050", ME, "claude")])
        self.assertIs(row["windowed"], True)

    def test_iterm2_s_kept_shells_stay_unknown_while_terminal_app_runs(self):
        # iTerm2 quit, its daemon keeps the shells: nothing is known about their
        # windows - Terminal.app answering for its own tabs changes nothing there
        row = self.row([(200, 1, DAEMON_CMD), (300, 200, LOGIN), (301, 300, "claude")] + TAB,
                       [(200, "??", ME, SERVER), (300, "ttys024", 0, "login"),
                        (301, "ttys024", ME, "claude")] + TAB_PROCS)
        self.assertIsNone(row["windowed"])

    def test_a_tty_no_app_shows_has_no_window_while_one_app_can_tell(self):
        row = self.row(TAB + [(301, 1, "claude")], TAB_PROCS + [(301, "ttys042", ME, "claude")])
        self.assertIs(row["windowed"], False)

    # an app that runs with no window cannot tell: nothing is known - the rule
    # main keeps for iTerm2 with no pane, now for every app (review P2-1)
    def test_terminal_app_running_with_no_window_cannot_tell(self):
        row = self.row([(400, 1, TERMINAL_APP), (301, 1, "claude")],
                       [(400, "??", ME, "Terminal"), (301, "ttys042", ME, "claude")])
        self.assertIsNone(row["windowed"])

    def test_iterm2_running_with_no_pane_cannot_tell(self):
        row = self.row([(100, 1, APP), (301, 1, "claude")],
                       [(100, "??", ME, IT), (301, "ttys042", ME, "claude")])
        self.assertIsNone(row["windowed"])


class TestARowSaysWhichAppShowsIt(TestTerminalAppTabsHaveWindowsToo):
    """Every row records the app whose tab shows it: a jump goes there, and a
    save keeps it for the restore (D6)."""

    test_with_iterm2_closed = test_beside_iterm2 = None
    test_iterm2_s_kept_shells_stay_unknown_while_terminal_app_runs = None
    test_a_tty_no_app_shows_has_no_window_while_one_app_can_tell = None
    test_terminal_app_running_with_no_window_cannot_tell = None
    test_iterm2_running_with_no_pane_cannot_tell = None

    def test_a_terminal_app_tab(self):
        row = self.row(TAB + [(301, 401, "claude")], TAB_PROCS + [(301, "ttys050", ME, "claude")])
        self.assertEqual(row["terminal"], "terminal")

    def test_an_iterm2_pane(self):
        row = self.row(ITERM2_PANE + [(301, 300, "claude")],
                       ITERM2_PANE_PROCS + [(301, "ttys024", ME, "claude")])
        self.assertEqual(row["terminal"], "iterm2")

    def test_a_shell_iterm2_s_daemon_kept_is_still_iterm2_s(self):
        row = self.row([(200, 1, DAEMON_CMD), (300, 200, LOGIN), (301, 300, "claude")],
                       [(200, "??", ME, SERVER), (300, "ttys024", 0, "login"),
                        (301, "ttys024", ME, "claude")])
        self.assertEqual(row["terminal"], "iterm2")

    def test_a_tty_no_app_shows(self):                                              # control
        row = self.row(TAB + [(301, 1, "claude")], TAB_PROCS + [(301, "ttys042", ME, "claude")])
        self.assertEqual(row["terminal"], "")

    def test_a_save_keeps_it(self):
        row = self.row(TAB + [(301, 401, "claude")], TAB_PROCS + [(301, "ttys050", ME, "claude")])
        man = ccwho.manifest_from_rows([dict(row, cwd="/x")])
        self.assertEqual(man["sessions"][0]["terminal"], "terminal")


class TestTerminalAppIsAskedForTabNamesOnlyForItsSessions(MachinelessCollect):
    """The first Apple Event to Terminal.app makes macOS ask whether ccwho may
    control it. So the background asks it for tab names only while a session
    sits in one of its tabs (D7): open Terminal.app for anything else, and no
    prompt appears, and no event is sent."""

    def setUp(self):
        super().setUp()
        real = ccwho.agents_json
        self.addCleanup(setattr, ccwho, "agents_json", real)
        ccwho.agents_json = lambda: json.dumps(
            [{"sessionId": "aaa", "pid": 301, "cwd": "/x", "status": "idle"}])
        self.asked = []
        ccwho.terms.TERMINAL.titles = lambda timeout=5.0, **k: (
            self.asked.append(k) or {"ttys050": "\u2733 fixing it"})
        self.addCleanup(ccwho.terms.TERMINAL.__dict__.pop, "titles", None)

    def collect(self, ps_rows, proc_rows):
        ccwho.ps_snapshot = lambda: ps(*ps_rows)
        ccwho.tty_snapshot = lambda: procs(*proc_rows)
        rows, _ = ccwho.collect(cache={})
        return rows

    def test_no_session_in_its_tabs_no_ask(self):
        self.collect(ITERM2_PANE + TAB + [(301, 300, "claude")],
                     ITERM2_PANE_PROCS + TAB_PROCS + [(301, "ttys024", ME, "claude")])
        self.assertEqual(self.asked, [])

    def test_a_session_in_its_tab_asks_once_and_names_the_row(self):             # control
        rows = self.collect(TAB + [(301, 401, "claude")], TAB_PROCS + [(301, "ttys050", ME, "claude")])
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(rows[0]["tab_title"], "\u2733 fixing it")

    def test_a_scan_that_asks_no_session_app_asks_it_nothing(self):
        # the autosave job's (save): no Terminal.app prompt from launchd's
        # python every 15 minutes (review 2 of slices 4-5)
        ccwho.ps_snapshot = lambda: ps(*TAB, (301, 401, "claude"))
        ccwho.tty_snapshot = lambda: procs(*TAB_PROCS, (301, "ttys050", ME, "claude"))
        rows, _ = ccwho.collect(cache={}, session_apps=False)
        self.assertEqual(self.asked, [])
        self.assertEqual((rows[0]["tty"], rows[0]["terminal"]), ("ttys050", "terminal"))

    # its tab is the one showing the session, not the session's own tty: the
    # daemon's bg-spare sits on a tty no window shows (review of slice 2)
    def assert_shown_in_its_tab(self, rows):
        self.assertEqual(len(self.asked), 1)
        self.assertEqual((rows[0]["tty"], rows[0]["windowed"], rows[0]["tab_title"]),
                         ("ttys050", True, "\u2733 fixing it"))

    def test_a_session_attached_in_its_tab(self):
        self.assert_shown_in_its_tab(self.collect(
            TAB + [(301, 1, "claude bg-spare"), (402, 401, "claude attach aaa")],
            TAB_PROCS + [(301, "ttys042", ME, "claude"), (402, "ttys050", ME, "claude")]))

    def test_a_session_its_tab_resumed_under_the_daemon(self):
        self.assert_shown_in_its_tab(self.collect(
            TAB + [(402, 401, "claude --resume"), (403, 402, "claude daemon run"),
                   (301, 403, "claude bg-spare")],
            TAB_PROCS + [(402, "ttys050", ME, "claude"), (403, "", ME, "claude"),
                         (301, "ttys042", ME, "claude")]))


class TestBackgroundAsksNeverPileUpInITerm2(unittest.TestCase):
    """The gate every background Apple Event to iTerm2 goes through.

    2026-09-29: ccwho killed osascript after 5 s, but a killed sender does not
    cancel its event - iTerm2 kept a handler thread stuck for each one, and 509
    of them filled its 512-thread pool and froze it. So: one event in flight at
    a time, the lock held by the osascript child itself until it exits, never
    killed; and an event that timed out inside iTerm2 (-1712) stops every
    background ask to that iTerm2 for a while (TestAStuckITerm2IsAskedAgainLater),
    or until it restarts."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.calls = os.path.join(self.tmp, "calls")
        terms = ccwho.terms             # put back on the module they were taken from
        saved = (terms.STATE_DIR, terms.OSASCRIPT)
        self.addCleanup(lambda: (setattr(terms, "STATE_DIR", saved[0]),
                                 setattr(terms, "OSASCRIPT", saved[1])))
        ccwho.terms.STATE_DIR = os.path.join(self.tmp, "state")
        ccwho.terms.ITERM2.ask = REAL["iterm_ask"]           # the real gate, against a stub
        self.addCleanup(setattr, ccwho.terms.ITERM2, "ask", GUARDS["iterm_ask"])

    def stub(self, body):
        """An osascript that logs each launch, then does `body`."""
        path = os.path.join(self.tmp, "osascript")
        with open(path, "w") as f:
            f.write(f"#!/bin/sh\necho x >> {shlex.quote(self.calls)}\n{body}\n")
        os.chmod(path, 0o755)
        ccwho.terms.OSASCRIPT = path

    def launches(self):
        try:
            with open(self.calls) as f:
                return len(f.read().split())
        except FileNotFoundError:
            return 0

    def table(self, pid=100):
        return apps((pid, ME, IT))

    def state(self, name):
        return os.path.join(ccwho.terms.STATE_DIR, "iterm-ae." + name)

    def ask(self, pid=100, timeout=10.0, now=None, **kw):
        # 10 s: an answer must come back even on a machine at load 20+ (seen
        # 2026-09-29); the asks meant to time out pass their own small timeout
        return ccwho.terms.ITERM2.ask(["-e", "whatever"], timeout=timeout,
                               procs=self.table(pid), now=now, **kw)

    def until(self, what, seconds=20.0):
        """Poll rather than sleep a guessed time: loaded machines are slow (a
        5 s limit failed once at load 22, 2026-09-29)."""
        end = time.time() + seconds
        while time.time() < end:
            if what():
                return True
            time.sleep(0.05)
        return False

    def ended(self):
        """The ask ran to its end: its status is written AND its wrapper, which
        writes the status and then exits, has let go of the lock."""
        def free():
            if not os.path.exists(self.state("status")):
                return False
            fd = os.open(self.state("lock"), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                return False
            finally:
                os.close(fd)
        return self.until(free)

    def assert_launches(self, n):
        """The stub logs as it starts - which can be after an ask that timed
        out has returned: wait for n to show up, then say it is exactly n."""
        self.until(lambda: self.launches() >= n)
        self.assertEqual(self.launches(), n)

    def test_an_answer_comes_back(self):                                # control
        self.stub('echo "ttys001\tmy tab"')
        self.assertEqual(self.ask(), "ttys001\tmy tab\n")
        self.assertEqual(self.ask(), "ttys001\tmy tab\n")
        self.assert_launches(2)

    def test_no_iterm2_running_means_no_ask(self):
        # `tell application "iTerm2"` would LAUNCH it
        self.stub("echo hi")
        why = {}
        self.assertIsNone(ccwho.terms.ITERM2.ask(["-e", "x"], timeout=3,
                                          procs=apps((1, 0, "launchd")), why=why))
        self.assertEqual(self.launches(), 0)
        self.assertEqual(why.get("refused"), "not-running")

    def test_while_one_ask_is_in_flight_no_second_is_sent(self):
        self.stub("sleep 1.5; echo late")
        why, why2 = {}, {}
        self.assertIsNone(self.ask(timeout=0.3))
        self.assertIsNone(self.ask(timeout=0.3, now=time.time() + 60, why=why))
        self.assertIsNone(self.ask(timeout=0.3, now=time.time() + 60, why=why2))
        self.assert_launches(1)
        self.assertEqual(why.get("refused"), "busy")
        self.assertFalse(why.get("asked") or why2.get("asked"), "a second ask was sent")

    def test_an_ask_that_timed_out_runs_to_its_end(self):
        # killing it frees nothing inside iTerm2 - and would free the lock. The
        # wrapper writes the status last: it exists only if nothing was killed
        self.stub("sleep 1; echo late")
        why = {}
        self.assertIsNone(self.ask(timeout=0.3, why=why))
        self.assertTrue(self.ended(), "the ask was killed before it could say how it ended")
        self.assertEqual((why.get("asked"), why.get("error")), (True, "timeout"))

    def test_after_a_timeout_it_waits_before_asking_again(self):
        # a slow iTerm2 would otherwise always have one event in flight
        self.stub("sleep 1; echo late")
        t = time.time()
        self.assertIsNone(self.ask(timeout=0.3, now=t))
        self.assertTrue(self.ended())
        self.assertIsNone(self.ask(now=t + 5), "asked again 5 s after a timeout")
        self.assert_launches(1)

    def test_the_wait_counts_from_when_the_late_answer_was_seen(self):
        # an ask started at t, given up at t+5, answering at t+40 has used up a
        # wait counted from t: the next ask would go straight out
        self.stub("sleep 0.5; echo late")
        t = time.time()
        self.assertIsNone(self.ask(timeout=0.1, now=t))
        self.assertTrue(self.ended())
        self.assertIsNone(self.ask(now=t + 40), "the late answer was seen now")
        self.assertIsNone(self.ask(now=t + 60))
        self.assert_launches(1)
        self.assertEqual(self.ask(now=t + 71), "late\n")                # control

    def test_the_time_is_read_after_the_lock_is_taken(self):
        # a wait set while this caller queued for the lock is a wait for it too
        self.stub("echo sent")
        os.makedirs(ccwho.terms.STATE_DIR, exist_ok=True)
        fd = os.open(self.state("lock"), os.O_RDWR | os.O_CREAT, 0o600)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)

        def meanwhile():
            time.sleep(0.6)
            with open(self.state("json"), "w") as f:
                json.dump({"wait_until": time.time() + ccwho.terms.ASK_WAIT_AFTER_ERROR}, f)
            os.close(fd)
        import threading
        threading.Thread(target=meanwhile).start()
        self.assertIsNone(ccwho.terms.ITERM2.ask(["-e", "x"], timeout=3, procs=self.table(), wait=5))
        self.assertEqual(self.launches(), 0)

    def test_a_timeout_inside_iterm2_stops_asking_that_iterm2(self):
        self.stub('sleep 0.5; echo "execution error: AppleEvent timed out. (-1712)" >&2; exit 1')
        self.assertIsNone(self.ask(timeout=0.2))
        self.assertTrue(self.ended())
        why, t = {}, time.time() + 3600
        self.assertIsNone(self.ask(now=t, why=why))
        self.assertIsNone(self.ask(now=t + ccwho.terms.STUCK_RETRY - 1))
        self.assert_launches(1)
        self.assertEqual(why.get("refused"), "stuck")

    def test_an_ask_whose_wrapper_died_still_counts(self):
        # the shell killed, osascript left to finish: no status, but the -1712
        # it wrote is there, and the lock is free
        self.stub("echo never")
        os.makedirs(ccwho.terms.STATE_DIR, exist_ok=True)
        with open(self.state("json"), "w") as f:
            json.dump({"asked_pid": 100, "pending": True}, f)
        with open(self.state("err"), "w") as f:
            f.write("execution error: AppleEvent timed out. (-1712)\n")
        self.assertIsNone(self.ask(now=time.time() + 3600))
        self.assertEqual(self.launches(), 0)

    def test_a_restarted_iterm2_is_asked_again(self):                   # control
        self.stub('echo "execution error: AppleEvent timed out. (-1712)" >&2; exit 1')
        self.assertIsNone(self.ask(pid=100))
        self.assertIsNone(self.ask(pid=100))
        self.assert_launches(1)
        self.stub('echo back')
        self.assertEqual(self.ask(pid=200), "back\n")

    def test_a_refusal_does_not_rewrite_the_state(self):
        self.stub('echo "execution error: AppleEvent timed out. (-1712)" >&2; exit 1')
        self.assertIsNone(self.ask())
        before = os.stat(self.state("json"))
        time.sleep(0.02)
        self.assertIsNone(self.ask())
        after = os.stat(self.state("json"))
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_a_quarantine_that_cannot_be_saved_is_not_lost(self):
        self.stub('sleep 0.3; echo "execution error: AppleEvent timed out. (-1712)" >&2; exit 1')
        self.assertIsNone(self.ask(timeout=0.1))
        self.assertTrue(self.ended())
        terms = ccwho.terms
        real = terms._write_gate
        terms._write_gate = lambda app, state: False  # a full disk, say
        try:
            self.assertIsNone(self.ask())
        finally:
            terms._write_gate = real
        # an hour later, past any wait: only the quarantine can still say no
        self.assertIsNone(self.ask(now=time.time() + 3600), "the evidence of -1712 was thrown away")
        self.assert_launches(1)

    def test_any_other_error_waits_thirty_seconds_and_says_which(self):
        self.stub('echo "Not authorized to send Apple events to iTerm2. (-1743)" >&2; exit 1')
        t = time.time()
        first, second = {}, {}
        self.assertIsNone(self.ask(now=t, why=first))
        self.assertIsNone(self.ask(now=t + 29, why=second))
        self.assert_launches(1)
        self.assertEqual(first.get("error"), "-1743")
        self.assertEqual((second.get("refused"), second.get("error")), ("waiting", "-1743"))
        self.stub('echo ok')
        self.assertEqual(self.ask(now=t + 31), "ok\n")

    def test_a_big_late_answer_does_not_hold_the_lock(self):
        # a pipe nobody reads fills at 64 KB and the child would never exit
        self.stub("sleep 0.3; head -c 1000000 /dev/zero | tr '\\0' a")
        t = time.time()
        self.assertIsNone(self.ask(timeout=0.1, now=t))
        self.assertTrue(self.ended())
        self.stub("echo small")
        self.assertIsNone(self.ask(now=t + 100), "the late answer is seen: a wait starts")
        self.assertEqual(self.ask(now=t + 131), "small\n", "the lock was held")

    def test_damaged_state_values_are_a_closed_gate(self):
        self.stub("echo fine")
        os.makedirs(ccwho.terms.STATE_DIR, exist_ok=True)
        t = time.time()
        for bad in ("{not json", '{"wait_until": "x"}', '{"wait_until": null}',
                    '{"wait_until": 1e308}', '{"quarantine": [[1]]}', '{"quarantine": [1, {"a": 2}]}',
                    '{"quarantine": []}', '{"quarantine": [-100]}', '{"quarantine": "1"}',
                    "[]", '{"pending": "yes"}'):
            with open(self.state("json"), "w") as f:
                f.write(bad)
            self.assertEqual(self.ask(now=t), "fine\n", bad)
        with open(self.state("json"), "w") as f:                        # control
            f.write(json.dumps({"wait_until": t + 10}))
        self.assertIsNone(self.ask(now=t))

    def test_its_files_are_private(self):
        # the answers hold tab names: paths, commands, host names. The dir may
        # already exist, made by an older ccwho with the default umask
        os.makedirs(ccwho.terms.STATE_DIR, mode=0o755)
        os.chmod(ccwho.terms.STATE_DIR, 0o755)
        self.stub("echo 'a tab'")
        self.assertEqual(self.ask(), "a tab\n")
        self.stub("sleep 0.3; echo later")
        self.assertIsNone(self.ask(timeout=0.1, now=time.time() + 100))
        self.assertTrue(self.ended())
        self.assertEqual(os.stat(ccwho.terms.STATE_DIR).st_mode & 0o077, 0)
        for name in ("json", "out", "status"):
            self.assertEqual(os.stat(self.state(name)).st_mode & 0o077, 0, name)

    def test_an_unwritable_state_dir_is_no_answer_not_a_crash(self):
        blocker = os.path.join(self.tmp, "file")
        open(blocker, "w").close()
        ccwho.terms.STATE_DIR = os.path.join(blocker, "state")
        self.stub("echo hi")
        why = {}
        self.assertIsNone(self.ask(why=why))
        self.assertEqual(self.launches(), 0)
        self.assertEqual(why.get("refused"), "io")

    def test_a_launch_that_fails_is_no_answer_not_a_crash(self):
        self.stub("echo hi")
        real = ccwho.subprocess.Popen

        def fails(*a, **k):
            raise OSError("no /bin/sh")
        ccwho.subprocess.Popen = fails
        try:
            self.assertIsNone(self.ask())
        finally:
            ccwho.subprocess.Popen = real

    def test_a_caller_that_may_wait_gets_the_lock_when_it_frees(self):
        # save runs every 15 minutes: meeting the list's ask must not cost it
        # its panes
        self.stub("sleep 0.8; echo ok")
        other = subprocess.Popen(
            [sys.executable, "-B", "-c",
             "import ccwho_engine as e; "
             f"e.terms.STATE_DIR = {ccwho.terms.STATE_DIR!r}; e.terms.OSASCRIPT = {ccwho.terms.OSASCRIPT!r}; "
             f"print(e.terms.ITERM2.ask(['-e','x'], timeout=5, procs={self.table()!r}))"],
            stdout=subprocess.PIPE, text=True, cwd=os.path.dirname(os.path.abspath(__file__)))
        self.addCleanup(other.wait)
        self.addCleanup(other.stdout.close)
        self.assertTrue(self.until(lambda: self.launches() == 1))
        self.assertIsNone(self.ask(wait=0), "no wait: busy is no")          # control
        self.assertEqual(self.ask(wait=5), "ok\n")

    def gate_held_with_stdio_closed(self, guarded):
        self.stub("sleep 1.5; echo late")
        result = os.path.join(self.tmp, "gate-held")
        code = f"""
import fcntl, os, sys
out = os.open({result!r}, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
for fd in (0, 1, 2):
    os.close(fd)
import ccwho_engine as e
e.terms.STATE_DIR, e.terms.OSASCRIPT = {ccwho.terms.STATE_DIR!r}, {ccwho.terms.OSASCRIPT!r}
if not {guarded!r}:
    e.terms.above_std = lambda fd: fd
e.terms.ITERM2.ask(["-e", "x"], timeout=0.3, procs={self.table()!r})
again = os.open(e.terms._gate_path(e.terms.ITERM2, "lock"), os.O_RDWR)
try:
    fcntl.flock(again, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(out, b"free")
except BlockingIOError:
    os.write(out, b"held")
"""
        subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, timeout=30,
                       cwd=os.path.dirname(os.path.abspath(__file__)))
        self.assertTrue(self.until(self.ended))
        with open(result) as fh:
            return fh.read()

    def test_a_ccwho_started_with_stdio_closed_still_holds_the_gate(self):
        # the gate lock opened as fd 0 is the wrapper's DEVNULL stdin there:
        # it would hold no lock, and the next ask would go beside a stuck one
        self.assertEqual(self.gate_held_with_stdio_closed(guarded=True), "held")

    def test_the_same_with_the_lock_left_low(self):                            # control
        self.assertEqual(self.gate_held_with_stdio_closed(guarded=False), "free")

    def test_the_lock_is_shared_between_processes(self):
        self.stub("sleep 1.5; echo late")
        self.assertIsNone(self.ask(timeout=0.2))
        code = ("import ccwho_engine as e, time; "
                f"e.terms.STATE_DIR = {ccwho.terms.STATE_DIR!r}; e.terms.OSASCRIPT = {ccwho.terms.OSASCRIPT!r}; "
                # past any wait: only the lock can say no
                f"print(e.terms.ITERM2.ask(['-e','x'], timeout=0.3, procs={self.table()!r}, now=time.time() + 600))")
        here = os.path.dirname(os.path.abspath(__file__))
        other = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                               text=True, timeout=20, cwd=here)
        self.assertEqual(other.stdout.strip(), "None")
        self.assert_launches(1)
        self.assertTrue(self.ended())
        # control: once the child is gone the lock is free (the first call sees
        # the late answer and waits; one past that wait asks)
        subprocess.run([sys.executable, "-B", "-c", code], capture_output=True,
                       text=True, timeout=20, cwd=here)
        later = code.replace("time.time() + 600", "time.time() + 700")
        subprocess.run([sys.executable, "-B", "-c", later], capture_output=True,
                       text=True, timeout=20, cwd=here)
        self.assert_launches(2)


class TestTabNamesAndPanesAskThroughTheGate(unittest.TestCase):
    """ITERM2.titles and ITERM2.panes run in the background - every list
    scan, every autosave - so they must never reach osascript except through
    the gate. None is "not asked"; {} is "iTerm2 answered: no tabs"."""

    def setUp(self):
        self.asked = []
        iterm = ccwho.terms.ITERM2      # put back on the object it was taken from
        real = (iterm.ask, ccwho.subprocess.run)
        self.addCleanup(lambda: (setattr(iterm, "ask", real[0]),
                                 setattr(ccwho.subprocess, "run", real[1])))

        def direct(*a, **k):
            raise AssertionError("osascript run directly, not through the gate")
        ccwho.subprocess.run = direct

    def answer(self, text):
        ccwho.terms.ITERM2.ask = lambda args, timeout=5.0, **k: self.asked.append((args, k)) or text

    def test_tab_names_come_through_the_gate(self):
        self.answer("/dev/ttys001\tmy tab\n")
        self.assertEqual(ccwho.terms.ITERM2.titles(), {"ttys001": "my tab"})
        self.assertIn('tell application "iTerm2"', self.asked[0][0][-1])

    def test_not_asked_is_not_an_empty_answer(self):
        self.answer(None)
        self.assertIsNone(ccwho.terms.ITERM2.titles())
        self.answer("")
        self.assertEqual(ccwho.terms.ITERM2.titles(), {})                      # control

    def test_panes_come_through_the_gate(self):
        self.answer("/dev/ttys001\tG-1\tmy tab\n")
        self.assertEqual(ccwho.terms.ITERM2.panes(), {"ttys001": {"pane": "G-1", "name": "my tab"}})

    def test_panes_wait_for_a_busy_gate_and_names_do_not(self):
        self.answer("")
        ccwho.terms.ITERM2.panes()
        ccwho.terms.ITERM2.titles()
        self.assertGreater(self.asked[0][1].get("wait", 0), 0, "save must not lose its panes")
        self.assertEqual(self.asked[1][1].get("wait", 0), 0, "a scan must never block on it")

    def test_panes_not_asked_are_unknown(self):
        self.answer(None)
        self.assertIsNone(ccwho.terms.ITERM2.panes())

    def test_a_restore_asks_for_its_panes_itself(self):
        # restore is something you do: it does not queue behind the gate, nor
        # wait out a pause some background ask set
        ccwho.terms.ITERM2.ask = lambda *a, **k: self.fail("restore went through the gate")
        seen = {}

        class Done:
            returncode, stdout, stderr = 0, "/dev/ttys001\tG-1\tt\n", ""

        ccwho.subprocess.run = lambda cmd, **k: seen.update(k) or Done()
        self.assertEqual(ccwho.terms.ITERM2.panes(direct=True), {"ttys001": {"pane": "G-1", "name": "t"}})
        self.assertGreater(seen.get("timeout", 0), 0)


class TestNewTabsDoNotMakeCollectAskITerm2(MachinelessCollect):
    """Opening and closing tabs changes the set of terminals on every tick of a
    busy day. That used to force a fresh AppleScript ask each time."""

    def test_changing_terminals_between_ticks_ask_once(self):
        calls = []
        ccwho.terms.ITERM2.titles = lambda timeout=5.0, **k: calls.append(1) or {"ttys001": "a"}
        real = ccwho.agents_json
        self.addCleanup(setattr, ccwho, "agents_json", real)
        ccwho.agents_json = lambda: "[]"
        cache = {}
        for n in range(1, 6):
            ccwho.tty_snapshot = lambda n=n: procs(*[(100 + i, f"ttys{i:03d}", ME, "zsh")
                                                      for i in range(n)])
            ccwho.collect(cache=cache)
        self.assertEqual(len(calls), 1)


class TestAReusedTerminalLosesItsOldName(unittest.TestCase):
    """A tab closes and a new one gets the same tty inside the minute the names
    are kept: it must show its "~" fallback, not the old session's name. Seen
    from ps - the terminal's first process changed - so no Apple Event."""

    def setUp(self):
        self.calls = []
        self.reply = {"ttys022": "old tab", "ttys030": "other tab"}
        testkit.patch(self, ccwho.terms.ITERM2, "titles",
                      lambda timeout=5.0, **k: self.calls.append(1) or self.reply)

    def test_a_terminal_with_a_new_first_process_loses_its_name(self):
        cache = {}
        ccwho.terms.titles_cached(cache, now=1000.0, owners={"ttys022": 300, "ttys030": 400})
        names = ccwho.terms.titles_cached(cache, now=1010.0, owners={"ttys022": 900, "ttys030": 400})
        self.assertEqual(names, {"ttys030": "other tab"})
        self.assertEqual(len(self.calls), 1, "no new ask for it")

    def test_the_same_terminals_keep_their_names(self):                 # control
        cache = {}
        ccwho.terms.titles_cached(cache, now=1000.0, owners={"ttys022": 300})
        self.assertEqual(ccwho.terms.titles_cached(cache, now=1010.0, owners={"ttys022": 300})["ttys022"],
                         "old tab")

    def test_an_answer_with_no_tabs_is_kept_for_the_minute(self):
        # iTerm2 open with no windows answers "" - asking on every tick for it
        # broke the once-a-minute promise
        self.reply = {}
        cache = {}
        for i in range(10):
            ccwho.terms.titles_cached(cache, now=1000.0 + i)
        self.assertEqual(len(self.calls), 1)

    def test_a_refused_refresh_keeps_the_names_it_had(self):
        cache = {}
        owners = {"ttys022": 300, "ttys040": 900}
        ccwho.terms.titles_cached(cache, now=1000.0, owners=owners)
        self.reply = None                               # the gate would not ask
        self.assertEqual(ccwho.terms.titles_cached(cache, now=1061.0, owners=owners).get("ttys022"),
                         "old tab")
        self.reply = {"ttys022": "new"}
        self.assertEqual(ccwho.terms.titles_cached(cache, now=1062.0, owners=owners), {"ttys022": "new"},
                         "a refusal must not count as the minute's ask")


class TestASaveThatCannotAskKeepsThePanesItKnew(unittest.TestCase):
    """A save every 15 minutes, keeping 20: five hours of saves while iTerm2 is
    quarantined would push every manifest that knew its panes out.

    Only for the same process: the same session id, tty and pid as the last
    save. A session id does not name a pane - a session resumed elsewhere, or
    after a reboot, would be given the pane it left (2026-09-30)."""

    ROW = {"sessionId": "s1", "cwd": "/x", "project": "x", "tty": "ttys022", "tab_title": "",
           "pid": 4100}
    WAS = {"pane": "G-1", "tabTitle": "old tab", "tty": "ttys022", "pid": 4100}

    def carried(self, row=None, **was):
        man = ccwho.manifest_from_rows([dict(self.ROW, **(row or {}))], panes=None,
                                       known={"s1": dict(self.WAS, **was)})
        return man["sessions"][0]["pane"], man["sessions"][0]["tabTitle"]

    def test_unknown_panes_carry_over_from_the_last_save(self):            # control
        self.assertEqual(self.carried(), ("G-1", "old tab"))

    def test_asked_and_no_pane_is_no_pane(self):                         # control
        man = ccwho.manifest_from_rows([self.ROW], panes={}, known={"s1": self.WAS})
        self.assertEqual(man["sessions"][0]["pane"], "")

    def test_a_new_pid_on_the_same_tty_does_not_carry(self):
        # resumed in the same tab, or after a reboot that gave the tty again
        self.assertEqual(self.carried(row={"pid": 5200}), ("", ""))

    def test_a_new_tty_does_not_carry(self):
        self.assertEqual(self.carried(row={"tty": "ttys031"}), ("", ""))

    def test_a_saved_entry_with_no_pid_does_not_carry(self):
        self.assertEqual(self.carried(pid=None), ("", ""))

    def test_a_row_with_no_tty_does_not_carry(self):
        self.assertEqual(self.carried(row={"tty": ""}, tty=""), ("", ""))

    def test_no_pid_on_either_side_does_not_carry(self):
        # str(None) is "None", which is not empty: the check is on the values
        self.assertEqual(self.carried(row={"pid": None}, pid=None), ("", ""))

    def test_pid_zero_on_both_sides_does_not_carry(self):
        self.assertEqual(self.carried(row={"pid": 0}, pid=0), ("", ""))

    def test_a_pid_read_back_as_text_still_matches(self):                  # control
        self.assertEqual(self.carried(pid="4100"), ("G-1", "old tab"))

    def test_a_long_tty_matches_its_short_form(self):                      # control
        self.assertEqual(self.carried(row={"tty": "/dev/ttys022"}), ("G-1", "old tab"))


class TestTheRealTablesNameTheProgramNotItsArgv0(unittest.TestCase):
    """The tables come from ps on this machine: only the kernel's name for the
    program can tell `exec -a .../MacOS/iTerm2 sleep` from iTerm2. Starts one
    such process - no Apple Event, nothing iTerm2 sees."""

    def test_a_process_calling_itself_iterm2_is_named_sleep(self):
        fake = subprocess.Popen(["/bin/bash", "-c", "exec -a /x/iTerm.app/Contents/MacOS/iTerm2 sleep 5"])
        self.addCleanup(fake.wait)
        self.addCleanup(fake.kill)
        time.sleep(0.2)
        for table in (REAL["terms.app_snapshot"](), REAL_TTY_SNAPSHOT()):
            self.assertEqual(ccwho.terms.parse_procs(table)[fake.pid][2], "sleep")



class TestATabIsItsPaneProcess(unittest.TestCase):
    """A tab's identity, for keeping its name: the process iTerm2 (or its
    daemon) started on that tty - not the lowest pid on it. pids wrap at 99999:
    measured 2026-09-29, 8 of 26 panes had a command with a lower pid than the
    pane, and each one that came and went dropped the tab's name."""

    def owners(self, *extra):
        p = procs((100, "??", ME, IT), (200, "??", ME, SERVER),
                  (78437, "ttys045", 0, "login"), *extra)
        out = ps((100, 1, APP), (200, 1, DAEMON_CMD), (78437, 200, LOGIN),
                 *[(pid, 78437, "git status") for pid, _t, _u, _c in extra])
        return ccwho.terms.owners(out, p)

    def test_a_command_with_a_lower_pid_does_not_change_the_tab(self):
        self.assertEqual(self.owners((38200, "ttys045", ME, "git")), {"ttys045": 78437})
        self.assertEqual(self.owners(), {"ttys045": 78437})

    def test_a_new_pane_on_the_same_tty_is_a_new_tab(self):              # control
        p = procs((100, "??", ME, IT), (200, "??", ME, SERVER), (91000, "ttys045", 0, "login"))
        out = ps((100, 1, APP), (200, 1, DAEMON_CMD), (91000, 200, LOGIN))
        self.assertEqual(ccwho.terms.owners(out, p), {"ttys045": 91000})


class TestTheLastErrorCodeIsTheError(unittest.TestCase):
    """osascript's error message can quote data - a -1728 lists the session
    names, and any program in a pane sets its own name. Only the code that ends
    the message is the error."""

    def test_a_quoted_code_is_not_the_error(self):
        err = '32:38: execution error: Can’t get item 3 of {"build (-1712)", "b"}. (-1728)\n'
        self.assertEqual(ccwho.terms.ae_error_code(err), "-1728")

    def test_the_code_at_the_end_is(self):                               # control
        self.assertEqual(ccwho.terms.ae_error_code("execution error: AppleEvent timed out. (-1712)\n"), "-1712")
        self.assertIsNone(ccwho.terms.ae_error_code(""))


class TestTheGateAfterASlowOrFailedAsk(unittest.TestCase):
    """What the gate does after an ask that was slow, or failed."""

    # the gate fixture, borrowed - not inherited, or its tests run here twice
    _F = TestBackgroundAsksNeverPileUpInITerm2
    stub, launches, table, state, ask, until, ended = (
        _F.stub, _F.launches, _F.table, _F.state, _F.ask, _F.until, _F.ended)

    def setUp(self):
        self._F.setUp(self)
        # a boot id of the test's own: not every host can read one
        self.addCleanup(setattr, ccwho.terms, "boot_id", ccwho.terms.boot_id)
        ccwho.terms.boot_id = lambda: "THIS-BOOT"
    assert_launches = _F.assert_launches

    def test_a_quoted_code_does_not_quarantine(self):
        self.stub('echo \'execution error: Can’t get item 3 of {"x (-1712)"}. (-1728)\' >&2; exit 1')
        why = {}
        self.assertIsNone(self.ask(why=why))
        self.assertEqual(why.get("error"), "-1728")
        self.stub("echo ok")
        self.assertEqual(self.ask(now=time.time() + 31), "ok\n", "quarantined on a quoted code")

    def test_a_caller_that_may_wait_does_not_queue_behind_a_slow_ask(self):
        # the ask in flight already timed out: waiting for it only freezes the
        # caller - the answer after it is a refusal anyway
        self.stub("sleep 3; echo late")
        self.assertIsNone(self.ask(timeout=0.2))
        start, why = time.time(), {}
        self.assertIsNone(self.ask(wait=3, why=why))
        self.assertLess(time.time() - start, 0.5)
        self.assertFalse(why.get("asked"))

    def test_a_slow_ask_that_has_ended_does_not_refuse_waiting_callers_forever(self):
        # only a caller holding the lock settles it: one that may wait must not
        # turn away from a free lock because of a mark nobody cleared
        self.stub("sleep 0.4; echo late")
        self.assertIsNone(self.ask(timeout=0.1))
        self.assertTrue(self.ended())
        self.stub("echo ok")
        why = {}
        self.ask(now=time.time() + 100, wait=3, why=why)           # settles it: a pause starts
        self.assertEqual(self.ask(now=time.time() + 200, wait=3), "ok\n")

    def test_past_the_pause_a_slow_ask_still_in_flight_turns_callers_away(self):
        # iTerm2 wedged: the ask that timed out still holds the lock after the
        # pause. A caller that may wait is refused at once, not after its wait
        self.stub("sleep 3; echo late")
        self.assertIsNone(self.ask(timeout=0.2))
        start, why = time.time(), {}
        self.assertIsNone(self.ask(now=time.time() + 60, wait=3, why=why))
        self.assertLess(time.time() - start, 0.5)
        self.assertEqual(why.get("refused"), "waiting")

    def test_a_queued_caller_stops_waiting_when_the_ask_ahead_times_out(self):
        # it queued behind a healthy ask, which then timed out: what it waits
        # for now is a refusal, and a save would sit out all of its 10 s
        self.stub("sleep 6; echo late")
        first = threading.Thread(target=self.ask, kwargs={"timeout": 0.5})
        first.start()
        self.addCleanup(first.join)
        self.assertTrue(self.until(lambda: self.launches() == 1))
        start, why = time.time(), {}
        self.assertIsNone(self.ask(wait=10, why=why))
        self.assertLess(time.time() - start, 4, "waited for the slow ask to end")
        self.assertEqual(why.get("refused"), "waiting")

    def test_an_ask_whose_wrapper_died_first_is_settled_after_it_ends(self):
        # the wrapper killed, its osascript still running and about to say
        # -1712. Settled at once, that goes into files already removed, and
        # the stuck iTerm2 is asked again
        marker = os.path.join(self.tmp, "ended")
        self.stub(f'kill -9 $PPID; sleep 0.5; echo "AppleEvent timed out. (-1712)" >&2; '
                  f'touch {shlex.quote(marker)}; exit 1')
        self.assertIsNone(self.ask())
        self.assertTrue(self.until(lambda: os.path.exists(marker)))
        why = {}
        self.assertTrue(self.until(lambda: self.ask(now=time.time() + 60, why=why) is None
                                   and why.get("refused") != "busy"))
        self.assertEqual(why.get("refused"), "stuck")
        self.assert_launches(1)

    def quarantined(self, boot):
        os.makedirs(ccwho.terms.STATE_DIR, exist_ok=True)
        with open(self.state("json"), "w") as f:
            json.dump({"quarantine": 100, "asked_pid": 100, "last_error": "-1712", "boot": boot}, f)
        self.stub("echo ok")

    def quarantined_on(self, pid):
        os.makedirs(ccwho.terms.STATE_DIR, exist_ok=True)
        with open(self.state("json"), "w") as f:
            json.dump({"quarantine": pid, "asked_pid": pid, "last_error": "-1712", "boot": "THIS-BOOT"}, f)
        self.stub("echo ok")

    def test_a_quarantined_iterm2_still_running_beside_a_lower_one(self):
        # two of ours: `tell application "iTerm2"` may reach the stuck one yet
        self.quarantined_on(200)
        why = {}
        self.assertIsNone(ccwho.terms.ITERM2.ask(["-e", "whatever"], timeout=10.0, procs=apps((100, ME, IT), (200, ME, IT)),
                                          why=why))
        self.assertEqual(why.get("refused"), "stuck")

    def test_every_iterm2_an_ask_may_have_reached_is_quarantined(self):
        # two of ours when it timed out: either may be the stuck one
        self.stub("echo 'execution error: AppleEvent timed out. (-1712)' >&2; exit 1")
        two = apps((100, ME, IT), (200, ME, IT))
        self.assertIsNone(ccwho.terms.ITERM2.ask(["-e", "whatever"], timeout=10.0, procs=two))
        self.stub("echo ok")
        why = {}
        self.assertTrue(self.until(lambda: ccwho.terms.ITERM2.ask(["-e", "whatever"], timeout=10.0,
                                                           procs=apps((200, ME, IT)), why=why) is None
                                   and why.get("refused") != "busy"))
        self.assertEqual(why.get("refused"), "stuck")
        self.assertEqual(ccwho.terms.ITERM2.ask(["-e", "whatever"], timeout=10.0, procs=apps((300, ME, IT))), "ok\n")

    def test_a_quarantined_iterm2_gone(self):                                   # control
        self.quarantined_on(200)
        self.assertEqual(ccwho.terms.ITERM2.ask(["-e", "whatever"], timeout=10.0, procs=apps((100, ME, IT))), "ok\n")

    def test_a_quarantine_from_before_a_reboot_is_dropped(self):
        # a pid names a process only within a boot: after one, an iTerm2 with
        # the old one's pid is a new iTerm2
        self.quarantined("ANOTHER-BOOT")
        self.assertEqual(self.ask(pid=100), "ok\n")

    def test_one_from_before_the_boot_was_recorded_holds(self):
        # written by the gate before it recorded boots: not a reason to ask a
        # stuck iTerm2 again
        self.quarantined(None)
        why = {}
        self.assertIsNone(self.ask(pid=100, why=why))
        self.assertEqual(why.get("refused"), "stuck")

    def test_a_boot_that_cannot_be_read_drops_nothing(self):
        # like the claims: not knowing this boot is no proof of another
        self.quarantined("ANOTHER-BOOT")
        real = ccwho.terms.boot_id
        self.addCleanup(setattr, ccwho.terms, "boot_id", real)
        ccwho.terms.boot_id = lambda: None
        why = {}
        self.assertIsNone(self.ask(pid=100, why=why))
        self.assertEqual(why.get("refused"), "stuck")

    def test_the_boot_is_recorded_when_the_ask_is_sent(self):
        # settled later - maybe after a reboot - it must still say which boot
        # its iTerm2 was of
        self.stub("sleep 1; echo late")
        self.assertIsNone(self.ask(pid=100, timeout=0.2))
        with open(self.state("json")) as f:
            self.assertEqual(json.load(f).get("boot"), ccwho.terms.boot_id())
        self.assertTrue(self.ended())

    def test_a_timeout_that_came_in_before_a_reboot_quarantines_nothing(self):
        # the ask went to an iTerm2 of the boot before: settled after a
        # reboot, its -1712 must not quarantine the new iTerm2 with that pid
        os.makedirs(ccwho.terms.STATE_DIR, exist_ok=True)
        with open(self.state("json"), "w") as f:
            json.dump({"pending": True, "asked_pid": 100, "boot": "ANOTHER-BOOT"}, f)
        for name, text in (("status", "1\n"), ("err", "AppleEvent timed out. (-1712)"), ("out", "")):
            with open(self.state(name), "w") as f:
                f.write(text)
        self.stub("echo ok")
        self.assertEqual(self.ask(pid=100, now=time.time() + 60), "ok\n")

    def test_one_from_this_boot_holds(self):                                   # control
        self.quarantined(ccwho.terms.boot_id())
        why = {}
        self.assertIsNone(self.ask(pid=100, why=why))
        self.assertEqual(why.get("refused"), "stuck")

    def test_a_timeout_inside_iterm2_records_the_boot(self):
        self.stub('echo "AppleEvent timed out. (-1712)" >&2; exit 1')
        self.assertIsNone(self.ask(pid=100))
        with open(self.state("json")) as f:
            self.assertEqual(json.load(f).get("boot"), ccwho.terms.boot_id())

    def test_an_error_pauses_from_when_it_came_back(self):
        self.stub('sleep 0.5; echo "Not authorized (-1743)" >&2; exit 1')
        start = time.time()
        self.assertIsNone(self.ask())
        with open(self.state("json")) as f:
            wait = json.load(f)["wait_until"]
        self.assertGreaterEqual(wait, start + ccwho.terms.ASK_WAIT_AFTER_ERROR + 0.4)


class TestAStuckITerm2IsAskedAgainLater(unittest.TestCase):
    """2026-10-03 19:24: one ask went unanswered for 2 minutes (-1712), and the
    gate asked that iTerm2 nothing more until it quit - two days, through 190
    saves, while it worked fine for its owner. The tab names in the list froze
    and new sessions were saved with no pane. So a quarantine lasts a while:
    10 minutes, doubled after each ask that times out again, up to 2 hours. The
    ask that tries again is the only one in flight (the lock), as every ask is:
    a stuck iTerm2 gets at most one more event each wait, not 509."""

    _F = TestBackgroundAsksNeverPileUpInITerm2
    stub, launches, table, state, ask, until, ended, assert_launches = (
        _F.stub, _F.launches, _F.table, _F.state, _F.ask, _F.until, _F.ended, _F.assert_launches)
    TIMED_OUT = 'echo "execution error: AppleEvent timed out. (-1712)" >&2; exit 1'

    def setUp(self):
        self._F.setUp(self)
        self.addCleanup(setattr, ccwho.terms, "boot_id", ccwho.terms.boot_id)
        ccwho.terms.boot_id = lambda: "THIS-BOOT"
        self.t = time.time()

    def gate(self):
        with open(self.state("json")) as f:
            return json.load(f)

    def write_gate(self, **state):
        os.makedirs(ccwho.terms.STATE_DIR, exist_ok=True)
        with open(self.state("json"), "w") as f:
            json.dump(dict({"quarantine": 100, "asked_pid": 100, "last_error": "-1712",
                            "boot": "THIS-BOOT"}, **state), f)

    def timed_out(self):
        """An ask at t that iTerm2 left unanswered: quarantined from t."""
        self.stub(self.TIMED_OUT)
        self.assertIsNone(self.ask(now=self.t))

    def test_it_is_asked_again_after_ten_minutes(self):
        self.timed_out()
        why = {}
        self.assertIsNone(self.ask(now=self.t + 599, why=why))
        self.assertEqual((why.get("refused"), why.get("retry_at")), ("stuck", self.t + 600))
        self.stub("echo ok")
        self.assertEqual(self.ask(now=self.t + 600), "ok\n")
        self.assert_launches(2)

    def test_an_answer_ends_the_quarantine(self):
        self.timed_out()
        self.stub("echo ok")
        self.assertEqual(self.ask(now=self.t + 600), "ok\n")
        self.assertEqual(self.ask(now=self.t + 601), "ok\n")
        self.assertFalse({"quarantine", "retry_at", "strikes"} & set(self.gate()), self.gate())

    def test_each_timeout_doubles_the_wait(self):
        self.timed_out()                                        # strike 1: t + 600
        self.assertIsNone(self.ask(now=self.t + 600))           # strike 2: + 1200
        self.assertIsNone(self.ask(now=self.t + 1799))
        self.assertIsNone(self.ask(now=self.t + 1800))          # strike 3: + 2400
        self.assertIsNone(self.ask(now=self.t + 4199))
        self.assert_launches(3)
        self.assertEqual(self.gate().get("retry_at"), self.t + 4200)

    def test_the_wait_stops_growing_at_two_hours(self):
        self.write_gate(strikes=9, retry_at=self.t)
        self.stub(self.TIMED_OUT)
        self.assertIsNone(self.ask(now=self.t))
        self.assert_launches(1)
        self.assertEqual((self.gate().get("strikes"), self.gate().get("retry_at")),
                         (10, self.t + ccwho.terms.STUCK_RETRY_LONGEST))

    def test_while_the_ask_that_tries_again_is_out_no_second_is_sent(self):
        self.timed_out()
        self.stub("sleep 1.5; " + self.TIMED_OUT)
        self.assertIsNone(self.ask(now=self.t + 600, timeout=0.3))
        why = {}
        self.assertIsNone(self.ask(now=self.t + 700, why=why))
        self.assertIn(why.get("refused"), ("busy", "waiting"))
        self.assert_launches(2)
        self.assertTrue(self.ended())

    def test_it_timing_out_again_is_the_next_strike(self):
        # the try ran past its caller's wait and then timed out inside iTerm2:
        # settled by the next ask, it doubles the wait from then
        self.timed_out()
        self.stub("sleep 0.5; " + self.TIMED_OUT)
        self.assertIsNone(self.ask(now=self.t + 600, timeout=0.1))
        self.assertTrue(self.ended())
        why = {}
        self.assertIsNone(self.ask(now=self.t + 900, why=why))
        self.assertEqual((why.get("refused"), why.get("retry_at")), ("stuck", self.t + 2100))

    def test_another_error_ends_the_quarantine(self):
        # an error came back: iTerm2 (or macOS for it) answered - not stuck
        self.timed_out()
        self.stub('echo "Not authorized to send Apple events to iTerm2. (-1743)" >&2; exit 1')
        self.assertIsNone(self.ask(now=self.t + 600))
        self.assertFalse({"quarantine", "retry_at", "strikes"} & set(self.gate()), self.gate())
        self.stub("echo ok")
        self.assertEqual(self.ask(now=self.t + 600 + ccwho.terms.ASK_WAIT_AFTER_ERROR), "ok\n")

    def test_a_try_that_ends_with_no_answer_keeps_the_quarantine(self):
        # killed, or no error code: no proof the app answers - review 1 of
        # env-panes. The wait starts again, at the same length
        self.write_gate(strikes=3, retry_at=self.t)
        self.stub("exit 1")
        self.assertIsNone(self.ask(now=self.t))
        self.assertEqual((self.gate().get("quarantine"), self.gate().get("strikes"),
                          self.gate().get("retry_at")), (100, 3, self.t + 2400))
        # and what shut it stays on record: doctor still says stuck (review 3)
        self.assertEqual(self.gate().get("last_error"), "-1712")
        self.assertEqual(ccwho.terms.gate_says(ccwho.terms.ITERM2, procs=self.table()), "stuck")

    def test_a_try_whose_wrapper_died_keeps_the_quarantine(self):
        # pending, and neither a status nor an error came back
        self.write_gate(strikes=2, retry_at=self.t - 1, pending=True)
        self.stub("echo ok")
        why = {}
        self.assertIsNone(self.ask(now=self.t, why=why))
        self.assertEqual((why.get("refused"), why.get("retry_at")), ("stuck", self.t + 1200))
        self.assert_launches(0)

    def test_the_strikes_stop_counting_where_the_wait_stops_growing(self):
        # a count past what the gate file keeps would drop it, and the wait
        # would fall back to 20 minutes after days stuck
        self.write_gate(strikes=64, retry_at=self.t)
        self.stub(self.TIMED_OUT)
        self.assertIsNone(self.ask(now=self.t))
        self.assertIsNone(self.ask(now=self.t + 7200))
        self.assertEqual(self.gate().get("retry_at"), self.t + 14400)

    def test_a_quarantine_from_before_this_change_waits_one_round(self):
        # its gate file has no retry time: the first wait starts when it is read
        self.write_gate()
        self.stub("echo ok")
        why = {}
        self.assertIsNone(self.ask(now=self.t, why=why))
        self.assertEqual(why.get("refused"), "stuck")
        self.assertEqual(self.gate().get("retry_at"), self.t + 600)
        self.assertIsNone(self.ask(now=self.t + 599))
        self.assertEqual(self.ask(now=self.t + 600), "ok\n")

    def test_a_retry_time_past_the_longest_wait_is_a_clock_set_back(self):
        # a clock set back by a day would hold the gate shut that much longer:
        # the wait starts again from now
        self.write_gate(strikes=1, retry_at=self.t + 86400)
        self.stub("echo ok")
        self.assertIsNone(self.ask(now=self.t))
        self.assertEqual(self.ask(now=self.t + 600), "ok\n")

    def test_a_retry_time_at_the_longest_wait_holds(self):                     # control
        # one strike: started again, its wait would be over at t + 600
        self.write_gate(strikes=1, retry_at=self.t + ccwho.terms.STUCK_RETRY_LONGEST)
        self.stub("echo ok")
        self.assertIsNone(self.ask(now=self.t))
        self.assertIsNone(self.ask(now=self.t + 600))
        self.assertEqual(self.ask(now=self.t + ccwho.terms.STUCK_RETRY_LONGEST), "ok\n")

    def test_damaged_retry_values_wait_one_round(self):
        self.stub("echo ok")
        for bad in ({"retry_at": "x"}, {"retry_at": None}, {"retry_at": float("nan")},
                    {"strikes": "2"}, {"strikes": 0}, {"strikes": -3}, {"strikes": 1e9},
                    {"strikes": 65}, {"strikes": True}):
            with self.subTest(bad=bad):
                self.write_gate(**bad)
                self.assertIsNone(self.ask(now=self.t))
                self.assertEqual(self.ask(now=self.t + 600), "ok\n")

    def test_a_restart_forgets_the_strikes(self):
        self.write_gate(strikes=6, retry_at=self.t + 7000)
        self.stub("echo ok")
        self.assertEqual(self.ask(pid=200, now=self.t), "ok\n")
        self.assertFalse({"quarantine", "retry_at", "strikes"} & set(self.gate()), self.gate())
        self.stub(self.TIMED_OUT)
        self.assertIsNone(self.ask(pid=200, now=self.t + 1))
        self.assertEqual(self.gate().get("retry_at"), self.t + 601)

    def test_a_reboot_forgets_the_strikes(self):
        self.write_gate(strikes=6, retry_at=self.t + 7000, boot="ANOTHER-BOOT")
        self.stub("echo ok")
        self.assertEqual(self.ask(now=self.t), "ok\n")
        self.assertFalse({"quarantine", "retry_at", "strikes"} & set(self.gate()), self.gate())
        self.stub(self.TIMED_OUT)
        self.assertIsNone(self.ask(now=self.t + 1))
        self.assertEqual(self.gate().get("retry_at"), self.t + 601)


class TestAPaneRestoreThatTimesOutSaysSo(unittest.TestCase):
    """The pane-filling script catches errors so one closed pane does not stop
    the rest. A write that timed out (-1712) is not a closed pane: caught, it
    reads as "missed" and the session gets launched a second time."""

    def test_a_timeout_inside_the_fill_is_raised(self):
        sid = "4f2b91ac-1111-4222-8333-abcdefabcdef"
        script = ccwho.open_script(ccwho.terms.ITERM2, [{"sessionId": sid, "cwd": "/x", "project": "x"}],
                                         fill={sid: "U-1"})
        self.assertIn("on error errMsg number errNum", script)
        self.assertIn("if errNum is not in {-1728, -1719} then error errMsg number errNum", script)

    def test_a_pane_it_cannot_even_read_is_passed_over(self):
        # nothing was written to it: whatever its error, it is not a launch
        sid = "4f2b91ac-1111-4222-8333-abcdefabcdef"
        script = ccwho.open_script(ccwho.terms.ITERM2, [{"sessionId": sid, "cwd": "/x", "project": "x"}],
                                         fill={sid: "U-1"})
        lines = [l.strip() for l in script.splitlines()]
        read = lines.index("set u to unique id of s")
        # u is set before the read: a pane whose read failed leaves no u
        # from an earlier pane, and none undefined (-2753, not passed over)
        self.assertEqual(lines[read - 2:read], ['set u to ""', "try"])
        block = lines[read:lines.index("end try", read) + 1]
        self.assertFalse(any(l.startswith("error ") or " error errMsg" in l for l in block), block)
        self.assertLess(read, lines.index("if errNum is not in {-1728, -1719} then error errMsg number errNum"))

    @unittest.skipUnless(shutil.which("osascript"), "macOS only")
    def test_only_a_pane_that_is_gone_is_passed_over(self):
        # the fill's own error handler, run as it is - outside any tell: it
        # sends no event. -1712 and iTerm2 dying (-609, -600) may come after
        # the resume line was written: raised, never read as a closed pane
        sid = "4f2b91ac-1111-4222-8333-abcdefabcdef"
        script = ccwho.open_script(ccwho.terms.ITERM2, [{"sessionId": sid, "cwd": "/x", "project": "x"}],
                                         fill={sid: "U-1"})
        handler = next(l.strip() for l in script.splitlines() if l.strip().startswith("if errNum"))
        for code, raised in ((-1712, True), (-609, True), (-600, True), (-10000, True),
                             (-1728, False), (-1719, False)):
            probe = (f'try\nerror "x" number {code}\non error errMsg number errNum\n'
                     f"{handler}\nend try\nreturn \"passed over\"")
            r = subprocess.run(["osascript", "-e", probe], capture_output=True, text=True, timeout=20)
            self.assertEqual(r.returncode != 0, raised, (code, r.stdout, r.stderr))


class TestASaveTakesTheTabNameFromThePanes(unittest.TestCase):
    """A save's names ask can meet a busy gate while its panes ask, which waits,
    is answered - with every tab's name in it."""

    ROW = {"sessionId": "s1", "cwd": "/x", "project": "x", "tty": "ttys022", "tab_title": ""}

    def test_a_row_without_a_name_takes_the_pane_s(self):
        man = ccwho.manifest_from_rows([self.ROW], panes={"ttys022": {"pane": "U", "name": "my tab"}})
        self.assertEqual(man["sessions"][0]["tabTitle"], "my tab")

    def test_a_row_with_a_name_keeps_it(self):                           # control
        row = dict(self.ROW, tab_title="its own")
        man = ccwho.manifest_from_rows([row], panes={"ttys022": {"pane": "U", "name": "my tab"}})
        self.assertEqual(man["sessions"][0]["tabTitle"], "its own")


class TestCollectDropsTheNameOfAReplacedPane(MachinelessCollect):
    """collect() gives the tab-name cache each tty's pane process."""

    def test_a_new_pane_on_the_tty_loses_the_old_name(self):
        ccwho.terms.ITERM2.titles = lambda timeout=5.0, **k: {"ttys045": "old tab"}
        real = ccwho.agents_json
        self.addCleanup(setattr, ccwho, "agents_json", real)
        ccwho.agents_json = lambda: json.dumps([{"sessionId": "aaa", "pid": 501, "cwd": "/x", "status": "idle"}])
        cache = {}

        def world(pane):
            ccwho.ps_snapshot = lambda: ps((100, 1, APP), (200, 1, DAEMON_CMD), (pane, 200, LOGIN),
                                           (501, pane, "claude"))
            ccwho.tty_snapshot = lambda: procs((100, "??", ME, IT), (200, "??", ME, SERVER),
                                               (pane, "ttys045", 0, "login"), (501, "ttys045", ME, "claude"))
            return ccwho.collect(cache=cache)[0][0]["tab_title"]
        self.assertEqual(world(300), "old tab")
        self.assertEqual(world(300), "old tab")                                  # control
        self.assertEqual(world(900), "", "a new pane kept the old tab's name")


class TestOneScanFindsThePanesOnce(MachinelessCollect):
    def test_collect_works_out_the_panes_once(self):
        # counted on each app's own pass (App.owners_in): survey() reaches it,
        # and so do App.owners, App.ttys and terms.owners() - once an app a scan
        calls = []
        for app in ccwho.terms.APPS:
            setattr(app, "owners_in", lambda *a, _k=app.key, _r=app.owners_in:
                    calls.append(_k) or _r(*a))
            self.addCleanup(app.__dict__.pop, "owners_in", None)
        ccwho.tty_snapshot = lambda: procs((100, "??", ME, IT))
        ccwho.ps_snapshot = lambda: ps((100, 1, APP))
        ccwho.collect(cache={})
        self.assertEqual(sorted(calls), sorted(app.key for app in ccwho.terms.APPS))


class TestACarriedPaneIsText(unittest.TestCase):
    """What the last save said is read off disk: a pane or title that is not a
    string would be copied into every save and break the restore that reads it."""

    ROW = {"sessionId": "s1", "cwd": "/x", "project": "x", "tty": "ttys022", "tab_title": "",
           "pid": 4100}
    SAME = {"tty": "ttys022", "pid": 4100}      # the same process: only the values are junk

    def test_a_pane_that_is_not_text_is_not_carried(self):
        man = ccwho.manifest_from_rows([self.ROW], panes=None,
                                       known={"s1": dict(self.SAME, pane=["G"], tabTitle=5)})
        self.assertEqual((man["sessions"][0]["pane"], man["sessions"][0]["tabTitle"]), ("", ""))

    def test_text_is_carried(self):                                         # control
        man = ccwho.manifest_from_rows([self.ROW], panes=None,
                                       known={"s1": dict(self.SAME, pane="G", tabTitle="t")})
        self.assertEqual((man["sessions"][0]["pane"], man["sessions"][0]["tabTitle"]), ("G", "t"))


class TestOnlyAnITerm2RowCarriesAPane(unittest.TestCase):
    """A pane id is iTerm2's. A save that could not ask iTerm2 carries the pane
    the last save knew - never onto a row another app shows (review of
    slices 2-3)."""

    # the same process as TestACarriedPaneIsText.ROW: only the app differs
    KNOWN = {"s1": {"pane": "G-OLD", "tabTitle": "t", "tty": "ttys022", "pid": 4100}}

    def pane(self, terminal):
        row = dict(TestACarriedPaneIsText.ROW, terminal=terminal)
        return ccwho.manifest_from_rows([row], panes=None, known=self.KNOWN)["sessions"][0]["pane"]

    def test_a_terminal_app_row(self):
        self.assertEqual(self.pane("terminal"), "")

    def test_an_iterm2_row(self):                                           # control
        self.assertEqual(self.pane("iterm2"), "G-OLD")

    def test_a_row_no_app_shows_carries_it_as_before(self):                 # control
        self.assertEqual(self.pane(""), "G-OLD")


class TestASaveThatCannotAskReadsThePaneFromTheProcess(unittest.TestCase):
    """2026-10-03 19:24: iTerm2 did not answer one ask, and the gate asked it
    nothing more for two days. Every session started after that, and every one
    a /clear gave a new id, was saved with no pane: after the reboot 9 of 12
    opened in new windows. The pane a process runs in is in its environment
    (procs.parse_iterm_pane), so a save that cannot ask still knows it.

    `env_panes`: pid -> the pane its environment names. Only for a row ps
    places in iTerm2 - a terminal started from an iTerm2 shell may inherit the
    variable - and only when iTerm2 was not asked: its answer, when it gives
    one, is what is there now."""

    ROW = dict(TestACarriedPaneIsText.ROW, terminal="iterm2")
    KNOWN = TestOnlyAnITerm2RowCarriesAPane.KNOWN          # the same process, pane G-OLD

    def saved(self, row=None, panes=None, known=None, env_panes=None):
        man = ccwho.manifest_from_rows([dict(self.ROW, **(row or {}))], panes=panes,
                                       known=known, env_panes=env_panes)
        return man["sessions"][0]["pane"]

    def test_a_session_the_last_save_never_saw_gets_its_pane(self):
        self.assertEqual(self.saved(env_panes={"4100": "P-ENV"}), "P-ENV")

    def test_its_process_beats_the_last_save(self):
        # /clear: the same process, a new session id - and a pane the carry,
        # by session id, would not find
        self.assertEqual(self.saved(known=self.KNOWN, env_panes={"4100": "P-ENV"}), "P-ENV")

    def test_a_pid_read_back_as_text_still_matches(self):
        self.assertEqual(self.saved(row={"pid": "4100"}, env_panes={"4100": "P-ENV"}), "P-ENV")

    def test_no_pane_in_its_environment_keeps_the_carry(self):              # control
        self.assertEqual(self.saved(known=self.KNOWN, env_panes={"4100": ""}), "G-OLD")
        self.assertEqual(self.saved(known=self.KNOWN, env_panes={}), "G-OLD")

    def test_another_process_s_pane_is_not_taken(self):
        self.assertEqual(self.saved(env_panes={"5200": "P-ENV"}), "")

    def test_a_terminal_app_row_takes_none(self):
        self.assertEqual(self.saved(row={"terminal": "terminal"}, env_panes={"4100": "P-ENV"}), "")

    def test_a_row_no_app_shows_takes_none(self):
        # VS Code's terminal, tmux: started from an iTerm2 shell, they keep its
        # ITERM_SESSION_ID - and a restore would write into that pane
        self.assertEqual(self.saved(row={"terminal": ""}, env_panes={"4100": "P-ENV"}), "")

    def test_a_pane_that_is_not_text_is_not_taken(self):
        self.assertEqual(self.saved(env_panes={"4100": ["P-ENV"]}), "")

    def test_iterm2_s_answer_wins_when_it_gave_one(self):
        asked = {"ttys022": {"pane": "G-ASKED", "name": "t"}}
        self.assertEqual(self.saved(panes=asked, env_panes={"4100": "P-ENV"}), "G-ASKED")

    def test_iterm2_s_answer_wins_when_it_names_no_pane(self):
        self.assertEqual(self.saved(panes={}, env_panes={"4100": "P-ENV"}), "")


class TestATableFromAPsThatFailedIsNone(unittest.TestCase):
    """A ps that died part way prints part of the table: an iTerm2 missing
    from it is not gone. Anything but a clean exit reads as no table."""

    def run_ps(self, returncode, stdout):
        class R:
            pass
        R.returncode, R.stdout = returncode, stdout
        real = ccwho.subprocess.run
        self.addCleanup(setattr, ccwho.subprocess, "run", real)
        ccwho.subprocess.run = lambda *a, **k: R()
        return ccwho.terms.app_snapshot()

    def test_killed_part_way(self):
        self.assertEqual(self.run_ps(-9, "  PID UID UCOMM\n1 0 launchd\n"), "")

    def test_failed(self):
        self.assertEqual(self.run_ps(1, "  PID UID UCOMM\n1 0 launchd\n"), "")

    def test_a_clean_exit(self):                                                # control
        self.assertEqual(self.run_ps(0, "  PID UID UCOMM\n1 0 launchd\n"), "  PID UID UCOMM\n1 0 launchd\n")


class TestCollectSamplesAFewAScan(MachinelessCollect):
    """STACK_PER_SCAN is one scan's - every session's together - not each
    session's: a sample takes about 1.5 s, and a fleet has many sessions
    (review 1). No session waits for ever: what was sampled is not due."""
    OTHER = "bbbb2222-0000-4000-8000-000000000002"
    PS = ("  10     1 claude\n  60     1 claude\n"
          "  50    10 /bin/zsh -c source /u/.claude/shell-snapshots/s.sh && eval 'x'\n"
          "  51    50 python3 a.py\n  52    50 python3 b.py\n  53    50 python3 e.py\n"
          "  80    60 /bin/zsh -c source /u/.claude/shell-snapshots/s.sh && eval 'y'\n"
          "  81    80 python3 c.py\n  82    80 python3 d.py\n")
    LEAVES = [51, 52, 53, 81, 82]       # one session with more than a scan's samples

    def setUp(self):
        super().setUp()
        sessions = [{"pid": 10, "sessionId": LIVE, "cwd": "/Users/x/p/app", "status": "idle"},
                    {"pid": 60, "sessionId": self.OTHER, "cwd": "/Users/x/p/lib", "status": "idle"}]
        testkit.patch(self, ccwho, "agents_json", lambda: json.dumps(sessions))
        testkit.patch(self, ccwho, "read_procargs", lambda pid: {})
        ccwho.ps_snapshot = lambda: self.PS
        ccwho.ps_table = lambda: {pid: (START, "claude" if pid in (10, 60) else "python3")
                                  for pid in (10, 60, 50, 80, *self.LEAVES)}
        ccwho.stdin_sockets = lambda pids: {
            10: {"16": ("0xa", "->0xb")}, 60: {"16": ("0xc", "->0xd")},
            51: {"0": ("0xb", "->0xa")}, 52: {"0": ("0xb", "->0xa")}, 53: {"0": ("0xb", "->0xa")},
            81: {"0": ("0xd", "->0xc")}, 82: {"0": ("0xd", "->0xc")}}
        out = ("w", "REG", "/x/task.output")
        testkit.patch(self, ccwho, "open_fds", lambda pids: {
            p: {"0": ("u", "unix", "->x"), "1": out, "2": out} for p in pids})
        self.samples = []
        testkit.patch(self, ccwho, "stack_sample", lambda pid: self.samples.append(pid) or True)

    def scans(self, cache):
        found = []
        for _ in range(3):
            n = len(self.samples)
            rows, _ = ccwho.collect(cache=cache() if callable(cache) else cache)
            self.assertLessEqual(len(self.samples) - n, ccwho.STACK_PER_SCAN)
            found = sorted(d["pid"] for r in rows for d in r["dead_loops"])
        self.assertEqual((sorted(self.samples), found), (self.LEAVES, self.LEAVES))

    def test_the_list_samples_a_few_a_scan(self):
        self.scans({})                  # one cache for every scan, as the list keeps

    def test_ccwho_ls_samples_a_few_a_run(self):
        self.scans(dict)                # a new cache each run: the file carries on

    def test_a_scan_that_shows_no_stuck_work_takes_no_sample(self):
        # ccwho save, open, restore: no row of theirs shows a stuck item (review 2)
        listed = []
        ccwho.open_fds = lambda pids: listed.append(pids) or {}
        ccwho.collect(cache={}, samples=0)
        self.assertEqual((self.samples, listed), ([], []))

    def test_the_row_names_the_executable(self):
        # ps -o comm (argv[0], spaces and all): the command's first word is
        # "/Users/x/Application"
        app = "/Users/x/Application Support/bin/python3"
        ccwho.ps_snapshot = lambda: self.PS.replace("51    50 python3 a.py", f"51    50 {app} a.py")
        table = ccwho.ps_table()
        ccwho.ps_table = lambda: {**table, 51: (START, app)}
        rows, _ = ccwho.collect(cache={})
        named = {d["pid"]: d["program"] for r in rows for d in r["dead_loops"]}
        self.assertEqual(named.get(51), "python3", named)


class TestNoPsReaderTrustsAPsThatFailed(unittest.TestCase):
    """Every process table ccwho reads from ps: a ps that did not exit cleanly
    may have printed part of it - taken as no table at all."""

    def run_ps(self, fn, returncode, stdout):
        class R:
            pass
        R.returncode, R.stdout, R.stderr = returncode, stdout, ""
        real = ccwho.subprocess.run
        self.addCleanup(setattr, ccwho.subprocess, "run", real)
        ccwho.subprocess.run = lambda *a, **k: R()
        return fn()

    ROWS = "  PID TTY UID UCOMM\n  100 ttys001 501 iTerm2\n"

    def test_each_of_them(self):
        for fn in (ccwho.tty_snapshot, ccwho.ps_snapshot):
            self.assertEqual(self.run_ps(fn, -9, self.ROWS), "", fn.__name__)
        self.assertEqual(self.run_ps(ccwho.idle_snapshot, 1, "  PID TTY COMMAND\n  1 ttys001 -zsh\n"), set())
        self.assertEqual(self.run_ps(REAL["ps_table"], 1, "  1 Tue Sep 22 14:45:15 2026 /sbin/launchd\n"), {})

    def test_a_clean_exit(self):                                                # control
        self.assertEqual(self.run_ps(ccwho.tty_snapshot, 0, self.ROWS), self.ROWS)


class TestTheGateSaysAnUnreadableTableAsOne(unittest.TestCase):
    """A process table that could not be read is not "no iTerm2": that sends
    the user to the Automation settings for nothing."""

    _F = TestBackgroundAsksNeverPileUpInITerm2
    stub, launches, table, state, ask, until, ended = (
        _F.stub, _F.launches, _F.table, _F.state, _F.ask, _F.until, _F.ended)
    setUp = TestTheGateAfterASlowOrFailedAsk.setUp

    def test_an_empty_table(self):
        self.stub("echo ok")
        why = {}
        self.assertIsNone(ccwho.terms.ITERM2.ask(["-e", "whatever"], procs="", why=why))
        self.assertEqual(why.get("refused"), "no-table")

    def test_a_table_with_no_iterm2(self):                                      # control
        self.stub("echo ok")
        why = {}
        self.assertIsNone(ccwho.terms.ITERM2.ask(["-e", "whatever"], procs=apps((1, 0, "launchd")), why=why))
        self.assertEqual(why.get("refused"), "not-running")

    def test_a_lock_that_cannot_work(self):
        # flock failing for no other holder: not "busy", and no wait
        self.stub("echo ok")
        real = ccwho.terms.fcntl

        class NoFlock:
            LOCK_EX, LOCK_NB, LOCK_SH = fcntl.LOCK_EX, fcntl.LOCK_NB, fcntl.LOCK_SH

            def flock(self, fd, op):
                raise OSError(errno.ENOTSUP, "not supported")
        ccwho.terms.fcntl = NoFlock()
        self.addCleanup(setattr, ccwho.terms, "fcntl", real)
        why, t = {}, time.monotonic()
        self.assertIsNone(self.ask(wait=2.0, why=why))
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual(why.get("refused"), "io")


class TestTheGateWritesOneIterm2AsOnePid(unittest.TestCase):
    """A gate reader started before lists (the hotkey panel keeps its engine)
    keeps an int: with one iTerm2 of ours - the usual case - the gate writes
    one."""

    _F = TestBackgroundAsksNeverPileUpInITerm2
    stub, launches, table, state, ask, until, ended = (
        _F.stub, _F.launches, _F.table, _F.state, _F.ask, _F.until, _F.ended)
    setUp = TestTheGateAfterASlowOrFailedAsk.setUp

    def asked(self, procs):
        self.stub("echo 'execution error: AppleEvent timed out. (-1712)' >&2; exit 1")
        ccwho.terms.ITERM2.ask(["-e", "whatever"], timeout=10.0, procs=procs)
        self.assertTrue(self.until(lambda: ccwho.terms.ITERM2.ask(["-e", "x"], timeout=10.0, procs=procs) is None
                                   and ccwho.terms._read_gate(ccwho.terms.ITERM2, time.time()).get("quarantine") is not None))
        return ccwho.terms._read_gate(ccwho.terms.ITERM2, time.time())["quarantine"]

    def test_one(self):
        self.assertEqual(self.asked(apps((100, ME, IT))), 100)

    def test_two(self):                                                          # control
        self.assertEqual(self.asked(apps((100, ME, IT), (200, ME, IT))), [100, 200])


class TestTheListsSearchFindsEndedSessions(unittest.TestCase):
    """A session you stopped is not in the live list, so no part of the id its
    resume line printed found anything there (the owner, 2026-10-04: a resume id
    copied wrong, and its prefix could not be searched). While you search, the
    ended sessions the index knows show under the running ones, matched as
    `ccwho ls` matches them: every word, in its names and what it was about."""

    NOW = "2026-10-04T12:00:00.000Z"
    SID = "10fe9603-70dd-459c-96dc-05a176242f56"
    OTHER = "aaaa1111-0000-4000-8000-000000000001"

    def entry(self, sid=SID, **kw):
        e = {"sessionId": sid, "project": "liveapp", "cwd": "/Users/x/liveapp",
             "title": "liveapp pull request 526 CI tests", "recap": "the CI flake, fixed",
             "recap_ts": "2026-09-27T16:00:00.000Z", "turns_since_recap": 2,
             "you_said": "fix the CI", "opened": "fix the CI", "last_any": "",
             "last_ts": "2026-09-27T16:29:54.470Z", "entrypoint": "cli",
             "path": f"/Users/x/.claude/projects/-Users-x-liveapp/{sid}.jsonl"}
        e.update(kw)
        return e

    def index(self, *entries):
        return {e["sessionId"]: e for e in entries}

    def ended(self, idx, query, live=()):
        return ccwho.ui_ended_groups(idx, query, list(live), self.NOW)

    def ids(self, groups):
        return [r["sessionId"] for g in groups for r in g["rows"]]

    def test_any_part_of_the_id_finds_it(self):
        idx = self.index(self.entry(), self.entry(self.OTHER, title="other", recap="",
                                                  you_said="", opened="", project="marketing"))
        for q in ("10fe", "fe9603", "70dd-459c", self.SID, "10FE96"):
            with self.subTest(q=q):
                groups = self.ended(idx, q)
                self.assertEqual([g["heading"] for g in groups], ["ENDED"])
                self.assertEqual(self.ids(groups), [self.SID])
        self.assertEqual(self.ids(self.ended(idx, "aaaa11")), [self.OTHER])    # control

    def test_every_word_matches_as_ls_matches(self):
        idx = self.index(self.entry())
        for q in ("CI flake", "pull 526", "liveapp"):
            with self.subTest(q=q):
                self.assertEqual(self.ids(self.ended(idx, q)), [self.SID])
        self.assertEqual(self.ended(idx, "pull docker"), [], "every word has to match")

    def test_a_running_session_is_not_also_ended(self):
        idx = self.index(self.entry())
        self.assertEqual(self.ended(idx, "10fe", live=[{"sessionId": self.SID}]), [])
        # a terminal that parked it (ctrl+b) shows it: it runs
        parked = {"sessionId": self.OTHER, "parked": [self.SID]}
        self.assertEqual(self.ended(idx, "10fe", live=[parked]), [])
        self.assertEqual(self.ids(self.ended(idx, "10fe", live=[{"sessionId": self.OTHER}])),
                         [self.SID])                                          # control

    def test_no_search_shows_no_ended_session(self):
        idx = self.index(self.entry())
        for q in ("", "   ", None):
            with self.subTest(q=q):
                self.assertEqual(self.ended(idx, q), [])
        for empty in ({}, None):
            with self.subTest(index=empty):
                self.assertEqual(self.ended(empty, "10fe"), [])

    def test_a_programs_session_is_left_out_as_ls_leaves_it_out(self):
        self.assertEqual(self.ended(self.index(self.entry(entrypoint="sdk-cli")), "10fe"), [])
        self.assertEqual(self.ids(self.ended(self.index(self.entry(entrypoint="cli")), "10fe")),
                         [self.SID])                                          # control

    def sessions(self, n):
        return self.index(*[self.entry(f"{i:08x}-0000-4000-8000-000000000000",
                                       last_ts=f"2026-09-{10 + i:02d}T00:00:00.000Z")
                            for i in range(n)])

    def test_a_long_list_shows_the_newest_ten_and_says_how_many(self):
        groups = self.ended(self.sessions(12), "liveapp")
        self.assertEqual(len(groups), 1)
        rows = groups[0]["rows"]
        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[0]["sessionId"], f"{11:08x}-0000-4000-8000-000000000000")
        self.assertIn("10 of 12", groups[0]["heading"])
        self.assertTrue(groups[0]["heading"].startswith("ENDED"))
        # the list's header counts them all, not the ten shown
        self.assertEqual(groups[0]["total"], 12)
        self.assertTrue(groups[0]["ended"])
        self.assertEqual(self.ended(self.sessions(10), "liveapp")[0]["heading"], "ENDED")  # control

    def test_the_ended_group_does_not_need_you(self):
        self.assertFalse(self.ended(self.index(self.entry()), "10fe")[0]["needs_you"])

    def test_its_row_says_it_ended_and_what_it_was_about(self):
        row = self.ended(self.index(self.entry()), "10fe")[0]["rows"][0]
        self.assertEqual(row["attention"], "ended")
        self.assertEqual(row["cwd"], "/Users/x/liveapp")
        first, second = ccwho.ui_row_lines(row, width=120)
        self.assertIn("10fe", first)
        self.assertIn("ended 6d ago", first)
        self.assertNotIn("no window", first)
        self.assertIn("the CI flake, fixed", second)
        self.assertIn("6d", second, "the recap's age")

    def test_no_recap_shows_what_you_said(self):
        row = self.ended(self.index(self.entry(recap="", recap_ts="")), "10fe")[0]["rows"][0]
        self.assertIn("fix the CI", ccwho.ui_row_lines(row, width=120)[1])

    def test_a_running_session_the_index_finds_is_found_running(self):
        # review 2: a word only the index has (what you said in it) - `ccwho ls`
        # shows such a session as running, and so does the list
        idx = self.index(self.entry(you_said="flamingo deploy"),
                         self.entry(self.OTHER, title="other", recap="", you_said="", opened=""))
        live, other = {"sessionId": self.SID}, {"sessionId": self.OTHER}
        self.assertEqual(ccwho.ui_found_running(idx, "flamingo", [other, live]), [live])
        parked = {"sessionId": "cccc3333-0000-4000-8000-000000000003", "parked": [self.SID]}
        self.assertEqual(ccwho.ui_found_running(idx, "flamingo", [parked]), [parked])
        self.assertEqual(ccwho.ui_found_running(idx, "pelican", [other, live]), [])   # control
        for q in ("", None):
            self.assertEqual(ccwho.ui_found_running(idx, q, [live]), [])
        self.assertEqual(ccwho.ui_found_running(None, "flamingo", [live]), [])

    # the grep of what was said (ccwho_index.grep): the ids it found go to a
    # SAID group of their own, last - never among the name matches (review 1)
    def said(self, idx, query, live=(), found=(), limit=10):
        # found: the grep's {id: Said}; a set of ids (no Said) is taken too
        return ccwho.ui_said_group(idx, query, list(live), found, self.NOW, limit=limit)

    def test_an_ended_session_found_only_by_what_was_said(self):
        idx = self.index(self.entry())                 # no "flamingo" in its names
        groups = self.said(idx, "flamingo", found={self.SID})
        self.assertEqual([g["heading"] for g in groups], ["SAID"])
        self.assertEqual(self.ids(groups), [self.SID])
        self.assertEqual(groups[0]["rows"][0]["attention"], "ended")
        self.assertFalse(groups[0]["needs_you"])
        self.assertTrue(groups[0]["said"])
        self.assertEqual(self.said(idx, "flamingo"), [])                      # control
        self.assertEqual(ccwho.ui_ended_groups(idx, "flamingo", [], self.NOW), [],
                         "not under ENDED")

    def test_a_running_session_found_only_by_what_was_said_is_its_own_row(self):
        idx = self.index(self.entry())
        live = {"sessionId": self.SID, "title": "x", "attention": "busy"}
        groups = self.said(idx, "flamingo", live=[live], found={self.SID})
        self.assertIs(groups[0]["rows"][0], live, "the running row, with its state")
        parked = {"sessionId": self.OTHER, "parked": [self.SID], "attention": "busy"}
        self.assertIs(self.said(idx, "flamingo", live=[parked], found={self.SID})[0]["rows"][0],
                      parked)

    def test_a_session_that_matches_by_name_is_not_also_said(self):
        named = self.entry(self.OTHER, title="flamingo notes")
        idx = self.index(self.entry(), named)
        self.assertEqual(self.ids(self.said(idx, "flamingo", found={self.SID, self.OTHER})),
                         [self.SID], "OTHER shows under ENDED by its title")
        live = {"sessionId": "cccc3333-0000-4000-8000-000000000003", "title": "flamingo run"}
        self.assertEqual(self.said(idx, "flamingo", live=[live], found={live["sessionId"]}), [],
                         "it shows in its group by its title")
        yours = self.index(self.entry(you_said="flamingo deploy"))
        live = {"sessionId": self.SID}
        self.assertEqual(self.said(yours, "flamingo", live=[live], found={self.SID}), [],
                         "the index finds it by what you said: in its group")

    def test_running_first_then_the_newest_ten_and_how_many(self):
        idx = self.sessions(12)
        live = {"sessionId": f"{3:08x}-0000-4000-8000-000000000000", "attention": "busy"}
        groups = self.said(idx, "flamingo", live=[live], found=set(idx))
        rows = groups[0]["rows"]
        self.assertIs(rows[0], live)
        self.assertEqual(rows[1]["sessionId"], f"{11:08x}-0000-4000-8000-000000000000")
        self.assertEqual(len(rows), 10)
        self.assertEqual(groups[0]["total"], 12)
        self.assertIn("10 of 12", groups[0]["heading"])
        self.assertEqual(self.said(self.sessions(10), "flamingo",
                                   found=set(self.sessions(10)))[0]["heading"], "SAID")  # control

    def test_nothing_said_without_a_search(self):
        idx = self.index(self.entry())
        for q in ("", "  ", None):
            self.assertEqual(self.said(idx, q, found={self.SID}), [])
        self.assertEqual(self.said(idx, "flamingo", found={"not-in-the-index"}), [])

    # what was said that matched (ccwho_index.grep's Said): on the row, and the
    # order - the owner's session was 3rd of 44, by a title about something else
    def s(self, key=(1, True, 1), text="it hasn't woken the loop", who="you",
          ts="2026-10-04T10:00:00.000Z"):
        return {"text": text, "who": who, "ts": ts, "key": key}

    def sid(self, i):
        return f"{i:08x}-0000-4000-8000-000000000000"

    def test_a_said_row_carries_what_was_said(self):
        idx = self.index(self.entry())
        row = self.said(idx, "wake", found={self.SID: self.s()})[0]["rows"][0]
        self.assertEqual(row["said"], {"text": "it hasn't woken the loop", "who": "you",
                                       "age": "2h"})
        row = self.said(idx, "wake", found={self.SID: self.s(ts="")})[0]["rows"][0]
        self.assertEqual(row["said"]["age"], "?", "an age not known says so")

    def test_ids_without_a_said_show_without_one(self):
        # review-plan1 F3: never a raise - it would cost ENDED and the running rows
        idx = self.index(self.entry())
        for found in ({self.SID: None}, {self.SID}):
            with self.subTest(found=found):
                rows = self.said(idx, "wake", found=found)[0]["rows"]
                self.assertEqual(rows[0]["sessionId"], self.SID)
                self.assertFalse(rows[0].get("said"))

    def test_the_closest_first(self):
        # (a) more words in one message, (b) yours, (c) as typed, then (e) newest
        idx = self.sessions(5)
        keys = {0: (2, False, 0), 1: (1, True, 1), 2: (1, True, 0), 3: (1, False, 1),
                4: (1, False, 1)}
        found = {self.sid(i): self.s(key=k) for i, k in keys.items()}
        rows = self.said(idx, "wake", found=found)[0]["rows"]
        self.assertEqual([r["sessionId"] for r in rows],
                         [self.sid(0), self.sid(1), self.sid(2), self.sid(4), self.sid(3)])

    def test_running_before_ended_only_when_as_close(self):
        idx = self.sessions(2)
        live = {"sessionId": self.sid(0), "attention": "busy"}            # the older one
        found = {self.sid(0): self.s(), self.sid(1): self.s()}
        rows = self.said(idx, "wake", live=[live], found=found)[0]["rows"]
        self.assertEqual([r["sessionId"] for r in rows], [self.sid(0), self.sid(1)])
        self.assertEqual(rows[0]["attention"], "busy", "the running row, with its state")
        self.assertEqual(rows[0]["said"]["text"], "it hasn't woken the loop")
        found[self.sid(0)] = self.s(key=(1, False, 1))                       # control
        rows = self.said(idx, "wake", live=[live], found=found)[0]["rows"]
        self.assertEqual([r["sessionId"] for r in rows], [self.sid(1), self.sid(0)])

    def test_a_running_row_can_fall_past_the_ten_shown(self):
        # review-plan1 F5, the owner's decision D1: closeness before running
        idx = self.sessions(12)
        live = {"sessionId": self.sid(11), "attention": "busy"}
        found = {self.sid(i): self.s() for i in range(11)}
        found[self.sid(11)] = self.s(key=(1, False, 1))
        group = self.said(idx, "loop", live=[live], found=found)[0]
        self.assertNotIn(self.sid(11), [r["sessionId"] for r in group["rows"]])
        self.assertEqual(group["total"], 12)
        self.assertIn("10 of 12", group["heading"])

    def test_a_parked_row_shows_what_its_parked_session_said(self):
        # review-plan1 F4: the best over every id the row stands for
        idx = self.index(self.entry(), self.entry(self.OTHER, title="other", recap="",
                                                  you_said="", opened=""))
        parked = {"sessionId": self.OTHER, "parked": [self.SID], "attention": "busy"}
        found = {self.OTHER: self.s(key=(1, False, 0), text="its own"),
                 self.SID: self.s(key=(2, True, 2), text="from the parked one")}
        rows = self.said(idx, "wake merge", live=[parked], found=found)[0]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sessionId"], self.OTHER)
        self.assertEqual(rows[0]["said"]["text"], "from the parked one")
        del found[self.SID]                                                  # control
        rows = self.said(idx, "wake merge", live=[parked], found=found)[0]["rows"]
        self.assertEqual(rows[0]["said"]["text"], "its own")

    ODD = "wake \u009b2J\u009d0;title\u0007 \x0e\x01\x7f done"

    def test_no_control_character_reaches_a_row(self):
        # review-build1 F3: every field of a live row goes through
        # procs.printable; what was said, and an ended row, must too
        controls = re.compile(r"[\x00-\x1f\x7f-\x9f]")
        idx = self.index(self.entry())
        row = self.said(idx, "wake", found={self.SID: self.s(text=self.ODD)})[0]["rows"][0]
        second = ccwho.ui_row_lines(row, width=140)[1]
        self.assertIsNone(controls.search(second), repr(second))
        self.assertIn("wake", second)
        self.assertIn("done", second)
        odd = self.index(self.entry(recap=self.ODD, you_said=self.ODD, title=self.ODD))
        ended = self.ended(odd, "10fe")[0]["rows"][0]
        for line in ccwho.ui_row_lines(ended, width=140):
            self.assertIsNone(controls.search(line), repr(line))
        for field in ("recap", "doing", "topic", "title"):
            self.assertIsNone(controls.search(ended[field]), field)
        plain = self.said(idx, "wake", found={self.SID: self.s()})[0]["rows"][0]       # control
        self.assertEqual(plain["said"]["text"], "it hasn't woken the loop")

    def test_running_rows_the_index_has_no_time_for_go_by_their_own(self):
        # review-build1 F5 (plan F20): the index's last_ts first, else the row's ts
        idx = self.index(self.entry(self.sid(0), last_ts=""), self.entry(self.sid(1), last_ts=""))
        old = {"sessionId": self.sid(0), "attention": "busy", "ts": "2026-10-04T09:00:00.000Z"}
        new = {"sessionId": self.sid(1), "attention": "busy", "ts": "2026-10-04T11:00:00.000Z"}
        found = {self.sid(0): self.s(), self.sid(1): self.s()}
        rows = self.said(idx, "wake", live=[old, new], found=found)[0]["rows"]
        self.assertEqual([r["sessionId"] for r in rows], [self.sid(1), self.sid(0)])
        idx[self.sid(0)]["last_ts"] = "2026-10-04T11:30:00.000Z"                    # control
        rows = self.said(idx, "wake", live=[old, new], found=found)[0]["rows"]
        self.assertEqual([r["sessionId"] for r in rows], [self.sid(0), self.sid(1)])

    def test_the_second_line_is_what_was_said(self):
        idx = self.index(self.entry())
        row = self.said(idx, "wake", found={self.SID: self.s()})[0]["rows"][0]
        second = ccwho.ui_row_lines(row, width=120)[1]
        self.assertIn("2h · you: it hasn't woken the loop", second)
        self.assertNotIn("the CI flake", second, "not the recap")
        row = self.said(idx, "wake", found={self.SID: self.s(who="claude")})[0]["rows"][0]
        self.assertIn("2h · Claude: it hasn't woken", ccwho.ui_row_lines(row, width=120)[1])
        plain = self.ended(idx, "10fe")[0]["rows"][0]                          # control
        self.assertIn("the CI flake", ccwho.ui_row_lines(plain, width=120)[1])
