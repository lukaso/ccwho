"""Codex threads in the list (Phase 3, the owner's D16, 2026-09-29): each open
thread is a row under CODEX, last - its name, folder, where it runs, its ports.
Enter says where it runs (there is no window ccwho can bring forward); the
detail shows what it is and its processes; `x` offers "kill its processes"
(the usual box) and never a stop. The fake collector's world only."""
import unittest

import ccwho_ui as ui


_UNPIN = []


def setUpModule():
    import ccwho
    import os
    import shutil
    import tempfile
    import testkit
    _UNPIN.append(testkit.pin_ccwho_dir(ccwho))
    # a park is written to disk: never the user's own file (test_ui does the same)
    parks = tempfile.mkdtemp(prefix="ccwho-test-parks-")
    os.environ["CCWHO_DIR"] = parks
    _UNPIN.append(lambda: (os.environ.pop("CCWHO_DIR", None), shutil.rmtree(parks, True)))


class TestNoTestHereWritesTheRealParks(unittest.TestCase):
    def test_the_parks_are_in_a_temp_dir(self):
        import os
        import ccwho_ui
        here = os.environ.get("CCWHO_DIR", "")
        self.assertTrue(here and ccwho_ui.engine.parks_path().startswith(here))


def tearDownModule():
    while _UNPIN:
        _UNPIN.pop()()
from test_ui import BUSY, LIVE, FakeAdapter, FakeCollector, ParkTest, UiTest
from test_ui_kill import KillCollector, RowBoxTest, prep

T = "01a0eca7-7b42-72f0-b19a-ff0ae32db6a3"
PROCS = {"ports_ok": True, "procs_ok": True, "sessions_ok": True, "agent_ports": [],
         "by_session": {}, "left_behind": [], "unsure": [],
         "codex": [{"pid": 30, "ports": [5173], "command": "vite --port 5173", "helper": False,
                    "orphan": False, "harness": "codex", "session": T}],
         "codex_threads": [{"thread": T, "name": "fix the navbar",
                            "cwd": "/Users/x/projects/liveapp", "host_pid": 4215,
                            "originator": "Codex Desktop", "source": "vscode",
                            "procs": 1, "ports": [5173], "pids": [30]}]}


def fleet(rows=(LIVE,), procs=PROCS):
    return ui.Fleet(list(rows), True, "12:00:00", procs=procs)


class CodexCollector(KillCollector):
    def __init__(self, procs=PROCS, rows=(LIVE,)):
        super().__init__()
        self.fleet_value = fleet(rows, procs)

    def kill_prepare(self, mode, target):
        self.prepared.append((mode, target))
        return dict(self.prep_value, mode=mode, target=target)


