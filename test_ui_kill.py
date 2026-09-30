"""Tests for killing from the list: `x` on the process screen and on the
left-behind line, and the box that lists what the kill takes before it does.
The owner's rules (2026-09-27): the box lists every process with its notes and
what is spared - the CLI's list; Enter or `y` kills, Esc or `n` closes; a
visible [kill] target for the mouse; the result replaces the list in the box.
The world is the fake collector's: nothing here reads or signals the machine."""
import unittest

import ccwho_ui as ui


_UNPIN = []


def setUpModule():
    import ccwho
    import testkit
    _UNPIN.append(testkit.pin_ccwho_dir(ccwho))


def tearDownModule():
    while _UNPIN:
        _UNPIN.pop()()
from test_ui import BUSY, HOLDING, PROCS, FakeCollector, UiTest

KILL = {"pid": 20, "ppid": 1, "start": "Tue Sep 22 13:45:15 2026", "command": "next-server",
        "ports": [8080], "harness": "claude", "session": None, "marked": None, "root": 20,
        "note": "no agent started it"}
SPARED = {"pid": 30, "command": "vite", "ports": [], "why": "30 is piped to 10, an agent - not killed"}


def prep(mode="pid", target=20, kill=(KILL,), spare=(), why=None, refused=False):
    return {"mode": mode, "target": target, "mine": None, "why": why, "refused": refused,
            "plan": {"kill": [dict(e) for e in kill], "spare": [dict(e) for e in spare]}}


REPORT = {"killed": [dict(KILL)], "survivors": [], "spare": [], "new": [],
          "ports": [":8080 is free"], "held": []}


class KillCollector(FakeCollector):
    def __init__(self, prepared=None, report=None):
        super().__init__(fleet=ui.Fleet([HOLDING, BUSY], True, "12:00:00", procs=PROCS))
        self.prep_value = prepared or prep()
        self.report_value = report or dict(REPORT)
        self.prepared, self.carried = [], []

    def kill_prepare(self, mode, target):
        self.prepared.append((mode, target))
        return self.prep_value

    def kill_carry(self, prepared):
        self.carried.append(prepared)
        return self.report_value


class KillTest(UiTest):
    async def into_procs(self, pilot, steps=0):
        """p, → into the process screen, then ↓ `steps` parts."""
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        for _ in range(steps):
            await pilot.press("down")
        await pilot.pause()

    async def settle(self, app, pilot):
        await app.workers.wait_for_complete()
        await pilot.pause()
        await pilot.pause()

    async def open_box(self, app, pilot):
        """The box for process 20: p, → , ↓↓, x."""
        await self.into_procs(pilot, steps=2)
        await pilot.press("x")
        await self.settle(app, pilot)

    def x_says(self, app):
        """What the footer says `x` does here; None when it shows no `x`."""
        active = app.active_bindings.get("x")
        return active.binding.description if active and active.binding.show else None

    def box(self, app):
        return app.screen if isinstance(app.screen, ui.KillBox) else None

    def box_text(self, app):
        return str(app.screen.query_one("#killbody").content)


class TestTheProcessScreenTakesTheKeys(KillTest):
    async def test_right_takes_the_keys_into_the_processes(self):
        app = self.app(collector=KillCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await self.into_procs(pilot)
            brief = app.query_one("#brief")
            self.assertIs(app.focused, brief)
            self.assertEqual(app.detail_mode, "procs")               # still the processes
            self.assertEqual((brief.values[brief.lit], brief.fields[brief.lit]), ("12", "proc"))

    async def test_the_parts_are_the_processes_and_left_behind(self):
        app = self.app(collector=KillCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await self.into_procs(pilot)
            brief = app.query_one("#brief")
            self.assertEqual([(brief.values[p], brief.fields[p]) for p in sorted(brief.values)],
                             [("12", "proc"), ("ccwho clean", "clean"), ("20", "proc")])


class TestXAsksFirst(KillTest):
    async def test_x_on_a_process_lists_what_the_kill_takes(self):
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.into_procs(pilot, steps=2)                   # on 20
            await pilot.press("x")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [("pid", 20)])
            self.assertIsNotNone(self.box(app))
            text = self.box_text(app)
            self.assertIn("1 process to kill:", text)
            self.assertIn("next-server", text)
            self.assertIn("! no agent started it", text)
            self.assertEqual(c.carried, [])                          # nothing yet

    async def test_x_on_left_behind_cleans_it(self):
        c = KillCollector(prep(mode="clean", target=None))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.into_procs(pilot, steps=1)                   # on the heading
            await pilot.press("x")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [("clean", None)])
            self.assertIsNotNone(self.box(app))

    async def test_a_click_on_the_left_behind_line_cleans_it(self):
        c = KillCollector(prep(mode="clean", target=None))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#procline")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [("clean", None)])
            self.assertIsNotNone(self.box(app))

    async def test_x_on_the_list_is_still_the_loop_kill(self):          # control
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("x")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [])
            self.assertIsNone(self.box(app))

    async def test_the_spared_and_the_why_are_listed(self):
        c = KillCollector(prep(spare=(SPARED,), refused=True))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.into_procs(pilot, steps=2)
            await pilot.press("x")
            await self.settle(app, pilot)
            self.assertIn("not killed: 30 is piped to 10", self.box_text(app))


