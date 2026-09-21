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


if __name__ == "__main__":
    unittest.main()
