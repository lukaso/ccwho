"""Tests for the TUI, driven through Textual's pilot.

These need Textual, which uv fetches: run them with `./test`. They FAIL rather
than skip when it is missing - a suite that reports "OK (skipped=12)" while the
screen is broken is a green light for nothing.

Everything that reaches the world (the session list, iTerm2) is injected, so no
test here depends on what is running on this machine.
"""
import asyncio
import subprocess
import threading
import unittest
from unittest import mock

import ccwho_ui as ui


_UNPIN = []
# the collector asks doctor every 5 minutes: osascript, launchd, the disk - this
# machine's. Every test gets a doctor that says nothing; the tests of it call
# the real one by name, with what it reads stubbed
REAL_DOCTOR = getattr(ui.Collector, "doctor", None)


def setUpModule():
    import ccwho
    import testkit
    _UNPIN.append(testkit.pin_ccwho_dir(ccwho))
    # the quick check stats the Codex lock folder: never the user's own
    _UNPIN.append(testkit.pin_codex_home())
    ui.Collector.doctor = lambda self: ""
    _UNPIN.append(lambda: setattr(ui.Collector, "doctor", REAL_DOCTOR)
                  if REAL_DOCTOR is not None else delattr(ui.Collector, "doctor"))


def tearDownModule():
    while _UNPIN:
        _UNPIN.pop()()
from textual.widgets import Input


def row(sid, attention="stopped", **kw):
    base = {"sessionId": sid, "project": "liveapp", "attention": attention,
            "ts": "", "windowed": None,
            "title": "Issue 362", "tab_title": "✳ Issue 362 (claude)",
            "tty": "ttys022", "since": "5h", "doing": "Bash: read the verdict",
            "recap": "", "recap_age": "", "turns_since_recap": 0,
            "name": "liveapp-b2", "pid": 2, "ask": "", "topic": "", "status": "idle",
            "cwd": "/Users/x/liveapp", "first": "", "orphans": 0, "work": 0}
    base.update(kw)
    return base


LIVE = row("aaaa1111-0000-4000-8000-000000000001", "asks",
           recap="the gate flake, finally understood", recap_age="2d",
           turns_since_recap=23)
BUSY = row("bbbb2222-0000-4000-8000-000000000002", "busy", title="Release queue",
           tab_title="✳ Release queue (claude)", tty="ttys007", since="2m")


_NOW = __import__("time").time()
POINTS = [
    {"name": "2026-09-26T1310.json", "path": "/r/2026-09-26T1310.json", "at": _NOW - 300,
     "count": 1, "running": 1, "to_open": 0, "before_reboot": False, "booted": _NOW - 1800},
    {"name": "2026-09-26T1222.json", "path": "/r/2026-09-26T1222.json", "at": _NOW - 3000,
     "count": 12, "running": 1, "to_open": 11, "before_reboot": True, "booted": _NOW - 1800},
]


class FakeCollector:
    def __init__(self, fleet=None, brief=None):
        self.fleet_value = fleet or ui.Fleet([LIVE, BUSY], True, "12:00:00")
        self.brief_value = brief or {
            "title": "Issue 362", "goal": "fix the gate flake",
            "you_said": "rebase and land", "recap": "the gate flake, understood",
            "recap_ts": "2026-09-18T10:00:00.000Z", "recap_age": "2d",
            "turns_since_recap": 23, "progress": ["Bash: run the gate"],
            "closing": "Shall I land it?",
            "aka": {"short_id": "aaaa", "session_id": LIVE["sessionId"],
                    "name": "liveapp-b2", "pid": 2, "cwd": "/Users/x/liveapp",
                    "tty": "ttys022"}}
        self.calls = 0

    def due(self, visible=None, now=None):
        return True

    last = 0.0
    digest = None

    def mark(self, now=None):
        self.last = now or 1.0

    def changed(self):
        return False            # the world sits still unless a test says so

    saved = 14

    def saved_count(self):
        return self.saved

    def restore(self, path=None, report=None):
        return "reopened 14 session(s)"

    def save_points(self, live_ids=()):
        return [dict(p) for p in POINTS]

    reloads = 0

    def reload(self):
        type(self).reloads += 1

    def fleet(self):
        self.calls += 1
        return self.fleet_value

    def brief(self, row):
        return self.brief_value

    killed = None

    def kill_loops(self, row):
        self.killed = (self.killed or []) + [row.get("sessionId")]
        return ["killed loop 86246"]

    # the session index the search reads for ended sessions: none, unless a
    # test gives it some
    index_value = None
    index_calls = 0

    def index(self):
        self.index_calls += 1
        return dict(self.index_value or {})

    # the grep of what was said: {query: {session id: Said}} - or a set of ids,
    # each then found with no Said - and each call made
    grep_value = None
    grep_calls = None

    def grep(self, entries, query, stop=None):
        self.grep_calls = (self.grep_calls or []) + [(query, stop)]
        found = (self.grep_value or {}).get(query, ())
        return dict(found) if isinstance(found, dict) else dict.fromkeys(found)

    reopened = None
    reopen_answer = "reopened 10fe9603-70dd-459c-96dc-05a176242f56"

    def reopen_ended(self, row):
        self.reopened = (self.reopened or []) + [row.get("sessionId")]
        return self.reopen_answer


class FakeAdapter:
    def __init__(self, answer="focused s022", hang=False, copy_error=""):
        self.answer, self.hang, self.asked = answer, hang, []
        self.attached, self.attached_rows = [], []
        self.kept_in_front = 0
        self.copied, self.copy_error = [], copy_error

    def copy(self, text):
        self.copied.append(text)
        return self.copy_error

    def keep_in_front(self):
        self.kept_in_front += 1

    def attach(self, cmd, deadline=5.0, row=None):
        self.attached.append(cmd)
        self.attached_rows.append(row)
        if self.hang:
            import time
            time.sleep(deadline + 0.2)
        return "attached in a new window"

    def focus(self, row, deadline=5.0):
        self.asked.append(row.get("sessionId"))
        if self.hang:
            import time
            time.sleep(deadline + 0.2)
            return f"iTerm2 did not answer in {deadline:g}s"
        return self.answer


class UiTest(unittest.IsolatedAsyncioTestCase):
    def app(self, collector=None, adapter=None):
        return ui.CcwhoUi(adapter=adapter or FakeAdapter(),
                          collector=collector or FakeCollector())

    def screen_text(self, app):
        # Textual 8 keeps a Static's text in .content (older versions: .renderable)
        return "\n".join(str(getattr(w, "content", "")) for w in app.query("Static"))