class TestTheBoxDecides(KillTest):

    async def test_y_kills_and_the_result_replaces_the_list(self):
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_box(app, pilot)
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertEqual([p["target"] for p in c.carried], [20])
            text = self.box_text(app)
            self.assertIn("killed 1: 20", text)
            self.assertIn(":8080 is free", text)
            self.assertNotIn("to kill:", text)

    async def test_enter_kills_too(self):
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_box(app, pilot)
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.assertEqual(len(c.carried), 1)

    async def test_the_kill_target_is_clickable(self):
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_box(app, pilot)
            await pilot.click("#killgo")
            await self.settle(app, pilot)
            self.assertEqual(len(c.carried), 1)

    async def test_escape_and_n_close_without_a_kill(self):
        for key in ("escape", "n"):
            c = KillCollector()
            app = self.app(collector=c)
            async with app.run_test(size=(160, 40)) as pilot:
                await self.open_box(app, pilot)
                await pilot.press(key)
                await self.settle(app, pilot)
                self.assertIsNone(self.box(app), key)
                self.assertEqual(c.carried, [], key)

    async def test_it_kills_once(self):
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_box(app, pilot)
            await pilot.press("y")
            await self.settle(app, pilot)
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertEqual(len(c.carried), 1)

    async def test_the_result_closes_with_escape_or_enter(self):
        for key in ("escape", "enter"):
            c = KillCollector()
            app = self.app(collector=c)
            async with app.run_test(size=(160, 40)) as pilot:
                await self.open_box(app, pilot)
                await pilot.press("y")
                await self.settle(app, pilot)
                await pilot.press(key)
                await self.settle(app, pilot)
                self.assertIsNone(self.box(app), key)
                self.assertEqual(len(c.carried), 1, key)

class TestTheBoxEnds(KillTest):
    """What the box does when there is nothing to confirm, and after a kill.
    (A class of its own: each class must run inside the 30 s kill.)"""

    async def test_nothing_to_kill_cannot_be_confirmed(self):
        c = KillCollector(prep(kill=(), spare=(SPARED,), why="nothing holds :9"))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_box(app, pilot)
            self.assertIn("ccwho: nothing holds :9", self.box_text(app))
            self.assertFalse(app.screen.query_one("#killgo").display)
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertEqual(c.carried, [])

    async def test_a_why_before_the_plan_is_shown(self):
        c = KillCollector(prep(kill=(), why="a live session's pid could not be read - nothing killed"))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_box(app, pilot)
            self.assertIn("could not be read - nothing killed", self.box_text(app))
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertEqual(c.carried, [])

    async def test_after_a_kill_the_list_is_read_again(self):
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_box(app, pilot)
            before = c.calls
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertGreater(c.calls, before)

    async def test_a_kill_that_fails_says_so(self):
        c = KillCollector()

        def boom(prepared):
            c.carried.append(prepared)
            raise RuntimeError("sk-ant-" + "Q" * 30)
        c.kill_carry = boom
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_box(app, pilot)
            await pilot.press("y")
            await self.settle(app, pilot)
            text = self.box_text(app)
            self.assertIn("may have been signalled", text)
            self.assertIn("RuntimeError", text)
            self.assertNotIn("sk-ant-", text)


class TestTheEdges(KillTest):
    async def test_x_on_a_brief_value_does_nothing(self):
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")              # the brief
            await pilot.press("right")              # the keys into its values
            await pilot.pause()
            self.assertEqual(app.detail_mode, "brief")
            self.assertIs(app.focused, app.query_one("#brief"))
            await pilot.press("x")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [])
            self.assertIsNone(self.box(app))

    async def test_one_kill_at_a_time(self):
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.kill_busy = True
            app.ask_kill("pid", "20")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [])
            self.assertIn("a kill is under way", app.status)

    async def test_after_the_box_closes_x_asks_again(self):
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.into_procs(pilot, steps=2)
            await pilot.press("x")
            await self.settle(app, pilot)
            await pilot.press("escape")
            await self.settle(app, pilot)
            await pilot.press("x")
            await self.settle(app, pilot)
            self.assertEqual(len(c.prepared), 2)
            self.assertIsNotNone(self.box(app))

    async def test_a_why_cannot_be_confirmed_even_with_a_list(self):
        c = KillCollector(prep(why="a live session's pid could not be read - nothing killed"))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.into_procs(pilot, steps=2)
            await pilot.press("x")
            await self.settle(app, pilot)
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertEqual(c.carried, [])

    async def test_the_box_stays_while_the_kill_runs(self):
        import threading
        c, go = KillCollector(), threading.Event()
        real = c.kill_carry

        def slow(prepared):
            go.wait(5)
            return real(prepared)
        c.kill_carry = slow
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.into_procs(pilot, steps=2)
            await pilot.press("x")
            await self.settle(app, pilot)
            await pilot.press("y")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsNotNone(self.box(app))                      # it says what it did
            go.set()
            await self.settle(app, pilot)
            self.assertIn("killed 1: 20", self.box_text(app))

    async def test_nothing_signalled_says_nothing_killed(self):
        c = KillCollector()

        def boom(prepared):
            err = RuntimeError("x")
            err.ccwho_nothing_signalled = True
            raise err
        c.kill_carry = boom
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.into_procs(pilot, steps=2)
            await pilot.press("x")
            await self.settle(app, pilot)
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertIn("failed (RuntimeError) - nothing killed", self.box_text(app))

    async def test_a_click_with_nothing_left_behind_does_nothing(self):
        c = KillCollector()
        c.fleet_value = ui.Fleet([HOLDING, BUSY], True, "12:00:00", procs=dict(PROCS, left_behind=[]))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.clicked_left_behind(None)
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [])

    async def test_an_error_while_reading_shows_its_type_only(self):
        c = KillCollector()
        c.kill_prepare = lambda mode, target: (_ for _ in ()).throw(RuntimeError("sk-ant-" + "Q" * 30))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.into_procs(pilot, steps=2)
            await pilot.press("x")
            await self.settle(app, pilot)
            text = self.box_text(app)
            self.assertIn("failed (RuntimeError) - nothing killed", text)
            self.assertNotIn("sk-ant-", text)


