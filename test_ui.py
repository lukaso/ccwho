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
            app.painted_shape = None          # force the full path
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


class TestItRestsWhenYouAreNotLookingAtIt(UiTest):
    """The window used to hide itself, so "nobody is looking" took care of
    itself. Now it stays open as a control panel - which means it would run
    three processes every three seconds, all day, behind whatever you are
    actually doing. The terminal tells us when it loses focus; use that."""

    async def test_it_slows_down_when_the_terminal_loses_focus(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app.post_message(ui.events.AppBlur())
            await pilot.pause()
            # Put focus back on a row by hand. Losing focus happens to clear it
            # too, and a test that leans on THAT passes whether or not anything
            # noticed the terminal go away.
            list(app.query(ui.Row))[0].focus()
            await pilot.pause()
            self.assertIsNotNone(app.focused)
            self.assertFalse(app.has_focus_hint())

    async def test_it_wakes_up_the_moment_you_come_back(self):
        collector = FakeCollector()
        app = self.app(collector=collector)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.post_message(ui.events.AppBlur())
            await pilot.pause()
            before = collector.calls
            app.post_message(ui.events.AppFocus())
            await pilot.pause()
            await pilot.pause()
            self.assertTrue(app.has_focus_hint())
            self.assertGreater(collector.calls, before,
                               "a list you just looked at must not be stale")

    async def test_while_you_are_in_it_it_is_awake(self):             # control
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertTrue(app.has_focus_hint())


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
        import os
        with open(os.path.join(os.path.dirname(os.path.abspath(ui.__file__)),
                               "jump.applescript")) as fh:
            return fh.read()

    def test_it_activates_before_it_selects(self):
        body = self.script()
        self.assertLess(body.index("activate"), body.index("select w"),
                        "activating raises windows, which would undo a selection"
                        " made before it")

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
