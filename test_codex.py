"""Codex threads as rows (Phase 3; the owner's D15-D17, 2026-09-29). A thread is
open when a process holds its lock file, $CODEX_HOME/thread-writer-locks/
<thread id>.lock, open: that process is its host (the ChatGPT app, a VS Code
window, the codex CLI). Measured: no shared daemon runs, each host serves its
own threads, and an idle thread keeps its lock - so "open", never "busy".
Nothing here reads the machine: a fake CODEX_HOME and fake lsof text."""
import json
import os
import shutil
import tempfile
import unittest

import ccwho_engine as engine
import ccwho_procs as procs

A = "01a0eca7-7b42-72f0-b19a-ff0ae32db6a3"
B = "01a0eca7-7be2-7d71-9d22-2515b52c97c4"
C = "01a0eca7-0000-7000-8000-00000000000c"


def lsof_text(held, home):
    """lsof -Fpn output: process sets (p) and the files each holds (n) - by the
    RESOLVED path, as the kernel names them (a temp folder is behind /var ->
    /private/var on macOS)."""
    real = os.path.realpath(home)
    out = []
    for pid, ids in held.items():
        out.append(f"p{pid}")
        for i in ids:
            out.append(f"n{real}/thread-writer-locks/{i}.lock")
    return "\n".join(out) + "\n"


class Home:
    """A CODEX_HOME with lock files, an index and rollouts."""

    def __init__(self):
        self.path = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.path, "thread-writer-locks"))
        os.makedirs(os.path.join(self.path, "sessions", "2026", "09", "29"))

    def lock(self, *ids):
        for i in ids + (".coordination",):
            open(os.path.join(self.path, "thread-writer-locks", f"{i}.lock"), "w").close()

    def index(self, *entries):
        with open(os.path.join(self.path, "session_index.jsonl"), "a") as fh:
            for thread, name in entries:
                fh.write(json.dumps({"id": thread, "thread_name": name,
                                     "updated_at": "2026-09-29T11:16:00Z"}) + "\n")

    def rollout(self, thread, cwd="/Users/x/liveapp", originator="Codex Desktop",
                source="vscode"):
        path = os.path.join(self.path, "sessions", "2026", "09", "29",
                            f"rollout-2026-09-29T11-13-00-{thread}.jsonl")
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "session_meta", "payload": {
                "id": thread, "cwd": cwd, "originator": originator, "source": source}}) + "\n")
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message",
                                                                  "message": "secret words"}}) + "\n")
        return path

    def done(self):
        shutil.rmtree(self.path, ignore_errors=True)


class TestTheLocksSayWhichThreadsAreOpen(unittest.TestCase):
    def test_a_held_lock_is_an_open_thread_and_its_holder_the_host(self):
        home = "/Users/x/.codex"
        text = lsof_text({4215: [A, B], 3795: [C]}, home)
        self.assertEqual(procs.parse_lsof_locks(text, home + "/thread-writer-locks"),
                         {A: 4215, B: 4215, C: 3795})

    def test_only_thread_locks_count(self):
        home = "/Users/x/.codex"
        text = ("p4215\nn/Users/x/.codex/thread-writer-locks/.coordination.lock\n"
                "n/Users/x/.codex/thread-writer-locks/not-a-thread.lock\n"
                "n/Users/x/.codex/state_5.sqlite\n"
                "n/Users/y/.codex/thread-writer-locks/" + A + ".lock\n"      # another home
                "n/Users/x/.codex/thread-writer-locks/" + B + ".lock\n")
        self.assertEqual(procs.parse_lsof_locks(text, home + "/thread-writer-locks"), {B: 4215})

    def test_two_holders_keep_the_first_lsof_lists(self):
        # lsof lists processes by pid: the lower pid is the host, every time
        home = "/Users/x/.codex"
        text = lsof_text({3000: [A], 5000: [A]}, home)
        self.assertEqual(procs.parse_lsof_locks(text, home + "/thread-writer-locks"), {A: 3000})

    def test_no_holder_is_no_thread(self):                              # control
        self.assertEqual(procs.parse_lsof_locks("", "/Users/x/.codex/thread-writer-locks"), {})