def many(n):
    return prep(kill=[dict(KILL, pid=100 + i, command=f"worker {i}") for i in range(n)])


class TestALongList(KillTest):
    """Nothing is killed that the person did not see: every row of a list
    longer than the box must have been on screen before Enter or `y` kills
    (H1, review 1; H1' and M, review 2)."""

    async def test_the_kill_bar_is_inside_the_box_at_every_height(self):
        for h in (24, 40, 60, 100):
            app = self.app(collector=KillCollector(many(80)))
            async with app.run_test(size=(160, h)) as pilot:
                await self.open_box(app, pilot)
                box = app.screen.query_one("#killbox").content_region
                for part in ("#killgo", "#killhint", "#killscroll", "#killhead"):
                    r = app.screen.query_one(part).region
                    self.assertTrue(box.contains_region(r), (h, part, r, box))

    async def test_end_alone_is_not_seeing_it(self):
        c = KillCollector(many(40))
        app = self.app(collector=c)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.open_box(app, pilot)
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.assertEqual(c.carried, [])
            self.assertIn("scroll through", str(app.screen.query_one("#killhint").content))
            await pilot.press("end")
            await pilot.pause(ui.KillBox.REST + 0.2)                 # resting at the end
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.assertEqual(c.carried, [])                           # the middle was skipped

    async def test_paging_through_it_is(self):
        c = KillCollector(many(40))
        app = self.app(collector=c)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.open_box(app, pilot)
            scroll = app.screen.query_one("#killscroll")
            for _ in range(40):
                if scroll.scroll_y >= scroll.max_scroll_y:
                    break
                was = scroll.scroll_y
                await pilot.press("pagedown")
                await pilot.pause(ui.KillBox.REST + 0.2)            # a person reads the page
                if scroll.scroll_y == was:
                    break                   # PgDn did not move it: fail now, not in 18 s
            self.assertGreater(scroll.scroll_y, 0)
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.assertEqual(len(c.carried), 1)

    async def test_a_resize_that_hides_lines_says_so_at_once(self):
        c = KillCollector(many(12))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 60)) as pilot:
            await self.open_box(app, pilot)
            self.assertIn("kills", str(app.screen.query_one("#killhint").content))   # all shown
            await pilot.resize_terminal(160, 16)
            await pilot.pause()
            await pilot.pause()
            self.assertIn("scroll through", str(app.screen.query_one("#killhint").content))
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertEqual(c.carried, [])

    async def test_a_short_list_kills_at_once(self):                     # control
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_box(app, pilot)
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.assertEqual(len(c.carried), 1)

    async def test_mounted_but_not_laid_out_is_not_seen(self):
        app = self.app(collector=KillCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            box, went = ui.KillBox("kill 20", ["1 process to kill:", "  20 x"], True), []
            box.on_kill = lambda: went.append(1)
            await app.push_screen(box)          # composed; the layout has not run
            box.action_go()
            self.assertEqual(went, [])
            await pilot.pause()
            await pilot.pause(ui.KillBox.REST + 0.1)
            box.action_go()                                              # control: laid out, seen
            self.assertEqual(went, [1])

    def test_no_rows_yet_is_not_seen(self):
        # composed, not laid out: 0 rows must not read as "all 0 seen"
        from types import SimpleNamespace as NS
        box = ui.KillBox("kill 20", ["x"], True)
        empty = NS(size=NS(height=0), virtual_size=NS(height=0), scroll_y=0)

        class Found(list):
            def first(self):
                return self[0]
        box.query = lambda sel: Found([empty])
        self.assertEqual(box.unseen(), 1)
        laid = NS(size=NS(height=5), virtual_size=NS(height=3), scroll_y=0)
        box.query = lambda sel: Found([laid])
        box.seen = {0, 1, 2}
        self.assertEqual(box.unseen(), 0)                                # control

    def test_before_the_layout_nothing_is_seen(self):
        box, went = ui.KillBox("kill 20", ["1 process to kill:", "  20 x"], True), []
        box.on_kill = lambda: went.append(1)
        box.action_go()                     # not composed, no size: fail closed
        self.assertEqual(went, [])


class TestTheKeysStayOnTheirProcess(KillTest):
    """A refresh never moves `x` onto another process (M1, review 1)."""

    async def test_a_process_added_above(self):
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.into_procs(pilot, steps=2)                   # on 20
            brief = app.query_one("#brief")
            self.assertEqual(brief.values[brief.lit], "20")
            more = dict(PROCS, left_behind=[dict(PROCS["left_behind"][0], pid=11)]
                        + PROCS["left_behind"])
            app.show(ui.Fleet([HOLDING, BUSY], True, "12:00:03", procs=more))
            await pilot.pause()
            self.assertEqual(brief.values[brief.lit], "20")

    async def test_a_process_that_went_is_no_target(self):
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.into_procs(pilot, steps=2)                   # on 20
            gone = dict(PROCS, left_behind=[dict(PROCS["left_behind"][0], pid=21)])
            app.show(ui.Fleet([HOLDING, BUSY], True, "12:00:03", procs=gone))
            await pilot.pause()
            brief = app.query_one("#brief")
            self.assertIsNone(brief.lit)
            await pilot.press("x")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [])

    async def test_x_on_the_brief_pid_does_nothing(self):
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("right")
            await pilot.pause()
            brief = app.query_one("#brief")
            at = [p for p, f in brief.fields.items() if f == "pid"]
            self.assertTrue(at, "the brief shows the session's pid")    # control: it is there
            brief.lit = at[0]
            await pilot.press("x")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [])


