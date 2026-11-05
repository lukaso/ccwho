"""An ask stays open until you answer it. Stdlib only: python3 -m unittest test_asks

A session asked its person to choose, and its last line was the plan's path. Another
session then messaged it, and it replied and wrote "I am still waiting for your
choice ... Nothing is built until you answer." Neither message read as an ask: the
row said FINISHED, then STOPPED, and the choice waited six hours.

The record shapes here are copied from 21 days of real transcripts: a person's prompt
has origin.kind "human"; `/compact` and an interrupt have no origin at all; a
scheduled prompt is isMeta with no origin; a usage-limit line is isApiErrorMessage.
"""
import json
import os
import shutil
import tempfile
import unittest

import ccwho_engine as ccwho
import testkit

REAL_ASKS_PATH = getattr(ccwho, "asks_path", None)


def said(text, **extra):
    return {"type": "assistant", **extra,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def tool_use(name="Bash", tool_id="t1"):
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": {}}]}}


def user(content, **extra):
    return {"type": "user", **extra, "message": {"role": "user", "content": content}}


def human(text, source="typed"):
    return user(text, promptSource=source, origin={"kind": "human"})


def queued(prompt, origin, mode="prompt"):
    """What Claude Code writes for a message that arrives while a turn runs."""
    return {"type": "attachment", "attachment": {
        "type": "queued_command", "prompt": prompt, "commandMode": mode, "origin": origin}}


def answered(tool, tool_id="q1", error=False):
    """A tool call and its result, as Claude Code writes them."""
    result = {"type": "tool_result", "tool_use_id": tool_id, "content": "Your questions were answered."}
    if error:
        result["is_error"] = True
    return [tool_use(tool, tool_id), user([result], toolUseResult={"answers": {}})]


END = {"type": "system", "subtype": "turn_duration"}
STOP = {"type": "system", "subtype": "stop_hook_summary"}

LIMIT = said("You've hit your monthly spend limit · raise it at "
             "https://claude.ai/settings/usage?from=ccclilimitmessage · "
             "your session limit resets 1:40pm (Europe/London)", isApiErrorMessage=True)

COMPACT_DONE = [
    user("/compact"),
    {"type": "system", "subtype": "compact_boundary"},
    user("This session is being continued from a previous conversation.",
         isCompactSummary=True),
    user("<local-command-caveat>The command below was run directly in Claude Code."
         "</local-command-caveat>"),
    user("<command-name>/compact</command-name>\n<command-message>compact</command-message>"),
    user("<local-command-stdout>Compacted (ctrl+o to see full summary)</local-command-stdout>"),
]

# what starts a turn, or lands in a transcript, with no person answering
NOT_A_PERSON = {
    "a peer's message": [user(
        'Another Claude session sent a message:\n<cross-session-message from="u" '
        'from-name="crm">Where is it tracked?</cross-session-message>',
        isMeta=True, promptSource="system", origin={"kind": "peer", "name": "crm"})],
    "a subagent's report": [user(
        'Another Claude session sent a message:\n<agent-message from="a8b">\n'
        "[Subagent hand-back] The report follows", isMeta=True, promptSource="system",
        origin={"kind": "peer", "name": "general-purpose"})],
    "a finished task": [user(
        "<task-notification>\n<task-id>b1</task-id>\n</task-notification>",
        promptSource="system", origin={"kind": "task-notification"})],
    "a scheduled prompt": [user("Daily scan (read-only): look at the four projects.",
                                isMeta=True, promptSource="system")],
    "the limit's reset": [user("Your claude.ai usage limit has reset. Continue the task.",
                               isMeta=True, promptSource="system",
                               origin={"kind": "auto-continuation"})],
    "an idle notice": [user('[Cross-session idle notice] "x", which you asked about, is idle',
                            isMeta=True, promptSource="system")],
    "a skill's text": [user("Base directory for this skill: /x", isMeta=True)],
    "a skill loaded again": [user("(Re-invocation of /verify: the skill was loaded before.)",
                                  isMeta=True)],
    "a program's prompt": [user("Review this change for security.", promptSource="sdk")],
    "a tool result": [user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])],
    "/compact while it runs": [user("/compact")],
    "/compact done": COMPACT_DONE,
    "an interrupt": [user("[Request interrupted by user]")],
    "a peer's message while it ran": [queued('<cross-session-message from="u">Hi',
                                              {"kind": "peer"})],
    "a finished task while it ran": [queued("<task-notification>", {"kind": "task-notification"},
                                            mode="task-notification")],
    "a finished task while it ran, with no origin": [queued("<task-notification>", None,
                                                            mode="task-notification")],
    "a tool's result": answered("Bash"),
    "a question you turned down": answered("AskUserQuestion", error=True),
    "a ! command": [user("<bash-input>ls</bash-input>"),
                    user("<bash-stdout>a</bash-stdout><bash-stderr></bash-stderr>")],
}

