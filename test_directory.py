"""The directory between sessions: what each session is for, and how it reads to
another session. Rules (ccwho_directory.py), then the commands (ccwho.py).

Rows here are the shape engine.collect gives. No test reads the real sessions:
scan and live_file_sessions are stand-ins, and CCWHO_DIR is a temp dir.
"""
import contextlib
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import ccwho as runner
import ccwho_directory as directory
import testkit

SID_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SID_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SID_C = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
NOW = 1793570000.0


def setUpModule():
    global _undo
    _undo = testkit.pin_ccwho_dir(runner)


def tearDownModule():
    _undo()


def row(name="app-4e", source="derived", sid=SID_A, **kw):
    r = {"name": name, "nameSource": source, "sessionId": sid, "status": "idle",
         "title": "a title", "topic": "", "cache": "", "project": "app"}
    r.update(kw)
    return r


def entries(purpose=None, now=None):
    return {"purpose": {k: {"key": k, "text": v[0], "updated": v[1]}
                        for k, v in (purpose or {}).items()},
            "now": {k: {"key": k, "text": v[0], "updated": v[1]}
                    for k, v in (now or {}).items()}}


def merged(rows, ent=None, **kw):
    return directory.merge(rows, ent or entries(), lambda r: r.get("name") or r.get("project"),
                           now=NOW, **kw)


class TestClean(unittest.TestCase):
    """Text from one session lands in another's context: one printable line."""

    def test_a_newline_or_tab_is_a_space(self):
        self.assertEqual(directory.clean("two\nlines\tand  gaps"), "two lines and gaps")

    def test_escape_sequences_go(self):
        self.assertEqual(directory.clean("\x1b[31mred\x1b[0m"), "red")

    def test_a_control_character_does_not_reach_the_reader(self):
        self.assertNotIn("\x07", directory.clean("a\x07b"))
        self.assertNotIn("\x9b", directory.clean("a\x9bb"))

    def test_long_text_is_cut_to_the_limit(self):
        self.assertEqual(len(directory.clean("x" * 500)), directory.MAX_TEXT)

    def test_the_limit_is_what_the_plan_says(self):
        self.assertEqual(directory.MAX_TEXT, 120)

    def test_only_space_is_no_text(self):
        self.assertEqual(directory.clean(" \n\t "), "")

    def test_other_scripts_stay(self):                                  # control
        self.assertEqual(directory.clean("Zoë 日本語"), "Zoë 日本語")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ccwho-dir-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _put(self, kind, key, text, updated=NOW):
        with open(directory.path_of(self.dir, kind, key), "w") as fh:
            fh.write(directory.entry_text(key, text, updated))

    def test_what_is_written_reads_back(self):
        self._put("purpose", "coo", "suppliers, staff, costs")
        self.assertEqual(directory.read_one(self.dir, "purpose", "coo"),
                         {"key": "coo", "text": "suppliers, staff, costs", "updated": NOW})

    def test_read_all_keeps_purpose_and_now_apart(self):
        self._put("purpose", "coo", "role")
        self._put("now", SID_A, "problem")
        got = directory.read_all(self.dir)
        self.assertEqual(got["purpose"]["coo"]["text"], "role")
        self.assertEqual(got["now"][SID_A]["text"], "problem")
        self.assertNotIn(SID_A, got["purpose"])
        self.assertNotIn("coo", got["now"])

    def test_a_name_cannot_leave_the_folder(self):
        for name in ("../../etc/x", "a/b", "/abs", ".."):
            with self.subTest(name=name):
                self.assertEqual(os.path.dirname(directory.path_of(self.dir, "purpose", name)),
                                 self.dir)

    def test_names_match_exactly(self):
        # two spellings are two addresses: nothing is folded or dropped
        self._put("purpose", "coo", "lower")
        self._put("purpose", "COO", "upper")
        self.assertEqual(directory.read_one(self.dir, "purpose", "coo")["text"], "lower")
        self.assertEqual(directory.read_one(self.dir, "purpose", "COO")["text"], "upper")
        self.assertIsNone(directory.read_one(self.dir, "purpose", "Coo"))

    def test_no_folder_is_no_entries(self):
        gone = os.path.join(self.dir, "not-there")
        self.assertEqual(directory.read_all(gone), {"purpose": {}, "now": {}})
        self.assertIsNone(directory.read_one(gone, "purpose", "coo"))

    def test_a_bad_file_costs_its_own_line_only(self):
        self._put("purpose", "good", "kept")
        bad = {"not-json": "{", "a-list": "[1]", "no-text": '{"key": "x", "updated": 1}',
               "text-a-number": '{"key": "x", "text": 7, "updated": 1}',
               "key-a-number": '{"key": 7, "text": "t", "updated": 1}',
               "time-a-string": '{"key": "x", "text": "t", "updated": "soon"}',
               "time-a-bool": '{"key": "x", "text": "t", "updated": true}',
               "time-nan": '{"key": "x", "text": "t", "updated": NaN}',
               "no-words": '{"key": "x", "text": "  ", "updated": 1}',
               "deep": "[" * 100000}
        for name, text in bad.items():
            with open(os.path.join(self.dir, f"purpose-{name}.json"), "w") as fh:
                fh.write(text)
        self.assertEqual(list(directory.read_all(self.dir)["purpose"]), ["good"])

    def test_a_folder_in_place_of_a_file_is_skipped(self):
        os.mkdir(directory.path_of(self.dir, "purpose", "coo"))
        self.assertIsNone(directory.read_one(self.dir, "purpose", "coo"))
        self.assertEqual(directory.read_all(self.dir)["purpose"], {})

    def test_a_write_in_progress_is_no_entry(self):
        with open(os.path.join(self.dir, ".purpose-x.json.1.tmp"), "w") as fh:
            fh.write(directory.entry_text("x", "half", NOW))
        self.assertEqual(directory.read_all(self.dir)["purpose"], {})

    def test_text_read_from_a_file_is_cleaned_again(self):
        # a file edited by hand, or an older ccwho: the reader still gets one line
        with open(directory.path_of(self.dir, "purpose", "coo"), "w") as fh:
            json.dump({"key": "coo", "text": "a\nb\x07" + "x" * 500, "updated": NOW}, fh)
        text = directory.read_all(self.dir)["purpose"]["coo"]["text"]
        self.assertNotIn("\n", text)
        self.assertNotIn("\x07", text)
        self.assertLessEqual(len(text), directory.MAX_TEXT)