class TestOneKillAtATime(KillTest):
    async def test_x_twice_while_reading(self):
        import threading
        c, go = KillCollector(), threading.Event()
        real = c.kill_prepare

        def slow(mode, target):
            go.wait(5)
            return real(mode, target)
        c.kill_prepare = slow
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.into_procs(pilot, steps=2)
            await pilot.press("x")
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            self.assertIn("a kill is under way", app.status)
            go.set()
            await self.settle(app, pilot)
            self.assertEqual(len(c.prepared), 1)
            self.assertEqual(sum(isinstance(s, ui.KillBox) for s in app.screen_stack), 1)

    async def test_a_box_that_cannot_open_frees_the_lock(self):
        # the plan has a shape the list cannot print: said by its type, and the
        # next x works - the lock is not kept until a restart
        c = KillCollector()
        real = ui.engine.kill_list_lines
        def boom(plan):
            raise KeyError("/secret/path")
        ui.engine.kill_list_lines = boom
        self.addCleanup(setattr, ui.engine, "kill_list_lines", real)
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_box(app, pilot)
            self.assertTrue(app.is_running)
            self.assertIsNone(self.box(app))
            self.assertFalse(app.kill_busy)
            self.assertIn("KeyError", app.status)
            self.assertNotIn("/secret/path", app.status)
            ui.engine.kill_list_lines = real            # the next x: one box, as usual
            await pilot.press("x")
            await self.settle(app, pilot)
            self.assertEqual(sum(isinstance(s, ui.KillBox) for s in app.screen_stack), 1)
            self.assertEqual(len(c.prepared), 2)

    async def test_a_refresh_that_fails_after_a_kill_keeps_the_app(self):
        c = KillCollector()
        real = c.fleet
        calls = []

        def fleet():
            calls.append(1)
            if c.carried:
                raise RuntimeError("ps broke")
            return real()
        c.fleet = fleet
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_box(app, pilot)
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertTrue(app.is_running)
            self.assertIn("killed 1: 20", self.box_text(app))

    async def test_only_the_left_behind_line_cleans(self):
        c = KillCollector()
        c.fleet_value = ui.Fleet([HOLDING, BUSY], True, "12:00:00",
                                 procs=dict(PROCS, codex=[dict(PROCS["left_behind"][0], pid=30)]))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.click("#procline", offset=(2, 1))              # the codex line
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [])
            await pilot.click("#procline", offset=(2, 0))              # control
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [("clean", None)])


class TestPTakesTheKeys(KillTest):
    """`p` opens the process screen with the keys in it, and the footer says
    what `x` does on the line they are on (the owner, 2026-09-27: "there's no
    indication of what actions are available")."""

    async def test_p_puts_the_keys_on_the_first_process(self):
        app = self.app(collector=KillCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            brief = app.query_one("#brief")
            self.assertIs(app.focused, brief)
            self.assertEqual((brief.values[brief.lit], brief.fields[brief.lit]), ("12", "proc"))

    async def test_x_then_kills_without_a_right(self):
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.press("down", "down")
            await pilot.press("x")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [("pid", 20)])

    async def test_the_footer_says_what_x_does_here(self):
        app = self.app(collector=KillCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            self.assertEqual(self.x_says(app), "kill")                  # on 12
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(self.x_says(app), "clean all")             # on LEFT BEHIND
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(self.x_says(app), "kill")                  # on 20

    async def test_the_brief_offers_no_kill(self):                       # control
        app = self.app(collector=KillCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right")
            await pilot.press("right")
            await pilot.pause()
            self.assertEqual(app.detail_mode, "brief")
            self.assertNotIn(self.x_says(app), ("kill", "clean all"))

    async def test_x_on_the_heading_cleans(self):
        c = KillCollector(prep(mode="clean", target=None))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.press("down")
            await pilot.press("x")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [("clean", None)])

    async def test_the_actions_act_only_on_their_line(self):
        # the footer hides them elsewhere; the actions check again themselves
        c = KillCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            brief = app.query_one("#brief")
            brief.action_clean()                    # on process 12: not a heading
            brief.lit = 1                           # on LEFT BEHIND: not a process
            brief.action_kill()
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [])
            await pilot.press("escape")
            await pilot.press("right", "right")     # the session brief
            await pilot.pause()
            brief.lit = [p for p, f in brief.fields.items() if f == "pid"][0]
            brief.action_kill()                     # its pid is the session's claude
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [])