# what a person says, each way it is recorded
A_PERSON = {
    "typed": [human("B")],
    "typed while it ran": [human("B", source="queued")],
    "typed while it ran, as Claude Code writes it now": [
        queued("B", {"kind": "human"}), said("Noted."), END],
    "an answer to its question box": answered("AskUserQuestion") + [said("Noted."), END],
    "a plan you approved": answered("ExitPlanMode") + [said("Noted."), END],
    "a suggestion taken": [human("B", source="suggestion_accepted")],
    "a slash command": [user("<command-message>land</command-message>\n"
                             "<command-name>/land</command-name>", origin={"kind": "human"})],
    "a prompt from the desktop app": [human("B", source="sdk")],
    "a line that starts like a path": [human("/tmp is full")],
    "before Claude Code wrote an origin": [user("B")],
}


class TestAQuestionAboveTheLastLine(unittest.TestCase):
    """A real question often sits above a pointer: `Plan: <path>`, `Commit: 58bf966`.
    Measured over 21 days: 398 turns, and about 16 of 20 read were real questions."""

    def test_the_message_that_was_reported(self):
        text = ("**I have stopped work on the plan to ask you one question.** I am not "
                "building yet, because each review round finds one more place.\n\n"
                "**During onboarding, what should the agent be doing?**\n\n"
                "- **A: Give it separate, short instructions (my recommendation).**\n"
                "  - **Gain:** there is one switch.\n"
                "- **B: Keep the full instructions.**\n"
                "  - **Cost:** about 15 places so far.\n\n"
                "If someone asks for a re-review, I plan to record the request.\n\n"
                "The findings so far are saved in `/x/plans/PLAN-hold.md`.")
        self.assertEqual(ccwho.ask_line(text), "During onboarding, what should the agent be doing?")

    def test_a_question_with_its_reason_after_it_on_the_line(self):
        text = "- Do you want an issue for it? It would read the whole branch.\n\nPlan: /x/p.md"
        self.assertEqual(ccwho.ask_line(text),
                         "- Do you want an issue for it? It would read the whole branch.")

    def test_the_question_nearest_the_end_is_the_one_shown(self):
        text = "Is the first one right?\n\nIs the second one right?\n\nCommit: 58bf966."
        self.assertEqual(ccwho.ask_line(text), "Is the second one right?")

    def test_the_last_line_goes_first_when_it_asks(self):               # control
        text = "Is the first one right?\n\nWant me to land it?"
        self.assertEqual(ccwho.ask_line(text), "Want me to land it?")

    def test_a_last_line_that_asks_with_no_question_mark_goes_first(self):  # control
        text = "Is the first one right?\n\nTell me which and I land it."
        self.assertEqual(ccwho.ask_line(text), "Tell me which and I land it.")

    def test_a_message_with_no_question_asks_nothing(self):             # control
        self.assertEqual(ccwho.ask_line("Done.\n\nAll landed."), "")
        self.assertEqual(ccwho.ask_line(""), "")

    def test_asks_user_says_the_same(self):
        self.assertTrue(ccwho.asks_user("Should I do X?\n\nThe record is in /x/p.md."))
        self.assertFalse(ccwho.asks_user("I did X.\n\nThe record is in /x/p.md."))

    def test_what_is_not_a_question_to_you(self):
        for name, line in (("a draft you may send", "> Does the limit apply abroad?"),
                           ("a table row", "| Is it on? | yes |"),
                           ("a question in quotation marks", 'The reviewer asked "is it pending?" there.'),
                           ("a quotation that goes on after it", 'The brief says "is it on? Say so" for that.'),
                           ("a URL", "See https://x.y/z?from=a for it."),
                           ("code on the line", "It uses `${path:?}` for that."),
                           ("code in a line of code", "Use `is_on? ` in the check."),
                           ("a line with no sentence end", "The value of x ? y : z is used.")):
            with self.subTest(name):
                self.assertEqual(ccwho.ask_line(f"{line}\n\nDone."), "")
                # the same line next to a real one: the rule did not switch off
                self.assertEqual(ccwho.ask_line(f"Is this right?\n\n{line}\n\nDone."),
                                 "Is this right?")

    def test_a_fence_hides_its_lines(self):
        text = "```\nwhat is this?\n```\n\nDone."
        self.assertEqual(ccwho.ask_line(text), "")
        self.assertEqual(ccwho.ask_line("Is this right?\n\n" + text), "Is this right?")

    def test_a_question_with_code_in_it_still_asks(self):               # control
        self.assertEqual(ccwho.ask_line("Does `foo` work?\n\nDone."), "Does foo work?")

    def test_a_question_in_brackets_still_asks(self):                   # control
        self.assertEqual(ccwho.ask_line("Keep it (do you agree?) for now.\n\nDone."),
                         "Keep it (do you agree?) for now.")


