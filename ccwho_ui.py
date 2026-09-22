#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["textual>=8.0,<9"]
# ///
"""The list, live, with the brief one keypress away.

Run it as `ccwho` in a terminal, or from the iTerm2 hotkey window. The shebang is
a uv script header: uv fetches Textual on the first run and caches it, so there is
no virtualenv to make and nothing to pip install. Everything it shows comes from
ccwho_engine, which is hot-reloaded, so the rules can be edited while it runs.

What it does that the one-shot table cannot:

  - the second line of every row is the RECAP, with its age
  - -> opens the full brief; at narrow widths it takes the whole screen
  - Enter goes to the window, and nothing here waits for iTerm2 to answer
  - / searches every name a session has, plus what it is about

Collection runs in a worker thread. `claude agents --json` can take 30 seconds
when it is unhappy, and a list that freezes at the moment you reach for it is the
problem this tool exists to solve.
"""
from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from textual import events, on, work                                       # noqa: E402
from textual.app import App, ComposeResult                         # noqa: E402
from textual.binding import Binding                                # noqa: E402
from textual.containers import Horizontal, VerticalScroll          # noqa: E402
from textual.widgets import Footer, Input, Static                  # noqa: E402
from rich.text import Text                                         # noqa: E402

import ccwho_engine as engine                                      # noqa: E402

# What each part of a row is drawn as. The engine says what a part IS; only this
# table says what that looks like, and it is deliberately short: the first screen
# painted whole rows by state, three states shared one colour, and the result was
# a wall of orange that was harder to read than plain text and told you nothing.
ROLE_STYLE = {"mark": "",              # the state's own colour, from STATE_STYLE
              "id": "dim",
              "project": "bold",
              "name": "",              # plain: this is the thing you are reading
              "meta": "dim",
              "age": "bold",           # inside a dim line, the age stands out
              "recap": "dim",
              "pad": ""}

# Three states, three colours, used on one glyph per row and on its heading.
STATE_STYLE = {"needs": "bold #e5a50a",    # amber: this one is waiting for you
               "busy": "#33d17a",          # green: it is working
               "quiet": "dim"}             # grey: nothing is happening

# A warm refresh costs about half a second (measured: 0.50s wall, 0.48s of child
# CPU over 16 sessions - ps, iTerm2 and the transcripts). At three seconds that
# is a fifth of a core, all day, for a panel that sits open. So the list
# refreshes every five, and the moments that MATTER get their own look: when you
# come back to it, and right after you answer a session.
# What the terminal calls this window. Without telling it, iTerm2 names the
# window after the running command - "python3", the interpreter uv happened to
# run - in its window list, in Mission Control and in the window menu.
WINDOW_NAME = "ccwho"

# The cheap check runs often; the full scan is the floor under it. Seeing a
# session start needing you is the whole point of the list, so that is the one
# that gets the short interval - and it is file stats only, starting no program
# at all, or it would cost more than the scans it saves.
WATCH_EVERY = 1.0              # "has anything changed?" - 0.5ms to answer
REFRESH_EVERY = 20.0           # a full scan even when nothing moved
AFTER_A_JUMP = 2.0             # long enough for the session to notice you

# There is no second cadence for "the window is hidden". Knowing that a session
# needs you is this tool's job whether or not you are looking at it - one day it
# will say so out loud - and a list that watches less while you are elsewhere is
# a list that tells you late. Measured: the check is 0.5ms, a live fleet of
# fifteen changed four times a minute, and acting on every change is ~4% of a
# core.
FOCUS_DEADLINE = 5.0           # iTerm2 is another program; it can hang


class Fleet:
    """One snapshot of the world, and the questions the screen asks of it."""

    def __init__(self, rows=(), source_ok=True, at="", error=""):
        self.rows, self.source_ok, self.at, self.error = list(rows), source_ok, at, error

    def visible(self, query):
        return engine.ui_filter(self.rows, query)

    def groups(self, query):
        return engine.ui_groups(self.visible(query))