class TestTheFooterFollows(KillTest):
    """The rest of TestPTakesTheKeys, split off so each class runs under the
    30 s kill: the footer after a refresh, the brief, and the way back."""

    async def test_the_footer_follows_a_refresh(self):
        # keys on 12; a refresh without it leaves the keys on nothing: no "kill"
        app = self.app(collector=KillCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            shown = lambda: [k.description for k in app.query("FooterKey")]
            self.assertIn("kill", shown())
            gone = dict(PROCS, by_session={})
            app.show(ui.Fleet([HOLDING, BUSY], True, "12:00:03", procs=gone))
            await pilot.pause()
            await pilot.pause()
            self.assertNotIn("kill", shown())

    async def test_x_in_a_session_brief_asks_about_that_session(self):
        # no kill or clean there: `x` is the session's own box, which asks
        # first - never a kill straight away
        from test_ui import LIVE, STUCK
        c = FakeCollector(fleet=ui.Fleet([LIVE, STUCK], True, "12:00:00"))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.selected = STUCK["sessionId"]
            app.restore_selection()
            await pilot.pause()
            await pilot.press("right", "right")
            await pilot.pause()
            self.assertIs(app.focused, app.query_one("#brief"))
            await pilot.press("x")
            await pilot.pause()
            self.assertIsInstance(app.screen, ui.ChoiceBox)
            self.assertIn(STUCK["sessionId"][:4], app.screen.title_text)
            await pilot.press("x")
            await pilot.pause()
            self.assertIsNone(c.killed)

    async def test_p_from_a_brief_lands_on_the_first_process(self):
        app = self.app(collector=KillCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("right", "right")
            await pilot.press("down", "down", "down")
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            brief = app.query_one("#brief")
            self.assertEqual((brief.values.get(brief.lit), brief.fields.get(brief.lit)), ("12", "proc"))

    async def test_one_escape_goes_back_to_the_list(self):
        app = self.app(collector=KillCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            # the Esc is the process list's own, not the list's (which also closes)
            self.assertIs(app.focused, app.query_one("#brief"))
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.detail_open)
            self.assertFalse(app.query_one("#detail").display)
            self.assertTrue(isinstance(app.focused, ui.Row))


class TestTheCollectorKills(unittest.TestCase):
    """The real collector reaches the engine: prepare, then carry the plan out."""

    def test_prepare_and_carry_go_to_the_engine(self):
        seen = []
        real_prep, real_carry = ui.engine.kill_prepare, ui.engine.carry_out
        ui.engine.kill_prepare = lambda mode, target: seen.append(("prep", mode, target)) or prep()
        ui.engine.carry_out = (lambda mode, target, confirmed, force=False, mine=None:
                               seen.append(("carry", mode, target, [e["pid"] for e in confirmed],
                                            mine)) or dict(REPORT))
        try:
            c = ui.Collector()
            p = c.kill_prepare("pid", 20)
            r = c.kill_carry(p)
        finally:
            ui.engine.kill_prepare, ui.engine.carry_out = real_prep, real_carry
        self.assertEqual(seen, [("prep", "pid", 20), ("carry", "pid", 20, [20], None)])
        self.assertEqual(r["killed"][0]["pid"], 20)

    def test_an_agents_kill_stays_the_agents(self):
        seen = []
        real = ui.engine.carry_out
        ui.engine.carry_out = (lambda mode, target, confirmed, force=False, mine=None:
                               seen.append(mine) or dict(REPORT))
        try:
            ui.Collector().kill_carry(dict(prep(), mine="dddd4444-0000-4000-8000-00000000dead"))
        finally:
            ui.engine.carry_out = real
        self.assertEqual(seen, ["dddd4444-0000-4000-8000-00000000dead"])

    def test_a_stop_runs_ccwho_stop_and_says_what_it_said(self):
        # `ccwho stop <id> --yes`: every rule of the CLI (the job, the wait, what it left)
        import subprocess
        import sys
        seen, kwargs = [], []
        real = subprocess.run
        def run(argv, **kw):
            seen.append(argv)
            kwargs.append(kw)
            return subprocess.CompletedProcess(argv, 0, "stopped - its conversation is kept\n",
                                               "")
        subprocess.run = run
        try:
            lines = ui.Collector().stop_session(BG_ROW)
        finally:
            subprocess.run = real
        self.assertEqual(seen[0][0], sys.executable)
        self.assertTrue(seen[0][1].endswith("ccwho.py"))
        self.assertEqual(seen[0][2:], ["stop", BG_ROW["sessionId"], "--yes"])
        self.assertIs(kwargs[0].get("stdin"), subprocess.DEVNULL)  # never the TUI's terminal
        self.assertEqual(lines, ["stopped - its conversation is kept"])

    def test_a_stop_that_cannot_run_says_its_type_only(self):
        import subprocess
        real = subprocess.run
        def run(argv, **kw):
            raise OSError("/secret/path")
        subprocess.run = run
        try:
            lines = ui.Collector().stop_session(BG_ROW)
        finally:
            subprocess.run = real
        self.assertEqual(len(lines), 1)
        self.assertIn("could not run the stop (OSError)", lines[0])
        self.assertNotIn("/secret/path", lines[0])

    def test_a_stop_that_fails_says_its_type_only(self):
        import subprocess
        real = subprocess.run
        def run(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 60)
        subprocess.run = run
        try:
            lines = ui.Collector().stop_session(BG_ROW)
        finally:
            subprocess.run = real
        self.assertEqual(len(lines), 1)
        self.assertIn("TimeoutExpired", lines[0])
        self.assertNotIn("could not run", lines[0])           # claude stop may have run
        self.assertIn("may have been stopped", lines[0])
        self.assertIn("run ccwho ls", lines[0])


BG_ROW = dict(HOLDING, kind="background")


class RowCollector(KillCollector):
    def __init__(self, rows=(HOLDING, BUSY), **kw):
        super().__init__(**kw)
        self.fleet_value = ui.Fleet(list(rows), True, "12:00:00", procs=PROCS)
        self.stopped = []

    def kill_prepare(self, mode, target):
        self.prepared.append((mode, target))
        return dict(self.prep_value, mode=mode, target=target)

    def stop_session(self, row):
        self.stopped.append(row["sessionId"])
        return ["stopped - its conversation is kept: claude attach aaaa1111"]


class RowBoxTest(KillTest):
    def choices(self, app):
        return app.screen if isinstance(app.screen, ui.ChoiceBox) else None

    def choice_text(self, app):
        return "\n".join(str(w.content) for w in app.screen.query("Static"))

    async def open_choices(self, app, pilot):
        await pilot.pause()
        await pilot.press("x")
        await pilot.pause()


class TestXOnARow(RowBoxTest):
    """`x` on a session row: a box of what can be done to it (the owner's D10,
    2026-09-27) - its processes, a stop for a background session, its stuck
    loop - each then asked about on its own."""

    async def test_x_offers_what_fits_the_row(self):
        app = self.app(collector=RowCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_choices(app, pilot)
            self.assertIsNotNone(self.choices(app))
            text = self.choice_text(app)
            self.assertIn("p  kill its processes  :3000", text)
            self.assertNotIn("stop the session", text)          # interactive
            self.assertIn("end it there", text)
            self.assertNotIn("kill the stuck loop", text)

    async def test_p_asks_about_the_sessions_processes(self):
        c = RowCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_choices(app, pilot)
            await pilot.press("p")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [("session", HOLDING["sessionId"])])
            self.assertIsNotNone(self.box(app))
            self.assertIn("aaaa", str(app.screen.query_one("#killhead").content))
            self.assertEqual(c.carried, [])

    async def test_the_process_screen_offers_no_row_x(self):
        # that screen is not about the selected row: no `x` in the footer, and
        # an `x` that reaches the row says why
        app = self.app(collector=RowCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(self.x_says(app), "kill or stop")          # control
            await pilot.press("p")
            await pilot.pause()
            app.query(ui.Row).first().focus()
            await pilot.pause()
            self.assertIsNone(self.x_says(app))
            await pilot.press("x")
            await pilot.pause()
            self.assertIsNone(self.choices(app))
            self.assertIn("Esc", app.status)

    async def test_no_box_while_a_kill_is_under_way(self):
        c = RowCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            app.kill_busy = True
            await self.open_choices(app, pilot)
            self.assertIsNone(self.choices(app))
            self.assertIn("a kill is under way", app.status)

    async def test_escape_closes_and_does_nothing(self):
        c = RowCollector(rows=(BG_ROW, BUSY))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_choices(app, pilot)
            await pilot.press("escape")
            await self.settle(app, pilot)
            self.assertIsNone(self.choices(app))
            self.assertEqual((c.prepared, c.stopped), ([], []))

    async def test_a_click_on_a_choice_picks_it(self):
        c = RowCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_choices(app, pilot)
            await pilot.click("#choice-p")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [("session", HOLDING["sessionId"])])

    async def test_a_key_the_box_does_not_offer_does_nothing(self):
        c = RowCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_choices(app, pilot)
            await pilot.press("s")                              # interactive: no stop
            await self.settle(app, pilot)
            self.assertIsNotNone(self.choices(app))
            self.assertEqual((c.prepared, c.stopped), ([], []))

    async def test_a_row_with_nothing_to_do_says_so(self):
        c = RowCollector(rows=(BUSY,))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_choices(app, pilot)
            self.assertIsNone(self.choices(app))
            self.assertIn("end it in its window", app.status)

    async def test_the_footer_follows_a_refresh_of_the_same_row(self):
        # the keys stay on the row; a refresh gives it processes: now `x` acts
        bare = dict(HOLDING, procs=0, ports=[])
        c = RowCollector(rows=(bare, BUSY))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            self.assertIsNone(self.x_says(app))
            app.show(ui.Fleet([HOLDING, BUSY], True, "12:00:05", procs=PROCS))
            await pilot.pause()
            self.assertEqual(self.x_says(app), "kill or stop")

    async def test_the_footer_names_x_only_where_it_does_something(self):
        app = self.app(collector=RowCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(self.x_says(app), "kill or stop")          # HOLDING: its processes
            await pilot.press("j")                                      # BUSY: nothing
            await pilot.pause()
            self.assertIsNone(self.x_says(app))


class TestStopFromARow(RowBoxTest):
    """`s` in the row's box: a background session is stopped after a box that
    asks, by `ccwho stop` (split from TestXOnARow for the 30 s kill)."""

    async def test_a_background_session_can_be_stopped_after_its_box(self):
        c = RowCollector(rows=(BG_ROW, BUSY))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_choices(app, pilot)
            self.assertIn("s  stop the session", self.choice_text(app))
            await pilot.press("s")
            await pilot.pause()
            self.assertIsNotNone(self.box(app))
            self.assertIn("conversation is kept", self.box_text(app))
            self.assertEqual(str(app.screen.query_one("#killgo").content).strip(), "stop")
            self.assertIn("y or Enter stops", str(app.screen.query_one("#killhint").content))
            self.assertEqual(c.stopped, [])                     # asked, not done
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertEqual(c.stopped, [BG_ROW["sessionId"]])
            self.assertIn("claude attach aaaa1111", self.box_text(app))

    async def test_no_stop_while_a_kill_is_under_way(self):
        c = RowCollector(rows=(BG_ROW, BUSY))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_choices(app, pilot)
            app.kill_busy = True        # a kill began while the box was open
            await pilot.press("s")
            await pilot.pause()
            self.assertIsNone(self.box(app))
            self.assertIn("a kill is under way", app.status)
            self.assertEqual(c.stopped, [])

    async def test_the_stop_box_holds_the_one_kill_at_a_time(self):
        c = RowCollector(rows=(BG_ROW, BUSY))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_choices(app, pilot)
            await pilot.press("s")
            await pilot.pause()
            self.assertTrue(app.kill_busy)
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertTrue(app.kill_busy)                      # its answer is on screen
            await pilot.press("enter")
            await pilot.pause()
            self.assertFalse(app.kill_busy)

    async def test_n_on_the_stop_box_stops_nothing(self):
        c = RowCollector(rows=(BG_ROW, BUSY))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_choices(app, pilot)
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("n")
            await self.settle(app, pilot)
            self.assertEqual(c.stopped, [])
            self.assertIsNone(self.box(app))


class GatedLoopCollector(FakeCollector):
    """kill_loops waits for the test to let it go; fleet() may fail after it."""

    def __init__(self, fleet, fail_fleet=False):
        super().__init__(fleet=fleet)
        import threading
        self.gate, self.loop_calls, self.fail_fleet, self.done = threading.Event(), 0, fail_fleet, False

    def kill_loops(self, row):
        self.loop_calls += 1           # not `calls`: FakeCollector counts fleet() in it
        self.gate.wait(10)
        self.done = True
        return ["killed loop 86246"]

    def fleet(self):
        if self.done and self.fail_fleet:
            raise RuntimeError("ps failed")
        return super().fleet()


class TestOneLoopKillAtATime(RowBoxTest):
    """A loop kill holds the one-kill-at-a-time lock (review round 2): pressed
    again while it runs, the same pids are not killed twice. The kill dialog
    holds it from when it opens until it closes, as every KillBox does."""

    def fleet(self):
        from test_ui import LIVE, STUCK
        return ui.Fleet([LIVE, STUCK], True, "12:00:00")

    def said(self, app):
        return self.box_text(app) if self.box(app) else app.status

    async def test_x_while_a_loop_kill_runs_opens_no_box(self):
        c = GatedLoopCollector(self.fleet())
        app = self.app(collector=c)
        async with app.run_test(size=(120, 30)) as pilot:
            try:
                await pilot.pause()
                await pilot.press("j", "x", "l", "y")
                await pilot.pause(0.2)
                self.assertTrue(app.kill_busy)
                app.action_row_x()
                await pilot.pause()
                self.assertIsNone(self.choices(app))
                self.assertIn("a kill is under way", app.status)
            finally:
                c.gate.set()
            await self.settle(app, pilot)
            self.assertEqual(c.loop_calls, 1)
            self.assertIn("killed loop 86246", self.said(app))
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.kill_busy)

    async def test_a_failed_list_read_after_the_kill_frees_the_lock(self):
        c = GatedLoopCollector(self.fleet(), fail_fleet=True)
        c.gate.set()
        app = self.app(collector=c)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x", "l", "y")
            await self.settle(app, pilot)
            self.assertIn("killed loop 86246", self.said(app))   # what it did is still said
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.kill_busy)
            self.assertTrue(app.is_running)

    async def test_l_after_a_kill_began_kills_nothing(self):
        c = GatedLoopCollector(self.fleet())
        c.gate.set()
        app = self.app(collector=c)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x")
            await pilot.pause()
            app.kill_busy = True        # a kill began while the box was open
            await pilot.press("l")
            await pilot.pause(0.2)
            self.assertIsNone(self.box(app))
            self.assertEqual(c.loop_calls, 0)
            self.assertIn("a kill is under way", app.status)

    async def test_a_kill_that_raises_says_so_in_the_box(self):
        c = GatedLoopCollector(self.fleet())
        def boom(row):
            raise RuntimeError("/secret/path")
        c.kill_loops = boom
        app = self.app(collector=c)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x", "l", "y")
            await self.settle(app, pilot)
            self.assertIn("could not kill", self.said(app))
            self.assertNotIn("/secret/path", self.said(app))
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.kill_busy)

    async def test_a_direct_x_on_a_row_with_nothing_opens_no_box(self):
        # the keys never reach it there (check_action), but the guard holds alone
        app = self.app(collector=RowCollector(rows=(BUSY,)))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.action_row_x()
            await pilot.pause()
            self.assertIsNone(self.choices(app))
            self.assertIn("end it in its window", app.status)


