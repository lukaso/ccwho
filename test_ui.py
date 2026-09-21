"""Tests for the TUI, driven through Textual's pilot.

These need Textual, which uv fetches: run them with `./test`. They FAIL rather
than skip when it is missing - a suite that reports "OK (skipped=12)" while the
screen is broken is a green light for nothing.

Everything that reaches the world (the session list, iTerm2) is injected, so no
test here depends on what is running on this machine.
"""
import asyncio
import unittest

import ccwho_ui as ui


def row(sid, attention="stopped", **kw):
    base = {"sessionId": sid, "project": "liveapp", "attention": attention,
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

    def due(self, visible, now=None):
        return True

    reloads = 0

    def reload(self):
        type(self).reloads += 1

    def fleet(self):
        self.calls += 1
        return self.fleet_value

    def brief(self, row):
        return self.brief_value


class FakeAdapter:
    def __init__(self, answer="focused s022", hang=False):
        self.answer, self.hang, self.asked = answer, hang, []

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
            await pilot.pause()
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
    """Hidden, the hotkey window still runs. Three subprocesses a tick for a
    window nobody is looking at is how --watch once burned a core."""

    def test_visible_refreshes_often(self):
        c = ui.Collector()
        self.assertTrue(c.due(True, now=1000.0))
        self.assertFalse(c.due(True, now=1000.0 + 1))
        self.assertTrue(c.due(True, now=1000.0 + ui.REFRESH_VISIBLE + 0.1))

    def test_hidden_refreshes_rarely(self):
        c = ui.Collector()
        self.assertTrue(c.due(False, now=1000.0))
        self.assertFalse(c.due(False, now=1000.0 + ui.REFRESH_VISIBLE + 1))
        self.assertTrue(c.due(False, now=1000.0 + ui.REFRESH_HIDDEN + 0.1))


if __name__ == "__main__":
    unittest.main()