class Row(Static):
    """One session: what it is, and what it is about.

    The text is built from the parts the engine names, each drawn by its role.
    No markup is parsed: a session title containing [brackets] is a title, not
    a style, and the only way to be sure of that is never to parse it.
    """

    # The keys are bound HERE, on the thing that has focus. On the App they
    # were reached only after the scrolling container had already taken up and
    # down for itself, so with more sessions than the window could hold the
    # list scrolled and the selection never moved.
    BINDINGS = [
        Binding("down,j", "app.next", "down", show=False),
        Binding("up,k", "app.prev", "up", show=False),
    ]

    def __init__(self, row, width):
        self.row = row
        self.width = width
        # NOT _render: Widget._render is Textual's own, and overriding it with a
        # different signature breaks every paint. Same trap as self.query.
        super().__init__(self._as_text(width), markup=False)
        self.can_focus = True
        self.add_class("row")

    def spans(self):
        """(text, role) for every part on both lines - what the tests read."""
        first, second = engine.ui_row_cells(self.row, width=max(40, self.width - 4))
        return list(first) + [("\n", "pad")] + list(second)

    def words(self, width):
        """Exactly what would be on screen at this width, as one string."""
        was, self.width = self.width, width
        try:
            return "".join(text for text, _ in self.spans())
        finally:
            self.width = was

    def _as_text(self, width):
        self.width = width
        self.painted = self.words(width)
        state = STATE_STYLE.get(engine.ui_state_style(self.row), "")
        text = Text(no_wrap=True, overflow="crop")
        for part, role in self.spans():
            text.append(part, style=state if role == "mark"
                        else ROLE_STYLE.get(role, ""))
        return text

    def refresh_text(self, width):
        self.update(self._as_text(width))


