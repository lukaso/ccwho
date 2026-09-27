"""Tests for killing from the list: `x` on the process screen and on the
left-behind line, and the box that lists what the kill takes before it does.
The owner's rules (2026-09-27): the box lists every process with its notes and
what is spared - the CLI's list; Enter or `y` kills, Esc or `n` closes; a
visible [kill] target for the mouse; the result replaces the list in the box.
The world is the fake collector's: nothing here reads or signals the machine."""
import unittest

import ccwho_ui as ui
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
                await pilot.press("pagedown")
                await pilot.pause(ui.KillBox.REST + 0.2)            # a person reads the page
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

    async def test_x_in_a_session_brief_does_nothing(self):
        # with no kill or clean there, `x` must not fall through to the list's
        # two-press loop kill of the session the brief is about
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
            await pilot.press("x")
            await pilot.pause()
            self.assertIsNone(c.killed)
            self.assertNotIn("kill loop", app.status)

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


if __name__ == "__main__":
    unittest.main()
