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

import ccwho_engine as engine                                      # noqa: E402

REFRESH_VISIBLE = 3.0
REFRESH_HIDDEN = 60.0          # the hotkey window is hidden most of the day
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
    """One session: what it is, and what it is about."""

    def __init__(self, row, width):
        self.row = row
        super().__init__(self._text(width), markup=False)
        self.can_focus = True
        self.add_class("row", row.get("attention", "idle"))

    def _text(self, width):
        first, second = engine.ui_row_lines(self.row, width=max(40, width - 4))
        return f"{first}\n{second}"

    def refresh_text(self, width):
        self.update(self._text(width))


class CcwhoUi(App):
    """The screen. Everything it knows comes from a Fleet snapshot."""

    CSS = """
    Screen { layout: vertical; }
    #header { height: 1; }
    #banner { height: auto; color: $warning; }
    #body { height: 1fr; }
    #list { width: 1fr; }
    #detail { width: 38%; border-left: solid $panel; padding: 0 1; }
    #detail.full { width: 1fr; border-left: none; }
    #search { height: 3; }
    .row { height: 2; padding: 0 1; }
    .row:focus { background: $boost; }
    .blocked, .waiting { color: $warning; text-style: bold; }
    .asks { color: $secondary; text-style: bold; }
    .stopped { color: $warning; }
    .busy, .running { color: $accent; }
    .heading { color: $text-muted; text-style: bold; padding: 1 1 0 1; }
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
        self.query_one("#search").display = False
        self.query_one("#detail").display = False
        self.set_interval(REFRESH_VISIBLE, self.tick)
        self.set_interval(0.2, self.check_width)      # no subprocess, just a number
        self.collect()

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
        if not self.collector.due(self.has_focus_hint()):
            return
        self.collect()

    def has_focus_hint(self):
        """Visible? The hotkey window is hidden most of the day, and refreshing
        three processes a tick for a window nobody is looking at is how --watch
        once burned a core."""
        return self.app.screen is not None and self.is_running and self.focused is not None

    def show(self, fleet):
        seq = getattr(fleet, "seq", 0)
        if seq and seq < self.shown:
            return                      # a slower collection, finishing late
        self.shown = max(self.shown, seq)
        self.fleet = fleet
        self.status = ""
        self.rebuild()

    # ---------------------------------------------------------------- painting

    def rebuild(self):
        listing = self.query_one("#list")
        keep = self.selected_id()
        listing.remove_children()
        width = self.size.width - (int(self.size.width * 0.38) if self.detail_open
                                   and self.size.width >= engine.UI_WIDE else 0)
        groups = self.fleet.groups(self.filter_text)
        for group in groups:
            listing.mount(Static(group["heading"], classes="heading", markup=False))
            for row in group["rows"]:
                listing.mount(Row(row, width))
        self.paint_header(groups)
        if not groups:
            listing.mount(Static(self.empty_text(), markup=False))
        # Mounting is asynchronous: focusing a widget in the same breath as
        # mounting it silently does nothing, and the cursor lands nowhere.
        self.call_after_refresh(self.restore_selection, keep)
        if self.detail_open:
            self.call_after_refresh(self.paint_detail)

    def empty_text(self):
        if self.filter_text:
            return f"  no session matches {self.filter_text!r} - esc to clear"
        if not self.fleet.source_ok:
            return "  cannot read the session list - run `ccwho doctor`"
        return "  No Claude Code sessions running.  o = restore the last save"

    def paint_header(self, groups):
        counts = " · ".join(f"{len(g['rows'])} {g['heading'].split()[0].lower()}"
                            for g in groups)
        stale = f"  (stale {self.fleet.at})" if self.fleet.error else ""
        self.query_one("#header").update(
            f"{len(self.fleet.rows)} sessions" + (f": {counts}" if counts else "")
            + stale + ("  " + self.status if self.status else ""))
        self.query_one("#banner").update(self.fleet.error or "")
        self.query_one("#banner").display = bool(self.fleet.error)

    def paint_detail(self):
        row = self.selected_row()
        if not row:
            return
        b = self.collector.brief(row)
        self.query_one("#brief").update(engine.render_brief(b, row, color=False))
        detail = self.query_one("#detail")
        detail.display = True
        detail.set_class(self.size.width < engine.UI_WIDE, "full")
        self.query_one("#list").display = self.size.width >= engine.UI_WIDE

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
        for row in self.fleet.rows:
            if row.get("sessionId") == self.selected:
                return row
        return self.fleet.rows[0] if self.fleet.rows else None

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
        box = self.query_one("#search")
        if box.display and self.focused is box:
            return        # you are typing: every keystroke rebuilds the list, and
                          # taking focus back would eat the rest of the word
        for widget in rows:
            if widget.row.get("sessionId") == session_id:
                widget.focus()
                self.selected = session_id
                return
        rows[0].focus()
        self.selected = rows[0].row.get("sessionId", "")

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

    @on(events.DescendantFocus)
    def followed_focus(self, event):
        """Clicking or tabbing to a row selects it - but only while the list is
        the thing on screen. Focus moved by hiding a container is not a choice."""
        widget = getattr(event, "widget", None)
        if isinstance(widget, Row) and self.query_one("#list").display:
            self.selected = widget.row.get("sessionId", "")
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
        here = next((i for i, w in enumerate(rows) if w is self.focused), -1)
        rows[max(0, min(len(rows) - 1, here + step))].focus()

    def action_detail(self):
        self.detail_open = True
        self.paint_detail()

    def action_back(self):
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
            return
        self.detail_open = False
        self.query_one("#detail").display = False
        self.query_one("#list").display = True

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
        self.status = f"going to {engine.brief.short_id(row.get('sessionId', ''))}..."
        self.paint_header(self.fleet.groups(self.filter_text))
        self.go_to(row)

    @work(thread=True)
    def go_to(self, row):
        """Also off the UI thread: osascript is another program, and a hung
        iTerm2 must not take the list with it."""
        result = self.adapter.focus(row, deadline=FOCUS_DEADLINE)
        self.call_from_thread(self.said, result)

    def said(self, text):
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

    def due(self, visible, now=None):
        import time
        now = time.time() if now is None else now
        wait = REFRESH_VISIBLE if visible else REFRESH_HIDDEN
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
        trouble = self.reload_error or ("" if status.get("source_ok") else
                                        "cannot read the session list - run `ccwho doctor`")
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