class CcwhoUi(App):
    """The screen. Everything it knows comes from a Fleet snapshot."""

    TITLE = WINDOW_NAME

    CSS = """
    Screen { layout: vertical; }
    #header { height: 1; }
    #banner { height: auto; color: $warning; }
    #body { height: 1fr; }
    #list { width: 1fr; }
    #detail { width: 38%; border-left: solid $panel; padding: 0 1; }
    #detail.full { width: 1fr; border-left: none; }
    #search { height: 3; }
    .row { height: 2; padding: 0 1; border-left: blank; }
    /* The selected row is a block of colour with a bar down its left edge, not
       a shade of the background: on a dark terminal $boost was invisible. */
    .row.selected { background: $primary 60%; border-left: thick $accent; }
    .heading { color: $accent; text-style: bold; padding: 1 1 0 1; }
    """

    BINDINGS = [
        Binding("enter", "go", "go to"),
        Binding("right", "detail", "detail"),
        Binding("left,escape", "back", "back"),
        Binding("slash", "search", "search"),
        Binding("j,down", "next", "down", show=False),
        Binding("k,up", "prev", "up", show=False),
        Binding("r", "restart", "restart", show=False),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, adapter=None, collector=None):
        super().__init__()
        # Injected so the tests never touch iTerm2 or this machine's sessions.
        self.adapter = adapter or Adapter()
        self.collector = collector or Collector()
        self.fleet = Fleet(at="")
        self.filter_text = ""   # not `query`: App.query() is Textual's own
        self.detail_open = False
        self.status = "collecting..."
        self.started = self.shown = 0
        self.painted_width = 0
        self.painted_shape = None
        self.selected = ""      # by session id: widgets come and go, this does not

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Static("", id="header", markup=False)
        yield Static("", id="banner", markup=False)
        with Horizontal(id="body"):
            yield VerticalScroll(id="list")
            yield VerticalScroll(Static("", id="brief", markup=False), id="detail")
        yield Input(placeholder="search", id="search")
        yield Footer()

    def on_mount(self):
        self.name_the_window()
        self.query_one("#search").display = False
        self.query_one("#detail").display = False
        self.set_interval(REFRESH_EVERY, self.tick)
        self.set_interval(WATCH_EVERY, self.watch_tick)
        self.set_interval(0.2, self.check_width)      # no subprocess, just a number
        self.collect()

    def name_the_window(self, write=None):
        """Tell the terminal what this window is, in OSC 0.

        Textual's own title never leaves the process. A window in a list of
        windows needs a name a person recognises.
        """
        write = write or self._to_terminal
        write(f"\x1b]0;{WINDOW_NAME}\x07")

    @staticmethod
    def _to_terminal(text):
        try:
            sys.__stdout__.write(text)
            sys.__stdout__.flush()
        except Exception:
            pass          # a title is never worth taking the screen down for

    # ------------------------------------------------------------- collecting

    @work(exclusive=True, thread=True)
    def collect(self):
        """Off the UI thread: `claude agents` can take 30s, and a list that
        freezes at the moment you reach for it is the problem this tool is about.

        `exclusive` cancels the WORKER, not the thread already running inside it,
        so a slow collection can still finish after a newer one - carrying a view
        of the world that is already wrong. Each carries a number, and show()
        keeps the highest.
        """
        self.started += 1
        mine = self.started
        fleet = self.collector.fleet()
        fleet.seq = mine
        self.call_from_thread(self.show, fleet)

    def tick(self):
        if not self.collector.due():
            return
        self.collect()

    def watch_tick(self):
        """The cheap question, always.

        This used to be gated on the terminal reporting focus. Focus reporting
        is a MESSAGE: when the "you have it back" message never arrived, the
        list sat on the sixty-second cadence while it was being watched -
        reported as a header going 12:07, 12:09:16, 12:10:16.

        There is nothing here worth gating. The check is 0.5ms, and a live
        fleet of fifteen changed four times a minute, so acting on every change
        costs about 4% of a core.
        """
        self.sniff()

    @work(exclusive=True, thread=True, group="sniff")
    def sniff(self):
        """Off the UI thread like every other call out of this process.

        It only stats files - 0.3ms, no program started - and only then does
        the expensive thing, and only if the answer moved.
        """
        if self.collector.changed():
            self.call_from_thread(self.look_again)

    def wake(self):
        """Bring the next look forward. Any deliberate action means the list in
        front of you should be current, not up to a cadence old."""
        if self.collector.due():
            self.collect()

    def show(self, fleet):
        seq = getattr(fleet, "seq", 0)
        if seq and seq < self.shown:
            return                      # a slower collection, finishing late
        self.shown = max(self.shown, seq)
        self.fleet = fleet
        self.status = ""
        self.rebuild()

    # ---------------------------------------------------------------- painting

    def list_width(self):
        return self.size.width - (int(self.size.width * 0.38) if self.detail_open
                                  and self.size.width >= engine.UI_WIDE else 0)

    @staticmethod
    def shape_of(groups, width):
        """Which rows are on screen, in which order, under which headings.

        Everything else - a new recap, a changed age - is text inside a row that
        is already there. Tearing the list down for THAT is what made the window
        flicker three times a second all day.
        """
        return (width, tuple((g["heading"],
                              tuple(r.get("sessionId") for r in g["rows"]))
                             for g in groups))

    def part(self, selector):
        """A widget, or None when the screen is going away.

        On the way out the widgets go before the timers stop, and a paint into
        one that has gone raises out of a callback nobody is waiting on. Asking
        for what this paint actually touches is the check; asking about some
        OTHER widget is how a teardown crash came back on #header after #list
        was already covered.
        """
        try:
            return self.query_one(selector)
        except Exception:
            return None

    def rebuild(self):
        listing = self.part("#list")
        if listing is None or self.part("#header") is None:
            return
        width = self.list_width()
        groups = self.fleet.groups(self.filter_text)
        shape = self.shape_of(groups, width)
        if shape == self.painted_shape and listing.children:
            self.repaint_rows(groups, width)
            self.paint_header(groups)
            return
        self.painted_shape = shape
        keep = self.selected_id()
        # One paint: mounting row by row shows the list half built, which is the
        # flicker you see on a window that refreshes all day.
        with self.batch_update():
            listing.remove_children()
            fresh = []
            for group in groups:
                fresh.append(Static(group["heading"], classes="heading",
                                    markup=False))
                fresh.extend(Row(row, width) for row in group["rows"])
            if not fresh:
                fresh.append(Static(self.empty_text(), markup=False))
            listing.mount_all(fresh)
            self.paint_header(groups)
        # Mounting is asynchronous: focusing a widget in the same breath as
        # mounting it silently does nothing, and the cursor lands nowhere.
        self.call_after_refresh(self.restore_selection, keep)
        if self.detail_open:
            self.call_after_refresh(self.paint_detail)

    def repaint_rows(self, groups, width):
        """Same rows, newer words: write them into the widgets that are there."""
        fresh = {r.get("sessionId"): r for g in groups for r in g["rows"]}
        for widget in self.query(Row):
            row = fresh.get(widget.row.get("sessionId"))
            if row is None:
                continue
            # Compare the WORDS, not the dict: a collection re-reads every
            # field, and most of them - a pid, a timestamp, a byte offset -
            # never reach the screen. Comparing dicts rewrote every row three
            # times a second for nothing.
            widget.row = row
            if widget.words(width) != widget.painted or width != widget.width:
                widget.refresh_text(width)
        self.mark_selected()

    def empty_text(self):
        if self.filter_text:
            return f"  no session matches {self.filter_text!r} - esc to clear"
        if not self.fleet.source_ok:
            return "  cannot read the session list - run `ccwho doctor`"
        return "  No Claude Code sessions running.  o = restore the last save"

    def paint_header(self, groups):
        header, banner = self.part("#header"), self.part("#banner")
        if header is None or banner is None:
            return
        counts = " · ".join(f"{len(g['rows'])} {g['heading'].split()[0].lower()}"
                            for g in groups)
        # Always say when this was true. Without it, a list that has stopped
        # refreshing and a fleet where nothing is happening look identical -
        # and the first thing anyone asks is "is this thing updating?".
        when = f"  {self.fleet.at}" + ("  (stale)" if self.fleet.error else "")
        header.update(
            f"{len(self.fleet.rows)} sessions" + (f": {counts}" if counts else "")
            + when + ("  " + self.status if self.status else ""))
        banner.update(self.fleet.error or "")
        banner.display = bool(self.fleet.error)

    def paint_detail(self):
        row = self.selected_row()
        detail, brief_box = self.part("#detail"), self.part("#brief")
        if not row or detail is None or brief_box is None or self.part("#list") is None:
            return
        b = self.collector.brief(row)
        # color=True and then from_ansi: the brief already knows which words are
        # labels and which are the answer. Asking for plain text and drawing it
        # all the same way threw that away and made the pane a wall of white.
        brief = engine.render_brief(self.collector.brief(row), row, color=True)
        brief_box.update(Text.from_ansi(brief, no_wrap=False))
        detail.display = True
        detail.set_class(self.size.width < engine.UI_WIDE, "full")
        self.part("#list").display = self.size.width >= engine.UI_WIDE

    # --------------------------------------------------------------- selection

    def rows_on_screen(self):
        return list(self.query(Row))

    def selected_row(self):
        """The session the keys act on.

        Focus is not enough on its own: at narrow widths the detail hides the
        list, and a refresh then replaces every Row with one that cannot take
        focus - so Enter would have nothing to act on. The id is remembered here
        and the widgets follow it, not the other way round.
        """
        # A pure read. It used to record whatever had focus, which meant focus
        # WANDERING - and it wanders the moment the list is hidden behind a
        # narrow detail - silently moved the selection to another session.
        #
        # In SCREEN order, and only rows that are on screen. The snapshot is not
        # in screen order - the screen groups by what each session needs - so
        # falling back to the snapshot's first row sent Enter to a session
        # further down the list than the one under the highlight. A session the
        # search has hidden cannot be the target either.
        rows = self.visible_rows()
        for row in rows:
            if row.get("sessionId") == self.selected:
                return row
        return rows[0] if rows else None

    def visible_rows(self):
        """Every row on screen, in the order the screen puts them."""
        return [row for group in self.fleet.groups(self.filter_text)
                for row in group["rows"]]

    def selected_id(self):
        row = self.selected_row()
        return row.get("sessionId") if row else ""

    def restore_selection(self, session_id=None):
        session_id = session_id or self.selected
        """Keyed by session, not by position: the list re-sorts under you, and
        Enter must go where you were looking."""
        rows = self.rows_on_screen()
        if not rows:
            return
        box = self.part("#search")
        if box is not None and box.display and self.focused is box:
            return        # you are typing: every keystroke rebuilds the list, and
                          # taking focus back would eat the rest of the word
        for widget in rows:
            if widget.row.get("sessionId") == session_id:
                widget.focus()
                self.selected = session_id
                self.mark_selected()
                return
        rows[0].focus()
        self.selected = rows[0].row.get("sessionId", "")
        self.mark_selected()

    def mark_selected(self):
        """One row carries the class the stylesheet paints. Focus alone is not
        enough: the list can be hidden behind a narrow detail, and focus then
        belongs to something else entirely."""
        for widget in self.rows_on_screen():
            widget.set_class(widget.row.get("sessionId") == self.selected,
                             "selected")

    # ----------------------------------------------------------------- actions

    def check_width(self):
        """The window changes shape - a drag, a split, the hotkey window - and
        both the layout and the text have to follow: row text is cut for the
        width it was mounted at.

        Polled, not handled: Textual delivers Resize to the screen, and an App
        `on_resize` is never called (measured - the handler fired zero times
        across two resizes). A width comparison costs nothing and cannot be
        missed.
        """
        if self.size.width == self.painted_width:
            return
        self.painted_width = self.size.width
        self.rebuild()
        if self.detail_open:
            self.paint_detail()

    @on(events.Click)
    def clicked(self, event):
        """One click picks the session AND goes to it. Reaching for the mouse to
        highlight a row and then for the keyboard to act on it is two gestures
        for one decision."""
        widget = getattr(event, "widget", None)
        while widget is not None and not isinstance(widget, Row):
            widget = widget.parent
        if widget is None:
            return
        self.selected = widget.row.get("sessionId", "")
        self.mark_selected()
        widget.focus()
        self.action_go()

    @on(events.DescendantFocus)
    def followed_focus(self, event):
        """Clicking or tabbing to a row selects it - but only while the list is
        the thing on screen. Focus moved by hiding a container is not a choice."""
        widget = getattr(event, "widget", None)
        if isinstance(widget, Row) and self.query_one("#list").display:
            self.selected = widget.row.get("sessionId", "")
            self.mark_selected()
            if self.detail_open:
                self.paint_detail()      # the brief always shows what Enter acts on

    def action_next(self):
        self.move(1)

    def action_prev(self):
        self.move(-1)

    def move(self, step):
        rows = self.rows_on_screen()
        if not rows:
            return
        here = next((i for i, w in enumerate(rows)
                     if w.row.get("sessionId") == self.selected), -1)
        widget = rows[max(0, min(len(rows) - 1, here + step))]
        widget.focus()
        self.selected = widget.row.get("sessionId", "")
        self.mark_selected()
        # or the selection walks off the bottom of a list too long to show
        # No scroll_visible: focus() scrolls the widget into view by itself,
        # and a line that cannot fail is a line nobody can test.
        if self.detail_open:
            self.paint_detail()

    def action_detail(self):
        self.detail_open = True
        self.paint_detail()

    def action_back(self):
        # The detail goes first. Coming back from a brief you opened FROM a
        # search must land you in that search, not in the whole fleet again.
        if self.detail_open:
            self.detail_open = False
            self.query_one("#detail").display = False
            self.query_one("#list").display = True
            self.call_after_refresh(self.restore_selection)
            return
        box = self.query_one("#search")
        if box.display:
            # an open search box keeps focus and eats j, k and q as text, so
            # escape closes it whether or not you typed anything
            box.display = False
            if self.filter_text:
                self.filter_text = ""
                box.value = ""
                self.rebuild()
            self.call_after_refresh(self.restore_selection)
            return
        # `self.query` is Textual's own method and is always truthy: naming an
        # attribute over it made this branch always taken, so the detail never
        # closed. The search text is `filter_text` everywhere for that reason.
        if self.filter_text:
            self.filter_text = ""
            self.query_one("#search").value = ""
            self.query_one("#search").display = False
            self.rebuild()

    def action_search(self):
        box = self.query_one("#search")
        box.display = True
        # after the refresh, for the same reason the selection is: a widget that
        # was hidden a moment ago cannot take focus yet, and the keys you type
        # next go to the app's own bindings instead of into the box
        self.call_after_refresh(box.focus)

    def action_go(self):
        row = self.selected_row()
        if not row:
            return
        # Name it the way you picked it. "going to daf9..." is not something
        # you can check against the window that comes forward; the title is.
        name = (row.get("tab_title") or row.get("title") or row.get("name") or "")
        self.status = (f"going to {engine.brief.short_id(row.get('sessionId', ''))}"
                       f" {engine.truncate(name, 40)}...")
        self.paint_header(self.fleet.groups(self.filter_text))
        self.go_to(row)
        # The session you just opened is about to stop needing you. Look again
        # shortly, rather than scanning everything more often for the sake of
        # the one row that is about to change.
        self.set_timer(AFTER_A_JUMP, self.look_again)

    def look_again(self):
        """A fresh look that the usual wait cannot hold up.

        It counts as the last look, rather than resetting the clock to zero: a
        zero clock made the very next tick collect all over again, so every jump
        cost two full scans a second apart.
        """
        self.collector.mark()
        self.collect()

    @work(thread=True)
    def go_to(self, row):
        """Also off the UI thread: osascript is another program, and a hung
        iTerm2 must not take the list with it."""
        result = self.adapter.focus(row, deadline=FOCUS_DEADLINE)
        self.call_from_thread(self.said, result, row)

    def said(self, text, row=None):
        """What happened, in the words you chose the session by.

        "focused s022" cannot be checked against the window that came forward.
        Anything the jump did NOT expect - not found, or landed somewhere else -
        passes through as it is, because that is the part worth reading.
        """
        if row is not None and text.startswith("focused"):
            name = (row.get("tab_title") or row.get("title")
                    or row.get("name") or "")
            text = (f"went to {engine.brief.short_id(row.get('sessionId', ''))}"
                    f"  {engine.truncate(name, 40)}")
        self.status = text
        self.paint_header(self.fleet.groups(self.filter_text))

    def action_restart(self):
        # result, not message: run() returns the result, and `message` is only
        # printed. With message= the runner saw None and never re-executed.
        self.exit(result="restart")

    @on(Input.Changed, "#search")
    def searched(self, event):
        self.filter_text = event.value
        self.rebuild()

    @on(Input.Submitted, "#search")
    def search_done(self):
        rows = self.rows_on_screen()
        if rows:
            rows[0].focus()