class TestAWaitOnYouIsAnAsk(unittest.TestCase):
    """The last line says it waits for you, with no question mark. These are real
    closing lines. Bare "your go" stays out: most are about later ("then I ask for
    your go")."""

    def test_it_waits_for_you(self):
        for line in ("I am still waiting for your choice on #1096, A or B, from my last "
                     "message. Nothing is built until you answer.",
                     'The plan still waits for your "go" and your three decisions.',
                     "The notes still need your approval.",
                     "I need your OK.",
                     "The fix still needs your go.",
                     "Landing needs your go.",
                     "My earlier questions are still open. The most important is the drafts.",
                     "The question from my last message is still open.",
                     "Your choice on the tests (A, B or C) is still open.",
                     "I need you to run the line above.",
                     "I start when you answer.",
                     "Nothing is built until you answer.",
                     "I will not push again until you decide.",
                     "That change waits for you.",
                     "The session is waiting on you."):
            with self.subTest(line):
                self.assertTrue(ccwho.asks_user(line))

    def test_it_says_it_does_not(self):
        for line in ("Nothing needs your input yet.",
                     "Nothing waits for you right now.",
                     "You do not need your key for this.",
                     "It doesn't need your go.",
                     "Then I ask for your go to land it.",
                     'The brief says "waiting for your answer". It runs now.',
                     "One question stays open, and only a measurement can answer it.",
                     "It needs yours too.",
                     "Still open, not blocking: the spend labelling question.",
                     "I stay quiet unless something genuinely needs you.",
                     "From here some commands may wait for your approval.",
                     "If it needs your go, I say so.",
                     "The pull request is still open."):
            with self.subTest(line):
                self.assertFalse(ccwho.asks_user(line))

    def test_a_no_in_another_clause_does_not_count(self):               # control
        self.assertTrue(ccwho.asks_user("No change here, but the land needs your go."))
        self.assertTrue(ccwho.asks_user("Nothing needs your input. The land waits for your go."))