class TestCodexThreads(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.addCleanup(self.home.done)

    def read(self, held, lsof_ok=True):
        text = lsof_text(held, self.home.path) if lsof_ok else None
        return engine.codex_threads(self.home.path, lsof=lambda args: text, cache={})

    def test_an_open_thread_has_its_name_folder_and_host(self):
        self.home.lock(A)
        self.home.index((A, "fix the navbar"))
        self.home.rollout(A, cwd="/Users/x/liveapp", originator="Codex Desktop")
        threads = self.read({4215: [A]})
        keys = ("thread", "name", "cwd", "host_pid", "originator", "source", "turn")
        self.assertEqual([{k: t[k] for k in keys} for t in threads],
                         [{"thread": A, "name": "fix the navbar", "cwd": "/Users/x/liveapp",
                           "host_pid": 4215, "originator": "Codex Desktop", "source": "vscode",
                           "turn": None}])

    def test_the_newest_name_wins(self):
        self.home.lock(A)
        self.home.index((A, "first"))
        with open(os.path.join(self.home.path, "session_index.jsonl"), "a") as fh:
            fh.write("{half a line\n")              # a line Codex was still writing
        self.home.index((A, "renamed"))
        self.home.rollout(A)
        self.assertEqual(self.read({4215: [A]})[0]["name"], "renamed")

    def test_a_lock_without_a_transcript_is_no_row(self):
        # measured 2026-09-29: each thread came with a second lock, taken in the
        # same second, with no transcript - Codex's own, not a thread you see
        self.home.lock(A, B)
        self.home.rollout(A)
        self.assertEqual([t["thread"] for t in self.read({4215: [A, B]})], [A])

    def test_an_unheld_lock_is_no_open_thread(self):                   # control
        self.home.lock(A, B)
        self.home.rollout(A)
        self.home.rollout(B)
        self.assertEqual([t["thread"] for t in self.read({4215: [A]})], [A])

    def test_lsof_not_asked_is_none_not_nothing(self):
        # "not known" must never read as "no Codex thread is open"
        self.home.lock(A)
        self.assertIsNone(self.read({}, lsof_ok=False))

    def test_a_home_behind_a_symlink_still_finds_its_threads(self):
        # lsof names the resolved path; ~/.codex may be a dotfiles symlink
        self.home.lock(A)
        self.home.rollout(A)
        link = os.path.join(tempfile.mkdtemp(), "codex-link")
        os.symlink(self.home.path, link)
        self.addCleanup(shutil.rmtree, os.path.dirname(link), True)
        text = lsof_text({4215: [A]}, self.home.path)
        threads = engine.codex_threads(link, lsof=lambda args: text, cache={})
        self.assertEqual([t["thread"] for t in threads], [A])
        other = lsof_text({4215: [A]}, "/Users/y/.codex")              # control: another home
        self.assertFalse(engine.codex_threads(link, lsof=lambda args: other, cache={}))

    def test_a_folder_with_no_locks_asks_no_lsof(self):
        asked = []
        self.assertEqual(engine.codex_threads(self.home.path, lsof=lambda a: asked.append(a),
                                              cache={}), [])
        self.assertEqual(asked, [])

    def test_no_codex_home_is_nothing_and_asks_no_lsof(self):
        # lsof on a folder that is not there fails: that is "none", not "not known"
        asked = []
        self.assertEqual(engine.codex_threads("/nonexistent/.codex",
                                              lsof=lambda a: asked.append(a), cache={}), [])
        self.assertEqual(asked, [])

    def test_what_it_reads_is_the_first_line_only(self):
        # the transcript holds what was said: only session_meta is read
        self.home.lock(A)
        path = self.home.rollout(A)
        with open(path, "a") as fh:
            fh.write("not json at all\n")
        threads = self.read({4215: [A]})
        self.assertNotIn("secret words", json.dumps(threads))
        self.assertEqual(threads[0]["cwd"], "/Users/x/liveapp")

    def test_only_session_meta_gives_the_folder(self):
        self.home.lock(A)
        path = self.home.rollout(A)
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {"cwd": "/Users/x/said"}}) + "\n")
        self.assertEqual(self.read({4215: [A]})[0]["cwd"], "")

    def test_a_broken_first_line_leaves_the_folder_empty(self):
        self.home.lock(A)
        path = self.home.rollout(A)
        with open(path, "w") as fh:
            fh.write("{not json\n")
        self.assertEqual(self.read({4215: [A]})[0]["cwd"], "")

    def test_its_cache_keys_are_the_scan_caches_own_kind(self):
        # collect() sweeps the cache by `key.startswith("_")`: text keys only
        cache = {}
        self.home.lock(A)
        self.home.rollout(A)
        engine.codex_threads(self.home.path, lsof=lambda a: lsof_text({1: [A]}, self.home.path),
                             cache=cache)
        self.assertTrue(cache)
        self.assertTrue(all(isinstance(k, str) and k.startswith("_") for k in cache))

    def test_a_transcript_that_appears_later_is_found(self):
        # looked for again once the TTL passed: a new thread shows within it
        cache, now = {}, [1000.0]
        self.home.lock(B)
        read = lambda: engine.codex_threads(
            self.home.path, lsof=lambda a: lsof_text({1: [B]}, self.home.path), cache=cache,
            clock=lambda: now[0])
        self.assertEqual(read(), [])
        self.home.rollout(B, cwd="/Users/x/later")
        now[0] += engine.CODEX_LOCK_TTL + 1
        self.assertEqual(read()[0]["cwd"], "/Users/x/later")

    def test_a_transcript_that_moved_is_found_again(self):
        cache = {}
        self.home.lock(A)
        path = self.home.rollout(A, cwd="/Users/x/first")
        read = lambda: engine.codex_threads(
            self.home.path, lsof=lambda a: lsof_text({1: [A]}, self.home.path), cache=cache)
        self.assertEqual(read()[0]["cwd"], "/Users/x/first")
        moved = os.path.join(self.home.path, "sessions", "2026", "09", "30")
        os.makedirs(moved)
        os.rename(path, os.path.join(moved, os.path.basename(path)))
        self.assertEqual(read()[0]["cwd"], "/Users/x/first")

    def test_one_bad_line_blanks_only_its_own_thread(self):
        self.home.lock(A, C)
        with open(os.path.join(self.home.path, "session_index.jsonl"), "w") as fh:
            fh.write(json.dumps({"id": ["not", "an", "id"], "thread_name": "x"}) + "\n")
            fh.write(json.dumps({"id": C, "thread_name": "the other"}) + "\n")
        with open(self.home.rollout(A), "w") as fh:
            fh.write("[1]\n")                       # JSON, but no object
        self.home.rollout(C)
        threads = {t["thread"]: t for t in self.read({1: [A, C]})}
        self.assertEqual(sorted(threads), sorted([A, C]))
        self.assertEqual(threads[A]["cwd"], "")
        self.assertEqual(threads[C]["name"], "the other")

    def test_lsof_is_asked_again_only_when_it_may_have_changed(self):
        # measured: lsof takes 0.5 s whatever its form - once a TTL, not every scan
        cache, asked, now = {}, [], [1000.0]
        self.home.lock(A)
        self.home.rollout(A)
        def lsof(args):
            asked.append(args)
            return lsof_text({1: [A]}, self.home.path)
        read = lambda: engine.codex_threads(self.home.path, lsof=lsof, cache=cache,
                                            clock=lambda: now[0])
        read()
        now[0] += engine.CODEX_LOCK_TTL - 1
        self.assertEqual([t["thread"] for t in read()], [A])
        self.assertEqual(len(asked), 1)                                # within the TTL
        self.home.lock(C)                                             # a new thread's lock
        read()
        self.assertEqual(len(asked), 2)
        now[0] += engine.CODEX_LOCK_TTL + 1                           # the TTL passed
        read()
        self.assertEqual(len(asked), 3)

    def test_not_known_is_not_kept(self):
        cache, answers = {}, [None]
        self.home.lock(A)
        self.home.rollout(A)
        def lsof(args):
            return answers.pop(0) if answers else lsof_text({1: [A]}, self.home.path)
        read = lambda: engine.codex_threads(self.home.path, lsof=lsof, cache=cache,
                                            clock=lambda: 1000.0)
        self.assertIsNone(read())
        self.assertEqual([t["thread"] for t in read()], [A])          # asked again at once

    def test_a_lock_that_vanished_under_lsof_is_asked_about_again(self):
        # a thread closed between the listing and lsof: lsof says "status error"
        # and fails - list again and ask once more, not a scan of "not known"
        self.home.lock(A, B)
        self.home.rollout(A)
        calls = []
        def lsof(args):
            calls.append(args)
            if len(calls) == 1:
                os.remove(os.path.join(self.home.path, "thread-writer-locks", f"{B}.lock"))
                return None
            return lsof_text({1: [A]}, self.home.path)
        threads = engine.codex_threads(self.home.path, lsof=lsof, cache={})
        self.assertEqual([t["thread"] for t in threads], [A])
        self.assertEqual(len(calls), 2)
        self.assertNotIn(os.path.join(os.path.realpath(self.home.path), "thread-writer-locks",
                                      f"{B}.lock"), calls[1])

    def test_a_failure_with_the_same_locks_is_not_known(self):          # control
        self.home.lock(A)
        calls = []
        self.assertIsNone(engine.codex_threads(self.home.path,
                                               lsof=lambda a: calls.append(a), cache={}))
        self.assertEqual(len(calls), 1)

    def test_a_path_lsof_prints_another_way_is_not_known(self):
        # lsof escapes bytes it cannot print: a name it did not echo back is
        # "not known", never "none open"
        self.home.lock(A)
        self.home.rollout(A)
        text = "p1\nn" + os.path.realpath(self.home.path).replace("/", "\\x2f") + "/x.lock\n"
        self.assertIsNone(engine.codex_threads(self.home.path, lsof=lambda a: text, cache={}))

    def test_a_missing_transcript_is_not_looked_for_every_scan(self):
        self.home.lock(B)
        cache, now = {}, [1000.0]
        text = lsof_text({1: [B]}, self.home.path)
        real = engine.glob.glob
        looked = []
        def counting(pattern, *a, **k):
            looked.append(pattern)
            return real(pattern, *a, **k)
        engine.glob.glob = counting
        self.addCleanup(setattr, engine.glob, "glob", real)
        read = lambda: engine.codex_threads(self.home.path, lsof=lambda a: text, cache=cache,
                                            clock=lambda: now[0])
        read()
        read()
        self.assertEqual(len(looked), 1)
        now[0] += engine.CODEX_LOCK_TTL + 1
        read()
        self.assertEqual(len(looked), 2)

    def test_the_newest_of_two_transcripts_gives_the_folder(self):
        self.home.lock(A)
        old = self.home.rollout(A, cwd="/Users/x/old")
        newer = os.path.join(os.path.dirname(old), f"rollout-2026-09-29T12-00-00-{A}.jsonl")
        with open(newer, "w") as fh:
            fh.write(json.dumps({"type": "session_meta", "payload": {"cwd": "/Users/x/new"}}) + "\n")
        self.assertEqual(self.read({1: [A]})[0]["cwd"], "/Users/x/new")

    def test_what_reaches_the_fleet_is_printable_text(self):
        # Codex writes a source as an object for a subagent; a name can hold
        # escape sequences: plain text only, as every other row
        self.home.lock(A)
        self.home.index((A, "fix \x1b[31mred\x07"))
        path = self.home.rollout(A, cwd="/Users/x/\x1b]0;title\x07")
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "session_meta", "payload": {
                "cwd": "/Users/x/\x1b]0;t\x07", "originator": "Codex\x1b[2J",
                "source": "vs\x1b[2Jcode"}}) + "\n")
        t = self.read({1: [A]})[0]
        for field in ("name", "cwd", "originator", "source"):
            self.assertIsInstance(t[field], str)
            self.assertNotIn("\x1b", t[field])
            self.assertNotIn("\x07", t[field])

    def test_a_source_that_is_no_text_is_empty(self):
        self.home.lock(A)
        path = self.home.rollout(A)
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "session_meta", "payload": {
                "cwd": "/x", "source": ["vscode", 1]}}) + "\n")
        self.assertEqual(self.read({1: [A]})[0]["source"], "")

    def test_a_first_line_that_is_not_utf8_still_reads(self):
        self.home.lock(A)
        path = self.home.rollout(A)
        with open(path, "wb") as fh:
            fh.write(b'{"type": "session_meta", "payload": {"cwd": "/Users/x/j\xf6rg"}}\n')
        self.assertTrue(self.read({1: [A]})[0]["cwd"].startswith("/Users/x/j"))

    def test_it_asks_lsof_about_the_lock_files_by_name(self):
        # the lock files by name, not +D on the folder
        seen = []
        self.home.lock(A, B)
        engine.codex_threads(self.home.path, lsof=lambda args: seen.append(args) or "",
                             cache={})
        real = os.path.realpath(os.path.join(self.home.path, "thread-writer-locks"))
        self.assertEqual(seen, [["-nP", "-Fpn", "--", os.path.join(real, f"{A}.lock"),
                                 os.path.join(real, f"{B}.lock")]])


