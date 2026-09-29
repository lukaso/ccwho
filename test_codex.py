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
        self.assertEqual(threads, [{"thread": A, "name": "fix the navbar",
                                    "cwd": "/Users/x/liveapp", "host_pid": 4215,
                                    "originator": "Codex Desktop", "source": "vscode"}])

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
                "source": {"subagent": "review"}}}) + "\n")
        t = self.read({1: [A]})[0]
        for field in ("name", "cwd", "originator", "source"):
            self.assertIsInstance(t[field], str)
            self.assertNotIn("\x1b", t[field])
            self.assertNotIn("\x07", t[field])
        self.assertEqual(t["source"], "")

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
