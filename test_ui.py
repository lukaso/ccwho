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

    def restore(self):
        return "reopened 14 session(s)"

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
        self.attached = []

    def attach(self, cmd, deadline=5.0):
        self.attached.append(cmd)
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


class TestTheEmptyListCanBringThemBack(UiTest):
    """The empty list has always said "o = restore the last save" and there was
    no o binding: the screen advertised a key that did nothing. It does it now,
    and only from an empty list - pressing o with fifteen sessions running must
    never start fifteen more."""

    def empty(self):
        return FakeCollector(fleet=ui.Fleet([], True, "12:00:00"))

    def setUp(self):
        self.restored = []
        self.real = FakeCollector.restore
        FakeCollector.restore = lambda s: self.restored.append(1) or "reopened 14"
        self.addCleanup(setattr, FakeCollector, "restore", self.real)

    async def test_o_reopens_the_last_save(self):
        app = self.app(collector=self.empty())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(self.restored, [1])

    async def test_it_says_what_happened(self):
        app = self.app(collector=self.empty())
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            await pilot.pause()
            self.assertIn("reopened", str(app.query_one("#header").content))

    async def test_o_does_nothing_while_sessions_are_running(self):   # control
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            await pilot.pause()
            self.assertEqual(self.restored, [],
                             "fifteen running sessions must not become thirty")

    async def test_the_offer_is_only_made_when_there_is_a_save(self):
        real = FakeCollector.saved_count
        FakeCollector.saved_count = lambda s: 0
        try:
            app = self.app(collector=self.empty())
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertNotIn(" o ", self.screen_text(app))
        finally:
            FakeCollector.saved_count = real

    async def test_the_offer_names_how_many(self):
        real = FakeCollector.saved_count
        FakeCollector.saved_count = lambda s: 14
        try:
            app = self.app(collector=self.empty())
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertIn("14", self.screen_text(app))
        finally:
            FakeCollector.saved_count = real


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
            self.assertEqual(adapter.attached, ["claude attach "
                                                "51fddd61-822b-49e0-9aeb-2145e91e1244"])
            self.assertEqual(adapter.asked, [], "there is no window to focus yet")

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
