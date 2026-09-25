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
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from textual import events, on, work                                       # noqa: E402
from textual.app import App, ComposeResult                         # noqa: E402
from textual.binding import Binding                                # noqa: E402
from textual.containers import Horizontal, VerticalScroll          # noqa: E402
from textual.widgets import Footer, Input, Static                  # noqa: E402
from rich.text import Text                                         # noqa: E402

import ccwho_engine as engine                                      # noqa: E402
import ccwho_panel as panel                                        # noqa: E402

# What each part of a row is drawn as. The engine says what a part IS; only this
# table says what that looks like, and it is deliberately short: the first screen
# painted whole rows by state, three states shared one colour, and the result was
# a wall of orange that was harder to read than plain text and told you nothing.
ROLE_STYLE = {"mark": "",              # the state's own colour, from STATE_STYLE
              "id": "dim",
              "project": "bold",
              "name": "",              # plain: this is the thing you are reading
              "meta": "dim",
              "action": "bold underline",   # the one part of a row you click on its own
              "detail": "bold",        # the › that opens the detail, not the jump
              "age": "bold",           # inside a dim line, the age stands out
              "recap": "dim",
              "account": "dim",        # which account the row spends (usage)
              "pad": ""}

# The usage line's styles (ccwho_usage.STYLES), as ANSI colour names so the
# iTerm2 theme applies (D7). The arrows are bold and never dim (owner, the
# mockup's faint green): a dim line must not fade them.
USAGE_STYLE = {"dim": "dim", "plain": "", "green": "bold green", "red": "bold red",
               "yellow": "yellow", "byellow": "bold yellow"}