class TestTheFleetHasTheOpenThreads(unittest.TestCase):
    """fleet["codex_threads"]: each open thread with its processes and ports.
    A Codex process whose thread holds no lock stays in "codex" - state
    unknown, never cleaned (D17)."""

    THREADS = [{"thread": A, "name": "fix the navbar", "cwd": "/Users/x/liveapp",
                "host_pid": 4215, "originator": "Codex Desktop"},
               {"thread": B, "name": "", "cwd": "", "host_pid": 4215, "originator": ""}]
    ATT = {"sessions": {}, "left_behind": [], "unsure": [],
           "codex": [{"pid": 30, "ports": [5173], "helper": False, "session": A,
                      "harness": "codex", "command": "vite", "orphan": False},
                     {"pid": 31, "ports": [], "helper": False, "session": A,
                      "harness": "codex", "command": "esbuild", "orphan": False},
                     {"pid": 32, "ports": [], "helper": True, "session": A,
                      "harness": "codex", "command": "npx some-mcp", "orphan": False},
                     {"pid": 40, "ports": [8787], "helper": False, "session": C,
                      "harness": "codex", "command": "workerd", "orphan": True}]}

    def test_each_open_thread_gets_its_processes_and_ports(self):
        rows = engine.codex_fleet(self.THREADS, self.ATT, ports_ok=True)
        self.assertEqual([(r["thread"], r["procs"], r["ports"], r["pids"]) for r in rows],
                         [(A, 2, [5173], [30, 31]), (B, 0, [], [])])
        self.assertEqual(rows[0]["name"], "fix the navbar")

    def test_ports_not_read_are_none(self):
        rows = engine.codex_fleet(self.THREADS, self.ATT, ports_ok=False)
        self.assertIsNone(rows[0]["ports"])

    def test_threads_not_known_are_none(self):
        self.assertIsNone(engine.codex_fleet(None, self.ATT, ports_ok=True))

    def test_a_process_of_a_thread_not_open_is_in_no_row(self):         # D17
        rows = engine.codex_fleet(self.THREADS, self.ATT, ports_ok=True)
        self.assertNotIn(40, [p for r in rows for p in r["pids"]])


class TestCollectReadsTheThreads(unittest.TestCase):
    def test_the_fleet_carries_them_and_a_failure_blanks_nothing(self):
        import ccwho_engine as e
        seen = {}
        real = e.codex_threads
        try:
            e.codex_threads = lambda home, lsof=None, cache=None: seen.setdefault("home", home) and []
            e.read_codex_threads({"CODEX_HOME": "/Users/x/.codex-work"}, cache={})
            self.assertEqual(seen["home"], "/Users/x/.codex-work")
            self.assertEqual(e.codex_home({"CODEX_HOME": "/Users/x/.codex-work"}),
                             "/Users/x/.codex-work")
            self.assertEqual(e.codex_home({}), os.path.expanduser("~/.codex"))
            def boom(home, lsof=None, cache=None):
                raise OSError("/secret/path")
            e.codex_threads = boom
            self.assertIsNone(e.read_codex_threads({}, cache={}))    # not known, not a crash
        finally:
            e.codex_threads = real