class TestAnOpenAskStaysOpen(unittest.TestCase):
    """The ask stands until you speak. 179 asks were replaced by a machine's turn in
    21 days. Then you replied after 353 min (median), against 103 min when the ask
    stayed on the last machine turn."""

    def asked(self):
        return [said("Which one?\n\nA or B?"), END]

    def test_nothing_a_machine_writes_clears_it(self):
        for name, records in NOT_A_PERSON.items():
            with self.subTest(name):
                got = ccwho.open_ask(self.asked() + records + [said("A status line."), END])
                self.assertEqual(got, (False, "A or B?"))

    def test_anything_a_person_says_clears_it(self):
        for name, records in A_PERSON.items():
            with self.subTest(name):
                self.assertEqual(ccwho.open_ask(self.asked() + records), (True, ""))

    def test_the_answer_clears_it_before_any_reply(self):
        self.assertEqual(ccwho.open_ask(self.asked() + [human("B")]), (True, ""))

    def test_a_new_ask_after_you_spoke_is_open(self):
        got = ccwho.open_ask(self.asked() + [human("B"), said("Land it now?"), END])
        self.assertEqual(got, (True, "Land it now?"))

    def test_the_newest_ask_is_the_one_shown(self):
        got = ccwho.open_ask(self.asked() + NOT_A_PERSON["a finished task"]
                             + [said("Or land it now?"), END])
        self.assertEqual(got, (False, "Or land it now?"))

    def test_a_message_of_a_turn_not_ended_goes_before_an_older_ask(self):
        self.assertEqual(ccwho.open_ask(self.asked() + [said("Or land it now?")]),
                         (False, "Or land it now?"))

    def test_the_newest_ask_stays_after_a_turn_that_asks_nothing(self):
        task = NOT_A_PERSON["a finished task"]
        got = ccwho.open_ask(self.asked() + task + [said("Or land it now?"), END]
                             + task + [said("Status."), END])
        self.assertEqual(got, (False, "Or land it now?"))

    def test_another_system_record_does_not_close_a_turn(self):
        got = ccwho.open_ask([said("Should I check the logs?"),
                              {"type": "system", "subtype": "api_retry"},
                              tool_use(), said("Checked them."), END]
                             + NOT_A_PERSON["a finished task"] + [said("Status."), END])
        self.assertEqual(got, (False, ""))

    def test_either_turn_end_record_closes_a_turn(self):
        for end in (END, STOP):
            with self.subTest(end["subtype"]):
                got = ccwho.open_ask([said("A or B?"), end]
                                     + NOT_A_PERSON["a finished task"] + [said("Status."), END])
                self.assertEqual(got, (False, "A or B?"))

    def test_a_question_inside_a_turn_is_not_carried(self):
        # only a turn's last message is said to you
        got = ccwho.open_ask([said("Should I check the logs?"), tool_use(), said("Checked them."),
                              END] + NOT_A_PERSON["a finished task"] + [said("Status."), END])
        self.assertEqual(got, (False, ""))

    def test_a_turn_not_yet_ended_still_reads_its_last_message(self):
        # 16 of 2251 ended turns have no end record
        self.assertEqual(ccwho.open_ask([said("Want me to do X?")]), (False, "Want me to do X?"))
        self.assertEqual(ccwho.open_ask([said("Want me to do X?"), said("Never mind, done.")]),
                         (False, ""))

    def test_no_messages(self):
        self.assertEqual(ccwho.open_ask([]), (False, ""))

    def test_it_reads_raw_lines_too(self):
        lines = [json.dumps(r) for r in self.asked()]
        self.assertEqual(ccwho.open_ask(lines), (False, "A or B?"))


class TestALimitMessageIsNotCarried(unittest.TestCase):
    """A limit line reads as an ask only for the `?` in its URL. As the newest message
    that stays. Carried, it would stay after the session recovered by itself."""

    TASK = NOT_A_PERSON["a finished task"]

    def test_it_ends_when_a_later_turn_ends(self):
        got = ccwho.open_ask([LIMIT, END] + self.TASK + [said("Status."), END])
        self.assertEqual(got, (False, ""))

    def test_as_the_newest_message_it_still_shows(self):                # control
        self.assertEqual(ccwho.open_ask([LIMIT, END])[1], ccwho.ask_line(LIMIT["message"]["content"][0]["text"]))
        self.assertTrue(ccwho.open_ask([LIMIT, END])[1])

    def test_a_real_ask_after_it_is_carried(self):
        got = ccwho.open_ask([LIMIT, END] + self.TASK + [said("Land it?"), END]
                             + self.TASK + [said("Status."), END])
        self.assertEqual(got, (False, "Land it?"))

    def test_a_real_ask_goes_before_it(self):
        self.assertEqual(ccwho.open_ask([said("A or B?"), END, LIMIT, END]), (False, "A or B?"))

    def test_a_real_ask_outlives_it(self):
        got = ccwho.open_ask([said("A or B?"), END, LIMIT, END] + self.TASK + [said("Status."), END])
        self.assertEqual(got, (False, "A or B?"))