class TestAStuckProcessHasItsOwnKillBox(RowBoxTest):
    """The stuck-process button opens the kill dialog about the stuck items
    alone - what each is, why it can never stop, that the session keeps
    running - and `y` kills them; the result stays in the box (design review
    2026-09-29, decisions 1, 2, 5, 6). `x` keeps the whole box, stuck first."""

    def rows(self, row=None):
        from test_ui import LIVE, STUCK
        return FakeCollector(fleet=ui.Fleet([LIVE, row or STUCK], True, "12:00:00"))

    def widget(self, app, sid):
        return [w for w in app.query(ui.Row) if w.row["sessionId"] == sid][0]

    async def click_button(self, app, pilot, sid):
        await pilot.pause()
        w = self.widget(app, sid)
        line = "".join(t for t, _ in w.spans()).split("\n")[1]
        await pilot.click(w, offset=(line.index(ui.engine.UI_KILL) + 3, 1))
        await pilot.pause(0.2)

    def head(self, app):
        return str(app.screen.query_one("#killhead").content)

    async def test_the_button_opens_the_kill_dialog_with_only_the_stuck_item(self):
        from test_ui import STUCK
        c = self.rows(dict(STUCK, procs=16, kind="background"))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.click_button(app, pilot, STUCK["sessionId"])
            self.assertIsNotNone(self.box(app))
            self.assertIsNone(self.choices(app))
            self.assertEqual(self.head(app), "kill stuck loop 86246")
            text = self.box_text(app)
            self.assertIn("loop 86246 waits on bscl8fc6k, which has ended", text)
            self.assertIn("the session keeps running", text)
            # no fallback: nothing about all its processes, nor a stop
            self.assertNotIn("processes", text)
            self.assertNotIn("stop", text)
            self.assertIsNone(c.killed)

    async def test_y_kills_and_the_result_stays_until_closed(self):
        from test_ui import STUCK
        c = self.rows()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.click_button(app, pilot, STUCK["sessionId"])
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertEqual(c.killed, [STUCK["sessionId"]])
            self.assertIn("killed loop 86246", self.box_text(app))
            self.assertTrue(app.kill_busy, "the box is open: one kill at a time")
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsNone(self.box(app))
            self.assertFalse(app.kill_busy)

    async def test_n_kills_nothing(self):                                   # control
        from test_ui import STUCK
        c = self.rows()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.click_button(app, pilot, STUCK["sessionId"])
            await pilot.press("n")
            await pilot.pause(0.2)
            self.assertIsNone(self.box(app))
            self.assertIsNone(c.killed)
            self.assertFalse(app.kill_busy)

    async def test_the_x_box_puts_the_stuck_item_first_with_its_reason(self):
        from test_ui import STUCK
        c = self.rows(dict(STUCK, procs=16, kind="background"))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x")
            await pilot.pause()
            box = self.choices(app)
            self.assertEqual([k for k, _ in box.choices], ["l", "p", "s"])
            label = box.choices[0][1]
            self.assertTrue(label.startswith("kill stuck loop 86246 - the session keeps running"))
            self.assertIn("loop 86246 waits on bscl8fc6k, which has ended", label)
            self.assertEqual(box.choices[1][1], "kill all its processes (16) - some may not be stuck")

    async def test_a_row_with_nothing_stuck_keeps_its_words(self):          # control
        app = self.app(collector=RowCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_choices(app, pilot)
            text = self.choice_text(app)
            self.assertIn("p  kill its processes  :3000", text)
            self.assertNotIn("some may not be stuck", text)

    async def test_two_stuck_items_are_listed_and_counted(self):
        from test_ui import STUCK
        row = dict(STUCK, dead_loops=[
            {"pid": 86246, "tasks": ["bscl8fc6k"]},
            {"pid": 54329, "tasks": [], "kind": "reader", "program": "cat", "root": 86083,
             "start": "Sat Sep 26 15:12:13 2026"}])
        app = self.app(collector=self.rows(row))
        async with app.run_test(size=(160, 40)) as pilot:
            await self.click_button(app, pilot, STUCK["sessionId"])
            self.assertEqual(self.head(app), "kill 2 stuck processes")
            text = self.box_text(app)
            self.assertIn("loop 86246 waits on bscl8fc6k, which has ended", text)
            self.assertIn("cat 54329 waits for input Claude Code never sends", text)

    async def test_a_stuck_reader_says_its_whole_task_stops(self):
        from test_ui import STUCK
        row = dict(STUCK, dead_loops=[
            {"pid": 54329, "tasks": [], "kind": "reader", "program": "cat", "root": 86083,
             "start": "Sat Sep 26 15:12:13 2026"}])
        app = self.app(collector=self.rows(row))
        async with app.run_test(size=(160, 40)) as pilot:
            await self.click_button(app, pilot, STUCK["sessionId"])
            self.assertEqual(self.head(app), "kill stuck cat 54329")
            self.assertIn("its whole task stops", self.box_text(app))

    async def test_l_on_a_row_that_stopped_being_stuck_opens_nothing(self):
        # the x box was open while the row changed: l must not ask about the
        # old items (review round 1, finding 6)
        from test_ui import STUCK
        c = self.rows(dict(STUCK))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x")
            await pilot.pause()
            from test_ui import LIVE
            moved = dict(STUCK, dead_loops=[{"pid": 99999, "tasks": ["b9"]}])
            app.show(ui.Fleet([LIVE, moved], True, "12:00:05"))
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause(0.2)
            self.assertIsNone(self.box(app))
            self.assertIn("the session changed", app.status)
            self.assertFalse(app.kill_busy)

    READER = {"pid": 54329, "tasks": [], "kind": "reader", "program": "cat", "root": 86083,
              "start": "Sat Sep 26 15:12:13 2026"}

    async def reader_changes_while_asked(self, start):
        from test_ui import LIVE, STUCK
        c = self.rows(dict(STUCK, dead_loops=[dict(self.READER)]))
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await self.click_button(app, pilot, STUCK["sessionId"])
            moved = dict(STUCK, dead_loops=[dict(self.READER, start=start)])
            app.show(ui.Fleet([LIVE, moved], True, "12:03:00"))
            await pilot.pause()
            await pilot.press("y")
            await self.settle(app, pilot)
            return c.killed, self.box_text(app)

    async def test_a_new_reader_on_that_pid_is_not_killed(self):
        # the box was open past the reader's grace; its pid went to a new cat:
        # not the one you saw (review round 2, finding 4)
        killed, said = await self.reader_changes_while_asked("Tue Sep 29 11:00:00 2026")
        self.assertIsNone(killed)
        self.assertIn("the session changed", said)

    async def test_the_same_reader_is_killed(self):                        # control
        killed, _ = await self.reader_changes_while_asked(self.READER["start"])
        self.assertIsNotNone(killed)

    async def test_l_on_the_same_row_opens_the_dialog(self):               # control
        from test_ui import STUCK
        app = self.app(collector=self.rows(dict(STUCK)))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j", "x", "l")
            await pilot.pause(0.2)
            self.assertIsNotNone(self.box(app))

    async def test_no_dialog_while_a_kill_is_under_way(self):
        from test_ui import STUCK
        c = self.rows()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            app.kill_busy = True
            await self.click_button(app, pilot, STUCK["sessionId"])
            self.assertIsNone(self.box(app))
            self.assertIn("a kill is under way", app.status)



if __name__ == "__main__":
    unittest.main()