class TestACodexThreadIsARow(unittest.TestCase):
    """The owner's D16 (2026-09-29): a Codex thread is a row - its name, folder,
    where it runs, its processes and ports - in its own group, CODEX, last.
    Only the list's own rows: collect()'s rows stay Claude's (--json, jump,
    show read them as sessions)."""

    FLEET = {"codex_threads": [
        {"thread": A, "name": "fix the navbar", "cwd": "/Users/x/projects/liveapp",
         "host_pid": 4215, "originator": "Codex Desktop", "source": "vscode",
         "procs": 2, "ports": [5173], "pids": [30, 31]},
        {"thread": C, "name": "", "cwd": "", "host_pid": 4215, "originator": "Codex Desktop",
         "source": "", "procs": 0, "ports": [], "pids": []}]}

    def test_each_open_thread_is_a_codex_row(self):
        rows = engine.codex_rows(self.FLEET)
        self.assertEqual([(r["sessionId"], r["kind"]) for r in rows],
                         [(A, "codex"), (C, "codex")])
        r = rows[0]
        self.assertEqual((r["project"], r["title"], r["procs"], r["ports"]),
                         ("liveapp", "fix the navbar", 2, [5173]))
        self.assertEqual(r["cwd"], "/Users/x/projects/liveapp")

    def test_where_it_runs_is_said_by_where_it_was_typed(self):
        where = lambda source, originator="Codex Desktop": engine.codex_where(
            {"source": source, "originator": originator})
        self.assertEqual(where("vscode"), "VS Code")
        self.assertEqual(where("cli", "codex_cli_rs"), "the codex CLI")
        self.assertEqual(where("exec", "codex_exec"), "codex exec")
        self.assertEqual(where(""), "the ChatGPT app")
        self.assertEqual(where("", "something else"), "Codex")

    def test_threads_not_known_or_none_give_no_rows(self):
        self.assertEqual(engine.codex_rows({"codex_threads": None}), [])
        self.assertEqual(engine.codex_rows({}), [])
        self.assertEqual(engine.codex_rows(None), [])

    def test_two_threads_of_one_minute_have_different_short_ids(self):
        # a UUIDv7 starts with its time: the first four hex are the same for
        # weeks - the short id is its random end
        a = "01a0ed5d-dc97-7333-8ff3-073a4c247d3e"
        b = "01a0ed5d-dd24-7401-b67c-f314b56b67b7"
        rows = engine.codex_rows({"codex_threads": [
            dict(self.FLEET["codex_threads"][0], thread=a),
            dict(self.FLEET["codex_threads"][0], thread=b)]})
        self.assertNotEqual(rows[0]["short"], rows[1]["short"])
        firsts = ["".join(t for t, _ in engine.ui_row_cells(r, width=140)[0]) for r in rows]
        self.assertIn(rows[0]["short"], firsts[0])
        self.assertNotIn(rows[1]["short"], firsts[0])

    def test_a_row_with_no_name_or_folder_still_says_what_it_is(self):
        r = engine.codex_rows(self.FLEET)[1]
        self.assertEqual(r["project"], "codex")
        self.assertTrue(r["title"])

    def test_its_row_says_codex_and_where_not_a_window(self):
        r = engine.codex_rows(self.FLEET)[0]
        text = "".join(t for t, _ in engine.ui_row_cells(r, width=140)[0])
        self.assertIn("Codex · VS Code", text)
        self.assertIn(":5173", text)
        self.assertNotIn("no window", text)
        second = "".join(t for t, _ in engine.ui_row_cells(r, width=140)[1])
        self.assertIn("~/projects/liveapp" if os.path.expanduser("~") == "/Users/x"
                      else "/Users/x/projects/liveapp", second)
        self.assertNotIn("no recap yet", second)

    def test_a_name_with_a_break_or_a_bidi_mark_is_one_plain_line(self):
        fleet = {"codex_threads": [dict(self.FLEET["codex_threads"][0],
                                        name="line one\nline two \u202eevil")]}
        r = engine.codex_rows(fleet)[0]
        self.assertNotIn("\n", r["title"])
        self.assertNotIn("\u202e", r["title"])


class TestTheCodexLineIsWhatNoRowShows(unittest.TestCase):
    """The bottom "codex:" line is the state-unknown processes (D17): a process
    whose thread is open is on that thread's row, not counted again."""

    def fleet(self, threads):
        return {"left_behind": [], "codex_threads": threads,
                "codex": [{"pid": 30, "ports": [], "session": A},
                          {"pid": 40, "ports": [], "session": C}]}

    def test_a_row_takes_its_processes_off_the_line(self):
        lines = engine.bottom_lines(self.fleet([{"thread": A, "pids": [30]}]))
        self.assertEqual(lines, ["codex: 1 process - ccwho ps"])

    def test_every_process_on_a_row_is_no_line(self):
        f = self.fleet([{"thread": A, "pids": [30]}, {"thread": C, "pids": [40]}])
        self.assertEqual(engine.bottom_lines(f), [])

    def test_threads_not_known_count_them_all(self):                     # control
        self.assertEqual(engine.bottom_lines(self.fleet(None)), ["codex: 2 processes - ccwho ps"])


class TestTheCodexDetail(unittest.TestCase):
    def test_it_lists_its_own_work_only_each_a_process_part(self):
        row = engine.codex_rows(TestACodexThreadIsARow.FLEET)[0]
        fleet = {"codex": [{"pid": 30, "ports": [5173], "command": "vite", "session": A},
                           {"pid": 32, "ports": [], "command": "npx mcp", "session": A,
                            "helper": True},
                           {"pid": 40, "ports": [8787], "command": "workerd", "session": C}]}
        parts = engine.codex_detail_parts(row, fleet, width=100)
        text = "\n".join(line for line, _, _ in parts)
        self.assertIn("fix the navbar", text)
        self.assertIn("VS Code", text)
        self.assertEqual([(v, f) for _, v, f in parts if v], [("30", "proc")])
        self.assertNotIn("workerd", text)
        self.assertNotIn("npx mcp", text)


class TestACodexRowIsPlainText(unittest.TestCase):
    """Review 1 of the rows: the folder reached line two with bidi marks."""

    def test_no_line_of_the_row_holds_a_control_or_format_character(self):
        import unicodedata
        t = dict(TestACodexThreadIsARow.FLEET["codex_threads"][0],
                 cwd="/tmp/ev\u202eil\u2066dir\u200b/x\ny\u202ez", name="n\u202eame")
        row = engine.codex_rows({"codex_threads": [t]})[0]
        for field in ("project", "folder", "title"):
            self.assertFalse([c for c in row[field] if unicodedata.category(c)
                              in ("Cc", "Cf", "Zl", "Zp")], field)
        for width in (160, 80, 60):
            for line in engine.ui_row_cells(row, width=width)[:2]:
                text = "".join(t for t, _ in line)
                self.assertFalse([c for c in text if unicodedata.category(c)
                                  in ("Cc", "Cf", "Zl", "Zp")], (width, text))


class TestTheCodexLineCountsHelpersOnTheirRow(unittest.TestCase):
    def test_an_open_threads_helper_is_not_state_unknown(self):
        fleet = {"left_behind": [], "codex_threads": [{"thread": A, "pids": [30]}],
                 "codex": [{"pid": 30, "ports": [], "session": A},
                           {"pid": 32, "ports": [], "session": A, "helper": True}]}
        self.assertEqual(engine.bottom_lines(fleet), [])