class Collector:
    """Everything that reaches the world, in one place the tests can replace."""

    def __init__(self):
        self.cache = {}
        self.last = 0.0
        self.reload_error = ""
        self.digest = None      # what the files said at the last look
        self.rows = []          # the ids the check watches, from the last scan

    def changed(self):
        """Has anything happened that could change the list?

        False when we could not ask: "the source is unreachable" is not "a
        session started needing you", and firing a full scan on every failed
        call is how a broken `claude` turns into a busy loop.
        """
        ids = [row.get("sessionId", "") for row in self.rows]
        now = engine.watch_digest(ids, cache=self.cache)
        if now == self.digest:
            return False
        self.digest = now
        return True

    def mark(self, now=None):
        """This moment counts as the last look."""
        import time
        self.last = time.time() if now is None else now

    def due(self, visible=None, now=None):
        import time
        now = time.time() if now is None else now
        wait = REFRESH_EVERY
        if now - self.last < wait:
            return False
        self.last = now
        return True

    def reload(self):
        """Re-read the engine and the modules it imports, the same way the watch
        loop does. This window stays open for days; a fix on disk that it cannot
        see is a fix that did not happen.

        A failed reload keeps the working modules: the candidate is built beside
        them and only swapped when it imports cleanly.
        """
        import importlib
        import importlib.util
        import sys
        try:
            for module in (engine.brief, engine.ccwho_index):
                spec = importlib.util.spec_from_file_location(module.__name__,
                                                              module.__file__)
                candidate = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(candidate)
                sys.modules[module.__name__] = candidate
            importlib.reload(engine)
            self.reload_error = ""
        except Exception as ex:
            self.reload_error = f"engine reload failed: {ex}"

    def fleet(self):
        import time
        self.reload()
        status = {}
        try:
            rows, _ = engine.collect(cache=self.cache, status=status)
        except Exception as ex:                      # never kill the screen
            return Fleet([], False, "", f"could not read the fleet: {ex}")
        # The scan's own answer to the cheap question, so the next cheap check
        # does not see a changed world and scan all over again.
        if status.get("watch") is not None:
            self.digest = status["watch"]
        trouble = self.reload_error or ("" if status.get("source_ok") else
                                        "cannot read the session list - run `ccwho doctor`")
        self.rows = rows
        return Fleet(rows, status.get("source_ok", False),
                     time.strftime("%H:%M:%S"), trouble)

    def brief(self, row):
        head, tail, _ = engine.read_windows(row.get("sessionId", ""), cache=self.cache)
        return engine.brief.build(head, tail, session=row, now=engine.now_iso(),
                                  as_records=engine.as_records)


class Adapter:
    """iTerm2, behind a door the tests can close."""

    def focus(self, row, deadline=FOCUS_DEADLINE):
        # The DEVICE path, not the short form the list shows: AppleScript matches
        # on /dev/ttys022, and /dev/s022 is not a terminal that exists.
        tty = (row.get("tty") or "").strip()
        if not tty:
            return f"pid {row.get('pid')} has no window - `ccwho show` for its resume line"
        device = tty if tty.startswith("/dev/") else f"/dev/{tty}"
        script = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                              "jump.applescript")
        try:
            done = subprocess.run(["osascript", script, device],
                                  capture_output=True, text=True, timeout=deadline)
        except subprocess.TimeoutExpired:
            return f"iTerm2 did not answer in {deadline:g}s"
        except OSError as ex:
            return f"could not reach iTerm2: {ex}"
        return (done.stdout or done.stderr).strip() or "done"


def main():
    app = CcwhoUi()
    result = app.run()
    return 42 if result == "restart" else 0       # the runner re-execs on 42


if __name__ == "__main__":
    sys.exit(main())