class TestPrune(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ccwho-dir-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _put(self, kind, key, age):
        path = directory.path_of(self.dir, kind, key)
        with open(path, "w") as fh:
            fh.write(directory.entry_text(key, "t", NOW - age))
        os.utime(path, (NOW - age, NOW - age))
        return path

    def test_an_old_now_line_goes(self):
        path = self._put("now", SID_A, directory.NOW_KEPT + 60)
        directory.prune_now(self.dir, NOW)
        self.assertFalse(os.path.exists(path))

    def test_a_recent_now_line_stays(self):                             # control
        path = self._put("now", SID_A, directory.NOW_KEPT - 60)
        directory.prune_now(self.dir, NOW)
        self.assertTrue(os.path.exists(path))

    def test_a_purpose_of_any_age_stays(self):
        # a role can sleep for a year; its purpose is what says what it is for
        path = self._put("purpose", "coo", directory.NOW_KEPT * 10)
        directory.prune_now(self.dir, NOW)
        self.assertTrue(os.path.exists(path))

    def test_the_time_kept_is_what_the_plan_says(self):
        self.assertEqual(directory.NOW_KEPT, 30 * 86400)

    def test_no_folder_does_not_raise(self):
        directory.prune_now(os.path.join(self.dir, "not-there"), NOW)

    def test_a_file_that_cannot_go_does_not_raise(self):
        self._put("now", SID_A, directory.NOW_KEPT + 60)
        with mock.patch("os.remove", side_effect=OSError("no")):
            directory.prune_now(self.dir, NOW)


class TestMerge(unittest.TestCase):
    def test_a_name_you_gave_is_a_role(self):
        got = merged([row("coo", "user", cache="532k cold")],
                     entries(purpose={"coo": ("suppliers", NOW)}))
        line = got["named"][0]
        self.assertEqual(got["others"], [])
        self.assertEqual((line["handle"], line["state"], line["cache"], line["purpose"]),
                         ("coo", "idle", "532k cold", "suppliers"))

    def test_a_derived_or_auto_name_is_not(self):
        for source in ("derived", "auto", "", None):
            with self.subTest(source=source):
                got = merged([row("app-4e", source)])
                self.assertEqual(got["named"], [])
                self.assertEqual(got["others"][0]["handle"], "app-4e")

    def test_a_row_with_no_name_source_is_not_a_role(self):
        r = row("app-4e")
        del r["nameSource"]
        self.assertEqual(merged([r])["named"], [])

    def test_a_purpose_with_no_live_session_is_not_running(self):
        got = merged([], entries(purpose={"crm": ("customers", NOW)}))
        self.assertEqual(got["named"][0]["handle"], "crm")
        self.assertEqual(got["named"][0]["state"], "not running")
        self.assertEqual(got["named"][0]["purpose"], "customers")

    def test_an_incomplete_list_never_says_not_running(self):
        got = merged([], entries(purpose={"crm": ("customers", NOW)}), complete=False)
        self.assertEqual(got["named"][0]["state"], "unknown (session list incomplete)")
        self.assertFalse(got["complete"])

    def test_a_complete_list_says_so(self):                             # control
        self.assertTrue(merged([])["complete"])

    def test_a_live_role_is_not_also_listed_as_not_running(self):
        got = merged([row("coo", "user")], entries(purpose={"coo": ("suppliers", NOW)}))
        self.assertEqual([x["state"] for x in got["named"]], ["idle"])

    def test_the_same_word_on_a_derived_name_does_not_make_the_role_live(self):
        got = merged([row("crm", "derived")], entries(purpose={"crm": ("customers", NOW)}))
        self.assertEqual(got["named"][0]["state"], "not running")
        self.assertEqual(got["others"][0]["handle"], "crm")

    def test_a_role_with_no_purpose_says_so(self):
        self.assertEqual(merged([row("coo", "user")])["named"][0]["purpose"], "")

    def test_the_now_line_is_kept_by_session_id(self):
        got = merged([row("app-4e", sid=SID_A, title="the title")],
                     entries(now={SID_A: ("the problem", NOW - 7200), SID_B: ("not mine", NOW)}))
        line = got["others"][0]
        self.assertEqual((line["now"], line["now_age"]), ("the problem", "2h"))

    def test_with_no_now_line_the_title_shows(self):
        line = merged([row("app-4e", title="the title")])["others"][0]
        self.assertEqual((line["now"], line["now_age"], line["title"]), ("", "", "the title"))

    def test_with_no_title_the_topic_shows(self):
        line = merged([row("app-4e", title="", topic="what you said")])["others"][0]
        self.assertEqual(line["title"], "what you said")

    def test_clear_loses_the_now_line_and_keeps_the_purpose(self):
        # /clear: a new session id, the same name
        ent = entries(purpose={"coo": ("suppliers", NOW)}, now={SID_A: ("old job", NOW)})
        line = merged([row("coo", "user", sid=SID_B)], ent)["named"][0]
        self.assertEqual((line["purpose"], line["now"]), ("suppliers", ""))

    def test_a_role_has_a_purpose_and_a_now_line(self):
        ent = entries(purpose={"coo": ("suppliers", NOW)}, now={SID_A: ("this job", NOW - 60)})
        line = merged([row("coo", "user", sid=SID_A)], ent)["named"][0]
        self.assertEqual((line["purpose"], line["now"], line["now_age"]),
                         ("suppliers", "this job", "1m"))

    def test_two_live_sessions_with_one_name_are_both_flagged(self):
        got = merged([row("coo", "user", sid=SID_A), row("coo", "user", sid=SID_B),
                      row("crm", "user", sid=SID_C)])
        self.assertEqual({(x["handle"], x["shared"]) for x in got["named"]},
                         {("coo", 2), ("crm", 1)})
        self.assertEqual(len(got["named"]), 3)

    def test_own_row_is_marked(self):
        got = merged([row("app-4e", sid=SID_A), row("app-5f", sid=SID_B)], me=SID_B)
        self.assertEqual({x["handle"]: x["you"] for x in got["others"]},
                         {"app-4e": False, "app-5f": True})

    def test_a_person_has_no_own_row(self):                             # control
        self.assertFalse(merged([row("app-4e", sid=SID_A)])["others"][0]["you"])

    def test_the_handle_comes_from_the_rule_given(self):
        r = row(None, sid=SID_A, project="thing")
        self.assertEqual(merged([r])["others"][0]["handle"], "thing")

    def test_a_named_caller_with_no_purpose_is_reminded(self):
        got = merged([row("coo", "user", sid=SID_A)], me=SID_A)
        self.assertEqual(got["reminder"], "coo")

    def test_a_named_caller_with_a_purpose_is_not(self):                # control
        got = merged([row("coo", "user", sid=SID_A)],
                     entries(purpose={"coo": ("suppliers", NOW)}), me=SID_A)
        self.assertIsNone(got["reminder"])

    def test_a_caller_with_no_name_is_not(self):                        # control
        self.assertIsNone(merged([row("app-4e", sid=SID_A)], me=SID_A)["reminder"])

    def test_live_roles_come_before_ended_ones(self):
        got = merged([row("zed", "user")], entries(purpose={"abc": ("x", NOW)}))
        self.assertEqual([x["handle"] for x in got["named"]], ["zed", "abc"])

    def test_the_row_order_is_kept_for_other_sessions(self):
        got = merged([row("b-1", sid=SID_A), row("a-1", sid=SID_B)])
        self.assertEqual([x["handle"] for x in got["others"]], ["b-1", "a-1"])

    def test_row_text_is_one_line_too(self):
        line = merged([row("app-4e", title="two\nlines\x07")])["others"][0]
        self.assertNotIn("\n", line["title"])
        self.assertNotIn("\x07", line["title"])

    def test_a_clock_moved_back_is_no_negative_age(self):
        ent = entries(now={SID_A: ("job", NOW + 500)})
        self.assertEqual(merged([row(sid=SID_A)], ent)["others"][0]["now_age"], "0s")


class TestMatching(unittest.TestCase):
    def setUp(self):
        self.got = merged([row("coo", "user", sid=SID_A), row("app-4e", sid=SID_B, title="certs")],
                          entries(purpose={"coo": ("Suppliers and staff", NOW),
                                           "crm": ("customers", NOW)},
                                  now={SID_A: ("the vat form", NOW)}))

    def _handles(self, words):
        got = directory.matching(self.got, words)
        return sorted(x["handle"] for x in got["named"] + got["others"])

    def test_no_words_is_everything(self):
        self.assertEqual(self._handles([]), ["app-4e", "coo", "crm"])

    def test_a_word_in_the_purpose(self):
        self.assertEqual(self._handles(["suppliers"]), ["coo"])

    def test_a_word_in_the_now_line(self):
        self.assertEqual(self._handles(["vat"]), ["coo"])

    def test_a_word_in_the_title_or_handle(self):
        self.assertEqual(self._handles(["certs"]), ["app-4e"])
        self.assertEqual(self._handles(["app-4e"]), ["app-4e"])

    def test_every_word_has_to_match(self):
        self.assertEqual(self._handles(["suppliers", "vat"]), ["coo"])
        self.assertEqual(self._handles(["suppliers", "certs"]), [])

    def test_a_role_that_has_ended_is_found_by_its_purpose(self):
        self.assertEqual(self._handles(["customers"]), ["crm"])

    def test_the_rest_of_the_result_stays(self):
        self.assertTrue(directory.matching(self.got, ["coo"])["complete"])


class TestRender(unittest.TestCase):
    def _text(self, rows, ent=None, **kw):
        return directory.render(merged(rows, ent, **kw))

    def test_the_two_sections_in_order(self):
        text = self._text([row("coo", "user", sid=SID_A), row("app-4e", sid=SID_B)])
        self.assertLess(text.index("NAMED"), text.index("coo"))
        self.assertLess(text.index("coo"), text.index("OTHER SESSIONS"))
        self.assertLess(text.index("OTHER SESSIONS"), text.index("app-4e"))

    def test_a_role_shows_state_size_and_purpose_on_one_line(self):
        text = self._text([row("coo", "user", cache="532k cold")],
                          entries(purpose={"coo": ("suppliers, staff", NOW)}))
        line = next(x for x in text.splitlines() if x.strip().startswith("coo"))
        for part in ("idle", "532k cold", "suppliers, staff"):
            self.assertIn(part, line)

    def test_a_role_shows_its_now_line_with_its_age_below(self):
        text = self._text([row("coo", "user")],
                          entries(purpose={"coo": ("suppliers", NOW)},
                                  now={SID_A: ("this job", NOW - 7200)}))
        self.assertIn("now: this job (2h)", text)

    def test_a_role_with_no_now_line_shows_its_title_with_no_age(self):
        text = self._text([row("coo", "user", title="the title")])
        self.assertIn("now: the title", text)
        self.assertNotIn("the title (", text)

    def test_a_role_with_no_purpose_says_so(self):
        self.assertIn("no purpose yet", self._text([row("coo", "user")]))

    def test_not_running(self):
        text = self._text([], entries(purpose={"crm": ("customers", NOW)}))
        self.assertIn("not running", text)
        self.assertIn("customers", text)
        self.assertNotIn("no purpose yet", text)

    def test_a_role_not_running_has_no_now_line(self):
        text = self._text([], entries(purpose={"crm": ("customers", NOW)}))
        self.assertNotIn("now:", text)

    def test_another_session_shows_its_now_line_with_its_age(self):
        text = self._text([row("app-4e", cache="640k ⧗19m", title="the title")],
                          entries(now={SID_A: ("the problem", NOW - 720)}))
        self.assertIn("the problem (12m)", text)
        self.assertIn("640k ⧗19m", text)
        self.assertNotIn("the title", text)

    def test_another_session_with_no_now_line_shows_its_title(self):
        text = self._text([row("app-4e", title="the title")])
        self.assertIn("the title", text)
        self.assertNotIn("(", text.split("the title")[1].splitlines()[0])

    def test_two_with_one_name_tells_how_to_tell_them_apart(self):
        text = self._text([row("coo", "user", sid=SID_A), row("coo", "user", sid=SID_B)])
        self.assertIn("2 sessions have this name", text)
        self.assertIn("ListAgents", text)

    def test_one_name_says_nothing_of_it(self):                         # control
        self.assertNotIn("sessions have this name", self._text([row("coo", "user")]))

    def test_own_row_is_marked(self):
        self.assertIn("(you)", self._text([row("app-4e", sid=SID_A)], me=SID_A))

    def test_no_own_row_for_a_person(self):                             # control
        self.assertNotIn("(you)", self._text([row("app-4e", sid=SID_A)]))

    def test_the_reminder_names_the_session_and_the_command(self):
        text = self._text([row("coo", "user", sid=SID_A)], me=SID_A)
        self.assertIn("/purpose", text)
        self.assertIn("coo", text.split("/purpose")[0].splitlines()[-1])

    def test_how_to_ask_is_at_the_end(self):
        text = self._text([row("app-4e")])
        self.assertIn("SendMessage", text.strip().splitlines()[-1])

    def test_an_empty_section_is_left_out(self):
        self.assertNotIn("NAMED", self._text([row("app-4e")]))
        self.assertNotIn("OTHER SESSIONS", self._text([row("coo", "user")]))

    def test_nothing_at_all_says_so(self):
        self.assertIn("no sessions", self._text([]))

    def test_a_row_stays_short_whatever_a_session_wrote(self):
        # a model reads this list: its size is a cost on each lookup
        long = "x" * 1000
        text = self._text([row("coo", "user", sid=SID_A, title=long, cache="532k cold"),
                           row("app-4e", sid=SID_B, title=long, cache="1k")],
                          entries(purpose={"coo": (long, NOW)}, now={SID_B: (long, NOW)}))
        self.assertLess(max(len(x) for x in text.splitlines()), 200)

    def test_a_long_title_is_cut(self):
        text = self._text([row("app-4e", title="y" * 1000)])
        self.assertLessEqual(text.count("y"), 80)

    def test_a_long_handle_stays_on_its_line(self):
        text = self._text([row("n" * 200, "user")])
        self.assertLess(max(len(x) for x in text.splitlines()), 200)


@contextlib.contextmanager
def as_agent(sid=SID_A):
    with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": sid}):
        os.environ.pop("CODEX_THREAD_ID", None)
        yield


@contextlib.contextmanager
def as_person():
    # these tests may themselves run inside a Claude Code session
    with mock.patch.dict(os.environ):
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        os.environ.pop("CODEX_THREAD_ID", None)
        yield


def call(fn, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = fn(list(argv))
    return rc, out.getvalue(), err.getvalue()


class CommandCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="ccwho-dir-test-")
        self.addCleanup(shutil.rmtree, self.home, True)
        patcher = mock.patch.dict(os.environ, {"CCWHO_DIR": self.home})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.folder = runner.directory_dir()

    def stored(self, kind, key):
        got = directory.read_one(self.folder, kind, key)
        return got["text"] if got else None

    def live(self, *rows):
        patcher = mock.patch.object(runner.engine, "live_file_sessions",
                                    return_value=(list(rows), 0))
        got = patcher.start()
        self.addCleanup(patcher.stop)
        return got


class TestPurposeCommand(CommandCase):
    def test_a_session_with_a_name_sets_its_own(self):
        self.live(row("coo", "user", sid=SID_A))
        with as_agent(SID_A):
            rc, out, _ = call(runner.purpose_cli, "suppliers,", "staff", "and", "costs")
        self.assertEqual(rc, 0)
        self.assertEqual(self.stored("purpose", "coo"), "suppliers, staff and costs")
        self.assertIn("coo", out)

    def test_the_folder_is_made_when_needed(self):
        self.assertFalse(os.path.exists(self.folder))
        self.live(row("coo", "user", sid=SID_A))
        with as_agent(SID_A):
            self.assertEqual(call(runner.purpose_cli, "x")[0], 0)
        self.assertTrue(os.path.isdir(self.folder))

    def test_it_is_kept_by_name_so_a_new_session_id_still_has_it(self):
        self.live(row("coo", "user", sid=SID_B))
        with as_agent(SID_B):
            call(runner.purpose_cli, "suppliers")
        self.assertEqual(self.stored("purpose", "coo"), "suppliers")
        self.assertIsNone(self.stored("purpose", SID_B))

    def test_a_session_with_no_name_is_told_to_name_itself(self):
        for source in ("derived", "auto"):
            with self.subTest(source=source):
                self.live(row("app-4e", source, sid=SID_A))
                with as_agent(SID_A):
                    rc, _, err = call(runner.purpose_cli, "suppliers")
                self.assertEqual(rc, 2)
                self.assertIn("Name it first: /name coo", err)
                self.assertEqual(os.path.exists(self.folder) and os.listdir(self.folder) or [], [])

    def test_an_agent_cannot_set_another_session(self):
        self.live(row("coo", "user", sid=SID_A))
        with as_agent(SID_A):
            rc, _, err = call(runner.purpose_cli, "--session", "crm", "customers")
        self.assertEqual(rc, 2)
        self.assertIn("own session", err)
        self.assertIsNone(self.stored("purpose", "crm"))
        self.assertIsNone(self.stored("purpose", "coo"))

    def test_an_agent_not_in_the_session_files_writes_nothing(self):
        self.live(row("coo", "user", sid=SID_B))
        with as_agent(SID_A):
            rc, _, err = call(runner.purpose_cli, "suppliers")
        self.assertEqual(rc, 1)
        self.assertIn("session", err)
        self.assertIsNone(self.stored("purpose", "coo"))

    def test_a_person_names_the_session(self):
        self.live(row("coo", "user", sid=SID_A))
        with as_person():
            rc, _, _ = call(runner.purpose_cli, "--session", "coo", "suppliers")
        self.assertEqual(rc, 0)
        self.assertEqual(self.stored("purpose", "coo"), "suppliers")

    def test_a_person_can_set_a_role_that_is_not_running(self):
        self.live()
        with as_person():
            rc, _, _ = call(runner.purpose_cli, "--session", "crm", "customers")
        self.assertEqual(rc, 0)
        self.assertEqual(self.stored("purpose", "crm"), "customers")

    def test_a_person_gives_no_session_and_is_told_how(self):
        self.live()
        with as_person():
            rc, _, err = call(runner.purpose_cli, "suppliers")
        self.assertEqual(rc, 2)
        self.assertIn("--session", err)

    def test_a_person_cannot_set_a_derived_name(self):
        # it changes at the next resume: the purpose would be left behind
        self.live(row("app-4e", "derived", sid=SID_A))
        with as_person():
            rc, _, err = call(runner.purpose_cli, "--session", "app-4e", "x")
        self.assertEqual(rc, 2)
        self.assertIn("Name it first: /name coo", err)
        self.assertIsNone(self.stored("purpose", "app-4e"))

    def test_no_text_prints_the_line(self):
        self.live(row("coo", "user", sid=SID_A))
        with as_agent(SID_A):
            call(runner.purpose_cli, "suppliers")
            rc, out, _ = call(runner.purpose_cli)
        self.assertEqual(rc, 0)
        self.assertIn("suppliers", out)

    def test_no_text_and_no_line_says_so(self):
        self.live(row("coo", "user", sid=SID_A))
        with as_agent(SID_A):
            rc, out, _ = call(runner.purpose_cli)
        self.assertEqual(rc, 0)
        self.assertIn("no purpose yet", out)

    def test_empty_text_removes_it(self):
        self.live(row("coo", "user", sid=SID_A))
        with as_agent(SID_A):
            call(runner.purpose_cli, "suppliers")
            rc, out, _ = call(runner.purpose_cli, "")
        self.assertEqual(rc, 0)
        self.assertIsNone(self.stored("purpose", "coo"))
        self.assertIn("removed", out)

    def test_a_person_can_remove_a_line_left_by_a_rename(self):
        self.live(row("coo", "user", sid=SID_A))
        with as_agent(SID_A):
            call(runner.purpose_cli, "suppliers")
        self.live()
        with as_person():
            rc, _, _ = call(runner.purpose_cli, "--session", "coo", "")
        self.assertEqual(rc, 0)
        self.assertIsNone(self.stored("purpose", "coo"))

    def test_removing_what_is_not_there_is_no_error(self):
        self.live(row("coo", "user", sid=SID_A))
        with as_agent(SID_A):
            self.assertEqual(call(runner.purpose_cli, "")[0], 0)

    def test_only_space_removes_it_too(self):
        self.live(row("coo", "user", sid=SID_A))
        with as_agent(SID_A):
            call(runner.purpose_cli, "suppliers")
            call(runner.purpose_cli, "  ")
        self.assertIsNone(self.stored("purpose", "coo"))

    def test_long_text_is_cut_and_the_caller_is_told(self):
        self.live(row("coo", "user", sid=SID_A))
        with as_agent(SID_A):
            rc, out, _ = call(runner.purpose_cli, "x" * 500)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.stored("purpose", "coo")), directory.MAX_TEXT)
        self.assertIn(str(directory.MAX_TEXT), out)

    def test_short_text_is_not_said_to_be_cut(self):                    # control
        self.live(row("coo", "user", sid=SID_A))
        with as_agent(SID_A):
            _, out, _ = call(runner.purpose_cli, "suppliers")
        self.assertNotIn(str(directory.MAX_TEXT), out)

    def test_a_failed_write_says_why(self):
        self.live(row("coo", "user", sid=SID_A))
        with as_agent(SID_A), mock.patch.object(runner, "write_atomic",
                                                side_effect=OSError("disk full")):
            rc, _, err = call(runner.purpose_cli, "suppliers")
        self.assertEqual(rc, 1)
        self.assertIn("disk full", err)

    def test_a_folder_that_cannot_be_made_says_why(self):
        with open(self.folder, "w") as fh:      # a file where the folder goes
            fh.write("x")
        self.live(row("coo", "user", sid=SID_A))
        with as_agent(SID_A):
            rc, _, err = call(runner.purpose_cli, "suppliers")
        self.assertEqual(rc, 1)
        self.assertIn("ccwho purpose", err)

    def test_session_files_that_cannot_be_read_write_nothing(self):
        # a person too: unread, a derived name would look free to hold a purpose
        for who, argv in ((as_agent, ("suppliers",)), (as_person, ("--session", "app-4e", "x"))):
            with self.subTest(who=who.__name__):
                with mock.patch.object(runner.engine, "live_file_sessions",
                                       side_effect=OSError("no")), who():
                    rc, _, err = call(runner.purpose_cli, *argv)
                self.assertEqual(rc, 1)
                self.assertIn("could not read the session files", err)
                self.assertFalse(os.path.exists(self.folder))