class TestKillingACodexThreadsWork(unittest.TestCase):
    """x -> p on a Codex row is kill_plan's "session" mode with the thread id: its
    processes, listed - with no "cannot tell whether it runs": it is open."""

    T = "Tue Sep 29 11:13:00 2026"

    def world(self):
        table = {1: (0, self.T, "/sbin/launchd"),
                 4215: (1, self.T, "/Applications/ChatGPT.app/Contents/Resources/codex"),
                 30: (4215, self.T, "vite --port 5173"),
                 99: (1, self.T, "python3 ccwho.py")}
        marks = {1: None, 4215: None, 30: ("codex", A), 99: None}
        ports = {30: [5173]}
        att = procs.attribute(table, marks, ports, [], own=99)
        return {"table": table, "att": att, "sessions": [], "own": 99, "ports": ports,
                "pipes": {q: set() for q in table}, "marks": marks, "connections": []}

    def test_its_processes_are_taken_without_the_unknown_note(self):
        p = procs.kill_plan("session", A, self.world())
        self.assertEqual([e["pid"] for e in p["kill"]], [30])
        self.assertNotIn("cannot tell whether", p["kill"][0].get("note", ""))

    def test_another_agents_work_inside_the_thread_is_not_taken(self):
        # review 4: host -> zsh -> claude (a live session) -> node, still with
        # the thread's mark. The stop is the thread's Codex host only: a claude
        # between is its own work - named apart, never taken with no word
        w = self.world()
        # a codex CLI host, no app: only the rule about another agent can spare 53
        w["table"][4215] = (1, self.T, "/opt/homebrew/bin/codex")
        w["table"].update({50: (4215, self.T, "zsh -c claude"),
                           51: (50, self.T, "/Users/x/.local/bin/claude"),
                           53: (51, self.T, "node devserver.js")})
        w["marks"].update({50: None, 51: None, 53: ("codex", A)})
        w["pipes"].update({50: set(), 51: set(), 53: set()})
        w["sessions"] = [{"sessionId": "aaaa1111-0000-4000-8000-000000000001", "pid": 51}]
        w["att"] = procs.attribute(w["table"], w["marks"], w["ports"], w["sessions"], own=99)
        p = procs.kill_plan("session", A, w)
        self.assertNotIn(53, [e["pid"] for e in p["kill"]])
        self.assertIn(30, [e["pid"] for e in p["kill"]])                 # control: its own
        why = {e["pid"]: e["why"] for e in p["spare"]}
        self.assertIn("runs above it (51)", why[53])
        # and a pid kill of it says so
        note = procs.kill_plan("pid", {"pid": 53, "start": self.T}, w)["kill"][0].get("note", "")
        self.assertIn("runs above it (51)", note)

    def cli_world(self, extra, marks, sessions=()):
        w = self.world()
        w["table"][4215] = (1, self.T, "/opt/homebrew/bin/codex")      # no app: only the rule
        w["table"].update(extra)
        w["marks"].update(marks)
        w["pipes"].update({q: set() for q in extra})
        w["sessions"] = list(sessions)
        w["att"] = procs.attribute(w["table"], w["marks"], w["ports"], w["sessions"], own=99)
        return w

    def test_a_codex_started_inside_the_thread_is_not_its_host(self):
        # review 5: host -> zsh (mark A) -> codex (a CLI with its own thread, A's
        # mark inherited) -> node (mark A): the inner codex is not A's host
        w = self.cli_world({13: (4215, self.T, "zsh -c codex"), 14: (13, self.T, "codex"),
                            15: (14, self.T, "node devserver.js")},
                           {13: ("codex", A), 14: ("codex", A), 15: ("codex", A)})
        p = procs.kill_plan("session", A, w)
        self.assertNotIn(15, [e["pid"] for e in p["kill"]])
        self.assertIn("(14)", {e["pid"]: e["why"] for e in p["spare"]}[15])
        self.assertIn(30, [e["pid"] for e in p["kill"]])                 # control
        note = procs.kill_plan("pid", {"pid": 15, "start": self.T}, w)["kill"][0].get("note", "")
        self.assertIn("(14)", note)

    def test_a_live_session_ccwho_does_not_name_an_agent_is_one(self):
        # a dev build of claude: its command says nothing, the session list does
        w = self.cli_world({50: (4215, self.T, "zsh -c run"),
                            51: (50, self.T, "/Users/x/dev/my-build"),
                            53: (51, self.T, "node devserver.js")},
                           {50: None, 51: None, 53: ("codex", A)},
                           sessions=[{"sessionId": "aaaa1111-0000-4000-8000-000000000001",
                                      "pid": 51}])
        p = procs.kill_plan("session", A, w)
        self.assertNotIn(53, [e["pid"] for e in p["kill"]])

    def test_an_agent_only_its_command_names_is_one(self):
        w = self.cli_world({50: (4215, self.T, "zsh -c run"),
                            51: (50, self.T, "node /Users/x/src/claude-code/cli.js"),
                            53: (51, self.T, "node devserver.js")},
                           {50: None, 51: None, 53: ("codex", A)})
        p = procs.kill_plan("session", A, w)
        self.assertNotIn(53, [e["pid"] for e in p["kill"]])

    def test_its_processes_get_no_app_note(self):
        # review 2 of the rows: its host is the ChatGPT app's codex - what is
        # under that agent does not "run in an app", as a Claude session's work
        p = procs.kill_plan("session", A, self.world())
        self.assertNotIn("note", p["kill"][0])

    def test_a_process_right_under_an_app_keeps_the_app_note(self):      # control
        w = self.world()
        w["table"][31] = (1, self.T, "/Applications/Foo.app/Contents/MacOS/Foo")
        w["table"][32] = (31, self.T, "node serve.js")
        w["marks"][31], w["marks"][32] = None, ("codex", A)
        w["pipes"][31], w["pipes"][32] = set(), set()
        w["att"] = procs.attribute(w["table"], w["marks"], w["ports"], [], own=99)
        p = procs.kill_plan("pid", {"pid": 32, "start": self.T}, w)
        self.assertIn("runs in an app", p["kill"][0].get("note", ""))

    def test_a_pid_kill_keeps_the_note(self):                             # control
        p = procs.kill_plan("pid", {"pid": 30, "start": self.T}, self.world())
        self.assertIn("cannot tell whether", p["kill"][0].get("note", ""))


def transcript(path, *events, pad=0):
    """A rollout: session_meta, `pad` bytes of noise, then events - each a
    (payload type, extra payload fields)."""
    with open(path, "w") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"cwd": "/x"}}) + "\n")
        if pad:
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message",
                                                                  "message": "x" * pad}}) + "\n")
        for kind, extra in events:
            fh.write(json.dumps({"type": "event_msg", "payload": dict({"type": kind}, **extra)})
                     + "\n")


