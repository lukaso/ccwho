"""Tests for ccwho_index: every session on disk, findable, read once.

Measured 2026-09-19: 1,480 transcripts, 1 GB, the oldest 29 days (Claude Code
deletes them after 30 by default). Re-reading that on every tick is not an
option, and neither is only knowing about sessions that happen to be running.

Transcripts only grow at the end, so the index remembers where it stopped and
reads the new bytes. python3 -m unittest test_index -v
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import ccwho_index as index


def rec(**kw):
    # ensure_ascii=False on purpose: 18 of the 20 newest real transcripts contain
    # raw multi-byte UTF-8, so a fixture that escapes it tests the easy case only
    return json.dumps(kw, ensure_ascii=False)


def user(text, ts="2026-09-18T10:00:00.000Z"):
    return rec(type="user", timestamp=ts, message={"role": "user", "content": text})


def assistant(text, ts="2026-09-18T10:01:00.000Z"):
    return rec(type="assistant", timestamp=ts,
               message={"role": "assistant", "content": [{"type": "text", "text": text}]})


def recap(text, ts="2026-09-18T09:00:00.000Z"):
    return rec(type="system", subtype="away_summary", content=text, timestamp=ts)


def title(t):
    return rec(type="ai-title", aiTitle=t)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.proj = os.path.join(self.tmp, "projects", "-Users-x-liveapp")
        os.makedirs(self.proj)
        self.reads = []
        self.real_open = index._read_from

        def counting(path, offset):
            self.reads.append((os.path.basename(path), offset))
            return self.real_open(path, offset)

        index._read_from = counting

    def tearDown(self):
        index._read_from = self.real_open
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, sid, lines, mode="w"):
        p = os.path.join(self.proj, f"{sid}.jsonl")
        with open(p, mode) as fh:
            fh.write("".join(l + "\n" for l in lines))
        return p

    def update(self, idx=None, **kw):
        return index.update(idx if idx is not None else {},
                            index.transcripts(self.tmp), **kw)


class TestFirstBuild(Base):
    def test_it_learns_what_a_session_was_about(self):
        self.write("aaa11111-0000-4000-8000-000000000001", [
            user("fix the gate flake", "2026-09-18T09:00:00.000Z"),
            title("Gate flake"),
            recap("Goal: fix the timing suite's flake.", "2026-09-18T09:30:00.000Z"),
            user("continue", "2026-09-18T10:00:00.000Z"),
            assistant("done, the gate is green", "2026-09-18T10:05:00.000Z")])
        idx = self.update()
        e = idx["aaa11111-0000-4000-8000-000000000001"]
        self.assertEqual(e["title"], "Gate flake")
        self.assertEqual(e["opened"], "fix the gate flake")
        self.assertEqual(e["you_said"], "fix the gate flake")   # "continue" is skipped
        self.assertIn("timing suite", e["recap"])
        self.assertEqual(e["last_ts"], "2026-09-18T10:05:00.000Z")
        self.assertEqual(e["project"], "liveapp")

    def test_the_project_comes_from_the_cwd_not_the_folder_name(self):
        # Claude Code's folder name is the path with slashes turned into dashes,
        # so the last segment of a WORKTREE is the branch: "wt", not "liveapp".
        # Every record carries the real cwd; the engine already knows how to read
        # a worktree path back to its repo.
        self.write("777a1111-0000-4000-8000-000000000007", [
            rec(type="user", timestamp="2026-09-18T10:00:00.000Z",
                cwd="/Users/x/projects/liveapp/.wt/feature-branch",
                message={"role": "user", "content": "fix the thing"})])
        e = self.update()["777a1111-0000-4000-8000-000000000007"]
        self.assertEqual(e["cwd"], "/Users/x/projects/liveapp/.wt/feature-branch")
        self.assertEqual(e["project"], "liveapp")

    def test_without_a_cwd_it_falls_back_to_the_folder(self):     # control
        self.write("666a1111-0000-4000-8000-000000000006", [user("no cwd here")])
        self.assertEqual(self.update()["666a1111-0000-4000-8000-000000000006"]["project"],
                         "liveapp")

    def test_turns_since_the_recap_are_counted_as_it_goes(self):
        self.write("bbb11111-0000-4000-8000-000000000002", [
            recap("r", "2026-09-18T09:00:00.000Z"),
            assistant("one", "2026-09-18T09:10:00.000Z"),
            user("two", "2026-09-18T09:20:00.000Z")])
        e = self.update()["bbb11111-0000-4000-8000-000000000002"]
        self.assertEqual(e["turns_since_recap"], 2)

    def test_a_new_recap_restarts_the_count(self):
        # the count answers "how much has happened since that summary was true",
        # so work BEFORE the recap must not be in it
        self.write("555a1111-0000-4000-8000-000000000055", [
            user("one", "2026-09-18T08:00:00.000Z"),
            assistant("two", "2026-09-18T08:10:00.000Z"),
            recap("a summary of all that", "2026-09-18T09:00:00.000Z"),
            user("three", "2026-09-18T09:30:00.000Z")])
        e = self.update()["555a1111-0000-4000-8000-000000000055"]
        self.assertEqual(e["turns_since_recap"], 1)

    def test_an_empty_transcript_is_not_an_entry(self):
        self.write("ccc11111-0000-4000-8000-000000000003", [])
        self.assertEqual(self.update(), {})

    def test_a_line_that_is_not_json_is_skipped_not_fatal(self):
        self.write("ddd11111-0000-4000-8000-000000000004",
                   ["{ not json", user("the real prompt")])
        e = self.update()["ddd11111-0000-4000-8000-000000000004"]
        self.assertEqual(e["you_said"], "the real prompt")


class TestIncremental(Base):
    SID = "eee11111-0000-4000-8000-000000000005"

    def test_an_unchanged_file_is_not_opened_again(self):
        self.write(self.SID, [user("first")])
        idx, _ = {}, None
        idx = self.update()
        self.reads.clear()
        self.update(idx)
        self.assertEqual(self.reads, [], "nothing changed, nothing to read")

    def test_only_the_new_bytes_are_read(self):
        self.write(self.SID, [user("first", "2026-09-18T09:00:00.000Z")])
        idx = self.update()
        size = os.path.getsize(os.path.join(self.proj, f"{self.SID}.jsonl"))
        self.reads.clear()
        self.write(self.SID, [user("second", "2026-09-18T11:00:00.000Z")], mode="a")
        idx = self.update(idx)
        self.assertEqual(self.reads[0][1], size, "it resumed where it stopped")
        self.assertEqual(idx[self.SID]["you_said"], "second")

    def test_a_half_written_last_line_is_finished_next_time(self):
        p = self.write(self.SID, [user("first")])
        with open(p, "a") as fh:
            fh.write('{"type":"user","timestamp":"2026-09-18T12:00:00.000Z",')
        idx = self.update()
        self.assertEqual(idx[self.SID]["you_said"], "first", "a torn line is not data")
        with open(p, "a") as fh:
            fh.write('"message":{"role":"user","content":"the rest arrived"}}\n')
        idx = self.update(idx)
        self.assertEqual(idx[self.SID]["you_said"], "the rest arrived")

    def test_a_replaced_file_is_read_from_the_start(self):
        self.write(self.SID, [user("first"), user("second"), user("third")])
        idx = self.update()
        self.reads.clear()
        self.write(self.SID, [user("a whole new conversation")])   # shorter: replaced
        idx = self.update(idx)
        self.assertEqual(self.reads[0][1], 0, "a file that shrank is not an append")
        self.assertEqual(idx[self.SID]["you_said"], "a whole new conversation")

    def test_a_rule_change_re_reads_everything(self):
        # is_human_prompt learning a new machine prefix must correct old entries,
        # or an upgrade fixes nothing that is already indexed
        self.write(self.SID, [user("first")])
        idx = self.update()
        idx[self.SID]["v"] = index.EXTRACT_VERSION - 1
        self.reads.clear()
        self.update(idx)
        self.assertEqual(self.reads[0][1], 0)

    def test_a_deleted_transcript_leaves_the_index(self):
        self.write(self.SID, [user("first")])
        idx = self.update()
        os.unlink(os.path.join(self.proj, f"{self.SID}.jsonl"))
        idx = self.update(idx)
        self.assertNotIn(self.SID, idx, "claude deletes transcripts after 30 days")


class TestTheIndexIsNotTiedToOneLogin(Base):
    """Sessions run under different credentials - some from ~/.claude, some from
    a CLAUDE_CODE_OAUTH_TOKEN, and a session can be pointed at another config
    directory entirely with CLAUDE_CONFIG_DIR. ccwho reads files and never logs
    in, but it must look in every root it is told about, or the sessions it
    cannot find are the ones that were run differently."""

    def test_it_reads_every_configured_root(self):
        other = os.path.join(self.tmp, "other-config")
        proj = os.path.join(other, "projects", "-Users-x-football")
        os.makedirs(proj)
        with open(os.path.join(proj, "1111aaaa-0000-4000-8000-00000000000f.jsonl"),
                  "w") as fh:
            fh.write(user("a session from the other config dir") + "\n")
        self.write("2222aaaa-0000-4000-8000-00000000000e", [user("the usual one")])
        idx = index.update({}, index.transcripts(roots=[self.tmp, other]))
        self.assertEqual(len(idx), 2)

    def test_the_roots_include_the_env_var_and_the_default(self):
        os.environ["CLAUDE_CONFIG_DIR"] = "/somewhere/else"
        try:
            got = index.config_roots()
        finally:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.assertIn("/somewhere/else", got)
        self.assertIn(os.path.expanduser("~/.claude"), got)

    def test_extra_roots_can_be_listed_in_a_file(self):
        listing = os.path.join(self.tmp, "roots")
        with open(listing, "w") as fh:
            fh.write("# where the football agent keeps its config\n"
                     "/Users/x/.claude-football\n\n")
        got = index.config_roots(roots_file=listing)
        self.assertIn("/Users/x/.claude-football", got)
        self.assertNotIn("", got)

    def test_a_root_that_does_not_exist_is_not_fatal(self):
        self.assertEqual(index.transcripts(roots=["/no/such/place"]), [])


class TestSizeIsBounded(Base):
    """The first build over the real 1,402 transcripts produced a 177 MB index,
    because people paste whole files into prompts and every one was kept whole.
    An index is a finding aid: it needs enough of a line to recognise it."""

    SID = "999a1111-0000-4000-8000-000000000009"

    def test_a_pasted_novel_does_not_become_the_index(self):
        self.write(self.SID, [user("here is the log " + "x" * 50000),
                              recap("r " + "y" * 50000)])
        e = self.update()[self.SID]
        self.assertLessEqual(len(e["you_said"]), index.TEXT_CAP)
        self.assertLessEqual(len(e["recap"]), index.RECAP_CAP)
        self.assertTrue(e["you_said"].startswith("here is the log"),
                        "the front of a line is the part you recognise")

    def test_a_normal_prompt_is_kept_whole(self):                 # control
        self.write("888a1111-0000-4000-8000-000000000008",
                   [user("rebase onto main and re-run the gate")])
        e = self.update()["888a1111-0000-4000-8000-000000000008"]
        self.assertEqual(e["you_said"], "rebase onto main and re-run the gate")

    def test_an_entry_stays_small_enough_to_keep_thousands_of(self):
        self.write(self.SID, [user("x" * 9000), recap("y" * 9000), title("z" * 9000)])
        line = json.dumps(self.update()[self.SID])
        self.assertLess(len(line), 4000, "1,400 of these have to fit in a few MB")


class TestYourSessionsComeFirstClass(Base):
    """Most transcripts on this disk are not conversations you had: 356 of a
    400-file sample were started by a program (`entrypoint` sdk-cli or sdk-py),
    against 2 typed at a terminal. Searching "your sessions from a while ago"
    cannot mean wading through a thousand agent runs - but they are still real
    sessions, so they are one flag away, not deleted."""

    def test_an_agent_run_is_marked(self):
        self.write("aaab1111-0000-4000-8000-00000000000a", [
            rec(type="user", timestamp="2026-09-18T10:00:00.000Z",
                entrypoint="sdk-cli",
                message={"role": "user", "content": "run the contract check"})])
        e = self.update()["aaab1111-0000-4000-8000-00000000000a"]
        self.assertEqual(e["entrypoint"], "sdk-cli")
        self.assertFalse(index.is_yours(e))

    def test_a_session_you_typed_is_yours(self):
        self.write("bbbb1111-0000-4000-8000-00000000000b", [
            rec(type="user", timestamp="2026-09-18T10:00:00.000Z", entrypoint="cli",
                message={"role": "user", "content": "fix the gate"})])
        self.assertTrue(index.is_yours(self.update()["bbbb1111-0000-4000-8000-00000000000b"]))

    def test_an_older_transcript_with_no_marker_counts_as_yours(self):
        # the field is not in every transcript, and dropping unknowns would hide
        # exactly the old sessions this index exists to find
        self.write("cccb1111-0000-4000-8000-00000000000c", [user("no entrypoint here")])
        self.assertTrue(index.is_yours(self.update()["cccb1111-0000-4000-8000-00000000000c"]))

    def test_search_leaves_agent_runs_out_unless_asked(self):
        idx = {"a": {"sessionId": "a", "title": "contract check", "entrypoint": "sdk-cli",
                     "recap": "", "you_said": "", "opened": "", "project": "liveapp",
                     "last_ts": "2026-09-20T10:00:00.000Z"},
               "b": {"sessionId": "b", "title": "contract work", "entrypoint": "cli",
                     "recap": "", "you_said": "", "opened": "", "project": "liveapp",
                     "last_ts": "2026-09-01T10:00:00.000Z"}}
        self.assertEqual([e["sessionId"] for e in index.search(idx, "contract")], ["b"])
        self.assertEqual(len(index.search(idx, "contract", everything=True)), 2)


class TestRanking(Base):
    def entries(self):
        return {
            "aaaa0001-0000-4000-8000-000000000001": {
                "sessionId": "aaaa0001-0000-4000-8000-000000000001",
                "title": "Docker high CPU", "recap": "", "you_said": "look into it",
                "opened": "", "project": "liveapp",
                "last_ts": "2026-09-01T10:00:00.000Z"},
            "bbbb0002-0000-4000-8000-000000000002": {
                "sessionId": "bbbb0002-0000-4000-8000-000000000002",
                "title": "Systemd scope reaping", "recap": "",
                "you_said": "here is a log mentioning docker and cpu in passing",
                "opened": "", "project": "liveapp",
                "last_ts": "2026-09-20T10:00:00.000Z"},
        }

    def test_a_title_match_beats_a_word_buried_in_a_pasted_log(self):
        hits = index.search(self.entries(), "docker cpu")
        self.assertEqual(hits[0]["title"], "Docker high CPU",
                         "newer is not more relevant than being about the thing")

    def test_recency_still_decides_between_equals(self):          # control
        hits = index.search(self.entries(), "liveapp")
        self.assertEqual(hits[0]["title"], "Systemd scope reaping")


class TestAppendsActuallyPersist(Base):
    """The index is only worth keeping if what it learned survives the process.
    It did not: update() shallow-copied the dict and then mutated the entries
    inside it, so the caller's "did anything change?" comparison said no and the
    save was skipped. Every append was re-read on the next run, for ever."""

    SID = "4444aaaa-0000-4000-8000-000000000044"

    def test_the_input_index_is_left_alone(self):
        self.write(self.SID, [user("first", "2026-09-18T09:00:00.000Z")])
        first = self.update()
        before = json.dumps(first, sort_keys=True)
        self.write(self.SID, [user("second", "2026-09-18T11:00:00.000Z")], mode="a")
        second = self.update(first)
        self.assertEqual(json.dumps(first, sort_keys=True), before,
                         "update() must not reach into the caller's index")
        self.assertNotEqual(second, first, "...so the caller can see it changed")
        self.assertEqual(second[self.SID]["you_said"], "second")


class TestSplitCharacters(Base):
    """A read that lands in the middle of a multi-byte character used to decode
    it as a replacement character and then advance past bytes it never read.
    Transcripts are full of accents, arrows and glyphs."""

    SID = "5555aaaa-0000-4000-8000-000000000055"

    def test_a_character_split_across_two_reads_survives(self):
        path = os.path.join(self.proj, f"{self.SID}.jsonl")
        line = (user("fix caf\u00e9 rendering and the \u2192 arrows",
                     "2026-09-18T09:00:00.000Z") + "\n").encode("utf-8")
        cut = line.index(b"\xc3\xa9") + 1        # between the two bytes of the e-acute
        with open(path, "wb") as fh:
            fh.write(line[:cut])
        idx = self.update()
        with open(path, "ab") as fh:
            fh.write(line[cut:])
        idx = self.update(idx)
        self.assertEqual(idx[self.SID]["you_said"],
                         "fix caf\u00e9 rendering and the \u2192 arrows")

    def test_incremental_equals_one_whole_read(self):
        # the property that matters: however the bytes arrive, the answer is the
        # same as reading the finished file once
        path = os.path.join(self.proj, f"{self.SID}.jsonl")
        whole = "".join(l + "\n" for l in [
            user("caf\u00e9 \u2615 first", "2026-09-18T09:00:00.000Z"),
            recap("r\u00e9sum\u00e9 of the work", "2026-09-18T09:30:00.000Z"),
            user("\u2192 second", "2026-09-18T10:00:00.000Z")]).encode("utf-8")
        open(path, "wb").write(whole)
        one_go = self.update()[self.SID]
        idx = {}
        for i in range(0, len(whole), 7):        # seven bytes at a time, mid-character
            open(path, "wb").write(whole[:i + 7])
            idx = self.update(idx)
        piecemeal = idx[self.SID]
        for field in ("you_said", "opened", "recap", "turns_since_recap", "last_ts"):
            self.assertEqual(piecemeal[field], one_go[field], field)

    def test_a_partial_line_does_not_grow_without_bound(self):
        path = os.path.join(self.proj, f"{self.SID}.jsonl")
        with open(path, "wb") as fh:
            fh.write((user("a finished line") + "\n").encode("utf-8"))
            fh.write(b'{"type":"user","message":{"content":"' + b"x" * 2_000_000)
        e = self.update()[self.SID]
        self.assertLess(len(json.dumps(e)), 50_000,
                        "an unfinished megabyte is not something to carry forever")
        self.assertEqual(e["you_said"], "a finished line")

    def test_an_oversized_partial_line_resynchronises_at_the_next_record(self):
        path = os.path.join(self.proj, f"{self.SID}.jsonl")
        with open(path, "wb") as fh:
            fh.write(b'{"type":"user","message":{"content":"' + b"x" * 2_000_000)
        idx = self.update()
        with open(path, "ab") as fh:
            fh.write(b'"}}\n' + (user("back on track") + "\n").encode("utf-8"))
        idx = self.update(idx)
        self.assertEqual(idx[self.SID]["you_said"], "back on track")


class TestPersistence(Base):
    def test_it_survives_a_round_trip(self):
        self.write("fff11111-0000-4000-8000-000000000006", [user("remember me")])
        idx = self.update()
        path = os.path.join(self.tmp, "index.jsonl")
        index.save(idx, path)
        back = index.load(path)
        self.assertEqual(back, idx)

    def test_a_missing_index_is_an_empty_one(self):
        self.assertEqual(index.load(os.path.join(self.tmp, "nothing.jsonl")), {})

    def test_a_corrupt_line_does_not_lose_the_rest(self):
        path = os.path.join(self.tmp, "index.jsonl")
        with open(path, "w") as fh:
            fh.write('{"sessionId":"a","title":"kept"}\n{ torn\n'
                     '{"sessionId":"b","title":"also kept"}\n')
        back = index.load(path)
        self.assertEqual(sorted(back), ["a", "b"])

    def test_the_write_is_atomic(self):
        path = os.path.join(self.tmp, "index.jsonl")
        index.save({"a": {"sessionId": "a"}}, path)
        self.assertFalse(os.path.exists(path + ".tmp"))

    def test_two_writers_do_not_share_a_temp_file(self):
        # a watch, a one-shot and the launchd save can all write at once; one
        # truncating the other's temp file publishes half an index
        path = os.path.join(self.tmp, "index.jsonl")
        seen = []
        real_replace = index.os.replace

        def spy(src, dst):
            seen.append(src)
            return real_replace(src, dst)

        index.os.replace = spy
        try:
            index.save({"a": {"sessionId": "a"}}, path)
            index.save({"b": {"sessionId": "b"}}, path)
        finally:
            index.os.replace = real_replace
        self.assertNotEqual(seen[0], seen[1], "each writer needs its own temp file")


class TestSearch(Base):
    def entries(self):
        return {
            "live0001-0000-4000-8000-000000000001": {
                "sessionId": "live0001-0000-4000-8000-000000000001",
                "title": "Issue 362", "recap": "memory bloat in the loop",
                "you_said": "rebase and land", "opened": "fix 362",
                "project": "liveapp", "last_ts": "2026-09-20T10:00:00.000Z"},
            "dead0002-0000-4000-8000-000000000002": {
                "sessionId": "dead0002-0000-4000-8000-000000000002",
                "title": "Docker high CPU", "recap": "containers pegged a core",
                "you_said": "why is docker hot", "opened": "docker cpu",
                "project": "liveapp", "last_ts": "2026-09-01T10:00:00.000Z"},
        }

    def test_it_finds_by_a_word_in_the_recap(self):
        hits = index.search(self.entries(), "pegged")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["title"], "Docker high CPU")

    def test_every_word_has_to_match(self):
        self.assertEqual(index.search(self.entries(), "docker bloat"), [])
        self.assertEqual(len(index.search(self.entries(), "docker cpu")), 1)

    def test_it_finds_by_short_id(self):
        self.assertEqual(len(index.search(self.entries(), "dead")), 1)

    def test_the_newest_comes_first(self):
        hits = index.search(self.entries(), "liveapp")
        self.assertEqual(hits[0]["title"], "Issue 362")

    def test_live_sessions_outrank_older_ended_ones(self):
        hits = index.search(self.entries(), "liveapp",
                            live_ids={"dead0002-0000-4000-8000-000000000002"})
        self.assertEqual(hits[0]["title"], "Docker high CPU")
        self.assertTrue(hits[0]["live"])
        self.assertFalse(hits[1]["live"])

    def test_no_query_finds_nothing(self):
        self.assertEqual(index.search(self.entries(), ""), [])



class TestGrep(Base):
    """A keyword search of what was said in a session - your prompts and
    Claude's replies (the owner, 2026-10-05: "a basic case independent grep";
    then, after review 1: what was said, not the whole file). Every word, in
    the same session, any case."""

    A = "aaaa1111-0000-4000-8000-000000000001"
    B = "bbbb2222-0000-4000-8000-000000000002"

    def entries(self):
        return self.update()

    def found(self, query, idx=None, **kw):
        """The ids the grep found, or None when it was stopped."""
        got = index.grep(self.entries() if idx is None else idx, query, **kw)
        return None if got is None else set(got)

    def test_a_word_in_a_prompt_or_a_reply_any_case(self):
        self.write(self.A, [user("deploy the Flamingo build"), assistant("done")])
        self.write(self.B, [user("hello"), assistant("The FLAMINGO build is green")])
        for q in ("flamingo", "Flamingo", "FLAMINGO", "flam"):
            with self.subTest(q=q):
                self.assertEqual(self.found(q), {self.A, self.B})
        self.assertEqual(self.found("pelican"), set())          # control

    def test_every_word_in_the_same_session(self):
        self.write(self.A, [user("flamingo"), assistant("ok")])
        self.write(self.B, [assistant("pelican")])
        self.assertEqual(self.found("flamingo pelican"), set())
        self.write(self.A, [user("flamingo"), assistant("and a pelican")])     # two records
        self.assertEqual(self.found("pelican flamingo"), {self.A})

    def test_not_what_claude_code_stored_or_a_tool_printed(self):
        # the skills, CLAUDE.md, a reminder, a tool's output, a key, the branch:
        # in every session, so a common word found them all (review 1)
        self.write(self.A, [
            rec(type="attachment", timestamp="2026-09-18T10:00:00.000Z",
                attachment={"type": "skill_listing", "content": "flamingo skill"}),
            rec(type="system", subtype="informational", content="flamingo",
                timestamp="2026-09-18T10:00:00.000Z"),
            rec(type="attachment", timestamp="2026-09-18T10:00:00.000Z",
                message={"role": "user", "content": "flamingo as a message"}),
            rec(type="system", timestamp="2026-09-18T10:00:00.000Z",
                message={"role": "user", "content": "flamingo as a system message"}),
            rec(type="user", timestamp="2026-09-18T10:00:00.000Z", gitBranch="flamingo",
                message={"role": "user",
                         "content": "<system-reminder>flamingo in CLAUDE.md</system-reminder>"}),
            rec(type="user", timestamp="2026-09-18T10:00:00.000Z",
                message={"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "flamingo log"}]}),
            rec(type="assistant", timestamp="2026-09-18T10:00:00.000Z", message={
                "role": "assistant", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash",
                     "input": {"command": "grep flamingo"}}]}),
            user("something else"),
        ])
        self.assertEqual(self.found("flamingo"), set())
        self.assertEqual(self.found("role"), set(), "a key is not said")
        self.write(self.B, [user("hello"), assistant("the flamingo log is clean")])  # control
        self.assertEqual(self.found("flamingo"), {self.B})

    TS = "2026-09-18T10:00:00.000Z"

    def test_a_slash_command_is_said_as_typed_not_as_its_tags(self):
        # how Claude Code records `/compact` you typed: "args" found 43 real
        # sessions through these tags alone (review 2)
        self.write(self.A, [user("<command-name>/compact</command-name>\n"
                                 "<command-message>compact</command-message>\n"
                                 "<command-args></command-args>"), assistant("ok")])
        self.write(self.B, [user("<command-name>/review</command-name>\n"
                                 "<command-args>the gate flake</command-args>")])
        self.assertEqual(self.found("args"), set(), "a tag is not said")
        self.assertEqual(self.found("message"), set(), "a tag is not said")
        self.assertEqual(self.found("/compact"), {self.A})        # control
        self.assertEqual(self.found("gate flake"), {self.B})

    def test_a_prompt_typed_while_claude_works_is_said(self):
        # queued: only an attachment, never a user record (review 2: 159 of them
        # in the 150 newest transcripts)
        def queued(prompt, **kw):
            return rec(type="attachment", timestamp=self.TS, attachment=dict(
                {"type": "queued_command", "prompt": prompt, "commandMode": "prompt",
                 "origin": {"kind": "human"}}, **kw))
        self.write(self.A, [user("first"), queued("and the flamingo too")])
        self.write(self.B, [user("first"), queued("<task-notification>flamingo</task-notification>",
                                                  commandMode="task-notification")])
        self.assertEqual(self.found("flamingo"), {self.A})
        self.write(self.B, [user("first"), queued("flamingo from a peer", origin={"kind": "peer"})])
        self.assertEqual(self.found("flamingo"), {self.A}, "not a person's")

    def test_an_interrupt_is_not_said(self):
        self.write(self.A, [user("deploy it"), user("[Request interrupted by user for tool use]")])
        self.assertEqual(self.found("interrupted"), set())
        self.assertEqual(self.found("deploy"), {self.A})          # control

    def test_what_the_harness_writes_as_meta_is_not_said(self):
        self.write(self.A, [user("deploy it"),
                            rec(type="user", isMeta=True, timestamp=self.TS, message={
                                "role": "user", "content": [{"type": "text", "text":
                                    "[Image: original 1932x2188, displayed at 1766x2000.]"}]}),
                            rec(type="user", isMeta=True, timestamp=self.TS, message={
                                "role": "user", "content": "(Re-invocation of /land - the skill"
                                " instructions were previously loaded)"})])
        self.assertEqual(self.found("displayed"), set())
        self.assertEqual(self.found("instructions"), set())
        self.assertEqual(self.found("deploy"), {self.A})          # control

    def test_what_the_harness_writes_as_a_reply_is_not_said(self):
        # "No response requested.", an API error, a spend limit: a reply of
        # model "<synthetic>" (review 3: 29 of the 300 newest transcripts)
        self.write(self.A, [user("hello"), rec(type="assistant", timestamp=self.TS, message={
            "role": "assistant", "model": "<synthetic>",
            "content": [{"type": "text", "text": "No response requested."}]})])
        self.write(self.B, [user("hello"), assistant("none was requested")])
        self.assertEqual(self.found("requested"), {self.B})

    def test_one_odd_record_does_not_cost_the_rest(self):
        self.write(self.A, [user("flamingo")])
        idx = self.entries()
        # B's entry by hand: the index build (update) is not what is tested here
        # the word is in the odd records too, outside what was said: they are parsed
        odd = self.write(self.B, [rec(type="user", timestamp=self.TS, message={
                                      "role": "user", "content": [{"type": "text", "text": None},
                                                                  {"type": "image", "alt": "flamingo"}]}),
                                  rec(type="assistant", timestamp=self.TS, message="flamingo"),
                                  user("flamingo")])
        idx[self.B] = {"sessionId": self.B, "path": odd, "entrypoint": "cli"}
        self.assertEqual(self.found("flamingo", idx=idx), {self.A, self.B})

    def test_letters_beyond_a_z_in_any_case(self):
        self.write(self.A, [user("Ärger im Büro"), assistant("ÉCOLE fermée")])
        for q in ("Ärger", "ärger", "ÄRGER", "école", "ÉCOLE", "büro"):
            with self.subTest(q=q):
                self.assertEqual(self.found(q), {self.A})

    def test_a_quote_or_a_backslash_as_it_was_said(self):
        # the file has them escaped (\" and \\): what was said has them as typed
        self.write(self.A, [user('run "deploy" now'), assistant("saved to C:\\temp\\new")])
        for q in ('"deploy"', 'deploy"', "c:\\temp", "temp\\new"):
            with self.subTest(q=q):
                self.assertEqual(self.found(q), {self.A})

    def test_a_programs_session_only_with_everything(self):
        self.write(self.A, [rec(type="user", entrypoint="sdk-cli", timestamp="2026-09-18T10:00:00.000Z",
                                message={"role": "user", "content": "flamingo"})])
        self.assertEqual(self.found("flamingo"), set())
        self.assertEqual(self.found("flamingo", everything=True), {self.A})

    def test_a_stop_asked_for_ends_it_with_no_answer(self):
        self.write(self.A, [user("flamingo")])
        self.write(self.B, [user("flamingo")])
        self.assertIsNone(self.found("flamingo", stop=lambda: True))
        self.assertEqual(self.found("flamingo", stop=lambda: False),
                         {self.A, self.B})                                     # control

    def test_a_stop_while_a_transcript_is_read_has_no_answer(self):
        # review-plan1 F6: the stop is asked for each line the store reads
        self.write(self.A, [user("one"), user("two"), user("three"), user("flamingo")])
        store = index.SaidStore()
        self.assertIsNone(self.found("flamingo", store=store, stop=lambda: store.parsed >= 2))
        self.assertEqual(store.files, {}, "a stopped read keeps nothing")
        self.assertEqual(self.found("flamingo", store=store), {self.A})         # control

    def test_a_stop_while_it_searches_what_was_read_has_no_answer(self):
        self.write(self.A, [user("flamingo")])
        store = index.SaidStore()
        idx = self.entries()
        self.assertEqual(self.found("flamingo", idx=idx, store=store), {self.A})   # read
        asked = []
        # the first ask is before the session; the second before its first message
        second = lambda: asked.append(1) or len(asked) >= 2         # noqa: E731
        self.assertIsNone(self.found("flamingo", idx=idx, store=store, stop=second))
        asked.clear()
        third = lambda: asked.append(1) or len(asked) >= 3          # noqa: E731
        self.assertEqual(self.found("flamingo", idx=idx, store=store, stop=third),
                         {self.A}, "one session, one message: two asks")     # control

    def test_a_transcript_gone_unreadable_or_cut_is_passed_over(self):
        self.write(self.A, [user("flamingo")])
        p = self.write(self.B, [user("flamingo")])
        idx = self.entries()
        os.remove(p)
        self.assertEqual(self.found("flamingo", idx=idx), {self.A})
        idx[self.A] = dict(idx[self.A], path=None)
        self.assertEqual(self.found("flamingo", idx=idx), set())
        cut = self.write(self.B, [user("hello")])
        with open(cut, "a") as fh:
            fh.write('{"type": "user", "message": {"content": "flamingo')     # a line cut off
        self.assertEqual(self.found("flamingo"), {self.A}, "not B")

    def test_no_words_finds_nothing(self):
        self.write(self.A, [user("flamingo")])
        for q in ("", "   ", None):
            with self.subTest(q=q):
                self.assertEqual(self.found(q), set())


def queued(prompt, ts="2026-09-18T10:02:00.000Z"):
    """A prompt typed while Claude worked: only an attachment, never a user record."""
    return rec(type="attachment", timestamp=ts, attachment={
        "type": "queued_command", "prompt": prompt, "commandMode": "prompt",
        "origin": {"kind": "human"}})


class TestWordForms(unittest.TestCase):
    """The owner remembered "wake"; what was said was "it hasn't woken the loop"
    (2026-10-07). A word also finds its forms - forward only, from the word to
    its forms: `coding` finding every "Claude Code" took 25 sessions to 102, and
    `willing`, `said`, `taken` widened the same way (review-plan2 F1)."""

    def test_the_keys(self):
        same = [("wake", "wakes", "waking", "woke", "woken"),
                ("merge", "merged", "merges", "merging"),
                ("fix", "fixes", "fixed", "fixing"),
                ("run", "runs", "running", "ran"),
                ("try", "tries", "tried", "trying"),
                ("use", "used", "uses", "using"),
                ("plan", "plans", "planned", "planning"),
                ("hope", "hoped", "hoping"),
                ("choose", "chose", "chosen", "choosing"),
                ("apply", "applies", "applied", "applying"),          # y -> i, not in the table
                ("agree", "agreed", "agrees")]
        for group in same:
            with self.subTest(group=group):
                self.assertEqual({index.word_key(w) for w in group}, {index.word_key(group[0])})
        for a, b in (("note", "not"), ("use", "us"), ("plan", "plane"), ("hop", "hope"),
                     ("win", "won"), ("stick", "stuck"), ("leave", "left"), ("lead", "led"),
                     # left out of the table on purpose: each is a word of its own
                     ("bite", "bit"), ("make", "made"), ("meet", "met"), ("sit", "sat"),
                     ("shoot", "shot"), ("light", "lit"), ("feed", "fed"), ("wear", "wore"),
                     ("tear", "tore"), ("go", "went"), ("go", "gone"), ("see", "saw"),
                     ("do", "did"), ("get", "got")):
            with self.subTest(apart=(a, b)):
                self.assertNotEqual(index.word_key(a), index.word_key(b))
        # Porter's own 5a: no vowel-consonant pair before the e keeps it
        self.assertEqual(index.word_key("true"), "true")
        self.assertEqual(index.word_key("does"), "doe")
        # Porter's eed: a word that only looks like one keeps it
        self.assertEqual(index.word_key("speed"), "speed")

    def match(self, word, text):
        return index.Word(word).match(text.lower())

    def test_a_word_finds_its_forms(self):
        for word, text in (("wake", "I merged 41 but it hasn't woken the loop"),
                           ("wake", "it woke."),
                           ("wake", "it is waking"),
                           ("merge", "after (merging) it"),
                           ("code", "coding it"),
                           ("hope", "hoping so"),
                           ("choose", "he chose it"),
                           ("use", "using it"),
                           ("try", "tried again"),
                           ("run", "it ran")):
            with self.subTest(word=word, text=text):
                self.assertEqual(self.match(word, text), index.FORM)

    def test_a_form_does_not_find_its_word(self):
        # forward only: type the word itself to find all its forms
        for word, text in (("woken", "wake the loop"), ("woke", "did not wake."),
                           ("merged", "we merge it"), ("waking", "it wakes"),
                           ("coding", "claude code"), ("willing", "I will do it"),
                           ("living", "it is live"), ("said", "say it"),
                           ("taken", "take it"), ("chose", "we choose"),
                           ("running", "run it"), ("tries", "I will try")):
            with self.subTest(word=word, text=text):
                self.assertIsNone(self.match(word, text))
        self.assertEqual(self.match("woken", "it hasn't woken"), index.TYPED)  # control

    def test_an_irregular_form_is_a_whole_word(self):
        # "ran" is inside "branch", "ate" inside "update" (review-plan2 F7)
        for word, text in (("run", "the branch"), ("eat", "update it"), ("wake", "awoken")):
            with self.subTest(word=word):
                self.assertIsNone(self.match(word, text))
        self.assertEqual(self.match("run", "it ran"), index.FORM)              # control

    def test_as_typed_is_a_substring_as_before(self):
        for word, text in (("merge", "I merged 41"), ("flam", "the flamingo"),
                           ("set", "open settings.json"), ("#41", "merged #41"),
                           ('"deploy"', 'run "deploy" now'), ("c:\\temp", "saved to c:\\temp"),
                           ("école", "une école"), ("on", "moon")):
            with self.subTest(word=word):
                self.assertEqual(self.match(word, text), index.TYPED)

    def test_no_form_where_it_would_be_another_word(self):
        for word, text in (("settings", "set the flag"), ("news", "a new one"),
                           ("win", "this won't work"), ("stick", "it is stuck"),
                           ("leave", "the left pane"), ("lead", "it led nowhere"),
                           ("see", "I saw it"), ("done", "do it now"), ("got", "get it"),
                           ("note", "not now"), ("fixed", "fixing")):
            with self.subTest(word=word, text=text):
                self.assertIsNone(self.match(word, text))
        self.assertEqual(self.match("wake", "it woke"), index.FORM)            # control

    def test_a_long_run_of_letters_is_no_word(self):
        # review-build3 F1: Porter's y-rule recursed once per "y" - a pasted
        # blob of them raised, and the grep lost every session with it
        for blob in ("y" * 5000, "ab" * 3000):
            with self.subTest(blob=blob[:4]):
                self.assertEqual(index.word_key(blob), blob)
        self.assertEqual(self.match("wake", "an awoken loop " + "y" * 1500), None)
        self.assertEqual(self.match("wake", "it woke"), index.FORM)              # control

    def test_only_letters_a_to_z_have_forms(self):
        for word, text in (("pr41", "pr41s"), ("wake2", "woke2"), ("éveil", "éveils")):
            with self.subTest(word=word):
                self.assertNotEqual(self.match(word, text), index.FORM)
        self.assertEqual(self.match("wake", "woke"), index.FORM)               # control


class TestSearchFindsForms(Base):
    def entries(self):
        return {"aaaa1111-0000-4000-8000-000000000001": {
                    "sessionId": "aaaa1111-0000-4000-8000-000000000001",
                    "title": "the loop woke up late", "recap": "", "you_said": "x",
                    "opened": "x", "project": "liveapp", "last_ts": "2026-09-20T10:00:00.000Z"}}

    def test_a_title_found_by_a_form_ranks_as_a_title(self):
        hits = index.search(self.entries(), "wake")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["rank"], 0, "a title is about the session")
        self.assertEqual(index.search(self.entries(), "sleep"), [])           # control


class TestTheBestMessage(Base):
    """What the grep answers for each session it finds: the one message that
    says most of what you typed - so the list can show it, and rank by it."""

    A = "aaaa1111-0000-4000-8000-000000000001"
    B = "bbbb2222-0000-4000-8000-000000000002"

    def said(self, query, sid=None, **kw):
        got = index.grep(self.update(), query, **kw)
        return got[sid or self.A] if got and (sid or self.A) in got else None

    def test_it_says_what_was_said_by_whom_and_when(self):
        self.write(self.A, [user("hello"), queued("I merged 41 but it hasn't woken the loop")])
        s = self.said("wake merge")
        self.assertEqual(s["text"], "I merged 41 but it hasn't woken the loop")
        self.assertEqual(s["who"], "you", "a queued prompt is yours")
        self.assertEqual(s["ts"], "2026-09-18T10:02:00.000Z")
        self.assertEqual(s["key"], (2, True, 1))

    def test_more_words_in_one_message_wins(self):
        self.write(self.A, [user("the wake"), assistant("wake after the merge")])
        self.assertEqual(self.said("wake merge")["text"], "wake after the merge")

    def test_yours_before_claudes(self):
        self.write(self.A, [assistant("wake after the merge"), user("merge, then wake it")])
        self.assertEqual(self.said("wake merge")["text"], "merge, then wake it")

    def test_as_typed_before_a_form(self):
        # review-plan1 F1/F2: what holds the word as you typed it says most
        self.write(self.A, [user("I merged 41 but it hasn't woken the loop"),
                            user("wake the loop", ts="2026-09-18T11:00:00.000Z")])
        self.assertEqual(self.said("wake")["text"], "wake the loop", "not the first")
        self.write(self.A, [user("I merged 41 but it hasn't woken the loop")])   # control
        self.assertEqual(self.said("wake")["text"], "I merged 41 but it hasn't woken the loop")

    def test_only_short_words_rank_by_them_all(self):
        # review-plan2 F10: with no word of three letters, every word counts
        self.write(self.A, [assistant("the pr and ci are green"), user("my pr")])
        self.assertEqual(self.said("pr ci")["text"], "the pr and ci are green")

    def test_short_words_are_needed_but_do_not_rank(self):
        # "on", "up", "pr" are inside almost every long message (review-plan1 F1)
        self.write(self.A, [user("wake the loop"), assistant("on and on, up and up, pr")])
        self.assertEqual(self.said("on up wake")["text"], "wake the loop")
        self.write(self.B, [user("wake the loop")])
        self.assertIsNone(self.said("on up wake", sid=self.B), "no 'on' said in B")
        self.assertIsNotNone(self.said("wake", sid=self.B))                     # control

    def test_the_first_of_equals(self):
        self.write(self.A, [user("wake one"), user("wake two")])
        self.assertEqual(self.said("wake")["text"], "wake one")

    def test_a_long_search_word_is_no_word_either(self):
        # review-build4 F4: the query word went to Porter uncut
        for word in ("y" * 1500 + "ing", "ba" + "y" * 1500 + "eed"):
            with self.subTest(word=word[-6:]):
                self.assertIsNone(index.Word(word).key)
                self.assertEqual(index.search({"x": {"sessionId": "x", "title": "wake"}}, word), [])
        self.assertEqual(index.Word("wake").key, "wake")                         # control

    def test_a_blob_in_one_message_costs_nothing_else(self):
        self.write(self.A, [user("an awoken loop " + "y" * 1500)])
        self.write(self.B, [user("it is waking")])
        self.assertEqual(set(index.grep(self.update(), "wake")), {self.B})

    def test_a_word_twice_counts_once(self):
        self.write(self.A, [user("wake the loop")])
        self.assertEqual(self.said("wake wake")["key"], self.said("wake")["key"])

    def test_the_text_starts_near_the_match(self):
        long = "x" * 490 + " it hasn't woken " + "y" * 100
        self.write(self.A, [user(long)])
        s = self.said("wake")
        self.assertIn("woken", s["text"], "a match by a form is shown too")
        self.assertTrue(s["text"].startswith("…"))
        self.assertLessEqual(len(s["text"]), index.TEXT_CAP)
        self.assertTrue(s["text"].startswith("\u2026it hasn't"), "from a word's start")
        self.write(self.A, [user("short and woken")])
        self.assertEqual(self.said("wake")["text"], "short and woken")           # control

    def test_a_short_word_does_not_move_the_text(self):
        # review-build1 F2: "on" is inside "Long" and "content" - the shown text
        # began there, and the word that matters was cut off
        long = "Long preamble about the content of the project. " * 12 + \
            "but it hasn't woken the loop"
        self.write(self.A, [user(long)])
        for q in ("on wake", "pr wake", "wake"):
            with self.subTest(q=q):
                text = self.said(q)["text"]
                self.assertTrue(text.startswith("\u2026"))
                self.assertIn("woken", text[:60])
        self.write(self.A, [user("x" * 400 + " the pr and ci")])                  # control
        self.assertIn("pr and ci", self.said("pr ci")["text"][:40])

    def test_the_first_form_or_word_is_where_the_text_starts(self):
        # the earliest of what it holds, a form or the word as typed
        self.write(self.A, [user("x " * 60 + "it woke early. " + "y " * 200 + "then wake again")])
        self.assertIn("woke early", self.said("wake")["text"][:50])

    def test_escapes_and_controls_are_not_shown(self):
        self.write(self.A, [queued("\x1b[31mwake\x1b[0m merge\x07 now")])
        text = self.said("wake merge")["text"]
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertIn("wake merge now", text)


class TestTheSaidStore(Base):
    """What was said, kept for the life of the list: read once, then only what
    was appended - so a search is a scan of what was said, not of 1 GB."""

    A = "aaaa1111-0000-4000-8000-000000000001"

    def test_it_reads_only_what_was_appended(self):
        p = self.write(self.A, [user("one"), assistant("two")])
        store = index.SaidStore()
        self.assertEqual([m[2] for m in store.messages(p)], ["one", "two"])
        self.write(self.A, [user("three")], mode="a")
        parsed = store.parsed
        self.assertEqual([m[2] for m in store.messages(p)], ["one", "two", "three"])
        self.assertEqual(store.parsed, parsed + 1, "only the new line")
        self.assertEqual([m[0] for m in store.messages(p)], [True, False, True], "yours")

    def test_an_unfinished_line_is_read_once_it_is_finished(self):
        p = self.write(self.A, [user("one")])
        line = user("flamingo")
        with open(p, "a") as fh:
            fh.write(line[:20])
        store = index.SaidStore()
        self.assertEqual([m[2] for m in store.messages(p)], ["one"])
        with open(p, "a") as fh:
            fh.write(line[20:] + "\n")
        self.assertEqual([m[2] for m in store.messages(p)], ["one", "flamingo"])

    def test_a_replaced_or_shorter_file_is_read_again(self):
        p = self.write(self.A, [user("one"), user("two")])
        store = index.SaidStore()
        store.messages(p)
        tmp = p + ".new"
        with open(tmp, "w") as fh:
            fh.write(user("other") + "\n" + user("more") + "\n" + user("again") + "\n")
        os.replace(tmp, p)                                   # a new inode, longer
        self.assertEqual([m[2] for m in store.messages(p)], ["other", "more", "again"])
        with open(p, "w") as fh:                             # same inode, shorter
            fh.write(user("cut") + "\n")
        self.assertEqual([m[2] for m in store.messages(p)], ["cut"])

    def test_a_prompt_about_tool_results_is_said(self):
        # review-plan2 F5: only the quoted marker of a record is passed over
        p = self.write(self.A, [user("why is tool_result empty"),
                                user('the "tool_result" key is missing')])
        store = index.SaidStore()
        self.assertEqual([m[2] for m in store.messages(p)],
                         ["why is tool_result empty", 'the "tool_result" key is missing'])

    def test_a_stopped_read_keeps_the_transcripts_read_before_it(self):
        # review-plan2 F4: else every new search text starts the 5-9 s read again
        a = self.write(self.A, [user("flamingo")])
        b = self.write("bbbb2222-0000-4000-8000-000000000002", [user("one"), user("two")])
        idx = self.update()
        store = index.SaidStore()
        order = [e["path"] for e in idx.values()]
        first = order[0]
        stop = lambda: first in store.files and store.parsed >= 2      # noqa: E731
        self.assertIsNone(index.grep(idx, "flamingo", store=store, stop=stop))
        self.assertEqual(set(store.files), {first})
        parsed = store.parsed
        self.assertEqual(set(index.grep(idx, "flamingo", store=store)), {self.A})
        self.assertEqual(store.parsed - parsed, 2 if first == a else 1,
                         "only the one not read yet")
        self.assertEqual(set(store.files), {a, b})

    def test_a_stopped_grep_forgets_no_transcript(self):
        # review-plan2 F8
        p = self.write(self.A, [user("flamingo")])
        store = index.SaidStore()
        index.grep(self.update(), "flamingo", store=store)
        os.remove(p)                                       # Claude Code deleted it
        q = self.write("bbbb2222-0000-4000-8000-000000000002", [user("flamingo")])
        idx = self.update()
        self.assertNotIn(p, [e["path"] for e in idx.values()])
        self.assertIsNone(index.grep(idx, "flamingo", store=store, stop=lambda: True))
        self.assertIn(p, store.files, "stopped before it saw every transcript")
        index.grep(idx, "flamingo", store=store)                             # control
        self.assertEqual(set(store.files), {q})

    def test_a_compact_line_with_a_text_block_is_said(self):
        # review-build1 F4: real transcripts are compact JSON, the fixtures are not
        def compact(**kw):
            return json.dumps(kw, separators=(",", ":"))
        p = self.write(self.A, [
            compact(type="user", timestamp="2026-09-18T10:00:00.000Z", message={"role": "user", "content": [
                {"type": "text", "text": "said beside it"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "log"}]}),
            compact(type="user", timestamp="2026-09-18T10:00:00.000Z", message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "a long log"}]})])
        store = index.SaidStore()
        self.assertEqual([m[2] for m in store.messages(p)], ["said beside it"])
        self.assertEqual(store.parsed, 1, "the compact tool output alone is not parsed")

    def test_an_unreadable_transcript_keeps_what_was_read(self):
        # the failure at open, as root would see it too (review-build2 F2)
        p = self.write(self.A, [user("one")])
        store = index.SaidStore()
        store.messages(p)
        self.write(self.A, [user("two")], mode="a")
        with mock.patch.object(index, "open", side_effect=PermissionError(13, "denied"),
                               create=True):
            self.assertEqual([m[2] for m in store.messages(p)], ["one"])
        self.assertEqual([m[2] for m in store.messages(p)], ["one", "two"])        # control

    def test_a_tool_output_is_not_parsed_but_a_text_beside_it_is(self):
        p = self.write(self.A, [
            rec(type="user", timestamp="2026-09-18T10:00:00.000Z", message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "a long log"}]}),
            rec(type="user", timestamp="2026-09-18T10:00:00.000Z", message={"role": "user", "content": [
                {"type": "text", "text": "said beside it"},
                {"type": "tool_result", "tool_use_id": "t2", "content": "log"}]})])
        store = index.SaidStore()
        self.assertEqual([m[2] for m in store.messages(p)], ["said beside it"])
        self.assertEqual(store.parsed, 1, "the tool output alone is never parsed")

    def test_a_transcript_that_is_gone_is_dropped(self):
        p = self.write(self.A, [user("flamingo")])
        store = index.SaidStore()
        idx = self.update()
        index.grep(idx, "flamingo", store=store)
        self.assertIn(p, store.files)
        index.grep({}, "flamingo", store=store)
        self.assertEqual(store.files, {})

    def test_a_store_knows_the_code_that_made_it(self):
        # the list keeps its store across every reload while this is the same;
        # an edit to the code that says what was said reads again (review-build5)
        import ccwho_brief
        import ccwho_text
        self.assertEqual(set(index.SOURCES),
                         {index.__file__, ccwho_text.__file__, ccwho_brief.__file__})
        self.assertEqual(index.SaidStore().source, index.SOURCE)
        self.assertEqual(index.SOURCE, index.source_key(), "the code as it was loaded")

    def test_a_store_carries_the_code_as_loaded_not_the_files_now(self):
        # review-build6 F2: an edit not loaded yet must not name a store
        a = os.path.join(self.tmp, "edited.py")
        with open(a, "w") as fh:
            fh.write("an edit that has landed but is not loaded\n")
        with mock.patch.object(index, "SOURCES", (a,)):
            self.assertNotEqual(index.source_key(), index.SOURCE)
            self.assertEqual(index.SaidStore().source, index.SOURCE)

    def test_the_key_is_the_code_itself(self):
        a = os.path.join(self.tmp, "a.py")
        b = os.path.join(self.tmp, "b.py")
        for path, text in ((a, "x = 1\n"), (b, "y = 2\n")):
            with open(path, "w") as fh:
                fh.write(text)
        key = index.source_key((a, b))
        self.assertEqual(index.source_key((a, b)), key)                          # control
        with open(a, "w") as fh:
            fh.write("x = 3\n")
        self.assertNotEqual(index.source_key((a, b)), key)

    def test_the_grep_uses_the_store_it_is_given(self):
        self.write(self.A, [user("flamingo")])
        store = index.SaidStore()
        idx = self.update()
        index.grep(idx, "flamingo", store=store)
        parsed = store.parsed
        self.assertEqual(set(index.grep(idx, "flamingo", store=store)), {self.A})
        self.assertEqual(store.parsed, parsed, "nothing read again")


if __name__ == "__main__":
    unittest.main()