class TestNowCommand(CommandCase):
    def setUp(self):
        super().setUp()
        # it runs in each session at each new job: it reads no rows
        for name in ("scan", ):
            patcher = mock.patch.object(runner, name, side_effect=AssertionError("read rows"))
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(runner.engine, "live_file_sessions",
                                    side_effect=AssertionError("read rows"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_session_records_its_problem(self):
        with as_agent(SID_A):
            rc, _, _ = call(runner.now_cli, "the", "cert", "chain")
        self.assertEqual(rc, 0)
        self.assertEqual(self.stored("now", SID_A), "the cert chain")

    def test_a_person_is_told_it_runs_from_inside_a_session(self):
        with as_person():
            rc, _, err = call(runner.now_cli, "the cert chain")
        self.assertEqual(rc, 2)
        self.assertIn("inside a session", err)
        self.assertFalse(os.path.exists(self.folder))

    def test_a_new_line_replaces_the_old_one(self):
        with as_agent(SID_A):
            call(runner.now_cli, "first")
            call(runner.now_cli, "second")
        self.assertEqual(self.stored("now", SID_A), "second")

    def test_a_session_has_its_own_line(self):
        with as_agent(SID_A):
            call(runner.now_cli, "mine")
        with as_agent(SID_B):
            call(runner.now_cli, "yours")
        self.assertEqual(self.stored("now", SID_A), "mine")
        self.assertEqual(self.stored("now", SID_B), "yours")

    def test_no_text_prints_the_line(self):
        with as_agent(SID_A):
            call(runner.now_cli, "the job")
            rc, out, _ = call(runner.now_cli)
        self.assertEqual(rc, 0)
        self.assertIn("the job", out)

    def test_empty_text_removes_it(self):
        with as_agent(SID_A):
            call(runner.now_cli, "the job")
            rc, _, _ = call(runner.now_cli, "")
        self.assertEqual(rc, 0)
        self.assertIsNone(self.stored("now", SID_A))

    def test_writing_prunes_old_lines(self):
        with as_agent(SID_B):
            call(runner.now_cli, "old")
        old = directory.path_of(self.folder, "now", SID_B)
        then = time.time() - directory.NOW_KEPT - 60
        os.utime(old, (then, then))
        with as_agent(SID_A):
            call(runner.now_cli, "new")
        self.assertFalse(os.path.exists(old))
        self.assertEqual(self.stored("now", SID_A), "new")

    def test_a_failed_write_says_why(self):
        with as_agent(SID_A), mock.patch.object(runner, "write_atomic",
                                                side_effect=OSError("disk full")):
            rc, _, err = call(runner.now_cli, "the job")
        self.assertEqual(rc, 1)
        self.assertIn("disk full", err)

    def test_the_time_is_recorded(self):
        before = time.time()
        with as_agent(SID_A):
            call(runner.now_cli, "the job")
        got = directory.read_one(self.folder, "now", SID_A)["updated"]
        self.assertGreaterEqual(got, before)


class TestDirectoryCommand(CommandCase):
    def _scan(self, rows, source_ok=True, bad=0):
        self.asked = {}

        def fake(cache=None, status=None, **kw):
            self.asked = dict(kw, cache=cache)
            status["source_ok"], status["session_files_bad"] = source_ok, bad
            return list(rows), {}
        patcher = mock.patch.object(runner, "scan", side_effect=fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _purpose(self, name, text):
        os.makedirs(self.folder, exist_ok=True)
        with open(directory.path_of(self.folder, "purpose", name), "w") as fh:
            fh.write(directory.entry_text(name, text, time.time()))

    def test_it_lists_roles_and_other_sessions(self):
        self._scan([row("coo", "user", sid=SID_A), row("app-4e", sid=SID_B)])
        self._purpose("coo", "suppliers")
        with as_person():
            rc, out, _ = call(runner.directory_cli)
        self.assertEqual(rc, 0)
        for part in ("NAMED", "coo", "suppliers", "OTHER SESSIONS", "app-4e"):
            self.assertIn(part, out)

    def test_it_uses_the_list_scan_with_the_cheap_options(self):
        self._scan([])
        with as_person():
            call(runner.directory_cli)
        self.assertEqual((self.asked["session_apps"], self.asked["samples"], self.asked["cache"]),
                         (False, 0, {}))

    def test_a_failed_feed_never_says_not_running(self):
        self._scan([], source_ok=False)
        self._purpose("crm", "customers")
        with as_person():
            _, out, _ = call(runner.directory_cli)
        self.assertNotIn("not running", out)
        self.assertIn("unknown (session list incomplete)", out)

    def test_a_session_file_that_cannot_be_read_never_says_not_running(self):
        self._scan([], bad=1)
        self._purpose("crm", "customers")
        with as_person():
            _, out, _ = call(runner.directory_cli)
        self.assertNotIn("not running", out)

    def test_a_scan_of_the_files_that_crashed_never_says_not_running(self):
        self._scan([], bad=None)            # bad is None when the file scan crashed
        self._purpose("crm", "customers")
        with as_person():
            _, out, _ = call(runner.directory_cli)
        self.assertNotIn("not running", out)

    def test_a_complete_list_says_not_running(self):                    # control
        self._scan([])
        self._purpose("crm", "customers")
        with as_person():
            _, out, _ = call(runner.directory_cli)
        self.assertIn("not running", out)

    def test_other_rows_still_show_when_the_list_is_incomplete(self):
        self._scan([row("app-4e")], source_ok=False)
        with as_person():
            rc, out, _ = call(runner.directory_cli)
        self.assertEqual(rc, 0)
        self.assertIn("app-4e", out)

    def test_a_scan_that_raises_says_so(self):
        with mock.patch.object(runner, "scan", side_effect=OSError("no")), as_person():
            rc, out, err = call(runner.directory_cli)
        self.assertEqual(rc, 1)
        self.assertIn("could not read", err)
        self.assertNotIn("not running", out)

    def test_words_narrow_it(self):
        self._scan([row("coo", "user", sid=SID_A), row("app-4e", sid=SID_B)])
        self._purpose("coo", "suppliers")
        with as_person():
            rc, out, _ = call(runner.directory_cli, "suppliers")
        self.assertEqual(rc, 0)
        self.assertIn("coo", out)
        self.assertNotIn("app-4e", out)

    def test_words_that_match_nothing_say_so(self):
        self._scan([row("app-4e")])
        with as_person():
            rc, _, err = call(runner.directory_cli, "nothing-like-this")
        self.assertEqual(rc, 1)
        self.assertIn("nothing-like-this", err)

    def test_json_has_the_same_data(self):
        self._scan([row("coo", "user", sid=SID_A, cache="532k cold"), row("app-4e", sid=SID_B)])
        self._purpose("coo", "suppliers")
        with as_person():
            rc, out, _ = call(runner.directory_cli, "--json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["named"][0]["purpose"], "suppliers")
        self.assertEqual(data["named"][0]["cache"], "532k cold")
        self.assertEqual(data["others"][0]["handle"], "app-4e")
        self.assertTrue(data["complete"])

    def test_own_row_is_marked_for_an_agent(self):
        self._scan([row("app-4e", sid=SID_A)])
        with as_agent(SID_A):
            _, out, _ = call(runner.directory_cli)
        self.assertIn("(you)", out)

    def test_the_now_line_reaches_the_row(self):
        self._scan([row("app-4e", sid=SID_A, title="the title")])
        with as_agent(SID_A):
            call(runner.now_cli, "the problem")
            _, out, _ = call(runner.directory_cli)
        self.assertIn("the problem", out)
        self.assertNotIn("the title", out)

    def test_bad_store_files_do_not_stop_it(self):
        self._scan([row("app-4e")])
        os.makedirs(self.folder)
        with open(os.path.join(self.folder, "purpose-x.json"), "w") as fh:
            fh.write("{")
        with as_person():
            rc, out, _ = call(runner.directory_cli)
        self.assertEqual(rc, 0)
        self.assertIn("app-4e", out)


class TestTheCommandsFollowTheRules(CommandCase):
    """COMMAND_TAKES: --help prints its usage and runs nothing; an unknown flag
    exits 2 and runs nothing."""

    COMMANDS = ("purpose", "now", "directory")

    def test_help_prints_usage(self):
        for cmd in self.COMMANDS:
            with self.subTest(cmd=cmd):
                rc, out, _ = call(runner.main, cmd, "--help")
                self.assertEqual(rc, 0)
                self.assertIn(f"ccwho {cmd}", out)

    def test_an_unknown_flag_is_refused(self):
        for cmd in self.COMMANDS:
            with self.subTest(cmd=cmd):
                rc, _, err = call(runner.main, cmd, "--no-such-flag")
                self.assertEqual(rc, 2)
                self.assertIn("unknown option", err)

    def test_main_reaches_each_command(self):
        with as_agent(SID_A):
            rc, _, _ = call(runner.main, "now", "the job")
        self.assertEqual(rc, 0)
        self.assertEqual(self.stored("now", SID_A), "the job")

    def test_the_general_help_names_them(self):
        rc, out, _ = call(runner.main, "--help")
        self.assertEqual(rc, 0)
        for cmd in self.COMMANDS:
            with self.subTest(cmd=cmd):
                self.assertIn(f"ccwho {cmd}", out)


if __name__ == "__main__":
    unittest.main()