class TestTheCodexRows(UiTest):
    async def test_an_open_thread_is_a_row_in_the_claude_groups(self):
        # the owner's D19: its state places it; the row says it is Codex
        app = self.app(collector=CodexCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            text = self.screen_text(app)
            self.assertNotIn("CODEX", text)
            self.assertIn("fix the navbar (codex)", text)
            self.assertIn("· VS Code", text)
            self.assertIn("STOPPED", text)                    # no turn read: stopped

    async def test_the_header_counts_a_thread_in_its_group(self):
        # the header counts the groups, not sessions and Codex threads apart
        app = self.app(collector=CodexCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            header = str(app.query_one("#header").content)
            self.assertIn("1 needs · 1 stopped", header)
            self.assertNotIn("codex", header)

    async def test_no_threads_no_group(self):                            # control
        app = self.app(collector=CodexCollector(procs=dict(PROCS, codex_threads=None)))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            self.assertNotIn("fix the navbar", self.screen_text(app))
            self.assertNotIn("stopped", str(app.query_one("#header").content))

    async def test_enter_says_where_it_runs_and_opens_nothing(self):
        adapter = FakeAdapter()
        app = self.app(collector=CodexCollector(), adapter=adapter)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j", "enter")
            await pilot.pause(0.2)
            self.assertEqual((adapter.asked, adapter.attached), ([], []))
            self.assertIn("VS Code", app.status)
            self.assertIn("open it there", app.status)
            # named as Enter on a Claude row names it: "went to aaaa  ✳ Title (claude)"
            self.assertIn(f"{T[-4:]}  fix the navbar (codex)", app.status)

    async def test_enter_on_a_long_title_keeps_its_codex_word(self):
        long = dict(PROCS, codex_threads=[dict(PROCS["codex_threads"][0],
                                               name="Refactor the session index parser for speed")])
        app = self.app(collector=CodexCollector(procs=long))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.selected = T
            app.restore_selection()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertIn(f"{T[-4:]}  Refactor the", app.status)
            self.assertIn("\u2026 (codex): it runs in VS Code", app.status)

    async def test_enter_on_a_programs_thread_says_nothing_to_open(self):
        # D22: `codex exec` - a program runs it, no app to open it in
        prog = dict(PROCS, codex_threads=[dict(PROCS["codex_threads"][0], source="exec")])
        adapter = FakeAdapter()
        app = self.app(collector=CodexCollector(procs=prog), adapter=adapter)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            self.assertIn("PROGRAMS", self.screen_text(app))
            app.selected = T
            app.restore_selection()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertEqual((adapter.asked, adapter.attached), ([], []))
            self.assertIn("a program runs it", app.status)
            self.assertIn("nothing to open", app.status)
            self.assertNotIn("open it there", app.status)

    async def test_enter_on_a_finished_thread_counts_as_looked_at(self):
        done = dict(PROCS, codex_threads=[dict(PROCS["codex_threads"][0], turn="done", ask="",
                                               ts=42, mtime=1.0)])
        seen = []
        real = ui.engine.mark_reviewed
        ui.engine.mark_reviewed = lambda sid, ts, path=None: seen.append((sid, ts))
        self.addCleanup(setattr, ui.engine, "mark_reviewed", real)
        app = self.app(collector=CodexCollector(procs=done))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            row = next(r for r in app.fleet.rows if r.get("kind") == "codex")
            self.assertEqual(row["attention"], "review")
            app.selected = T
            app.restore_selection()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(seen, [(T, 42)])
            self.assertEqual(row["attention"], "stopped")

    async def test_enter_on_a_finished_thread_leaves_the_highlight_in_needs_you(self):
        # as for a Claude row you go to (owner, 2026-10-08): the next one
        from test_ui import reviewable
        done = dict(PROCS, codex_threads=[dict(PROCS["codex_threads"][0], turn="done", ask="",
                                               ts=42, mtime=1.0)])
        real = ui.engine.mark_reviewed
        ui.engine.mark_reviewed = lambda sid, ts, path=None: None
        self.addCleanup(setattr, ui.engine, "mark_reviewed", real)
        app = self.app(collector=CodexCollector(procs=done, rows=(LIVE, reviewable())))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            needs = [r.get("sessionId") for g in app.groups() if g["heading"] == "NEEDS YOU"
                     for r in g["rows"]]
            self.assertEqual(needs, [LIVE["sessionId"], T, reviewable()["sessionId"]])  # control
            app.selected = T
            app.restore_selection()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            row = next(r for r in app.fleet.rows if r.get("kind") == "codex")
            self.assertEqual(row["attention"], "stopped")
            self.assertEqual(app.selected_row()["sessionId"], reviewable()["sessionId"])

    async def test_a_failed_jump_ends_only_its_own_sessions_go(self):
        # Enter on a Claude row, then - while its window opens - Enter on a
        # Codex thread that asks: that is going there. The Claude jump that
        # fails later ends its own go, not the thread's (review s3b)
        import time
        from test_ui import ASKING, OTHER, GatedAdapter

        class Fails(GatedAdapter):
            def focus(self, row, deadline=5.0):
                super().focus(row, deadline)
                return "iTerm2 did not answer in 5s"

        thread = dict(PROCS["codex_threads"][0], turn="done", ask="Shall I land it?", ts=42,
                      mtime=1.0)
        asks = dict(PROCS, codex_threads=[thread])
        adapter = Fails()
        c = CodexCollector(procs=asks, rows=(ASKING, OTHER))
        app = self.app(adapter=adapter, collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            try:
                await pilot.pause()
                needs = [r.get("sessionId") for g in app.groups() if g["heading"] == "NEEDS YOU"
                         for r in g["rows"]]
                self.assertEqual(needs, [T, ASKING["sessionId"], OTHER["sessionId"]])  # control
                await pilot.press("down", "enter")      # the Claude row: its window opens
                await pilot.pause(0.1)
                await pilot.press("up", "enter")        # the thread
                await pilot.pause()
            finally:
                adapter.gate.set()
            await pilot.pause(0.1)
            await pilot.pause()
            self.assertEqual(adapter.asked, [ASKING["sessionId"]])
            self.assertEqual(app.selected_row()["sessionId"], T)
            busy = dict(PROCS, codex_threads=[dict(thread, turn="open", ask="",
                                                   mtime=time.time())])
            c.fleet_value = fleet((ASKING, OTHER), busy)
            app.show(fleet((ASKING, OTHER), busy))
            await pilot.pause()
            await pilot.pause()
            row = next(r for r in app.fleet.rows if r.get("kind") == "codex")
            self.assertEqual(row["attention"], "busy")
            self.assertEqual(app.selected_row()["sessionId"], ASKING["sessionId"])

    async def test_its_detail_says_what_it_is_and_lists_its_processes(self):
        app = self.app(collector=CodexCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j", "right")
            await pilot.pause()
            text = str(app.query_one("#brief").content)
            for words in ("fix the navbar", "/Users/x/projects/liveapp", "VS Code", "30",
                          "vite --port 5173", ":5173"):
                self.assertIn(words, text)

    async def test_its_usage_entry_is_bright_while_it_is_highlighted(self):
        # as a Claude row's account: the snapshot the collector builds, from a
        # Codex usage row that names the open thread spending it
        import ccwho_usage as usage
        from test_ui import NOW
        codex = {"id": "codex:codex", "kind": "codex", "brand": "oai", "email": "",
                 "label": "", "sessions": 1, "age": 60, "five_hour": None, "threads": [T],
                 "seven_day": {"state": "ok", "pct": 65.0, "resets_at": NOW + 72 * 3600,
                               "age": 60}}
        snap = usage.snapshot([], NOW, set(), {"usage_roots": []}, codex=[codex])
        collector = CodexCollector()
        collector.fleet_value = ui.Fleet([LIVE], True, "12:00:00", procs=PROCS, usage=snap)
        app = self.app(collector=collector)

        def bright():
            text = app.query_one("#usage").render()
            dim = {i for s in text.spans if "dim" in str(s.style) for i in range(s.start, s.end)}
            return "".join(c for i, c in enumerate(text.plain) if i not in dim)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            for sid, lit in ((T, True), (LIVE["sessionId"], False), (T, True)):
                app.selected = sid
                app.mark_selected()
                await pilot.pause()
                self.assertEqual("oai:codex" in bright(), lit, (sid, bright()))


def asks(**kw):
    """PROCS with its thread on a finished turn that asks you (▲) - or as `kw` says."""
    t = dict(PROCS["codex_threads"][0], turn="done", ask="Shall I land it?", ts=42, mtime=1.0)
    return dict(PROCS, codex_threads=[dict(t, **kw)])


class TestParkACodexThread(ParkTest):
    """[park] and `z` on a Codex thread, as on a Claude session (owner,
    2026-10-17): its question stayed in NEEDS YOU, and Enter on it cannot
    answer it."""

    def fleet(self, procs):
        return FakeCollector(fleet=ui.Fleet([dict(BUSY)], True, "12:00:00", procs=procs))

    async def test_z_parks_its_question_with_your_note(self):
        app = self.app(collector=self.fleet(asks()))
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await self.select(app, pilot, T)
            self.assertEqual(self.z_says(app), "park")
            await self.park_it(app, pilot, "checked later")
            self.assertEqual(self.parks, [(T, 42, "checked later")])
            self.assertEqual(self.state(app, T), ["parked"], "now, not on the next scan")
            self.assertEqual(self.heading_of(app, T), "PARKED")
            self.assertIn("checked later", self.screen_text(app))

    async def test_a_click_on_park_opens_the_box_and_goes_nowhere(self):
        adapter = FakeAdapter()
        app = self.app(adapter=adapter, collector=self.fleet(asks()))
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            w = next(w for w in app.query(ui.Row) if w.row["sessionId"] == T)
            line = "".join(t for t, _ in w.spans_lines()[1])
            self.assertIn(ui.engine.UI_PARK, line)
            await pilot.click(w, offset=(line.index(ui.engine.UI_PARK) + 2, 1))
            await pilot.pause()
            self.assertIsNotNone(self.box(app))
            self.assertEqual(adapter.asked, [], "a click on [park] is not a jump")
            self.assertNotIn("open it there", app.status)

    def box_text(self, app):
        return "\n".join(str(w.content) for w in self.box(app).query(ui.Static))

    async def test_the_box_names_it_by_its_rows_id_and_says_when_the_park_ends(self):
        # a thread id starts with its time: its row's id is its random end. And
        # Enter on it goes nowhere: no "or you go to it"
        app = self.app(collector=self.fleet(asks()))
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await self.select(app, pilot, T)
            await pilot.press("z")
            await pilot.pause()
            text = self.box_text(app)
            self.assertIn(f"park {T[-4:]}", text)
            self.assertNotIn(T[:4], text)
            self.assertIn("writes a new turn", text)
            self.assertNotIn("you go to it", text)

    async def test_a_sessions_box_says_going_to_it_ends_it(self):         # control
        from test_ui import ASKING
        app = self.app(collector=FakeCollector(fleet=ui.Fleet([dict(ASKING)], True,
                                                              "12:00:00")))
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await pilot.press("z")
            await pilot.pause()
            text = self.box_text(app)
            self.assertIn(f"park {ASKING['sessionId'][:4]}", text)
            self.assertIn("writes a new turn or you go to it", text)

    async def test_an_unpark_not_saved_names_it_by_its_rows_id(self):
        self.saves = False
        app = self.app(collector=self.fleet(asks(park={"ts": 42, "note": "checked later"})))
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await self.select(app, pilot, T)
            await pilot.press("z")
            await pilot.pause()
            self.assertEqual(self.state(app, T), ["parked"])
            self.assertIn(f"{T[-4:]} stays parked - could not save the unpark", app.note)

    async def test_a_park_leaves_the_highlight_on_the_next_that_needs_you(self):
        # the thread first, the session under it: the fixture's time is text, not known
        from test_ui import ASKING
        app = self.app(collector=FakeCollector(fleet=ui.Fleet(
            [dict(ASKING), dict(BUSY)], True, "12:00:00", procs=asks())))
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await self.select(app, pilot, T)
            await self.park_it(app, pilot, "later")
            self.assertEqual(self.heading_of(app, T), "PARKED")
            self.assertEqual(app.selected, ASKING["sessionId"])

    async def test_a_busy_thread_has_no_z(self):                          # control
        import time
        app = self.app(collector=self.fleet(asks(turn="open", ask="", ts=None,
                                                 mtime=time.time())))
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await self.select(app, pilot, T)
            self.assertIsNone(self.z_says(app))

    async def test_z_on_its_parked_row_unparks_it(self):
        app = self.app(collector=self.fleet(asks(park={"ts": 42, "note": "checked later"})))
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(self.heading_of(app, T), "PARKED")
            self.assertIn("checked later", self.screen_text(app))
            await self.select(app, pilot, T)
            self.assertEqual(self.z_says(app), "unpark")
            await pilot.press("z")
            await pilot.pause()
            await pilot.pause()
            self.assertIsNone(self.box(app), "no box: nothing to ask")
            self.assertEqual(self.unparks, [T])
            self.assertEqual(self.state(app, T), ["asks"])
            self.assertEqual(self.heading_of(app, T), "NEEDS YOU")

    async def test_enter_on_its_parked_row_says_where_it_runs_and_keeps_it(self):
        # Enter goes nowhere - it runs in its app - as on a Claude row with no
        # window: it stays parked, whatever it was when parked - every state a
        # thread parks in (parkable): a question, a finished turn, one you have
        # seen - and with no note too: a row is parked by its state, not by a note
        was = set()
        for kw in (dict(), dict(ask=""), dict(ask="", seen_ts=42)):
            for note in ("checked later", ""):
                with self.subTest(**kw, note=note):
                    await self.enter_keeps_the_park(was, note, **kw)
        self.assertEqual(was, {"asks", "review", "stopped"})                 # control

    async def enter_keeps_the_park(self, was, note, **kw):
        """Enter on the thread parked with `note`, its turn as `kw` says: it
        stays parked. Adds the state it was parked in to `was`."""
        self.unparks.clear(), self.marks.clear()
        adapter = FakeAdapter()
        app = self.app(adapter=adapter, collector=self.fleet(
            asks(park={"ts": 42, "note": note}, **kw)))
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await self.select(app, pilot, T)
            was.add(app.selected_row()["park_was"])
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertIn("open it there", app.status)
            self.assertEqual((adapter.asked, self.unparks, self.marks), ([], [], []))
            self.assertEqual(self.state(app, T), ["parked"])

    async def test_a_new_turn_ends_it(self):
        app = self.app(collector=self.fleet(asks()))
        async with app.run_test(size=(140, 30)) as pilot:
            await pilot.pause()
            await self.select(app, pilot, T)
            await self.park_it(app, pilot, "later")
            app.show(ui.Fleet([dict(BUSY)], True, "12:00:05", procs=asks()))
            await pilot.pause()
            self.assertEqual(self.state(app, T), ["parked"], "a scan from before the park")
            app.show(ui.Fleet([dict(BUSY)], True, "12:00:10", procs=asks(ts=43)))
            await pilot.pause()
            self.assertEqual(self.state(app, T), ["asks"])


class TestACodexOnlyListKeepsItsHint(UiTest):
    """No Claude session, Codex threads open: the list is not empty, and it
    still says no session runs and how to reopen a save (item 2, 2026-10-01)."""

    async def test_the_hint_stays_under_the_codex_rows(self):
        app = self.app(collector=CodexCollector(rows=()))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            text = self.screen_text(app)
            self.assertIn("fix the navbar (codex)", text)
            self.assertIn("No Claude Code sessions running", text)

    async def test_no_hint_beside_a_claude_session(self):                # control
        app = self.app(collector=CodexCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            self.assertNotIn("No Claude Code sessions running", self.screen_text(app))

    async def test_the_hint_survives_a_reorder_and_goes_with_a_session(self):
        two = dict(PROCS, codex_threads=[dict(PROCS["codex_threads"][0], mtime=1.0),
                                         dict(PROCS["codex_threads"][0], thread=T[:-4] + "0000",
                                              name="second", mtime=2.0)])
        c = CodexCollector(procs=two, rows=())
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            self.assertIn("No Claude Code sessions running", self.screen_text(app))
            # the same rows, swapped by age: the list moves them, the hint stays
            swapped = dict(two, codex_threads=[dict(two["codex_threads"][0], mtime=3.0),
                                               two["codex_threads"][1]])
            c.fleet_value = fleet((), swapped)
            app.collect()
            await pilot.pause()
            await pilot.pause()
            self.assertIn("No Claude Code sessions running", self.screen_text(app))
            self.assertEqual(len(app.query(".claude-hint")), 1)
            # a Claude session arrives: the hint goes
            c.fleet_value = fleet((LIVE,), swapped)
            app.collect()
            await pilot.pause()
            await pilot.pause()
            self.assertNotIn("No Claude Code sessions running", self.screen_text(app))

    async def test_a_build_and_a_repaint_in_one_tick_leave_one_hint(self):
        # Textual keeps a widget being removed among the children until it
        # goes: a repaint in the same tick took the new hint for an extra
        c = CodexCollector(rows=())
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            two = dict(PROCS, codex_threads=PROCS["codex_threads"] + [
                dict(PROCS["codex_threads"][0], thread=T[:-4] + "0000", name="second")])
            app.fleet = fleet((), two)
            app.rebuild()                                  # a new row: a build
            app.rebuild()                                  # the same shape: a repaint
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(len(app.query(".claude-hint")), 1)

    async def test_a_search_typed_and_cleared_in_one_tick_leaves_one_hint(self):
        app = self.app(collector=CodexCollector(rows=()))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.filter_text = "navbar"
            app.rebuild()
            app.filter_text = ""
            app.rebuild()
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(len(app.query(".claude-hint")), 1)

    async def test_a_search_has_no_hint(self):                           # control
        app = self.app(collector=CodexCollector(rows=()))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(len(app.query(".claude-hint")), 1)
            app.filter_text = "navbar"
            app.rebuild()
            await pilot.pause()
            self.assertEqual(len(app.query(".claude-hint")), 0)
            self.assertNotIn("no session matches", self.screen_text(app))
            self.assertIn("fix the navbar (codex)", self.screen_text(app))

    async def test_the_last_claude_session_leaving_brings_the_hint_at_once(self):
        c = CodexCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            # the first width paint (check_width, every 0.2 s) is done: a
            # loaded machine ran it inside the window counted below
            for _ in range(50):
                if app.painted_width == app.size.width:
                    break
                await pilot.pause(0.05)
            self.assertEqual(app.painted_width, app.size.width)
            self.assertEqual(len(app.query(".claude-hint")), 0)
            builds = []
            real = app.rebuild
            app.rebuild = lambda: builds.append(1) or real()
            c.fleet_value = fleet((), PROCS)
            app.collect()
            await pilot.pause()
            # the paint the session left in, not a later one
            self.assertEqual(len(builds), 1, builds)
            self.assertEqual(len(app.query(".claude-hint")), 1)


class TestXOnACodexRow(RowBoxTest):
    async def test_x_offers_its_processes_and_no_stop(self):
        app = self.app(collector=CodexCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            await self.open_choices(app, pilot)
            text = self.choice_text(app)
            self.assertIn(T[-4:], app.screen.title_text)         # the row's own id
            self.assertNotIn(T[:4], app.screen.title_text)
            # its id, handle and title, as the row shows them
            self.assertEqual(app.screen.title_text, f"{T[-4:]}  liveapp  fix the navbar")
            self.assertIn("p  kill its processes  :5173", text)
            self.assertNotIn("stop the session", text)
            self.assertIn("end it there", text)
            self.assertIn("VS Code", text)

    async def test_x_on_a_thread_with_no_processes_says_where_to_end_it(self):
        idle = dict(PROCS, codex=[], codex_threads=[dict(PROCS["codex_threads"][0],
                                                          procs=0, ports=[], pids=[])])
        c = CodexCollector(procs=idle)
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            await self.open_choices(app, pilot)
            self.assertIsNone(self.choices(app))
            self.assertIn("VS Code", app.status)
            self.assertNotIn("/exit", app.status)
            self.assertNotIn("interactive session", app.status)
            self.assertEqual(c.prepared, [])

    async def test_x_on_a_programs_thread_says_a_program_runs_it(self):
        idle = dict(PROCS, codex=[], codex_threads=[dict(PROCS["codex_threads"][0], source="exec",
                                                          procs=0, ports=[], pids=[])])
        app = self.app(collector=CodexCollector(procs=idle))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.selected = T
            app.restore_selection()
            await pilot.pause()
            await self.open_choices(app, pilot)
            self.assertIsNone(self.choices(app))
            self.assertIn("a program runs it (codex exec)", app.status)
            self.assertNotIn("end it there", app.status)

    async def test_x_box_on_a_programs_thread_says_a_program_runs_it(self):
        prog = dict(PROCS, codex_threads=[dict(PROCS["codex_threads"][0], source="exec")])
        app = self.app(collector=CodexCollector(procs=prog))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.selected = T
            app.restore_selection()
            await pilot.pause()
            await self.open_choices(app, pilot)
            text = self.choice_text(app)
            self.assertIn("p  kill its processes  :5173", text)
            self.assertIn("a program runs it (codex exec)", text)
            self.assertNotIn("end it there", text)

    async def test_p_asks_about_the_threads_processes(self):
        c = CodexCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            await self.open_choices(app, pilot)
            await pilot.press("p")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [("session", T)])
            self.assertEqual(c.carried, [])
            title = str(app.screen.query_one("#killhead").content)
            self.assertIn("Codex thread", title)                # which one you confirm
            self.assertIn("fix the navbar", title)
            self.assertNotIn("session", title)
            self.assertIn(T[-4:], title)                         # the random end,
            self.assertNotIn(T[:4], title)                       # not the time bits

    async def test_x_on_a_process_in_its_detail_kills_that_process(self):
        c = CodexCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j", "right", "right")
            await pilot.pause()
            brief = app.query_one("#brief")
            self.assertIs(app.focused, brief)
            procs_ = [p for p, f in brief.fields.items() if f == "proc"]
            self.assertTrue(procs_)
            # as a Claude brief's: the title, folder and whole id can be copied
            self.assertEqual(sorted(set(brief.fields.values())),
                             ["cwd", "proc", "session_id", "title"])
            brief.lit = procs_[0]
            brief.paint()
            await pilot.press("x")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [("pid", 30)])


if __name__ == "__main__":
    unittest.main()