class TestARowKeepsTheAsk(unittest.TestCase):

    def row(self, status, tail, **kw):
        return ccwho.build_row({"sessionId": "a", "status": status, "pid": 1}, "", tail, None, **kw)

    WOKEN = [said("A or B?"), END] + NOT_A_PERSON["a peer's message"] + [said("I told crm."), END]

    def test_a_peer_turn_does_not_take_the_row_out_of_asked_you(self):
        row = self.row("idle", self.WOKEN)
        self.assertEqual((row["attention"], row["ask"]), ("asks", "A or B?"))

    def test_nor_does_work_left_running(self):
        # this one was `running`, outside NEEDS YOU altogether
        row = self.row("idle", self.WOKEN, work=3)
        self.assertEqual((row["attention"], row["ask"]), ("asks", "A or B?"))

    def test_nor_a_feed_that_says_busy_with_the_turn_over(self):
        self.assertEqual(self.row("busy", self.WOKEN)["attention"], "asks")

    def test_nor_a_feed_that_says_waiting(self):
        self.assertEqual(self.row("waiting", self.WOKEN)["attention"], "asks")

    def test_mid_turn_it_is_busy(self):                                 # control
        tail = [said("A or B?"), END] + NOT_A_PERSON["a peer's message"]
        self.assertEqual(self.row("busy", tail)["attention"], "busy")

    def test_your_answer_ends_it_before_the_feed_catches_up(self):
        for status in ("idle", "waiting"):
            with self.subTest(status):
                row = self.row(status, self.WOKEN + [human("B")])
                self.assertNotEqual(row["attention"], "asks")
                self.assertEqual(row["ask"], "")

    def test_the_reported_session_from_start_to_end(self):
        asked = [said("**During onboarding, what should the agent be doing?**\n\n- A\n- B\n\n"
                      "The findings are saved in `/x/p.md`."), END]
        question = "During onboarding, what should the agent be doing?"
        self.assertEqual(self.row("idle", asked)["ask"], question)
        compacted = asked + COMPACT_DONE
        self.assertEqual(self.row("idle", compacted)["ask"], question)
        woken = compacted + NOT_A_PERSON["a peer's message"]
        quiet = woken + [said("I told crm it is not tracked."), END]
        self.assertEqual(self.row("idle", quiet)["ask"], question)
        # it says so again without a question mark: that line is the newest ask
        again = woken + [said("I am still waiting for your choice, A or B, from my last "
                              "message. Nothing is built until you answer."), END]
        row = self.row("idle", again)
        self.assertEqual(row["attention"], "asks")
        self.assertIn("still waiting for your choice", row["ask"])
        self.assertNotEqual(self.row("idle", again + [human("B")])["attention"], "asks")

    def test_an_older_ask_comes_in_when_the_tail_has_none(self):
        tail = [said("Status."), END]
        row = self.row("idle", tail, carried="A or B?")
        self.assertEqual((row["attention"], row["ask"]), ("asks", "A or B?"))

    def test_an_older_ask_is_out_once_you_spoke_in_the_tail(self):      # control
        row = self.row("idle", [human("B"), said("Done."), END], carried="A or B?")
        self.assertNotEqual(row["attention"], "asks")
        self.assertEqual(row["ask"], "")

    def test_a_newer_ask_in_the_tail_goes_first(self):                  # control
        row = self.row("idle", [said("Land it?"), END], carried="A or B?")
        self.assertEqual(row["ask"], "Land it?")

    def test_a_program_stays_a_program(self):
        row = ccwho.build_row({"sessionId": "a", "status": "idle", "pid": 1, "entrypoint": "sdk-cli"},
                              "", self.WOKEN, None)
        self.assertEqual(row["attention"], "program")

    def test_a_park_on_the_last_turn_still_parks_it(self):
        tail = [{**r, "timestamp": "2026-11-05T12:00:00Z"} for r in self.WOKEN]
        ts = ccwho.last_turn_ts(tail)
        row = self.row("idle", tail, parks={"a": {"ts": ts, "note": "later"}})
        self.assertEqual((row["attention"], row["park_was"]), ("parked", "asks"))