class TestTheEndOfATranscriptSaysTheState(unittest.TestCase):
    """The owner's D19 (2026-09-29): a Codex thread's state from its transcript's
    end - task_started / task_complete (with the agent's last words) /
    turn_aborted. Codex writes no approval request: a turn that went quiet is
    "waiting?"."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, f"rollout-x-{A}.jsonl")

    def test_a_done_turn_that_asks(self):
        transcript(self.path, ("task_started", {}),
                   ("task_complete", {"completed_at": 1790000000,
                                      "last_agent_message": "I fixed it.\n\nShall I land it?"}))
        t = engine.codex_turn(self.path, cache={})
        self.assertEqual((t["turn"], t["ask"], t["ts"]), ("done", "Shall I land it?", 1790000000))

    def test_a_done_turn_that_does_not_ask(self):
        transcript(self.path, ("task_started", {}),
                   ("task_complete", {"completed_at": 5, "last_agent_message": "Done: all green."}))
        self.assertEqual(engine.codex_turn(self.path, cache={})["ask"], "")

    def test_aborted_and_open(self):
        transcript(self.path, ("task_started", {}), ("turn_aborted", {}))
        self.assertEqual(engine.codex_turn(self.path, cache={})["turn"], "aborted")
        transcript(self.path, ("task_complete", {"completed_at": 1}), ("task_started", {}),
                   ("token_count", {}))
        self.assertEqual(engine.codex_turn(self.path, cache={})["turn"], "open")

    def test_no_boundary_is_none(self):
        transcript(self.path)
        self.assertIsNone(engine.codex_turn(self.path, cache={})["turn"])

    def test_only_codex_s_own_events_are_boundaries(self):
        # a response item that names a boundary is what was said, not an end
        transcript(self.path, ("task_started", {}))
        with open(self.path, "a") as fh:
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "task_complete", "completed_at": 1}}) + "\n")
        self.assertEqual(engine.codex_turn(self.path, cache={})["turn"], "open")

    def test_the_last_boundary_wins_past_a_long_line(self):
        transcript(self.path, ("task_started", {}), pad=engine.CODEX_TAIL * 2)
        with open(self.path, "a") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted"}}) + "\n")
        self.assertEqual(engine.codex_turn(self.path, cache={})["turn"], "aborted")

    def test_a_tool_call_without_output_is_what_can_wait(self):
        # quiet after a tool call: a long build or an approval; quiet after the
        # model's own words: thinking - never an approval (review 1 of D19)
        transcript(self.path, ("task_started", {}))
        with open(self.path, "a") as fh:
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "function_call", "call_id": "c1"}}) + "\n")
        self.assertTrue(engine.codex_turn(self.path, cache={})["pending_tool"])
        with open(self.path, "a") as fh:
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "c1"}}) + "\n")
            fh.write(json.dumps({"type": "response_item", "payload": {"type": "message"}}) + "\n")
        self.assertFalse(engine.codex_turn(self.path, cache={})["pending_tool"])

    def noise(self, n):
        with open(self.path, "a") as fh:
            for _ in range(n):
                fh.write(json.dumps({"type": "event_msg", "payload": {
                    "type": "item_completed", "item": "x" * 8000}}) + "\n")

    def test_a_long_turn_is_still_open(self):
        # review 1 of D19: 269 of 1157 real turns wrote more than the tail after
        # their start - read back to the boundary, from a fresh start too
        transcript(self.path, ("task_complete", {"completed_at": 1}), ("task_started", {}))
        self.noise(2 * engine.CODEX_TAIL // 8000)
        self.assertEqual(engine.codex_turn(self.path, cache={})["turn"], "open")

    def test_a_done_turn_far_back_is_still_done(self):                  # control
        transcript(self.path, ("task_started", {}),
                   ("task_complete", {"completed_at": 3, "last_agent_message": "Go?"}))
        self.noise(2 * engine.CODEX_TAIL // 8000)
        t = engine.codex_turn(self.path, cache={})
        self.assertEqual((t["turn"], t["ask"], t["ts"]), ("done", "Go?", 3))

    def test_a_line_written_in_two_parts_is_read_whole(self):
        # review 2 of D19: a read while Codex wrote half a line lost that line
        for kind, extra in (("task_complete", {"completed_at": 5, "last_agent_message": "Go?"}),):
            transcript(self.path, ("task_started", {}))
            with open(self.path, "a") as fh:
                fh.write(json.dumps({"type": "response_item", "payload": {
                    "type": "function_call", "call_id": "c"}}) + "\n")
            line = json.dumps({"type": "event_msg", "payload": dict({"type": kind}, **extra)}) + "\n"
            cache = {}
            with open(self.path, "a") as fh:
                fh.write(line[:30])
            engine.codex_turn(self.path, cache=cache)
            with open(self.path, "a") as fh:
                fh.write(line[30:])
            warm = engine.codex_turn(self.path, cache=cache)
            cold = engine.codex_turn(self.path, cache={})
            self.assertEqual({k: warm[k] for k in ("turn", "ask", "ts", "pending_tool")},
                             {k: cold[k] for k in ("turn", "ask", "ts", "pending_tool")})
            self.assertEqual(warm["turn"], "done")

    def test_a_half_line_on_a_grown_read_is_read_whole_later(self):
        # review 3 of D19: the first read must be a WHOLE-line read into the
        # cache, so the half line meets the grown read, not the cold one
        cache = {}
        transcript(self.path, ("task_started", {}))
        self.assertEqual(engine.codex_turn(self.path, cache=cache)["turn"], "open")
        line = json.dumps({"type": "event_msg", "payload": {
            "type": "task_complete", "completed_at": 5, "last_agent_message": "Go?"}}) + "\n"
        with open(self.path, "a") as fh:
            fh.write(line[:25])
        engine.codex_turn(self.path, cache=cache)
        with open(self.path, "a") as fh:
            fh.write(line[25:])
        warm = engine.codex_turn(self.path, cache=cache)
        cold = engine.codex_turn(self.path, cache={})
        self.assertEqual((warm["turn"], warm["ask"]), ("done", "Go?"))
        self.assertEqual({k: warm[k] for k in ("turn", "ask", "ts", "pending_tool")},
                         {k: cold[k] for k in ("turn", "ask", "ts", "pending_tool")})

    def test_the_last_turns_time_goes_with_a_new_turn(self):
        # a cold read stops at task_started; a warm or full read went past an
        # older completion: both must say the same - no time for an open turn
        transcript(self.path, ("task_complete", {"completed_at": 4}))
        self.noise(2 * engine.CODEX_TAIL // 8000)
        with open(self.path, "a") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
        cold = engine.codex_turn(self.path, cache={})
        full = {"turn": None, "ask": "", "ts": None, "pending_tool": False}
        with open(self.path, "rb") as fh:
            for line in fh.read().split(b"\n"):
                engine._codex_step(full, line)
        self.assertEqual((cold["turn"], cold["ts"]), ("open", None))
        self.assertEqual(full["ts"], None)


    def test_an_interrupt_right_after_a_done_turn_has_no_time(self):
        # the property check found it: complete, far back, then aborted - the
        # cold read stops at the interrupt; the full read must agree
        transcript(self.path, ("task_complete", {"completed_at": 4}))
        self.noise(2 * engine.CODEX_TAIL // 8000)
        with open(self.path, "a") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted"}}) + "\n")
        cold = engine.codex_turn(self.path, cache={})
        full = {"turn": None, "ask": "", "ts": None, "pending_tool": False}
        with open(self.path, "rb") as fh:
            for line in fh.read().split(b"\n"):
                engine._codex_step(full, line)
        self.assertEqual((cold["turn"], cold["ts"]), (full["turn"], full["ts"]))
        self.assertEqual(cold["ts"], None)

    def test_a_last_line_without_its_newline_is_no_boundary_yet(self):
        transcript(self.path, ("task_started", {}))
        self.noise(2 * engine.CODEX_TAIL // 8000)
        with open(self.path, "a") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_complete", "completed_at": 6}}))          # no "\n" yet
        self.assertEqual(engine.codex_turn(self.path, cache={})["turn"], "open")

    def test_a_file_that_shrank_is_read_as_new(self):
        cache = {}
        transcript(self.path, ("task_started", {}))
        self.noise(3)
        engine.codex_turn(self.path, cache=cache)
        with open(self.path, "r+") as fh:                  # the same inode, smaller
            fh.truncate(0)
        transcript(self.path, ("task_complete", {"completed_at": 2}))
        self.assertEqual(engine.codex_turn(self.path, cache=cache)["turn"], "done")

    def test_a_grown_file_is_read_only_where_it_grew(self):
        cache, seen = {}, []
        transcript(self.path, ("task_started", {}))
        self.noise(20)
        engine.codex_turn(self.path, cache=cache)
        real = engine._codex_step
        engine._codex_step = lambda state, line: seen.append(line) or real(state, line)
        self.addCleanup(setattr, engine, "_codex_step", real)
        with open(self.path, "a") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted"}}) + "\n")
        self.assertEqual(engine.codex_turn(self.path, cache=cache)["turn"], "aborted")
        self.assertLessEqual(len([l for l in seen if l]), 1)

    def test_a_replaced_file_is_read_as_new(self):
        cache = {}
        transcript(self.path, ("task_started", {}))
        self.noise(5)                                     # ~40 KB, still open
        engine.codex_turn(self.path, cache=cache)
        other = self.path + ".new"
        transcript(other, ("task_complete", {"completed_at": 8}))   # its end early...
        with open(other, "a") as fh:
            for _ in range(10):                           # ...then more than the old size
                fh.write(json.dumps({"type": "event_msg", "payload": {
                    "type": "item_completed", "item": "y" * 8000}}) + "\n")
        os.replace(other, self.path)
        self.assertEqual(engine.codex_turn(self.path, cache=cache)["turn"], "done")

    def test_a_cold_read_of_a_long_turn_is_linear(self):
        # 40 MB took 6 s when each step re-read all it had read before
        import time
        transcript(self.path, ("task_started", {}))
        self.noise(40 * 1024 * 1024 // 8000)
        started = time.monotonic()
        self.assertEqual(engine.codex_turn(self.path, cache={})["turn"], "open")
        self.assertLess(time.monotonic() - started, 3.0)

    def test_a_tool_search_can_wait_too(self):
        transcript(self.path, ("task_started", {}))
        with open(self.path, "a") as fh:
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "tool_search_call"}}) + "\n")
        self.assertTrue(engine.codex_turn(self.path, cache={})["pending_tool"])
        with open(self.path, "a") as fh:
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "tool_search_output"}}) + "\n")
        self.assertFalse(engine.codex_turn(self.path, cache={})["pending_tool"])

    def test_what_grew_without_a_boundary_keeps_the_state(self):
        cache = {}
        transcript(self.path, ("task_started", {}))
        self.assertEqual(engine.codex_turn(self.path, cache=cache)["turn"], "open")
        self.noise(3)
        self.assertEqual(engine.codex_turn(self.path, cache=cache)["turn"], "open")

    def test_a_transcript_that_grew_is_read_again(self):
        cache = {}
        transcript(self.path, ("task_started", {}))
        self.assertEqual(engine.codex_turn(self.path, cache=cache)["turn"], "open")
        with open(self.path, "a") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_complete", "completed_at": 9}}) + "\n")
        self.assertEqual(engine.codex_turn(self.path, cache=cache)["turn"], "done")

    def test_the_words_kept_are_the_closing_question_only(self):
        transcript(self.path, ("task_complete", {"completed_at": 1,
                                                 "last_agent_message": "secret plan\n\nOK to go?"}))
        t = engine.codex_turn(self.path, cache={})
        self.assertNotIn("secret plan", json.dumps(t))


class TestEveryWayOfReadingAgrees(unittest.TestCase):
    """A property, from review 3 of D19: after any appends - lines split in two,
    boundaries in any order, noise longer than a chunk - the warm read (one
    cache the whole way), a cold read and a full forward read say the same."""

    EVENTS = [("event_msg", "task_started", {}), ("event_msg", "turn_aborted", {}),
              ("event_msg", "task_complete", {"last_agent_message": "Go on?"}),
              ("event_msg", "task_complete", {"last_agent_message": "Done."}),
              ("response_item", "function_call", {}), ("response_item", "function_call_output", {}),
              ("response_item", "message", {}), ("event_msg", "item_completed", {"x": "z" * 900})]

    def test_warm_cold_and_full_agree(self):
        import random
        was, engine.CODEX_TAIL = engine.CODEX_TAIL, 700        # many chunks, small files
        self.addCleanup(setattr, engine, "CODEX_TAIL", was)
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        for seed in range(12):
            rnd = random.Random(seed)
            path = os.path.join(folder, f"rollout-{seed}-{A}.jsonl")
            with open(path, "w") as fh:
                fh.write(json.dumps({"type": "session_meta", "payload": {"cwd": "/x"}}) + "\n")
            cache, pending, n = {}, "", 0
            for step in range(40):
                if pending and rnd.random() < 0.5:
                    chunk, pending = pending, ""
                else:
                    top, kind, extra = rnd.choice(self.EVENTS)
                    n += 1
                    extra = dict(extra, completed_at=n) if kind == "task_complete" else extra
                    line = json.dumps({"type": top, "payload": dict({"type": kind}, **extra)}) + "\n"
                    cut = rnd.randrange(1, len(line)) if rnd.random() < 0.3 else len(line)
                    chunk, pending = pending + line[:cut], line[cut:]
                with open(path, "a") as fh:
                    fh.write(chunk)
                warm = engine.codex_turn(path, cache=cache)
                cold = engine.codex_turn(path, cache={})
                full = {"turn": None, "ask": "", "ts": None, "pending_tool": False}
                data = open(path, "rb").read()
                for line in data.split(b"\n")[:-1]:
                    engine._codex_step(full, line)
                keys = ("turn", "ask", "ts", "pending_tool")
                self.assertEqual({k: warm[k] for k in keys}, full, (seed, step))
                self.assertEqual({k: cold[k] for k in keys}, full, (seed, step))


class TestACodexRowsState(unittest.TestCase):
    NOW = 1790000000.0

    def row(self, turn, ask="", ts=1, quiet=0, seen=None, pending=True):
        t = dict(TestACodexThreadIsARow.FLEET["codex_threads"][0],
                 turn=turn, ask=ask, ts=ts, mtime=self.NOW - quiet, seen_ts=seen,
                 pending_tool=pending)
        return engine.codex_rows({"codex_threads": [t]}, now=self.NOW)[0]

    def test_each_end_is_its_state(self):
        self.assertEqual(self.row("done", ask="Shall I land it?")["attention"], "asks")
        self.assertEqual(self.row("done")["attention"], "review")
        self.assertEqual(self.row("done", ts=7, seen=7)["attention"], "stopped")
        self.assertEqual(self.row("aborted")["attention"], "stopped")
        self.assertEqual(self.row("open", quiet=10)["attention"], "busy")
        self.assertEqual(self.row("open", quiet=engine.CODEX_QUIET + 1)["attention"], "waiting")
        # quiet with no tool call pending: the model thinks - busy
        self.assertEqual(self.row("open", quiet=engine.CODEX_QUIET + 1, pending=False)
                         ["attention"], "busy")
        self.assertEqual(self.row(None)["attention"], "stopped")

    def test_they_join_the_claude_groups(self):
        heads = [g["heading"] for g in engine.ui_groups(
            [self.row("done", ask="Go?"), self.row("open", quiet=5)])]
        self.assertEqual(heads, ["NEEDS YOU", "BUSY"])
        self.assertNotIn("CODEX", [h for h, _ in engine.UI_GROUPS])

    def test_line_two_says_what_it_waits_for(self):
        line = lambda r: "".join(t for t, _ in engine.ui_row_cells(r, width=140)[1])
        self.assertIn("waiting? (maybe an approval)",
                      line(self.row("open", quiet=engine.CODEX_QUIET + 1)))
        self.assertIn("Shall I land it?", line(self.row("done", ask="Shall I land it?")))
        self.assertIn("liveapp", line(self.row("done")))                  # else its folder

    def test_the_row_still_says_codex(self):
        first = "".join(t for t, _ in engine.ui_row_cells(self.row("done", ask="Go?"), 140)[0])
        self.assertIn("Codex · VS Code", first)


class TestTheStateReachesTheFleet(unittest.TestCase):
    def test_a_thread_carries_its_turn(self):
        home = Home()
        self.addCleanup(home.done)
        home.lock(A)
        path = home.rollout(A)
        with open(path, "a") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {
                "type": "task_complete", "completed_at": 42,
                "last_agent_message": "Tests pass.\n\nShall I merge?"}}) + "\n")
        t = engine.codex_threads(home.path, lsof=lambda a: lsof_text({1: [A]}, home.path),
                                 cache={})[0]
        self.assertEqual((t["turn"], t["ask"], t["ts"]), ("done", "Shall I merge?", 42))
        self.assertIsInstance(t["mtime"], float)

    def test_a_thread_carries_its_pending_tool(self):
        home = Home()
        self.addCleanup(home.done)
        home.lock(A)
        path = home.rollout(A)
        with open(path, "a") as fh:
            fh.write(json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n")
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "function_call", "call_id": "c"}}) + "\n")
        t = engine.codex_threads(home.path, lsof=lambda a: lsof_text({1: [A]}, home.path),
                                 cache={})[0]
        self.assertEqual((t["turn"], t["pending_tool"]), ("open", True))
        with open(path, "a") as fh:                       # its output came: none pending
            fh.write(json.dumps({"type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "c"}}) + "\n")
        t = engine.codex_threads(home.path, lsof=lambda a: lsof_text({1: [A]}, home.path),
                                 cache={})[0]
        self.assertEqual((t["turn"], t["pending_tool"]), ("open", False))

    def test_the_fleet_carries_the_turn_you_looked_at(self):
        threads = [{"thread": A, "name": "", "cwd": "", "host_pid": 1, "originator": "",
                    "source": "", "turn": "done", "ask": "", "ts": 42, "mtime": 1.0}]
        att = {"codex": []}
        self.assertEqual(engine.codex_fleet(threads, att, True, reviewed={A: 42})[0]["seen_ts"], 42)
        self.assertIsNone(engine.codex_fleet(threads, att, True)[0]["seen_ts"])


class TestASubagentThreadIsNoRow(unittest.TestCase):
    """Review 1 of D19: half of all finished turns are subagents' - each would
    sit in NEEDS YOU. Its parent thread is the row."""

    def test_a_subagent_thread_is_not_listed(self):
        home = Home()
        self.addCleanup(home.done)
        home.lock(A, C)
        home.rollout(A)
        path = home.rollout(C)
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "session_meta", "payload": {
                "cwd": "/x", "source": {"subagent": "review"}}}) + "\n")
        threads = engine.codex_threads(home.path, lsof=lambda a: lsof_text({1: [A, C]}, home.path),
                                       cache={})
        self.assertEqual([t["thread"] for t in threads], [A])
        with open(path, "w") as fh:                       # agent_path: the same
            fh.write(json.dumps({"type": "session_meta", "payload": {
                "cwd": "/x", "agent_path": "reviewer"}}) + "\n")
        threads = engine.codex_threads(home.path, lsof=lambda a: lsof_text({1: [A, C]}, home.path),
                                       cache={})
        self.assertEqual([t["thread"] for t in threads], [A])


class TestTheRealLsof(unittest.TestCase):
    """The real lsof, on a lock file this test holds open itself: lsof escapes
    bytes it cannot print in a C locale, and ccwho may start without a UTF-8
    one (a hotkey, a LaunchAgent). Nothing is signalled; only its own file."""

    @unittest.skipUnless(__import__("sys").platform == "darwin", "reads macOS lsof")
    def test_a_non_ascii_home_under_a_c_locale_still_gives_its_rows(self):
        home = os.path.join(tempfile.mkdtemp(), "j\u00f6rg \u65e5\u672c", ".codex")
        self.addCleanup(shutil.rmtree, os.path.dirname(os.path.dirname(home)), True)
        os.makedirs(os.path.join(home, "thread-writer-locks"))
        os.makedirs(os.path.join(home, "sessions", "2026", "09", "29"))
        with open(os.path.join(home, "sessions", "2026", "09", "29",
                               f"rollout-2026-09-29T11-13-00-{A}.jsonl"), "w") as fh:
            fh.write(json.dumps({"type": "session_meta", "payload": {"cwd": "/x"}}) + "\n")
        held = open(os.path.join(home, "thread-writer-locks", f"{A}.lock"), "w")
        self.addCleanup(held.close)
        # lsof that cannot be asked at all is its own failure, not "escaped"
        self.assertIsNotNone(engine._lsof(["-nP", "-Fp", "--", held.name]),
                             "lsof could not be asked")
        saved = {k: os.environ.get(k) for k in ("LANG", "LC_ALL", "LC_CTYPE")}
        def restore():
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        os.environ["LANG"] = "C"
        os.environ.pop("LC_ALL", None)
        os.environ.pop("LC_CTYPE", None)
        threads = engine.codex_threads(home, cache={})
        self.assertIsNotNone(threads, "the lock's path came back escaped")
        self.assertEqual([(t["thread"], t["host_pid"]) for t in threads], [(A, os.getpid())])


class TestAnIndexWithABrokenByte(unittest.TestCase):
    def test_the_names_still_come_back(self):
        home = Home()
        self.addCleanup(home.done)
        home.lock(A)
        home.rollout(A)
        with open(os.path.join(home.path, "session_index.jsonl"), "wb") as fh:
            fh.write(b'{"id": "x", "thread_name": "torn \xe6"}\n')
            fh.write(json.dumps({"id": A, "thread_name": "fix the navbar"}).encode() + b"\n")
        threads = engine.codex_threads(home.path, lsof=lambda a: lsof_text({1: [A]}, home.path),
                                       cache={})
        self.assertEqual(threads[0]["name"], "fix the navbar")


if __name__ == "__main__":
    unittest.main()