# Three states, three colours, used on one glyph per row and on its heading.
STATE_STYLE = {"needs": "bold #e5a50a",    # amber: it is waiting on you
               "review": "#e5a50a",        # the same amber, unbold: it finished
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
PULSE_EVERY = 0.45             # the blink on a row that is being opened

# There is no second cadence for "the window is hidden". Knowing that a session
# needs you is this tool's job whether or not you are looking at it - one day it
# will say so out loud - and a list that watches less while you are elsewhere is
# a list that tells you late. Measured: the check is 0.5ms, a live fleet of
# fifteen changed four times a minute, and acting on every change is ~4% of a
# core.
FOCUS_DEADLINE = 5.0           # iTerm2 is another program; it can hang
ARM_SECS = 10.0                # a kill asked for once is confirmed within this


class Fleet:
    """One snapshot of the world, and the questions the screen asks of it."""

    def __init__(self, rows=(), source_ok=True, at="", error="", procs=None,
                 secure="", usage=None):
        self.source_ok, self.at, self.error = source_ok, at, error
        # subscription usage, as ccwho_usage.snapshot built it; None = not read
        self.usage = usage
        self.rows = [self.tagged(r) for r in rows]
        # an app holding macOS Secure Input: no hotkey works until it lets go.
        # The list can still come forward (click iTerm2, run ccwho), so it is
        # where a dead hotkey gets explained
        self.secure = secure
        # collect()'s second answer: what agents started, the ports they hold,
        # what was left behind. None is "not collected": the list stays quiet
        # (a false alarm on every start is still an alarm) and `p` says unknown
        self.procs = procs if isinstance(procs, dict) else {"collected": False}

    def tagged(self, row):
        """The row, with the account it spends when 2+ accounts are seen. A copy:
        the collected row is not ours to change. Usage never costs a row."""
        try:
            tag = engine.ccwho_usage.row_tag(self.usage, row.get("sessionId", ""))
        except Exception:
            tag = ""
        return dict(row, usage_tag=tag) if tag else row

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
        self.lit = None         # the action under the mouse: "detail", "kill" or None
        self.mouse_at = None    # (x, y) in the text, while the mouse is on the row
        # NOT _render: Widget._render is Textual's own, and overriding it with a
        # different signature breaks every paint. Same trap as self.query.
        super().__init__(self._as_text(width), markup=False)
        self.can_focus = True
        self.add_class("row")

    def text_width(self):
        """The cells the text is drawn in. Once laid out that is the row's own
        content width - which the list's scrollbar makes narrower: laid out any
        wider, a full line lost its end, a [kill loop] button included."""
        try:
            drawn = self.content_region.width
        except Exception:       # the text is first built before the widget exists
            drawn = 0
        return drawn if drawn > 0 else max(40, self.width - 4)

    def spans_lines(self):
        """The two lines, each as (text, role) parts, at the width laid out."""
        return engine.ui_row_cells(self.row, width=self.text_width(),
                                   tag=self.row.get("usage_tag", ""))

    def on_resize(self, event):
        # the scrollbar came or went: lay the text out at the width it now has
        if self.words(self.width) != self.painted:
            self.refresh_text(self.width)

    def spans(self):
        """(text, role) for every part on both lines - what the tests read."""
        first, second = self.spans_lines()
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
        # read again for this text: a row rewritten under a still mouse has moved
        # its parts, and the light follows what is under the mouse now
        self.lit = self.action_at(*self.mouse_at) if self.mouse_at else None
        lit = {"detail": "detail", "kill": "action"}.get(self.lit)
        for part, role in self.spans():
            style = state if role == "mark" else ROLE_STYLE.get(role, "")
            # under the mouse, the part a click on it acts on lights up
            text.append(part, style=f"{style} reverse".strip() if role == lit else style)
        return text

    def action_at(self, x, y):
        """What a click at (x, y) of the text does on its own, or None."""
        return engine.ui_action_at(self.row, self.text_width(), y, x,
                                   tag=self.row.get("usage_tag", ""))

    def on_mouse_move(self, event):
        at = event.get_content_offset(self)
        self.mouse_at = (at.x, at.y) if at is not None else None
        if (self.action_at(*self.mouse_at) if self.mouse_at else None) != self.lit:
            self.refresh_text(self.width)

    def on_leave(self, event):
        self.mouse_at = None
        if self.lit:
            self.refresh_text(self.width)

    def refresh_text(self, width):
        self.update(self._as_text(width))


class CcwhoUi(App):
    """The screen. Everything it knows comes from a Fleet snapshot."""

    # Textual focuses the first focusable widget on mount, which is the search
    # Input - a box that is not on screen. The list decides its own focus.
    AUTO_FOCUS = None

    TITLE = WINDOW_NAME

    CSS = """
    Screen { layout: vertical; }
    #header { height: 1; }
    #usage, #ports, #procline { height: auto; padding: 0 1; }
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
    /* Being opened: a slow blink between two shades, so a row that is waiting
       on iTerm2 is obvious from across the room. */
    .row.acting { background: $warning 30%; border-left: thick $warning; }
    .row.acting.pulse { background: $warning 70%; }
    .heading { color: $accent; text-style: bold; padding: 1 1 0 1; }
    #closebar { dock: top; height: 1; align: right top; }
    #close { width: 3; height: 1; color: $text-muted; }
    #close:hover { background: $accent; color: $text; }
    """

    BINDINGS = [
        Binding("enter", "go", "go to"),
        Binding("right", "detail", "detail"),
        Binding("left,escape", "back", "back"),
        Binding("slash", "search", "search"),
        Binding("p", "procs", "processes"),
        Binding("j,down", "next", "down", show=False),
        Binding("k,up", "prev", "up", show=False),
        Binding("o", "reopen", "reopen", show=False),
        Binding("x", "kill_loop", "kill loop", show=False),
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
        self.detail_mode = "brief"      # or "procs": what the detail pane shows
        self.status = "collecting..."
        self.started = self.shown = 0
        self.painted_width = 0
        self.painted_shape = None
        self.selected = ""      # by session id: widgets come and go, this does not
        self.acting = ""        # the session a window is being opened for
        # (session, its loop pids, when): what the next x or click kills. Only
        # while the question is on screen: a kill is not undone
        self.armed = None
        self.clock = time.monotonic
        self.pulsing = False
        # While a window is being opened nothing on the list moves - you are
        # watching one row blink - so a scan waits here, and so does the row you
        # looked at leaving NEEDS YOU. Both land when the jump does.
        self.held = None
        self.reviewing = {}     # session id -> row, looked at by a jump that landed
        self.jumps = 0          # which jump is the latest: only its answer ends it
        self.seen = {}          # session id -> the turn (ts) you looked at

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield Static("", id="header", markup=False)
        # subscription usage, one line per account a live session spends: dim,
        # the selected row's account bright
        yield Static("", id="usage", markup=False)
        # what agents hold: one dim line, never louder than what needs you
        yield Static("", id="ports", markup=False)
        yield Static("", id="banner", markup=False)
        with Horizontal(id="body"):
            yield VerticalScroll(id="list")
            # the close sits on its own line, docked: it stays at the top while
            # a long detail scrolls under it
            yield VerticalScroll(Horizontal(Static(" ✕ ", id="close", markup=False),
                                            id="closebar"),
                                 Static("", id="brief", markup=False), id="detail")
        # what was left behind, and Codex's: one collapsed line each
        yield Static("", id="procline", markup=False)
        yield Input(placeholder="search", id="search")
        yield Footer()

    def on_mount(self):
        self.name_the_window()
        self.adapter.keep_in_front()
        self.hide_search()
        self.query_one("#detail").display = False
        self.set_interval(REFRESH_EVERY, self.tick)
        self.set_interval(WATCH_EVERY, self.watch_tick)
        self.set_interval(PULSE_EVERY, self.pulse)
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
        # numbered on the UI thread: two workers adding at once can share a
        # number, and then the older picture can win
        mine = self.call_from_thread(self.next_seq)
        fleet = self.collector.fleet()
        fleet.seq = mine
        self.call_from_thread(self.show, fleet)

    def next_seq(self):
        self.started += 1
        return self.started

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
        if self.acting:
            self.held = fleet           # shown when the jump is done: landed()
            return
        self.fleet = self.keep_seen(fleet)
        self.status = self.still_armed() or ""
        self.rebuild()

    def keep_seen(self, fleet):
        """A scan that read the marks before you looked, arriving after: the
        turn you saw stays seen. Every fleet that reaches the screen comes
        through here - the held one included."""
        for row in fleet.rows:
            if row.get("attention") == "review" and row.get("ts") \
                    and self.seen.get(row.get("sessionId")) == row.get("ts"):
                row["attention"] = "stopped"
        return fleet

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

    def hide_search(self):
        """Out of sight AND out of the focus chain.

        display=False is not enough: with no rows to focus, focus fell into the
        box anyway and every key went into a filter nobody could see.
        """
        box = self.part("#search")
        if box is None:
            return
        box.display = False
        box.can_focus = False
        if self.focused is box:
            # it kept focus after being hidden, and every key went into it
            self.set_focus(None)

    def show_search(self):
        box = self.part("#search")
        if box is None:
            return
        box.display = True
        box.can_focus = True
        return box

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
            self.repaint_detail()
            return
        if self.reordered(listing, groups, width):
            self.painted_shape = shape
            self.paint_header(groups)
            self.repaint_detail()
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

    def reordered(self, listing, groups, width):
        """Move the rows that are already there, when that is all it takes.

        Rows sort newest-first inside their group, so every session that writes
        jumps up, and a session that finishes changes group - measured at nine
        times a minute on a live fleet. Tearing the list down for that is the
        flashing that came back. The ROWS have not changed; where they sit has.

        False when a session has arrived or left, which is a real build.
        """
        on_screen = {w.row.get("sessionId"): w for w in self.query(Row)}
        want_ids = {r.get("sessionId") for g in groups for r in g["rows"]}
        if not on_screen or want_ids != set(on_screen):
            return False
        spare = {str(getattr(w, "content", "")): w
                 for w in listing.children if not isinstance(w, Row)}
        with self.batch_update():
            wanted = []
            for group in groups:
                head = spare.pop(group["heading"], None)
                if head is None:
                    # a group that was empty a moment ago: the heading is a
                    # one-line Static, the rows are what must not be remade
                    head = Static(group["heading"], classes="heading",
                                  markup=False)
                    listing.mount(head)
                wanted.append(head)
                wanted += [on_screen[r.get("sessionId")] for r in group["rows"]]
            for leftover in spare.values():
                leftover.remove()
            for index, want in enumerate(wanted):
                if index >= len(listing.children) or \
                        list(listing.children)[index] is not want:
                    listing.move_child(want, before=index)
            self.repaint_rows(groups, width)
        return True

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
        self.mark_acting()

    def empty_text(self):
        if self.filter_text:
            return f"  no session matches {self.filter_text!r} - esc to clear"
        if not self.fleet.source_ok:
            return "  cannot read the session list - run `ccwho doctor`"
        saved = self.collector.saved_count()
        if saved:
            return (f"  No Claude Code sessions running."
                    f"   o = reopen the {saved} from the last save")
        return "  No Claude Code sessions running, and nothing saved to reopen."

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
        # A list hiding most of itself has to SAY so, where you are already
        # looking. The box at the bottom is easy to forget, and a filtered list
        # you have forgotten about looks like a broken one.
        shown = sum(len(g["rows"]) for g in groups)
        searching = (f"   search {self.filter_text!r}: {shown} of "
                     f"{len(self.fleet.rows)} · esc to clear"
                     if self.filter_text else "")
        header.update(
            f"{len(self.fleet.rows)} sessions" + (f": {counts}" if counts else "")
            + when + searching + ("  " + self.status if self.status else ""))
        trouble = "\n".join(line for line in (self.fleet.error, self.fleet.secure)
                            if line)
        banner.update(trouble)
        banner.display = bool(trouble)
        self.paint_procs_lines()
        self.paint_usage()

    def paint_usage(self):
        """From the snapshot in hand: moving the cursor reads no file."""
        box = self.part("#usage")
        if box is None:
            return
        snap = self.fleet.usage
        if snap is None:
            # nothing to show: leave a hidden box alone - every mark_selected
            # comes here, and a display write asks for a layout pass
            if box.display:
                box.display = False
            return
        selected = None
        try:
            row = self.selected_row()
            selected = (snap.get("sessions", {}).get(row.get("sessionId"))
                        if row and isinstance(snap, dict) else None)
            lines = engine.ccwho_usage.usage_lines(snap, max(10, self.size.width - 2),
                                                   selected=selected)
        except Exception:               # a usage problem never costs the list
            lines = [[("usage  unknown", "dim")]]
        # the cursor moves far more often than anything here changes: the same
        # snapshot, account and width is the same line, so the box is left alone
        painted = (snap, selected, self.size.width)
        last = getattr(self, "usage_painted", None)
        if last and last[0] is painted[0] and last[1:] == painted[1:]:
            return
        self.usage_painted = painted
        text = Text(no_wrap=True, overflow="crop")
        for i, line in enumerate(lines):
            if i:
                text.append("\n")
            for part, style in line:
                text.append(part, style=USAGE_STYLE.get(style, ""))
        box.update(text)
        if box.display != bool(lines):
            box.display = bool(lines)

    def paint_procs_lines(self):
        ports, bottom = self.part("#ports"), self.part("#procline")
        if ports is None or bottom is None:
            return
        width = max(10, self.size.width - 2)
        try:
            held = engine.ports_line(self.fleet.procs, width)
            lines = "\n".join(engine.bottom_lines(self.fleet.procs, hint="p", width=width))
        except Exception:               # an odd shape is "unknown", never a crash
            held, lines = "processes unknown", ""
        ports.update(Text(held, style="dim", no_wrap=True, overflow="crop"))
        ports.display = bool(held)
        bottom.update(Text(lines, style="dim", no_wrap=True, overflow="crop"))
        bottom.display = bool(lines)

    def repaint_detail(self):
        """A refresh that kept the rows still changes what they started: an open
        pane that is not repainted contradicts the lines around it."""
        if self.detail_open:
            self.paint_detail()

    def paint_detail(self):
        row = self.selected_row()
        detail, brief_box = self.part("#detail"), self.part("#brief")
        if detail is None or brief_box is None or self.part("#list") is None:
            return
        if self.detail_mode == "procs":
            procs = self.fleet.procs
            # the pane's own width - wide, it is 38% of the screen - less its
            # border, padding and the scrollbar a long list brings
            wide = self.size.width >= engine.UI_WIDE
            width = (int(self.size.width * 0.38) - 5 if wide else self.size.width - 4)
            try:
                text = engine.render_ps_screen(engine.ps_listing(self.fleet.rows, procs),
                                               procs, width=max(20, width))
            except Exception:           # an odd shape is "unknown", never a crash
                text = "processes unknown"
            brief_box.update(Text(text, no_wrap=True, overflow="crop"))
        else:
            if not row:
                return
            # color=True and then from_ansi: the brief already knows which words
            # are labels and which are the answer. Asking for plain text and
            # drawing it all the same way made the pane a wall of white.
            brief = engine.render_brief(self.collector.brief(row), row, color=True)
            text = Text.from_ansi(brief, no_wrap=False)
            try:
                mine = engine.session_procs_lines(self.fleet.procs, row.get("sessionId"))
            except Exception:
                mine = []
            if mine:
                text.append("\n\nprocesses\n", style="bold")
                text.append("\n".join(mine), style="dim")
            brief_box.update(text)
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
        self.mark_acting()

    def pulse(self):
        """Blink whatever is being opened. Nothing to blink costs nothing."""
        if not self.acting:
            return
        self.pulsing = not self.pulsing
        for widget in self.rows_on_screen():
            if widget.row.get("sessionId") == self.acting:
                widget.set_class(self.pulsing, "pulse")

    def mark_acting(self, session_id=None):
        """One row carries the "being opened" mark. Like the selection, it is
        held by session id, so a scan that rebuilds the list cannot lose it."""
        if session_id is not None:
            self.acting = session_id
            self.pulsing = False
        for widget in self.rows_on_screen():
            here = widget.row.get("sessionId") == self.acting
            widget.set_class(bool(self.acting) and here, "acting")
            if not here:
                widget.set_class(False, "pulse")

    def mark_selected(self):
        """One row carries the class the stylesheet paints. Focus alone is not
        enough: the list can be hidden behind a narrow detail, and focus then
        belongs to something else entirely."""
        for widget in self.rows_on_screen():
            widget.set_class(widget.row.get("sessionId") == self.selected,
                             "selected")
        self.paint_usage()          # the selected row's account is the bright one

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

    @on(events.Click, "#close")
    def close_clicked(self, event):
        event.stop()
        if self.detail_open:
            self.action_back()

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
        if self.armed and self.armed[0] != self.selected:
            self.armed = None
        # a right click - or ctrl-click, the Mac's own - asks about the row
        if getattr(event, "button", 1) == 3 or getattr(event, "ctrl", False):
            self.open_detail("brief")
            return
        at = event.get_content_offset(widget)
        action = widget.action_at(at.x, at.y) if at is not None else None
        if action == "kill":
            self.action_kill_loop()
            return
        if action == "detail":
            self.open_detail("brief")
            return
        self._go()          # a click names its row, whatever the pane shows

    def action_kill_loop(self):
        """Kill the selected session's wait loops that cannot end - on the second
        ask. The first only says what the second will do: a kill is not undone."""
        if self.detail_open and self.detail_mode == "procs":
            return      # that screen is not about the selected row
        row = self.selected_row()
        if not engine.loop_kill_offered(row or {}):
            return
        if self.acting:
            # a kill moves rows, and nothing moves while a window opens
            self.status = "a window is being opened - kill it after that"
            self.paint_header(self.fleet.groups(self.filter_text))
            return
        if self.still_armed() and self.armed[0] == row.get("sessionId", ""):
            self.armed = None
            self.status = "killing..."
            self.paint_header(self.fleet.groups(self.filter_text))
            self.killing(row)
            return
        self.armed = (row.get("sessionId", ""), self._pids(row), self.clock())
        self.status = self.still_armed()
        self.paint_header(self.fleet.groups(self.filter_text))
        # the question goes when the arm does: one left on screen would be false
        self.set_timer(ARM_SECS + 0.05, self.arm_expired)

    def arm_expired(self):
        if self.status.startswith("x or click again") and not self.still_armed():
            self.status = ""
            self.paint_header(self.fleet.groups(self.filter_text))

    @staticmethod
    def _pids(row):
        return tuple(d["pid"] for d in (row or {}).get("dead_loops") or [])

    def still_armed(self):
        """The question, while the kill it asks about still stands; else ""."""
        if not self.armed:
            return ""
        sid, pids, since = self.armed
        row = next((r for r in self.fleet.rows if r.get("sessionId") == sid), None)
        if (self.clock() - since > ARM_SECS or row is None or self._pids(row) != pids
                or not engine.loop_kill_offered(row)):
            self.armed = None
            return ""
        return f"x or click again to kill loop {', '.join(str(p) for p in pids)}"

    @work(thread=True)
    def killing(self, row):
        said = self.collector.kill_loops(row)
        # the list after the kill, and THEN what the kill did: the other order
        # has the refresh wipe the answer before it can be read
        mine = self.call_from_thread(self.next_seq)
        fleet = self.collector.fleet()
        fleet.seq = mine
        self.call_from_thread(self.show, fleet)
        self.call_from_thread(self.said, said)

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
        self.armed = None       # a kill is armed for the row you were on, only
        if self.detail_open and self.detail_mode == "procs":
            # the process screen is not about the selected session: these keys
            # scroll it, and never move a selection you cannot see
            detail = self.part("#detail")
            if detail is not None:
                detail.scroll_relative(y=step, animate=False)
            return
        rows = self.rows_on_screen()
        if not rows:
            return
        here = next((i for i, w in enumerate(rows)
                     if w.row.get("sessionId") == self.selected), -1)
        widget = rows[max(0, min(len(rows) - 1, here + step))]
        widget.focus()
        if self.detail_open and widget.row.get("sessionId", "") != self.selected:
            # another session's brief starts at its top
            detail = self.part("#detail")
            if detail is not None:
                detail.scroll_home(animate=False)
        self.selected = widget.row.get("sessionId", "")
        self.mark_selected()
        # or the selection walks off the bottom of a list too long to show
        # No scroll_visible: focus() scrolls the widget into view by itself,
        # and a line that cannot fail is a line nobody can test.
        if self.detail_open:
            self.paint_detail()

    def action_detail(self):
        self.open_detail("brief")

    def action_procs(self):
        """Every process agents started, in the detail pane - `ccwho ps`, here."""
        self.open_detail("procs")

    def open_detail(self, mode):
        # another screen starts at its top, not where the last one was scrolled
        if mode != self.detail_mode or not self.detail_open:
            detail = self.part("#detail")
            if detail is not None:
                detail.scroll_home(animate=False)
        self.detail_open = True
        self.detail_mode = mode
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
            self.hide_search()
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
            self.hide_search()
            self.rebuild()

    def action_search(self):
        box = self.show_search()
        # after the refresh, for the same reason the selection is: a widget that
        # was hidden a moment ago cannot take focus yet, and the keys you type
        # next go to the app's own bindings instead of into the box
        self.call_after_refresh(box.focus)

    def action_go(self):
        if self.detail_open and self.detail_mode == "procs" \
                and self.size.width < engine.UI_WIDE:
            # narrow, the process screen hides the list: Enter would go to a
            # session you cannot see. Not said(): a jump under way stays marked
            self.status = "Esc for the list, then Enter on a session"
            self.paint_header(self.fleet.groups(self.filter_text))
            return
        self._go()

    def _go(self):
        row = self.selected_row()
        if not row:
            return
        self.armed = None       # the question leaves the screen with the jump
        # Name it the way you picked it. "going to daf9..." is not something
        # you can check against the window that comes forward; the title is.
        name = (row.get("tab_title") or row.get("title") or row.get("name") or "")
        self.status = (f"going to {engine.brief.short_id(row.get('sessionId', ''))}"
                       f" {engine.truncate(name, 40)}...")
        action, value = engine.resolve_open(row.get("sessionId", ""),
                                            self.fleet.rows, [],
                                            source_ok=self.fleet.source_ok)
        if action == "program":
            self.said(value)
            return
        if action == "attach":
            # running, with no window: give it one. The same guard the command
            # line uses decides this, so neither can resume a live session.
            self.status = "opening a window for it..."
            self.mark_acting(row.get("sessionId", ""))
            self.paint_header(self.fleet.groups(self.filter_text))
            self.jumps += 1         # only where a jump starts: a number with no
            self.attaching(value, row, self.jumps)      # answer would never end
            return
        if row.get("windowed") is False:
            # We asked iTerm2 about this tty and it had never heard of it: the
            # session is alive with nowhere to go to - `claude bg-spare` does
            # this. Asking iTerm2 anyway returns "not found: /dev/ttys042",
            # which is not an answer anyone can act on.
            self.said(engine.no_window_note(row))
            return
        self.mark_acting(row.get("sessionId", ""))
        self.paint_header(self.fleet.groups(self.filter_text))
        self.jumps += 1
        self.go_to(row, self.jumps)
        # The session you just opened is about to stop needing you. Look again
        # shortly, rather than scanning everything more often for the sake of
        # the one row that is about to change.
        self.set_timer(AFTER_A_JUMP, self.look_again)

    def looked_at(self, row, ts):
        """You are going to this session, so you have reviewed what it did.

        It leaves NEEDS YOU here and now rather than on the next scan, and the
        row is rewritten in place - anything else is a list that argues with
        you for two seconds after you act on it. A session that is genuinely
        waiting on you cannot be dismissed this way: looking at a question does
        not answer it.

        `ts` is the turn you were shown. A newer turn - one that came while the
        window was opening - is one you have not seen.
        """
        if row.get("attention") != "review":
            return
        sid = row.get("sessionId", "")
        if not (sid and ts) or row.get("ts", "") != ts:
            return
        engine.mark_reviewed(sid, ts)
        self.seen[sid] = ts
        row["attention"] = "stopped"
        self.painted_shape = None       # it moves group, so the list is rebuilt
        self.rebuild()

    def look_again(self):
        """A fresh look that the usual wait cannot hold up.

        It counts as the last look, rather than resetting the clock to zero: a
        zero clock made the very next tick collect all over again, so every jump
        cost two full scans a second apart.
        """
        self.collector.mark()
        self.collect()

    @work(thread=True)
    def attaching(self, cmd, row, jump):
        said = self.adapter.attach(cmd, deadline=FOCUS_DEADLINE)
        self.call_from_thread(self.landed, said, row, jump)

    @work(thread=True)
    def go_to(self, row, jump):
        """Also off the UI thread: osascript is another program, and a hung
        iTerm2 must not take the list with it."""
        result = self.adapter.focus(row, deadline=FOCUS_DEADLINE)
        self.call_from_thread(self.landed, result, row, jump)

    def landed(self, text, row, jump):
        """A jump's answer. Rows move here and nowhere else during a jump: the
        scans that waited, and the row you looked at leaving NEEDS YOU.

        Only the LATEST jump's answer ends it. An older one landing while a
        newer row blinks keeps its review for later; a failed jump is not a
        look - "iTerm2 did not answer" showed you nothing."""
        worked = text.startswith(("focused", "attached"))
        if worked and row.get("sessionId"):
            self.reviewing[row["sessionId"]] = row
        latest = jump == self.jumps
        if latest or not worked:
            # an older jump that worked is not the window in front: say nothing
            self.said(text, row)
        if latest:
            self.mark_acting("")        # it is open: stop saying it is opening
            held, self.held = self.held, None
            if held is not None:
                self.fleet = self.keep_seen(held)   # the scans that waited on it
                self.rebuild()
        elif self.acting:
            return                      # a newer row still blinks: later
        reviewing, self.reviewing = self.reviewing, {}
        for sid, looked in reviewing.items():
            self.looked_at(next((r for r in self.fleet.rows
                                 if r.get("sessionId") == sid), looked),
                           looked.get("ts", ""))
        self.paint_header(self.fleet.groups(self.filter_text))

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

    def action_reopen(self):
        """Bring back the last saved fleet - but only when nothing is running.

        The empty list has always offered this and no key did it. Offering it
        with sessions alive would be a way to start a second copy of every one
        of them, so the offer and the key both belong to an empty list.
        """
        if self.fleet.rows:
            return
        self.status = "reopening the last save..."
        self.paint_header(self.fleet.groups(self.filter_text))
        self.reopening()

    @work(thread=True)
    def reopening(self):
        said = self.collector.restore()
        self.call_from_thread(self.said, said)

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
        try:
            engine.reload_all(engine)
            self.reload_error = ""
        except Exception as ex:
            self.reload_error = f"engine reload failed: {ex}"

    def fleet(self):
        import time
        self.reload()
        status = {}
        try:
            rows, procs = engine.collect(cache=self.cache, status=status)
        except Exception as ex:                      # never kill the screen
            return Fleet([], False, "", f"could not read the fleet: {ex}",
                         secure=self.secure_input())
        # The scan's own answer to the cheap question, so the next cheap check
        # does not see a changed world and scan all over again.
        if status.get("watch") is not None:
            self.digest = status["watch"]
        trouble = self.reload_error or ("" if status.get("source_ok") else
                                        "cannot read the session list - run `ccwho doctor`")
        self.rows = rows
        return Fleet(rows, status.get("source_ok", False),
                     time.strftime("%H:%M:%S"), trouble, procs=procs,
                     secure=self.secure_input(), usage=self.usage(rows))

    def usage(self, rows):
        """The usage snapshot, on this collecting thread; "unknown" when it
        cannot be read. Never the reason the list goes down."""
        try:
            import ccwho as runner
            return runner.usage_snapshot(rows)
        except Exception:
            return {"state": "unknown"}

    def secure_input(self):
        """The Secure Input line, or "". 18ms, on the collecting thread, and
        never the reason the list goes down."""
        try:
            import ccwho_setup as setup
            line = setup.secure_input_problem(setup.secure_input_holder())
        except Exception:
            return ""
        return f"hotkey: {line}" if line else ""

    def saved_count(self):
        """How many sessions the newest manifest holds, or 0."""
        try:
            import ccwho as runner
            return len(runner.newest_manifest().get("sessions", []))
        except Exception:
            return 0

    def restore(self):
        """Reopen the saved fleet, by running ccwho's own restore."""
        try:
            import ccwho as runner
            return runner.reopen_saved()
        except Exception as ex:
            return f"could not reopen: {ex}"

    def kill_loops(self, row):
        pids = [d["pid"] for d in row.get("dead_loops") or []]
        try:
            killed = engine.kill_dead_loops(row.get("pid"), pids)
        except Exception as ex:
            return f"could not kill: {ex}"
        if not killed:
            return "nothing killed: no loop there is stuck any more"
        return "killed loop " + ", ".join(str(p) for p in killed)

    def brief(self, row):
        head, tail, _ = engine.read_windows(row.get("sessionId", ""), cache=self.cache)
        return engine.brief.build(head, tail, session=row, now=engine.now_iso(),
                                  as_records=engine.as_records)


class Adapter:
    """iTerm2, behind a door the tests can close."""

    def attach(self, cmd, deadline=FOCUS_DEADLINE):
        """One new iTerm2 window, running `claude attach <id>`."""
        try:
            done = subprocess.run(
                ["osascript", "-e", engine.iterm_run_script(cmd)],
                capture_output=True, text=True, timeout=deadline)
        except subprocess.TimeoutExpired:
            return f"iTerm2 did not answer in {deadline:g}s"
        except OSError as ex:
            return f"could not reach iTerm2: {ex}"
        if done.returncode:
            return f"could not open a window: {(done.stderr or '').strip()}"
        return "attached it in a new window"

    def keep_in_front(self):
        """The hotkey panel only: take back the focus iTerm2 gives it and then
        loses, when the key is pressed from another app. See ccwho_panel."""
        panel.keep_in_front(os.environ)

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