class AskBook(unittest.TestCase):
    """A real transcript in a temp folder. 1 MiB holds about 7 ended turns in a busy
    session, and about half of the carried asks lie further back."""

    SID = "aaaaaaaa-0000-0000-0000-000000000000"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ccwho-test-asks-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, f"{self.SID}.jsonl")
        testkit.patch(self, ccwho, "asks_path", lambda: os.path.join(self.dir, "asks.json"))
        testkit.patch(self, ccwho, "transcript_path", lambda sid, roots=None: self.path)
        real, self.deep = ccwho._records_further_back, []

        def counted(path, size):
            self.deep.append(size)
            return real(path, size)
        testkit.patch(self, ccwho, "_records_further_back", counted)
        self.write({"type": "permission-mode"})     # the tail never shows a file's first line

    def write(self, *records):
        with open(self.path, "a") as fh:
            for r in records:
                fh.write(json.dumps(r) + "\n")
        return os.path.getsize(self.path)

    def pad(self, count):
        """About `count` bytes of what fills a real transcript: records that say
        nothing about a turn. Claude Code's request copies are the largest."""
        chunk = 64 * 1024
        for _ in range(count // chunk + 1):
            self.write({"type": "api-request-blob", "blob": "x" * chunk})
        return os.path.getsize(self.path)

    def tail(self):
        return ccwho.read_windows(self.SID)[1]

    def look(self, book):
        return ccwho.carried_ask(self.SID, self.tail(), book)


class TestAnAskOlderThanTheTail(AskBook):

    def asked_then_filled(self):
        self.write(said("A or B?"), END)
        self.pad(ccwho.TAIL_BYTES + 1024)
        self.write(*NOT_A_PERSON["a finished task"], said("Status."), END)

    def test_the_first_look_reads_further_back_and_finds_it(self):
        self.asked_then_filled()
        self.assertEqual(ccwho.open_ask(self.tail()), (False, ""))      # the tail cannot see it
        book = {}
        self.assertEqual(self.look(book), "A or B?")
        self.assertEqual(len(self.deep), 1)
        self.assertEqual(book[self.SID]["ask"], "A or B?")

    def test_you_spoke_beyond_the_tail_so_it_is_closed(self):           # control
        self.write(said("A or B?"), END, human("B"), said("Done."), END)
        self.pad(ccwho.TAIL_BYTES + 1024)
        self.write(*NOT_A_PERSON["a finished task"], said("Status."), END)
        self.assertEqual(ccwho.open_ask(self.tail()), (False, ""))
        self.assertEqual(self.look({}), "")

    def test_the_next_look_does_not_read_back_again(self):
        self.asked_then_filled()
        book = {}
        self.look(book)
        self.write(said("Status."), END)
        self.assertEqual(self.look(book), "A or B?")
        self.assertEqual(len(self.deep), 1)

    def test_it_stays_after_the_tail_moved_past_it(self):
        self.write(said("A or B?"), END)
        book = {}
        self.assertEqual(self.look(book), "A or B?")
        for _ in range(20):                         # far past the tail, a look at each step
            self.pad(ccwho.ASKS_STEP // 2)
            self.write(said("Status."), END)
            self.assertEqual(self.look(book), "A or B?")
        self.assertEqual(ccwho.open_ask(self.tail()), (False, ""))      # the tail cannot see it
        self.assertEqual(self.deep, [])

    def test_a_question_inside_a_turn_is_not_kept(self):
        self.pad(ccwho.TAIL_BYTES + 1024)
        book = {}
        self.write(*NOT_A_PERSON["a finished task"], said("Should I check the logs?"), tool_use())
        self.look(book)                             # a scan while the turn runs
        self.write(user([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
                   said("Checked them."), END)
        self.assertEqual(self.look(book), "")
        self.assertEqual(book[self.SID]["ask"], "")

    def test_a_limit_line_is_not_kept(self):
        self.pad(ccwho.TAIL_BYTES + 1024)
        book = {}
        self.write(LIMIT, END)
        self.look(book)
        self.write(*NOT_A_PERSON["a finished task"], said("Status."), END)
        self.assertEqual(self.look(book), "")
        self.assertEqual(book[self.SID]["ask"], "")

    def walk_past_the_tail(self, book):
        for _ in range(6):                          # a look at each step, as the list does
            self.pad(ccwho.TAIL_BYTES // 4)
            self.look(book)

    def test_a_limit_line_does_not_replace_a_kept_ask(self):
        self.write(said("A or B?"), END)
        book = {}
        self.look(book)
        self.walk_past_the_tail(book)
        self.write(LIMIT, END)                      # the tail shows it, and not the ask
        self.look(book)
        self.walk_past_the_tail(book)
        self.write(*NOT_A_PERSON["a finished task"], said("Status."), END)
        self.assertEqual(self.look(book), "A or B?")
        self.assertEqual(book[self.SID]["ask"], "A or B?")

    def test_a_long_gap_reads_again_and_finds_your_answer(self):
        self.write(said("A or B?"), END)
        book = {}
        self.look(book)
        self.write(human("B"), said("Done."), END)  # no look while this went by
        self.pad(ccwho.TAIL_BYTES + 1024)
        self.write(said("Status."), END)
        self.assertEqual(self.look(book), "")
        self.assertEqual(len(self.deep), 1)

    def test_a_short_gap_needs_no_read(self):                           # control
        self.write(said("A or B?"), END)
        book = {}
        self.look(book)
        self.write(human("B"), said("Done."), END)
        self.assertEqual(self.look(book), "")
        self.assertEqual(self.deep, [])

    def test_a_newer_ask_in_the_tail_goes_before_the_books(self):
        size = self.pad(ccwho.TAIL_BYTES + 1024)
        book = {self.SID: {"ask": "An older ask?", "size": size}}
        self.write(said("A newer ask?"), END)
        self.assertEqual(self.look(book), "A newer ask?")
        self.assertEqual(self.deep, [])

    def test_a_person_in_the_tail_needs_no_read(self):
        self.pad(ccwho.TAIL_BYTES + 1024)
        self.write(human("Go on."), said("Done."), END)
        self.assertEqual(self.look({}), "")
        self.assertEqual(self.deep, [])

    def test_a_short_transcript_needs_no_read(self):
        self.write(said("Status."), END)
        self.assertEqual(self.look({}), "")
        self.assertEqual(self.deep, [])

    def test_a_transcript_shorter_than_at_the_last_look_is_read_again(self):
        self.asked_then_filled()
        size = os.path.getsize(self.path)
        book = {self.SID: {"ask": "An old ask?", "size": size + 1}}
        self.assertEqual(self.look(book), "A or B?")
        self.assertEqual(len(self.deep), 1)

    def test_it_reads_back_only_so_far(self):
        self.write(said("A or B?"), END)
        self.pad(ccwho.TAIL_BYTES * 2 + 1024)
        self.write(said("Status."), END)
        testkit.patch(self, ccwho, "ASKS_DEEP", ccwho.TAIL_BYTES + 1024)
        self.assertEqual(self.look({}), "")

    def test_it_reads_back_that_far(self):                              # control
        self.pad(1024)
        self.write(said("A or B?"), END)
        self.pad(ccwho.TAIL_BYTES * 2 + 1024)
        self.write(said("Status."), END)
        self.assertEqual(self.look({}), "A or B?")

    def test_a_transcript_that_appears_later_is_found(self):
        self.write(said("A or B?"), END)
        self.pad(ccwho.TAIL_BYTES + 1024)
        self.write(said("Status."), END)
        cache, there = {}, []
        testkit.patch(self, ccwho, "transcript_path",
                      lambda sid, roots=None: self.path if there else None)
        self.assertEqual(ccwho.carried_ask(self.SID, [], {}, cache=cache), "")
        there.append(True)                          # 12 to 67 s after the session file
        book = {}
        tail = ccwho.read_windows(self.SID)[1]
        self.assertEqual(ccwho.carried_ask(self.SID, tail, book, cache=cache), "A or B?")
        self.assertIn(self.SID, book)

    def test_no_transcript_is_no_ask(self):
        testkit.patch(self, ccwho, "transcript_path", lambda sid, roots=None: None)
        self.assertEqual(ccwho.carried_ask(self.SID, [], {}), "")

    def test_a_transcript_that_went_away_is_no_ask(self):
        os.remove(self.path)
        self.assertEqual(ccwho.carried_ask(self.SID, [], {}), "")


class TestTheBookIsWrittenOnlyWhenItMust(AskBook):

    def test_a_look_that_changes_nothing_leaves_it_alone(self):
        self.write(said("A or B?"), END)
        book = {}
        self.look(book)
        before = dict(book)
        self.look(book)
        self.assertEqual(book, before)

    def test_a_little_growth_leaves_it_alone(self):
        self.write(said("A or B?"), END)
        book = {}
        self.look(book)
        before = dict(book)
        self.write(said("Status."), END)
        self.look(book)
        self.assertEqual(book, before)

    def test_growth_by_a_step_moves_its_mark(self):
        self.write(said("A or B?"), END)
        book = {}
        self.look(book)
        size = self.pad(ccwho.ASKS_STEP)
        self.look(book)
        self.assertEqual(book[self.SID], {"ask": "A or B?", "size": size})

    def test_a_changed_ask_is_written_at_once(self):
        self.write(said("A or B?"), END)
        book = {}
        self.look(book)
        size = self.write(human("B"))
        self.look(book)
        self.assertEqual(book[self.SID], {"ask": "", "size": size})


class TestTheBookOnDisk(AskBook):

    def test_it_is_under_the_cache_folder(self):
        self.assertEqual(REAL_ASKS_PATH(), os.path.expanduser("~/.cache/ccwho/asks.json"))

    def test_it_comes_back_as_it_went(self):
        book = {self.SID: {"ask": "A or B?", "size": 12}}
        ccwho.save_asks(book)
        self.assertEqual(ccwho.load_asks(), book)

    def test_no_file_is_an_empty_book(self):
        self.assertEqual(ccwho.load_asks(), {})

    def test_a_bad_file_is_an_empty_book(self):
        with open(ccwho.asks_path(), "w") as fh:
            fh.write("{nope")
        self.assertEqual(ccwho.load_asks(), {})
        with open(ccwho.asks_path(), "w") as fh:
            fh.write("[1]")
        self.assertEqual(ccwho.load_asks(), {})

    def test_bad_entries_are_left_out(self):
        good = {"ask": "A?", "size": 3}
        with open(ccwho.asks_path(), "w") as fh:
            json.dump({"a": good, "b": {"ask": 1, "size": 3}, "c": {"ask": "A?", "size": "3"},
                       "d": {"ask": "A?"}, "e": 7, "f": {"ask": "A?", "size": True}}, fh)
        self.assertEqual(ccwho.load_asks(), {"a": good})

    def test_a_write_that_fails_raises_nothing(self):
        blocker = os.path.join(self.dir, "a-file")
        with open(blocker, "w") as fh:
            fh.write("x")
        ccwho.save_asks({}, path=os.path.join(blocker, "under", "asks.json"))

    def test_a_write_leaves_no_temp_file(self):
        before = set(os.listdir(self.dir))
        ccwho.save_asks({self.SID: {"ask": "", "size": 1}})
        self.assertEqual(set(os.listdir(self.dir)) - before, {"asks.json"})


if __name__ == "__main__":
    unittest.main()