class TestTheList(UiTest):
    async def test_the_panel_is_kept_in_front_from_the_start(self):
        # the hotkey panel loses the focus iTerm2 gives it - see ccwho_panel;
        # whether this window IS the panel is the adapter's question, not ours
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(adapter.kept_in_front, 1)

    async def test_it_groups_by_what_each_session_needs(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            text = self.screen_text(app)
            self.assertIn("NEEDS YOU", text)
            self.assertIn("BUSY", text)
            self.assertIn("Issue 362", text)

    async def test_the_second_line_is_the_recap_with_its_age(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            text = self.screen_text(app)
            self.assertIn("gate flake, finally understood", text)
            self.assertIn("2d", text)

    async def test_an_empty_fleet_says_what_to_do(self):
        app = self.app(collector=FakeCollector(fleet=ui.Fleet([], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertIn("No Claude Code sessions running", self.screen_text(app))

    async def test_an_unreadable_source_says_so_rather_than_looking_empty(self):
        fleet = ui.Fleet([], False, "12:00:00",
                         "cannot read the session list - run `ccwho doctor`")
        app = self.app(collector=FakeCollector(fleet=fleet))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertIn("ccwho doctor", self.screen_text(app))


class TestSecureInputShowsInTheList(UiTest):
    """The list can come forward without the hotkey (click iTerm2, run
    `ccwho`), so it is where a dead hotkey gets explained."""

    LINE = ("hotkey: Discord (pid 7835) holds Secure Input, so no hotkey"
            " reaches any app - lock the screen (Ctrl+Cmd+Q) and log back in")

    async def test_it_is_shown_above_the_list(self):
        fleet = ui.Fleet([LIVE], True, "12:00:00", secure=self.LINE)
        app = self.app(collector=FakeCollector(fleet=fleet))
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = app.query_one("#banner")
            self.assertTrue(banner.display)
            self.assertIn("Ctrl+Cmd+Q", str(banner.render()))

    async def test_nothing_held_means_no_banner(self):                # control
        app = self.app(collector=FakeCollector(fleet=ui.Fleet([LIVE], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one("#banner").display)

    async def test_it_does_not_hide_a_fleet_error(self):
        fleet = ui.Fleet([], False, "12:00:00", "could not read the fleet: boom",
                         secure=self.LINE)
        app = self.app(collector=FakeCollector(fleet=fleet))
        async with app.run_test() as pilot:
            await pilot.pause()
            text = str(app.query_one("#banner").render())
            self.assertIn("boom", text)
            self.assertIn("Ctrl+Cmd+Q", text)


class TestTheListShowsDoctorsFirstFault(UiTest):
    """`ccwho --watch` showed doctor's first fault in its header - the only
    place a fault showed without `ccwho doctor`. The watch is gone (the owner's
    CLI revamp, 2026-10-06); the list shows it on its warning line."""

    LINE = "autosave job: loaded but not running - run `ccwho doctor`"

    async def test_it_is_shown_above_the_list(self):
        app = self.app(collector=FakeCollector(fleet=ui.Fleet([LIVE], True, "12:00:00",
                                                              doctor=self.LINE)))
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = app.query_one("#banner")
            self.assertTrue(banner.display)
            self.assertIn("⚠ autosave job: loaded but not running", str(banner.render()))

    async def test_a_healthy_machine_shows_nothing(self):                 # control
        app = self.app(collector=FakeCollector(fleet=ui.Fleet([LIVE], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one("#banner").display)

    async def test_it_hides_neither_a_fleet_error_nor_secure_input(self):
        fleet = ui.Fleet([], False, "12:00:00", "could not read the fleet: boom",
                         secure=TestSecureInputShowsInTheList.LINE, doctor=self.LINE)
        app = self.app(collector=FakeCollector(fleet=fleet))
        async with app.run_test() as pilot:
            await pilot.pause()
            text = str(app.query_one("#banner").render())
            for part in ("boom", "Ctrl+Cmd+Q", "autosave job"):
                self.assertIn(part, text)


class TestTheCollectorAsksDoctor(unittest.TestCase):
    """At most every DOCTOR_TTL (ccwho.doctor_banner_cached), on a thread of its
    own - a scan never waits for its checks - and never the reason the list
    goes down."""

    def collector(self, answer):
        import ccwho as runner
        c = ui.Collector()
        c.reload = lambda: None
        c.doctor = lambda: REAL_DOCTOR(c)
        self.states = []
        real = runner.doctor_banner_cached
        self.addCleanup(setattr, runner, "doctor_banner_cached", real)
        runner.doctor_banner_cached = lambda state, now=None: self.states.append(state) or answer(state)
        real_collect = ui.engine.collect
        self.addCleanup(setattr, ui.engine, "collect", real_collect)
        ui.engine.collect = lambda cache=None, status=None: (
            status.update(source_ok=True) or ([LIVE], {}))
        self.addCleanup(self.settle, c)
        return c

    def settle(self, c):
        """Doctor's answer is in once its thread has ended."""
        thread = getattr(c, "doctor_thread", None)
        if thread is not None:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "doctor's thread did not end")

    def test_its_line_reaches_the_fleet(self):
        c = self.collector(lambda state: "autosave job: not loaded")
        c.fleet()
        self.settle(c)
        self.assertEqual(c.fleet().doctor, "autosave job: not loaded")

    def test_a_slow_doctor_does_not_hold_the_rows(self):
        # its checks are subprocesses and an ask of iTerm2: seconds when one
        # is slow. The rows come first, its line on a later scan (review 1 of
        # the CLI revamp, slice 1)
        import time
        go = threading.Event()
        self.addCleanup(go.set)
        c = self.collector(lambda state: go.wait(3) and "autosave job: not loaded")
        began = time.monotonic()
        first = c.fleet()
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertEqual((len(first.rows), first.doctor), (1, ""))
        go.set()
        self.settle(c)
        self.assertEqual(c.fleet().doctor, "autosave job: not loaded")

    def test_one_doctor_at_a_time(self):
        # a slow one is not joined by another on every scan
        go = threading.Event()
        self.addCleanup(go.set)
        c = self.collector(lambda state: go.wait(3) and "")
        for _ in range(3):
            c.fleet()
        go.set()
        self.settle(c)
        self.assertEqual(len(self.states), 1)

    def test_one_cache_across_scans(self):
        # the TTL lives in the state it is given: a new one each scan would
        # run doctor's checks every 20 seconds
        c = self.collector(lambda state: "")
        c.fleet()
        self.settle(c)
        c.fleet()
        self.settle(c)
        self.assertEqual(len(self.states), 2)
        self.assertIs(self.states[0], self.states[1])

    def test_a_failing_doctor_never_takes_the_fleet_down(self):
        # nor prints its traceback over the screen: an error a thread does not
        # catch goes to threading.excepthook, which writes it to stderr
        def boom(state):
            raise RuntimeError("launchctl moved")
        uncaught = []
        self.addCleanup(setattr, threading, "excepthook", threading.excepthook)
        threading.excepthook = uncaught.append
        c = self.collector(boom)
        c.fleet()
        self.settle(c)
        fleet = c.fleet()
        self.assertEqual((len(fleet.rows), fleet.doctor), (1, ""))
        self.assertEqual(uncaught, [])

    def test_a_doctor_thread_that_cannot_start_never_takes_the_fleet_down(self):
        # "can't start new thread": the scan keeps its rows (review 2 of the
        # CLI revamp, slice 1), and the next scan tries again
        c = self.collector(lambda state: "autosave job: not loaded")
        with mock.patch.object(threading.Thread, "start",
                               side_effect=RuntimeError("can't start new thread")):
            fleet = c.fleet()
        self.assertEqual((len(fleet.rows), fleet.doctor), (1, ""))
        c.fleet()
        self.settle(c)
        self.assertEqual(c.fleet().doctor, "autosave job: not loaded")

    def test_a_failing_doctor_keeps_its_last_answer(self):
        # "could not tell" is not "fixed"
        answers = iter(["autosave job: not loaded"])

        def first_then_failing(state):
            return next(answers)                     # StopIteration the second time
        c = self.collector(first_then_failing)
        c.fleet()
        self.settle(c)
        c.fleet()                                    # its checks fail this time
        self.settle(c)
        self.assertEqual(len(self.states), 2)
        self.assertEqual(c.fleet().doctor, "autosave job: not loaded")

    def test_a_failed_scan_says_why_after_doctor_s_first_look(self):
        # the real doctor line, not a stand-in: "claude: not found" explains
        # the error once doctor's first look is in, not 5 minutes later
        # (review 4 of the CLI revamp, slice 1)
        import ccwho as runner
        c = ui.Collector()
        c.reload = lambda: None
        c.doctor = lambda: REAL_DOCTOR(c)
        facts = {"claude": "", "iterm_ok": True, "handler_registered": True,
                 "launchd_loaded": True, "last_run_age": 10.0, "newest_manifest_age": 10.0,
                 "cc_status_hook": True, "settings_path": "/x", "iterm_use": "in use",
                 "iterm_host": True}
        for owner, name, value in (
                (runner.setup, "gather", lambda **kw: dict(facts)),
                (runner, "usage_facts", lambda now=None: {"usage_roots": [],
                                                          "usage_newest_age": None})):
            self.addCleanup(setattr, owner, name, getattr(owner, name))
            setattr(owner, name, value)
        self.addCleanup(setattr, ui.engine, "collect", ui.engine.collect)

        def failing(cache=None, status=None):
            raise OSError("no claude")
        ui.engine.collect = failing
        self.addCleanup(self.settle, c)
        c.fleet()
        self.settle(c)
        fleet = c.fleet()
        self.assertIn("could not read the fleet", fleet.error)
        self.assertIn("claude", fleet.doctor)

    def test_a_scan_that_failed_still_says_what_doctor_found(self):
        # doctor's line is what explains the error: "claude: not found"
        # (review 1 of the CLI revamp, slice 1)
        c = self.collector(lambda state: "claude: not found")

        def failing(cache=None, status=None):
            raise OSError("no claude")
        ui.engine.collect = failing
        c.fleet()
        self.settle(c)
        fleet = c.fleet()
        self.assertIn("could not read the fleet", fleet.error)
        self.assertEqual(fleet.doctor, "claude: not found")


class TestTheListsStopIsACommandCcwhoTakes(unittest.TestCase):
    """`x` then `s` runs `ccwho stop <id> --yes` as its own process: the rules
    of every command (COMMAND_TAKES) take that argv as it is (review 1 of the
    CLI revamp, slice 3a)."""

    def test_its_argv_passes_the_rules(self):
        import ccwho as runner
        ran = []

        def run(argv, **k):
            ran.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(ui.subprocess, "run", side_effect=run):
            ui.Collector().stop_session({"sessionId": "4f2b91ac-1111-4222-8333-abcdefabcdef"})
        argv = ran[0][2:]                         # past the python and ccwho.py
        self.assertEqual(argv[0], "stop")
        self.assertEqual(runner.command_args(argv[0], argv[1:]), argv[1:])


class TestTheCollectorLooksForSecureInput(unittest.TestCase):
    def fleet_with(self, holder):
        import ccwho_setup as setup
        collector = ui.Collector()
        collector.reload = lambda: None
        real = ui.engine.collect, setup.secure_input_holder
        ui.engine.collect = lambda cache=None, status=None: (
            status.update(source_ok=True) or ([], {}))
        setup.secure_input_holder = lambda *a, **k: holder
        try:
            return collector.fleet()
        finally:
            ui.engine.collect, setup.secure_input_holder = real

    def test_a_holder_reaches_the_fleet(self):
        fleet = self.fleet_with({"pid": 7835, "app": "Discord"})
        self.assertIn("Discord", fleet.secure)
        self.assertIn("Ctrl+Cmd+Q", fleet.secure)

    def test_no_holder_is_quiet(self):                                # control
        self.assertEqual(self.fleet_with(None).secure, "")

    def test_a_failing_look_never_takes_the_fleet_down(self):
        import ccwho_setup as setup
        collector = ui.Collector()
        collector.reload = lambda: None
        real = ui.engine.collect, setup.secure_input_holder
        ui.engine.collect = lambda cache=None, status=None: (
            status.update(source_ok=True) or ([LIVE], {}))
        def boom(*a, **k):
            raise RuntimeError("ioreg moved")
        setup.secure_input_holder = boom
        try:
            fleet = collector.fleet()
        finally:
            ui.engine.collect, setup.secure_input_holder = real
        self.assertEqual(len(fleet.rows), 1)
        self.assertEqual(fleet.secure, "")


class TestTheListLetsGoOfLaunchesItSeesRunning(unittest.TestCase):
    """The list scans all day: a launch that timed out and then ran is seen
    here first. Its claim must go, or a reopen after the session ends says
    "restart iTerm2" for a launch that worked."""

    SID = "4f2b91ac-1111-4222-8333-abcdefabcdef"

    def claim_after(self, rows):
        import os, shutil, tempfile, time
        import ccwho as runner
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.environ["CCWHO_DIR"] = tmp
        self.addCleanup(os.environ.pop, "CCWHO_DIR", None)
        runner._write_claim(self.SID, {"pid": 4_000_000, "since": time.time() - 5,
                                       "sessionId": self.SID, "unresolved": True,
                                       "iterm_pid": 4_000_000})
        collector = ui.Collector()
        collector.reload = lambda: None
        collector.secure_input = lambda: ""
        asked = []
        collector.usage = lambda rows, procs=None: asked.append(procs) or {"state": "unknown"}
        self.addCleanup(lambda: self.assertEqual(asked, [{}]))   # collect's own procs
        real = ui.engine.collect
        ui.engine.collect = lambda cache=None, status=None: (
            status.update(source_ok=True) or (list(rows), {}))
        try:
            collector.fleet()
        finally:
            ui.engine.collect = real
        return runner._reason(runner._read_claim(self.SID), time.time(), None)[0] == "unresolved"

    def test_a_ccwho_that_will_not_import_keeps_the_list_up(self):
        import sys
        collector = ui.Collector()
        collector.reload = lambda: None
        collector.secure_input = lambda: ""
        collector.usage = lambda rows, procs=None: {"state": "unknown"}
        real, saved = ui.engine.collect, sys.modules.get("ccwho")
        ui.engine.collect = lambda cache=None, status=None: (
            status.update(source_ok=True) or ([row(self.SID)], {}))
        sys.modules["ccwho"] = None
        try:
            fleet = collector.fleet()
        finally:
            ui.engine.collect = real
            sys.modules["ccwho"] = saved
        self.assertEqual([r["sessionId"] for r in fleet.rows], [self.SID])

    def test_a_session_on_the_list_lets_go_of_its_claim(self):
        self.assertFalse(self.claim_after([row(self.SID)]))

    def test_one_not_on_it_keeps_it(self):                              # control
        self.assertTrue(self.claim_after([row("99999999-9999-4999-8999-999999999999")]))

    def test_a_claim_made_during_the_lists_scan_survives_it(self):
        # the list's `claude agents` read can take 30 s: a claim made while it
        # ran may be a new launch of a session that has ended since
        import os, shutil, tempfile, time
        import ccwho as runner
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.environ["CCWHO_DIR"] = tmp
        self.addCleanup(os.environ.pop, "CCWHO_DIR", None)

        def collect(cache=None, status=None):
            time.sleep(0.02)
            runner._write_claim(self.SID, {"pid": 4_000_000, "since": time.time(), "sessionId": self.SID,
                                           "unresolved": True, "iterm_pid": 4_000_000})
            time.sleep(0.02)
            status.update(source_ok=True)
            return [row(self.SID)], {}
        collector = ui.Collector()
        collector.reload = lambda: None
        collector.secure_input = lambda: ""
        collector.usage = lambda rows, procs=None: {"state": "unknown"}
        real = ui.engine.collect
        ui.engine.collect = collect
        try:
            collector.fleet()
        finally:
            ui.engine.collect = real
        self.assertEqual(runner._reason(runner._read_claim(self.SID), time.time(), None)[0], "unresolved")


class TestKeys(UiTest):
    async def test_enter_goes_to_the_selected_session(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(adapter.asked, [LIVE["sessionId"]])

    async def test_a_hung_iterm_does_not_freeze_the_keys(self):
        app = self.app(adapter=FakeAdapter(hang=True))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.press("j")              # while the focus call is stuck
            await pilot.pause()
            self.assertTrue(app.rows_on_screen(), "the list is still there")

    async def test_right_opens_the_brief_and_left_closes_it(self):
        app = self.app()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertTrue(app.query_one("#detail").display)
            self.assertIn("Shall I land it?", self.screen_text(app))
            await pilot.press("left")
            await pilot.pause()
            self.assertFalse(app.query_one("#detail").display)

    async def test_at_a_narrow_width_the_brief_takes_the_screen(self):
        app = self.app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertTrue(app.query_one("#detail").has_class("full"))
            self.assertFalse(app.query_one("#list").display)

    async def test_search_narrows_the_list(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            for ch in "release":
                await pilot.press(ch)
            await pilot.pause()
            text = self.screen_text(app)
            self.assertIn("Release queue", text)
            self.assertNotIn("Issue 362", text)

    async def test_escape_clears_the_search(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            for ch in "release":
                await pilot.press(ch)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertIn("Issue 362", self.screen_text(app))

    async def test_a_search_matching_nothing_says_so(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            for ch in "zzzz":
                await pilot.press(ch)
            # once what was said is searched too: half a second after the last key
            for _ in range(60):
                if "no session matches" in self.screen_text(app):
                    break
                await pilot.pause(0.05)
            self.assertIn("no session matches", self.screen_text(app))


class TestTheAdapterTalksToIterm(unittest.TestCase):
    """The device path is what AppleScript matches on. `s022` is the short form
    the LIST shows; `/dev/s022` is not a terminal that exists."""

    def test_it_passes_the_real_device_path(self):
        seen = {}

        class Done:
            returncode, stdout, stderr = 0, "focused s022", ""

        def fake(cmd, **kw):
            seen["cmd"] = cmd
            return Done()

        real = ui.subprocess.run
        ui.subprocess.run = fake
        try:
            ui.Adapter().focus({"tty": "ttys022", "pid": 1})
        finally:
            ui.subprocess.run = real
        self.assertIn("/dev/ttys022", seen["cmd"])

    def test_a_long_form_tty_is_not_doubled(self):
        seen = {}

        class Done:
            returncode, stdout, stderr = 0, "focused", ""

        def fake(cmd, **kw):
            seen["cmd"] = cmd
            return Done()

        real = ui.subprocess.run
        ui.subprocess.run = fake
        try:
            ui.Adapter().focus({"tty": "/dev/ttys022", "pid": 1})
        finally:
            ui.subprocess.run = real
        self.assertIn("/dev/ttys022", seen["cmd"])
        self.assertNotIn("/dev//dev", " ".join(seen["cmd"]))

    def said_after_a_refusal(self, act):
        class Refused:
            returncode, stdout = 1, ""
            stderr = "execution error: Can\u2019t get window of /Users/x/secret. (-1728)"
        real = ui.subprocess.run
        ui.subprocess.run = lambda *a, **k: Refused()
        try:
            return act(ui.Adapter())
        finally:
            ui.subprocess.run = real

    def test_a_refused_focus_is_said_by_its_code(self):
        # osascript's message quotes paths and tab names: its code, never its text
        said = self.said_after_a_refusal(lambda a: a.focus({"tty": "ttys022", "pid": 1}))
        self.assertIn("(-1728)", said)
        self.assertNotIn("/Users/x/secret", said)

    def test_a_focus_iterm2_did_not_answer_is_not_a_refusal(self):
        class Stuck:
            returncode, stdout, stderr = 1, "", "execution error: AppleEvent timed out. (-1712)"
        real = ui.subprocess.run
        ui.subprocess.run = lambda *a, **k: Stuck()
        try:
            said = ui.Adapter().focus({"tty": "ttys022", "pid": 1})
            attached = ui.Adapter().attach("claude attach x")
        finally:
            ui.subprocess.run = real
        for text in (said, attached):
            self.assertNotIn("refused", text)
            self.assertIn("did not answer", text)

    def test_a_refused_attach_is_said_by_its_code(self):
        said = self.said_after_a_refusal(lambda a: a.attach("claude attach x"))
        self.assertIn("(-1728)", said)
        self.assertNotIn("/Users/x/secret", said)

    def test_a_session_with_no_window_says_so_and_runs_nothing(self):
        ran = []
        real = ui.subprocess.run
        ui.subprocess.run = lambda *a, **k: ran.append(a) or None
        try:
            said = ui.Adapter().focus({"tty": "", "pid": 42})
        finally:
            ui.subprocess.run = real
        self.assertEqual(ran, [])
        self.assertIn("42", said)


class TestRestart(UiTest):
    async def test_r_exits_with_the_code_the_runner_re_execs_on(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
        self.assertEqual(app.return_value, "restart",
                         "exit(message=...) is printed, not returned")


class TestOneCollectionAtATime(UiTest):
    async def test_an_older_snapshot_cannot_overwrite_a_newer_one(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            newer = ui.Fleet([BUSY], True, "12:00:10")
            newer.seq = 9                      # what the worker stamps on it
            older = ui.Fleet([LIVE, BUSY], True, "12:00:00")
            older.seq = 8
            app.show(newer)
            app.show(older)                    # a slow worker finishing late
            await pilot.pause()
            self.assertEqual(len(app.fleet.rows), 1,
                             "a stale collection must not repaint over a fresh one")


class TestSelectionIsNotJustFocus(UiTest):
    async def test_enter_still_targets_the_session_when_the_list_is_hidden(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(100, 40)) as pilot:   # narrow: detail covers it
            await pilot.pause()
            chosen = app.selected_id()
            await pilot.press("right")
            await pilot.pause()
            app.show(app.fleet)                # a refresh while the list is hidden
            await pilot.pause()
            # the full-window detail has the keys (Enter copies there): back to
            # the list, and Enter goes to the session you chose
            await pilot.press("left")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(adapter.asked, [chosen])

    async def test_back_from_a_narrow_detail_restores_the_selection(self):
        app = self.app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            chosen = app.selected_id()
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
            self.assertEqual(app.selected_id(), chosen)


class TestTheBriefFollowsTheSelection(UiTest):
    async def test_moving_the_cursor_repaints_the_open_brief(self):
        # the pane showing session A while Enter would go to session B is worse
        # than no pane: you act on what you are reading
        briefs = {LIVE["sessionId"]: dict(FakeCollector().brief_value,
                                          title="Issue 362"),
                  BUSY["sessionId"]: dict(FakeCollector().brief_value,
                                          title="Release queue")}

        class PerSession(FakeCollector):
            def brief(self, row):
                return briefs[row["sessionId"]]

        app = self.app(collector=PerSession())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertIn("Issue 362", str(app.query_one("#brief").content))
            await pilot.press("j")
            await pilot.pause()
            self.assertIn("Release queue", str(app.query_one("#brief").content),
                          "the brief has to follow the cursor")


class TestSelectionSurvivesARefresh(UiTest):
    async def test_the_same_session_stays_selected_when_the_list_re_sorts(self):
        collector = FakeCollector()
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("j")               # move to the second row
            await pilot.pause()
            chosen = app.selected_id()
            collector.fleet_value = ui.Fleet([BUSY, LIVE], True, "12:00:05")
            app.show(collector.fleet_value)      # the refresh re-sorts under you
            await pilot.pause()
            self.assertEqual(app.selected_id(), chosen,
                             "Enter must go where you were looking")


class TestResize(UiTest):
    async def test_crossing_the_width_moves_the_brief_in_or_out(self):
        app = self.app()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertFalse(app.query_one("#detail").has_class("full"))
            await pilot.resize_terminal(100, 40)
            await pilot.pause(0.3)
            self.assertTrue(app.query_one("#detail").has_class("full"),
                            "a narrow window cannot hold both")
            await pilot.resize_terminal(160, 40)
            await pilot.pause(0.3)
            self.assertFalse(app.query_one("#detail").has_class("full"))
            self.assertTrue(app.query_one("#list").display)

    async def test_rows_are_re_measured_for_the_new_width(self):
        long_recap = row("eeee5555-0000-4000-8000-000000000005", "asks",
                         recap="a recap long enough that it cannot fit twice over: "
                               + "and then some more of it " * 6,
                         recap_age="2d", turns_since_recap=4)
        app = self.app(collector=FakeCollector(
            fleet=ui.Fleet([long_recap], True, "12:00:00")))
        async with app.run_test(size=(170, 40)) as pilot:
            await pilot.pause()
            wide = str(app.rows_on_screen()[0].content)
            await pilot.resize_terminal(70, 40)
            await pilot.pause(0.3)          # the width poll runs on a 0.2s timer
            narrow = str(app.rows_on_screen()[0].content)
            self.assertNotEqual(wide, narrow, "text cut for 170 columns at 70")
            for line in narrow.splitlines():
                self.assertLessEqual(len(line), 70)


class TestSearchKeys(UiTest):
    async def test_escape_closes_an_empty_search_box(self):
        # otherwise the box keeps focus and swallows j, k, q as text
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.query_one("#search").display)
            self.assertTrue(isinstance(app.focused, ui.Row))


class TestTheSearchBoxStandsOut(UiTest):
    """Reported: "when searching, give the search box a highlight ... Otherwise
    it's too easy to miss we are in search mode." Drawn like any input, the box
    was a grey frame at the bottom - blue while you typed - and after Enter,
    with the keys back on the list, nothing down there said the list was cut.
    Open, it is framed in the accent, the colour of NEEDS YOU and of the
    selected row's bar: not the kill box's red, which on an input reads as an
    error."""

    def assert_lit(self, app):
        """What the screen draws, not what the stylesheet says: a frame of
        spaces, one a tint dims, or one an amber widget covers has the accent in
        the box's styles too."""
        from textual.color import Color
        from textual.geometry import Region
        box = app.query_one("#search")
        # the theme says #ffa62b; Textual's $accent is #fea62b
        accent = Color.parse(app.get_css_variables()["accent"]).rgb
        r = box.region
        lines = box.render_lines(Region(0, 0, r.width, r.height))
        for side, (x, y) in {"corner": (0, 0), "top": (r.width // 2, 0),
                             "bottom": (r.width // 2, r.height - 1),
                             "left": (0, r.height // 2),
                             "right": (r.width - 1, r.height // 2)}.items():
            self.assertIs(app.screen.get_widget_at(r.x + x, r.y + y)[0], box,
                          f"the {side} frame is covered")
            # by cell, not by character: a wide character takes two
            self.assertNotIn(lines[y].crop(x, x + 1).text, " ", f"no {side} frame is drawn")
            drawn = app.screen.get_style_at(r.x + x, r.y + y).color
            self.assertEqual(tuple(drawn.triplet), accent, f"the {side} frame is not lit")

    async def search(self, pilot, text):
        await pilot.press("slash")
        for ch in text:
            await pilot.press(ch)
        await pilot.pause()

    async def test_the_box_you_type_in_is_lit(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await self.search(pilot, "release")
            self.assertIs(app.focused, app.query_one("#search"))
            self.assert_lit(app)

    async def test_it_stays_lit_when_enter_gives_the_keys_to_the_list(self):
        # where it was missed: the list is cut, and the keys are on a row
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await self.search(pilot, "release")
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.focused, ui.Row)
            self.assertNotIn("Issue 362", self.screen_text(app), "the list is cut")
            self.assert_lit(app)


class TestTheEngineIsHotReloaded(unittest.TestCase):
    """This window stays open for days. A fix on disk it cannot see is a fix that
    did not happen - so every collection re-reads the engine first."""

    def test_collecting_re_reads_the_engine(self):
        collector = ui.Collector()
        calls = []
        real_reload, real_collect = collector.reload, ui.engine.collect
        collector.reload = lambda: calls.append("reload")
        ui.engine.collect = lambda cache=None, status=None: ([], 0)
        try:
            collector.fleet()
        finally:
            collector.reload = real_reload
            ui.engine.collect = real_collect
        self.assertEqual(calls, ["reload"])

    def test_a_broken_edit_keeps_the_working_rules_and_says_so(self):
        collector = ui.Collector()
        path = ui.engine.brief.__file__
        original = open(path).read()
        real_collect = ui.engine.collect
        ui.engine.collect = lambda cache=None, status=None: ([], 0)
        try:
            with open(path, "w") as fh:
                fh.write(original + "\nthis is not python(")
            fleet = collector.fleet()
            self.assertIn("reload failed", fleet.error)
            self.assertTrue(ui.engine.brief.is_substantive("fix the gate"),
                            "the rules that worked a moment ago still work")
        finally:
            with open(path, "w") as fh:
                fh.write(original)
            ui.engine.collect = real_collect
            collector.reload()


class TestRefreshRate(unittest.TestCase):
    """One cadence. The cheap check between scans is what makes the list quick;
    the scan is the floor under it, for what a file stat cannot see."""

    def test_a_scan_waits_its_turn(self):
        c = ui.Collector()
        self.assertTrue(c.due(now=1000.0))
        self.assertFalse(c.due(now=1000.0 + 1))
        self.assertTrue(c.due(now=1000.0 + ui.REFRESH_EVERY + 0.1))

    def test_the_same_whether_or_not_the_window_is_in_front(self):
        # one cadence: the check between scans is what makes it quick, and it
        # runs whatever the terminal thinks about focus
        c = ui.Collector()
        self.assertTrue(c.due(now=1000.0))
        self.assertEqual(c.due(True, now=1000.0 + ui.REFRESH_EVERY + 0.1),
                         True)


if __name__ == "__main__":
    unittest.main()


class TestComingBackFromTheDetailKeepsTheSearch(UiTest):
    """Reported from real use: search, open the detail, press left - and the
    search was gone, so you were back at the whole fleet instead of the three
    rows you had narrowed it to."""

    async def test_left_closes_the_detail_and_leaves_the_search_alone(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            app.query_one("#search").value = "queue"
            await pilot.pause()
            await pilot.press("enter")               # out of the box, into the list
            await pilot.pause()
            await pilot.press("right")               # the brief
            await pilot.pause()
            self.assertTrue(app.detail_open)
            await pilot.press("left")
            await pilot.pause()
            self.assertFalse(app.detail_open, "left closes the detail first")
            self.assertEqual(app.filter_text, "queue", "and the search survives it")

    async def test_a_second_left_then_clears_the_search(self):        # control
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            app.query_one("#search").value = "queue"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("left")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.filter_text, "")


class TestTheScreenDoesNotFlash(UiTest):
    """Every collection tore the list down and built it again - three times a
    second's worth of flicker on a window that sits open all day. A refresh that
    changes nothing must change nothing on screen."""

    async def test_an_unchanged_fleet_does_not_rebuild_the_list(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            before = list(app.query(ui.Row))
            app.collect()
            await pilot.pause()
            await pilot.pause()
            after = list(app.query(ui.Row))
            self.assertEqual([id(w) for w in before], [id(w) for w in after],
                             "the same rows, not new widgets in their place")

    async def test_a_changed_recap_is_written_into_the_row_that_is_there(self):
        collector = FakeCollector()
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = list(app.query(ui.Row))
            changed = dict(LIVE, recap="now it says something else")
            collector.fleet_value = ui.Fleet([changed, BUSY], True, "12:01:00")
            app.collect()
            await pilot.pause()
            await pilot.pause()
            self.assertIn("something else", self.screen_text(app))
            self.assertEqual([id(w) for w in before],
                             [id(w) for w in app.query(ui.Row)])

    async def test_a_session_that_appears_does_rebuild(self):          # control
        collector = FakeCollector()
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = len(list(app.query(ui.Row)))
            collector.fleet_value = ui.Fleet(
                [LIVE, BUSY, row("cccc3333-0000-4000-8000-000000000003")],
                True, "12:01:00")
            app.collect()
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(len(list(app.query(ui.Row))), before + 1)


class TestYouCanSeeWhichRowYouAreOn(UiTest):
    async def test_the_selected_row_is_marked_in_the_text_not_only_by_colour(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            rows = list(app.query(ui.Row))
            self.assertTrue(rows)
            picked = [w for w in rows if w.has_class("selected")]
            self.assertEqual(len(picked), 1, "exactly one row is the selected one")
            self.assertIs(picked[0], app.focused)

    async def test_moving_moves_the_mark(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            first = [w for w in app.query(ui.Row) if w.has_class("selected")][0]
            await pilot.press("down")
            await pilot.pause()
            second = [w for w in app.query(ui.Row) if w.has_class("selected")][0]
            self.assertIsNot(first, second)
            self.assertFalse(first.has_class("selected"))


class TestColourCarriesOneMeaning(UiTest):
    async def test_a_row_is_drawn_from_the_parts_the_engine_named(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            widget = list(app.query(ui.Row))[0]
            styles = {style for _, style in widget.spans()}
            self.assertIn("recap", styles, "the recap is drawn as the recap")
            self.assertIn("mark", styles)

    async def test_the_state_is_in_the_mark_and_not_over_the_whole_row(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            for widget in app.query(ui.Row):
                painted = [role for _, role in widget.spans()
                           if role in ("mark",)]
                self.assertEqual(len(painted), 1, widget.row.get("sessionId"))

    def test_every_role_the_engine_names_has_a_style(self):
        import ccwho_engine as engine
        for role in engine.UI_ROLES:
            self.assertIn(role, ui.ROLE_STYLE, role)
        for name in set(engine.UI_STATE_STYLE.values()):
            self.assertIn(name, ui.STATE_STYLE, name)

    def headings(self, app):
        return {str(w.content): w.styles.color for w in app.query(".heading")}

    def assert_warm(self, colour, heading):
        self.assertGreater(colour.r, colour.g, f"{heading} is not amber: {colour}")

    def assert_green(self, colour, heading):
        self.assertGreater(colour.g, max(colour.r, colour.b),
                           f"{heading} is not green: {colour}")

    async def test_only_a_group_that_needs_you_has_its_colour(self):
        # reported: STOPPED in the colour of NEEDS YOU read, at a glance, as a
        # question waiting. What needs you keeps the colour; the rest are green.
        states = ("asks", "stuck", "stopped", "busy", "program")
        rows = [row(f"{i}{i}{i}{i}0000-0000-4000-8000-00000000000{i}", s)
                for i, s in enumerate(states, 1)]
        app = self.app(collector=FakeCollector(fleet=ui.Fleet(rows, True, "12:00:00")))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            seen = self.headings(app)
            self.assertEqual(set(seen), {"NEEDS YOU", "STUCK", "STOPPED", "BUSY",
                                         "PROGRAMS"})
            for heading in ("NEEDS YOU", "STUCK"):
                self.assert_warm(seen[heading], heading)
            for heading in ("STOPPED", "BUSY", "PROGRAMS"):
                self.assert_green(seen[heading], heading)

    async def refresh(self, app, pilot, collector, rows):
        collector.fleet_value = ui.Fleet(rows, True, "12:01:00")
        app.collect()
        await pilot.pause()
        await pilot.pause()

    def row_ids(self, app):
        """The row widgets, in any order: moved rows are the same widgets."""
        return {id(w) for w in app.query(ui.Row)}

    async def test_a_group_that_appears_on_a_refresh_has_its_colour_too(self):
        # a session that changes group moves rows that are there and makes only
        # the new heading: that path builds a heading of its own
        collector = FakeCollector()
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = self.row_ids(app)
            await self.refresh(app, pilot, collector,
                               [LIVE, dict(BUSY, attention="stopped")])
            self.assertEqual(before, self.row_ids(app),
                             "the rows moved, not rebuilt: this is the other path")
            seen = self.headings(app)
            self.assertEqual(set(seen), {"NEEDS YOU", "STOPPED"})
            self.assert_warm(seen["NEEDS YOU"], "NEEDS YOU")
            self.assert_green(seen["STOPPED"], "STOPPED")

    async def test_needs_you_that_appears_on_a_refresh_has_its_colour(self):
        # the common case on that path: a stopped session asks you something
        collector = FakeCollector(fleet=ui.Fleet(
            [dict(LIVE, attention="stopped"), BUSY], True, "12:00:00"))
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = self.row_ids(app)
            await self.refresh(app, pilot, collector, [LIVE, BUSY])
            self.assertEqual(before, self.row_ids(app),
                             "the rows moved, not rebuilt: this is the other path")
            self.assert_warm(self.headings(app)["NEEDS YOU"], "NEEDS YOU")

    async def test_the_screen_draws_what_the_engine_decided(self):
        # not a list of its own of which headings need you
        real = ui.engine.ui_groups

        def inverted(rows):
            return [dict(g, needs_you=not g["needs_you"]) for g in real(rows)]
        with mock.patch.object(ui.engine, "ui_groups", inverted):
            app = self.app()
            async with app.run_test() as pilot:
                await pilot.pause()
                seen = self.headings(app)
                self.assert_green(seen["NEEDS YOU"], "NEEDS YOU")
                self.assert_warm(seen["BUSY"], "BUSY")

    async def test_an_older_engine_on_disk_leaves_the_headings_as_they_were(self):
        # the engine is read from disk again on each refresh, the screen only at
        # start: an older engine checked out under a running list must not
        # close it. Its headings are drawn as they were before: all amber.
        real = ui.engine.ui_groups

        def older(rows):
            return [{k: v for k, v in g.items() if k != "needs_you"}
                    for g in real(rows)]
        other = row("cccc3333-0000-4000-8000-000000000003", "program")
        collector = FakeCollector()
        with mock.patch.object(ui.engine, "ui_groups", older):
            app = self.app(collector=collector)
            async with app.run_test() as pilot:
                await pilot.pause()
                # a session arrives: a full build
                await self.refresh(app, pilot, collector, [LIVE, BUSY, other])
                self.assertNotIn("STOPPED", self.headings(app))
                before = self.row_ids(app)
                # one changes group: the rows move, and STOPPED is made there
                await self.refresh(app, pilot, collector,
                                   [LIVE, dict(BUSY, attention="stopped"), other])
                self.assertTrue(app.is_running)
                self.assertEqual(before, self.row_ids(app),
                                 "the rows moved: the other path was taken too")
                seen = self.headings(app)
                self.assertEqual(set(seen), {"NEEDS YOU", "STOPPED", "PROGRAMS"})
                for heading, colour in seen.items():
                    self.assert_warm(colour, heading)


class TestTheBriefIsNotAWallOfWhite(UiTest):
    """render_brief already marks its labels dim and its title bold. The screen
    was asking it for plain text and then drawing every word the same."""

    async def test_the_detail_is_drawn_with_the_structure_the_brief_has(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            content = app.query_one("#brief").content
            styles = {str(s.style) for s in getattr(content, "spans", [])}
            self.assertGreater(len(styles), 1,
                               "labels, values and the title cannot all look the same")
            self.assertNotIn("\x1b", str(content), "no escape codes as text")

    async def test_the_words_are_all_there(self):                     # control
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            shown = str(app.query_one("#brief").content)
            for word in ("Issue 362", "rebase and land", "claude --resume"):
                self.assertIn(word, shown)


class TestNothingPaintsIntoAScreenThatIsGone(UiTest):
    """The width poll runs on a timer. On the way out - q, or the hotkey window
    closing - the widgets go before the timer stops, and a paint then raises
    NoMatches out of a callback nobody is waiting on."""

    async def test_a_tick_after_the_widgets_are_gone_is_not_a_crash(self):
        for gone in ("#list", "#header", "#banner", "#detail"):
            app = self.app()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.query_one(gone).remove()
                await pilot.pause()
                app.painted_width = 0      # force the poll to act
                app.painted_shape = None   # and the full rebuild path
                app.check_width()          # none of these may raise
                app.rebuild()
                app.paint_detail()
                app.mark_selected()
                app.restore_selection()


def many(n):
    return [row("%04d1111-0000-4000-8000-00000000%04d" % (i, i),
                "stopped" if i % 2 else "asks", title=f"session {i}",
                tab_title=f"session {i}") for i in range(n)]


class TestTheArrowsMoveTheSelectionNotTheScrollbar(UiTest):
    """With more sessions than the window can hold, the list container took the
    arrow keys for scrolling and the selection never moved. The keys belong to
    the row that has focus."""

    def long(self):
        return FakeCollector(fleet=ui.Fleet(many(40), True, "12:00:00"))

    async def test_down_moves_to_the_next_session(self):
        app = self.app(collector=self.long())
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            first = app.selected
            await pilot.press("down")
            await pilot.pause()
            self.assertNotEqual(app.selected, first)
            self.assertEqual([w.row["sessionId"] for w in app.query(ui.Row)
                              if w.has_class("selected")], [app.selected])

    async def test_up_comes_back(self):
        app = self.app(collector=self.long())
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            first = app.selected
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(app.selected, first)

    async def test_the_list_follows_the_selection_down_past_the_window(self):
        app = self.app(collector=self.long())
        async with app.run_test(size=(120, 12)) as pilot:
            await pilot.pause()
            for _ in range(12):
                await pilot.press("down")
            await pilot.pause()
            listing = app.query_one("#list")
            picked = [w for w in app.query(ui.Row) if w.has_class("selected")][0]
            self.assertGreater(listing.scroll_offset.y, 0,
                               "the list scrolled to keep up")
            self.assertIn(picked, listing.children,
                          "and the selected row is one of the rows on screen")

    async def test_a_short_list_still_moves(self):                    # control
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            first = app.selected
            await pilot.press("down")
            await pilot.pause()
            self.assertNotEqual(app.selected, first)


class TestOneClickGoesThere(UiTest):
    """Reported: picking a session with the mouse only highlighted it, and then
    you still had to reach for the keyboard."""

    async def test_clicking_a_row_selects_it_and_goes(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test() as pilot:
            await pilot.pause()
            rows = list(app.query(ui.Row))
            await pilot.click(rows[1])
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(app.selected, rows[1].row["sessionId"])
            self.assertEqual(adapter.asked, [rows[1].row["sessionId"]])


class TestARebuildIsOnePaint(UiTest):
    """Tearing the list down and mounting it again shows the empty list for a
    frame. Textual can hold the screen still until the whole rebuild is done."""

    async def test_the_rows_are_mounted_while_the_screen_is_held(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            listing = app.query_one("#list")
            held = []
            real = listing.mount_all
            # Textual calls batch_update itself in places, so watching THAT
            # proves nothing. What matters is whether the screen is being held
            # at the moment the rows go in.
            listing.mount_all = lambda widgets: (
                held.append(app._batch_count) or real(widgets))
            # a real build: a session that was not there before. Re-ordering
            # the rows that ARE there no longer goes through this path.
            app.fleet = ui.Fleet([LIVE, BUSY, row("aaaa9999-0000-4000-8000-000000009999")],
                                 True, "12:00:02")
            app.painted_shape = None
            app.rebuild()
            await pilot.pause()
            self.assertTrue(held, "the rebuild mounts in one go")
            self.assertGreater(held[0], 0,
                               "a rebuild paints once, not row by row")


class TestARowIsWrittenOnlyWhenItsWordsChange(UiTest):
    """A collection re-reads everything, so most row dicts differ every tick in
    fields the screen never shows - a process id, a byte offset, a timestamp.
    Comparing the dict meant every row was written three times a second."""

    async def counted(self, changed):
        collector = FakeCollector()
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            writes = []
            for widget in app.query(ui.Row):
                real = widget.refresh_text
                widget.refresh_text = lambda w, r=real, name=widget: (
                    writes.append(name) or r(w))
            collector.fleet_value = ui.Fleet([dict(LIVE, **changed), BUSY],
                                             True, "12:00:01")
            app.collect()
            await pilot.pause()
            await pilot.pause()
            return writes

    async def test_a_field_nobody_sees_does_not_repaint_the_row(self):
        self.assertEqual(await self.counted({"pid": 99999, "work": 3}), [])

    async def test_a_new_recap_does_repaint_it(self):                 # control
        writes = await self.counted({"recap": "something new to read"})
        self.assertEqual(len(writes), 1)
class TestEnterActsOnTheRowYouAreLookingAt(UiTest):
    """Reported from real use: Enter on the first NEEDS YOU row went to a busy
    session further down instead.

    The selection fell back to the first row of the SNAPSHOT, and the snapshot
    is not in screen order - the screen groups by what each session needs. So
    "nothing chosen yet" meant "whatever the collector happened to list first".
    """

    def out_of_order(self):
        # BUSY first in the snapshot, NEEDS YOU first on screen.
        return FakeCollector(fleet=ui.Fleet([BUSY, LIVE], True, "12:00:00"))

    async def test_the_first_row_on_screen_is_the_one_that_is_selected(self):
        app = self.app(collector=self.out_of_order())
        async with app.run_test() as pilot:
            await pilot.pause()
            first_shown = list(app.query(ui.Row))[0]
            self.assertEqual(app.selected, first_shown.row["sessionId"])
            self.assertTrue(first_shown.has_class("selected"))

    async def test_enter_goes_where_the_highlight_is(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter, collector=self.out_of_order())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(adapter.asked, [LIVE["sessionId"]],
                             "the row under the highlight, not the snapshot's first")

    async def test_a_selection_the_search_hid_does_not_stay_the_target(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.selected = BUSY["sessionId"]
            await pilot.press("slash")
            await pilot.pause()
            app.query_one("#search").value = "Issue 362"      # hides BUSY
            await pilot.pause()
            await pilot.press("enter")     # leaves the box, lands in the list
            await pilot.pause()
            await pilot.press("enter")     # and now: go
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(adapter.asked, [LIVE["sessionId"]],
                             "Enter cannot act on a session that is not shown")

    async def test_a_hidden_session_is_never_what_enter_would_act_on(self):
        # Straight at the method: a rebuild repairs the selection a moment
        # later, which is enough to hide this from a test that goes through
        # keystrokes - and not enough to make it safe in between.
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.selected = BUSY["sessionId"]
            app.filter_text = "Issue 362"                 # hides BUSY
            self.assertEqual(app.selected_row()["sessionId"], LIVE["sessionId"])
            self.assertEqual([r["sessionId"] for r in app.visible_rows()],
                             [LIVE["sessionId"]])

    async def test_moving_still_chooses_deliberately(self):           # control
        adapter = FakeAdapter()
        app = self.app(adapter=adapter, collector=self.out_of_order())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(adapter.asked, [BUSY["sessionId"]])


class TestTheJumpSaysWhereItLanded(unittest.TestCase):
    """A jump that goes to the wrong window in silence is how "it went to the
    wrong session" becomes a mystery. The script checks what is in front after
    it selects, and says so when it is not what was asked for."""

    def script(self):
        """The CODE, with the comments stripped out.

        The first version of this read the whole file, and every order it
        checked was satisfied by the prose above the code - the comment
        explaining `select s` sits before the line that does it, so the test
        passed with the calls in either order. A mutant caught that.
        """
        import os
        with open(os.path.join(os.path.dirname(os.path.abspath(ui.__file__)),
                               "jump.applescript")) as fh:
            lines = [line.split("--")[0] for line in fh]
        return "\n".join(line for line in lines if line.strip())

    def test_the_comments_are_not_what_is_being_tested(self):         # control
        self.assertNotIn("select s", "-- only `select s` picks the pane"
                         .split("--")[0])

    def test_it_ends_on_the_pane(self):
        # Measured on a real fleet: eleven PANES in a single tab. Selecting the
        # tab alone leaves you looking at whichever pane was last active in it.
        body = self.script()
        self.assertLess(body.index("select t"), body.index("select s"))

    def test_it_never_holds_a_window_by_its_position(self):
        # `repeat with w in windows` yields "window 3 of ...", and the first
        # select reorders the windows - so that reference then means a
        # DIFFERENT window. Everything after the search goes by id.
        body = self.script()
        self.assertIn("set winId to id of w", body)
        self.assertIn("tell window id winId", body)
        self.assertNotIn("select w\n", body + "\n")
        self.assertNotIn("index of w to 1", body)

    def test_it_raises_the_window_it_chose(self):
        # Activating raises every window the app has, so the chosen one has to
        # be put at the front explicitly, or another window ends up on top.
        body = self.script()
        self.assertLess(body.index("select window id winId"),
                        body.index("activate"))
        self.assertIn("set index of window id winId to 1", body)

    def test_it_compares_what_is_in_front_with_what_was_asked_for(self):
        body = self.script()
        self.assertIn("current session of current tab of current window", body)
        self.assertIn("landed on", body)

    def test_a_session_it_cannot_find_is_not_reported_as_focused(self):  # control
        self.assertIn('return "not found: "', self.script())


class TestTheStatusSaysWhichSessionItWent(UiTest):
    """"focused /dev/ttys007" is not an answer to "did it go to the one I
    picked". The name you chose it by is."""

    async def test_going_names_the_session_not_just_the_device(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            header = str(app.query_one("#header").content)
            self.assertIn("Issue 362", header,
                          "say which session, in the words you picked it by")


class TestYouCanSeeWhenItLastLooked(UiTest):
    """"The list is not updating" should be answerable by looking at it. The
    time of the snapshot was shown only when something had gone wrong, so a
    frozen list and a quiet fleet looked exactly the same."""

    async def test_the_header_says_when_the_snapshot_was_taken(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertIn("12:00:00", str(app.query_one("#header").content))

    async def test_a_newer_snapshot_shows_its_own_time(self):
        collector = FakeCollector()
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            collector.fleet_value = ui.Fleet([LIVE, BUSY], True, "12:00:30")
            app.collect()
            await pilot.pause()
            await pilot.pause()
            self.assertIn("12:00:30", str(app.query_one("#header").content))


class TestItChecksAgainRightAfterYouAnswerOne(UiTest):
    """Your words: when you open one that needs you, check THAT soon rather
    than scanning everything all the time. The moment you jump to a session is
    the moment its state is about to change - it stops needing you."""

    def setUp(self):
        # The real wait is two seconds; the behaviour under test is "it looks
        # again", not how long it waits.
        self.real_wait = ui.AFTER_A_JUMP
        ui.AFTER_A_JUMP = 0.05
        self.addCleanup(setattr, ui, "AFTER_A_JUMP", self.real_wait)

    async def test_a_jump_schedules_another_look_soon(self):
        collector = FakeCollector()
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = collector.calls
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.2)
            await pilot.pause()
            self.assertGreater(collector.calls, before,
                               "the session you just opened no longer needs you")

    async def test_the_look_is_not_blocked_by_the_usual_wait(self):
        # due() would say "not yet" for another few seconds; answering a session
        # is new information, not a tick.
        collector = FakeCollector()
        collector.due = lambda visible=None, now=None: False
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = collector.calls
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertGreater(collector.calls, before)
class TestTheWindowSaysWhatItIs(UiTest):
    """In iTerm2's window list the panel showed up as "python3" - the name of
    the interpreter uv happened to run. Anything that has a window has a name
    in Mission Control, in the window menu, and in a jump script's output."""

    async def test_it_tells_the_terminal_its_name(self):
        # App.title is Textual's own idea of the title and never leaves the
        # process - measured: running the app emits no title sequence at all.
        # The terminal only knows what it is told, in OSC 0.
        written = []
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.name_the_window(write=written.append)
        self.assertEqual(written, ["\x1b]0;ccwho\x07"])

    def test_the_name_is_not_the_interpreter(self):                   # control
        self.assertNotIn("python", ui.WINDOW_NAME.lower())


class TestALookAfterAJumpIsOneLook(UiTest):
    """The look after a jump set the refresh clock to zero and then collected,
    so the very next tick saw a zero clock and collected all over again - two
    full scans, a second apart, every time you opened a session."""

    def setUp(self):
        self.real_wait = ui.AFTER_A_JUMP
        ui.AFTER_A_JUMP = 0.05
        self.addCleanup(setattr, ui, "AFTER_A_JUMP", self.real_wait)

    async def test_the_tick_after_it_does_not_collect_again(self):
        collector = ui.Collector()          # the real one: its clock is the point
        collector.fleet = lambda: ui.Fleet([LIVE, BUSY], True, "12:00:00")
        collector.brief = lambda row: {}
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await asyncio.sleep(0.2)        # the look after the jump
            await pilot.pause()
            self.assertFalse(collector.due(True),
                             "that look counts as the last one")

    async def test_it_still_looks_when_the_wait_is_over(self):        # control
        collector = ui.Collector()
        collector.fleet = lambda: ui.Fleet([LIVE, BUSY], True, "12:00:00")
        collector.brief = lambda row: {}
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            collector.last = 0.0
            self.assertTrue(collector.due(True))


class TestItNoticesQuicklyWhenSomethingNeedsYou(UiTest):
    """The point of the list. A full scan is 0.50s, so scanning every second
    would cost half a core; the cheap check is 0.16s and answers the only
    question that has to be answered quickly - did anything change?"""

    class Sniffer(FakeCollector):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.digest = ("a", "idle")
            self.looks = 0

        def changed(self):
            self.looks += 1
            return self.digest != self.seen

        seen = ("a", "idle")

    async def test_a_change_brings_a_scan_forward(self):
        collector = self.Sniffer()
        collector.due = lambda visible=None, now=None: False     # not due for ages
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = collector.calls
            collector.digest = ("a", "waiting")             # it now needs you
            app.sniff()
            await pilot.pause()
            await pilot.pause()
            self.assertGreater(collector.calls, before,
                               "a session that starts needing you cannot wait"
                               " for the next scheduled scan")

    async def test_nothing_changing_costs_nothing(self):              # control
        collector = self.Sniffer()
        collector.due = lambda visible=None, now=None: False
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = collector.calls
            for _ in range(3):
                app.sniff()
                await pilot.pause()
            self.assertEqual(collector.calls, before,
                             "no scan while the world sits still")

    async def test_it_asks_while_you_are_looking(self):               # control
        collector = self.Sniffer()
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = collector.looks
            app.watch_tick()
            await pilot.pause()
            await pilot.pause()
            self.assertGreater(collector.looks, before)


class TestTheCheapCheckIsCheap(unittest.TestCase):
    def test_a_scan_records_the_digest_so_the_check_does_not_refire(self):
        collector = ui.Collector()
        seen = {}

        class FakeEngine:
            @staticmethod
            def collect(cache=None, status=None):
                status["source_ok"] = True
                status["watch"] = ("a", 1.0)
                return [], 0

        # fleet() re-imports the engine first, which would put the real
        # collect back before it is called.
        collector.reload = lambda: None
        real = ui.engine.collect
        ui.engine.collect = FakeEngine.collect
        try:
            collector.fleet()
        finally:
            ui.engine.collect = real
        self.assertEqual(collector.digest, ("a", 1.0),
                         "the scan's own answer, or the next check fires again")

    def test_the_check_starts_no_program(self):
        collector = ui.Collector()
        called = []
        real = ui.engine.agents_json
        ui.engine.agents_json = lambda: called.append(1) or "[]"
        try:
            collector.changed()
        finally:
            ui.engine.agents_json = real
        self.assertEqual(called, [], "0.23s of CPU per check is not a check")
class TestItWatchesWhetherOrNotYouAre(UiTest):
    """The list is not only a screen: its job is to know when a session needs
    you, so that one day it can say so out loud - a sound, a notification -
    while you are looking at something else entirely.

    That makes "is anyone looking at the window?" the wrong question. It is
    gone: one cadence, always watching.
    """

    async def test_the_cadence_does_not_depend_on_focus(self):
        collector = FakeCollector()
        looks = []
        collector.changed = lambda: looks.append(1) or False
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.post_message(ui.events.AppBlur())
            await pilot.pause()
            app.watch_tick()
            await pilot.pause()
            app.tick()
            await pilot.pause()
            self.assertTrue(looks)

    def test_there_is_one_interval_not_two(self):
        self.assertFalse(hasattr(ui, "REFRESH_HIDDEN"),
                         "a hidden cadence is a list that misses things")
        self.assertTrue(hasattr(ui, "REFRESH_EVERY"))

    def test_nothing_asks_whether_you_are_looking(self):
        import inspect
        body = inspect.getsource(ui)
        for gone in ("has_focus_hint", "AppBlur", "AppFocus", "self.watched"):
            self.assertNotIn(gone, body, f"{gone} decides nothing any more")

    async def test_a_scan_still_happens_when_nothing_changes(self):   # control
        collector = FakeCollector()
        collector.changed = lambda: False
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            collector.last = 0.0            # the floor is due
            before = collector.calls
            app.tick()
            await pilot.pause()
            await pilot.pause()
            self.assertGreater(collector.calls, before)


def reviewable(sid="dddd4444-0000-4000-8000-000000000004"):
    return row(sid, "review", title="Landed the rebase",
               tab_title="✳ Landed the rebase (claude)",
               ts="2026-09-22T10:00:00.000Z")


class TestLookingAtOneDropsItOutOfTheList(UiTest):
    """"Once I click on a row, it drops to stopped immediately until it picks
    up again." Immediately means on the screen, not on the next scan."""

    def setUp(self):
        self.marks = []
        self.real_mark = ui.engine.mark_reviewed
        ui.engine.mark_reviewed = lambda sid, ts, path=None: self.marks.append((sid, ts))
        self.addCleanup(setattr, ui.engine, "mark_reviewed", self.real_mark)

    def fleet(self):
        return FakeCollector(fleet=ui.Fleet([reviewable(), BUSY], True, "12:00:00"))

    async def test_going_to_a_session_records_that_you_looked(self):
        app = self.app(collector=self.fleet())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(self.marks,
                             [(reviewable()["sessionId"], "2026-09-22T10:00:00.000Z")])

    async def test_the_row_leaves_needs_you_at_once(self):
        app = self.app(collector=self.fleet())
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertIn("NEEDS YOU", self.screen_text(app))
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            listed = [w.row["attention"] for w in app.query(ui.Row)
                      if w.row["sessionId"] == reviewable()["sessionId"]]
            self.assertEqual(listed, ["stopped"],
                             "not on the next scan - now")

    async def test_a_click_does_the_same(self):
        app = self.app(collector=self.fleet())
        async with app.run_test() as pilot:
            await pilot.pause()
            rows = list(app.query(ui.Row))
            await pilot.click(rows[0])
            await pilot.pause()
            self.assertTrue(self.marks)

    async def test_a_session_with_a_question_is_not_dismissed(self):  # control
        # it is still actually waiting on you: looking cannot make that false
        app = self.app(collector=FakeCollector(
            fleet=ui.Fleet([row("eeee5555-0000-4000-8000-000000000005",
                                "blocked", ts="2026-09-22T10:00:00.000Z"),
                            BUSY], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            still = [w.row["attention"] for w in app.query(ui.Row)
                     if w.row["attention"] == "blocked"]
            self.assertEqual(still, ["blocked"])


class GatedAdapter(FakeAdapter):
    """iTerm2 that answers when the test says so."""

    def __init__(self):
        super().__init__()
        import threading
        self.gate = threading.Event()

    def focus(self, row, deadline=5.0):
        self.asked.append(row.get("sessionId"))
        self.gate_for(row.get("sessionId")).wait(10)
        return "focused s022"

    def gate_for(self, sid):
        """One gate for every session, unless a test asks for one each."""
        return getattr(self, "gates", {}).get(sid, self.gate)


class TestTheRowMovesWhenTheBlinkEnds(UiTest):
    """Reported: "When clicking on a line in needs you, it immediately moves to
    the stopped line, even before the blinking finishes." The row blinks where
    you clicked it, and moves once the window is open."""

    def setUp(self):
        self.marks = []
        real = ui.engine.mark_reviewed
        ui.engine.mark_reviewed = lambda sid, ts, path=None: self.marks.append((sid, ts))
        self.addCleanup(setattr, ui.engine, "mark_reviewed", real)

    def where(self, app):
        return [w.row["attention"] for w in app.query(ui.Row)
                if w.row["sessionId"] == reviewable()["sessionId"]]

    async def test_it_stays_and_blinks_in_needs_you_while_it_opens(self):
        adapter = GatedAdapter()
        app = self.app(adapter=adapter, collector=FakeCollector(
            fleet=ui.Fleet([reviewable(), BUSY], True, "12:00:00")))
        async with app.run_test() as pilot:
            try:
                await pilot.pause()
                await pilot.click(list(app.query(ui.Row))[0])
                await pilot.pause(0.1)
                self.assertEqual(adapter.asked, [reviewable()["sessionId"]])
                self.assertEqual(self.where(app), ["review"])
                self.assertEqual(self.marks, [],
                                 "marked now, the next scan would move it early")
                self.assertTrue([w for w in app.query(ui.Row) if w.has_class("acting")])
            finally:
                adapter.gate.set()
            await pilot.pause(0.1)
            await pilot.pause()
            self.assertEqual(self.where(app), ["stopped"])
            self.assertEqual(len(self.marks), 1)

    async def test_a_scan_during_the_jump_does_not_move_it(self):
        adapter = GatedAdapter()
        collector = FakeCollector(fleet=ui.Fleet([reviewable(), BUSY], True, "12:00:00"))
        app = self.app(adapter=adapter, collector=collector)
        async with app.run_test() as pilot:
            try:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause(0.1)
                collector.fleet_value = ui.Fleet([reviewable(), BUSY], True, "12:00:05")
                app.collect()
                await pilot.pause(0.1)
                self.assertEqual(self.where(app), ["review"])
            finally:
                adapter.gate.set()
            await pilot.pause(0.1)
            await pilot.pause()
            self.assertEqual(self.where(app), ["stopped"])

    async def test_no_row_moves_until_the_jump_is_complete(self):
        # "no rows should move until the jump is complete, not just needs you"
        adapter = GatedAdapter()
        other = row("ffff6666-0000-4000-8000-000000000006", "stopped",
                    title="Other", tab_title="Other")
        collector = FakeCollector(fleet=ui.Fleet([reviewable(), other, BUSY],
                                                 True, "12:00:00"))
        app = self.app(adapter=adapter, collector=collector)
        order = lambda: [w.row["sessionId"] for w in app.query(ui.Row)]
        async with app.run_test() as pilot:
            try:
                await pilot.pause()
                before = order()
                await pilot.press("enter")
                await pilot.pause(0.1)
                # the other stopped session starts asking: it would jump to the top
                collector.fleet_value = ui.Fleet(
                    [reviewable(), dict(other, attention="asks"), BUSY], True, "12:00:05")
                app.collect()
                await pilot.pause(0.1)
                self.assertEqual(order(), before, "nothing moves under the blink")
                self.assertIn("going to", app.status)
            finally:
                adapter.gate.set()
            await pilot.pause(0.1)
            await pilot.pause()
            self.assertEqual(order()[0], other["sessionId"], "and then it does")
            self.assertIn("12:00:05", self.screen_text(app))
            self.assertIn("went to", app.status)


class TestAJumpEndsOnlyWithItsOwnAnswer(UiTest):
    """Review of the change above: the end of the jump is where rows move, so
    only THAT jump's answer may end it - not an older jump's, not a kill's."""

    def setUp(self):
        # fresh each test: looking at a row rewrites it in place
        self.A = reviewable("aaaa0000-0000-4000-8000-00000000000a")
        self.B = reviewable("bbbb0000-0000-4000-8000-00000000000b")
        self.marks = []
        real = ui.engine.mark_reviewed
        ui.engine.mark_reviewed = lambda sid, ts, path=None: self.marks.append(sid)
        self.addCleanup(setattr, ui.engine, "mark_reviewed", real)

    def state(self, app):
        return [(w.row["sessionId"], w.row["attention"], w.has_class("acting"))
                for w in app.query(ui.Row)]

    async def test_an_older_jump_landing_does_not_end_the_newer_one(self):
        import threading
        adapter = GatedAdapter()
        adapter.gates = {self.A["sessionId"]: threading.Event(),
                         self.B["sessionId"]: threading.Event()}
        app = self.app(adapter=adapter, collector=FakeCollector(
            fleet=ui.Fleet([self.A, self.B, BUSY], True, "12:00:00")))
        async with app.run_test() as pilot:
            try:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause(0.1)
                await pilot.press("j", "enter")
                await pilot.pause(0.1)
                during = self.state(app)
                adapter.gates[self.A["sessionId"]].set()
                await pilot.pause(0.1)
                await pilot.pause()
                self.assertEqual(self.state(app), during,
                                 "B still blinks, and nothing moves under it")
                self.assertEqual(app.acting, self.B["sessionId"])
            finally:
                for gate in adapter.gates.values():
                    gate.set()
            await pilot.pause(0.1)
            await pilot.pause()
            self.assertEqual(sorted(self.marks),
                             sorted([self.A["sessionId"], self.B["sessionId"]]),
                             "both were looked at, each once")
            self.assertEqual([a for sid, a, _ in self.state(app)
                              if sid != BUSY["sessionId"]], ["stopped", "stopped"])

    async def test_a_kill_waits_for_the_jump(self):
        # a kill moves rows, and nothing moves while a window is being opened:
        # it is refused, and says why, rather than half done
        adapter = GatedAdapter()
        collector = FakeCollector(fleet=ui.Fleet([self.A, STUCK], True, "12:00:00"))
        app = self.app(adapter=adapter, collector=collector)
        async with app.run_test(size=(120, 30)) as pilot:
            try:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause(0.1)
                before = self.state(app)
                await pilot.press("j", "x", "l", "y")
                await pilot.pause(0.3)
                self.assertIsNone(collector.killed)
                self.assertIn("window is being opened",
                              str(app.screen.query_one("#killbody").content))
                self.assertEqual(app.acting, self.A["sessionId"])
                self.assertEqual([s[:2] for s in self.state(app)],
                                 [s[:2] for s in before])
            finally:
                adapter.gate.set()
            await pilot.pause(0.1)
            await pilot.pause()
            self.assertEqual(self.marks, [self.A["sessionId"]])
            await pilot.press("escape")             # the refusal read, the box closed
            await pilot.pause(0.1)
            await pilot.press("x")                  # and after it, the usual box:
            await pilot.pause(0.1)
            self.assertIsNone(collector.killed, "the refused kill is not kept for later")
            self.assertIsInstance(app.screen, ui.ChoiceBox)
            await pilot.press("l", "y")
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            self.assertEqual(collector.killed, [STUCK["sessionId"]])

    async def test_enter_in_the_kill_box_neither_jumps_nor_kills(self):
        # the box asks about the kill: Enter there is no answer, and no jump
        adapter = FakeAdapter()
        collector = FakeCollector(fleet=ui.Fleet([self.A, STUCK], True, "12:00:00"))
        app = self.app(adapter=adapter, collector=collector)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x", "enter")
            await pilot.pause(0.3)
            self.assertIsNone(collector.killed)
            self.assertEqual(adapter.asked, [])
            self.assertIsInstance(app.screen, ui.ChoiceBox)

    async def test_a_turn_that_came_during_the_jump_is_not_marked_seen(self):
        adapter = GatedAdapter()
        collector = FakeCollector(fleet=ui.Fleet([self.A, BUSY], True, "12:00:00"))
        app = self.app(adapter=adapter, collector=collector)
        async with app.run_test() as pilot:
            try:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause(0.1)
                collector.fleet_value = ui.Fleet(
                    [dict(self.A, ts="2026-09-22T11:00:00.000Z"), BUSY], True, "12:00:05")
                app.collect()
                await pilot.pause(0.1)
            finally:
                adapter.gate.set()
            await pilot.pause(0.1)
            await pilot.pause()
            self.assertEqual(self.marks, [], "you looked at the turn before it")
            self.assertEqual(self.state(app)[0][:2], (self.A["sessionId"], "review"))

    async def test_a_scan_that_was_under_way_does_not_bring_it_back(self):
        # it read the marks before the jump landed, and arrives after
        app = self.app(collector=FakeCollector(
            fleet=ui.Fleet([self.A, BUSY], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.1)
            await pilot.pause()
            late = ui.Fleet([reviewable(self.A["sessionId"]), BUSY], True, "12:00:09")
            late.seq = app.started + 1
            app.show(late)
            await pilot.pause()
            self.assertEqual(self.state(app)[0][:2], (self.A["sessionId"], "stopped"))

    async def late_scan_held_by_the_next_jump(self, ts):
        import threading
        adapter = GatedAdapter()
        adapter.gates = {self.B["sessionId"]: threading.Event()}
        adapter.gate.set()                                   # A lands at once
        app = self.app(adapter=adapter, collector=FakeCollector(
            fleet=ui.Fleet([self.A, self.B, BUSY], True, "12:00:00")))
        async with app.run_test() as pilot:
            try:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause(0.1)
                await pilot.pause()
                # A has moved to STOPPED: B is picked by name, with a click
                await pilot.click([w for w in app.query(ui.Row)
                                   if w.row["sessionId"] == self.B["sessionId"]][0])
                await pilot.pause(0.1)
                self.assertEqual(app.acting, self.B["sessionId"], "B: still opening")
                late = ui.Fleet([reviewable(self.A["sessionId"]) | {"ts": ts},
                                 reviewable(self.B["sessionId"]), BUSY], True, "12:00:09")
                late.seq = app.started + 1
                app.show(late)                             # held
                await pilot.pause()
            finally:
                adapter.gates[self.B["sessionId"]].set()
            await pilot.pause(0.1)
            await pilot.pause()
            return dict((sid, a) for sid, a, _ in self.state(app))[self.A["sessionId"]]

    async def test_a_late_scan_held_by_the_next_jump_does_not_bring_it_back(self):
        self.assertEqual(await self.late_scan_held_by_the_next_jump(self.A["ts"]),
                         "stopped")

    async def test_a_new_turn_held_by_the_next_jump_does_come_back(self):  # control
        self.assertEqual(await self.late_scan_held_by_the_next_jump(
            "2026-09-22T12:00:00.000Z"), "review")

    async def test_a_new_turn_in_a_late_scan_does_come_back(self):      # control
        app = self.app(collector=FakeCollector(
            fleet=ui.Fleet([self.A, BUSY], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.1)
            await pilot.pause()
            late = ui.Fleet([reviewable(self.A["sessionId"]) | {"ts": "2026-09-22T12:00:00.000Z"},
                             BUSY], True, "12:00:09")
            late.seq = app.started + 1
            app.show(late)
            await pilot.pause()
            self.assertEqual(self.state(app)[0][:2], (self.A["sessionId"], "review"))

    async def test_enter_on_a_row_with_no_window_does_not_freeze_the_list(self):
        adapter = GatedAdapter()
        nowhere = row("cafe0000-0000-4000-8000-00000000000c", "stopped",
                      windowed=False, tab_title="nowhere", title="nowhere")
        collector = FakeCollector(fleet=ui.Fleet([self.A, nowhere], True, "12:00:00"))
        app = self.app(adapter=adapter, collector=collector)
        async with app.run_test() as pilot:
            try:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause(0.1)
                await pilot.press("j", "enter")         # nothing to go to: no jump
                await pilot.pause(0.1)
                collector.fleet_value = ui.Fleet([self.A, nowhere], True, "12:00:07")
                app.collect()
                await pilot.pause(0.1)
            finally:
                adapter.gate.set()
            await pilot.pause(0.1)
            await pilot.pause()
            self.assertEqual(app.acting, "")
            self.assertIsNone(app.held)
            self.assertIn("12:00:07", self.screen_text(app))
            self.assertEqual(self.marks, [self.A["sessionId"]])

    async def test_an_older_jump_landing_last_is_applied_at_once(self):
        import threading
        adapter = GatedAdapter()
        adapter.gates = {self.A["sessionId"]: threading.Event(),
                         self.B["sessionId"]: threading.Event()}
        app = self.app(adapter=adapter, collector=FakeCollector(
            fleet=ui.Fleet([self.A, self.B, BUSY], True, "12:00:00")))
        async with app.run_test() as pilot:
            try:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause(0.1)
                await pilot.press("j", "enter")
                await pilot.pause(0.1)
                adapter.gates[self.B["sessionId"]].set()     # B first
                await pilot.pause(0.1)
                await pilot.pause()
                self.assertEqual(app.acting, "")
                adapter.gates[self.A["sessionId"]].set()     # then A, late
                await pilot.pause(0.1)
                await pilot.pause()
            finally:
                for gate in adapter.gates.values():
                    gate.set()
            self.assertEqual(sorted(self.marks),
                             sorted([self.A["sessionId"], self.B["sessionId"]]))
            self.assertEqual(app.reviewing, {})
            self.assertIn("bbbb", app.status, "B is the window in front")

    async def test_a_jump_that_failed_does_not_count_as_looking(self):
        app = self.app(adapter=FakeAdapter(answer="iTerm2 did not answer in 5s"),
                       collector=FakeCollector(fleet=ui.Fleet([self.A, BUSY], True,
                                                              "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.1)
            await pilot.pause()
            self.assertEqual(self.marks, [])
            self.assertEqual(self.state(app)[0][:2], (self.A["sessionId"], "review"))
            self.assertEqual(app.acting, "", "but it is over: the blink stops")

    async def test_a_jump_that_landed_does(self):                        # control
        app = self.app(collector=FakeCollector(
            fleet=ui.Fleet([self.A, BUSY], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.1)
            await pilot.pause()
            self.assertEqual(self.marks, [self.A["sessionId"]])


class TestTheDetailClosesWithTheMouse(UiTest):
    """Reported: "There's also no clear way to close the detail with the mouse.
    Maybe an X at the top right?" """

    async def test_the_detail_has_a_close_at_its_top_right(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            close, detail = app.query_one("#close"), app.query_one("#detail")
            self.assertTrue(close.display)
            self.assertIn("✕", str(close.content))
            self.assertEqual(close.region.y, detail.content_region.y, "at the top")
            bar = app.query_one("#closebar")
            self.assertEqual(close.region.right, bar.region.right, "at the right")
            self.assertGreater(close.region.x, detail.content_region.x
                               + detail.content_region.width // 2, "not the left")

    async def test_clicking_it_closes_the_detail_and_goes_nowhere(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            close = app.query_one("#close")
            await pilot.click(close, offset=(close.size.width - 2, 0))
            await pilot.pause()
            self.assertFalse(app.detail_open)
            self.assertFalse(app.query_one("#detail").display)
            self.assertEqual(adapter.asked, [])

    async def test_it_closes_the_process_screen_too(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            close = app.query_one("#close")
            await pilot.click(close, offset=(close.size.width - 2, 0))
            await pilot.pause()
            self.assertFalse(app.detail_open)

    async def test_it_stays_at_the_top_of_a_long_detail(self):
        app = self.app(collector=FakeCollector(brief=dict(
            FakeCollector().brief_value, progress=[f"step {i}" for i in range(80)])))
        async with app.run_test(size=(160, 20)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            detail = app.query_one("#detail")
            detail.scroll_end(animate=False)
            await pilot.pause()
            self.assertGreater(detail.scroll_y, 0, "the test needs a detail that scrolls")
            self.assertEqual(app.query_one("#close").region.y, detail.content_region.y)

    async def test_a_narrow_detail_has_it_too(self):
        app = self.app()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            close = app.query_one("#close")
            await pilot.click(close, offset=(close.size.width - 2, 0))
            await pilot.pause()
            self.assertFalse(app.detail_open)
            self.assertTrue(app.query_one("#list").display, "the list is back")


class TestClickableThingsLightUp(UiTest):
    """"Are you able to do hover effects on clickable things?" The parts of a
    row that do something of their own light up under the mouse; the rest of
    the row does not, because a click there does what it always does."""

    def roles_lit(self, widget):
        text = widget.content
        lit = set()
        for span in text.spans:
            if "reverse" in str(span.style):
                lit.add(text.plain[span.start:span.end])
        return lit

    async def test_the_arrow_lights_up_both_halves(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            w = list(app.query(ui.Row))[1]
            await pilot.hover(w, offset=TestTheDetailWithTheMouse.detail_offset(None, w))
            await pilot.pause()
            self.assertEqual(self.roles_lit(w), set(ui.engine.UI_DETAIL))

    async def test_the_kill_lights_up(self):
        app = self.app(collector=FakeCollector(fleet=ui.Fleet([LIVE, STUCK], True, "12:00:00")))
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            w = [w for w in app.query(ui.Row) if w.row["sessionId"] == STUCK["sessionId"]][0]
            await pilot.hover(w, offset=TestKillingAStuckLoop.kill_offset(None, w))
            await pilot.pause()
            self.assertEqual(self.roles_lit(w), {ui.engine.UI_KILL})

    async def test_the_light_goes_when_the_mouse_does(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            rows = list(app.query(ui.Row))
            await pilot.hover(rows[1], offset=TestTheDetailWithTheMouse.detail_offset(None, rows[1]))
            await pilot.pause()
            await pilot.hover(rows[0], offset=(10, 0))
            await pilot.pause()
            self.assertEqual(self.roles_lit(rows[1]), set(), "left the row")
            self.assertEqual(self.roles_lit(rows[0]), set(), "the name is not a control")

    async def test_a_row_that_changes_under_a_still_mouse_is_lit_where_it_is(self):
        # the loop's words got shorter: the kill moved left, the mouse did not -
        # further than the button is wide, or the mouse is still on it
        long = dict(STUCK, dead_loops=[{"pid": 86246, "tasks": ["bscl8fc6k", "b2", "b3", "b4"]},
                                       {"pid": 1, "tasks": ["x"]}])
        collector = FakeCollector(fleet=ui.Fleet([LIVE, long], True, "12:00:00"))
        app = self.app(collector=collector)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            w = [w for w in app.query(ui.Row) if w.row["sessionId"] == STUCK["sessionId"]][0]
            at = TestKillingAStuckLoop.kill_offset(None, w)
            await pilot.hover(w, offset=at)
            await pilot.pause()
            self.assertEqual(self.roles_lit(w), {ui.engine.UI_KILL})
            collector.fleet_value = ui.Fleet([LIVE, STUCK], True, "12:00:05")
            app.collect()
            await pilot.pause(0.2)
            w = [w for w in app.query(ui.Row) if w.row["sessionId"] == STUCK["sessionId"]][0]
            self.assertIsNotNone(w.mouse_at, "the same row, and the mouse is still on it")
            under = w.action_at(*w.mouse_at)
            self.assertNotEqual(under, "kill", "the test needs the kill to have moved")
            self.assertEqual(w.lit, under)
            self.assertEqual(self.roles_lit(w),
                             {ui.engine.UI_KILL} if under == "kill" else set())

    async def test_the_close_lights_up(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            close = app.query_one("#close")
            before = close.styles.background
            await pilot.hover(close, offset=(close.size.width - 2, 0))
            await pilot.pause()
            self.assertNotEqual(close.styles.background, before)


class TestTheDetailWithTheMouse(UiTest):
    """Reported: "there's no good way to get the detail screen with the mouse".
    A click goes to the session; the arrow at the right edge opens its detail
    instead - either half of it. (Right click and ctrl-click do too, in a
    terminal that passes them on: iTerm2 keeps both for its own menu.)"""

    def detail_offset(self, widget, line=0):
        cells = widget.spans_lines()[line]
        col = sum(ui.engine._cells(t) for t, r in cells if r != "detail")
        # the row's left border and padding come before its text; one column in
        return (col + 2 + 1, line)

    async def test_the_bottom_half_of_the_arrow_opens_it_too(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            w = list(app.query(ui.Row))[1]
            await pilot.click(w, offset=self.detail_offset(w, line=1))
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(adapter.asked, [])
            self.assertTrue(app.detail_open)
            self.assertEqual(app.selected, w.row["sessionId"])

    async def test_the_arrow_opens_that_rows_detail_and_does_not_go(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            w = list(app.query(ui.Row))[1]
            await pilot.click(w, offset=self.detail_offset(w))
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(adapter.asked, [])
            self.assertTrue(app.detail_open)
            self.assertEqual(app.detail_mode, "brief")
            self.assertEqual(app.selected, w.row["sessionId"])

    async def test_a_right_click_opens_the_detail_and_does_not_go(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            w = list(app.query(ui.Row))[1]
            await pilot.click(w, button=3)
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(adapter.asked, [])
            self.assertTrue(app.detail_open)
            self.assertEqual(app.selected, w.row["sessionId"])

    async def test_a_ctrl_click_is_a_right_click(self):
        # the Mac's own right click; the terminal reports it as button 1 + ctrl
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            w = list(app.query(ui.Row))[1]
            await pilot.click(w, control=True)
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(adapter.asked, [])
            self.assertTrue(app.detail_open)

    async def test_a_right_click_on_the_kill_does_not_arm_it(self):
        collector = FakeCollector(fleet=ui.Fleet([LIVE, STUCK], True, "12:00:00"))
        app = self.app(collector=collector)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            w = [w for w in app.query(ui.Row) if w.row["sessionId"] == STUCK["sessionId"]][0]
            await pilot.click(w, offset=TestKillingAStuckLoop.kill_offset(None, w),
                              button=3)
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ui.ChoiceBox)
            self.assertTrue(app.detail_open)

    async def test_it_works_from_the_process_screen_too(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            w = list(app.query(ui.Row))[1]
            await pilot.click(w, offset=self.detail_offset(w))
            await pilot.pause()
            self.assertEqual(app.detail_mode, "brief")

    async def test_a_plain_click_still_goes(self):                      # control
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.pause()
            w = list(app.query(ui.Row))[1]
            await pilot.click(w, offset=(10, 0))
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(adapter.asked, [w.row["sessionId"]])
            self.assertFalse(app.detail_open)


class TestTheTwoTiersLookDifferent(UiTest):
    async def test_a_finished_session_is_drawn_more_quietly(self):
        app = self.app(collector=FakeCollector(
            fleet=ui.Fleet([LIVE, reviewable()], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            marks = {w.row["attention"]: [t for t, r in w.spans() if r == "mark"][0]
                     for w in app.query(ui.Row)}
            self.assertNotEqual(marks.get("asks"), marks.get("review"))

    def test_the_style_table_knows_the_new_tier(self):
        import ccwho_engine as engine
        self.assertIn(engine.UI_STATE_STYLE["review"], ui.STATE_STYLE)
        self.assertNotEqual(ui.STATE_STYLE[engine.UI_STATE_STYLE["review"]],
                            ui.STATE_STYLE[engine.UI_STATE_STYLE["blocked"]])


class TestReorderingIsNotRebuilding(UiTest):
    """Rows sort newest-first inside their group, so every session that writes
    jumps to the top of its group. Measured: nine reorderings a minute on a
    live fleet - and each one tore the list down and built it again, which is
    the flashing that came back.

    Moving a row is not the same as replacing it.
    """

    def two(self, first, second):
        return FakeCollector(fleet=ui.Fleet([first, second], True, "12:00:00"))

    async def test_the_same_rows_in_a_new_order_are_moved_not_remade(self):
        collector = self.two(LIVE, BUSY)
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = {w.row["sessionId"]: id(w) for w in app.query(ui.Row)}
            # same two sessions, swapped: BUSY is now the one that just spoke
            collector.fleet_value = ui.Fleet([dict(BUSY, attention="asks"),
                                              dict(LIVE, attention="asks")],
                                             True, "12:00:01")
            app.collect()
            await pilot.pause()
            await pilot.pause()
            after = {w.row["sessionId"]: id(w) for w in app.query(ui.Row)}
            self.assertEqual(before, after, "the same widgets, moved")

    async def test_and_the_order_on_screen_actually_changes(self):
        collector = self.two(LIVE, BUSY)
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            first = [w.row["sessionId"] for w in app.query(ui.Row)]
            collector.fleet_value = ui.Fleet([dict(BUSY, attention="asks"),
                                              dict(LIVE, attention="asks")],
                                             True, "12:00:01")
            app.collect()
            await pilot.pause()
            await pilot.pause()
            self.assertEqual([w.row["sessionId"] for w in app.query(ui.Row)],
                             list(reversed(first)))

    async def test_a_row_moving_between_groups_is_still_a_move(self):
        collector = self.two(LIVE, BUSY)
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = {w.row["sessionId"]: id(w) for w in app.query(ui.Row)}
            collector.fleet_value = ui.Fleet([dict(LIVE, attention="stopped"),
                                              BUSY], True, "12:00:01")
            app.collect()
            await pilot.pause()
            await pilot.pause()
            self.assertEqual({w.row["sessionId"]: id(w) for w in app.query(ui.Row)},
                             before)
            self.assertIn("STOPPED", self.screen_text(app))

    async def test_a_session_appearing_still_rebuilds(self):          # control
        collector = self.two(LIVE, BUSY)
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            before = len(list(app.query(ui.Row)))
            collector.fleet_value = ui.Fleet(
                [LIVE, BUSY, row("ffff6666-0000-4000-8000-000000000006")],
                True, "12:00:01")
            app.collect()
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(len(list(app.query(ui.Row))), before + 1)

    async def test_the_selection_survives_a_move(self):
        collector = self.two(LIVE, BUSY)
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.selected = BUSY["sessionId"]
            app.mark_selected()
            collector.fleet_value = ui.Fleet([dict(BUSY, attention="asks"),
                                              dict(LIVE, attention="asks")],
                                             True, "12:00:01")
            app.collect()
            await pilot.pause()
            await pilot.pause()
            picked = [w.row["sessionId"] for w in app.query(ui.Row)
                      if w.has_class("selected")]
            self.assertEqual(picked, [BUSY["sessionId"]])


class TestYouCanTellWhenYouAreSearching(UiTest):
    """"When in search mode, it's hard to tell, so if you forget, it looks
    broken." A list that is hiding most of itself has to say so where you are
    already looking - the header - not only in a box at the bottom."""

    async def test_the_header_says_what_is_being_hidden(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.filter_text = "Issue 362"
            app.rebuild()
            await pilot.pause()
            header = str(app.query_one("#header").content)
            self.assertIn("Issue 362", header)
            self.assertIn("1 of 2", header, "how much you cannot see")
            self.assertIn("esc", header.lower(), "and how to get it back")

    async def test_a_search_that_finds_nothing_still_says_it(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.filter_text = "nothing matches this"
            app.rebuild()
            await pilot.pause()
            header = str(app.query_one("#header").content)
            self.assertIn("0 of 2", header)

    async def test_no_search_says_nothing_about_searching(self):      # control
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            header = str(app.query_one("#header").content)
            self.assertNotIn("esc", header.lower())
            self.assertNotIn(" of ", header)

    async def test_clearing_it_takes_the_notice_away(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            app.query_one("#search").value = "Issue 362"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIn("esc to clear",
                             str(app.query_one("#header").content).lower())


class TestOOffersTheSaves(UiTest):
    """o opens a menu of every save, newest first and highlighted. It used to
    work only on an empty list - so after a restart, with the loops started
    first, o did nothing at all. restore() skips every session that is still
    running, so o is always there."""

    def setUp(self):
        self.restored = []
        self.asked = []
        self.real = (FakeCollector.restore, FakeCollector.save_points)
        FakeCollector.restore = lambda s, path=None, report=None: (
            self.restored.append(path) or "reopened 11 session(s)")
        FakeCollector.save_points = lambda s, live_ids=(): (
            self.asked.append(set(live_ids)) or [dict(p) for p in self.points])
        self.addCleanup(self.put_back)
        self.points = POINTS

    def put_back(self):
        FakeCollector.restore, FakeCollector.save_points = self.real

    def empty(self):
        return FakeCollector(fleet=ui.Fleet([], True, "12:00:00"))

    async def opened(self, pilot):
        await pilot.pause()
        await pilot.press("o")
        for _ in range(4):
            await pilot.pause()

    def menu_text(self, app):
        box = app.screen.query_one("#saves")
        return "\n".join(str(box.get_option_at_index(i).prompt)
                         for i in range(box.option_count))

    async def test_the_head_says_what_a_restore_leaves(self):
        # a save of two Claude Desktop sessions reads "2 sessions · 0 to
        # reopen": the screen says why (review 4 of env-panes)
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            head = str(app.screen.query_one("#saveshead").content)
            self.assertIn("Sessions still running, and those Claude Desktop or a program ran,"
                          " are left alone.", head)

    async def test_o_opens_the_menu_while_sessions_are_running(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            self.assertIsInstance(app.screen, ui.SavesMenu)

    async def test_o_opens_the_menu_on_an_empty_list(self):
        app = self.app(collector=self.empty())
        async with app.run_test() as pilot:
            await self.opened(pilot)
            self.assertIsInstance(app.screen, ui.SavesMenu)

    async def test_the_menu_asks_about_the_sessions_running_now(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            self.assertEqual(self.asked, [{LIVE["sessionId"], BUSY["sessionId"]}])

    async def test_a_terminal_that_parked_a_job_is_running_too(self):
        job = row("4e3efc1d-3639-4af3-91e9-6d6373c1cf94", parked=["fc509261-e383-4ed4-aacc-44087dc5a599"])
        app = self.app(collector=FakeCollector(fleet=ui.Fleet([job], True, "12:00:00")))
        async with app.run_test() as pilot:
            await self.opened(pilot)
            self.assertEqual(self.asked, [{job["sessionId"], job["parked"][0]}])

    async def test_each_line_says_when_how_many_and_the_restart(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            text = self.menu_text(app)
            self.assertIn("12 sessions", text)
            self.assertIn("1 running, 11 to reopen", text)
            self.assertIn("last save before the restart", text)
            self.assertEqual(len(text.splitlines()), 2)

    async def test_the_newest_is_highlighted(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            self.assertEqual(app.screen.query_one("#saves").highlighted, 0)

    async def test_enter_reopens_the_newest(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            await pilot.press("enter")
            for _ in range(4):
                await pilot.pause()
            self.assertEqual(self.restored, [POINTS[0]["path"]])
            self.assertNotIsInstance(app.screen, ui.SavesMenu)

    async def test_down_then_enter_reopens_the_older_one(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            await pilot.press("down", "enter")
            for _ in range(4):
                await pilot.pause()
            self.assertEqual(self.restored, [POINTS[1]["path"]])

    async def test_a_click_on_a_line_reopens_that_one(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            box = app.screen.query_one("#saves")
            # the SECOND line, not the highlighted one: a click names its line
            await pilot.click(box, offset=(3, 1))      # no border: y=1 is line 2
            for _ in range(4):
                await pilot.pause()
            self.assertEqual(self.restored, [POINTS[1]["path"]])

    async def test_j_and_k_move_the_menu_not_the_list_behind_it(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            before = app.selected
            await self.opened(pilot)
            await pilot.press("j")
            await pilot.pause()
            self.assertEqual(app.screen.query_one("#saves").highlighted, 1)
            self.assertEqual(app.selected, before)
            await pilot.press("k")
            await pilot.pause()
            self.assertEqual(app.screen.query_one("#saves").highlighted, 0)

    async def test_the_list_keys_do_nothing_behind_the_menu(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            await pilot.press("p", "o", "slash")
            for _ in range(4):
                await pilot.pause()
            self.assertFalse(app.detail_open, "p opened the process pane behind it")
            self.assertEqual(len(app.screen_stack), 2, "o stacked a second menu")
            self.assertFalse(app.query_one("#search").display)

    async def test_two_quick_presses_open_one_menu(self):
        slow = FakeCollector.save_points

        def slowly(s, live_ids=()):
            import time
            time.sleep(0.4)
            return slow(s, live_ids)
        FakeCollector.save_points = slowly
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("o", "o")
            for _ in range(10):
                await pilot.pause(0.1)
            self.assertEqual(len(app.screen_stack), 2)
            self.assertEqual(len(self.asked), 1)

    async def test_the_close_mark_closes_it_with_a_click(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            await pilot.click("#savesclose")
            for _ in range(4):
                await pilot.pause()
            self.assertNotIsInstance(app.screen, ui.SavesMenu)
            self.assertEqual(self.restored, [])

    async def test_a_click_outside_the_box_closes_it(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            await pilot.click(offset=(0, 0))
            for _ in range(4):
                await pilot.pause()
            self.assertNotIsInstance(app.screen, ui.SavesMenu)
            self.assertEqual(self.restored, [])

    async def test_a_click_inside_the_box_keeps_it(self):                 # control
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            await pilot.click("#saveshead")
            for _ in range(4):
                await pilot.pause()
            self.assertIsInstance(app.screen, ui.SavesMenu)

    async def test_a_failed_read_says_so_and_o_still_works(self):
        calls = []

        def broken(s, live_ids=()):
            calls.append(1)
            raise OSError("disk says no")
        FakeCollector.save_points = broken
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            header = str(app.query_one("#header").content)
            self.assertIn("could not read the saves", header)
            self.assertNotIn("nothing saved", header)
            self.assertFalse(app.loading)
            await self.opened(pilot)
            self.assertEqual(len(calls), 2, "o must not go dead after a failed read")

    async def test_escape_closes_it_and_reopens_nothing(self):           # control
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            await pilot.press("escape")
            for _ in range(4):
                await pilot.pause()
            self.assertEqual(self.restored, [])
            self.assertNotIsInstance(app.screen, ui.SavesMenu)

    async def test_it_says_what_happened(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            await pilot.press("enter")
            for _ in range(4):
                await pilot.pause()
            self.assertIn("reopened", str(app.query_one("#header").content))

    async def test_nothing_saved_says_so_and_opens_no_menu(self):
        self.points = []
        app = self.app()
        async with app.run_test() as pilot:
            await self.opened(pilot)
            self.assertNotIsInstance(app.screen, ui.SavesMenu)
            self.assertIn("ccwho save", str(app.query_one("#header").content))

    async def test_o_is_named_in_the_footer(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            shown = [b.key for b in app.BINDINGS if b.show]
            self.assertIn("o", shown)

    async def test_the_empty_list_offer_names_how_many(self):
        real = FakeCollector.saved_count
        FakeCollector.saved_count = lambda s: 14
        try:
            app = self.app(collector=self.empty())
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertIn("14", self.screen_text(app))
                self.assertIn("o = choose a save to reopen", self.screen_text(app))
        finally:
            FakeCollector.saved_count = real

    async def test_no_save_no_offer(self):                                 # control
        real = FakeCollector.saved_count
        FakeCollector.saved_count = lambda s: 0
        try:
            app = self.app(collector=self.empty())
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertNotIn(" o ", self.screen_text(app))
        finally:
            FakeCollector.saved_count = real


class TestAReopenThatMissedItsPanesStaysOnScreen(UiTest):
    """`o` that sent an iTerm2 session to a new window - it did not go back
    into the pane iTerm2 restored - says so on the warning line until a key is
    pressed. The header's status is gone at the next refresh, seconds after the
    reopened sessions start, and a narrow window cuts it (2026-09-30).
    Terminal.app's sessions always get new windows: they are no miss."""

    MISSED = "reopened 11: 10 in their panes, 1 new window"
    LONG = "reopened 11: all in new windows, 3 left out - they could not resume"

    def answer(self, text, iterm_windows):
        def restore(s, path=None, report=None):
            if report is not None:
                report["iterm_windows"] = iterm_windows
            return text
        real = FakeCollector.restore
        FakeCollector.restore = restore
        self.addCleanup(setattr, FakeCollector, "restore", real)

    async def reopen(self, pilot):
        await pilot.pause()
        await pilot.press("o")
        for _ in range(4):
            await pilot.pause()
        await pilot.press("enter")
        for _ in range(6):
            await pilot.pause()

    def banner(self, app):
        banner = app.query_one("#banner")
        return str(banner.render()) if banner.display else ""

    async def test_an_iterm2_new_window_puts_it_on_the_warning_line(self):
        self.answer(self.MISSED, 1)
        app = self.app()
        async with app.run_test() as pilot:
            await self.reopen(pilot)
            self.assertIn(self.MISSED, self.banner(app))

    async def test_it_stays_through_a_refresh(self):
        self.answer(self.MISSED, 1)
        app = self.app()
        async with app.run_test() as pilot:
            await self.reopen(pilot)
            app.show(ui.Fleet([LIVE, BUSY], True, "12:00:05"))
            await pilot.pause()
            self.assertIn(self.MISSED, self.banner(app))

    async def test_a_key_press_clears_it(self):
        self.answer(self.MISSED, 1)
        app = self.app()
        async with app.run_test() as pilot:
            await self.reopen(pilot)
            self.assertIn(self.MISSED, self.banner(app))        # shown, then gone
            await pilot.press("down")
            await pilot.pause()
            self.assertNotIn(self.MISSED, self.banner(app))

    async def test_the_key_that_clears_it_still_does_its_job(self):
        self.answer(self.MISSED, 1)
        app = self.app()
        async with app.run_test() as pilot:
            await self.reopen(pilot)
            before = app.selected
            await pilot.press("down")
            await pilot.pause()
            self.assertNotIn(self.MISSED, self.banner(app))
            self.assertNotEqual(app.selected, before, "down must still move the list")

    async def test_a_key_s_second_pass_does_not_clear_it(self):
        # Textual gives the App a key twice: from the terminal, then forwarded
        # back up from the widget. The result may land between the two
        # (reviews 1 and 2 of carry-panes)
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            key = ui.events.Key("down", None)
            await app.on_event(key)                         # its first pass: no note yet
            app.reopened(self.MISSED, 1)                    # the result lands between
            await app.on_event(key)                         # its pass back up, forwarded
            self.assertIn(self.MISSED, self.banner(app))

    async def test_a_key_pressed_before_it_arrived_does_not_clear_it(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            key = ui.events.Key("down", None)               # pressed, not yet handled
            app.reopened(self.MISSED, 1)
            await app.on_event(key)
            self.assertIn(self.MISSED, self.banner(app))

    async def test_a_note_that_lands_under_a_menu_is_seen_after_it(self):
        # the keys pressed in a menu are the menu's: the note is under it, not
        # seen yet (review 3)
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("o")
            for _ in range(4):
                await pilot.pause()
            self.assertIsInstance(app.screen, ui.SavesMenu)
            app.reopened(self.MISSED, 1)                    # an earlier reopen lands now
            await pilot.press("down", "escape")
            for _ in range(3):
                await pilot.pause()
            self.assertNotIsInstance(app.screen, ui.SavesMenu)
            self.assertIn(self.MISSED, self.banner(app))
            await pilot.press("down")                       # the first key on the list
            await pilot.pause()
            self.assertNotIn(self.MISSED, self.banner(app))

    async def test_a_narrow_window_shows_all_of_it(self):
        self.answer(self.LONG, 2)
        app = self.app()
        async with app.run_test(size=(60, 24)) as pilot:
            await self.reopen(pilot)
            self.assertIn(self.LONG, self.banner(app))
            self.assertGreaterEqual(app.query_one("#banner").size.height, 2,
                                    "the warning line wraps; it is not cut")

    async def test_it_does_not_hide_a_fleet_error(self):
        self.answer(self.MISSED, 1)
        fleet = ui.Fleet([LIVE], False, "12:00:00", "could not read the fleet: boom")
        app = self.app(collector=FakeCollector(fleet=fleet))
        async with app.run_test() as pilot:
            await self.reopen(pilot)
            self.assertIn("boom", self.banner(app))
            self.assertIn(self.MISSED, self.banner(app))

    async def test_only_terminal_app_windows_use_the_header(self):          # control
        self.answer("reopened 3: all in new windows", 0)
        app = self.app()
        async with app.run_test() as pilot:
            await self.reopen(pilot)
            self.assertEqual(self.banner(app), "")
            self.assertIn("reopened 3: all in new windows",
                          str(app.query_one("#header").content))


class TestAHiddenSearchBoxCannotEatYourKeys(UiTest):
    """With no rows to focus, focus fell to the search Input - which is not on
    screen. Every key then went into an invisible filter: pressing o typed "o"
    and the list, already empty, stayed empty for a new reason."""

    def empty(self):
        return FakeCollector(fleet=ui.Fleet([], True, "12:00:00"))

    async def test_an_empty_list_does_not_put_focus_in_the_box(self):
        app = self.app(collector=self.empty())
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertNotIsInstance(app.focused, Input,
                                     "a box nobody can see cannot hold focus")

    async def test_keys_reach_their_bindings_with_an_empty_list(self):
        app = self.app(collector=self.empty())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            self.assertEqual(app.filter_text, "",
                             "o is a key, not a letter to type")

    async def test_the_box_still_takes_keys_when_you_open_it(self):   # control
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("q")
            await pilot.pause()
            self.assertEqual(app.query_one("#search").value, "q")


class TestItNeverOffersAJumpItCannotMake(UiTest):
    """Clicking a session did nothing: it runs as `claude bg-spare`, on a tty
    iTerm2 has never heard of, so there was no window to raise. ccwho already
    knew - it had asked iTerm2 for that tty and got nothing - and said neither
    that nor "not found: /dev/ttys042", which is what it did say."""

    def homeless(self):
        return row("9999aaaa-0000-4000-8000-000000009999", "busy",
                   tab_title="", tty="ttys042", windowed=False)

    async def test_enter_says_there_is_no_window(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter, collector=FakeCollector(
            fleet=ui.Fleet([self.homeless()], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            header = str(app.query_one("#header").content).lower()
            self.assertIn("no window", header)
            self.assertEqual(adapter.asked, [],
                             "and it does not ask iTerm2 to do the impossible")

    async def test_it_says_how_to_reach_it_instead(self):
        app = self.app(collector=FakeCollector(
            fleet=ui.Fleet([self.homeless()], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertIn("resume", str(app.query_one("#header").content).lower())

    async def test_a_session_with_a_window_still_jumps(self):         # control
        adapter = FakeAdapter()
        app = self.app(adapter=adapter, collector=FakeCollector(
            fleet=ui.Fleet([dict(LIVE, windowed=True)], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(adapter.asked, [LIVE["sessionId"]])

    async def test_not_knowing_still_tries(self):                     # control
        # iTerm2 shut or unscriptable: try anyway and report what comes back
        adapter = FakeAdapter()
        app = self.app(adapter=adapter, collector=FakeCollector(
            fleet=ui.Fleet([dict(LIVE, windowed=None)], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(adapter.asked, [LIVE["sessionId"]])


class TestEnterOnABackgroundSessionGivesItAWindow(UiTest):
    """The third kind: running, no window, and you want one. Enter opens a
    terminal and attaches - it never resumes, which would fork a conversation
    that is running."""

    def bg(self):
        return row("51fddd61-822b-49e0-9aeb-2145e91e1244", "busy",
                   tab_title="", tty="", windowed=False, kind="background")

    def fleet(self):
        return FakeCollector(fleet=ui.Fleet([self.bg()], True, "12:00:00"))

    async def test_enter_attaches_it(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter, collector=self.fleet())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            # the short id: given the full one, `claude attach` says
            # "No job matching" about this very session
            self.assertEqual(adapter.attached, ["claude attach 51fddd61"])
            self.assertEqual(adapter.asked, [], "there is no window to focus yet")
            # its row goes along: the app it was in is looked up by its id (D6)
            self.assertEqual([r and r.get("sessionId") for r in adapter.attached_rows],
                             ["51fddd61-822b-49e0-9aeb-2145e91e1244"])

    async def test_it_says_so(self):
        app = self.app(collector=self.fleet())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertIn("attach", str(app.query_one("#header").content).lower())

    async def test_a_windowed_session_still_jumps(self):              # control
        adapter = FakeAdapter()
        app = self.app(adapter=adapter, collector=FakeCollector(
            fleet=ui.Fleet([dict(LIVE, windowed=True)], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(adapter.asked, [LIVE["sessionId"]])
            self.assertEqual(adapter.attached, [])

    def test_the_adapter_builds_one_window_running_that_command(self):
        ran = []
        adapter = ui.Adapter()
        real = ui.subprocess.run
        ui.subprocess.run = lambda cmd, **k: ran.append(cmd) or type(
            "D", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
        try:
            adapter.attach("claude attach abc")
        finally:
            ui.subprocess.run = real
        script = " ".join(" ".join(c) for c in ran)
        self.assertIn("create window", script)
        self.assertIn("claude attach abc", script)


class TestYouCanSeeWhichRowIsBeingOpened(UiTest):
    """Pressing Enter sends the work to another program - iTerm2, or a new
    window running `claude attach` - and that can take a moment. The list said
    so in one word at the end of the header, which is not where you are
    looking: you are looking at the row you just acted on."""

    async def test_the_row_is_marked_while_it_is_being_opened(self):
        adapter = FakeAdapter(hang=True)          # iTerm2 taking its time
        app = self.app(adapter=adapter)
        async with app.run_test() as pilot:
            await pilot.pause()
            target = app.selected
            await pilot.press("enter")
            await pilot.pause()
            acting = [w.row["sessionId"] for w in app.query(ui.Row)
                      if w.has_class("acting")]
            self.assertEqual(acting, [target])

    async def test_it_blinks_rather_than_sitting_there(self):
        adapter = FakeAdapter(hang=True)
        app = self.app(adapter=adapter)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            row = [w for w in app.query(ui.Row) if w.has_class("acting")][0]
            first = row.has_class("pulse")
            app.pulse()
            await pilot.pause()
            self.assertNotEqual(row.has_class("pulse"), first,
                                "a mark that never changes is easy to miss")

    async def test_the_mark_goes_when_the_window_is_open(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertEqual([w for w in app.query(ui.Row)
                              if w.has_class("acting")], [],
                             "it is open: stop saying it is opening")

    async def test_nothing_is_marked_when_nothing_is_happening(self):  # control
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual([w for w in app.query(ui.Row)
                              if w.has_class("acting")], [])

    async def test_the_mark_survives_a_refresh(self):
        adapter = FakeAdapter(hang=True)
        collector = FakeCollector()
        app = self.app(adapter=adapter, collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            target = app.selected
            await pilot.press("enter")
            await pilot.pause()
            collector.fleet_value = ui.Fleet([dict(LIVE, recap="moved on"), BUSY],
                                             True, "12:00:05")
            app.collect()
            await pilot.pause()
            await pilot.pause()
            acting = [w.row["sessionId"] for w in app.query(ui.Row)
                      if w.has_class("acting")]
            self.assertEqual(acting, [target], "a scan must not clear it")

    async def test_a_background_session_being_attached_is_marked_too(self):
        adapter = FakeAdapter(hang=True)
        bg = row("51fddd61-822b-49e0-9aeb-2145e91e1244", "busy", tab_title="",
                 tty="", windowed=False, kind="background")
        app = self.app(adapter=adapter, collector=FakeCollector(
            fleet=ui.Fleet([bg], True, "12:00:00")))
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual([w.row["sessionId"] for w in app.query(ui.Row)
                              if w.has_class("acting")], [bg["sessionId"]])


class TestTheListReloadsThroughTheOneRoutine(unittest.TestCase):
    """The hotkey window stays open for days and re-reads its rules through
    engine.reload_all, the one routine. It used to keep its own copy of the
    module list, and that copy did not put the working modules back when a new
    file raised half way: the window then ran rules that were neither version."""

    def reload(self):
        c = ui.Collector()
        c.reload()
        return c

    def test_every_rule_module_is_re_read(self):
        for mod, handle in (("ccwho_brief", "brief"), ("ccwho_index", "ccwho_index"),
                            ("ccwho_procs", "procs"), ("ccwho_terms", "terms")):
            with self.subTest(module=mod):
                m = __import__(mod)
                path = m.__file__
                original = open(path).read()
                try:
                    with open(path, "w") as fh:
                        fh.write(original + "\n\ndef _hot_probe():\n    return 1\n")
                    self.reload()
                    self.assertTrue(hasattr(getattr(ui.engine, handle), "_hot_probe"),
                                    f"the list still runs the old {mod}")
                finally:
                    with open(path, "w") as fh:
                        fh.write(original)
                    self.reload()

    def test_a_half_raising_edit_leaves_the_old_rules_working(self):
        # A guard, not a fix: this passed before the shared routine too, because a
        # candidate that raises is never swapped in. It pins that the shared
        # routine keeps it that way for the list as well as the watch.
        import ccwho_brief
        path = ccwho_brief.__file__
        original = open(path).read()
        try:
            with open(path, "w") as fh:
                fh.write(original.replace(
                    'MAX_PROGRESS = 5', 'MAX_PROGRESS = 5\nLOW_SIGNAL = set()\n'
                    'raise RuntimeError("boom")'))
            c = self.reload()
            self.assertTrue(c.reload_error, "the error has to reach the header")
            self.assertTrue(ui.engine.brief.LOW_SIGNAL,
                            "the half-applied edit must not become the live rule set")
        finally:
            with open(path, "w") as fh:
                fh.write(original)
            self.reload()

    def test_a_good_edit_is_not_reported_as_an_error(self):             # control
        c = self.reload()
        self.assertEqual(c.reload_error, "")


# ---------------------------------------------------------------- slice 2b
# What agents started, in the list: one dim line under the header, the ports on
# each row, one collapsed line at the bottom, and `p` for the whole list. NEEDS
# YOU still owns the screen (DX D9).

PROCS = {"ports_ok": True, "procs_ok": True, "sessions_ok": True,
         "agent_ports": [{"port": 3000, "pid": 12, "who": "liveapp"},
                         {"port": 8080, "pid": 20, "who": "left behind"}],
         "by_session": {LIVE["sessionId"]: [
             {"pid": 12, "ports": [3000], "command": "vite --port 3000", "helper": False,
              "orphan": False, "harness": "claude", "session": LIVE["sessionId"]}]},
         "left_behind": [{"pid": 20, "ports": [8080], "command": "next-server",
                          "helper": False, "orphan": True, "harness": "claude",
                          "session": "dddd"}],
         "codex": [], "unsure": []}
HOLDING = dict(LIVE, ports=[3000], procs=1)


def with_procs(procs=PROCS, rows=(HOLDING, BUSY)):
    return FakeCollector(fleet=ui.Fleet(list(rows), True, "12:00:00", procs=procs))


class TestTheListSaysWhatAgentsHold(UiTest):
    async def test_one_dim_line_under_the_header(self):
        app = self.app(collector=with_procs())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            line = app.query_one("#ports")
            self.assertTrue(line.display)
            self.assertIn("agents hold :3000 liveapp", str(line.content))

    async def test_no_ports_no_line(self):                               # control
        app = self.app(collector=with_procs(dict(PROCS, agent_ports=[], left_behind=[])))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one("#ports").display)
            self.assertFalse(app.query_one("#procline").display)

    async def test_the_row_shows_its_ports(self):
        app = self.app(collector=with_procs())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            row = [w for w in app.rows_on_screen()
                   if w.row["sessionId"] == LIVE["sessionId"]][0]
            self.assertIn(":3000", row.painted)

    async def test_left_behind_is_one_line_at_the_bottom(self):
        app = self.app(collector=with_procs())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            line = app.query_one("#procline")
            self.assertTrue(line.display)
            self.assertEqual(str(line.content), "left behind: 1 process, 1 port · p to see")

    async def test_p_shows_every_process_and_escape_goes_back(self):
        app = self.app(collector=with_procs())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            self.assertTrue(app.query_one("#detail").display)
            text = str(app.query_one("#brief").content)
            for want in ("vite --port 3000", "LEFT BEHIND", "next-server", ":8080"):
                self.assertIn(want, text)
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.query_one("#detail").display)
            self.assertTrue(isinstance(app.focused, ui.Row))

    async def test_the_brief_lists_the_sessions_processes(self):
        app = self.app(collector=with_procs())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            text = str(app.query_one("#brief").content)
            self.assertIn("vite --port 3000", text)
            self.assertIn("Shall I land it?", text)                     # still the brief

    async def test_a_port_search_finds_the_row(self):
        app = self.app(collector=with_procs())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            for ch in ":3000":
                await pilot.press(ch)
            await pilot.pause()
            text = self.screen_text(app)
            self.assertIn("Issue 362", text)
            self.assertNotIn("Release queue", text)

    async def test_the_old_fleet_shape_still_works(self):                # compat
        app = self.app(collector=FakeCollector(fleet=ui.Fleet([LIVE, BUSY], True, "12:00:00")))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            self.assertTrue(app.rows_on_screen())


class TestTheCollectorPassesWhatAgentsStarted(unittest.TestCase):
    def test_the_fleet_carries_collects_second_answer(self):
        real_collect, real_reload = ui.engine.collect, ui.engine.reload_all
        ui.engine.collect = lambda cache=None, status=None: (
            [dict(LIVE)], PROCS) if status.update(source_ok=True) is None else None
        ui.engine.reload_all = lambda engine: None
        try:
            fleet = ui.Collector().fleet()
        finally:
            ui.engine.collect, ui.engine.reload_all = real_collect, real_reload
        self.assertEqual(fleet.procs, PROCS)
        self.assertIs(ui.Fleet([], True, "").procs.get("collected"), False)  # control: not yet


class TestTheProcessScreenReview(UiTest):
    """Review of slice 2b: the pane follows refreshes, unknown is not none, the
    keys on the process screen do not move a selection you cannot see."""

    async def test_a_refresh_repaints_the_open_process_screen(self):
        collector = with_procs()
        app = self.app(collector=collector)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            self.assertIn("next-server", str(app.query_one("#brief").content))
            # one refresh with the pane open settles the list at its new width:
            # the next one keeps the rows and takes the cheap path
            app.show(ui.Fleet([HOLDING, BUSY], True, "12:00:03", procs=PROCS))
            await pilot.pause()
            app.show(ui.Fleet([HOLDING, BUSY], True, "12:00:05",
                              procs=dict(PROCS, left_behind=[])))
            await pilot.pause()
            text = str(app.query_one("#brief").content)
            self.assertNotIn("next-server", text)
            self.assertIn("vite --port 3000", text)                         # control

    async def test_a_refresh_repaints_the_briefs_processes(self):
        app = self.app(collector=with_procs())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertIn("vite --port 3000", str(app.query_one("#brief").content))
            app.show(ui.Fleet([HOLDING, BUSY], True, "12:00:03", procs=PROCS))
            await pilot.pause()
            app.show(ui.Fleet([HOLDING, BUSY], True, "12:00:05",
                              procs=dict(PROCS, by_session={})))
            await pilot.pause()
            self.assertNotIn("vite --port 3000", str(app.query_one("#brief").content))

    async def test_not_collected_is_not_no_processes(self):
        for fleet in (ui.Fleet([LIVE, BUSY], True, "12:00:00"),
                      ui.Fleet([], False, "", "could not read the fleet: boom")):
            with self.subTest(error=fleet.error):
                app = self.app(collector=FakeCollector(fleet=fleet))
                async with app.run_test(size=(160, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("p")
                    await pilot.pause()
                    text = str(app.query_one("#brief").content)
                    self.assertNotIn("no processes", text)
                    self.assertIn("unknown", text)

    async def test_an_empty_valid_answer_is_no_processes(self):          # control
        empty = dict(PROCS, by_session={}, left_behind=[], agent_ports=[])
        app = self.app(collector=with_procs(empty))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            self.assertIn("no processes started by agents",
                          str(app.query_one("#brief").content))

    async def test_keys_on_the_process_screen_do_not_move_the_selection(self):
        app = self.app(collector=with_procs())
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            before = app.selected
            await pilot.press("p")
            await pilot.pause()
            await pilot.press("j")
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app.selected, before)

    async def test_keys_on_the_brief_still_move_it(self):                # control
        app = self.app(collector=with_procs())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            before = app.selected
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("j")
            await pilot.pause()
            self.assertNotEqual(app.selected, before)

    async def test_bad_process_data_never_takes_the_list_down(self):
        # a shape the engine itself cannot read: the UI still stands
        bad = dict(PROCS, left_behind="not a list", agent_ports=[{"port": 3000, "pid": 1}],
                   codex=[{"ports": [], "command": "y"}])
        app = self.app(collector=with_procs(bad))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            self.assertIsNone(app.return_code)
            self.assertTrue(app.rows_on_screen())

    async def test_the_lines_stay_one_line(self):
        long_who = [{"port": 3000 + i, "pid": i, "who": "project\nwith a newline"}
                    for i in range(12)]
        procs = dict(PROCS, agent_ports=long_who,
                     left_behind=PROCS["left_behind"] * 12,
                     codex=[dict(PROCS["left_behind"][0], harness="codex")])
        for width in (40, 60, 200):
            with self.subTest(width=width):
                app = self.app(collector=with_procs(procs))
                async with app.run_test(size=(width, 30)) as pilot:
                    await pilot.pause()
                    self.assertLessEqual(app.query_one("#ports").size.height, 1)
                    self.assertLessEqual(app.query_one("#procline").size.height, 2)
                    for line in str(app.query_one("#procline").content).splitlines():
                        self.assertLessEqual(len(line), width - 2)

    async def test_the_process_screen_fits_its_pane(self):
        # enough processes to scroll: the scrollbar takes a cell of the pane
        from rich.cells import cell_len
        long = dict(PROCS, left_behind=[dict(PROCS["left_behind"][0], pid=100 + i,
                                             command="node " + "x" * 200 + ".js")
                                        for i in range(80)])
        for width in (140, 160, 80):
            with self.subTest(width=width):
                app = self.app(collector=with_procs(long))
                async with app.run_test(size=(width, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("p")
                    await pilot.pause()
                    box = app.query_one("#brief")
                    for line in str(box.content).splitlines():
                        self.assertLessEqual(cell_len(line), box.content_size.width)


STUCK = row("cccc3333-0000-4000-8000-000000000003", "stuck", status="busy", pid=73787,
            title="Blabberate", tab_title="✳ Blabberate (claude)",
            dead_loops=[{"pid": 86246, "tasks": ["bscl8fc6k"]}])


class TestKillingAStuckLoop(UiTest):
    """A loop that can never end is killed from the list: a click on its
    button, or `x` then `l`, opens the kill dialog about it alone (design review
    2026-09-29, decisions 1, 2, 6), and `y` there kills it - a kill is not
    undone, so the dialog asks first. The row's other clicks still go to the
    session."""

    def setUp(self):
        self.collector = FakeCollector(fleet=ui.Fleet([LIVE, STUCK], True, "12:00:00"))
        self.adapter = FakeAdapter()

    def stuck_row(self, app):
        return [w for w in app.query(ui.Row) if w.row["sessionId"] == STUCK["sessionId"]][0]

    def kill_offset(self, widget):
        """Where the button is drawn, in the row's own coordinates."""
        text = "".join(t for t, _ in widget.spans())
        line = text.split("\n")[1]
        # the row's left border and padding come before its text; one column in
        return (line.index(ui.engine.UI_KILL) + 2 + 1, 1)

    def boxed(self, app):
        return isinstance(app.screen, ui.ChoiceBox)

    def asked(self, app):
        return isinstance(app.screen, ui.KillBox)

    def said(self, app):
        return str(app.screen.query_one("#killbody").content) if self.asked(app) else app.status

    async def test_the_stuck_row_has_its_own_heading(self):
        app = self.app(collector=self.collector, adapter=self.adapter)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            text = self.screen_text(app)
            self.assertIn("STUCK", text)
            self.assertIn(ui.engine.UI_KILL, text)

    async def test_x_only_asks(self):
        app = self.app(collector=self.collector, adapter=self.adapter)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x")
            await pilot.pause()
            self.assertIsNone(self.collector.killed)
            self.assertTrue(self.boxed(app))
            text = "\n".join(str(w.content) for w in app.screen.query("Static"))
            self.assertIn("l  kill stuck loop 86246", text)
            self.assertIn("the session keeps running", text)

    async def test_l_in_the_box_asks_and_y_kills_it(self):
        app = self.app(collector=self.collector, adapter=self.adapter)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x", "l")
            await pilot.pause(0.2)
            self.assertIsNone(self.collector.killed)
            self.assertTrue(self.asked(app))
            await pilot.press("y")
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            self.assertEqual(self.collector.killed, [STUCK["sessionId"]])

    async def test_escape_kills_nothing(self):                           # control
        app = self.app(collector=self.collector, adapter=self.adapter)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x", "escape")
            await pilot.pause(0.2)
            self.assertIsNone(self.collector.killed)
            self.assertFalse(self.boxed(app))

    async def test_x_on_a_row_with_no_dead_loop_does_nothing(self):      # control
        app = self.app(collector=self.collector, adapter=self.adapter)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("x", "l", "y")
            await pilot.pause(0.2)
            self.assertIsNone(self.collector.killed)

    async def test_mid_turn_or_a_program_offers_no_loop_kill(self):
        # mid-turn the agent may be about to deal with the loop itself; a
        # program's row is no more a kill than a busy one of yours
        for attention in ("busy", "program"):
            with self.subTest(attention=attention):
                row = dict(STUCK, attention=attention)
                collector = FakeCollector(fleet=ui.Fleet([LIVE, row], True, "12:00:00"))
                app = self.app(collector=collector, adapter=self.adapter)
                async with app.run_test(size=(120, 30)) as pilot:
                    await pilot.pause()
                    await pilot.press("j", "x")
                    await pilot.pause()
                    self.assertFalse(self.boxed(app), "nothing to offer: no box")
                    await pilot.press("l", "y")
                    await pilot.pause(0.2)
                    self.assertIsNone(collector.killed)

    async def test_a_row_that_became_a_program_is_not_killed(self):
        # the row changes in place, so it is a copy: STUCK is shared by every test
        collector = FakeCollector(fleet=ui.Fleet([LIVE, dict(STUCK)], True, "12:00:00"))
        app = self.app(collector=collector, adapter=self.adapter)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x", "l")
            await pilot.pause()
            for r in app.fleet.rows:
                if r["sessionId"] == STUCK["sessionId"]:
                    r["attention"] = "program"
            await pilot.press("y")
            await pilot.pause(0.2)
            self.assertIsNone(collector.killed)
            self.assertIn("nothing killed", self.said(app))

    async def test_one_click_on_the_kill_only_asks_and_does_not_go(self):
        app = self.app(collector=self.collector, adapter=self.adapter)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            w = self.stuck_row(app)
            await pilot.click(w, offset=self.kill_offset(w))
            await pilot.pause(0.2)
            self.assertIsNone(self.collector.killed)
            self.assertEqual(self.adapter.asked, [])
            self.assertTrue(self.asked(app))

    async def test_the_click_then_y_kills_it(self):
        app = self.app(collector=self.collector, adapter=self.adapter)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            w = self.stuck_row(app)
            await pilot.click(w, offset=self.kill_offset(w))
            await pilot.pause()
            await pilot.press("y")
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            self.assertEqual(self.collector.killed, [STUCK["sessionId"]])
            self.assertEqual(self.adapter.asked, [])

    async def test_x_on_the_process_screen_does_nothing(self):
        # that screen is not about the selected row: you cannot see what x hits
        app = self.app(collector=self.collector, adapter=self.adapter)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("j", "p", "x", "l", "y")
            await pilot.pause(0.2)
            self.assertIsNone(self.collector.killed)
            self.assertFalse(self.boxed(app))
            self.assertFalse(self.asked(app))

    async def test_a_click_on_the_kill_with_the_process_screen_open_says_why(self):
        app = self.app(collector=self.collector, adapter=self.adapter)
        async with app.run_test(size=(180, 30)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            w = self.stuck_row(app)
            await pilot.click(w, offset=self.kill_offset(w))
            await pilot.pause(0.2)
            self.assertFalse(self.boxed(app))
            self.assertFalse(self.asked(app))
            self.assertIsNone(self.collector.killed)
            self.assertIn("Esc for the list", app.status)

    async def test_a_refresh_while_asked_keeps_the_kill(self):          # control
        app = self.app(collector=self.collector, adapter=self.adapter)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x", "l")
            await pilot.pause()
            app.show(ui.Fleet([LIVE, STUCK], True, "12:00:05"))
            await pilot.pause()
            await pilot.press("y")
            await app.workers.wait_for_complete()
            await pilot.pause(0.2)
            self.assertEqual(self.collector.killed, [STUCK["sessionId"]])

    async def test_a_refresh_with_other_loops_kills_nothing(self):
        app = self.app(collector=self.collector, adapter=self.adapter)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x", "l")
            await pilot.pause()
            moved = dict(STUCK, dead_loops=[{"pid": 99999, "tasks": ["b9"]}])
            app.show(ui.Fleet([LIVE, moved], True, "12:00:05"))
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause(0.2)
            self.assertIsNone(self.collector.killed)
            self.assertIn("nothing killed", self.said(app))

    async def test_what_the_kill_did_stays_in_the_box(self):
        app = self.app(collector=self.collector, adapter=self.adapter)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x", "l", "y")
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)
            self.assertTrue(self.asked(app), "the result waits for you to close it")
            self.assertIn("killed loop 86246", self.said(app))

    async def test_a_click_on_the_name_still_goes(self):                 # control
        app = self.app(collector=self.collector, adapter=self.adapter)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.click(self.stuck_row(app), offset=(20, 0))
            await pilot.pause(0.2)
            self.assertEqual(self.adapter.asked, [STUCK["sessionId"]])
            self.assertIsNone(self.collector.killed)



class TestTheButtonHasNoUnderline(unittest.TestCase):
    """The owner decided against underlines (design review 2026-09-29, decision
    4): brackets and bold say it is a button, the hover lights it."""

    def test_bold_and_no_underline(self):
        self.assertNotIn("underline", ui.ROLE_STYLE["action"])
        self.assertIn("bold", ui.ROLE_STYLE["action"])


class TestTheCollectorKillsOnlyThatSessionsLoops(unittest.TestCase):
    """What the kill did, one line per outcome, each pid named - and every
    outcome that applies, not the first (review round 2 of 62a1169)."""

    def kill(self, killed, report_says=None, row=STUCK):
        real = ui.engine.kill_dead_loops
        asked = []

        def fake(session_pid, pids, started=None, report=None, cache=None):
            asked.append((session_pid, pids))
            report.update(report_says or {})
            return killed
        ui.engine.kill_dead_loops = fake
        try:
            said = ui.Collector().kill_loops(row)
        finally:
            ui.engine.kill_dead_loops = real
        return asked, said

    def said_when(self, report_says, killed=()):
        return "\n".join(self.kill(list(killed), report_says)[1])

    def test_the_session_and_its_loops_are_passed(self):
        asked, said = self.kill([86246])
        self.assertEqual(asked, [(73787, [86246])])
        self.assertEqual(said, ["killed loop 86246"])

    def test_nothing_killed_says_so(self):                               # control
        _, said = self.kill([])
        self.assertNotIn("killed loop", "\n".join(said))

    def test_a_loop_that_still_runs_is_not_called_not_stuck(self):
        said = self.said_when({"unconfirmed": [86246]})
        self.assertNotIn("no loop there is stuck", said)
        self.assertIn("86246 still runs", said)

    def test_an_unread_ps_is_not_called_not_stuck(self):
        said = self.said_when({"unread": True})
        self.assertNotIn("no loop there is stuck", said)
        self.assertIn("could not read", said)

    def test_a_loop_that_could_not_be_signalled_says_so(self):
        said = self.said_when({"failed": [86246]})
        self.assertNotIn("no loop there is stuck", said)
        self.assertIn("could not signal 86246", said)

    def test_a_mixed_result_names_what_was_not_killed(self):
        said = self.said_when({"failed": [86247]}, killed=[86246])
        self.assertIn("killed loop 86246", said)
        self.assertIn("could not signal 86247", said)

    def test_every_outcome_that_applies_is_said(self):
        said = self.said_when({"failed": [86246], "unread": True, "unconfirmed": [86247]})
        self.assertIn("could not signal 86246", said)
        self.assertIn("could not read", said)
        self.assertIn("86247 still runs", said)

    def test_what_the_kill_saw_reaches_the_next_scan(self):
        # its new sample of a reader is the list's newest verdict (review 2) -
        # handed over: the kill runs in its own thread, beside a scan that may
        # be walking the cache, so the scan's own thread takes it (review 3)
        real_kill, real_collect = ui.engine.kill_dead_loops, ui.engine.collect
        collector = ui.Collector()
        collector.reload = lambda: None
        collector.cache["_stacks"] = {"k": [True, 1.0]}

        def kill(session_pid, pids, started=None, report=None, cache=None):
            cache.setdefault("_stacks", {})["k"] = [False, 2.0]
            cache["_verdicts_new"] = True
            return []
        seen = []

        def collect(cache=None, status=None):
            seen.append((dict(cache.get("_stacks") or {}), cache.get("_verdicts_new")))
            status["source_ok"] = True
            return [], 0
        ui.engine.kill_dead_loops, ui.engine.collect = kill, collect
        try:
            collector.kill_loops(STUCK)
            self.assertEqual(collector.cache, {"_stacks": {"k": [True, 1.0]}},
                             "the kill does not write the cache a scan walks")
            collector.fleet()
            collector.cache["_stacks"]["k"] = [True, 3.0]      # a newer scan's own look
            collector.fleet()
        finally:
            ui.engine.kill_dead_loops, ui.engine.collect = real_kill, real_collect
        self.assertEqual(seen[0], ({"k": [False, 2.0]}, True))
        self.assertEqual(seen[1][0], {"k": [True, 3.0]}, "never over a newer look")
        self.assertEqual(collector.learned, [], "handed over once: the queue does not grow")

    def test_an_engine_of_before_still_kills(self):
        # a hot reload onto an older engine, whose kill takes no cache: the kill
        # still runs, once, and nothing is handed over (review 4)
        real, calls = ui.engine.kill_dead_loops, []

        def before(session_pid, pids, ps=None, read=None, kill=None, matches=None, unix=None,
                   own=None, started=None, starts=None, report=None):
            calls.append(pids)
            return list(pids)
        ui.engine.kill_dead_loops = before
        collector = ui.Collector()
        try:
            said = collector.kill_loops(STUCK)
        finally:
            ui.engine.kill_dead_loops = real
        self.assertEqual((calls, collector.learned), ([[86246]], []))
        self.assertEqual(said, ["killed loop 86246"])

    def test_a_loop_that_is_gone_is_still_not_stuck(self):             # control
        self.assertEqual(self.kill([], {})[1], ["nothing killed: no loop there is stuck any more"])

    def test_a_readers_task_says_it_was_stopped(self):
        row = dict(STUCK, dead_loops=[{"pid": 54329, "kind": "reader", "program": "cat",
                                       "root": 86083, "start": "Sat Sep 26 15:12:13 2026"}])
        real, asked = ui.engine.kill_dead_loops, []

        def fake(session_pid, pids, started=None, report=None, cache=None):
            asked.append(started)
            report["spared"] = [54332]
            return [54329]
        ui.engine.kill_dead_loops = fake
        try:
            said = ui.Collector().kill_loops(row)
        finally:
            ui.engine.kill_dead_loops = real
        # the start it was listed with: a new cat on that pid is not killed
        self.assertEqual(asked, [{54329: "Sat Sep 26 15:12:13 2026"}])
        self.assertEqual(said, ["stopped the task of cat 54329", "spared the agent 54332"])


class TestTheProcessScreenVerify(UiTest):
    """Verification review of slice 2b's fixes."""

    async def test_enter_on_the_process_screen_goes_nowhere(self):
        # #2: it went to a session the screen did not show
        adapter = FakeAdapter()
        app = self.app(collector=with_procs(), adapter=adapter)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(adapter.asked, [])

    async def test_enter_on_the_brief_still_goes(self):                  # control
        adapter = FakeAdapter()
        app = self.app(collector=with_procs(), adapter=adapter)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(adapter.asked, [LIVE["sessionId"]])

    async def test_a_new_mode_starts_at_the_top(self):
        # #5: the brief opened scrolled down by what the process screen had scrolled
        many = dict(PROCS, left_behind=[dict(PROCS["left_behind"][0], pid=100 + i)
                                        for i in range(80)])
        app = self.app(collector=with_procs(many))
        async with app.run_test(size=(160, 20)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            for _ in range(30):             # the keys are in the processes: down them
                await pilot.press("j")
            await pilot.pause()
            self.assertGreater(app.query_one("#detail").scroll_y, 0)            # control
            # → now takes the keys into the process screen (as into the brief);
            # the brief is Esc, then →
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app.detail_mode, "brief")
            self.assertEqual(app.query_one("#detail").scroll_y, 0)

    async def test_unknown_processes_are_said_in_the_brief(self):
        app = self.app(collector=with_procs(dict(PROCS, procs_ok=False)))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertIn("unknown", str(app.query_one("#brief").content))


class TestTheProcessScreenRound3(UiTest):
    """Third review of slice 2b's fixes."""

    async def test_no_false_alarm_before_the_first_collect(self):
        # #1: the panel said "processes unknown" on every start
        class Slow(FakeCollector):
            def fleet(self):
                import time
                time.sleep(2)
                return super().fleet()
        app = self.app(collector=Slow())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause(0.3)
            self.assertFalse(app.query_one("#ports").display)

    async def test_a_scan_that_failed_says_so_on_p(self):                # control
        app = self.app(collector=with_procs(dict(PROCS, procs_ok=False)))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            self.assertIn("unknown", str(app.query_one("#ports").content))

    async def test_enter_on_the_process_screen_leaves_a_running_jump_alone(self):
        # #4: it cleared the "opening..." of a jump still under way
        app = self.app(collector=with_procs(), adapter=FakeAdapter(hang=True))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.1)
            acting = app.acting
            self.assertTrue(acting)                                             # control
            await pilot.press("p")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.acting, acting)

    async def test_wide_enter_goes_to_the_row_beside_the_screen(self):
        # #5: at wide widths the list and its highlight are on screen
        adapter = FakeAdapter()
        app = self.app(collector=with_procs(), adapter=adapter)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            # `p` puts the keys in the process screen: Enter there copies the pid
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual((adapter.asked, adapter.copied), ([], ["12"]))
            # Esc gives them back to the list: Enter goes to the row
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(adapter.asked, [LIVE["sessionId"]])

    async def test_another_session_starts_at_the_top_of_its_brief(self):
        # #7: moving to another session kept the last one's scroll
        long = dict(FakeCollector().brief_value, progress=[f"Bash: step {i}" for i in range(80)])
        app = self.app(collector=FakeCollector(fleet=ui.Fleet([LIVE, BUSY], True, "12:00:00"),
                                               brief=long))
        async with app.run_test(size=(160, 20)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            app.query_one("#detail").scroll_to(y=10, animate=False)
            await pilot.pause()
            app.show(ui.Fleet([LIVE, BUSY], True, "12:00:05"))
            await pilot.pause()
            self.assertGreater(app.query_one("#detail").scroll_y, 0)            # control: same session
            await pilot.press("j")
            await pilot.pause()
            self.assertEqual(app.query_one("#detail").scroll_y, 0)

    def test_cells_agree_with_rich(self):
        # #6: the screen is drawn by Rich; a row counted any other way overflows
        from rich.cells import cell_len
        import ccwho_engine as engine
        for s in ("✳️ x", "⚠️ warn", "👨‍👩‍👧 family", "👍🏽 ok", "1️⃣ one", "🇩🇪 de",
                  "修复登录页面", "a\tb", "\U0001FAE9 new", "plain ascii"):
            with self.subTest(s=s):
                self.assertEqual(engine._cells(s), cell_len(s))


class TestAClickStillGoesThere(UiTest):
    async def test_a_click_on_a_row_goes_with_the_process_screen_open(self):
        adapter = FakeAdapter()
        app = self.app(collector=with_procs(), adapter=adapter)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            row = [w for w in app.rows_on_screen()
                   if w.row["sessionId"] == BUSY["sessionId"]][0]
            await pilot.click(row)
            await pilot.pause(0.2)
            self.assertEqual(adapter.asked, [BUSY["sessionId"]])


class TestTheKillButtonOnAScrollingList(UiTest):
    """The list's scrollbar takes cells from every row. Laid out wider than its
    content, a full second line lost its end - `[kill` with `loop]` cut away -
    and the blank cells where it had been still armed a kill."""

    FILLERS = [row(f"ffff{i:04d}-0000-4000-8000-000000000000", "busy", title=f"filler {i}")
               for i in range(30)]

    def stuck(self, recap):
        return dict(STUCK, recap=recap, recap_age="3h")

    def button(self, widget):
        """The cells [kill loop] is drawn in, in the row's own coordinates."""
        from rich.cells import cell_len
        _, second = widget.spans_lines()
        col = 0
        for text, role in second:
            if role == "action":
                return col, cell_len(text)
            col += cell_len(text)
        return None, 0

    async def check(self, recap, size=(120, 30), detail=False):
        collector = FakeCollector(fleet=ui.Fleet([self.stuck(recap)] + self.FILLERS, True, "12:00:00"))
        app = self.app(collector=collector, adapter=FakeAdapter())
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one("#list").scrollbars_enabled[0], "the list scrolls")
            if detail:
                # wide, the brief takes 38%: the list narrows with no refresh
                await pilot.press("right")
                await pilot.pause()
                await self.assert_button(app, pilot)
                await pilot.press("left")
                await pilot.pause()
            await self.assert_button(app, pilot)

    async def assert_button(self, app, pilot):
        from rich.cells import cell_len
        if True:
            widget = [w for w in app.query(ui.Row) if w.row["sessionId"] == STUCK["sessionId"]][0]
            # what is PAINTED is laid out at the width it is drawn in, whole
            painted = widget.painted.split("\n")[1]
            self.assertLessEqual(cell_len(painted), widget.content_region.width)
            self.assertIn(ui.engine.UI_KILL, painted)
            col, width = self.button(widget)
            self.assertEqual(width, cell_len(ui.engine.UI_KILL))
            gutter = widget.content_region.x - widget.region.x
            async def screen_is(boxed, tries=40):
                # a loaded machine can take more than one frame to push a screen
                for _ in range(tries):
                    if isinstance(app.screen, ui.KillBox) == boxed:
                        return True
                    await pilot.pause(0.05)
                return False
            # every cell names the kill; the ends are clicked for real (a box
            # each is slow: all of them ran close to the 30 s kill)
            for cell in range(width):
                self.assertEqual(widget.action_at(col + cell, 1), "kill", f"cell {cell}")
            self.assertNotEqual(widget.action_at(col + width, 1), "kill")          # control
            for cell in (0, width - 1):
                await pilot.click(widget, offset=(gutter + col + cell, 1))
                self.assertTrue(await screen_is(True), f"cell {cell} of the button asks")
                await pilot.press("escape")
                self.assertTrue(await screen_is(False))
            await pilot.click(widget, offset=(gutter + col + width, 1))           # control
            self.assertFalse(await screen_is(True, tries=10), "past the button: no box")

    async def test_an_ascii_recap(self):
        await self.check("Goal was moving the durable files into STATE; " * 6)

    async def test_a_cjk_recap(self):
        # an odd width: at an even one a double-width cut leaves a spare cell and
        # the old layout happened to fit (this test was green without the fix)
        await self.check("把持久化文件移到状态目录并验证每一个游标的恢复路径都能工作" * 3,
                         size=(121, 30))

    async def test_the_brief_opening_narrows_the_row(self):
        await self.check("Goal was moving the durable files into STATE; " * 6,
                         size=(160, 30), detail=True)


# ------------------------------------------------------------------ usage
NOW = 1790340000.0


def usage_snap(two=True):
    import ccwho_usage as usage
    login = {"id": "login:a", "kind": "login", "email": "lukaso@gmail.com", "brand": "ant",
             "label": "", "sessions": 1, "age": 5,
             "five_hour": {"state": "ok", "pct": 42, "resets_at": NOW + 7200, "age": 5},
             "seven_day": None}
    token = {"id": "token:1a2b3c4d", "kind": "token", "email": "", "brand": "ant",
             "label": "", "sessions": 1, "age": 5,
             "five_hour": {"state": "ok", "pct": 67, "resets_at": NOW + 9000, "age": 5},
             "seven_day": None}
    rows = [login, token] if two else [login]
    names = usage.short_names(rows, {})
    sessions = {LIVE["sessionId"]: "login:a"}
    if two:
        sessions[BUSY["sessionId"]] = "token:1a2b3c4d"
    return {"state": "ok", "accounts": usage.ordered(rows, names), "names": names,
            "sessions": sessions, "now": NOW}


class TestTheCollectorAsksForCodexUsage(unittest.TestCase):
    """The list hands the usage read its fleet's open Codex threads: a resumed
    thread's rollout, in an old folder, is found by its id."""

    def test_the_open_threads_go_to_the_usage_read(self):
        import ccwho as runner
        seen = []
        real = runner.usage_snapshot
        runner.usage_snapshot = (lambda rows, *a, **kw:
                                 seen.append(kw.get("codex_threads")) or {"state": "ok"})
        self.addCleanup(setattr, runner, "usage_snapshot", real)
        c = ui.Collector()
        c.usage([], {"codex_threads": [{"thread": "x"}, {"name": "no id"}, None]})
        c.usage([], {"codex_threads": []})
        c.usage([], None)
        self.assertEqual(seen, [["x"], [], []])


class TestUsageLine(UiTest):
    def app_with(self, snap):
        fleet = ui.Fleet([LIVE, BUSY], True, "12:00:00", usage=snap)
        return self.app(collector=FakeCollector(fleet=fleet))

    def usage_text(self, app):
        return app.query_one("#usage").render()

    async def test_the_line_is_under_the_header(self):
        app = self.app_with(usage_snap())
        async with app.run_test() as pilot:
            await pilot.pause()
            widget = app.query_one("#usage")
            self.assertTrue(widget.display)
            self.assertIn("usage  ant:lukaso 5h 42%", str(self.usage_text(app)))
            ids = [w.id for w in app.screen.children]
            self.assertLess(ids.index("header"), ids.index("usage"))
            self.assertLess(ids.index("usage"), ids.index("ports"))

    async def test_off_means_no_line(self):
        app = self.app_with({"state": "off"})
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one("#usage").display)

    async def test_no_usage_collected_means_no_line(self):                # control
        app = self.app_with(None)
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one("#usage").display)

    def bright(self, app):
        """The characters no dim span covers: plain text has no span at all."""
        text = self.usage_text(app)
        dim = set()
        for s in text.spans:
            if "dim" in str(s.style):
                dim.update(range(s.start, s.end))
        return "".join(c for i, c in enumerate(text.plain) if i not in dim)

    async def test_the_selected_rows_account_is_bright_and_follows_the_cursor(self):
        app = self.app_with(usage_snap())
        async with app.run_test() as pilot:
            await pilot.pause()
            app.selected = LIVE["sessionId"]
            app.mark_selected()
            await pilot.pause()
            self.assertIn("lukaso", self.bright(app))
            self.assertNotIn("1a2b", self.bright(app))
            app.selected = BUSY["sessionId"]
            app.mark_selected()
            await pilot.pause()
            self.assertIn("1a2b", self.bright(app))
            self.assertNotIn("lukaso", self.bright(app))

    async def test_rows_carry_the_account_tag_with_two_accounts(self):
        app = self.app_with(usage_snap())
        async with app.run_test() as pilot:
            await pilot.pause()
            words = {w.row["sessionId"]: w.words(w.width) for w in app.query(ui.Row)}
            self.assertIn(" · ant:lukaso", words[LIVE["sessionId"]].split("\n")[0])
            self.assertIn(" · ant:1a2b", words[BUSY["sessionId"]].split("\n")[0])

    async def test_one_account_no_tag(self):
        plain = self.app_with(None)
        async with plain.run_test() as pilot:
            await pilot.pause()
            before = sorted(w.words(w.width) for w in plain.query(ui.Row))
        app = self.app_with(usage_snap(two=False))
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertEqual(sorted(w.words(w.width) for w in app.query(ui.Row)), before)

    async def test_a_broken_snapshot_says_unknown_and_keeps_the_rows(self):
        app = self.app_with({"state": "ok", "accounts": [{"id": 3}]})
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertIn("usage  unknown", str(self.usage_text(app)))
            self.assertEqual(len(list(app.query(ui.Row))), 2)


class TestTheCollectorReadsUsage(unittest.TestCase):
    def collect(self, snapshot, procs=None):
        import ccwho as runner
        real = (ui.engine.collect, runner.usage_snapshot)
        procs = {"collected": True} if procs is None else procs
        ui.engine.collect = lambda cache=None, status=None: (
            status.update(source_ok=True) or ([LIVE], procs))
        runner.usage_snapshot = snapshot
        try:
            c = ui.Collector()
            c.reload = lambda: None
            c.secure_input = lambda: ""
            return c.fleet()
        finally:
            ui.engine.collect, runner.usage_snapshot = real

    def test_the_fleet_carries_the_snapshot(self):
        fleet = self.collect(lambda rows, codex_threads=(): {"state": "waiting",
                                                             "rows": len(rows)})
        self.assertEqual(fleet.usage, {"state": "waiting", "rows": 1})

    def test_an_open_thread_reaches_the_usage_read(self):
        # the list's own scan says which threads are open
        seen = []
        snap = lambda rows, codex_threads=(): seen.append(codex_threads) or {"state": "ok"}
        self.collect(snap, {"collected": True, "codex_threads": [{"thread": "t1"}]})
        self.collect(snap, {"collected": True, "codex_threads": []})          # control
        self.assertEqual(seen, [["t1"], []])

    def test_a_usage_failure_is_unknown_and_the_rows_survive(self):
        def boom(rows, codex_threads=()):
            raise ValueError("bad reading")
        fleet = self.collect(boom)
        self.assertEqual(fleet.usage, {"state": "unknown"})
        self.assertEqual(len(fleet.rows), 1)


class TestCursorRepaintsUsageOnlyWhenTheAccountChanges(UiTest):
    async def test_same_account_no_update(self):
        snap = usage_snap()
        snap["sessions"][BUSY["sessionId"]] = "login:a"      # both rows: one account
        fleet = ui.Fleet([LIVE, BUSY], True, "12:00:00", usage=snap)
        app = self.app(collector=FakeCollector(fleet=fleet))
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one("#usage")
            calls = []
            real = box.update
            box.update = lambda *a, **k: calls.append(1) or real(*a, **k)
            app.selected = LIVE["sessionId"]
            app.mark_selected()
            app.selected = BUSY["sessionId"]
            app.mark_selected()
            self.assertEqual(calls, [])
            app.fleet.usage["sessions"][BUSY["sessionId"]] = "token:1a2b3c4d"   # control
            app.mark_selected()
            self.assertEqual(calls, [1])


class TestAMalformedSnapshotNeverCrashesTheList(UiTest):
    async def test_sessions_none_on_a_cursor_move(self):
        fleet = ui.Fleet([LIVE, BUSY], True, "12:00:00", usage={"state": "ok", "sessions": None})
        app = self.app(collector=FakeCollector(fleet=fleet))
        async with app.run_test() as pilot:
            await pilot.pause()
            app.selected = BUSY["sessionId"]
            app.mark_selected()
            await pilot.pause()
            self.assertIn("usage  unknown", str(app.query_one("#usage").render()))


class TestAClickInTheDetailCopiesWhatYouClicked(UiTest):
    """Selecting text in the list never reached iTerm2: the list takes the mouse.
    So the detail offers its values to copy - each one a left-click target, as
    iTerm2 keeps right-click and ctrl-click for itself."""

    async def open_detail(self, pilot):
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()

    async def click_on(self, pilot, app, text):
        """Click the middle of `text` where the detail draws it."""
        box = app.query_one("#brief")
        lines = [str(line.text) for line in
                 (box.render_line(y) for y in range(box.size.height))]
        for y, line in enumerate(lines):
            if text in line:
                await pilot.click("#brief", offset=(line.index(text) + len(text) // 2, y))
                await pilot.pause()
                return
        self.fail(f"{text!r} is not on the screen: {lines}")

    async def test_a_click_on_a_value_copies_it_and_says_so(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.open_detail(pilot)
            await self.click_on(pilot, app, "/Users/x/liveapp")
            self.assertEqual(adapter.copied, ["/Users/x/liveapp"])
            self.assertIn("copied /Users/x/liveapp", self.screen_text(app))
            self.assertEqual(adapter.asked, [], "a copy is not a jump")
            self.assertTrue(app.query_one("#detail").display, "and the detail stays")

    async def test_the_resume_line_copies_as_a_command(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.open_detail(pilot)
            await self.click_on(pilot, app, "claude --resume")
            self.assertEqual(adapter.copied, [f"claude --resume {LIVE['sessionId']}"])

    async def test_a_label_copies_nothing(self):                        # control
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.open_detail(pilot)
            await self.click_on(pilot, app, "you said")
            self.assertEqual(adapter.copied, [])

    async def test_what_you_copy_is_the_value_exactly_not_the_cut(self):
        said = "he said \"run x(1)\", then 'y' - " + "and more " * 20 + "\nthe end"
        collector = FakeCollector()
        collector.brief_value = dict(collector.brief_value, you_said=said)
        adapter = FakeAdapter()
        app = self.app(adapter=adapter, collector=collector)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.open_detail(pilot)
            await self.click_on(pilot, app, "he said")
            self.assertEqual(adapter.copied, [said.strip()])

    async def test_a_copy_that_failed_says_so(self):
        adapter = FakeAdapter(copy_error="pbcopy is not there")
        app = self.app(adapter=adapter)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.open_detail(pilot)
            await self.click_on(pilot, app, "liveapp-b2")
            self.assertIn("could not copy: pbcopy is not there", self.screen_text(app))
            self.assertNotIn("copied liveapp-b2", self.screen_text(app))


class TestTheAdapterCopiesThroughPbcopy(unittest.TestCase):
    """pbcopy reads bytes in the locale's encoding: without UTF-8, "日本語"
    arrives on the clipboard as something else."""

    def run_copy(self, result=None, raises=None):
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(cmd=cmd, **kw)
            if raises:
                raise raises
            return result or subprocess.CompletedProcess(cmd, 0, "", "")
        with mock.patch.object(ui.subprocess, "run", fake_run):
            answer = ui.Adapter().copy("日本語 liveapp-40")
        return answer, seen

    def test_it_sends_the_text_as_utf8(self):
        answer, seen = self.run_copy()
        self.assertEqual(answer, "")
        self.assertEqual(seen["cmd"], ["pbcopy"])
        self.assertEqual(seen["input"], "日本語 liveapp-40")
        self.assertIn("UTF-8", seen["env"].get("LC_CTYPE", ""))
        self.assertEqual(seen.get("encoding"), "utf-8", "and Python must write UTF-8 too")
        self.assertTrue(seen.get("timeout"))

    def test_a_failure_is_said_not_raised(self):
        answer, _ = self.run_copy(raises=OSError("No such file: pbcopy"))
        self.assertIn("pbcopy", answer)
        answer, _ = self.run_copy(raises=subprocess.TimeoutExpired("pbcopy", 2))
        self.assertTrue(answer)
        answer, _ = self.run_copy(result=subprocess.CompletedProcess(["pbcopy"], 1, "", "boom"))
        self.assertIn("boom", answer)


class TestWhatYouCanCopyLightsUp(UiTest):
    """iTerm2 gives ccwho only the left click, so what a click would copy must
    show itself: it lights up under the mouse, and a label does not."""

    def styles_at(self, app, text):
        box = app.query_one("#brief")
        for y in range(box.size.height):
            strip = box.render_line(y)
            if text in strip.text:
                at, x = strip.text.index(text), 0
                for seg in strip:
                    if x <= at < x + len(seg.text):
                        return (y, at), seg.style
                    x += len(seg.text)
        self.fail(f"{text!r} is not on the screen")

    async def test_a_value_lights_up_under_the_mouse_and_a_label_does_not(self):
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            _, resume = self.styles_at(app, "claude --resume")
            for text, lights in (("/Users/x/liveapp", True), ("you said", False)):
                with self.subTest(text=text):
                    (y, x), before = self.styles_at(app, text)
                    await pilot.hover("#brief", offset=(x + 1, y))
                    await pilot.pause()
                    _, after = self.styles_at(app, text)
                    self.assertEqual(before != after, lights, (before, after))
                    # only what a click there would copy: not every value at once
                    _, other = self.styles_at(app, "claude --resume")
                    self.assertEqual(other, resume, "another value lit up too")
                    await pilot.hover("#header")
                    await pilot.pause()


class TestTheDetailRound1(UiTest):
    """Review of the copy slice: codes in a value, a value pbcopy cannot take,
    and what lights up after a repaint."""

    def reversed_rows(self, app):
        box = app.query_one("#brief")
        return [y for y in range(box.size.height)
                if any(seg.style and seg.style.reverse and seg.text.strip()
                       for seg in box.render_line(y))]

    async def hover(self, pilot, app, text):
        box = app.query_one("#brief")
        for y in range(box.size.height):
            line = box.render_line(y).text
            if text in line:
                await pilot.hover("#brief", offset=(line.index(text) + 1, y))
                await pilot.pause()
                return y
        self.fail(f"{text!r} is not on the screen")

    async def test_terminal_codes_in_a_value_do_not_reach_the_screen(self):
        collector = FakeCollector()
        collector.brief_value = dict(collector.brief_value,
                                     you_said="pasted \x1b[31mRED\x1b[0m done",
                                     recap="a\x1b]52;c;aGk=\x07b")
        app = self.app(collector=collector)
        async with app.run_test(size=(300, 50)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            box = app.query_one("#brief")
            lines = [box.render_line(y) for y in range(box.size.height)]
            self.assertFalse(any("\x1b" in seg.text or "\x07" in seg.text
                                 for line in lines for seg in line))
            self.assertIn("pasted RED done", "\n".join(line.text for line in lines))

    async def test_a_click_never_ends_the_list(self):
        class Broken(FakeAdapter):
            def copy(self, text):
                raise UnicodeEncodeError("utf-8", text, 0, 1, "surrogates not allowed")
        app = self.app(adapter=Broken())
        async with app.run_test(size=(300, 50)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            app.action_copy_value("x")
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertIn("could not copy", self.screen_text(app))

    async def test_a_repaint_under_a_still_mouse_does_not_light_the_wrong_place(self):
        # the title says the name, and the name is on the aka line too: after
        # the title changes, the light must not jump to the aka line
        collector = FakeCollector()
        collector.brief_value = dict(collector.brief_value, title="liveapp-b2")
        app = self.app(collector=collector)
        async with app.run_test(size=(300, 50)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            y = await self.hover(pilot, app, "liveapp-b2")
            self.assertIn(y, self.reversed_rows(app))                       # control
            app.collector.brief_value = dict(app.collector.brief_value,
                                             title="Something else entirely")
            app.paint_detail()
            await pilot.pause()
            self.assertIn(self.reversed_rows(app), ([], [y]))

    async def test_a_repaint_that_adds_a_line_lights_nothing_that_is_not_under_the_mouse(self):
        collector = FakeCollector()
        collector.brief_value = dict(collector.brief_value, recap="", goal="",
                                     you_said="", progress=[], closing="")
        app = self.app(collector=collector)
        async with app.run_test(size=(300, 50)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            y = await self.hover(pilot, app, "/Users/x/liveapp")
            self.assertEqual(self.reversed_rows(app), [y])                  # control
            app.collector.brief_value = dict(app.collector.brief_value,
                                             recap="now there is a recap", goal="a goal",
                                             you_said="said", closing="closing")
            app.paint_detail()
            await pilot.pause()
            self.assertIn(self.reversed_rows(app), ([], [y]))

    async def test_one_value_in_two_places_lights_only_where_the_mouse_is(self):
        collector = FakeCollector()
        collector.brief_value = dict(collector.brief_value, title="liveapp-b2")
        app = self.app(collector=collector)
        async with app.run_test(size=(300, 50)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            y = await self.hover(pilot, app, "liveapp-b2")
            self.assertEqual(self.reversed_rows(app), [y])


class TestPbcopyGetsWhatItCanTake(unittest.TestCase):
    """The real pbcopy path, with a stand-in pbcopy on PATH that keeps its bytes."""

    def setUp(self):
        import os
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.out = os.path.join(self.dir, "clip")
        tool = os.path.join(self.dir, "pbcopy")
        with open(tool, "w") as fh:
            fh.write(f"#!/bin/sh\ncat > '{self.out}'\n")
        os.chmod(tool, 0o755)
        self.env = mock.patch.dict(os.environ, {"PATH": self.dir + os.pathsep + os.environ["PATH"]})
        self.env.start()

    def tearDown(self):
        import shutil
        self.env.stop()
        shutil.rmtree(self.dir)

    def clip(self):
        with open(self.out, "rb") as fh:
            return fh.read()

    def test_utf8_arrives_as_utf8(self):
        self.assertEqual(ui.Adapter().copy("日本語 liveapp-40"), "")
        self.assertEqual(self.clip(), "日本語 liveapp-40".encode("utf-8"))

    def test_a_half_emoji_is_copied_not_a_crash(self):
        self.assertEqual(ui.Adapter().copy("cut emoji \ud83d end"), "")
        self.assertEqual(self.clip(), b"cut emoji ? end")


class TestTheDetailRound2(UiTest):
    """Review of the copy slice, round 2: the first value lights too, a copy is
    what the pane shows, and a Windows line end does not blank a line."""

    reversed_rows = TestTheDetailRound1.reversed_rows
    hover = TestTheDetailRound1.hover

    async def open(self, pilot):
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()

    async def test_the_first_value_lights_up_and_goes_out(self):
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await self.open(pilot)
            y = await self.hover(pilot, app, "aaaa")
            self.assertEqual(self.reversed_rows(app), [y])
            await pilot.hover("#header")
            await pilot.pause()
            self.assertEqual(self.reversed_rows(app), [])
            self.assertIsNone(app.query_one("#brief").lit)

    async def test_a_copy_is_what_the_pane_shows(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        base = dict(app.collector.brief_value)
        cases = ("run \x1b[1mmake\x1b(B\x1b[m now", "a\x1bMb", "a\x1b]0;title",
                 "c\x1bd e\x1b", "x [1m] y", "pasted \x1b[31mRED\x1b[0m done",
                 "a\x1b[?25lb")
        async with app.run_test(size=(300, 50)) as pilot:
            await self.open(pilot)
            for said in cases:
                with self.subTest(said=said):
                    adapter.copied.clear()
                    app.collector.brief_value = dict(base, you_said=said)
                    app.paint_detail()
                    await pilot.pause()
                    box = app.query_one("#brief")
                    lines = [box.render_line(y) for y in range(box.size.height)]
                    self.assertFalse(any("\x1b" in seg.text
                                         for line in lines for seg in line))
                    y, line = next((y, line.text) for y, line in enumerate(lines)
                                   if "you said" in line.text)
                    shown = line.split("you said", 1)[1].strip()
                    await pilot.click("#brief", offset=(line.index("you said") + 10, y))
                    await pilot.pause()
                    self.assertEqual(adapter.copied, [shown])

    async def test_a_windows_line_end_does_not_blank_the_line(self):
        collector = FakeCollector()
        collector.brief_value = dict(collector.brief_value, recap="first\r\nsecond",
                                     you_said="10%\r100%")
        app = self.app(collector=collector)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.open(pilot)
            box = app.query_one("#brief")
            shown = "\n".join(box.render_line(y).text for y in range(box.size.height))
            self.assertIn("first", shown)
            self.assertIn("second", shown)
            self.assertIn("100%", shown)
            self.assertNotIn("10%1", shown, "a bare \\r still overwrites, as a terminal does")


class TestTheDetailLooksLikeTheList(UiTest):
    """What you can copy is shown the way the row's arrow is: it lights up under
    the mouse, and at rest it looks like the text around it."""

    async def test_nothing_in_the_detail_is_underlined(self):
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            box = app.query_one("#brief")
            under = [seg.text for y in range(box.size.height) for seg in box.render_line(y)
                     if seg.style and seg.style.underline and seg.text.strip()]
            self.assertEqual(under, [])
            shown = "\n".join(box.render_line(y).text for y in range(box.size.height))
            self.assertIn("/Users/x/liveapp", shown)                        # control


    async def test_a_value_is_the_colour_of_the_text_around_it_in_every_theme(self):
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            box = app.query_one("#brief")

            def style_of(text):
                for y in range(box.size.height):
                    for seg in box.render_line(y):
                        if text in seg.text:
                            return seg.style
                self.fail(f"{text!r} is not on the screen")
            for theme in sorted(app.available_themes):
                with self.subTest(theme=theme):
                    app.theme = theme
                    await pilot.pause()
                    plain, value = style_of("Bash: run the gate"), style_of("Shall I land it?")
                    # the reference must be plain text, or a link colour on both passes
                    self.assertNotIn("@click", plain.meta or {})
                    self.assertIn("@click", value.meta or {})
                    self.assertEqual((value.color, value.bgcolor), (plain.color, plain.bgcolor))
                    self.assertFalse(value.underline)


class TestTheKeysGoIntoTheDetail(UiTest):
    """Finder's column view: → opens the detail and ↑/↓ still browse sessions
    under it; → again takes the keys into the detail, where ↑/↓ move between
    its values and Enter copies one; ← gives the keys back to the list."""

    def lit(self, app):
        """The text of each segment the detail draws reversed, in order."""
        box = app.query_one("#brief")
        return [seg.text for y in range(box.size.height) for seg in box.render_line(y)
                if seg.style and seg.style.reverse and seg.text.strip()]

    async def enter(self, pilot):
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()

    async def test_right_again_puts_the_keys_on_the_first_value(self):
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            self.assertIs(app.focused, app.query_one("#brief"))
            self.assertEqual(self.lit(app), ["aaaa"])

    async def test_up_and_down_move_between_values_and_stop_at_the_ends(self):
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(self.lit(app), ["liveapp"])
            await pilot.press("up", "up", "up")
            await pilot.pause()
            self.assertEqual(self.lit(app), ["aaaa"], "the first value is where up stops")
            for _ in range(30):
                await pilot.press("down")
            await pilot.pause()
            self.assertEqual(self.lit(app), [f"claude --resume {LIVE['sessionId']}"])
            self.assertEqual(app.selected, LIVE["sessionId"], "the session did not change")

    async def test_enter_copies_the_value_and_does_not_jump(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            await pilot.press("down", "down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(adapter.copied, ["Issue 362"])
            self.assertIn("copied Issue 362", self.screen_text(app))
            self.assertEqual(adapter.asked, [], "Enter in the detail is a copy, not a jump")

    async def test_the_footer_says_what_the_keys_do_in_the_detail(self):
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            enter = app.active_bindings.get("enter")
            self.assertEqual(enter.binding.description, "copy")

    async def test_left_gives_the_keys_back_and_left_again_closes(self):
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            await pilot.press("left")
            await pilot.pause()
            self.assertIsNot(app.focused, app.query_one("#brief"))
            self.assertTrue(app.query_one("#detail").display, "the detail stays open")
            self.assertEqual(self.lit(app), [])
            await pilot.press("down")                   # the list has the keys again
            await pilot.pause()
            self.assertEqual(app.selected, BUSY["sessionId"])
            await pilot.press("left")
            await pilot.pause()
            self.assertFalse(app.query_one("#detail").display)

    async def test_one_right_leaves_the_keys_on_the_list(self):          # control
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(300, 50)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(self.lit(app), [])
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(adapter.copied, [])
            self.assertEqual(adapter.asked, [LIVE["sessionId"]], "Enter still jumps")

    async def test_a_refresh_keeps_the_keys_on_the_same_value(self):
        collector = FakeCollector()
        collector.brief_value = dict(collector.brief_value, recap="", goal="", you_said="",
                                     closing="")
        app = self.app(collector=collector)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            for _ in range(5):                          # id, project, title, tty, pid -> name
                await pilot.press("down")
            await pilot.pause()
            self.assertEqual(self.lit(app), ["liveapp-b2"])
            app.collector.brief_value = dict(app.collector.brief_value, recap="a recap now",
                                             goal="a goal", you_said="said", closing="said back")
            app.paint_detail()
            await pilot.pause()
            self.assertEqual(self.lit(app), ["liveapp-b2"])
            self.assertIs(app.focused, app.query_one("#brief"))

    async def test_the_pane_scrolls_to_the_value_the_keys_are_on(self):
        collector = FakeCollector()
        collector.brief_value = dict(collector.brief_value,
                                     progress=[f"step {i}" for i in range(40)])
        app = self.app(collector=collector)
        async with app.run_test(size=(100, 20)) as pilot:
            await self.enter(pilot)
            for _ in range(30):
                await pilot.press("down")
            await pilot.pause()
            detail, box = app.query_one("#detail"), app.query_one("#brief")
            y = next(y for y in range(box.size.height)
                     if any(seg.style and seg.style.reverse and seg.text.strip()
                            for seg in box.render_line(y)))
            top = box.virtual_region.y + y
            self.assertGreater(detail.scroll_y, 0)
            self.assertTrue(detail.scroll_y <= top < detail.scroll_y + detail.size.height,
                            (detail.scroll_y, top, detail.size.height))

    async def test_when_the_keys_leave_by_another_way_nothing_stays_lit(self):
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            self.assertEqual(self.lit(app), ["aaaa"])                          # control
            app.query_one("#brief").blur()
            await pilot.pause()
            self.assertIsNot(app.focused, app.query_one("#brief"))
            self.assertEqual(self.lit(app), [])


class TestTheKeysStayInTheDetail(UiTest):
    """Review of the keys slice, round 1: nothing but ← takes the keys out of
    the detail, and a refresh leaves them where they were."""

    lit = TestTheKeysGoIntoTheDetail.lit
    enter = TestTheKeysGoIntoTheDetail.enter

    def lit_row(self, app):
        box = app.query_one("#brief")
        return next((y for y in range(box.size.height)
                     if any(seg.style and seg.style.reverse and seg.text.strip()
                            for seg in box.render_line(y))), None)

    async def test_a_session_arriving_leaves_the_keys_in_the_detail(self):
        new = row("dddd4444-0000-4000-8000-000000000004", "busy", title="New one")
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            await pilot.press("down")
            await pilot.pause()
            app.show(ui.Fleet([LIVE, BUSY, new], True, "12:00:01"))
            await pilot.pause()
            await pilot.pause()
            self.assertIs(app.focused, app.query_one("#brief"))
            self.assertEqual(self.lit(app), ["liveapp"])
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(adapter.copied, ["liveapp"])
            self.assertEqual(adapter.asked, [], "a copy, never a jump")

    async def test_with_the_keys_on_the_list_a_rebuild_still_focuses_the_row(self):  # control
        new = row("dddd4444-0000-4000-8000-000000000004", "busy", title="New one")
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            app.show(ui.Fleet([LIVE, BUSY, new], True, "12:00:01"))
            await pilot.pause()
            await pilot.pause()
            self.assertIsInstance(app.focused, ui.Row)

    async def test_a_value_that_changes_under_the_keys_keeps_its_place(self):
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            while self.lit(app) != ["Shall I land it?"]:
                await pilot.press("down")
                await pilot.pause()
            app.collector.brief_value = dict(app.collector.brief_value,
                                             closing="Landed. Anything else?")
            app.paint_detail()
            await pilot.pause()
            self.assertEqual(self.lit(app), ["Landed. Anything else?"])

    async def test_after_a_refresh_the_value_the_keys_are_on_is_on_screen(self):
        collector = FakeCollector()
        collector.brief_value = dict(collector.brief_value,
                                     progress=[f"step {i}" for i in range(40)])
        app = self.app(collector=collector)
        async with app.run_test(size=(100, 20)) as pilot:
            await self.enter(pilot)
            for _ in range(30):
                await pilot.press("down")
            await pilot.pause()
            app.collector.brief_value = dict(app.collector.brief_value,
                                             progress=[f"step {i}" for i in range(80)])
            app.paint_detail()
            await pilot.pause()
            await pilot.pause()
            detail, box = app.query_one("#detail"), app.query_one("#brief")
            top = box.virtual_region.y + self.lit_row(app)
            self.assertTrue(detail.scroll_y <= top < detail.scroll_y + detail.size.height,
                            (detail.scroll_y, top, detail.size.height))

    async def test_one_value_in_two_places_keeps_the_place_the_keys_are_on(self):
        collector = FakeCollector()
        brief = collector.brief_value
        collector.brief_value = dict(brief, aka=dict(brief["aka"], name="Issue 362"))
        app = self.app(collector=collector)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            for _ in range(20):
                await pilot.press("down")
                await pilot.pause()
                if self.lit(app) == ["Issue 362"] and self.lit_row(app) > 2:
                    break
            y = self.lit_row(app)
            self.assertGreater(y, 2, "the second Issue 362, on the aka line")
            app.paint_detail()
            await pilot.pause()
            self.assertEqual(self.lit_row(app), y)

    async def test_the_processes_screen_gives_the_keys_back_to_the_list(self):
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            moved = []
            real = app.move
            app.move = lambda step: (moved.append(step), real(step))
            await pilot.press("p")
            await pilot.pause()
            self.assertIsNot(app.focused, app.query_one("#brief"))
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(moved, [1], "the processes screen scrolls again")

    async def test_a_click_copies_and_leaves_the_keys_on_the_list(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(300, 50)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            box = app.query_one("#brief")
            y = next(y for y in range(box.size.height) if "Issue 362" in box.render_line(y).text)
            await pilot.click("#brief", offset=(box.render_line(y).text.index("Issue 362") + 2, y))
            await pilot.pause()
            self.assertEqual(adapter.copied, ["Issue 362"])
            self.assertIsNot(app.focused, box)
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app.selected, BUSY["sessionId"], "↓ still moves between sessions")


class TestTheKeysKeepTheirField(UiTest):
    """Review of the keys slice, round 2: a refresh keeps the keys on the same
    FIELD, and with the keys in the detail the mouse does not move them."""

    lit = TestTheKeysGoIntoTheDetail.lit
    enter = TestTheKeysGoIntoTheDetail.enter

    async def on_the_closing(self, pilot, app):
        while self.lit(app) != ["Shall I land it?"]:
            await pilot.press("down")
            await pilot.pause()

    async def test_a_recap_arriving_with_a_new_closing_keeps_the_keys_on_the_closing(self):
        adapter = FakeAdapter()
        collector = FakeCollector()
        collector.brief_value = dict(collector.brief_value, recap="")
        app = self.app(collector=collector, adapter=adapter)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            await self.on_the_closing(pilot, app)
            app.collector.brief_value = dict(app.collector.brief_value, recap="landed the fix",
                                             closing="Landed. Anything else?")
            app.paint_detail()
            await pilot.pause()
            self.assertEqual(self.lit(app), ["Landed. Anything else?"])
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(adapter.copied, ["Landed. Anything else?"])

    async def test_a_recap_going_with_a_new_closing_keeps_the_keys_on_the_closing(self):
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            await self.on_the_closing(pilot, app)
            app.collector.brief_value = dict(app.collector.brief_value, recap="",
                                             closing="new closing")
            app.paint_detail()
            await pilot.pause()
            self.assertEqual(self.lit(app), ["new closing"])

    async def test_the_mouse_does_not_move_the_keys(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            box = app.query_one("#brief")
            y = next(y for y in range(box.size.height) if "Issue 362" in box.render_line(y).text)
            await pilot.hover("#brief", offset=(box.render_line(y).text.index("Issue 362") + 2, y))
            await pilot.pause()
            self.assertEqual(self.lit(app), ["aaaa"], "the one light is the keys'")
            await pilot.hover("#header")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(adapter.copied, ["aaaa"])

    async def test_when_the_field_goes_the_keys_go_to_the_next_field(self):
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            await self.on_the_closing(pilot, app)
            app.collector.brief_value = dict(app.collector.brief_value, recap="", closing="")
            app.paint_detail()
            await pilot.pause()
            self.assertEqual(self.lit(app), ["s022"], "the tty comes after the closing")

    async def test_the_keys_never_go_up_to_a_field_that_arrived(self):
        collector = FakeCollector()
        collector.brief_value = dict(collector.brief_value, title="")
        app = self.app(collector=collector)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            while self.lit(app) != ["the gate flake, understood"]:
                await pilot.press("down")
                await pilot.pause()
            app.collector.brief_value = dict(app.collector.brief_value, recap="", title="T")
            app.paint_detail()
            await pilot.pause()
            self.assertEqual(self.lit(app), ["fix the gate flake"], "the goal, not up to T")

    async def test_only_the_recap_going_lands_on_the_goal(self):              # control
        app = self.app()
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            while self.lit(app) != ["the gate flake, understood"]:
                await pilot.press("down")
                await pilot.pause()
            app.collector.brief_value = dict(app.collector.brief_value, recap="")
            app.paint_detail()
            await pilot.pause()
            self.assertEqual(self.lit(app), ["fix the gate flake"])

    async def test_the_last_field_going_lands_on_the_one_before(self):
        collector = FakeCollector()
        app = self.app(collector=collector)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            for _ in range(30):
                await pilot.press("down")
            await pilot.pause()
            self.assertEqual(self.lit(app), [f"claude --resume {LIVE['sessionId']}"])
            real = ui.engine.brief_parts
            def no_resume(b, r=None, fields=False):
                parts = [[p for p in line if p[3] != "resume"]
                         for line in real(b, r, fields=True)]
                return parts if fields else [[p[:3] for p in line] for line in parts]
            ui.engine.brief_parts = no_resume
            try:
                app.paint_detail()
                await pilot.pause()
            finally:
                ui.engine.brief_parts = real
            self.assertEqual(self.lit(app), [LIVE["sessionId"]], "the session id, before it")

    async def test_a_newer_list_on_an_older_engine_still_copies(self):
        real = ui.engine.brief_parts
        fields = ui.engine.BRIEF_FIELDS

        def old_shape(b, row=None):             # the engine before fields
            return real(b, row)
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        ui.engine.brief_parts = old_shape
        del ui.engine.BRIEF_FIELDS
        try:
            async with app.run_test(size=(300, 50)) as pilot:
                await self.enter(pilot)
                await pilot.press("down")
                await pilot.pause()
                app.paint_detail()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(adapter.copied, ["liveapp"])
        finally:
            ui.engine.brief_parts = real
            ui.engine.BRIEF_FIELDS = fields

    async def test_a_field_that_goes_is_followed_by_the_next_even_if_its_value_is_elsewhere(self):
        collector = FakeCollector()
        brief = collector.brief_value
        collector.brief_value = dict(brief, title="liveapp-b2")      # the title says the name
        app = self.app(collector=collector)
        async with app.run_test(size=(300, 50)) as pilot:
            await self.enter(pilot)
            box = app.query_one("#brief")
            for _ in range(30):                                       # to the name, on the aka line
                await pilot.press("down")
                await pilot.pause()
                if self.lit(app) == ["liveapp-b2"] and box.fields.get(box.lit) == "name":
                    break
            app.collector.brief_value = dict(app.collector.brief_value,
                                             aka=dict(brief["aka"], name=""))
            app.paint_detail()
            await pilot.pause()
            self.assertEqual(self.lit(app), ["/Users/x/liveapp"], "the cwd, not up to the title")


class TestTheDetailThatCoversTheWindow(UiTest):
    """Below UI_WIDE the detail covers the list. Browsing a list you cannot see
    is no use there: → opens the detail with the keys in it, one ← closes it."""

    lit = TestTheKeysGoIntoTheDetail.lit

    async def test_right_opens_it_with_the_keys_on_the_first_value(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertIs(app.focused, app.query_one("#brief"))
            self.assertEqual(self.lit(app), ["aaaa"])
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(self.lit(app), ["liveapp"])
            self.assertEqual(app.selected, LIVE["sessionId"], "no hidden list moved")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(adapter.copied, ["liveapp"])
            self.assertEqual(adapter.asked, [])

    async def test_one_left_closes_it(self):
        for key in ("left", "escape"):
            with self.subTest(key=key):
                app = self.app()
                async with app.run_test(size=(100, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("right")
                    await pilot.pause()
                    await pilot.press(key)
                    await pilot.pause()
                    await pilot.pause()
                    self.assertFalse(app.query_one("#detail").display)
                    self.assertTrue(app.query_one("#list").display)
                    self.assertIsInstance(app.focused, ui.Row)
                    self.assertEqual(app.selected, LIVE["sessionId"])

    async def test_a_window_that_narrows_gives_the_detail_the_keys(self):
        app = self.app()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertIsInstance(app.focused, ui.Row)                      # control
            await pilot.resize_terminal(100, 40)
            await pilot.pause(0.3)
            self.assertIs(app.focused, app.query_one("#brief"))
            self.assertEqual(self.lit(app), ["aaaa"])

    async def test_side_by_side_keeps_the_finder_keys(self):                  # control
        app = self.app()
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertIsInstance(app.focused, ui.Row)
            self.assertEqual(self.lit(app), [])

    async def test_a_refresh_never_takes_the_keys_out_of_the_search_box(self):
        app = self.app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            box = app.query_one("#search")
            self.assertIs(app.focused, box)                                 # control
            app.detail_open, app.detail_mode = True, "brief"
            app.paint_detail()                  # the refresh, with the detail up
            await pilot.pause()
            self.assertIs(app.focused, box, "you are typing")

    async def test_the_processes_screen_over_a_narrow_window_stays_whole(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            await pilot.pause()
            detail = app.query_one("#detail")
            self.assertTrue(app.detail_open)
            self.assertEqual(app.detail_mode, "procs")
            self.assertEqual(detail.display, app.detail_open)
            chosen = app.selected
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app.selected, chosen, "↓ scrolls the processes, no hidden list")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(adapter.asked, [], "no jump to a session you cannot see")
            self.assertIn("Esc for the list", self.screen_text(app))
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.detail_open)
            self.assertFalse(detail.display)
            self.assertTrue(app.query_one("#list").display)

    async def test_a_left_that_races_a_rebuild_closes_the_detail_whole(self):
        new = row("dddd4444-0000-4000-8000-000000000004", "busy", title="New one")
        app = self.app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            self.assertIs(app.focused, app.query_one("#brief"))                # control
            app.show(ui.Fleet([LIVE, BUSY, new], True, "12:00:01"))
            app.query_one("#brief").action_leave()  # ← before the rebuild has painted
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()
            self.assertFalse(app.detail_open)
            self.assertFalse(app.query_one("#detail").display)
            self.assertTrue(app.query_one("#list").display)

    async def test_after_a_search_the_narrow_detail_keeps_the_keys(self):
        app = self.app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("i", "enter")
            await pilot.pause()
            await pilot.pause()
            self.assertTrue(app.query_one("#detail").display, "the detail stays up")
            self.assertIsNot(type(app.focused), ui.Row, "not on a list you cannot see")
            chosen = app.selected
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app.selected, chosen)


class TestOneCollectAtATime(UiTest):
    """2026-09-29: a collect stuck on a slow iTerm2 did not stop the next tick
    from starting another - `exclusive` cancels the worker, not its thread - and
    each one sent iTerm2 another Apple Event. One collect runs at a time; a look
    asked for meanwhile runs once when it finishes, so no change is lost."""

    class Blocking(FakeCollector):
        def __init__(self):
            super().__init__()
            self.go = threading.Event()
            self.lock = threading.Lock()
            self.running = self.most = 0

        def fleet(self):
            with self.lock:
                self.running += 1
                self.most = max(self.most, self.running)
            self.go.wait(10)
            with self.lock:
                self.running -= 1
            return super().fleet()

    async def settle(self, pilot, collector, calls):
        for _ in range(100):
            await pilot.pause(0.02)
            if collector.calls >= calls and collector.running == 0:
                return

    async def test_ticks_during_a_slow_collect_start_no_second_one(self):
        collector = self.Blocking()
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            for _ in range(3):
                app.collect()
            await pilot.pause(0.1)
            self.assertEqual(collector.most, 1)
            collector.go.set()
            await self.settle(pilot, collector, 2)
            await pilot.pause(0.2)
            self.assertEqual(collector.most, 1)
            self.assertEqual(collector.calls, 2, "the asks made meanwhile run once, after")

    async def test_a_collect_after_the_last_one_ended_runs(self):        # control
        collector = self.Blocking()
        collector.go.set()
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await self.settle(pilot, collector, 1)
            app.collect()
            await self.settle(pilot, collector, 2)
            self.assertEqual(collector.calls, 2)


class TestARefreshAfterAKillWaitsItsTurn(UiTest):
    """After a kill the list reads the world again - through collect(), so it
    never runs beside a tick's collect on the same cache."""

    class Box:
        def show_result(self, lines):
            self.lines = lines

    async def test_a_refresh_after_a_kill_does_not_run_beside_a_collect(self):
        collector = TestOneCollectAtATime.Blocking()
        collector.kill_loops = lambda row: ["killed"]
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            app.loop_killing(self.Box(), dict(LIVE))
            await pilot.pause(0.2)
            self.assertEqual(collector.most, 1)
            collector.go.set()
            for _ in range(100):
                await pilot.pause(0.02)
                if collector.calls >= 2 and collector.running == 0:
                    break
            self.assertEqual(collector.most, 1)
            self.assertGreaterEqual(collector.calls, 2, "the refresh still happened")


SECRET = "/Users/x/secret-project/.env"


class TestTheCollectorPassesTheReportOn(unittest.TestCase):
    """The list learns from the report whether an iTerm2 session missed its
    pane: the real Collector must hand it on to reopen_saved (2026-09-30)."""

    def test_the_report_reaches_reopen_saved(self):
        import ccwho as runner

        def reopen(path=None, report=None):
            report["iterm_windows"] = 2
            return "reopened 2: all in new windows"
        report = {}
        with mock.patch.object(runner, "reopen_saved", side_effect=reopen):
            said = ui.Collector().restore("/r/x.json", report=report)
        self.assertEqual((said, report), ("reopened 2: all in new windows", {"iterm_windows": 2}))


class TestAnErrorSaysItsTypeOnly(unittest.TestCase):
    """An error's text can hold a path or a command line: a message says what
    failed and the error's type, never its text (the rule of the kill path,
    2026-09-29, for every message the list shows)."""

    def check(self, said, words):
        self.assertIn(words, said)
        self.assertNotIn(SECRET, said)

    def test_the_fleet_read(self):
        # fleet() reloads the engine first: a reload would put the real collect back
        with mock.patch.object(ui.engine, "reload_all"), \
                mock.patch.object(ui.engine, "collect", side_effect=OSError(SECRET)):
            fleet = ui.Collector().fleet()
        self.check(fleet.error, "could not read the fleet (OSError)")

    def test_the_engine_reload(self):
        c = ui.Collector()
        with mock.patch.object(ui.engine, "reload_all", side_effect=RuntimeError(SECRET)):
            c.reload()
        self.check(c.reload_error, "engine reload failed (RuntimeError)")

    def test_a_reopen(self):
        import ccwho as runner
        with mock.patch.object(runner, "reopen_saved", side_effect=OSError(SECRET)):
            said = ui.Collector().restore("/r/x.json")
        self.check(said, "could not reopen (OSError)")

    def test_iterm2_and_pbcopy(self):
        import ccwho as runner
        a = ui.Adapter()
        with mock.patch.object(subprocess, "run", side_effect=OSError(SECRET)), \
                mock.patch.object(runner, "window_app", return_value=ui.engine.terms.ITERM2):
            self.check(a.attach("claude attach aaaa1111"), "could not reach iTerm2 (OSError)")
            self.check(a.focus({"tty": "ttys022"}), "could not reach iTerm2 (OSError)")
            self.check(a.copy("x"), "could not run pbcopy (OSError)")



class TestTheListJumpsToTheAppThatShowsTheRow(unittest.TestCase):
    """Enter on a row asks the app whose tab shows it (terms.focus): a
    Terminal.app row never asks iTerm2 (D9)."""

    def test_a_terminal_app_row(self):
        calls = []
        with mock.patch.object(subprocess, "run", side_effect=lambda argv, **k: calls.append(argv) or
                               subprocess.CompletedProcess(argv, 0, stdout="focused /dev/ttys050\n")):
            said = ui.Adapter().focus({"tty": "ttys050", "terminal": "terminal"})
        self.assertEqual(said, "focused /dev/ttys050")
        # Terminal.app's own script, the tty its argument: not whatever
        # jump_args says now, which is what a mistake there would change
        self.assertEqual(calls, [["osascript", "-e", ui.engine.terms._TERMINAL_JUMP, "/dev/ttys050"]])

    def test_an_iterm2_row(self):                                                  # control
        calls = []
        with mock.patch.object(subprocess, "run", side_effect=lambda argv, **k: calls.append(argv) or
                               subprocess.CompletedProcess(argv, 0, stdout="focused /dev/ttys024\n")):
            ui.Adapter().focus({"tty": "ttys024", "terminal": "iterm2"})
        self.assertEqual(calls, [["osascript", ui.engine.terms.HERE + "/jump.applescript", "/dev/ttys024"]])

    def test_a_tty_no_app_shows_asks_no_app(self):
        with mock.patch.object(subprocess, "run") as run:
            said = ui.Adapter().focus({"tty": "ttys042", "terminal": ""})
        run.assert_not_called()
        self.assertIn("no terminal app", said)



class TestTheListAttachesInTheAppTheSessionWasIn(unittest.TestCase):
    """Enter on a running session with no window opens one (`claude attach`)
    in the app ccwho.window_app picks for its session (D6)."""

    def test_a_terminal_app_window(self):
        import ccwho as runner
        calls = []
        with mock.patch.object(runner, "window_app", return_value=ui.engine.terms.TERMINAL) as pick, \
                mock.patch.object(subprocess, "run", side_effect=lambda argv, **k: calls.append(argv) or
                                  subprocess.CompletedProcess(argv, 0, stdout="")):
            said = ui.Adapter().attach("claude attach aaaa1111", row={"sessionId": "aaaa1111"})
        pick.assert_called_once_with("aaaa1111")
        self.assertEqual(calls, [["osascript", "-e",
                                  ui.engine.terms.TERMINAL.run_script("claude attach aaaa1111")]])
        self.assertEqual(said, "attached it in a new window")


class TestAnErrorOnScreenSaysItsTypeOnly(UiTest):
    async def test_a_copy_that_raised(self):
        class Broken(FakeAdapter):
            def copy(self, text):
                raise OSError(SECRET)
        app = self.app(adapter=Broken())
        async with app.run_test(size=(300, 50)) as pilot:
            await pilot.pause()
            app.action_copy_value("x")
            await pilot.pause()
            self.assertIn("could not copy (OSError)", app.status)
            self.assertNotIn(SECRET, app.status)

    async def test_the_saves_read(self):
        def broken(s, live_ids=()):
            raise OSError(SECRET)
        real = FakeCollector.save_points
        FakeCollector.save_points = broken
        self.addCleanup(setattr, FakeCollector, "save_points", real)
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("o")
            await app.workers.wait_for_complete()
            await pilot.pause()
            header = str(app.query_one("#header").content)
            self.assertIn("could not read the saves (OSError)", header)
            self.assertNotIn(SECRET, header)


class TestTheSpawnRuleOnTheListsPython(unittest.TestCase):
    """The list sends launches (o) on its own Python: the spawn rule ccwho
    reads from CPython's frames and fields sorts errors there too."""

    def test_it_sorts(self):
        import os
        import shutil
        import sys
        import tempfile
        import ccwho
        import testkit
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        r = subprocess.run([sys.executable, "-c", testkit.SPAWN_RULE_CHECK,
                            os.path.dirname(os.path.abspath(ccwho.__file__))],
                           capture_output=True, text=True, timeout=120,
                           env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CCWHO_DIR=tmp))
        self.assertEqual(r.stdout.split(), testkit.SPAWN_RULE_SORTS, r.stderr[-800:])


class TestAFocusAfterTheListIsGone(UiTest):
    """A row can take the focus while the app is torn down, after the list is
    gone (a collect that ends as the list quits): not a choice, and no
    error."""

    async def test_it_selects_nothing(self):
        import types
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            row = list(app.query(ui.Row))[-1]
            before = app.selected
            self.assertNotEqual(before, row.row.get("sessionId", ""))      # a choice it could make
            await app.query_one("#list").remove()
            app.followed_focus(types.SimpleNamespace(widget=row))
            self.assertEqual(app.selected, before)

    async def test_while_it_is_hidden(self):
        # focus moved by hiding a container is not a choice either
        import types
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            row = list(app.query(ui.Row))[-1]
            before = app.selected
            app.query_one("#list").display = False
            app.followed_focus(types.SimpleNamespace(widget=row))
            self.assertEqual(app.selected, before)
            self.assertNotEqual(before, row.row.get("sessionId", ""))

    async def test_while_it_is_there(self):                                    # control
        import types
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            row = list(app.query(ui.Row))[-1]
            app.followed_focus(types.SimpleNamespace(widget=row))
            self.assertEqual(app.selected, row.row.get("sessionId", ""))


ENDED_SID = "10fe9603-70dd-459c-96dc-05a176242f56"
ENDED_ENTRY = {"sessionId": ENDED_SID, "project": "liveapp", "cwd": "/Users/x/liveapp",
               "title": "liveapp pull request 526 CI tests", "recap": "the CI flake, fixed",
               "recap_ts": "2026-09-27T16:00:00.000Z", "turns_since_recap": 2,
               "you_said": "fix the CI", "opened": "fix the CI", "last_any": "",
               "last_ts": "2026-09-27T16:29:54.470Z", "entrypoint": "cli",
               "path": f"/Users/x/.claude/projects/-Users-x-liveapp/{ENDED_SID}.jsonl"}


class TestTheSearchFindsASessionThatEnded(UiTest):
    """The owner stopped a session, copied its resume id wrong, and no part of
    that id found anything here (2026-10-04): the list had only the running
    sessions. While you search, the ended ones the session index knows show
    under them, under ENDED - found by any word `ccwho ls` finds them by, a
    part of the id too. Enter on one reopens it in a new window."""

    def collector(self, fleet=None):
        c = FakeCollector(fleet=fleet)
        c.index_value = {ENDED_SID: dict(ENDED_ENTRY)}
        return c

    async def until(self, pilot, done, wait=2.0):
        """The index is read off the UI thread: wait for what it brings."""
        for _ in range(int(wait / 0.05)):
            if done():
                return True
            await pilot.pause(0.05)
        return done()

    async def search(self, pilot, text):
        await pilot.press("slash")
        await pilot.pause()
        for ch in text:
            await pilot.press(ch)
        await pilot.pause()

    async def test_part_of_its_id_finds_it(self):
        app = self.app(collector=self.collector())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            self.assertNotIn("ENDED", self.screen_text(app), "not while you do not search")
            await self.search(pilot, "fe9603")
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)),
                            self.screen_text(app))
            text = self.screen_text(app)
            self.assertIn("liveapp pull request 526", text)
            self.assertIn("ended ", text)
            self.assertNotIn("no session matches", text)
            self.assertNotIn("Issue 362", text, "the running ones still narrow")

    async def test_escape_takes_it_away(self):
        app = self.app(collector=self.collector())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "fe9603")
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
            await pilot.press("escape")
            await pilot.pause()
            text = self.screen_text(app)
            self.assertNotIn("ENDED", text)
            self.assertIn("Issue 362", text)

    async def test_the_header_counts_the_running_ones_as_before(self):
        app = self.app(collector=self.collector())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "fe9603")
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
            header = str(app.query_one("#header").content)
            self.assertIn("0 of 2 + 1 ended", header)
            # the counts before the time are of the sessions that run
            self.assertNotIn("ended", header.split("12:00:00")[0])

    async def test_a_scan_that_finds_it_running_again_takes_it_out_of_ended(self):
        c = self.collector()
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "fe9603")
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
            # resumed somewhere: the next scan has it running
            live = row(ENDED_SID, "stopped", title="liveapp pull request 526 CI tests",
                       tab_title="")
            app.show(ui.Fleet([live, BUSY], True, "12:00:05"))
            await pilot.pause()
            self.assertNotIn("ENDED", self.screen_text(app))
            self.assertEqual([w.row.get("sessionId") for w in app.rows_on_screen()], [ENDED_SID])

    async def test_the_index_is_read_only_when_you_search(self):
        c = self.collector()
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause(0.2)
            self.assertEqual(c.index_calls, 0)
            await self.search(pilot, "fe96")
            self.assertTrue(await self.until(pilot, lambda: c.index_calls >= 1))

    async def read(self, pilot, app, c):
        """Until the index the search asked for is back: an absence checked
        before it is back checks nothing."""
        self.assertTrue(await self.until(
            pilot, lambda: c.index_calls >= 1 and not app.index_reading))
        await pilot.pause()

    async def test_a_search_it_does_not_match_shows_no_ended_group(self):     # control
        c = self.collector()
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "release")
            await self.read(pilot, app, c)
            text = self.screen_text(app)
            self.assertIn("Release queue", text)
            self.assertNotIn("ENDED", text)

    async def test_a_running_session_is_shown_once(self):
        live = row(ENDED_SID, "stopped", title="liveapp pull request 526 CI tests",
                   tab_title="")
        c = self.collector(fleet=ui.Fleet([live, BUSY], True, "12:00:00"))
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "fe9603")
            await self.read(pilot, app, c)
            self.assertNotIn("ENDED", self.screen_text(app))
            self.assertEqual([w.row.get("sessionId") for w in app.rows_on_screen()], [ENDED_SID])

    async def test_enter_reopens_it(self):
        adapter, c = FakeAdapter(), self.collector()
        app = self.app(collector=c, adapter=adapter)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "fe9603")
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
            await pilot.press("enter")          # out of the box, onto its row
            await pilot.pause()
            await pilot.press("enter")
            self.assertTrue(await self.until(pilot, lambda: c.reopened))
            self.assertEqual(c.reopened, [ENDED_SID])
            self.assertEqual(adapter.asked, [], "no window to go to: none is asked for")
            self.assertEqual(adapter.attached, [])
            self.assertTrue(await self.until(
                pilot, lambda: "reopened" in str(app.query_one("#header").content)))

    async def test_enter_on_a_running_one_still_goes_to_it(self):             # control
        adapter, c = FakeAdapter(), self.collector()
        app = self.app(collector=c, adapter=adapter)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "release")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual(adapter.asked, [BUSY["sessionId"]])
            self.assertIsNone(c.reopened)

    async def test_x_on_it_says_it_has_ended(self):
        app = self.app(collector=self.collector())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "fe9603")
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ui.ChoiceBox)
            header = str(app.query_one("#header").content)
            self.assertIn("it has ended", header)
            self.assertNotIn("end it in its window", header)

    async def test_an_index_that_cannot_be_read_leaves_the_search_working(self):
        c = self.collector()

        def broken():
            c.index_calls += 1
            raise OSError("index unreadable")
        c.index = broken
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "release")
            self.assertTrue(await self.until(pilot, lambda: c.index_calls >= 1))
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertIn("Release queue", self.screen_text(app))


class TestTheEndedSessionsArriveAfterYouType(UiTest):
    """The first index build reads every transcript (~11 s): the words are
    typed long before it lands. When it lands, the ENDED group shows - no
    further key needed."""

    async def test_the_index_landing_late_still_shows_them(self):
        gate = threading.Event()
        c = FakeCollector()
        c.index_value = {ENDED_SID: dict(ENDED_ENTRY)}
        real = c.index

        def slow():
            gate.wait(5)
            return real()
        c.index = slow
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            for ch in "fe9603":
                await pilot.press(ch)
            await pilot.pause(0.2)
            self.assertNotIn("ENDED", self.screen_text(app), "not read yet")
            gate.set()
            for _ in range(40):
                if "ENDED" in self.screen_text(app):
                    break
                await pilot.pause(0.05)
            self.assertIn("ENDED", self.screen_text(app))

    async def test_while_it_is_read_nothing_matching_is_not_said_yet(self):
        # eleven seconds of "no session matches" is wrong about the one
        # session you are looking for
        gate = threading.Event()
        c = FakeCollector()
        real = c.index

        def slow():
            gate.wait(5)
            return real()
        c.index = slow
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            for ch in "zzzz":
                await pilot.press(ch)
            await pilot.pause(0.2)
            text = self.screen_text(app)
            gate.set()
            self.assertIn("reading the ended sessions", text)
            self.assertNotIn("no session matches", text)
            for _ in range(40):
                if "no session matches" in self.screen_text(app):
                    break
                await pilot.pause(0.05)
            self.assertIn("no session matches 'zzzz'", self.screen_text(app),
                          "once it is read - and all of what you typed")
            self.assertNotIn("reading the ended sessions", self.screen_text(app))

    async def test_an_index_that_cannot_be_read_says_nothing_matches(self):
        c = FakeCollector()

        def broken():
            c.index_calls += 1
            raise OSError("index unreadable")
        c.index = broken
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            for ch in "zzzz":
                await pilot.press(ch)
            for _ in range(40):
                if "no session matches" in self.screen_text(app):
                    break
                await pilot.pause(0.05)
            self.assertIn("no session matches", self.screen_text(app))
            self.assertNotIn("reading the ended sessions", self.screen_text(app))


class TestTheCollectorReadsTheIndexAndReopens(unittest.TestCase):
    """The list's real doors to ccwho's own index and `open`."""

    def test_it_reads_the_index_quietly(self):
        import ccwho as runner
        asked = []
        with mock.patch.object(runner, "fresh_index",
                               lambda quiet=False: asked.append(quiet) or {"x": {}}):
            self.assertEqual(ui.Collector().index(), {"x": {}})
        self.assertEqual(asked, [True], "a first build prints to a terminal the list owns")

    def test_it_reopens_by_the_session_id(self):
        import ccwho as runner
        asked = []
        with mock.patch.object(runner, "reopen_ended",
                               lambda sid: asked.append(sid) or "reopened it"):
            said = ui.Collector().reopen_ended({"sessionId": ENDED_SID})
        self.assertEqual((asked, said), ([ENDED_SID], "reopened it"))

    def test_a_reopen_that_raises_is_said_by_its_type(self):
        import ccwho as runner

        def boom(sid):
            raise RuntimeError("/Users/x/secret")
        with mock.patch.object(runner, "reopen_ended", boom):
            said = ui.Collector().reopen_ended({"sessionId": ENDED_SID})
        self.assertEqual(said, "could not reopen (RuntimeError)")


def ended_entry(sid, **kw):
    return dict(ENDED_ENTRY, sessionId=sid,
                path=f"/Users/x/.claude/projects/-Users-x-liveapp/{sid}.jsonl", **kw)


class GatedFleetCollector(FakeCollector):
    """The first scan waits for the test: the list as it is before it lands."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.gate = threading.Event()

    def fleet(self):
        self.gate.wait(5)
        return super().fleet()


class TestEndedRowsAfterTheFirstReview(UiTest):
    """Review 1 of the search-ended slice: an ENDED row only from a list that
    knows what runs; Enter on one blinks and holds the list, as a jump does;
    the footer names the reopen; one index read at a time; a failed read keeps
    the last good one; the header counts every ended match."""

    _T = TestTheSearchFindsASessionThatEnded
    until, search, read = _T.until, _T.search, _T.read

    def collector(self, fleet=None, cls=FakeCollector):
        c = cls(fleet=fleet)
        c.index_value = {ENDED_SID: dict(ENDED_ENTRY),
                         BUSY["sessionId"]: ended_entry(BUSY["sessionId"], title="Release queue")}
        return c

    async def test_a_list_that_could_not_be_read_shows_no_ended_row(self):
        c = self.collector(fleet=ui.Fleet([], False, "", "could not read the fleet (OSError)"))
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "bbbb2222")
            await self.read(pilot, app, c)
            # BUSY may well run: nothing says it does not
            self.assertNotIn("ENDED", self.screen_text(app))
            self.assertNotIn("ended", str(app.query_one("#header").content))

    async def test_before_the_first_scan_no_ended_row_and_after_it_one(self):
        c = self.collector(fleet=ui.Fleet([BUSY], True, "12:00:00"), cls=GatedFleetCollector)
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            try:
                await pilot.pause()
                await self.search(pilot, "bbbb2222")
                await self.read(pilot, app, c)
                self.assertNotIn("ENDED", self.screen_text(app), "it runs - not known yet")
            finally:
                c.gate.set()
            await pilot.pause()
            app.query_one("#search").value = "fe9603"
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)),
                            "control: the scan is in, and fe96 does not run")

    async def test_enter_blinks_the_row_and_holds_the_list_until_it_is_reopened(self):
        gate, c = threading.Event(), self.collector()
        real = c.reopen_ended

        def slow(row):
            gate.wait(5)
            return real(row)
        c.reopen_ended = slow
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            try:
                await pilot.pause()
                await self.search(pilot, "fe9603")
                self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                self.assertEqual(app.acting, ENDED_SID)
                widget = next(w for w in app.rows_on_screen()
                              if w.row.get("sessionId") == ENDED_SID)
                self.assertTrue(widget.has_class("acting"))
                later = ui.Fleet([LIVE, BUSY], True, "12:00:09")
                app.show(later)
                self.assertIs(app.held, later, "nothing moves while it opens")
            finally:
                gate.set()
            self.assertTrue(await self.until(pilot, lambda: not app.acting))
            self.assertIsNone(app.held)

    async def test_the_status_says_the_short_id_and_the_title(self):
        c = self.collector()
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "fe9603")
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            header = lambda: str(app.query_one("#header").content)
            self.assertTrue(await self.until(pilot, lambda: "reopened" in header()))
            self.assertIn("reopened 10fe", header())
            self.assertIn("liveapp pull request 526", header())
            self.assertNotIn(ENDED_SID, header())

    def enter_says(self, app):
        """What the footer on screen says Enter does: its drawn keys, not
        active_bindings - that asks check_action afresh on every read."""
        said = [k.description for k in app.query("FooterKey") if k.key == "enter"]
        return said[0] if len(said) == 1 else said or None

    async def test_the_footer_says_enter_reopens_an_ended_row(self):
        app = self.app(collector=self.collector())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(self.enter_says(app), "go to")                    # control
            await self.search(pilot, "fe9603")
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
            await pilot.press("enter")              # out of the box, onto the row
            await pilot.pause()
            self.assertEqual(self.enter_says(app), "resume")

    async def test_a_click_on_an_ended_row_reopens_it(self):
        adapter, c = FakeAdapter(), self.collector()
        app = self.app(collector=c, adapter=adapter)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "fe9603")
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
            widget = next(w for w in app.rows_on_screen() if w.row.get("sessionId") == ENDED_SID)
            await pilot.click(widget)
            self.assertTrue(await self.until(pilot, lambda: c.reopened))
            self.assertEqual(c.reopened, [ENDED_SID])
            self.assertEqual((adapter.asked, adapter.attached), ([], []))

    async def test_the_header_counts_every_ended_match_not_the_ten_shown(self):
        c = FakeCollector()
        c.index_value = {sid: ended_entry(sid, title="old work") for sid in
                         (f"{i:08x}-0000-4000-8000-000000000000" for i in range(12))}
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "old work")
            await self.read(pilot, app, c)
            self.assertIn("+ 12 ended", str(app.query_one("#header").content))

    async def test_a_read_that_fails_keeps_the_last_good_index(self):
        c = self.collector()
        good = c.index

        def then_broken():
            c.index = broken
            return good()

        def broken():
            c.index_calls += 1
            raise OSError("index unreadable")
        c.index = then_broken
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "fe9603")
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
            await pilot.press("escape")
            await pilot.pause()
            await self.search(pilot, "fe9603")
            self.assertTrue(await self.until(pilot, lambda: c.index_calls >= 2
                                             and not app.index_reading))
            await pilot.pause()
            self.assertIn("ENDED", self.screen_text(app))

    async def test_a_second_search_while_the_index_is_read_does_not_read_it_again(self):
        gate, c = threading.Event(), FakeCollector()
        real = c.index

        def slow():
            c.entered = getattr(c, "entered", 0) + 1
            gate.wait(5)
            return real()
        c.index = slow
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            try:
                await pilot.pause()
                await self.search(pilot, "zz")
                await pilot.press("escape")
                await pilot.pause()
                await self.search(pilot, "zz")
                await pilot.pause(0.2)
                self.assertEqual(getattr(c, "entered", 0), 1, "one build at a time")
                self.assertIn("reading the ended sessions", self.screen_text(app))
            finally:
                gate.set()
            self.assertTrue(await self.until(
                pilot, lambda: "no session matches" in self.screen_text(app)))

    async def test_a_list_read_without_its_source_shows_no_ended_row(self):
        # read, at a time, but `claude agents` did not answer: what runs is not known
        c = self.collector(fleet=ui.Fleet([], False, "12:00:00",
                                          "cannot read the session list - run `ccwho doctor`"))
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "bbbb2222")
            await self.read(pilot, app, c)
            self.assertNotIn("ENDED", self.screen_text(app))

    async def test_the_footer_follows_a_reopened_row_out_of_ended(self):
        app = self.app(collector=self.collector())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "fe9603")
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(self.enter_says(app), "resume")
            # it runs again: the scan has it, the same widget moves out of ENDED
            live = row(ENDED_SID, "stopped", title="liveapp pull request 526 CI tests",
                       tab_title="")
            app.show(ui.Fleet([live, BUSY], True, "12:00:09"))
            await pilot.pause()
            await pilot.pause()
            self.assertNotIn("ENDED", self.screen_text(app))
            self.assertEqual(self.enter_says(app), "go to")


class TestEndedRowsAfterTheSecondReview(UiTest):
    """Review 2 of the search-ended slice: why a reopen did not happen can be
    read in a narrow window; a second Enter does not end the first reopen's
    blink; a session the index finds by what it was about stays when it runs;
    an index read that never returns does not block the next; the guards and
    the look a jump has, a reopen has too."""

    _T = TestTheSearchFindsASessionThatEnded
    until, search, read = _T.until, _T.search, _T.read
    collector = _T.collector

    async def ended_row_selected(self, pilot, app, q="fe9603"):
        await pilot.pause()
        await self.search(pilot, q)
        self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
        await pilot.press("enter")              # out of the box, onto the row
        await pilot.pause()

    def drawn(self, widget):
        return "\n".join(widget.render_line(y).text for y in range(widget.size.height))

    async def test_a_failed_reopen_says_why_where_a_narrow_window_shows_it(self):
        c = self.collector()
        c.reopen_answer = (f"not reopening {ENDED_SID} - cwd is gone:"
                           " /private/var/folders/ab/T/tmp1")
        app = self.app(collector=c)
        async with app.run_test(size=(100, 40)) as pilot:
            await self.ended_row_selected(pilot, app)
            await pilot.press("enter")
            banner = app.query_one("#banner")
            self.assertTrue(await self.until(pilot, lambda: banner.display))
            await pilot.pause()
            shown = self.drawn(banner) + self.drawn(app.query_one("#header"))
            self.assertIn("cwd is gone", self.drawn(banner))
            self.assertIn("10fe", shown)
            self.assertNotIn(ENDED_SID, shown, "the short id, as the row shows it")
            self.assertFalse(app.acting, "it is over")

    async def test_a_reopen_that_worked_leaves_the_warning_line_alone(self):      # control
        app = self.app(collector=self.collector())
        async with app.run_test(size=(100, 40)) as pilot:
            await self.ended_row_selected(pilot, app)
            await pilot.press("enter")
            self.assertTrue(await self.until(pilot, lambda: "reopened" in app.status))
            await pilot.pause()
            self.assertFalse(app.query_one("#banner").display)

    async def test_a_second_enter_while_it_reopens_does_not_end_the_blink(self):
        gate, c = threading.Event(), self.collector()
        real, calls = c.reopen_ended, []

        def slow(row):
            calls.append(row.get("sessionId"))
            gate.wait(5)
            return real(row)
        c.reopen_ended = slow
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            try:
                await self.ended_row_selected(pilot, app)
                await pilot.press("enter")
                await pilot.pause(0.2)
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertEqual(calls, [ENDED_SID], "one reopen")
                self.assertEqual(app.acting, ENDED_SID, "the first still runs")
                later = ui.Fleet([LIVE, BUSY], True, "12:00:09")
                app.show(later)
                self.assertIs(app.held, later)
            finally:
                gate.set()
            self.assertTrue(await self.until(pilot, lambda: not app.acting))

    async def test_a_session_found_by_what_it_was_about_stays_when_it_runs(self):
        c = FakeCollector()
        c.index_value = {ENDED_SID: dict(ENDED_ENTRY, you_said="flamingo deploy")}
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "flamingo")
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
            # reopened: the scan has it running - no word of "flamingo" on its row
            live = row(ENDED_SID, "stopped", title="liveapp pull request 526 CI tests",
                       tab_title="")
            app.show(ui.Fleet([live, BUSY], True, "12:00:05"))
            await pilot.pause()
            await pilot.pause()
            self.assertEqual([w.row.get("sessionId") for w in app.rows_on_screen()], [ENDED_SID])
            self.assertNotIn("ENDED", self.screen_text(app))
            self.assertIn("STOPPED", self.screen_text(app), "in its running group")

    async def test_an_index_read_that_never_returns_is_asked_again_after_a_while(self):
        gate, c, calls = threading.Event(), FakeCollector(), []

        def hung():
            calls.append(1)
            gate.wait(5)
            return {}
        c.index = hung
        app = self.app(collector=c)
        with mock.patch.object(ui, "INDEX_PATIENCE", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "zz")
                    await pilot.pause(0.3)
                    await pilot.press("escape")
                    await pilot.pause()
                    await self.search(pilot, "zz")
                    await pilot.pause(0.2)
                    self.assertEqual(len(calls), 2, "past its patience, the read is asked again")
                finally:
                    gate.set()

    async def test_narrow_with_the_process_screen_enter_does_not_reopen(self):
        # the process screen covers the list: Enter would act on a row you cannot see
        c = self.collector()
        app = self.app(collector=c)
        async with app.run_test(size=(100, 40)) as pilot:
            await self.ended_row_selected(pilot, app)
            self.assertTrue(app.on_ended())
            await pilot.press("p")
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(app.detail_mode, "procs")
            await pilot.press("enter")
            await pilot.pause(0.3)
            self.assertIsNone(c.reopened)
            self.assertIn("Esc for the list", app.status)

    async def test_it_looks_again_soon_after_a_reopen(self):
        c = self.collector()
        app = self.app(collector=c)
        with mock.patch.object(ui, "AFTER_A_JUMP", 0.05):
            async with app.run_test(size=(140, 40)) as pilot:
                await self.ended_row_selected(pilot, app)
                await pilot.pause(0.3)
                before = c.calls
                await pilot.press("enter")
                self.assertTrue(await self.until(pilot, lambda: c.reopened))
                self.assertTrue(await self.until(pilot, lambda: c.calls > before, wait=1.0),
                                "a scan soon after the reopen")

    async def test_a_new_index_with_the_same_words_and_list_is_searched_anew(self):
        # the second read comes back after the words are typed: the same search
        # text and the same scan, searched in the last index until then
        c, gate = self.collector(), threading.Event()
        real = c.index
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            try:
                await pilot.pause()
                await self.search(pilot, "fe9603")
                await self.read(pilot, app, c)
                await pilot.press("escape")
                await pilot.pause()
                new = "cccc3333-0000-4000-8000-000000000003"
                c.index_value = dict(c.index_value,
                                     **{new: ended_entry(new, title="notes on fe9603")})

                def later():
                    gate.wait(5)
                    return real()
                c.index = later
                await self.search(pilot, "fe9603")
                await pilot.pause(0.2)
                self.assertIn("ENDED", self.screen_text(app), "from the last index")
                self.assertNotIn("notes on fe9603", self.screen_text(app), "not read yet")
            finally:
                gate.set()
            self.assertTrue(await self.until(pilot, lambda: "notes on fe9603" in self.screen_text(app)))

    async def test_after_a_reopen_lands_the_next_enter_reopens_again(self):
        c = self.collector()
        c.reopen_answer = f"not reopening {ENDED_SID} - cwd is gone: /x"
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.ended_row_selected(pilot, app)
            await pilot.press("enter")
            self.assertTrue(await self.until(pilot, lambda: c.reopened and not app.acting))
            await pilot.press("enter")
            self.assertTrue(await self.until(pilot, lambda: len(c.reopened or []) == 2),
                            "the first one is over: the next Enter is not held back")


def attribute_clashes(source, namespace):
    """Every `self.<name> = ...` in a class of `source` where <name> is a method
    of that class - its own, or one it inherits (Textual's): `namespace` holds
    the classes, built."""
    import ast
    clashes = []
    for cls in (n for n in ast.parse(source).body if isinstance(n, ast.ClassDef)):
        own = {n.name for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        real = namespace.get(cls.name)
        methods = own | {n for n in dir(real) if not n.startswith("__")
                         and callable(getattr(real, n, None))}
        for node in ast.walk(cls):
            targets = (node.targets if isinstance(node, ast.Assign) else
                       [node.target] if isinstance(node, (ast.AugAssign, ast.AnnAssign)) else [])
            for t in targets:
                for e in (t.elts if isinstance(t, ast.Tuple) else [t]):
                    if (isinstance(e, ast.Attribute) and isinstance(e.value, ast.Name)
                            and e.value.id == "self" and e.attr in methods):
                        clashes.append(f"{cls.name}.{e.attr} (line {node.lineno})")
    return clashes


class TestNoAttributeHidesAMethod(UiTest):
    """Review 3: `self.reopening = ""` hid the worker `reopening(path)` that `o`
    starts - every reopen of a save took the list down. No class here may set
    an attribute named as one of its methods, its own or Textual's."""

    def test_no_self_assignment_is_named_as_a_method(self):
        with open(ui.__file__) as fh:
            self.assertEqual(attribute_clashes(fh.read(), vars(ui)), [])

    def test_the_check_sees_an_own_and_a_textual_clash(self):                 # control
        src = ("from textual.app import App\n"
               "class A:\n    def go(self):\n        self.go = 1\n"
               "class B(App):\n    def x(self):\n        self.notify = 1\n")
        built = {}
        exec(src, built)
        self.assertEqual(attribute_clashes(src, built), ["A.go (line 4)", "B.notify (line 7)"])

    async def test_once_running_no_attribute_hides_a_method(self):
        # what the source cannot show: a name set by setattr, or by Textual
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            kind = type(app)
            self.assertEqual(sorted(n for n in vars(app) if not n.startswith("__")
                                    and callable(getattr(kind, n, None))), [])


class TestEndedRowsAfterTheThirdReview(UiTest):
    """Review 3 of the search-ended slice: `o` reopens a save after an ended
    reopen; a path in a reason keeps the whole id; a read that lands after a
    newer one does not replace it."""

    _T = TestTheSearchFindsASessionThatEnded
    until, search, read = _T.until, _T.search, _T.read
    collector = _T.collector
    ended_row_selected = TestEndedRowsAfterTheSecondReview.ended_row_selected

    async def test_o_reopens_a_save_after_an_ended_reopen(self):
        c = self.collector()
        restored = []
        c.restore = lambda path=None, report=None: restored.append(path) or "reopened 1 session(s)"
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await self.ended_row_selected(pilot, app)
            await pilot.press("enter")
            self.assertTrue(await self.until(pilot, lambda: c.reopened and not app.acting))
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("o")
            for _ in range(4):
                await pilot.pause()
            await pilot.press("enter")
            self.assertTrue(await self.until(pilot, lambda: restored))
            self.assertEqual(restored, [POINTS[0]["path"]])
            self.assertTrue(app.is_running)

    async def test_a_path_in_the_reason_keeps_the_whole_id(self):
        c = self.collector()
        path = f"/Users/x/.ccwho/launching/{ENDED_SID}.json"
        c.reopen_answer = (f"not launching {ENDED_SID} - its claim file cannot be read:"
                           f" {path}. Remove it if no ccwho is launching it")
        app = self.app(collector=c)
        async with app.run_test(size=(200, 40)) as pilot:
            await self.ended_row_selected(pilot, app)
            await pilot.press("enter")
            self.assertTrue(await self.until(pilot, lambda: bool(app.note)))
            self.assertIn("not launching 10fe -", app.note)
            self.assertIn(path, app.note, "the file to remove, as it is on disk")

    async def test_a_late_read_does_not_replace_the_newer_index(self):
        c, first, calls = FakeCollector(), threading.Event(), []
        old = {ENDED_SID: dict(ENDED_ENTRY)}
        newer = "cccc3333-0000-4000-8000-000000000003"
        new = dict(old, **{newer: ended_entry(newer, title="notes on fe9603 later")})

        def index():
            calls.append(1)
            if len(calls) == 1:
                first.wait(5)
                return dict(old)
            return dict(new)
        c.index = index
        app = self.app(collector=c)
        with mock.patch.object(ui, "INDEX_PATIENCE", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "fe9603")
                    await pilot.pause(0.3)
                    await pilot.press("escape")
                    await pilot.pause()
                    await self.search(pilot, "fe9603")
                    self.assertTrue(await self.until(
                        pilot, lambda: "notes on fe9603 later" in self.screen_text(app)))
                finally:
                    first.set()
                await pilot.pause(0.3)
                await pilot.pause()
                self.assertIn("notes on fe9603 later", self.screen_text(app),
                              "the older read, landing last, must not take it away")

    async def test_a_late_read_does_not_end_the_newer_ones_reading(self):
        c, first, second, calls = FakeCollector(), threading.Event(), threading.Event(), []

        def index():
            calls.append(1)
            (first if len(calls) == 1 else second).wait(5)
            return {}
        c.index = index
        app = self.app(collector=c)
        with mock.patch.object(ui, "INDEX_PATIENCE", 0.3):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "zz")
                    await pilot.pause(0.4)              # past its patience
                    await pilot.press("escape")
                    await pilot.pause()
                    await self.search(pilot, "zz")      # a second read, within its own
                    self.assertEqual(len(calls), 2)
                    first.set()                         # the first lands, late
                    await pilot.pause(0.2)
                    # the flag, not reading(): that also counts the patience, and
                    # typing under load outlasts 0.3 s
                    self.assertTrue(app.index_reading, "the second is still out")
                finally:
                    first.set()
                    second.set()

    async def test_a_running_row_found_both_ways_shows_once(self):
        live = row(ENDED_SID, "stopped", title="liveapp flamingo", tab_title="")
        c = FakeCollector(fleet=ui.Fleet([live, BUSY], True, "12:00:00"))
        c.index_value = {ENDED_SID: dict(ENDED_ENTRY, you_said="flamingo deploy")}
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "flamingo")
            await self.read(pilot, app, c)
            self.assertEqual([w.row.get("sessionId") for w in app.rows_on_screen()], [ENDED_SID])
            self.assertIn("1 of 2", str(app.query_one("#header").content))


class TestEndedRowsAfterTheFourthReview(UiTest):
    """Review 4 of the search-ended slice: a list that cannot say what runs
    does not say "no session matches"; reads that never return do not fill the
    thread pool; Enter's word on an ended row is not `o`'s."""

    _T = TestTheSearchFindsASessionThatEnded
    until, search, read = _T.until, _T.search, _T.read
    collector = _T.collector
    enter_says = TestEndedRowsAfterTheFirstReview.enter_says

    async def test_an_unread_list_does_not_say_nothing_matches(self):
        c = self.collector(fleet=ui.Fleet([], False, "", "could not read the fleet (OSError)"))
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "fe9603")
            await self.read(pilot, app, c)
            text = self.screen_text(app)
            self.assertNotIn("no session matches", text, "it may well be there")
            self.assertIn("not read yet", text)

    async def test_a_read_list_with_no_match_still_says_so(self):              # control
        c = self.collector()
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "zzzz")
            await self.read(pilot, app, c)
            # once what was said is searched too: half a second after the last key
            self.assertTrue(await self.until(
                pilot, lambda: "no session matches 'zzzz'" in self.screen_text(app)))
            self.assertNotIn("not read yet", self.screen_text(app))

    async def test_reads_that_never_return_take_two_threads_at_most(self):
        gate, c, calls = threading.Event(), FakeCollector(), []

        def hung():
            calls.append(1)
            gate.wait(5)
            return {}
        c.index = hung
        app = self.app(collector=c)
        with mock.patch.object(ui, "INDEX_PATIENCE", 0.05):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    for _ in range(5):
                        await pilot.press("slash")
                        await pilot.pause(0.15)         # past its patience each time
                        await pilot.press("escape")
                        await pilot.pause()
                    self.assertEqual(len(calls), 2, "one read, and one more if it hangs")
                finally:
                    gate.set()

    async def test_enter_on_an_ended_row_is_not_named_as_o_is(self):
        c = self.collector()
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "fe9603")
            self.assertTrue(await self.until(pilot, lambda: "ENDED" in self.screen_text(app)))
            await pilot.press("enter")
            await pilot.pause()
            o_says = [k.description for k in app.query("FooterKey") if k.key == "o"]
            self.assertEqual(self.enter_says(app), "resume")
            self.assertNotIn(self.enter_says(app), o_says)

    async def test_reads_that_come_back_free_their_thread(self):              # control
        c = self.collector()
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            for n in (1, 2, 3):
                await pilot.press("slash")
                self.assertTrue(await self.until(
                    pilot, lambda: c.index_calls == n and not app.index_reading))
                await pilot.press("escape")
                await pilot.pause()
            self.assertEqual(c.index_calls, 3, "every search reads it again")


class TestEndedRowsAfterTheFifthReview(UiTest):
    """Review 5 of the search-ended slice: a list not read says so while the
    index reads too; a read that comes back stale, or was dropped before it
    ran, gives its slot back."""

    _T = TestTheSearchFindsASessionThatEnded
    until, search = _T.until, _T.search

    async def empty_text_while_the_index_reads(self, fleet=None, gated=False):
        gate = threading.Event()
        c = (GatedFleetCollector if gated else FakeCollector)(fleet=fleet)

        def index():
            gate.wait(5)
            return {}
        c.index = index
        app = self.app(collector=c)
        async with app.run_test(size=(140, 40)) as pilot:
            try:
                await pilot.pause()
                await self.search(pilot, "fe9603")
                await pilot.pause(0.2)
                return self.screen_text(app)
            finally:
                gate.set()
                if gated:
                    c.gate.set()

    async def test_an_unreadable_list_while_the_index_reads(self):
        text = await self.empty_text_while_the_index_reads(
            fleet=ui.Fleet([], False, "", "could not read the fleet (OSError)"))
        self.assertNotIn("no running session matches", text)
        self.assertNotIn("no session matches", text)
        self.assertIn("not read yet", text)

    async def test_the_first_scan_not_back_while_the_index_reads(self):
        text = await self.empty_text_while_the_index_reads(gated=True)
        self.assertNotIn("no running session matches", text)
        self.assertIn("not read yet", text)

    async def test_a_read_list_while_the_index_reads(self):                     # control
        text = await self.empty_text_while_the_index_reads()
        self.assertIn("no running session matches 'fe9603'", text)
        self.assertIn("reading the ended sessions", text)

    async def slash(self, pilot):
        await pilot.press("slash")
        await pilot.pause(0.15)
        await pilot.press("escape")
        await pilot.pause()

    async def test_a_stale_read_that_comes_back_gives_its_slot_back(self):
        first, second, rest, calls = (threading.Event(), threading.Event(),
                                      threading.Event(), [])

        def index():
            calls.append(1)
            (first if len(calls) == 1 else second if len(calls) == 2 else rest).wait(5)
            return {}
        c = FakeCollector()
        c.index = index
        app = self.app(collector=c)
        with mock.patch.object(ui, "INDEX_PATIENCE", 0.05):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.slash(pilot)
                    await self.slash(pilot)
                    self.assertEqual(len(calls), 2)
                    first.set()             # the stale one lands
                    second.set()            # then the latest
                    self.assertTrue(await self.until(pilot, lambda: not app.index_reading))
                    await pilot.pause(0.1)
                    await self.slash(pilot)
                    await self.slash(pilot)
                    self.assertEqual(len(calls), 4, "both came back: two more may go")
                finally:
                    first.set()
                    second.set()
                    rest.set()

    async def test_a_read_dropped_before_it_ran_gives_its_slot_back(self):
        import asyncio
        import concurrent.futures
        loop = asyncio.get_running_loop()
        loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
        blocker = threading.Event()
        c = FakeCollector()
        app = self.app(collector=c)
        with mock.patch.object(ui, "INDEX_PATIENCE", 0.05):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause(0.3)
                    held = loop.run_in_executor(None, blocker.wait, 5)   # the pool is full
                    await self.slash(pilot)         # read 1: waits for a thread
                    await self.slash(pilot)         # read 2: waits too
                    blocker.set()
                    await held
                    self.assertTrue(await self.until(pilot, lambda: not app.index_reading))
                    await pilot.pause(0.2)
                    self.assertEqual(app.index_out, 0, "nothing is out")
                finally:
                    blocker.set()


QUIT_WHILE_THE_INDEX_READS = """
import sys, threading
sys.path.insert(0, sys.argv[1])
import ccwho_ui as ui, test_ui
c = test_ui.FakeCollector()
c.index = lambda: threading.Event().wait(20) or {}      # a read that does not come back
app = ui.CcwhoUi(adapter=test_ui.FakeAdapter(), collector=c)

async def drive(pilot):
    await pilot.pause()
    await pilot.press("slash")
    await pilot.pause(0.3)
    await pilot.press("escape")
    await pilot.pause()
    await pilot.press("q")

app.run(headless=True, auto_pilot=drive)
print("quit", flush=True)
"""


class TestEndedRowsAfterTheSixthReview(UiTest):
    """Review 6 of the search-ended slice: `q` does not wait for an index read
    that has not come back; a read older than the latest, back while nothing
    newer has landed, shows its rows."""

    _T = TestTheSearchFindsASessionThatEnded
    until, search = _T.until, _T.search

    def test_q_does_not_wait_for_an_index_read(self):
        import os
        import sys
        import time
        here = os.path.dirname(os.path.abspath(ui.__file__))
        began = time.monotonic()
        done = subprocess.run([sys.executable, "-c", QUIT_WHILE_THE_INDEX_READS, here],
                              capture_output=True, text=True, timeout=12, cwd=here,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        took = time.monotonic() - began
        self.assertIn("quit", done.stdout, done.stderr[-500:])
        self.assertLess(took, 6, "q waited for the read")

    async def two_reads(self, which_first):
        first, second, calls = threading.Event(), threading.Event(), []
        old = {ENDED_SID: dict(ENDED_ENTRY)}
        newer = "cccc3333-0000-4000-8000-000000000003"
        new = dict(old, **{newer: ended_entry(newer, title="notes on fe9603 later")})

        def index():
            calls.append(1)
            mine = len(calls)               # this read's own number, kept
            (first if mine == 1 else second).wait(5)
            return dict(old if mine == 1 else new)
        c = FakeCollector()
        c.index = index
        app = self.app(collector=c)
        with mock.patch.object(ui, "INDEX_PATIENCE", 0.05):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await pilot.press("slash")
                    await pilot.pause(0.15)
                    await pilot.press("escape")
                    await pilot.pause()
                    await self.search(pilot, "fe9603")      # read 2, read 1 still out
                    self.assertEqual(len(calls), 2)
                    (first if which_first == 1 else second).set()
                    shown = await self.until(pilot, lambda: "ENDED" in self.screen_text(app))
                    self.assertTrue(shown, "back first, and nothing newer has landed")
                    self.assertEqual("notes on fe9603 later" in self.screen_text(app),
                                     which_first == 2)
                    (second if which_first == 1 else first).set()
                    await pilot.pause(0.3)
                    await pilot.pause()
                    # whatever the order, the newer read's rows are what stays
                    self.assertIn("notes on fe9603 later", self.screen_text(app))
                    self.assertFalse(app.index_reading)
                finally:
                    first.set()
                    second.set()

    async def test_an_older_read_back_first_shows_its_rows_until_the_newer(self):
        await self.two_reads(which_first=1)

    async def test_the_newer_read_back_first_stays(self):                       # control
        await self.two_reads(which_first=2)

    def test_a_read_back_when_the_list_is_not_running_says_nothing(self):
        # its thread outlives the list (q, restart): nobody to tell, and no
        # traceback on the terminal the list gave back
        app = ui.CcwhoUi(adapter=FakeAdapter(), collector=FakeCollector())
        app.index_out = 1
        app._read_index(1)          # no raise


class TestTheSearchFindsWhatWasSaid(UiTest):
    """The owner, 2026-10-05: "a keyword search on the session contents (a
    basic case independent grep)". Half a second after the last key, the list
    greps what was said in every session - your prompts and Claude's replies -
    in the background; the sessions only that finds show last, under SAID."""

    _T = TestTheSearchFindsASessionThatEnded
    until, read = _T.until, _T.read

    def collector(self, fleet=None, found=None):
        c = FakeCollector(fleet=fleet)
        c.index_value = {ENDED_SID: dict(ENDED_ENTRY)}       # no "flamingo" in its names
        c.grep_value = {"flamingo": {ENDED_SID}} if found is None else found
        return c

    async def search(self, pilot, text, app=None):
        """`/`, then the words in one step: pilot keys come slower than the
        test's short pause, and each gap would start a grep of its own."""
        await pilot.press("slash")
        await pilot.pause()
        (app or self.app_now).query_one("#search").value = text
        await pilot.pause()

    def app(self, collector=None, adapter=None):
        self.app_now = super().app(collector=collector, adapter=adapter)
        return self.app_now

    def calls(self, c):
        return [q for q, _ in c.grep_calls or []]

    def header(self, app):
        return str(app.query_one("#header").content)

    async def test_a_pause_after_typing_finds_a_session_by_what_was_said(self):
        c = self.collector()
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await self.search(pilot, "flamingo")
                self.assertTrue(await self.until(pilot, lambda: "SAID" in self.screen_text(app)))
                self.assertIn("liveapp pull request 526", self.screen_text(app))
                self.assertEqual(self.calls(c), ["flamingo"])

    async def test_typing_fast_greps_once_after_the_last_key(self):
        c = self.collector()
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.4):
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await pilot.press("slash")
                await pilot.pause()
                for ch in "flamingo":
                    await pilot.press(ch)           # keys well inside the pause
                self.assertEqual(self.calls(c), [], "not while you type")
                self.assertTrue(await self.until(pilot, lambda: self.calls(c)))
                await pilot.pause(0.5)
                self.assertEqual(self.calls(c), ["flamingo"])

    async def test_a_running_session_found_by_what_was_said_shows_under_said(self):
        c = self.collector(found={"flamingo": {BUSY["sessionId"]}})
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await self.search(pilot, "flamingo")
                self.assertTrue(await self.until(pilot, lambda: "Release queue" in self.screen_text(app)))
                self.assertEqual([w.row.get("sessionId") for w in app.rows_on_screen()],
                                 [BUSY["sessionId"]])
                text = self.screen_text(app)
                self.assertIn("SAID", text)
                self.assertNotIn("BUSY", text, "not in its group: no name of it matched")
                self.assertIn("0 of 2 + 1 said", self.header(app))

    async def test_a_said_row_shows_what_was_said_that_matched(self):
        # the owner, 2026-10-07: the session was in the list, its row about
        # something else - so it was not found
        said = {"text": "the flamingo build is green", "who": "claude",
                "ts": "2026-09-27T16:00:00.000Z", "key": (1, False, 1)}
        c = self.collector(found={"flamingo": {ENDED_SID: said}})
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await self.search(pilot, "flamingo")
                self.assertTrue(await self.until(pilot, lambda: "SAID" in self.screen_text(app)))
                text = self.screen_text(app)
                self.assertIn("Claude: the flamingo build is green", text)
                self.assertNotIn(ENDED_ENTRY["recap"], text, "not its recap")

    async def test_the_ended_ones_stay_when_the_said_group_fails(self):
        # review-plan1 F3: one try for all three hid ENDED with it
        c = self.collector(found={"526": {BUSY["sessionId"]}})
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.1), \
                mock.patch.object(ui.engine, "ui_said_group",
                                  mock.Mock(side_effect=KeyError("odd"))):
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await self.search(pilot, "526")
                self.assertTrue(await self.until(pilot, lambda: c.grep_calls))
                await pilot.pause(0.3)
                text = self.screen_text(app)
                self.assertIn("ENDED", text)
                self.assertIn("liveapp pull request 526", text)
                self.assertNotIn("SAID", text)

    async def test_a_said_row_changes_with_its_session_at_once(self):
        # review-plan1 F13: a row copied for SAID would say "review" until the
        # next scan, after you went to it
        marks = []
        real = ui.engine.mark_reviewed
        ui.engine.mark_reviewed = lambda sid, ts, path=None: marks.append((sid, ts))
        self.addCleanup(setattr, ui.engine, "mark_reviewed", real)
        sid = reviewable()["sessionId"]
        said = {"text": "the flamingo build is green", "who": "you",
                "ts": "2026-09-22T10:00:00.000Z", "key": (1, True, 1)}
        c = self.collector(fleet=ui.Fleet([reviewable(), BUSY], True, "12:00:00"),
                           found={"flamingo": {sid: said}})
        adapter = FakeAdapter()
        app = self.app(collector=c, adapter=adapter)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await self.search(pilot, "flamingo")
                self.assertTrue(await self.until(pilot, lambda: "SAID" in self.screen_text(app)))
                where = lambda: [w.row["attention"] for w in app.query(ui.Row)   # noqa: E731
                                 if w.row["sessionId"] == sid]
                self.assertEqual(where(), ["review"])
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")
                self.assertTrue(await self.until(pilot, lambda: adapter.asked))
                self.assertTrue(await self.until(pilot, lambda: where() == ["stopped"]),
                                f"still {where()}")
                self.assertIn("you: the flamingo build", self.screen_text(app))

    async def test_enter_after_the_grep_goes_to_the_name_match(self):
        # review 1: "release" was said in LIVE too; Enter went to LIVE
        c = self.collector(found={"release": {LIVE["sessionId"], BUSY["sessionId"]}})
        adapter = FakeAdapter()
        app = self.app(collector=c, adapter=adapter)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await self.search(pilot, "release")
                self.assertTrue(await self.until(pilot, lambda: "SAID" in self.screen_text(app)))
                self.assertEqual([w.row.get("sessionId") for w in app.rows_on_screen()],
                                 [BUSY["sessionId"], LIVE["sessionId"]],
                                 "the name match first, what was said last")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause(0.2)
                self.assertEqual(adapter.asked, [BUSY["sessionId"]])

    async def test_a_new_text_does_not_show_the_last_texts_finds(self):
        c = self.collector()                        # "flamingo" -> ENDED_SID
        gate = threading.Event()
        real = c.grep

        def later(entries, query, stop=None):
            if query != "flamingo":
                gate.wait(5)                        # the new text's grep is still out
            return real(entries, query, stop)
        c.grep = later
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "flamingo")
                    self.assertTrue(await self.until(pilot, lambda: "SAID" in self.screen_text(app)))
                    app.query_one("#search").value = "flamingo zebra"
                    await pilot.pause(0.3)
                    self.assertNotIn("SAID", self.screen_text(app),
                                     "the finds for 'flamingo' are not those for 'flamingo zebra'")
                finally:
                    gate.set()

    async def test_a_grep_dropped_while_two_were_out_runs_once_they_are_back(self):
        c, gate = self.collector(found={"abc": {ENDED_SID}}), threading.Event()

        def hung(entries, query, stop=None):
            c.grep_calls = (c.grep_calls or []) + [(query, stop)]
            if query != "abc":
                gate.wait(5)                        # and never looks at stop
            return dict.fromkeys(c.grep_value.get(query, ()))
        c.grep = hung
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.05):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "a")
                    self.assertTrue(await self.until(pilot, lambda: app.content_out == 1))
                    app.query_one("#search").value = "ab"
                    self.assertTrue(await self.until(pilot, lambda: app.content_out == 2))
                    app.query_one("#search").value = "abc"
                    await pilot.pause(0.3)
                    self.assertEqual(self.calls(c), ["a", "ab"], "two out: abc waits")
                    self.assertNotIn("no session matches", self.screen_text(app),
                                     "nothing has searched the conversations for abc yet")
                finally:
                    gate.set()
                self.assertTrue(await self.until(pilot, lambda: "SAID" in self.screen_text(app)))
                self.assertEqual(self.calls(c)[-1], "abc")

    async def test_with_no_answer_yet_the_list_does_not_search_again_on_every_paint(self):
        c = self.collector(found={})
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 30):      # no answer lands
          async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await self.search(pilot, "zzzz")
            await self.read(pilot, app, c)
            self.assertIsNone(app.content_found)
            with mock.patch.object(ui.engine, "ui_ended_groups",
                                   wraps=ui.engine.ui_ended_groups) as made:
                app.index_found()
                app.index_found()
                app.index_found()
            self.assertLessEqual(made.call_count, 1)
            app.filter_text = ""                    # the 30 s timer: nothing to grep

    async def test_the_pause_before_the_grep_does_not_say_nothing_matches(self):
        # review 2: for the half second after every key the list said "no
        # session matches" about words no grep had looked for yet
        c = self.collector(found={})
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 30):
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await self.search(pilot, "zzzz")
                await self.read(pilot, app, c)
                self.assertNotIn("no session matches", self.screen_text(app))
                self.assertIn("searching the conversations", self.screen_text(app))
                app.query_one("#search").value = ""          # the 30 s timer goes

    async def test_a_grep_that_hangs_stops_saying_it_searches_after_a_while(self):
        c, gate = self.collector(found={}), threading.Event()

        def hung(entries, query, stop=None):
            c.grep_calls = (c.grep_calls or []) + [(query, stop)]
            gate.wait(5)                    # a root on a volume that is gone
            return {}
        c.grep = hung
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.05), \
                mock.patch.object(ui, "CONTENT_PATIENCE", 0.3):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "zzzz")
                    self.assertTrue(await self.until(pilot, lambda: app.content_out == 1))
                    await pilot.pause()
                    self.assertIn("searching the conversations", self.header(app))
                    await pilot.pause(0.6)              # past its patience: no key, no scan
                    self.assertNotIn("searching the conversations", self.header(app))
                    self.assertIn("no session matches", self.screen_text(app))
                finally:
                    gate.set()

    async def test_an_index_that_hangs_says_nothing_matches_after_its_patience(self):
        # review 3: no grep can run without the index; "searching" stayed for good
        c, gate = self.collector(found={}), threading.Event()

        def hung():
            c.index_calls += 1
            gate.wait(5)
            return {}
        c.index = hung
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.05), \
                mock.patch.object(ui, "INDEX_PATIENCE", 1.0):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "zzzz")
                    self.assertIn("reading the ended sessions", self.screen_text(app))
                    await pilot.pause(1.4)              # past its patience: no key, no scan
                    text = self.screen_text(app)
                    self.assertIn("no session matches 'zzzz'", text)
                    self.assertNotIn("searching the conversations", text)
                    self.assertNotIn("reading the ended sessions", text)
                finally:
                    gate.set()

    async def test_a_read_that_fails_while_the_grep_waits_says_nothing_matches(self):
        c, gate = self.collector(found={}), threading.Event()

        def late_and_broken():
            c.index_calls += 1
            gate.wait(5)
            raise OSError("index unreadable")
        c.index = late_and_broken
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.05):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "zzzz")
                    self.assertTrue(await self.until(pilot, lambda: app.content_waiting))
                finally:
                    gate.set()                  # the read comes back: no index
                self.assertTrue(await self.until(
                    pilot, lambda: "no session matches 'zzzz'" in self.screen_text(app)))

    async def failed_then_good(self, slow_failure):
        """Review 4: the first read of the index fails - after the pause ended,
        or before - and `/` again (from a row, the words kept) reads it. The
        grep for the words has never run: it runs now."""
        c, gate = self.collector(found={"release": {ENDED_SID}}), threading.Event()
        calls = []

        def index():
            calls.append(1)
            if len(calls) == 1:
                if slow_failure:
                    gate.wait(5)                    # back after the pause ended
                raise OSError("index unreadable")
            return {ENDED_SID: dict(ENDED_ENTRY)}
        c.index = index
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.05):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "release")
                    self.assertTrue(await self.until(pilot, lambda: app.content_waiting))
                finally:
                    gate.set()
                self.assertTrue(await self.until(pilot, lambda: app.index_out == 0))
                await pilot.pause(0.2)
                await pilot.press("enter")          # to the row: the box keeps the words
                await pilot.pause()
                await pilot.press("slash")          # read again: this one works
                await pilot.pause()
                self.assertTrue(await self.until(pilot, lambda: len(calls) == 2
                                                 and app.index_out == 0))
                await self.until(pilot, lambda: "SAID" in self.screen_text(app), wait=1.5)
                self.assertEqual(self.calls(c), ["release"], "the grep for the words ran")
                self.assertIn("SAID", self.screen_text(app))

    async def test_a_new_text_while_the_index_is_read_greps_it_once(self):
        # review 5: the index lands inside the pause after a new text - the
        # pause's timer greps it, not index_read as well (a new text ends the
        # old wait); two greps for one text filled both threads
        c, gate = self.collector(found={"flamingo": {ENDED_SID}}), threading.Event()
        real = c.index

        def slow():
            gate.wait(5)
            return real()
        c.index = slow
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.6):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "release")
                    self.assertTrue(await self.until(pilot, lambda: app.content_waiting))
                    app.query_one("#search").value = "flamingo"
                    await pilot.pause()
                finally:
                    gate.set()                      # lands inside the 0.6 s pause
                self.assertTrue(await self.until(pilot, lambda: app.index_out == 0))
                await pilot.pause(1.2)
                self.assertEqual(self.calls(c), ["flamingo"])

    async def test_a_slow_failed_read_then_a_good_one_greps(self):
        await self.failed_then_good(slow_failure=True)

    async def test_a_fast_failed_read_then_a_good_one_greps(self):              # control
        await self.failed_then_good(slow_failure=False)

    async def test_nothing_says_it_greps_before_a_grep_was_asked(self):
        app = self.app(collector=self.collector())
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app.filter_text = "zzzz"                # no key: no grep asked
            self.assertFalse(app.grepping())

    def gated(self, c):
        """Each grep waits for the gate - recorded as it starts, not as it ends."""
        gate = threading.Event()

        def slow(entries, query, stop=None):
            c.grep_calls = (c.grep_calls or []) + [(query, stop)]
            gate.wait(5)
            found = (c.grep_value or {}).get(query, ())
            return dict(found) if isinstance(found, dict) else dict.fromkeys(found)
        c.grep = slow
        return gate

    async def test_while_it_greps_it_says_so_and_not_that_nothing_matches(self):
        c = self.collector(found={})
        gate = self.gated(c)
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "zzzz")
                    self.assertTrue(await self.until(pilot, lambda: c.grep_calls is not None
                                                     or app.content_out))
                    await pilot.pause(0.2)
                    text = self.screen_text(app)
                    self.assertIn("searching the conversations", self.header(app))
                    self.assertNotIn("no session matches", text)
                    self.assertIn("searching the conversations", text)
                finally:
                    gate.set()
                self.assertTrue(await self.until(
                    pilot, lambda: "no session matches 'zzzz'" in self.screen_text(app)))
                self.assertNotIn("searching the conversations", self.header(app))

    async def test_a_newer_search_stops_the_older_grep(self):
        c = self.collector(found={"flam": {BUSY["sessionId"]}, "flamingo": {ENDED_SID}})
        gates = {"flam": threading.Event(), "flamingo": threading.Event()}

        def slow(entries, query, stop=None):
            c.grep_calls = (c.grep_calls or []) + [(query, stop)]
            gates[query].wait(5)            # and never looks at stop: it lands late
            return dict.fromkeys(c.grep_value[query])
        c.grep = slow
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "flam")
                    self.assertTrue(await self.until(pilot, lambda: app.content_out == 1))
                    app.query_one("#search").value = "flamingo"
                    self.assertTrue(await self.until(pilot, lambda: app.content_out == 2))
                    gates["flamingo"].set()                 # the newer one first
                    self.assertTrue(await self.until(pilot, lambda: "SAID" in self.screen_text(app)))
                    gates["flam"].set()                     # then the older one, late
                    self.assertTrue(await self.until(pilot, lambda: app.content_out == 0))
                    await pilot.pause()
                finally:
                    for g in gates.values():
                        g.set()
                (first, stop1), (second, stop2) = c.grep_calls
                self.assertEqual((first, second), ("flam", "flamingo"))
                self.assertTrue(stop1(), "the older one is told to stop")
                self.assertFalse(stop2())
                self.assertIn("SAID", self.screen_text(app))
                self.assertNotIn("Release queue", self.screen_text(app),
                                 "the older answer, back last, is not shown")

    async def test_esc_stops_the_grep_and_its_answer_shows_nowhere(self):
        c = self.collector()
        gate = self.gated(c)
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "flamingo")
                    self.assertTrue(await self.until(pilot, lambda: app.content_out == 1))
                    await pilot.press("escape")
                    await pilot.pause()
                finally:
                    gate.set()
                self.assertTrue(await self.until(pilot, lambda: app.content_out == 0))
                await pilot.pause()
                self.assertTrue(c.grep_calls[0][1](), "told to stop")
                self.assertNotIn("SAID", self.screen_text(app))
                self.assertEqual(app.filter_text, "")

    async def test_no_grep_until_the_index_is_read(self):
        c = self.collector()
        gate, real = threading.Event(), c.index

        def slow():
            gate.wait(5)
            return real()
        c.index = slow
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "flamingo")
                    await pilot.pause(0.4)
                    self.assertEqual(self.calls(c), [], "it greps the index's transcripts")
                finally:
                    gate.set()
                self.assertTrue(await self.until(pilot, lambda: "SAID" in self.screen_text(app)))
                self.assertEqual(self.calls(c), ["flamingo"])

    async def test_a_grep_that_fails_leaves_the_search_working(self):
        c = self.collector()

        def broken(entries, query, stop=None):
            c.grep_calls = (c.grep_calls or []) + [(query, stop)]
            raise OSError("unreadable")
        c.grep = broken
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.1):
            async with app.run_test(size=(140, 40)) as pilot:
                await pilot.pause()
                await self.search(pilot, "release")
                self.assertTrue(await self.until(pilot, lambda: c.grep_calls and not app.content_out))
                await pilot.pause()
                self.assertTrue(app.is_running)
                self.assertIn("Release queue", self.screen_text(app))
                self.assertNotIn("searching the conversations", self.header(app))

    async def test_greps_that_never_come_back_take_two_threads_at_most(self):
        c, gate = self.collector(found={}), threading.Event()

        def hung(entries, query, stop=None):
            c.grep_calls = (c.grep_calls or []) + [(query, stop)]
            gate.wait(5)                    # and never looks at stop
            return {}
        c.grep = hung
        app = self.app(collector=c)
        with mock.patch.object(ui, "CONTENT_PAUSE", 0.05):
            async with app.run_test(size=(140, 40)) as pilot:
                try:
                    await pilot.pause()
                    await self.search(pilot, "a")
                    for text in ("ab", "abc", "abcd"):
                        await pilot.pause(0.3)
                        app.query_one("#search").value = text
                    await pilot.pause(0.3)
                    self.assertEqual(len(c.grep_calls), 2)
                finally:
                    gate.set()


QUIT_WHILE_IT_GREPS = """
import sys, threading
sys.path.insert(0, sys.argv[1])
import ccwho_ui as ui, test_ui
ui.CONTENT_PAUSE = 0.05
c = test_ui.FakeCollector()
c.index_value = {"x": {"sessionId": "x"}}
c.grep = lambda entries, query, stop=None: threading.Event().wait(20) or set()

app = ui.CcwhoUi(adapter=test_ui.FakeAdapter(), collector=c)

async def drive(pilot):
    await pilot.pause()
    await pilot.press("slash")
    await pilot.pause()
    app.query_one("#search").value = "flamingo"
    for _ in range(40):
        await pilot.pause(0.05)
        if app.content_out:
            break
    print("out", app.content_out, flush=True)
    await pilot.press("escape")
    await pilot.pause()
    await pilot.press("escape")
    await pilot.pause()
    await pilot.press("q")

app.run(headless=True, auto_pilot=drive)
print("quit", flush=True)
"""


class TestTheGrepNeverHoldsTheList(unittest.TestCase):
    def test_q_does_not_wait_for_a_grep(self):
        import os
        import sys
        import time
        here = os.path.dirname(os.path.abspath(ui.__file__))
        began = time.monotonic()
        done = subprocess.run([sys.executable, "-c", QUIT_WHILE_IT_GREPS, here],
                              capture_output=True, text=True, timeout=12, cwd=here,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        self.assertIn("out 1", done.stdout, "a grep was out when q came: " + done.stderr[-400:])
        self.assertIn("quit", done.stdout)
        self.assertLess(time.monotonic() - began, 6, "q waited for the grep")

    def test_a_grep_back_when_the_list_is_not_running_says_nothing(self):
        app = ui.CcwhoUi(adapter=FakeAdapter(), collector=FakeCollector())
        app.content_out = 1
        app._grep(1, "flamingo", {})            # no raise


class TestTheCollectorGreps(unittest.TestCase):
    def test_it_greps_the_index_entries_it_is_given(self):
        import ccwho_index
        asked = []
        stop = lambda: False                        # noqa: E731
        with mock.patch.object(ccwho_index, "grep",
                               lambda entries, query, stop=None, store=None: asked.append(
                                   (entries, query, stop)) or {"x": None}):
            self.assertEqual(ui.Collector().grep({"e": {}}, "flamingo", stop), {"x": None})
        self.assertEqual(asked, [({"e": {}}, "flamingo", stop)])

    def test_every_grep_reads_from_one_store_of_what_was_said(self):
        # read once for the life of the list: the next search reads what was appended
        import ccwho_index
        stores = []
        with mock.patch.object(ccwho_index, "grep",
                               lambda entries, query, stop=None, store=None: stores.append(
                                   store) or {}):
            c = ui.Collector()
            c.grep({}, "flamingo")
            c.grep({}, "pelican")
            self.assertIsInstance(stores[0], ccwho_index.SaidStore)
            self.assertIs(stores[0], stores[1])
            # a store other code made holds what other rules read: a new one
            stores[0].source = "the code before an edit"
            c.grep({}, "flamingo")
            self.assertIsNot(stores[2], stores[0])
            self.assertEqual(stores[2].source, ccwho_index.SOURCE)

    def test_an_edit_not_loaded_yet_keeps_the_store(self):
        # review-build6 F2: the list compares the code as loaded, not the files
        import ccwho_index
        import os
        import tempfile
        fd, edited = tempfile.mkstemp(suffix=".py")
        os.close(fd)
        self.addCleanup(os.remove, edited)
        stores = []
        with mock.patch.object(ccwho_index, "grep",
                               lambda entries, query, stop=None, store=None: stores.append(
                                   store) or {}), \
                mock.patch.object(ccwho_index, "SOURCES", (edited,)):
            c = ui.Collector()
            c.grep({}, "flamingo")
            c.grep({}, "pelican")
        self.assertIs(stores[0], stores[1], "not a new store at every search")

    def test_a_reload_keeps_what_was_said(self):
        # review-build1 F1: every scan reloads the engine, and its index module
        # is a new object - a store tied to the class was read again every time
        import json
        import os
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "s.jsonl")
        line = json.dumps({"type": "user", "timestamp": "2026-10-07T10:00:00.000Z",
                           "message": {"role": "user", "content": "the flamingo build"}})
        with open(path, "w") as fh:
            fh.write(line + "\n")
        entries = {"s": {"sessionId": "s", "path": path, "entrypoint": "cli"}}
        c = ui.Collector()
        self.assertEqual(set(c.grep(entries, "flamingo")), {"s"})
        store, parsed = c.said_store, c.said_store.parsed
        c.reload()
        self.assertEqual(c.reload_error, "")
        self.assertEqual(set(c.grep(entries, "flamingo")), {"s"})
        self.assertIs(c.said_store, store)
        self.assertEqual(c.said_store.parsed, parsed, "nothing read again")
        with open(path, "a") as fh:                                          # control
            fh.write(line + "\n")
        c.grep(entries, "flamingo")
        self.assertEqual(c.said_store.parsed, parsed + 1, "only the new line")
