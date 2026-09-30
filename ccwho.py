#!/usr/bin/env python3
"""ccwho - which Claude Code session needs you, and what is it about.

This file is the RUNNER and is meant to stay small. All logic lives in
ccwho_engine.py, which is reloaded on every tick of --watch, so you can edit the
engine while a watch is running and the next tick picks it up without a restart.
Same model as network_check_ruby: thin loop, hot-reloaded engine, state carried
between iterations rather than held inside the engine.
"""
from __future__ import annotations

import contextlib
import errno
import fcntl
import io
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid

import ccwho_engine as engine
import ccwho_index as index
import ccwho_setup as setup
import ccwho_usage as usage

CLEAR_HOME = "\033[H\033[2J"
DIM, RESET, RED = "\033[2m", "\033[0m", "\033[31m"
AMBER = "\033[33;1m"


def reload_engine(state):
    """Re-read the engine. A broken edit keeps the last good module and says so.

    engine.reload_all does the work - the rule modules first, in the order of
    engine.RELOAD_FIRST - and is shared with the list, so the two cannot drift.
    What stays here is the runner's own handle on the index, rebound below.
    """
    try:
        engine.reload_all(engine)
        # and rebind OUR handles: sys.modules is what the next import sees, but
        # this module's globals still point at the objects loaded at startup
        globals()["index"] = sys.modules[index.__name__]
        globals()["usage"] = sys.modules[usage.__name__]
        state["engine_error"] = ""
    except Exception:
        state["engine_error"] = traceback.format_exc(limit=2).strip().splitlines()[-1]
    return engine


def tick(state, argv, color):
    eng = reload_engine(state) if state["watch"] else engine
    rows, fleet = scan(cache=state.setdefault("cache", {}), eng=eng)
    fleet = with_usage(rows, fleet)
    if "--blocked" in argv:
        rows = eng.only_blocked(rows)
    out = eng.render(rows, fleet, color=color,
                     show_prompt="--prompt" in argv or "-p" in argv,
                     links=state.get("links", False))
    state["ticks"] += 1
    return out, rows


WATCH_FLAGS = ("--watch", "-w")
KNOWN_FLAGS = {"--watch", "-w", "--blocked", "--prompt", "-p", "--json",
               "--no-color", "--no-links", "--help", "-h"}
MIN_INTERVAL = 1.0


def watch_requested(argv):
    return any(a in WATCH_FLAGS or a.startswith("--watch=") for a in argv)


def parse_interval(argv, default=5.0):
    """Accepts --watch N, --watch=N and -w N. A missing or unparseable value is
    the default, never a silent no-op."""
    for i, a in enumerate(argv):
        if a.startswith("--watch="):
            raw = a.split("=", 1)[1]
        elif a in WATCH_FLAGS and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            raw = argv[i + 1]
        else:
            continue
        try:
            return max(MIN_INTERVAL, float(raw.rstrip("s")))
        except ValueError:
            return default
    return default


def unknown_flags(argv):
    """Anything dash-prefixed we do not recognise. Silently ignoring a flag is how
    `--watch=3` used to run one-shot and look like a broken loop."""
    bad, skip = [], False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            continue
        if not a.startswith("-"):
            continue
        if a.startswith("--watch="):
            continue
        if a in KNOWN_FLAGS:
            if a in WATCH_FLAGS and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                skip = True
            continue
        bad.append(a)
    return bad


# Things you asked for (jump, open, restore) go straight to iTerm2 - they are one
# at a time by nature, unlike the background asks behind engine.iterm_ask - but
# never without a deadline: an osascript with none waits as long as a stuck
# iTerm2 does, which is forever (2026-09-29).
# 20 s, not the list's 5 (FOCUS_DEADLINE): a command waits where you typed it,
# while the list must stay responsive and says so on the screen.
ITERM_ACTION_DEADLINE = 20.0


MAY_STILL_RUN = "the launch may still run; if it does not appear, restart iTerm2"
RECEIVER_GONE = ("-609", "-600")  # connection invalid, not running: the event died with it
CUT_OFF = "cut off when iTerm2 quit"     # in every line about such a launch: `o` looks for it
AUTOMATION = "allow it under System Settings > Privacy > Automation"


def _not_recorded(sids):
    """Why a launch of these was not recorded: what the claim dir, its lock or
    the send lock said (_unrecorded), else where to look."""
    why = next((w for sid, w in _unrecorded().items() if sid in sids and w), None)
    return ("could not record the launch in ccwho's own dir - not sending it ("
            + (why or f"check that {os.path.join(ccwho_dir(), 'launching')} can be written") + ")")


def launch_in_iterm(script, deadline, sids):
    """Run an osascript that starts sessions: (result, "") when iTerm2 ran all
    of it, else (None, what to tell the user).

    The launch's send lock is taken (_hold_send), and every claim in `sids`
    marked unresolved, naming it, with no iTerm2 named yet (any of ours holds
    it), BEFORE the event is sent: a timeout, -1712 from inside iTerm2,
    Ctrl-C or a closed terminal leaves the event queued there - a killed
    osascript does not cancel it - and a retry would start the session
    twice. osascript holds the lock too (_send): a launcher that dies mid-send
    leaves it held while its osascript runs. Then, by outcome:
    - all of it ran (rc 0): launched (claim_launched), held until seen, or
      LAUNCH_CLAIM_SECONDS - and against any decision made before it;
    - nothing was sent - an error raised in Popen's constructor before any
      process was made (its pipes, fork, an argument refused), or after exec
      reported the program could not run (_being_made) - or iTerm2 refused
      Apple Events (-1743) at the script's first event (engine.AE_PROBE,
      which writes nothing): the claims go (drop_claims);
    - iTerm2 quit during it (-600/-609): the event died with it, and what it
      opened may still start - held LAUNCH_CLAIM_SECONDS, cut off
      (claim_launched);
    - anything else - a timeout, -1712, a signal, -1743 after the first
      event, an error after a write (a window closing while the pane-fill
      script walks them), an error raised after osascript's process was made
      (before exec's report, or while its output was read), whatever its
      errno, or one that cannot be placed: unresolved, bound to the iTerm2 a
      table taken after the send shows - to each of them, when it shows two
      of ours (_sent). A process Popen's constructor made is first ended and
      reaped when it can be (_ended), as subprocess.run ends one it gives up
      on, and only then are the claims bound; on Ctrl-C, only when its
      process is found and shown ended (_popen_in). One that cannot be
      ended, or found, leaves the record made before the send, which names
      its send lock: held while that lock is (_sending), dated when it is let
      go (_dated).
    A launch that cannot be recorded is not sent."""
    if _unrecorded().keys() & set(sids):
        # its claim could not be written: nothing to mark, nothing to send
        drop_claims(sids)
        return None, _not_recorded(sids)
    send = None
    try:
        try:
            send = _hold_send()
        except OSError as ex:
            for sid in sids:
                _unrecorded()[sid] = _said(ex)      # _not_recorded says why
        # the first mark that cannot be written ends it: all of them go
        marked = send is not None and all(claim_unresolved(sid, None, send=send[0]) for sid in sids)
    except BaseException:
        # Ctrl-C while marking: nothing was sent
        if send:
            _let_go_send(send)
        drop_claims(sids)
        raise
    if not marked:
        elsewhere = any(_theirs(sid) for sid in sids)
        if send:
            _let_go_send(send)
        drop_claims(sids)                  # nothing was sent: none of them may hold
        return None, ("a session in it is running now, or another ccwho is opening it"
                      " - not sending it" if elsewhere else _not_recorded(sids))
    try:
        return _send(script, deadline, sids, send[1])
    finally:
        _let_go_send(send)


def _being_made(ex):
    """The Popen whose constructor `ex` was raised in - while subprocess.run
    was still making osascript's process - else None: raised after (while
    its output was read, or it was waited for), or where this cannot tell.
    Told by where it was raised, never by its errno: fork's EAGAIN is
    poll(2)'s too."""
    tb = ex.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code is subprocess.Popen.__init__.__code__:
            return tb.tb_frame.f_locals.get("self")
        tb = tb.tb_next
    return None


def _never_ran(p):
    """Did osascript never run, by the Popen an error was raised in the
    making of (_being_made)? None was made (its pipes, fork, an argument
    refused), or exec reported it could not run and it was reaped. False for
    None: raised after, or where _being_made cannot tell."""
    return p is not None and (not getattr(p, "_child_created", True)
                              or getattr(p, "returncode", None) is not None)


_RUN_CODE = subprocess.run.__code__      # subprocess.run's own, taken at import


def _popen_in(ex):
    """The Popen `ex` was raised with, from its frames: the one whose
    constructor raised it (_being_made), else subprocess.run's own
    (`process`, once made) - else None, where this cannot tell (raised on
    run's `with` line, or by a fake run)."""
    made = _being_made(ex)
    if made is not None:
        return made
    tb = ex.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code is _RUN_CODE:
            p = tb.tb_frame.f_locals.get("process")
            if isinstance(p, subprocess.Popen):
                return p
        tb = tb.tb_next
    return None


def _interrupted(ex):
    """Was `ex` raised while an interrupt (Ctrl-C) was being handled - one in
    its chain (__context__, __cause__) that is no Exception? It may then hide
    a Ctrl-C that landed between Popen's fork and its note of the child."""
    seen, e = set(), ex.__context__ or ex.__cause__
    while e is not None and id(e) not in seen:
        if not isinstance(e, Exception):
            return True
        seen.add(id(e))
        e = e.__context__ or e.__cause__
    return False


def _ended(p):
    """Is the send's process p ended - killed and reaped here, as
    subprocess.run ends one it gives up on? p is the one Popen's constructor
    left (_being_made: the error branch) or the one found on Ctrl-C
    (_popen_in). Ended only when it can be shown to be: its returncode, which
    only reaping sets (read as a Popen of another CPython may lack it).
    False when it could not be ended: the send is then not recorded as over
    (_sent) - the record made before it names the send lock, which that
    process holds, and holds while it runs (_sending). True for None - which
    means "ended" only in the error branch, where subprocess.run has already
    ended its own; a caller whose None means "cannot tell" (_popen_in's) must
    not take it for ended."""
    if p is None:
        return True
    with contextlib.suppress(Exception):
        p.kill()
        p.wait()
    return getattr(p, "returncode", None) is not None


def _send(script, deadline, sids, lock):
    """launch_in_iterm's send, with its send lock held - by osascript too."""
    try:
        # errors="replace": decoding comes after osascript ran - a byte the
        # locale cannot read must not look like nothing was sent
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True,
                           errors="replace", timeout=deadline, pass_fds=(lock,))
    except subprocess.TimeoutExpired:
        _sent(sids)
        return None, f"iTerm2 did not answer in {deadline:g}s - {MAY_STILL_RUN}"
    except (OSError, ValueError, TypeError) as ex:
        # the type only: the list's `o` shows this line, and an error's text
        # can hold a path
        p = _being_made(ex)
        if _never_ran(p) and not _interrupted(ex):
            # no process was made, or exec reported it could not run (and it
            # was reaped): osascript never ran, nothing was sent. (_never_ran
            # reads "no child noted" as none made: an error of these kinds
            # comes only from what Popen calls - never between its fork and
            # its note of the child, as a Ctrl-C can. One raised while a
            # Ctrl-C was handled - a pipe closed as it unwound - may hide one
            # that landed there: then it is no proof, and the claims stay)
            drop_claims(sids)
            return None, f"could not drive iTerm2 ({type(ex).__name__})"
        if _ended(p):
            _sent(sids)                         # it may have run: its event may be out
        return None, f"could not drive iTerm2 ({type(ex).__name__}) - {MAY_STILL_RUN}"
    except BaseException as ex:
        # Ctrl-C: it may still run. (A closed terminal kills us without a
        # word: the record made before the send holds then.) The send is
        # recorded as over only when its process is found and shown ended
        # (_popen_in, _ended). A Ctrl-C between Popen's fork and its note of
        # the child, or on subprocess.run's `with` line, leaves a process of
        # ours running with the send lock that nothing names: the record made
        # before the send names that lock - held while it is held
        # (_sending), dated when it is let go (_dated). (Shown ended: a
        # returncode, which only reaping a child sets - one whose fork was
        # never noted has no pid to kill or reap.)
        _LOCAL.stopped_send = True              # _interruptible says it may still run
        p = _popen_in(ex)
        if p is not None and _ended(p):
            _sent(sids)
        raise
    if r.returncode == 0:
        claim_launched(sids)
        return r, ""
    # its code, never its text: the list shows this line, and osascript's
    # message quotes data (paths, tab names)
    code = engine.ae_error_code(r.stderr)
    if code == engine.AE_NOT_PERMITTED and engine.ae_error_at(r.stderr, script, engine.AE_PROBE):
        # refused at the script's first event, which writes nothing
        drop_claims(sids)
        return None, f"iTerm2 refused ({code}) - nothing was sent; {AUTOMATION}"
    if code in RECEIVER_GONE:
        # the event died with its receiver; windows opened before may start:
        # held LAUNCH_CLAIM_SECONDS, never on an iTerm2 started since
        claim_launched(sids, cut_off=True)
        return None, (f"the launch was {CUT_OFF} ({code}) - what it opened may still start;"
                      f" try again in {int(LAUNCH_CLAIM_SECONDS)} s")
    _sent(sids)
    if code == engine.AE_TIMED_OUT:
        return None, f"iTerm2 did not answer ({code}) - {MAY_STILL_RUN}"
    if code == engine.AE_NOT_PERMITTED:
        # after the first event - or no telling where: what came before the
        # refusal may have been written (the pane fill walks on after its
        # write), so what may run holds
        where = " part way through" if engine.ae_error_start(r.stderr) is not None else ""
        return None, f"iTerm2 refused ({code}){where} - {AUTOMATION}; {MAY_STILL_RUN}"
    # killed by a signal, failed with no code, or failed after a write
    how = code or ("stopped by a signal" if r.returncode < 0 else f"exit {r.returncode}")
    return None, f"iTerm2 did not finish ({how}) - {MAY_STILL_RUN}"


def _sent(sids):
    """The send is over and its launch may still run: the claims are bound to
    the iTerm2 a table taken now shows - the one that got the event, or its
    restart; all of them when it shows two of ours, as the one `tell
    application "iTerm2"` picked is not known - and dated the end of the
    send, so that no table from before it can bind or release them. When the
    table shows none, or cannot be read, the record from before the send
    stays: it names its send lock, and is dated when that is first seen let
    go of (_dated); any iTerm2 of ours holds it."""
    shown = engine.iterm_app_pids(engine.app_snapshot())
    if shown:
        for sid in sids:
            claim_unresolved(sid, shown)


def scan(cache=None, status=None, eng=None):
    """engine.collect(), and every session it sees running - a parked terminal
    too - lets go of its launch claim (release_claims). From the moment the
    scan STARTED: a claim made while `claude agents` was being read may be a
    new launch of a session that has ended since. That moment is
    status["seen_at"]: what a caller decides from these rows, it decided then.
    `eng` is the engine to scan with - the watch loop's reloaded one."""
    eng = engine if eng is None else eng
    seen_at, seen_mono = time.time(), _mono()
    if status is not None:
        status["seen_at"], status["seen_mono"] = seen_at, seen_mono
    rows, fleet = eng.collect(cache=cache, status=status)
    release_claims(eng.live_ids(rows), seen_at, seen_mono)
    return rows, fleet


def restore_deadline(windows):
    """A window per session, dozens of them: time for each, not one for all."""
    return 30.0 + 5.0 * max(1, windows)


def jump(argv):
    """Focus the terminal window holding a session. Needs iTerm2."""
    query = " ".join(a for a in argv if not a.startswith("-"))
    rows, _ = scan(cache={})
    hits = engine.match_rows(rows, query)
    if not hits:
        print(f"ccwho: no session matches {query!r}", file=sys.stderr)
        return 1
    if len(hits) > 1:
        print(f"ccwho: {query!r} matches {len(hits)} sessions - be more specific:",
              file=sys.stderr)
        for line in engine.pick_lines(hits):
            print(f"  {line}", file=sys.stderr)
        return 2
    row = hits[0]
    tty = row.get("tty", "")
    if not tty:
        print(f"ccwho: {row.get('title')} has no controlling terminal", file=sys.stderr)
        return 1
    script = os.path.join(os.path.dirname(os.path.realpath(__file__)), "jump.applescript")
    try:
        res = subprocess.run(["osascript", script, f"/dev/{tty}"],
                             capture_output=True, text=True, errors="replace",
                             timeout=ITERM_ACTION_DEADLINE)
    except subprocess.TimeoutExpired:
        print(f"ccwho: iTerm2 did not answer in {ITERM_ACTION_DEADLINE:g}s", file=sys.stderr)
        return 1
    # what jump.applescript says, or osascript's code - never its text, which
    # quotes paths and tab names
    out = (res.stdout or "").strip() or f"ccwho: {engine.osascript_trouble(res.returncode, res.stderr)}"
    print(out)
    return 0 if out.startswith("focused") else 1


def show(argv):
    """What was this session working on? The answer without opening it.

    The recap the harness already wrote leads, because it is the only line that
    was written to answer this exact question - but never without its age and the
    turns since, or a two-day-old recap reads as the current state of the work.
    """
    want_json = "--json" in argv
    query = " ".join(a for a in argv if not a.startswith("-"))
    cache = {}
    rows, _ = scan(cache=cache)
    entry = {}
    if query:
        # Not running is not gone: the transcript is still on disk, and so is the
        # answer to "what was that one about".
        hits, ended = matches(query, rows, everything="--all" in argv)
        hits = hits + ended
        found = index.search(fresh_index(), query, everything="--all" in argv)
        by_id = {e.get("sessionId"): e for e in found}
        if hits:
            entry = by_id.get(hits[0].get("sessionId"), {})
    else:
        hits = rows
    if not hits:
        print(f"ccwho show: no session matches {query!r}", file=sys.stderr)
        return 1
    if len(hits) > 1:
        print(f"ccwho show: {query!r} matches {len(hits)} sessions - be more specific:",
              file=sys.stderr)
        for line in engine.pick_lines(hits):
            print(f"  {line}", file=sys.stderr)
        return 2
    if not hits:
        return 1
    row = hits[0]
    head, tail, _mtime = engine.read_windows(row.get("sessionId", ""), cache=cache)
    b = engine.brief.build(head, tail, session=row,
                           now=engine.now_iso(), as_records=engine.as_records)
    # The index read the WHOLE transcript once; the brief reads a head and a tail
    # window. A recap in the unread middle belongs to the index, and dropping it
    # here would lose the very line that found the session.
    if entry.get("recap") and (not b["recap"] or entry.get("recap_ts", "") > b["recap_ts"]):
        b["recap"] = entry["recap"]
        b["recap_ts"] = entry.get("recap_ts", "")
        b["recap_age"] = engine.brief.age_between(b["recap_ts"], engine.now_iso())
        b["turns_since_recap"] = entry.get("turns_since_recap", 0)
    if want_json:
        print(json.dumps(b, indent=2))
        return 0
    print(engine.render_brief(b, row, color=sys.stdout.isatty()
                              and "NO_COLOR" not in os.environ))
    return 0


UI_RESTART = 42             # what ccwho_ui exits with when you press R


def find_uv():
    """uv on PATH, or wherever it installed itself. "" when it is not here.

    A hotkey window has iTerm2's environment, and iTerm2 was started by the
    Dock: no homebrew on PATH, so `uv` is not found, the list never starts, and
    the window closes reporting nothing.
    """
    return engine.find_tool("uv")


def wait_for_a_key():
    """Hold a window open so the line above can be read."""
    try:
        input("\npress return to close this window ")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def hotkey_window(argv):
    """The list, started by the hotkey rather than by you.

    The difference that matters: nobody typed this, so nobody is watching a
    shell prompt afterwards. If the list cannot start, the window would close
    with the reason unread, and iTerm2 would report only "a session ended very
    soon after starting" - which is what it did.
    """
    rc = run_ui()
    if rc:
        wait_for_a_key()
    return rc


def run_ui():
    """Hand over to the TUI. uv fetches Textual from the script's own header, so
    there is no virtualenv to make - but if uv is missing, say that in one line
    rather than dying in an import."""
    ui = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ccwho_ui.py")
    uv = find_uv()
    if not uv:
        print("ccwho: the live list needs uv (it fetches Textual for you):",
              file=sys.stderr)
        print("  brew install uv          # then run ccwho again", file=sys.stderr)
        print("  ccwho ls                 # the one-shot table needs nothing",
              file=sys.stderr)
        return 1
    while True:
        done = subprocess.run([uv, "run", "--quiet", "--script", ui])
        if done.returncode != UI_RESTART:
            return done.returncode
        # R: the code on disk changed under a window that stays open for days


# ------------------------------------------------------------- ccwho setup
# One command for a new machine: the applet, the autosave job, and a key that
# brings the list up over whatever you are doing. Everything it decides is in
# ccwho_setup and tested there; this is the part that writes, prints, and asks.

# The tests need setup to write somewhere that is not the real home. A FLAG
# would let a real user install another account's paths into their own launchd
# domain, so this is an environment variable the help never mentions instead.
TEST_HOME_VAR = "CCWHO_TEST_HOME"


def setup_home():
    return os.environ.get(TEST_HOME_VAR) or os.path.expanduser("~")


def repo_dir():
    """Where ccwho's own files are - the template and install-handler.sh live
    beside the runner, wherever it was cloned."""
    return os.path.dirname(os.path.realpath(__file__))


def ccwho_bin():
    """The name to put in a plist or a profile. `ccwho` on PATH if that is this
    same file (the usual symlink install), else this file itself - never a name
    that resolves to somebody else's copy."""
    link = shutil.which("ccwho")
    if link and os.path.realpath(link) == os.path.realpath(__file__):
        return link
    return os.path.realpath(__file__)


SETUP_FLAGS = {"--yes", "-y", "--no-hotkey", "--no-list", "--hotkey", "--proof",
               "--usage", "--no-usage"}


def setup_cmd(argv):
    """Install what ccwho needs, say what was already there, end in the list."""
    # An option setup does not know is refused rather than ignored. This is the
    # door that a --home would come back through, and a --home would install one
    # account's paths into another account's launchd domain.
    unknown = [a for i, a in enumerate(argv)
               if a.startswith("-") and a not in SETUP_FLAGS
               and not (i and argv[i - 1] in ("--hotkey", "--proof"))]
    if unknown:
        print(f"ccwho setup: unknown option(s): {' '.join(unknown)}", file=sys.stderr)
        print(f"usage: ccwho setup [--yes] [--hotkey {setup.DEFAULT_HOTKEY}]"
              " [--no-hotkey] [--no-list] [--usage | --no-usage]", file=sys.stderr)
        return 2
    if "--usage" in argv and "--no-usage" in argv:
        print("ccwho setup: --usage and --no-usage cannot both be meant", file=sys.stderr)
        return 2
    # Run BY the hotkey window, not by a person: record the nonce setup is
    # waiting on, then become the list, so the first press is already useful.
    if "--proof" in argv:
        nonce = _arg(argv, "--proof", "")
        if not nonce:
            print("ccwho setup --proof needs the nonce setup is waiting for"
                  " (setup puts it there itself)", file=sys.stderr)
            return 2
        setup.write_proof(ccwho_dir(), nonce)
        return hotkey_window([])       # a proof window dies as silently as any

    yes = "--yes" in argv or "-y" in argv
    home = setup_home()
    hotkey = _arg(argv, "--hotkey", setup.DEFAULT_HOTKEY)
    if hotkey not in setup.HOTKEYS:
        print(f"ccwho setup: no hotkey called {hotkey!r}. Choose one of: "
              + ", ".join(sorted(setup.HOTKEYS)), file=sys.stderr)
        return 2

    facts = setup.gather(ccwho_dir=ccwho_dir())
    facts["uv"] = shutil.which("uv") or ""
    facts["hotkey_installed"] = setup.hotkey_current(
        home, setup.hotkey_profile(setup.window_command(ccwho_bin()),
                                   hotkey=hotkey))
    # "Installed" is not "working": a job whose ccwho moved loads, runs, finds
    # nothing and exits 0, and an applet can claim ccwho:// while calling a copy
    # that was deleted. Both read as done unless we look.
    facts["autosave_current"] = setup.plist_matches(
        read_text(autosave_plist_path(home)), render_autosave(home) or "")
    facts["handler_current"] = setup.handler_target(handler_script()) == ccwho_bin()
    steps = setup.setup_plan(facts)

    for s in steps:
        print(f"{setup.step_mark(s)} {s['name']:<20} {s['detail']}")
        if setup.step_mark(s) == "note":
            print(f"     {s['fix']}")
        if s["todo"] and s["blocking"]:
            print(f"\n{s['fix']}")
            return 1                 # nothing is written on a machine that cannot work
    print()

    todo = {s["name"] for s in steps if s["todo"]}
    did = []
    if "uv" in todo:
        print("the live list needs uv: brew install uv   (everything else still works)\n")

    if "hotkey reach" in todo:
        # Ask the way any app asks: by trying to use it. macOS raises the
        # dialog, attributed to iTerm2 - the app that needs the permission,
        # because it is the one that registers the key grab.
        print("asking macOS for Accessibility (the dialog names iTerm)...")
        if setup.ask_for_accessibility():
            did.append("Accessibility granted")
        else:
            print("  not granted. System Settings > Privacy & Security >"
                  " Accessibility > iTerm, then run ccwho setup again.")

    if "ccwho:// handler" in todo:
        rc = install_handler()
        if rc:
            print("ccwho setup: the ccwho:// handler could not be registered",
                  file=sys.stderr)
            return 1
        did.append("registered the ccwho:// handler")

    if "autosave job" in todo:
        rc = install_autosave(home)
        if rc:
            return 1
        did.append("loaded the autosave job (every 15 minutes, and at login)")

    for line in did:
        print(line)

    # Subscription usage comes from a statusLine in each interactive config dir.
    # The one step that writes another tool's file: it shows the diff and asks.
    roots, skipped = usage_roots()
    usage_setup(roots, skipped, yes=yes,
                mode="off" if "--no-usage" in argv else "on" if "--usage" in argv else "")

    # Said, never done: quitting iTerm2 ends every session running in it, which
    # is the user's call and nobody else's.
    reach = setup.hotkey_reach(facts)
    if reach["do"] == "restart":
        print(f"\n{reach['detail']}.\n  restart iTerm2 once to fix it"
              " - `ccwho save` first, so `ccwho restore --open` can bring your"
              " sessions back.")

    # An installed hotkey is left alone: a second run must not take the key away
    # and ask you to press it again. Naming a key is how you change your mind.
    asked_for_a_key = "--hotkey" in argv
    if "--no-hotkey" in argv:
        pass
    elif facts["hotkey_installed"]:
        # Already exactly what we would write - including the key asked for,
        # because hotkey_current compares the whole profile. Rewriting it would
        # take the hotkey away and give it back for nothing.
        print(f"hotkey {setup.HOTKEYS[hotkey]['label']} was already installed")
    elif setup.hotkey_installed(home, hotkey) and not asked_for_a_key:
        # The key itself is unchanged and already proved: only the profile's
        # settings moved on. Rewrite it, and do not make anyone press anything.
        install_hotkey(home, hotkey, prove=False, iterm_ok=facts.get("iterm_ok"),
                       iterm_why=facts.get("iterm_why"))
    else:
        rc = install_hotkey(home, hotkey, prove=not yes and facts.get("iterm_ok"),
                            iterm_ok=facts.get("iterm_ok"), iterm_why=facts.get("iterm_why"))
        if rc:
            return 1
    if "--no-hotkey" in argv and not did:
        print("already set up - nothing to do.")

    if "--no-list" in argv:
        return 0
    if "uv" in todo:
        return 0            # the install worked; only the list cannot open yet
    return run_ui()


def install_handler():
    """The AppleScript applet that makes ccwho:// links work. The script is the
    installer; running it is not reimplemented here."""
    script = os.path.join(repo_dir(), "install-handler.sh")
    env = dict(os.environ, CCWHO_BIN=ccwho_bin())
    done = subprocess.run([shutil.which("bash") or "/bin/bash", script],
                          env=env, capture_output=True, text=True)
    return done.returncode


def autosave_plist_path(home):
    return os.path.join(home, "Library", "LaunchAgents",
                        setup.PLIST_LABEL + ".plist")


def read_text(path):
    """A file's contents, or "" - never an exception. Every caller here is
    asking "what is installed", and "nothing readable" is a fine answer."""
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return ""


def handler_script():
    """The applet's own source, so we can see which ccwho it calls."""
    for app in setup.handler_candidates():
        scpt = os.path.join(app, "Contents", "Resources", "Scripts", "main.scpt")
        if os.path.exists(scpt):
            done = subprocess.run(["osadecompile", scpt],
                                  capture_output=True, text=True)
            return done.stdout if done.returncode == 0 else ""
    return ""


# macOS always has this one, and nothing an unrelated `pyenv uninstall` or
# `brew upgrade` does can take it away. ccwho is stdlib only, so it runs there.
SYSTEM_PYTHON = "/usr/bin/python3"


def job_python():
    """The interpreter to write into a job that runs unattended for years."""
    return SYSTEM_PYTHON if os.path.exists(SYSTEM_PYTHON) else sys.executable


def render_autosave(home):
    """The job this machine should be running. None when the template is gone."""
    template = read_text(os.path.join(repo_dir(), setup.PLIST_TEMPLATE_NAME))
    if not template:
        return None
    return setup.render_plist(template, home=home, ccwho=ccwho_bin(),
                              claude=engine.find_tool("claude"),
                              python=job_python())


def install_autosave(home):
    """Render the job for THIS machine, check it, then replace the loaded one.

    Order matters: the text is validated BEFORE the working job is taken out,
    and the previous plist is kept so a refused bootstrap can be undone. The
    failure being designed against is a machine left with no autosave at all.
    """
    body = render_autosave(home)
    if body is None:
        print("ccwho setup: cannot read the autosave template beside ccwho.py",
              file=sys.stderr)
        return 1
    claude = engine.find_tool("claude")
    problems = setup.plist_problems(body, home=home, claude=claude)
    if problems:
        print("ccwho setup: the autosave job was not installed:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        return 1                      # nothing written: no half-installed job

    path = autosave_plist_path(home)
    before = read_text(path)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        write_atomic(path, body)
    except OSError as exc:
        print(f"ccwho setup: cannot write {path}: {exc}", file=sys.stderr)
        return 1

    # bootstrap over a loaded job fails, so take it out first. A bootout that
    # finds nothing loaded is not an error here.
    target = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{target}/{setup.PLIST_LABEL}"],
                   capture_output=True, text=True)
    done = subprocess.run(["launchctl", "bootstrap", target, path],
                          capture_output=True, text=True)
    if done.returncode:
        print(f"ccwho setup: launchctl refused the job: "
              f"{(done.stderr or '').strip()}", file=sys.stderr)
        if before:
            # Put back the one that was working and load it again, or this
            # leaves the machine with no autosave at all.
            try:
                write_atomic(path, before)
            except OSError:
                pass
            back = subprocess.run(["launchctl", "bootstrap", target, path],
                                  capture_output=True, text=True)
            print("ccwho setup: the previous autosave job is "
                  + ("loaded again" if back.returncode == 0
                     else "back on disk but would not load - run it yourself:"
                          f" launchctl bootstrap {target} {path}"),
                  file=sys.stderr)
        return 1
    return 0


def write_atomic(path, text, mode=None):
    """Temp + replace. A crash half way through must not leave half a file, and
    must never lose the one that was there. With `mode`, the temp file has it
    from the start: a private file is never readable for a moment."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        if mode is None:
            fh = open(tmp, "w", encoding="utf-8")
        else:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            try:
                os.fchmod(fd, mode)   # a leftover temp file keeps its old mode
                fh = os.fdopen(fd, "w", encoding="utf-8")
            except BaseException:
                os.close(fd)
                raise
        with fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# iTerm2 registers a hotkey when a profile APPEARS. Measured: editing the
# profile in place left it firing on the OLD key - the file said key code 44
# while iTerm2 still had 13 - so a changed key means taking the profile away,
# letting iTerm2 notice, and putting it back.
PROFILE_RELOAD_PAUSE = 1.5


def install_hotkey(home, hotkey, prove=True, iterm_ok=True, deadline=30.0, iterm_why=None):
    """Write the iTerm2 profile, and - when there is an iTerm2 to ask - make the
    user press the key before calling it done.

    A profile that was written is not a hotkey that works: another app can own
    the key already. So the profile runs a one-shot command carrying a nonce,
    and this waits to see that nonce land. If it never does, whatever was on
    this machine before goes back, and the user is told which key to try next.
    """
    key = setup.HOTKEYS[hotkey]
    path = setup.profile_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    before = None
    if os.path.exists(path):
        with open(path) as fh:
            before = fh.read()

    def key_of(text):
        try:
            for p in json.loads(text or "")["Profiles"]:
                if p.get("Guid") == setup.PROFILE_GUID:
                    return p.get("HotKey Key Code")
        except (ValueError, KeyError, TypeError):
            pass
        return None

    if before and key_of(before) not in (None, setup.HOTKEYS[hotkey]["code"]):
        # A different key is registered. Editing the profile does not move it:
        # the profile has to go away and come back.
        try:
            os.remove(path)
        except OSError:
            pass
        time.sleep(PROFILE_RELOAD_PAUSE)
        before = None           # it is gone; there is nothing to put back

    def write(doc):
        # Ours is one profile in a file that may hold others. Replace the entry
        # with our Guid and keep every other one exactly as it was.
        try:
            existing = json.loads(before)["Profiles"]
        except (TypeError, ValueError, KeyError):
            existing = []
        keep = [p for p in existing if p.get("Guid") != setup.PROFILE_GUID]
        write_atomic(path, json.dumps({"Profiles": keep + doc["Profiles"]},
                                      indent=2))

    plain = setup.hotkey_profile(setup.window_command(ccwho_bin()),
                                 hotkey=hotkey)
    if not prove:
        write(plain)
        print(f"hotkey {key['label']} installed"
              f" ({key['label']} no longer types {key['instead_of']})")
        if iterm_ok is None and iterm_why == "no-table":
            print("could not read the process list (ps) to find iTerm2 - press "
                  + key['label'] + " to try it")
        elif iterm_ok is None:
            print("iTerm2 is not answering Apple Events - restart iTerm2, then press "
                  + key['label'])
        elif not iterm_ok:
            print("start iTerm2, then press " + key['label'])
        return 0

    nonce = setup.new_nonce()
    write(setup.hotkey_profile(setup.proof_command(ccwho_bin(), nonce),
                               hotkey=hotkey))
    print(f"press {key['label']} now - it should open the list."
          f"  ({key['label']} no longer types {key['instead_of']})")
    if setup.wait_for_proof(ccwho_dir(), nonce, deadline=deadline):
        write(plain)               # the nonce was a test, not the hotkey's job
        print(f"{key['label']} works.")
        return 0

    if before is None:
        os.remove(path)
    else:
        write_atomic(path, before)
    print(f"{key['label']} did not reach ccwho - something else may own that key."
          f"\nthe hotkey was left as it was. Try another: "
          + " or ".join(f"ccwho setup --hotkey {k}"
                        for k in setup.HOTKEYS if k != hotkey))
    return 1                # setup did not do what it said it would do

def index_path():
    return os.path.join(ccwho_dir(), "index.jsonl")


def fresh_index(quiet=False):
    """The index of every session on disk, brought up to date.

    Cold: ~11s over 1,402 transcripts, once. Warm: 0.03s, because only the bytes
    appended since last time are read. The first build says what it is doing -
    eleven silent seconds reads as a hang.
    """
    idx = index.load(index_path())
    paths = index.transcripts()
    if not idx and not quiet:
        print(f"indexing {len(paths)} sessions (first run only)...", file=sys.stderr)
    fresh = index.update(idx, paths)
    if fresh != idx:
        index.save(fresh, index_path())
    return fresh


def _ended_row(entry, now_iso):
    """An index entry in the shape the row renderers expect."""
    return {"sessionId": entry.get("sessionId", ""), "project": entry.get("project", "?"),
            "title": entry.get("title", ""), "tab_title": "", "name": "",
            "attention": "ended", "status": "ended", "tty": "", "pid": None,
            "cwd": entry.get("cwd", ""), "topic": entry.get("you_said", ""),
            "first": entry.get("opened", ""), "ask": "", "doing": "",
            "orphans": 0, "work": 0, "ts": entry.get("last_ts", ""),
            "since": engine.brief.age_between(entry.get("last_ts", ""), now_iso),
            "age": "", "waitingFor": ""}


def matches(query, rows, everything=False):
    """Every session matching, live ones as live rows and the rest as ended ones.

    Two sources with different strengths: the live table knows what is running,
    the index knows what each session was ABOUT (including recaps from the middle
    of a long transcript) and matches word by word rather than as one phrase. A
    session found through the index that is running is still running - reporting
    it as ended because of where the match came from is a lie about the world.
    """
    # a parked terminal (ctrl+b) has no row: the job it shows answers for it
    by_id = {sid: r for r in rows for sid in engine.answers_for(r)}
    hits = list(engine.match_rows(rows, query))
    seen = {sid for r in hits for sid in engine.answers_for(r)}
    now = engine.now_iso()
    ended = []
    for entry in index.search(fresh_index(), query, live_ids=set(by_id),
                              everything=everything):
        sid = entry.get("sessionId")
        if sid in seen:
            continue
        seen.add(sid)
        if sid in by_id:
            if by_id[sid] not in hits:
                hits.append(by_id[sid])      # running, found by what it is about
        else:
            ended.append(_ended_row(entry, now))
    hits.sort(key=engine.sort_key)
    return hits, ended


def ls(argv):
    """The table. With words, the sessions that match - including ended ones."""
    query = " ".join(a for a in argv if not a.startswith("-"))
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and "--no-color" not in argv
    rows, fleet = scan(cache={})
    if not query:
        sys.stdout.write(engine.render(rows, with_usage(rows, fleet), color=color))
        return 0
    live, ended = matches(query, rows, everything="--all" in argv)
    if not live and not ended:
        print(f"ccwho: no session matches {query!r}", file=sys.stderr)
        return 1
    if live:
        # the fleet too: without it the filtered table lost its ports, and now
        # its usage and account tags
        sys.stdout.write(engine.render(live, with_usage(live, fleet, record=False),
                                       color=color))
    if ended:
        print(f"\n{DIM if color else ''}ended sessions{RESET if color else ''}")
        for r in ended[:10]:
            print(f"  {engine.brief.short_id(r['sessionId']):<6} {r['project'][:12]:<12}"
                  f" {engine.truncate(r['title'] or r['topic'], 44):<44}"
                  f" ended {r['since']} ago")
    return 0


def doctor(argv):
    """What is installed, what drifted, and what to run about it.

    Read-only by design. The failure this exists for - iTerm2's own status hook
    vanishing from settings.json - was silent for weeks, and a tool that repairs
    another tool's config without asking is how that happened in the first place.
    """
    facts = setup.gather(ccwho_dir=ccwho_dir())
    facts.update(usage_facts())
    checks = setup.doctor_checks(facts)
    if "--json" in argv:
        print(json.dumps({"ok": setup.doctor_verdict(checks) == 0,
                          "checks": checks}, indent=2))
        return setup.doctor_verdict(checks)
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    for c in checks:
        mark = "ok  " if c["ok"] else "BAD "
        if color:
            mark = ("\033[32mok  \033[0m" if c["ok"] else "\033[33;1mBAD \033[0m")
        print(f"{mark}{c['name']:<20} {c['detail']}")
        if c["fix"]:
            print(f"    {DIM}fix:{RESET} {c['fix']}" if color else f"    fix: {c['fix']}")
    rc = setup.doctor_verdict(checks)
    print("\neverything ccwho depends on is in place." if rc == 0
          else "\nrun the fixes above, then `ccwho doctor` again.")
    return rc


# Drift happens over weeks, not seconds, and each check is a subprocess. A watch
# that re-ran them every tick would spend more time diagnosing than listing.
DOCTOR_TTL = 300.0


def doctor_banner_cached(state, now=None):
    """One line for the watch header when something drifted, at most every
    DOCTOR_TTL. Empty when everything ccwho needs is in place."""
    now = time.time() if now is None else now
    ts, line = state.get("_doctor", (None, ""))
    if ts is None or now - ts >= DOCTOR_TTL:
        facts = setup.gather(ccwho_dir=ccwho_dir())
        facts.update(usage_facts(now))
        line = setup.doctor_banner(setup.doctor_checks(facts))
        state["_doctor"] = (now, line)
    return line


KEEP_LOG_LINES = int(os.environ.get("CCWHO_KEEP_LOG_LINES", "1000"))
LOG_TRIM_EVERY = 86400.0        # once a day: 96 runs a day need not each rewrite it


def autosave_log():
    """Where the launchd job's stdout lands. Nothing else writes here."""
    return os.path.join(ccwho_dir(), "autosave.log")


def log_trim_marker():
    return os.path.join(ccwho_dir(), ".log-trimmed")


def trim_log(path, keep=KEEP_LOG_LINES):
    """Keep the newest `keep` lines. Bounded the same way the manifests are.

    keep<=0 bounds NOTHING, exactly as prune_manifests does - fail safe, never
    "empty it". The rewrite is a temp + os.replace, so a log read while this runs
    is either the old file or the new one and never a half of each.
    """
    if keep <= 0:
        return
    try:
        with open(path) as fh:
            lines = fh.readlines()
    except OSError:
        return                    # no log yet, or unreadable: nothing to bound
    if len(lines) <= keep:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as fh:
            fh.writelines(lines[-keep:])
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def maybe_trim_log(keep=KEEP_LOG_LINES):
    """Trim at most once a day. True when this call did the pass.

    Runs AFTER the save that wrote to the log, never before: os.replace leaves the
    caller's already-open stdout pointing at the replaced inode, so anything still
    to be printed would go nowhere.
    """
    marker = log_trim_marker()
    try:
        last = os.path.getmtime(marker)
    except OSError:
        last = None
    if not should_autosave(last, time.time(), LOG_TRIM_EVERY):
        return False
    trim_log(autosave_log(), keep=keep)
    try:
        os.makedirs(ccwho_dir(), exist_ok=True)
        with open(marker, "w") as fh:
            fh.write("")
    except OSError:
        pass                      # housekeeping never fails the run it follows
    return True


def manifest_names():
    """Saved manifests, oldest first. Missing directory reads as none, never raises."""
    try:
        return sorted(n for n in os.listdir(restore_dir())
                      if engine._MANIFEST_NAME.match(n))
    except OSError:
        return []


def known_entries():
    """Every session any saved manifest remembers, the newest record of each winning.

    Not just the newest manifest: a link is clicked from whatever list is on
    screen, and the point of keeping 20 is that you read the older ones. A later
    record wins because it holds the cwd that is still true. Unreadable files are
    skipped, never raised - a click must not fail at the user.
    """
    seen, d = {}, restore_dir()
    for n in reversed(manifest_names()):
        try:
            with open(os.path.join(d, n)) as fh:
                man = json.load(fh)
        except (OSError, ValueError):
            continue
        for e_ in engine.manifest_entries(man):
            sid = e_.get("sessionId")
            if sid and sid not in seen:
                seen[sid] = e_
    return list(seen.values())


LAUNCH_CLAIM_SECONDS = 90.0       # long enough for a session to appear in the feed
# An unresolved launch dated further ahead than this was written while the
# clock ran fast: _dated dates it now, and a scan that sees it running turns
# it into a sighting (_see). Records stamped in this boot are read on the
# clock now (_read_claim) - a step since moves none of them.
CLOCK_STEP_SECONDS = 3600.0
# A sighting or launch dated further ahead than this is none ccwho wrote. One
# dated less far ahead was written before the clock was set back: it keeps
# its times, and holds until the clock catches up - never re-dated, which
# would let a decision older than it through. Nor is any record - an
# unresolved launch too - stamped in this boot with a monotonic time or an
# uptime this far ahead of those clocks, which never step (_read_claim).
FAR_FUTURE_SECONDS = 86400.0
SIGHTING_SECONDS = 600.0          # longer than any caller takes from its scan to its claim
# A decision given on the wall clock alone (no decided_mono - no caller in
# use) stamped later than now by more than this was made before the clock
# was set back: refused. A small step (a time sync's) is let pass.
CLOCK_SLACK_SECONDS = 1.0


def _pid_alive(pid):
    """Is there a process with this pid? Only "no such process" (ESRCH) says
    it is gone: one of another user's (EPERM) is there, and a probe refused
    for another reason (a sandbox's) tells nothing - it holds, and the table
    decides (_iterm_holds). What is no pid (_read_claim's rule) is no
    process."""
    if type(pid) is not int or not 0 < pid < 2 ** 31:
        return False
    try:
        os.kill(pid, 0)
    except OSError as ex:
        return ex.errno != errno.ESRCH
    return True


_boot_id = engine.boot_id


def _mono():
    """Seconds on this boot's monotonic clock: the same in every process, and
    counting sleep - never set back, nor forward, as the wall clock can be."""
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def _uptime():
    """Seconds this boot has been awake: the same in every process; sleep does
    not count."""
    return time.clock_gettime(time.CLOCK_UPTIME_RAW)


def _now():
    """The wall clock now, on this judgement's reading of it (_skew): inside
    a claims-lock section, the one taken when the lock was had - every
    record read there is put on it (_read_claim), so a step while the section
    runs moves nothing it writes, reads or compares; outside one, the wall
    clock."""
    return _mono() + _skew()


def _clock_now():
    """(the wall clock now - _now() -, "clock": [boot, monotonic, uptime])
    read at one instant - the boot id first, its first call is slow. What a
    record whose time is "now" is written with, in the lock it is written
    under (_mark, _take, _see, _dated): a step or a sleep while its writer
    waited for the lock, or while it holds it, can then neither age it nor
    date it back or ahead."""
    boot = _boot_id()
    mono, up = _mono(), _uptime()
    return mono + _skew(), [boot, mono, up]


def _stamped(record):
    """The record with its "clock": [boot, monotonic, uptime] at its main time
    - a sighting's until, else its since - kept when it has one. By it a
    reader puts the record's times on the wall clock it reads by (_read_claim)
    and ages it by time awake (_age): a clock set back or forward since, or a
    sleep, moves neither its order against a decision nor its hold."""
    if "clock" in record:
        return record
    # one written with a time of its own (a test's; an older record): its
    # clocks as far back as that time is from the wall clock now
    wall, (boot, mono, up) = _clock_now()
    ago = wall - float(record["until"] if record.get("seen") is True else record["since"])
    return dict(record, clock=[boot, mono - ago, up - ago])


def _skew():
    """The wall clock less the monotonic one: what turns a monotonic time into
    a wall clock time now. One reading for a whole locked judgement
    (_claims_locked): a decision and the records it is judged against are
    put on the same clock, whatever the wall clock does meanwhile - and so
    is "now" (_now, _clock_now). What a section hands to the next is on the
    monotonic clock (_take, _tables)."""
    frozen = _LOCAL.__dict__.get("skew")
    return frozen if frozen is not None else time.time() - _mono()


def _this_boot(rec):
    """The record's clock, when it was stamped in this boot - else None."""
    c = rec.get("clock")
    return c if c is not None and c[0] is not None and c[0] == _boot_id() else None


def _age(rec, now, lead=None):
    """Seconds since a claim's since, awake - by uptime in the boot it was
    made in (a Mac asleep does not age a launch whose sessions start only
    when it wakes; no wall clock step moves it), else by the wall clock.
    `lead`: how far a test's `now` runs ahead - none in use."""
    c = _this_boot(rec)
    if c is not None:
        return _uptime() + (lead or 0.0) - c[2]
    return now - float(rec["since"])


_LOCAL = threading.local()


def _unrecorded():
    """session id -> why this thread could not record a launch of it: what the
    claim dir or its lock said, why its send lock could not be had (removed
    or replaced as it was made: try again), or None (a write that failed).
    Written by claim_launch (its lock, or _take's write), by _mark (its lock)
    and by launch_in_iterm (_hold_send's refusal); a take that succeeds
    removes it. launch_in_iterm refuses a launch of any of them at once, and
    says why (_not_recorded)."""
    return _LOCAL.__dict__.setdefault("unrecorded", {})


def _said(ex):
    """An OSError in words: what, and the file."""
    if ex.strerror and ex.filename:
        return f"{ex.strerror}: {ex.filename}"
    return ex.strerror or str(ex)


def _tokens():
    """session id -> the token of the claim this thread's last claim_launch
    made on it. A claim is owned by the call that made it: not by its
    process or thread - an earlier launch of the same thread is not this
    one's to rewrite and send again."""
    return _LOCAL.__dict__.setdefault("tokens", {})


def _refusals():
    """session id -> why this thread's last claim_launch on it was refused,
    from the same locked read that refused it (why_held)."""
    return _LOCAL.__dict__.setdefault("refusals", {})


def _claim_path(session_id):
    return os.path.join(ccwho_dir(), "launching", f"{session_id}.json")


def _send_path(token):
    return os.path.join(ccwho_dir(), "launching", f"{token}.send")


def _claims_dir():
    """launching/, made if need be. It and the ccwho dir above it are this
    user's own, and no one else's to write in: another user could remove a
    send lock's file mid-send, or move launching/ away with every claim in
    it. One writable by others is made private - through the dir opened,
    never through a name that could have been swapped for a link since.
    launching/ itself is never a link (a root-run ccwho writes and removes
    files in it); the ccwho dir may be one of the user's own."""
    top = ccwho_dir()
    os.makedirs(top, mode=0o700, exist_ok=True)
    _own_private_dir(top, os.O_RDONLY)
    d = os.path.join(top, "launching")
    with contextlib.suppress(FileExistsError):
        os.mkdir(d, 0o700)
    _own_private_dir(d, os.O_RDONLY | os.O_NOFOLLOW)
    return d


def _own_private_dir(path, flags):
    try:
        fd = os.open(path, flags | os.O_DIRECTORY)
    except OSError as ex:
        if ex.errno in (errno.ELOOP, errno.ENOTDIR):
            raise OSError(f"{path} is a link, or no dir - it must be a dir of yours, not a link")
        raise
    try:
        st = os.fstat(fd)
        if st.st_uid != os.geteuid():
            raise OSError(f"{path} is not a dir of this user's own")
        if st.st_mode & 0o022:
            os.fchmod(fd, stat.S_IMODE(st.st_mode) & 0o700)
    finally:
        os.close(fd)


CLAIMS_LOCK_SECONDS = 5.0     # a launch waits this long for the claims lock,
GAVE_UP_SECONDS = 60.0        # and this long after one gave up, its thread tries once


@contextlib.contextmanager
def _claims_locked(wait=None):
    """One ccwho at a time reads, judges and rewrites claims: two that both
    judged a stale claim free both took it (a double click), and a sighting
    could remove a claim made again since it was read. Every scan and every
    launch takes it, so nothing slow - no ps, no send - runs under it. A
    holder can still be stopped (Ctrl-Z) inside: a scan does not wait
    (wait=0 - it releases what it saw next time); a launch waits
    CLAIMS_LOCK_SECONDS, and for GAVE_UP_SECONDS after one of its thread gave
    up, tries once - until one is had: the rest of a batch does not wait the
    whole time again, and a click a minute on waits in full. The
    lock file is this user's own, and its only name - not one another user
    made, or links to, while the dir was open to them. Raises OSError when the
    lock cannot be had."""
    launch = wait is None
    if launch:
        gave_up = _LOCAL.__dict__.get("lock_gave_up")
        wait = 0 if gave_up is not None and time.monotonic() - gave_up < GAVE_UP_SECONDS \
            else CLAIMS_LOCK_SECONDS
    path = os.path.join(_claims_dir(), ".lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_nlink != 1:
            raise OSError(f"{path} is not a lock of this user's own - remove it")
        # its own wait, not the engine's: the list reloads the engine, never
        # this file - the guard's lock must not change under a running one
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:     # another holds it
                if time.monotonic() >= end:
                    if launch and wait:
                        _LOCAL.lock_gave_up = time.monotonic()
                    raise BlockingIOError(errno.EWOULDBLOCK, "another ccwho holds the claims lock"
                                          " - a stopped job? (resume or quit it)")
                time.sleep(0.02)
            except OSError as ex:       # no other holder: no lock can be had here at all
                raise OSError(ex.errno, ex.strerror, path) from ex
        _LOCAL.lock_gave_up = None
        _LOCAL.skew = time.time() - _mono()     # one reading of the clocks for this judgement
        try:
            yield
        finally:
            _LOCAL.skew = None
    finally:
        os.close(fd)


# a send lock's name: what a claim may name, and nothing else
_SEND_TOKEN = re.compile(r"[0-9a-f]{32}")


def _above_std(fd):
    """fd as a descriptor above stdin, stdout and stderr (the original
    closed): a ccwho started with one of them closed gets that number from
    os.open, and a child it passes a lock to (pass_fds) has those replaced -
    its pipes, its DEVNULL - before it runs: that child would hold no lock."""
    if fd > 2:
        return fd
    try:
        return fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, 3)
    finally:
        os.close(fd)


def _hold_send():
    """A launch's send lock: a file of its own - a new name, made exclusively,
    never through a link - locked from before its claims are marked
    unresolved (naming it) until what the send did is recorded. "Being sent"
    is exactly that lock held (_sending): it dies with its holders - the
    launcher and the osascript it runs - and outlasts a launcher stopped
    mid-send (Ctrl-Z); no clock says how long a send may take. It is made
    new and locked before any claim names it - and refused when, locked, its
    name no longer names it (removed before the lock was had) - and others
    only probe it without waiting (_sending; the sweep, which removes it only
    when no one holds it): no removal of it can hide a send. Returns (token,
    fd); raises OSError when it cannot be made."""
    token = uuid.uuid4().hex
    path = os.path.join(_claims_dir(), f"{token}.send")
    fd = _above_std(os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            same = os.path.samestat(os.fstat(fd), os.lstat(path))
        except FileNotFoundError:
            same = False                        # removed before the lock was had
        if not same:
            raise OSError(errno.EAGAIN, "its send lock was removed or replaced as it was made"
                          " - try again", path)
    except OSError:
        os.close(fd)
        raise
    return token, fd


def _let_go_send(held):
    """The send is recorded: the lock is let go, and its file goes when no
    one holds it then (_sweep_send) - a process that inherited it and still
    runs keeps it named, and every claim naming it held; the sweep removes
    it once free."""
    token, fd = held
    with contextlib.suppress(OSError):
        os.close(fd)
    with contextlib.suppress(OSError, ValueError):
        _sweep_send(_send_path(token))


def _open_in_claims(path):
    """A file in the claim dir, opened for reading under the claims lock
    every scan and launch takes: never through a link, never blocking - a
    FIFO planted at the name does not hang ccwho. Raises ValueError when it
    is no regular file of this user's own (another user's, planted while the
    dir was open to them, is no claim ccwho wrote), OSError when it cannot be
    opened."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as ex:
        if ex.errno == errno.ELOOP:
            raise ValueError("a link")
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a file")
        if st.st_uid != os.geteuid():
            raise ValueError("not a file of this user's own")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _sending(rec):
    """Is the launch this claim names being sent - its send lock held? A lock
    file that is there and cannot be read, or is no file, holds: the guard
    fails closed. Gone: the send is over."""
    token = rec.get("send")
    if token is None:
        return False
    try:
        fd = _open_in_claims(_send_path(token))
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def _read_claim(session_id):
    """The record on disk. Raises FileNotFoundError when there is none - any
    other OSError is a file that is there and cannot be read, which callers
    hold - and ValueError when it is not one ccwho wrote: no dict, no time,
    times it never sets (not finite; an until before its since; a sighting
    ending, or any claim but an unresolved launch starting, more than
    FAR_FUTURE_SECONDS ahead of the clock; a stamp of this boot's - on any
    record - that far ahead of its clocks), a pid no process can have. Such
    a record would crash every scan, or hold a session for good, past an
    iTerm2 restart and a reboot. A record stamped in this boot (_stamped)
    comes back with its times on the wall clock now (_now). An unresolved
    launch dated ahead of the wall clock - one not stamped in this boot - is
    kept: _dated judges it as of the moment it is first read. A link, a FIFO
    - no regular file - is no claim ccwho wrote either (_open_in_claims):
    never followed, never waited on."""
    with os.fdopen(_open_in_claims(_claim_path(session_id))) as fh:
        try:
            rec = json.load(fh)
        except RecursionError:
            raise ValueError("not a claim")
    try:
        if not isinstance(rec, dict) or any(type(rec.get(k, 0)) not in (int, float)
                                            for k in ("since", "until")) or "since" not in rec:
            raise ValueError("not a claim")
        since, until = float(rec["since"]), float(rec.get("until", 0))
    except OverflowError:
        raise ValueError("not a claim")
    clock = rec.get("clock")
    if clock is not None:
        try:
            if not (type(clock) is list and len(clock) == 3
                    and (clock[0] is None or type(clock[0]) is str)
                    and all(type(v) in (int, float) and math.isfinite(float(v)) for v in clock[1:])):
                raise ValueError("not a clock")
        except OverflowError:
            raise ValueError("not a clock")
    if _this_boot(rec) is not None and (float(clock[1]) > _mono() + FAR_FUTURE_SECONDS
                                        or float(clock[2]) > _uptime() + FAR_FUTURE_SECONDS):
        raise ValueError("a clock ahead of this boot's")    # it would never age
    if math.isfinite(since) and math.isfinite(until) and _this_boot(rec) is not None:
        # on the wall clock now: moved by any step the clock took since
        shift = (clock[1] + _skew()) - (until if rec.get("seen") is True else since)
        since, until = since + shift, until + shift
        rec["since"] = since
        if "until" in rec:
            rec["until"] = until
    far = _now() + FAR_FUTURE_SECONDS
    bad = ((rec.get("seen") is True and until > far)
           or ("until" in rec and until < since)
           or (since > far and rec.get("unresolved") is not True))
    if not (math.isfinite(since) and math.isfinite(until)) or bad:
        raise ValueError("not a claim ccwho wrote")
    iterm = rec.get("iterm_pid")
    for pid in [rec.get("pid")] + (iterm if type(iterm) is list and iterm else [iterm]):
        if pid is not None and not (type(pid) is int and 0 < pid < 2 ** 31):
            raise ValueError("not a pid")
    send = rec.get("send")
    if send is not None and not (type(send) is str and _SEND_TOKEN.fullmatch(send)):
        raise ValueError("not a send lock")
    return rec


def _write_claim(session_id, record):
    """Written whole - a new temp file of its own (made exclusively, never
    through a link planted at its name), then renamed: a reader never sees it
    half done. The caller holds _claims_locked()."""
    path = _claim_path(session_id)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=f"{session_id}.json.",
                                   suffix=".tmp")
    except OSError:
        return False
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(_stamped(record), fh)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return False
    return True


def _dated(session_id, rec):
    """An unresolved launch is judged as of the moment it is first seen to be
    able to run no more than it already has, and saved so. One naming a send
    lock no one holds any more (its launcher died before it could record what
    the send did) is dated now - with every other claim of that send
    (_redate_send), so a batch of them takes one table after it, not one
    each; so is one dated more than a clock step ahead (written while the
    clock ran fast). Only a table taken after that can bind it or call its
    iTerm2 gone - never one taken while it was being sent, before its
    osascript had started iTerm2. A sighting or a launch keeps its times
    (FAR_FUTURE_SECONDS). The caller holds the lock."""
    at, clock = _clock_now()
    if rec.get("unresolved") is not True:
        return rec
    if rec.get("send") is not None and not _sending(rec):
        _redate_send(rec["send"], at, clock)
        rec = dict({k: v for k, v in rec.items() if k not in ("until", "send")}, since=at, clock=clock)
        _write_claim(session_id, rec)
    elif float(rec["since"]) > at + CLOCK_STEP_SECONDS:
        # a send still under way keeps its lock named: it holds by that
        rec = dict({k: v for k, v in rec.items() if k != "until"}, since=at, clock=clock)
        _write_claim(session_id, rec)
    return rec


def _redate_send(token, at, clock):
    """Every unresolved claim naming this send lock, dated `at` - its send is
    over. Under the lock."""
    try:
        entries = list(os.scandir(os.path.join(ccwho_dir(), "launching")))
    except OSError:
        return
    for e in entries:
        m = _CLAIM_FILE.fullmatch(e.name)
        if not m or m.group(2) != "json" or not engine._SESSION_ID.match(m.group(1)):
            continue
        try:
            rec = _read_claim(m.group(1))
        except (OSError, ValueError):
            continue
        if rec.get("unresolved") is True and rec.get("send") == token:
            _write_claim(m.group(1), dict({k: v for k, v in rec.items() if k not in ("until", "send")},
                                          since=at, clock=clock))


def _mine(session_id):
    """Is the claim on it the one this call's claim_launch made (_tokens)?"""
    token = _tokens().get(session_id)
    try:
        rec = _read_claim(session_id)
    except (OSError, ValueError):
        return False
    return token is not None and rec.get("owner") == token and rec.get("seen") is not True


def _theirs(session_id):
    """Is it held against this call - a sighting of the session running, or
    another ccwho's claim that holds (or may)? It cannot tell a record left
    in place by a write of ours that failed: launch_in_iterm refuses a launch
    in _unrecorded() before it asks."""
    try:
        rec = _read_claim(session_id)
        if rec.get("seen") is True:
            return True
        verdict = _held(rec, _now(), _pid_alive, None, None, None)
    except (OSError, ValueError, TypeError):
        return False
    return rec.get("owner") != _tokens().get(session_id) and (verdict is None or verdict[0])


def _mark(session_ids, record):
    """Rewrite our own claims on these sessions as `record`. False when one
    could not be written, or is not ours any more: a scan saw it running, or
    another ccwho took it over."""
    ok = True
    try:
        with _claims_locked():
            if record.get("since") is None:     # "now": read here, with its clocks
                wall, clock = _clock_now()
                record = dict(record, since=wall, clock=clock)
            for sid in session_ids:
                ok = _mine(sid) and _write_claim(sid, dict(record, sessionId=sid,
                                                           owner=_tokens()[sid])) and ok
    except OSError as ex:
        for sid in session_ids:
            _unrecorded()[sid] = _said(ex)      # launch_in_iterm refuses it, and says why
        return False
    return ok


def claim_unresolved(session_id, iterm_pid, now=None, send=None):
    """Mark our claim as a launch that may still run inside iTerm2 - a killed
    osascript does not cancel its event. Held while it is being sent (the
    send lock it names, `send`), then while the iTerm2 it went to runs -
    iterm_pid: None (not known yet: any of ours holds it), or the list of
    iTerm2s of ours it may have reached, any of which holds it
    (_iterm_holds) - until the session is seen running. False when it could
    not be written, or the claim is not ours any more (_mark)."""
    record = {"pid": os.getpid(), "since": now, "unresolved": True,
              "iterm_pid": iterm_pid, "boot": _boot_id()}
    return _mark([session_id], dict(record, send=send) if send else record)


def claim_launched(session_ids, cut_off=False):
    """iTerm2 ran the launch: held until the session is seen running, or for
    LAUNCH_CLAIM_SECONDS - a session takes a moment to appear in `claude
    agents`, and the ccwho a click started has exited by then. `cut_off`:
    iTerm2 quit during it - what it opened may start, for the same time."""
    record = {"pid": os.getpid(), "since": None, "launched": True}      # dated in _mark
    return _mark(session_ids, dict(record, cut_off=True) if cut_off else record)


def _reason(rec, now, decided_at, lead=None):
    """Why a claim that holds holds, for the one who asked (why_held)."""
    since = float(rec["since"])
    if rec.get("seen") is True:
        return "newer", 0
    if rec.get("unresolved") is True:
        if not _sending(rec):
            return "unresolved", 0
        # its launcher gone, its osascript still holds the lock
        return ("starting", 0) if _pid_alive(rec.get("pid")) else ("sending", 0)
    if rec.get("launched") is True:
        left = math.ceil(LAUNCH_CLAIM_SECONDS - _age(rec, now, lead))
        if rec.get("cut_off") is True and left > 0:
            return "cut off", left
        if decided_at is not None and decided_at < since:
            return "newer", 0
    return "starting", 0


def why_held(session_id):
    """Why this thread's last claim_launch on it said no: (reason, seconds
    left), recorded by the read that said no (_refusals). "old list": decided
    on a scan older than any sighting kept (by the monotonic clock; a
    decision given on the wall clock alone also when stamped more than
    CLOCK_SLACK_SECONDS after it); "unreadable": a claim file there
    that cannot be read; "newer": launched or seen running after the list was
    read - it has changed; "unresolved": an earlier launch did not finish and
    may still run; "cut off": iTerm2 quit during an earlier launch; else
    "starting"."""
    return _refusals().pop(session_id, ("starting", 0))


def drop_claims(session_ids):
    """None of the launch ran: let go of our own claims. By removing them -
    a rollback after a full disk must not need the space it lacks. It waits
    for the lock in full, whatever a wait before it gave up: a claim of a
    launch never sent, left behind, holds the session for nothing. With no
    claim of this call among them, it takes no lock."""
    if not any(sid in _tokens() for sid in session_ids):
        return
    with contextlib.suppress(OSError), _claims_locked(wait=CLAIMS_LOCK_SECONDS):
        for sid in session_ids:
            if _mine(sid):
                with contextlib.suppress(OSError):
                    os.remove(_claim_path(sid))


def release_claims(session_ids, seen_at, seen_mono=None):
    """Sessions seen running: whatever launched them is done - their claims go.
    Only claims older than the sighting: `claude agents` can take 30 s, and a
    claim made since may be a new launch of a session that has ended.

    A claim goes to a sighting, not to nothing: a ccwho that read "not
    running" before it, and decides only now, must not find the claim free
    (claim_launch's decided_at). Its `until` is when this scan ended: scans
    overlap, and one that started earlier may have read the session later
    than another's "not running". A sighting goes SIGHTING_SECONDS after its
    scan ended; a record no ccwho wrote goes at once. The sweep runs after:
    what this scan saw has its sighting by then. A claims lock that is busy
    defers both to the next scan. A start on the monotonic clock after now
    is none a scan gives (scan() reads it first): nothing is released or
    swept on it."""
    if seen_mono is not None and seen_mono > _mono():
        return
    d = os.path.join(ccwho_dir(), "launching")
    try:
        names = set(os.listdir(d))
    except OSError:
        return                                  # no claims at all
    sids = [sid for sid in session_ids
            if sid and engine._SESSION_ID.match(str(sid)) and f"{sid}.json" in names]
    if sids:
        try:
            with _claims_locked(wait=0):
                at = seen_at if seen_mono is None else seen_mono + _skew()   # the scan's start
                for sid in sids:
                    _see(sid, at)
        except OSError:
            return          # busy: no sightings, so no sweep either - next scan
    _sweep(d, seen_at, seen_mono)


def _see(sid, seen_at):
    """release_claims for one session seen running, under the lock: a claim
    older than the scan becomes a sighting (not written - a full disk - it
    stays, or an older decision would take the session); so does an
    unresolved launch dated further ahead than any clock step (written while
    the clock ran fast). Any other record dated after the scan stays as it
    is - one from before the clock was set back keeps refusing what is older
    than it. An old sighting, or a record no ccwho wrote, goes. A file there
    that cannot be read is dated by its own time; when not even that can be
    read, it stays."""
    path = _claim_path(sid)
    try:
        rec = _read_claim(sid)
    except ValueError:
        rec = None
    except FileNotFoundError:
        return
    except OSError:
        try:
            rec = {"since": os.lstat(path).st_mtime}
        except OSError:
            return
    if rec is None or (rec.get("seen") is True
                       and float(rec.get("until", rec["since"])) <= seen_at - SIGHTING_SECONDS):
        with contextlib.suppress(OSError):
            os.remove(path)
    elif rec.get("seen") is not True and (
            float(rec.get("since", 0)) < seen_at
            or (rec.get("unresolved") is True and float(rec["since"]) > _now() + CLOCK_STEP_SECONDS)):
        wall, clock = _clock_now()
        sighting = {"sessionId": sid, "seen": True, "since": seen_at, "until": max(seen_at, wall)}
        _write_claim(sid, dict(sighting, clock=clock) if sighting["until"] == wall else sighting)


# <session id>.json, its temp files, and a launch's send lock (<token>.send)
_CLAIM_FILE = re.compile(r"(.+)\.(json(?:\.\w+\.tmp)?|send)")


def _sweep(d, now, now_mono=None):
    """Remove what no scan will read again: sightings and claims past their
    time whose session is not seen any more - each such launch would leave a
    file for good, in a dir every scan lists. Once per SIGHTING_SECONDS, by a
    marker's time, across processes; only files that old are read (_recent).
    A time further ahead of the clock now than that - a marker or file
    written while the clock ran ahead, set right since - is none: the sweep
    runs, and judges the file by its own times. A launch
    that may still run stays - unless it cannot hold without a ps: made in
    another boot, or on an iTerm2 that is gone. A send lock goes when no one
    holds it. Only files ccwho writes (_CLAIM_FILE: a session id's claim and
    its temp files, a send lock's token), only regular ones - never through
    a symlink; one entry it cannot read does not stop the rest."""
    if now_mono is not None and now_mono > _mono():
        return                          # a start after now is none a scan gives
    mark = os.path.join(d, ".swept")
    try:
        if _recent(os.stat(mark).st_mtime, now):
            return
    except OSError:
        pass
    with contextlib.suppress(OSError), _claims_locked(wait=0):
        if now_mono is not None:
            now = now_mono + _skew()                    # the scan's start, on the clock now
        with contextlib.suppress(OSError):
            _mark_swept(d, mark, now)
        for e in os.scandir(d):
            m = _CLAIM_FILE.fullmatch(e.name)
            ok = _SEND_TOKEN.fullmatch if m and m.group(2) == "send" else engine._SESSION_ID.match
            if not m or not ok(m.group(1)):
                continue                         # not a file ccwho writes
            try:
                st = e.stat(follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode) or _recent(st.st_mtime, now):
                    continue
                if m.group(2) == "send":
                    _sweep_send(e.path)
                    continue
                if m.group(2) == "json":
                    try:
                        rec = _read_claim(m.group(1))
                        # its own times, not its file's: a clock stepped
                        # forward ages a file it did not age
                        if float(rec.get("until", rec["since"])) > now - SIGHTING_SECONDS:
                            continue
                        # what the judge still holds - a launch held for its
                        # time awake too - or cannot tell about, stays
                        verdict = _held(rec, now, _pid_alive, None, None, None)
                        if verdict is None or verdict[0]:
                            continue
                    except ValueError:
                        pass                     # no claim ccwho wrote: goes too
                os.remove(e.path)                # a sighting, an old claim, a leftover
            except (OSError, ValueError):
                continue


def _recent(mtime, now):
    """Is a file's time one the sweep leaves: from SIGHTING_SECONDS before
    `now` - its scan's start - to SIGHTING_SECONDS past the clock NOW (_now),
    or past `now` when it is later (a start given on the wall clock alone,
    taken at its word - its marker is dated by it)? A scan can take longer
    than that (the Mac asleep while it ran): a file made since it started -
    a send lock not yet locked - is recent, never "ahead". Only one dated
    further ahead was written while the clock ran ahead, set right since."""
    return now - SIGHTING_SECONDS < mtime <= max(now, _now()) + SIGHTING_SECONDS


def _mark_swept(d, mark, now):
    """The sweep's marker, replaced - never opened (one left unopenable by a
    root-run ccwho does not stop it), never through a link planted at a
    name. A temp file that could not be put in place goes: every scan would
    leave one."""
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".swept.", suffix=".tmp")
    os.close(fd)
    try:
        os.utime(tmp, (now, now))
        os.replace(tmp, mark)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _sweep_send(path):
    """A send lock's file goes when no one holds it - removed while THIS
    caller holds its flock, whatever its age: the sweep's (an old one), and
    _let_go_send's (the launch's own, right after it let go). One held, or
    that cannot be opened as a regular file of ours, stays."""
    fd = _open_in_claims(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.unlink(path)
    finally:
        os.close(fd)


def _held(rec, now, alive, table, taken, decided_at, lead=None):
    """Does this claim still hold? (held, the record to keep in its place), or
    None when that cannot be told on `table` (taken at `taken`; None: no
    table) - it may be older than the iTerm2 it would have to show.

    A sighting: against a decision made before its scan ended. Claimed and
    not yet sent: while its launcher lives, for the usual time. Launched: for
    the usual time - until seen running - and against any decision made
    before it. Unresolved: never when made before this Mac started (its
    iTerm2, and the queue in it, went with that); else while it is being
    sent (its send lock), then _iterm_holds."""
    since = float(rec.get("since", 0))
    if rec.get("seen") is True:
        return (decided_at is not None and decided_at < float(rec.get("until", since))), rec
    young = _age(rec, now, lead) < LAUNCH_CLAIM_SECONDS
    if rec.get("launched") is True:
        return young or (decided_at is not None and decided_at < since), rec
    if rec.get("unresolved") is not True:
        return alive(rec.get("pid")) and young, rec
    if rec.get("boot") and _boot_id() and rec["boot"] != _boot_id():
        return False, rec
    if _sending(rec):
        return True, rec
    return _iterm_holds(rec, alive, table, taken)


def _iterm_holds(rec, alive, table, taken):
    """An unresolved launch no longer being sent: its event may still be
    queued in the iTerm2 it went to, so the claim holds while THAT iTerm2 runs
    - by its own entry in the table (our uid, iTerm2's executable), not a
    process that reused its pid. iterm_pid None is what every launch records
    before _sent names the iTerm2 (and what stays if it could not): any
    iTerm2 of ours holds it, and it is bound to the list of iTerm2s of ours a
    table taken after the claim shows - one or more, as with two the one the
    event went to is not known: it holds while any of them runs, and
    restarting them all clears it. (A bare pid is read too: records written
    before lists.) A ps that could not be read is "could not
    tell", and holds; binding, and "gone", happen only on a table taken after
    the claim's since - which is after its send was over (_sent, _dated).
    (Owner, 2026-09-29: a fork is worse than a block you can clear by
    restarting iTerm2 - no timing rule releases it.)"""
    iterm = rec.get("iterm_pid")
    pids = None if iterm is None else ([iterm] if type(iterm) is int else iterm)
    if pids is not None and not any(alive(p) for p in pids):
        return False, rec
    if table is None:
        return None
    if not engine.parse_procs(table):
        return True, rec
    # a table taken after the claim was written: only such a one can bind it
    # or call its iTerm2 gone - an older one may predate that iTerm2. (Not
    # after now: _tables' time never is; a (table, taken) given on the wall
    # clock alone - tests' - dated ahead is not trusted.)
    fresh = taken is not None and float(rec.get("since", 0)) < taken <= _now()
    if pids is None:
        found = engine.iterm_app_pids(table)
        if found:
            return True, (dict(rec, iterm_pid=found) if fresh else rec)
    elif set(pids) & set(engine.iterm_app_pids(table)):
        return True, rec
    return (False, rec) if fresh else None


def _take(session_id, pid, now, alive, table, taken, decided_at, decided_mono=None, lead=None,
          taken_mono=None):
    """claim_launch's judgement, under the lock: True taken, False held, or
    the claim's since on the monotonic clock (a float) when it needs a
    process table taken after that - handed out of the section, on no
    reading of the wall clock (_tables). Only a missing file (FileNotFoundError) or a record no ccwho wrote
    (ValueError) is no claim: a file there that cannot be read holds, and so
    does an error while judging one - the guard fails closed (the list
    reloads the engine, never this file). A decision older than
    SIGHTING_SECONDS is refused - judged here, by the clock after any wait
    for the lock, by the monotonic clock (decided_mono); a decision given on
    the wall clock alone is also refused when more than CLOCK_SLACK_SECONDS
    later than the clock now - set back since, its order against what was
    written since is unknown. Why it said no is kept for why_held."""
    wall = _now()
    if taken_mono is not None:
        taken = taken_mono + _skew()                  # the table's time, on the clock now
    if decided_mono is not None:
        # its age on the monotonic clock: no wall clock step makes it old
        old = _mono() + (lead or 0.0) - decided_mono > SIGHTING_SECONDS
        decided_at = decided_mono + _skew()           # the scan's start, on the clock now
    else:
        old = decided_at is not None and not (max(now, wall) - SIGHTING_SECONDS <= decided_at
                                              <= wall + CLOCK_SLACK_SECONDS)
    if old:
        _refusals()[session_id] = ("old list", 0)
        return False
    try:
        rec = _dated(session_id, _read_claim(session_id))
    except (FileNotFoundError, ValueError):
        rec = None                             # no claim, or one no ccwho wrote
    except OSError:
        _refusals()[session_id] = ("unreadable", 0)
        return False                           # there, and cannot be read: it holds
    try:
        verdict = (False, None) if rec is None else _held(rec, now, alive, table, taken, decided_at, lead)
    except Exception:
        verdict = (True, rec)
    if verdict is None:
        return float(rec["since"]) - _skew()    # on the monotonic clock
    held, keep = verdict
    if held:
        if keep is not rec:
            _write_claim(session_id, keep)
        try:
            _refusals()[session_id] = _reason(rec, now, decided_at, lead)
        except Exception:
            _refusals()[session_id] = ("starting", 0)
        return False
    token = uuid.uuid4().hex
    claim = {"pid": pid, "owner": token, "since": now, "sessionId": session_id}
    if lead is None:                    # now: read here, with its clocks
        at, stamp = _clock_now()
        claim.update(since=at, clock=stamp)
    if _write_claim(session_id, claim):
        _unrecorded().pop(session_id, None)
        if pid == os.getpid():
            _tokens()[session_id] = token
        return True
    _tokens().pop(session_id, None)
    # not written (a full disk): launch_in_iterm will say so. What no longer
    # holds for anyone goes; a sighting or a launch still refuses decisions
    # older than it, and stays
    _unrecorded()[session_id] = None
    if rec is None or not (rec.get("seen") is True or rec.get("launched") is True):
        with contextlib.suppress(OSError):
            os.remove(_claim_path(session_id))
    return True


def _tables():
    """One process table for a batch of claims, taken when first needed:
    get(after) -> (table, when it was taken on the wall clock now, the same on
    the monotonic clock). One not taken after `after` - a claim's since, on
    the monotonic clock (_take) - is taken again, if one can be (`after` is
    not ahead of now): a claim is never judged on a table older than it
    (_iterm_holds). Both are compared on the monotonic clock: no wall clock
    step between the judgement and its table moves either."""
    cached = []

    def get(after=None):
        # a claim dated ahead of now can have no table after it yet: none taken for it
        if not cached or (after is not None and cached[0][1] <= after <= _mono()):
            mono = _mono()
            cached[:] = [(engine.app_snapshot(), mono)]
        table, mono = cached[0]
        return table, mono + _skew(), mono
    return get


def claim_launch(session_id, pid=None, now=None, alive=_pid_alive, decided_at=None, refresh=None,
                 decided_mono=None):
    """Claim the right to start THIS session, across processes. True unless
    another claim holds it.

    resolve_open() reads the world and then the caller launches. Another ccwho -
    a second click, a `restore --open` in another window - can read the same world
    in between and launch the same session, which is the fork the whole guard
    exists to prevent. A new session also takes a moment to appear in
    `claude agents --json`, so the window is real even for one hurried human.
    `decided_at` is when the caller's own scan started (scan()), and
    `decided_mono` the same on the monotonic clock (_mono) - which orders it
    against what was written since whatever the wall clock did: a sighting
    since then refuses it, and so does a scan older than SIGHTING_SECONDS.

    A file per session under $HOME, read, judged and replaced under one lock,
    no daemon. A claim that no longer holds (_held) or that no ccwho wrote is
    taken over - a crashed launcher must not lock a session out forever.
    A process table is taken when the judgement needs one, never under the
    lock - by `refresh(after)` (_tables), which a batch shares: a new one only
    for a claim newer than the table it has. True also when the lock cannot
    be had at all:
    launch_in_iterm then cannot record the launch, and does not send it.
    """
    lead = None if now is None else now - time.time()  # a test's now; None: the clocks now
    now = time.time() if now is None else now
    pid = os.getpid() if pid is None else pid
    _refusals().pop(session_id, None)
    refresh = refresh or _tables()
    table = taken = taken_mono = None
    for _ in range(2):
        try:
            with _claims_locked():
                got = _take(session_id, pid, now, alive, table, taken, decided_at, decided_mono, lead,
                            taken_mono)
        except OSError as ex:
            _unrecorded()[session_id] = _said(ex)   # launch_in_iterm will say so, and why
            _tokens().pop(session_id, None)     # and this call owns no claim
            return True
        if type(got) is bool:
            return got
        got = refresh(got)
        table, taken = got[0], got[1]
        # _tables' (table, taken, monotonic time), put on the clock under the
        # lock; a (table, taken) - tests' - is taken on the wall clock as it is
        taken_mono = got[2] if len(got) > 2 else None
    # its table could not tell either: it holds - an unresolved launch, the
    # only record a table is needed for
    _refusals()[session_id] = ("unresolved", 0)
    return False


def resume_problem(entry):
    """Why `claude --resume` would fail for this saved session, or "".

    Asked by everything that launches one - `open` and `restore --open` - right
    before it does. A saved cwd in a temp dir is gone after the next cleanup, and
    a headless worker leaves no transcript: both opened a window onto an error.
    """
    if not isinstance(entry.get("cwd", ""), str):
        return "its saved cwd is not a path"
    finder = engine.manifest_transcript_finder({"sessions": [entry]})
    return engine.entry_problem(entry, os.path.isdir, finder)


OLD_LIST = "the session list it read is too old to act on (the Mac slept?) - run it again"


def _why_text(reason, session_id, left):
    """Why a launch of a session was held, in words (why_held's reasons) -
    the same for open and restore."""
    return {
        "old list": OLD_LIST,
        "newer": "it was launched or seen running after the list was read - run it again",
        "unreadable": (f"its claim file cannot be read: {_claim_path(session_id)}."
                       " Remove it if no ccwho is launching it"),
        "unresolved": f"an earlier launch did not finish inside iTerm2; {MAY_STILL_RUN}",
        "sending": ("an earlier launch is still being sent - its ccwho is gone, its osascript"
                    " runs on; try again when it ends, or restart iTerm2"),
        "cut off": f"an earlier launch was {CUT_OFF}; try again in {left} s",
    }.get(reason, "it is already starting in another window")


def open_session(argv):
    """Get me to this session: focus its window if it is up, reopen it if not.

    The verb is decided HERE, against the world at click time, not baked into the
    link when the list was printed. After a reboot every link in a restore list
    resolves to "reopen"; ten minutes later the same link focuses the window it
    just made.
    """
    sid = (argv[0] if argv else "").strip()
    status = {}
    rows, _ = scan(cache={}, status=status)
    action, value = engine.resolve_open(sid, rows, known_entries(),
                                        source_ok=status.get("source_ok", False))
    if action == "jump":
        return jump([value])
    if action == "attach":
        # Running, with no window: give it one. `claude attach` opens a session
        # that is already running; reopening it would fork the conversation.
        try:
            res = subprocess.run(["osascript", "-e", engine.iterm_run_script(value)],
                                 capture_output=True, text=True, errors="replace",
                                 timeout=ITERM_ACTION_DEADLINE)
        except subprocess.TimeoutExpired:
            print(f"ccwho open: iTerm2 did not answer in {ITERM_ACTION_DEADLINE:g}s",
                  file=sys.stderr)
            return 1
        if res.returncode != 0:
            # its code, not its text: osascript's message quotes paths
            print("ccwho open: could not open a window - %s"
                  % engine.osascript_trouble(res.returncode, res.stderr), file=sys.stderr)
            return 1
        print(f"attached {sid} in a new window")
        return 0
    if action == "program":
        print(f"ccwho open: {value}", file=sys.stderr)
        return 1
    if action == "unknown":
        print(f"ccwho open: {value} - not reopening anything.", file=sys.stderr)
        print("  Check that `claude` is on PATH and `claude agents --json` answers.",
              file=sys.stderr)
        return 1
    if action == "resume":
        entry = next((e_ for e_ in known_entries() if e_.get("sessionId") == sid), {})
        why = resume_problem(entry)
        if why:
            print(f"ccwho open: not reopening {sid} - {why}", file=sys.stderr)
            return 1
        if not claim_launch(sid, decided_at=status.get("seen_at"), decided_mono=status.get("seen_mono")):
            reason, left = why_held(sid)
            print(f"ccwho open: not launching {sid} - {_why_text(reason, sid, left)}.", file=sys.stderr)
            return 1
        res, why = launch_in_iterm(engine.iterm_run_script(value), ITERM_ACTION_DEADLINE, [sid])
        if res is None:
            print(f"ccwho open: {why}", file=sys.stderr)
            return 1
        print("reopened %s" % sid)
        return 0
    print("ccwho open: %s is neither running nor in any saved manifest" % (sid or "?"),
          file=sys.stderr)
    return 1


def url(argv):
    """Dispatch a ccwho:// URL. The registered handler forwards them all here, so
    it never needs updating for a new verb - and nothing it does not recognise
    reaches a verb at all."""
    verb, target = engine.parse_ccwho_url(argv[0] if argv else "")
    if verb == "open":
        return open_session([target])
    if verb == "jump":
        return jump([target])
    print("ccwho url: not a ccwho:// link I understand", file=sys.stderr)
    return 1


def parse_age(text, default=3600):
    """--older-than 90m / 2h / 3d / 600 (seconds)."""
    t = (text or "").strip().lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if t and t[-1] in mult:
        try:
            return int(float(t[:-1]) * mult[t[-1]])
        except ValueError:
            return default
    try:
        return int(t)
    except ValueError:
        return default


VALUE_FLAGS = ("--older-than",)


def positional(argv, value_flags, default):
    """First real positional. A flag's VALUE is not one - taking it as the pattern
    is how `reap --older-than 1h` came to match PeopleViewService."""
    skip = False
    for a in argv:
        if skip:
            skip = False
            continue
        if a in value_flags:
            skip = True
            continue
        if a.startswith("-"):
            continue
        return a
    return default


def ps(argv):
    """Every process an agent started: its session, its ports, its command.

    The plain view of what the list shows - for a terminal, a script, or an
    agent told to clean up after itself (--json). Helpers (MCP servers and the
    like) only with --all; `--port N` answers "who holds :N".
    """
    port = None
    given = [a for a in argv if a.startswith("--port=")]
    if "--port" in argv or given:
        # `--port N` and `--port=N` ask the same question
        raw = (given[0].split("=", 1)[1] if given else _arg(argv, "--port", "")).lstrip(":")
        if not (raw.isascii() and raw.isdigit() and 0 < int(raw) <= 65535):
            print("ccwho ps: --port needs a port number, e.g. --port 3000",
                  file=sys.stderr)
            return 2
        port = int(raw)
    # --full: the whole command line, redacted - best effort, so never the default
    full = "--full" in argv
    rows, fleet = scan(cache={})
    fleet = fleet if isinstance(fleet, dict) else {}
    if not fleet.get("procs_ok", True):
        # 3, the unknown exit: 1 is "no agent holds it", and a script acts on 1
        print("ccwho ps: processes unknown - ps or the environment read failed",
              file=sys.stderr)
        return 3
    # a port question asks about every holder, helpers included
    listed = engine.ps_listing(rows, fleet, show_all="--all" in argv or port is not None)
    known = fleet.get("ports_ok", True)
    if port is not None:
        if not known:
            # not 1: "nobody holds it" is an answer, and this is not one
            print(f"ccwho ps: ports unknown - lsof could not be asked, so who holds"
                  f" :{port} is not known", file=sys.stderr)
            return 3
        listed = [p for p in listed if port in p.get("ports", [])]
        if not listed and port in (fleet.get("unknown_ports") or []):
            held = ", ".join(f"{engine.procs.printable(str(h.get('name')))} (pid {h.get('pid')})"
                             for h in (fleet.get("unknown_holders") or {}).get(port) or [])
            print(f"ccwho ps: :{port} is held by {held or 'a process'}, whose environment"
                  f" could not be read - who started it is not known", file=sys.stderr)
            return 3
        if not listed:
            print(f"ccwho ps: nothing an agent started holds :{port}", file=sys.stderr)
            return 1
    if "--json" in argv:
        keep = ("pid", "ports", "command", "group", "who", "session", "harness",
                "orphan", "helper", "why") + (("command_full",) if full else ())
        # ports unknown is null, not [] - a script must not read "holds nothing"
        print(json.dumps([dict({k: p.get(k) for k in keep},
                               ports=p.get("ports") if known else None)
                          for p in listed], indent=2))
        return 0
    if not listed:
        print("no processes started by agents")
        return 0
    for p in listed:
        ports = (" ".join(f":{n}" for n in p.get("ports", [])) or "-") if known else "?"
        print(f"{p['pid']:<7} {ports:<14} {engine.truncate(p['who'], 40):<40} "
              + (p.get("command_full", "") if full
                 else engine.truncate(p.get("command", ""), 60)))
    return 0


KILL_USAGE = ("usage: ccwho kill <pid>|:<port>|<session> [--pid] [--dry-run] [--yes] [--force]\n"
              "       ccwho clean [--mine] [--dry-run] [--yes] [--force]")
KILL_FLAGS = ("--dry-run", "--yes", "--force")


def _kill_target(argv, clean):
    """(mode, target) from the arguments, or None: a usage error."""
    flags = [a for a in argv if a.startswith("-")]
    words = [a for a in argv if not a.startswith("-")]
    if any(f not in KILL_FLAGS + (("--mine",) if clean else ("--pid",)) for f in flags):
        return None
    if clean:
        return None if words else (("mine", None) if "--mine" in flags else ("clean", None))
    if len(words) != 1:
        return None
    w = words[0]
    # a session, as `ccwho jump` names it: a short id or a name - a letter in
    # it, so a pid is never read as one - or its full id, or a short id led by
    # a zero (a pid has none)
    if ((re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", w) and re.search(r"[A-Za-z]", w))
            or engine.procs._UUID.fullmatch(w) or re.fullmatch(r"0[0-9]{3,7}", w)):
        return None if "--pid" in flags else ("query", w)
    digits = w[1:] if w.startswith(":") else w
    if not (digits.isascii() and digits.isdigit() and len(digits) < 8 and digits[0] != "0"):
        return None                         # no sign, no leading zero: one spelling each
    if w.startswith(":"):
        return None if int(digits) > 65535 or "--pid" in flags else ("port", digits)
    return ("pid", int(digits)) if "--pid" in flags else ("digits", int(digits))


def _can_ask(stdin, stdout):
    """A person can answer only what they see: the list and the question go to
    stdout, the answer comes from stdin - both must be the terminal."""
    try:
        return bool(stdin.isatty() and stdout.isatty())
    except (AttributeError, ValueError, OSError):   # none, or closed
        return False


def _ask(prompt):
    """The question on stdout, where the list is (input() writes it to stderr);
    one line of stdin as the answer. The end of input raises EOFError: no.
    What was typed before the question is thrown away: reading the machine
    takes a second, and a "y" typed in it would confirm a list nobody saw."""
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        fd = None                           # no file behind it: nothing typed ahead
    if fd is not None and os.isatty(fd):
        import termios
        try:
            termios.tcflush(fd, termios.TCIFLUSH)
        except (termios.error, OSError):
            raise EOFError from None        # typed-ahead text may still be there: no
    sys.stdout.write(prompt)
    sys.stdout.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line


def _names_exactly(row, word):
    """`word` is one of the row's own names - an id prefix, its name, its tty -
    not a piece of its title."""
    w = word.strip().lower()
    tty = row.get("tty") or ""
    return bool(w) and ((row.get("sessionId") or "").lower().startswith(w)
                        or w == (row.get("name") or "").lower()
                        or w in (tty.lower(), engine.short_tty(tty).lower()))


def _one_session(word, rows, exact, nothing):
    """(the one live session `word` names, None), or (None, exit code) after
    saying why: none (1), several (2), a loose word when `exact` - nobody
    reads the list, so a word found in a title is no one's choice (2) - or the
    sessions not read (1; interrupted 130). `nothing` ends each refusal."""
    try:
        hits = engine.match_rows(rows(), word)
    except KeyboardInterrupt:
        print(f"ccwho: interrupted - {nothing}", file=sys.stderr)
        return None, 130
    except Exception as err:
        print(f"ccwho: the sessions could not be read ({type(err).__name__})"
              f" - {nothing}", file=sys.stderr)
        return None, 1
    if not hits:
        print(f"ccwho: no live session matches {word!r} - what an ended session"
              f" left: ccwho clean", file=sys.stderr)
        return None, 1
    if len(hits) > 1:
        print(f"ccwho: {word!r} matches {len(hits)} sessions - be more specific:",
              file=sys.stderr)
        for line in engine.pick_lines(hits):
            print(f"  {line}", file=sys.stderr)
        return None, 2
    if exact and not _names_exactly(hits[0], word):
        print(f"ccwho: {word!r} found {engine.pick_line(hits[0])} - with no one to"
              f" confirm, name it exactly: its id, name or tty", file=sys.stderr)
        return None, 2
    print(f"session {engine.pick_line(hits[0])}")
    return hits[0], None


def _live_rows():
    """The live sessions, or an error: an unread feed is no "no match"."""
    status = {}
    rows, _ = scan(cache={}, status=status)
    if not status.get("source_ok", True):
        raise LookupError("claude agents could not be read")
    return rows


def kill_cli(argv, clean=False, seams=None):
    """`ccwho kill <pid>|:<port>` and `ccwho clean [--mine]`.

    A person sees every process the kill takes, each with what ccwho doubts
    about it, and answers on a terminal; --yes skips the question; with no
    terminal and no --yes the list is printed and the exit is 3. An agent - a
    session id in ccwho's own environment - is never asked, and kill_plan takes
    only its own session's work. Right before each signal the signaller checks
    again (engine.carry_out). Exit 0 all killed, 1 not all (or nothing), 2
    usage, 3 needs --yes. `seams` replaces the machine (tests)."""
    s = {"build": lambda mine=None, status=None: engine.build_world(mine, status=status),
         "ask": lambda prompt: _ask(prompt), "tty": lambda: _can_ask(sys.stdin, sys.stdout),
         "env": os.environ,
         "carry": engine.carry_out, "rows": _live_rows}
    s.update(seams or {})
    parsed = _kill_target(argv, clean)
    if parsed is None:
        print(KILL_USAGE, file=sys.stderr)
        return 2
    mode, target = parsed
    asked_digits = mode == "digits"          # a pid, unless it starts a session id
    mode = "pid" if asked_digits else mode
    env = s["env"]
    mine = engine.agent_id(env)             # an agent: its own session's work only
    if mode == "query":
        # a live session: what it started (the owner's D12)
        row, rc = _one_session(target, s["rows"], "--yes" in argv or mine is not None,
                               "nothing killed")
        if row is None:
            return rc
        mode, target = "session", row.get("sessionId") or ""
    if mode == "mine":
        target = mine
    status = {}
    try:
        world = s["build"](mine=mine, status=status)
        if status.get("trouble"):
            print(f"ccwho: {status['trouble']} - nothing killed", file=sys.stderr)
            return 1
        starts = [x.get("sessionId") for x in world["sessions"]
                  if asked_digits and (x.get("sessionId") or "").startswith(str(target))]
        if starts:
            # a short id is four hex, often all digits: asked, never guessed
            print(f"ccwho: {target} is a pid and the start of a session id - the session:",
                  file=sys.stderr)
            for sid in starts:
                print(f"  ccwho kill {sid}", file=sys.stderr)
            print(f"  the pid: ccwho kill {target} --pid", file=sys.stderr)
            return 2
        if mode == "pid":
            row = world["table"].get(target)
            target = {"pid": target, "start": row[1] if row else ""}
        plan = engine.procs.kill_plan(mode, target, world)
    except KeyboardInterrupt:
        print("ccwho: interrupted - nothing killed", file=sys.stderr)
        return 130
    except Exception as err:        # its text may hold a command line: the type only
        print(f"ccwho: failed ({type(err).__name__}) - nothing killed", file=sys.stderr)
        return 1
    kill, spare = plan["kill"], plan["spare"]
    for line in engine.kill_list_lines(plan):
        print(line)
    if not kill:
        return 1
    # a tree refused (not clean's "outside" spares): the real run could not take it all
    refused = any(not e.get("outside") for e in spare)
    if "--dry-run" in argv:
        print("dry run - nothing killed")
        return 1 if refused else 0
    if mine is None and "--yes" not in argv:
        if not s["tty"]():
            print(f"would kill {len(kill)} process{'es' if len(kill) != 1 else ''}"
                  f" (listed above) - add --yes to do it")
            return 3
        try:
            answer = s["ask"](f"kill these {len(kill)}? [y/N] ")
        except EOFError:
            answer = ""
        except KeyboardInterrupt:
            print("\nccwho: interrupted - nothing killed", file=sys.stderr)
            return 130
        if answer.strip().lower() not in ("y", "yes"):
            print("nothing killed")
            return 1
    try:
        r = s["carry"](mode, target, kill, force="--force" in argv, mine=mine)
    except (Exception, KeyboardInterrupt) as err:
        interrupted = isinstance(err, KeyboardInterrupt)
        if getattr(err, "ccwho_nothing_signalled", False):
            what = "interrupted" if interrupted else f"failed ({type(err).__name__})"
            print(f"ccwho: {what} - nothing killed", file=sys.stderr)
            return 130 if interrupted else 1
        # the signaller had started: what it signalled is not known here
        print(f"ccwho: {'interrupted' if interrupted else f'failed ({type(err).__name__})'}"
              f" - some of these may have been signalled; run ccwho ps", file=sys.stderr)
        return 1
    try:
        lines, rc = engine.kill_report_lines(r, refused)
        for line in lines:
            print(line)
        return rc
    except (Exception, KeyboardInterrupt):
        print("\nccwho: stopped while reporting - run ccwho ps to see what still runs",
              file=sys.stderr)
        return 1


STOP_USAGE = "usage: ccwho stop <session> [--and-procs] [--dry-run] [--yes]"
STOP_FLAGS = ("--and-procs", "--dry-run", "--yes")


def _claude_stop(sid, config_dir=None):
    """`claude stop <job>`: (exit code, its text). The job is the session id's
    first eight (the full id is "No job matching"), in the session's own config
    dir, as `claude attach` needs it (engine.attach_command)."""
    # a default-dir session has none - whatever dir ccwho's own shell names
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    done = subprocess.run(["claude", "stop", engine.daemon_short(sid)], capture_output=True,
                          text=True, timeout=30, env=env)
    return done.returncode, (done.stderr or done.stdout or "")


def _not_known(plan):
    """kill_plan took nothing and cannot say what runs: a "why" (it comes only
    with an empty plan) that is not "nothing left"."""
    return bool(plan.get("why")) and plan["why"] != engine.procs.SESSION_LEFT_NOTHING


STOP_WAIT = 10         # seconds for a stopped session to leave the list and the table


def _session_gone(row):
    """True: the session is no longer listed and its claude process has ended
    (ps: no signal). False: it still runs. None: not known (the feed or ps
    could not be read)."""
    try:
        if any(r.get("sessionId") == row.get("sessionId") for r in _live_rows()):
            return False
        pid = row.get("pid")
        if isinstance(pid, int) and pid > 1:
            done = subprocess.run(["ps", "-p", str(pid), "-o", "pid="], capture_output=True,
                                  text=True, timeout=5)
            if done.returncode == 0 or done.stdout.strip():
                return False                # its claude runs
            # ps says nothing, and no error: the pid is gone. An error: not known
            return None if done.stderr.strip() else True
        return True
    except Exception:
        return None


def stop_cli(argv, seams=None):
    """`ccwho stop <session> [--and-procs]` (the owner's D11, 2026-09-27).

    A background session is stopped with `claude stop <id>`, which keeps its
    conversation (`claude attach` brings it back). An interactive one is never
    signalled: it runs in a window, and that is where to end it. The session is
    named as `ccwho kill` names one. --and-procs also kills what it started
    (kill_plan's "session" list, shown first; one question covers both) -
    after the stop, and only when the stop worked. Without it, what the session
    left running is said. An agent stops no session. Exit 0 done, 1 not (or
    not all), 2 usage, 3 needs --yes, 130 interrupted."""
    s = {"build": lambda mine=None, status=None: engine.build_world(mine, status=status),
         "ask": lambda prompt: _ask(prompt), "tty": lambda: _can_ask(sys.stdin, sys.stdout),
         "env": os.environ, "carry": engine.carry_out, "rows": _live_rows,
         "stop": _claude_stop, "gone": _session_gone, "sleep": time.sleep,
         "clock": time.monotonic}
    s.update(seams or {})
    flags = [a for a in argv if a.startswith("-")]
    words = [a for a in argv if not a.startswith("-")]
    parsed = _kill_target(words, False) if len(words) == 1 else None
    if any(f not in STOP_FLAGS for f in flags) or parsed is None or parsed[0] != "query":
        print(STOP_USAGE, file=sys.stderr)
        return 2
    word = words[0]
    if engine.agent_id(s["env"]) is not None:
        print("ccwho: an agent does not stop sessions - nothing stopped", file=sys.stderr)
        return 1
    row, rc = _one_session(word, s["rows"], "--yes" in argv, "nothing stopped")
    if row is None:
        return rc
    sid, conf = row.get("sessionId") or "", row.get("configDir") or None
    job = f"claude stop {engine.daemon_short(sid)}"
    # `claude attach` keeps it "background": someone may be using it in a window
    tty = engine.short_tty(row.get("tty") or "")
    seen = f" - it is open in a window ({tty})" if tty and row.get("windowed") else ""
    if row.get("kind") != "background":
        print(f"ccwho: it runs in a window{f' ({tty})' if tty else ''} - ccwho does not"
              f" stop it; end it there (/exit). To find it: ccwho jump {word}", file=sys.stderr)
        return 1
    procs_ = "--and-procs" in argv
    plan, kill, refused = None, [], False
    if procs_:
        status = {}
        try:
            world = s["build"](mine=None, status=status)
            if status.get("trouble"):
                print(f"ccwho: {status['trouble']} - nothing stopped", file=sys.stderr)
                return 1
            plan = engine.procs.kill_plan("session", sid, world)
        except KeyboardInterrupt:
            print("ccwho: interrupted - nothing stopped", file=sys.stderr)
            return 130
        except Exception as err:
            print(f"ccwho: failed ({type(err).__name__}) - nothing stopped", file=sys.stderr)
            return 1
        for line in engine.kill_list_lines(plan):
            print(line)
        kill = plan["kill"]
        # a tree refused, or what it started not known: not all of it can go
        refused = (any(not e.get("outside") for e in plan["spare"])
                   or _not_known(plan))
    print(f"and stop the session: {job} - its conversation is kept{seen}"
          if kill else f"stop the session: {job} - its conversation is kept{seen}")
    if "--dry-run" in argv:
        print("dry run - nothing stopped")
        return 1 if refused else 0
    if "--yes" not in argv:
        what = (f"stop it and kill these {len(kill)}" if kill else "stop it") + (
            f" - it is open in a window ({tty})" if seen else "")
        if not s["tty"]():
            print(f"would {what} (listed above) - add --yes to do it")
            return 3
        try:
            answer = s["ask"](f"{what}? [y/N] ")
        except EOFError:
            answer = ""
        except KeyboardInterrupt:
            print("\nccwho: interrupted - nothing stopped", file=sys.stderr)
            return 130
        if answer.strip().lower() not in ("y", "yes"):
            print("nothing stopped")
            return 1
    try:
        code, _text = s["stop"](sid, conf)  # its text may name paths: not shown
    except KeyboardInterrupt:
        print("ccwho: interrupted - the session may have been stopped; run ccwho ls",
              file=sys.stderr)
        return 130
    except Exception as err:
        print(f"ccwho: could not stop it ({type(err).__name__}) - nothing killed",
              file=sys.stderr)
        return 1
    if code:
        print(f"ccwho: could not stop it (claude stop exited {code}) - nothing killed",
              file=sys.stderr)
        return 1
    # claude's word is not enough: what it started is killed only once the
    # session is gone - listed no more, its claude ended
    # a deadline by the clock: one read of the feed takes a second or more
    deadline = s["clock"]() + STOP_WAIT
    try:
        while not (gone := s["gone"](row)):
            if s["clock"]() >= deadline:
                what = ("its end could not be confirmed" if gone is None
                        else "the session still runs")
                print(f"ccwho: claude stop said done, but {what} - nothing killed;"
                      f" run ccwho ls", file=sys.stderr)
                return 1
            s["sleep"](0.5)
    except KeyboardInterrupt:
        print("ccwho: interrupted - the session may have been stopped; nothing killed;"
              " run ccwho ls", file=sys.stderr)
        return 130
    print(f"stopped - its conversation is kept: {engine.attach_command(sid, conf)}")
    if kill:
        try:
            r = s["carry"]("session", sid, kill, force=False, mine=None)
            lines, rc = engine.kill_report_lines(r, refused)
        except (Exception, KeyboardInterrupt) as err:
            stopped = isinstance(err, KeyboardInterrupt)
            what = "interrupted" if stopped else f"failed ({type(err).__name__})"
            said = ("nothing killed" if getattr(err, "ccwho_nothing_signalled", False)
                    else "some of its processes may have been signalled; run ccwho ps")
            print(f"ccwho: {what} - {said}", file=sys.stderr)
            return 130 if stopped else 1
        for line in lines:
            print(line)
        return rc
    # `claude stop` leaves what a session started with nohup running (S3)
    try:
        status = {}
        world = s["build"](mine=None, status=status)
        if status.get("trouble"):
            raise LookupError
        plan = engine.procs.kill_plan("session", sid, world)
    except KeyboardInterrupt:
        print("ccwho: interrupted - what it left running: run ccwho ps", file=sys.stderr)
        return 130
    except Exception:
        print("what it left running: run ccwho ps")
        return 1 if refused else 0
    # what it takes, and what it names apart (a helper, a shell, ...): all still run
    for e in plan["kill"] + [e for e in plan["spare"] if e.get("outside")]:
        print(f"still running: {e['pid']} {e.get('command') or ''} - ccwho kill {e['pid']}")
    for e in (e for e in plan["spare"] if not e.get("outside")):
        print(f"not killed: {e['pid']} {e.get('command') or ''} - {e.get('why') or ''}")
    if _not_known(plan):
        print("what it left running: not known - run ccwho ps")
    # --and-procs with a tree refused: as its dry run said, not all of it went
    return 1 if refused else 0


def reap(argv):
    """Kill leaked helper processes older than a threshold. Dry run by default."""
    pattern = positional(argv, VALUE_FLAGS, "liveapp-pty-guards")
    age = parse_age(_arg(argv, "--older-than", "1h"))
    ps = engine.ps_snapshot_elapsed()
    hits = engine.reap_candidates(ps, pattern, min_age=age)
    if not hits:
        print(f"nothing matching {pattern!r} older than {age}s")
        return 0
    roots = [h for h in hits if h["ppid"] == 1]
    print(f"{len(hits)} processes match {pattern!r} and are older than {age}s "
          f"({len(roots)} orphaned roots)")
    for h in sorted(hits, key=lambda x: -x["age"])[:5]:
        # a command line can carry a token; this output reaches agents too
        print(f"  {h['pid']:<8} {h['age'] // 3600}h  "
              f"{engine.procs.safe_command(h['command'])[:88]}")
    if len(hits) > 5:
        print(f"  ... and {len(hits) - 5} more")
    if "--kill" not in argv:
        print("\ndry run. add --kill to actually terminate them.")
        return 0
    killed = 0
    for h in sorted(hits, key=lambda x: x["ppid"] != 1):   # orphan roots first
        try:
            os.kill(h["pid"], signal.SIGTERM)
            killed += 1
        except (ProcessLookupError, PermissionError):
            pass
    print(f"sent SIGTERM to {killed} processes")
    return 0


AUTOSAVE_SECS = float(os.environ.get("CCWHO_AUTOSAVE", "300"))
KEEP_MANIFESTS = 20


def should_autosave(last, now, interval):
    """True when a watch should refresh the restore manifest.

    Fires on the FIRST tick (last is None) so a watch is useful straight away, and
    treats a backwards clock as due rather than never-due: an ntp step must not
    wedge the one file that makes an unplanned reboot survivable.
    """
    if not interval or interval <= 0:
        return False
    if last is None:
        return True
    return not (0 <= now - last < interval)


def prune_manifests(names, keep=KEEP_MANIFESTS):
    """Which manifests to delete. This tool exists because a disk filled up; it
    does not get to fill one. keep<=0 deletes NOTHING - fail safe, not empty."""
    if keep <= 0:
        return []
    found = sorted(n for n in (names or []) if n and engine._MANIFEST_NAME.match(n))
    return found[:-keep] if len(found) > keep else []


def ccwho_dir():
    """Under $HOME, never a temp dir: the whole point is to outlive the reboot."""
    return os.environ.get("CCWHO_DIR") or os.path.expanduser("~/.ccwho")


def usage_dir():
    return os.path.join(ccwho_dir(), "usage")


def labels_path():
    return os.path.join(ccwho_dir(), "accounts.json")


def prune_usage():
    """Readings older than the 7-day window. Housekeeping: it never fails."""
    d = usage_dir()
    for name in usage.prune(d, time.time()):
        try:
            os.unlink(os.path.join(d, name))
        except OSError:
            pass


def statusline_claude(env):
    """(pid, auth) of the claude running this statusLine; (None, None) unless
    CLAUDE_PID names this process's own parent.

    Claude Code gives the statusLine no CLAUDE_CODE_OAUTH_TOKEN (#28): which
    account a token session spends is known only to its claude. The parent check
    is what makes the pid that claude, and not a stale or borrowed number. A
    statusLine wrapped in a script of its own is not confirmed, and is read as
    before. Never raises."""
    try:
        text = env.get("CLAUDE_PID", "")
        if not (text.isascii() and text.isdigit() and len(text) < 8):
            return None, None
        pid = int(text)
        if pid != os.getppid():
            return None, None
        return pid, engine.read_auth(pid)
    except Exception:             # noqa: BLE001 - the reading is still recorded
        return None, None


def statusline(argv):
    """Claude Code runs this in every session, on every status update, with the
    session's JSON on stdin. It records the usage it is handed and prints
    nothing. Whatever arrives, it exits 0: a statusLine that errors is noise in
    every session the user has open."""
    try:
        payload = json.loads(sys.stdin.read() or "null")
        sid = payload.get("session_id") if isinstance(payload, dict) else None
        d = usage_dir()
        path = os.path.join(d, f"{sid}.json")
        previous = None
        if isinstance(sid, str) and usage._SID.match(sid):
            try:
                with open(path) as fh:
                    previous = json.load(fh)
            except (OSError, ValueError):
                previous = None
        claude_pid, claude_auth = statusline_claude(os.environ)
        rec = usage.record(payload, os.environ, setup_home(), time.time(), previous,
                           claude_pid=claude_pid, claude_auth=claude_auth)
        if rec is None:
            return 0
        try:
            os.makedirs(d, exist_ok=True)
            write_atomic(path, json.dumps(rec))
        except Exception:         # noqa: BLE001 - a failed write still prints
            pass
        # D10: the session's own entry, in its own status bar
        text = usage.status_text(rec, time.time(), read_labels(), names=read_names())
        if text:
            print(text)
    except Exception:             # noqa: BLE001 - see the docstring
        pass
    return 0


_SHOWN_LAST = None       # what the shown-log last recorded, so it logs changes only


def usage_snapshot(rows, now=None, record=True):
    """What the list and the table show about usage, for these live rows. Also
    logs what it shows (owner, 2026-09-25: the data that decides when an old
    window loses its pace arrow)."""
    global _SHOWN_LAST
    now = time.time() if now is None else now
    # the module in sys.modules, not this file's handle: the live list imports
    # the runner once, and after its reload (or a failed one) sys.modules is
    # what holds the module to use
    u = sys.modules.get("ccwho_usage", usage)
    readings = u.load_readings(usage_dir(), now)
    snap = u.snapshot(readings, now, engine.live_ids(rows),
                      lambda: usage_facts(now, readings), read_labels())
    # only a snapshot over the whole fleet is recorded: a filtered `ls` sees
    # fewer accounts, so fewer clashes - its names and lines are not the list's
    if record:
        _SHOWN_LAST = u.append_shown(os.path.join(ccwho_dir(), "usage-shown.jsonl"),
                                     u.shown_records(snap), _SHOWN_LAST, now)
        save_names(snap.get("names") or {})
    return snap


def names_path():
    return os.path.join(ccwho_dir(), "usage-names.json")


def save_names(names):
    """The names the list uses, for the status bar: one name per account in
    both places, clashes and all. Written only when they change."""
    if not names:
        return
    old = read_names()
    merged = dict(old, **names)
    if merged == old:
        return
    try:
        os.makedirs(ccwho_dir(), exist_ok=True)
        write_atomic(names_path(), json.dumps(merged))
    except OSError:
        pass


def read_names():
    try:
        with open(names_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def with_usage(rows, fleet, record=True):
    """The fleet, with the usage snapshot the table draws. A usage failure is
    "unknown" and never costs the table its rows (eng E-D7)."""
    fleet = dict(fleet) if isinstance(fleet, dict) else {}
    try:
        fleet["usage"] = usage_snapshot(rows, record=record)
    except Exception:             # noqa: BLE001 - see the docstring
        fleet["usage"] = {"state": "unknown"}
    return fleet


def read_labels():
    try:
        with open(labels_path()) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def accounts(argv):
    """Usage per account, from what the sessions' statusLines recorded."""
    now = time.time()
    rows = usage.accounts(usage.load_readings(usage_dir(), now), now, read_labels())
    if argv and argv[0] == "name":
        if len(argv) != 3:
            print("usage: ccwho accounts name <id> <label>", file=sys.stderr)
            return 2
        aid = usage.resolve_id(argv[1], [r["id"] for r in rows])
        if not aid:
            print(f"ccwho accounts: no single account matches {argv[1]!r}"
                  " - `ccwho accounts --json` lists the ids", file=sys.stderr)
            return 2
        labels = read_labels()
        labels[aid] = argv[2]
        os.makedirs(ccwho_dir(), exist_ok=True)
        write_atomic(labels_path(), json.dumps(labels, indent=2))
        print(f"{aid} -> {argv[2]}")
        return 0
    if "--json" in argv:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no usage recorded yet. Sessions record it through `ccwho statusline`,"
              " after their first reply.")
        return 0
    for row in rows:
        print(usage.format_row(row, now))
    return 0


def usage_roots():
    """(interactive config dirs, the others), for setup.

    The known roots go through the saved index, refreshed. Dirs only a running
    session names are scanned but NOT saved: the index drops every path it was
    not given, so saving them would rescan them on every later refresh.
    """
    try:
        dirs = engine.config_dirs_now()
    except Exception:             # noqa: BLE001 - ps unreadable: the known roots
        dirs = index.config_roots()
    dirs = [d for d in dirs if os.path.isdir(d)]
    known = set(index.config_roots())
    entries = list(fresh_index().values())
    extra = [d for d in dirs if d not in known]
    if extra:
        entries += list(index.update({}, index.transcripts(roots=extra)).values())
    return setup.interactive_roots(entries, dirs)


def usage_facts(now=None, readings=None):
    """What doctor says about usage. Read-only and cheap: the saved index as it
    is, and the settings files. A dir is reported when it is interactive, holds
    ccwho's statusLine, or was opted out."""
    now = time.time() if now is None else now
    dirs = []
    for d in index.config_roots():
        d = os.path.normpath(d)
        if d not in dirs and os.path.isdir(d):
            dirs.append(d)
    yes, _ = setup.interactive_roots(index.load(index_path()).values(), dirs)
    try:
        opted = {os.path.normpath(d) for d in
                 setup.parse_opt_out(_text_or_none(opt_out_path()))}
    except (OSError, ValueError):
        opted = set()
    roots = []
    for d in dirs:
        text, problem = read_settings(os.path.join(d, "settings.json"))
        state = "invalid" if problem else setup.statusline_state(text)
        if d in yes or state == "ours" or d in opted:
            roots.append({"root": d, "state": state, "opted_out": d in opted})
    if readings is None:
        readings = usage.load_readings(usage_dir(), now)
    newest = max((r["received_at"] for r in readings), default=None)
    return {"usage_roots": roots,
            "usage_newest_age": None if newest is None else max(0.0, now - newest)}


def opt_out_path():
    return os.path.join(ccwho_dir(), "usage-opt-out")


def _text_or_none(path):
    """A file's text, or None when there is no file - unlike read_text, an
    empty settings.json and a missing one are different answers here."""
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def read_settings(path):
    """(text, problem) for a settings.json. A problem means: do not touch it."""
    if os.path.islink(path) and not os.path.exists(path):
        return None, "is a broken link - not touched"
    try:
        return _text_or_none(os.path.realpath(path)), ""
    except UnicodeDecodeError:
        return None, "is not valid UTF-8 - not touched"
    except OSError as ex:
        return None, f"cannot be read ({ex.strerror}) - not touched"


def _new_file_mode():
    """What open() would give a new file under the user's umask."""
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def _backup(path, text, mode):
    """Keep the text we replace, beside the file, never over an older backup:
    the first one holds what was there before ccwho ever touched it."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for n in range(1, 100):
        name = f"{path}.bak-ccwho-{stamp}" + (f"-{n}" if n > 1 else "")
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            continue
        try:
            try:
                os.fchmod(fd, mode)
                fh = os.fdopen(fd, "w", encoding="utf-8")
            except BaseException:
                os.close(fd)
                raise
            with fh:
                fh.write(text)
        except BaseException:
            # half a backup looks like the first one and holds the wrong text
            os.unlink(name)
            raise
        return name
    raise OSError("no free backup name")


def _ask_tty(prompt):
    """y/N on a terminal; None when nobody is there to answer."""
    if not sys.stdin.isatty():
        return None
    try:
        return input(prompt)
    except EOFError:
        return None


def _said_yes(reply):
    return (reply or "").strip().lower() in ("y", "yes")


def guarded_write(path, before, new):
    """Replace a settings file we read as `before` with `new`, or say why not.

    The file is read again first: another tool may have written it while we
    were asking, and writing over that is exactly the silent breakage doctor
    exists for. A symlinked file is written through its link, and the old text
    is kept beside it.
    """
    target = os.path.realpath(path)
    try:
        now = _text_or_none(target)
        mode = os.stat(target).st_mode & 0o777 if now is not None else _new_file_mode()
    except (OSError, ValueError) as ex:
        print(f"  {path}: cannot read it again ({ex}) - not written")
        return 1
    if now != before:
        print(f"  {path} changed while ccwho was asking - not written."
              " Run ccwho setup again.")
        return 1
    try:
        if before is not None:
            _backup(path, before, mode)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        write_atomic(target, new, mode=mode)
        with open(target, encoding="utf-8") as fh:
            ok = json.load(fh) == json.loads(new)
    except (OSError, ValueError) as ex:
        print(f"  {path}: could not write it ({ex})")
        return 1
    if not ok:
        print(f"  {path}: read back different from what was written")
        return 1
    print(f"  wrote {path}")
    return 0


USAGE_WHY = "Without this, you won't get usage info."


def usage_setup(roots, skipped, yes=False, mode="", ask=None, ccwho=None):
    """ccwho's statusLine in each interactive config dir, asked for dir by dir.

    It is only added where no statusLine is set, and a no is remembered for
    that dir: a later setup does not ask again, and --yes does not override it.
    mode "on" (--usage) forgets every no; mode "off" (--no-usage) says no for
    every dir and takes ccwho's own statusLine back out, with the same care.
    """
    ask = ask or _ask_tty
    command = setup.statusline_command(ccwho or ccwho_bin())
    # one spelling per dir: CLAUDE_CONFIG_DIR=/x/ and /x are the same dir
    roots = [os.path.normpath(r) for r in roots]
    skipped = [os.path.normpath(r) for r in skipped]
    try:
        opted = {os.path.normpath(d) for d in
                 setup.parse_opt_out(_text_or_none(opt_out_path()))}
    except (OSError, ValueError):
        opted = set()

    def remember():
        os.makedirs(ccwho_dir(), exist_ok=True)
        write_atomic(opt_out_path(), setup.render_opt_out(opted))

    rc = 0
    if mode == "off":
        opted |= set(roots) | set(skipped)
        remember()
        still_on, unknown = [], []
        for root in list(roots) + list(skipped):
            path = os.path.join(root, "settings.json")
            before, problem = read_settings(path)
            if problem:
                # unknown content: it may hold ours, so "off" cannot be said
                print(f"  not checked: {path} {problem}")
                unknown.append(f"{path} {problem}")
                rc = 1
                continue
            new = setup.remove_statusline(before)
            if new is None:
                continue
            print(setup.settings_diff(before, new, path), end="")
            if yes or _said_yes(ask(f"Remove ccwho's statusLine from {path}? [y/N] ")):
                if guarded_write(path, before, new) == 0:
                    continue
                rc = 1
            still_on.append(path)
        if still_on:
            # Remembered as a no, but the statusLine is still there: saying "off"
            # here would be the false all-clear doctor exists to avoid.
            print("usage info: still on - ccwho's statusLine was not taken out of "
                  + ", ".join(still_on)
                  + " (answer y in a terminal, or add --yes, to take it out)")
        if unknown:
            # --yes cannot help here: the file itself has to be fixed first
            print("usage info: not known - fix, then run ccwho setup --no-usage again: "
                  + "; ".join(unknown))
        if not still_on and not unknown:
            print("usage info: off (ccwho setup --usage turns it back on)")
        return rc
    if mode == "on" and opted:
        opted = set()
        remember()

    wrote_any = False
    for root in skipped:
        print(f"note usage               skipped {root}: no interactive session there"
              " yet - run ccwho setup again after you use it")
    for root in roots:
        path = os.path.join(root, "settings.json")
        label = f"usage ({root})"
        before, problem = read_settings(path)
        if problem:
            print(f"note {label:<20} settings.json {problem}")
            continue
        state = setup.statusline_state(before)
        if state == "ours":
            print(f"ok   {label:<20} on")
        elif state == "other":
            print(f"note {label:<20} another statusLine is set - left alone,"
                  " so no usage info from this dir")
        elif state == "invalid":
            print(f"note {label:<20} settings.json is not valid JSON - not touched")
        elif root in opted:
            print(f"ok   {label:<20} off (your choice) - ccwho setup --usage turns it on")
        else:
            new = setup.add_statusline(before, command)
            print(f"todo {label:<20} add ccwho's statusLine:")
            print(setup.settings_diff(before, new, path), end="")
            if not yes:
                reply = ask(f"{USAGE_WHY} Add it to {path}? [y/N] ")
                if reply is None:
                    print("  not asked (no terminal) - run ccwho setup in a terminal to add it")
                    continue
                if not _said_yes(reply):
                    opted.add(root)
                    remember()
                    print("  off (your choice) - ccwho setup --usage turns it on")
                    continue
            written = guarded_write(path, before, new)
            rc |= written
            wrote_any = wrote_any or written == 0
    if wrote_any:
        # measured 2026-09-25: after setup wrote ~/.claude/settings.json, 11 of
        # 11 sessions already running reported - they reload it, no restart
        print("  sessions report usage after their next reply")
    return rc


def restore_dir():
    return os.path.join(ccwho_dir(), "restore")


def temp_roots():
    """Where a cwd does not outlive the reboot a save is for. liveapp's test runs
    put a whole claude session - its own CLAUDE_CONFIG_DIR - in $TMPDIR."""
    return [tempfile.gettempdir(), "/tmp", "/private/var/folders"]


def save_problem(row):
    """Why this live session is not worth saving, or "". Not saved: a session
    no restore could reopen, which is what every restore then showed as an error."""
    cwd = row.get("cwd", "") or ""
    roots = [os.path.realpath(r) for r in temp_roots()]
    if engine.in_temp_dir(os.path.realpath(cwd), roots) or engine.in_temp_dir(cwd, roots):
        return "cwd is in a temp dir"
    return resume_problem(row)


def save(argv):
    """Capture the live fleet so a reboot stops being a one-way door."""
    status = {}
    rows, _ = scan(cache={}, status=status)
    if not status.get("source_ok", True):
        print("ccwho save: cannot reach `claude agents` - nothing written.", file=sys.stderr)
        print("  A save that cannot ask must not answer: an empty manifest would be",
              file=sys.stderr)
        print("  kept as the newest and would evict the record of what you had open.",
              file=sys.stderr)
        print("  Check that `claude` is on PATH for whoever ran this.", file=sys.stderr)
        return 1
    panes = engine.panes_snapshot()
    # iTerm2 not asked (None): keep the panes the last save knew
    known = ({e_.get("sessionId"): e_ for e_ in engine.manifest_entries(newest_manifest())
              if isinstance(e_.get("sessionId"), str)} if panes is None else None)
    man = engine.manifest_from_rows(rows, why_not=save_problem, panes=panes, known=known)
    if man["count"] == 0:
        # Writing this would only push a manifest that HAS something out of the
        # keep-20 window. Nothing to restore is not something to record.
        print("no restorable sessions - nothing written.")
        return 0
    out = _arg(argv, "--out", None)
    if not out:
        d = restore_dir()
        os.makedirs(d, exist_ok=True)
        out = os.path.join(d, engine.manifest_name())
    else:
        os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    # Atomic: a half-written manifest read after a reboot is worse than none, and
    # the reboot is exactly when a partial write would happen.
    tmp = out + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(man, fh, indent=2)
        os.replace(tmp, out)
    except OSError as ex:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        print(f"ccwho save: could not write {out}: {ex}", file=sys.stderr)
        return 1
    d = os.path.dirname(out)
    try:
        for stale in prune_manifests(os.listdir(d)):
            os.unlink(os.path.join(d, stale))
    except OSError:
        pass                      # housekeeping never fails the save it follows
    prune_usage()
    why = man.get("skippedWhy") or {}
    note = (", %d not saved (%s)" % (man["skipped"], "; ".join(
        "%d %s" % (n, w) for w, n in sorted(why.items()))) if man["skipped"] else "")
    print(f"saved {man['count']} session(s){note} -> {out}")
    print("after the reboot:  ccwho restore        (add --open to reopen them)")
    return 0


def list_manifests():
    """The saved manifests, oldest first, with what each one holds.

    Reads only - it is the picker for --from, and a picker that could launch
    something would be the same mistake --check was written to avoid.
    """
    names = manifest_names()
    if not names:
        print("ccwho restore: nothing saved yet - run `ccwho save` BEFORE you reboot.",
              file=sys.stderr)
        return 1
    d = restore_dir()
    print("%d manifest(s) in %s" % (len(names), d))
    for i, n in enumerate(names, 1):
        try:
            with open(os.path.join(d, n)) as fh:
                man = json.load(fh)
            count = len(engine.manifest_entries(man))
        except (OSError, ValueError):
            count = -1
        held = "unreadable" if count < 0 else "%2d session%s" % (count, "" if count == 1 else "s")
        tag = "   <- newest, the one a bare `ccwho restore` reads" if n == names[-1] else ""
        print("  %2d. %s  %s%s" % (i, n, held, tag))
    print("\nread one:  ccwho restore --from %s" % os.path.join(d, names[-1]))
    return 0


def newest_manifest():
    """The last saved fleet, or {}. Never raises: the screen asks this to decide
    whether to offer a reopen at all."""
    d = restore_dir()
    try:
        names = os.listdir(d)
    except OSError:
        return {}
    newest = engine.newest_manifest(names)
    if not newest:
        return {}
    try:
        with open(os.path.join(d, newest)) as fh:
            man = json.load(fh)
    except (OSError, ValueError):
        return {}
    return man if isinstance(man, dict) else {}


def booted_at():
    """When the Mac last started, or None. Never raises: it only adds a mark."""
    try:
        r = subprocess.run(["sysctl", "-n", "kern.boottime"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return engine.boot_time(r.stdout) if r.returncode == 0 else None


def save_points(live_ids=()):
    """Every save, newest first, for the `o` menu - each with its path, what it
    holds, how much of it runs now, and the mark on the last one before the
    restart. A save it cannot read is listed as unreadable; no restore dir is
    no saves. A dir it cannot read raises: [] would say "nothing saved"."""
    d = restore_dir()
    try:
        names = os.listdir(d)
    except FileNotFoundError:
        return []
    saves = []
    # the name first: anything else in there - a FIFO, a big file - is never read
    for n in (n for n in names if engine._MANIFEST_NAME.match(n)):
        try:
            with open(os.path.join(d, n)) as fh:
                man = json.load(fh)
        except (OSError, ValueError):
            man = None
        saves.append((n, man if isinstance(man, dict) else None))
    booted = booted_at()
    points = engine.save_points(saves, live_ids=live_ids, booted=booted)
    for p in points:
        p["path"], p["booted"] = os.path.join(d, p["name"]), booted
    return points


_REOPENING = threading.Lock()


def reopen_saved(path=None):
    """What `o` in the list does: the same restore the command line runs, on
    the save chosen in its menu (the newest when none is named).

    Not a second implementation - every guard that stops a bulk reopen forking
    live conversations lives in restore(), and one of those written twice is
    one of them wrong.

    One at a time: its output is taken by swapping the process's stdout, and
    the list runs it in a thread - two at once mixed their lines and left
    stdout on a buffer.
    """
    if not _REOPENING.acquire(blocking=False):
        return "a reopen is already running - wait for it to finish"
    try:
        return _reopen_saved(path)
    finally:
        _REOPENING.release()


def _reopen_saved(path):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = restore(["--open"] + (["--from", path] if path else []))
    if rc:
        tail = [l for l in out.getvalue().splitlines() if l.strip()]
        last = tail[-1] if tail else "see `ccwho restore --open`"
        if MAY_STILL_RUN in last or CUT_OFF in last:
            return last.removeprefix("ccwho restore: ")    # not failed: not answered
        return "could not reopen: " + last
    text = out.getvalue()
    m = re.search(r"^(all \d+ session\(s\)) in that manifest (are already running[^.\n]*)",
                  text, re.M)
    if m:
        return f"{m.group(1)} in that save {m.group(2)}"
    counts = [int(m.group(1)) for m in
              re.finditer(r"^(?:opened|filled) (\d+) (?:window|pane)", text, re.M)]
    left = sum(1 for l in text.splitlines() if l.startswith("ccwho restore: not reopening"))
    waiting = sum(1 for l in text.splitlines() if l.startswith("ccwho restore: still waiting on"))
    held = sum(1 for l in text.splitlines() if l.startswith("ccwho restore: holding"))
    blocked = sum(1 for l in text.splitlines() if l.startswith("ccwho restore: blocked"))
    changed = sum(1 for l in text.splitlines() if l.startswith("ccwho restore: changed"))
    wait = max((int(n) for n in re.findall(r"try again in (\d+) s", text)), default=0)
    said = (f"reopened {sum(counts)} session(s)" if counts
            else "reopened that save" if path else "reopened the last save")
    return (said + (f", {left} left out - they could not resume" if left else "")
            + (f", {waiting} still waiting on an earlier launch - restart iTerm2 if they"
               " do not appear" if waiting else "")
            + (f", {held} {CUT_OFF} - try again in {wait} s" if held else "")
            + (f", {blocked} blocked - a claim file that cannot be read" if blocked else "")
            + (f", {changed} changed since the list was read - run it again" if changed else ""))


def restore(argv):
    """Read the manifest back: what was open, what it was about, how to reopen it."""
    if "--list" in argv:
        return list_manifests()
    path = _arg(argv, "--from", None)
    if not path:
        d = restore_dir()
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        newest = engine.newest_manifest(names)
        if not newest:
            print("ccwho restore: nothing saved yet - run `ccwho save` BEFORE you reboot.",
                  file=sys.stderr)
            return 1
        path = os.path.join(d, newest)

    try:
        with open(path) as fh:
            man = json.load(fh)
    except (OSError, ValueError) as ex:
        # the type only: the list's `o` shows this line, and an error's text can
        # hold a path
        print(f"ccwho restore: cannot read {path} ({type(ex).__name__})", file=sys.stderr)
        return 1

    if "--check" in argv:
        # Deliberately BEFORE --open: a check must never launch anything, even if
        # both flags are given. Answering "would this work?" cannot be the thing
        # that opens seventeen windows.
        ok, problems = engine.check_manifest(
            man, cwd_exists=os.path.isdir,
            transcript_for=engine.manifest_transcript_finder(man))
        n = len(engine.manifest_entries(man))
        saved = {e_.get("pane") for e_ in engine.manifest_entries(man) if e_.get("pane")}
        if saved:
            # asked only of a manifest that names panes: nothing else to count
            live = {p["pane"] for p in engine.panes_snapshot(direct=True).values()}
            print(f"iTerm2 has {len(saved & live)} of {len(saved)} saved panes open now.")
        if ok:
            print(f"restorable: {n} session(s) in {path}")
            print("every cwd exists, every transcript is on disk, every resume line builds.")
            return 0
        print(f"NOT fully restorable: {len(problems)} of {n} session(s) in {path}",
              file=sys.stderr)
        for who, why in problems:
            print(f"  {who}: {why}", file=sys.stderr)
        return 1

    if "--open" in argv:
        # Bulk reopen is the worst place to be blind: "open everything in this
        # manifest" run while some of it is still up starts a second process on
        # each live transcript. Every entry goes through the same guard a single
        # click does, and an unreadable fleet opens nothing at all.
        entries = engine.manifest_entries(man)
        status = {}
        live, _ = scan(cache={}, status=status)
        source_ok = status.get("source_ok", False)
        if not source_ok:
            print("ccwho restore: cannot read the live session list - not reopening"
                  " anything.", file=sys.stderr)
            print("  Reopening a session that is still running forks its conversation,"
                  " and here it would be every one of them.", file=sys.stderr)
            return 1
        openable, running, unusable, starting, seen = [], [], [], [], set()
        gone = []
        # why_held's reasons -> the entries held for each (the rest: starting)
        held = {"old list": [], "newer": [], "unreadable": [], "unresolved": [], "sending": [],
                "cut off": []}
        # from the first claim to the send, any way out - a return, an error
        # sorting the entries or in the pane lookup - lets go of what this
        # call claimed: nothing was sent
        sending = False
        try:
            # one process table for the claims, when one needs it - taken again
            # for a claim newer than it (_tables)
            tables = _tables()
            for e_ in entries:
                sid = e_.get("sessionId", "")
                if sid in seen:
                    continue          # one process per transcript, however the manifest got two
                seen.add(sid)
                action, _v = engine.resolve_open(sid, live, [e_], source_ok=True)
                if action in ("jump", "attach", "program"):
                    running.append((e_, action))
                elif action == "resume":
                    why = resume_problem(e_)
                    if why:
                        gone.append((e_, why))    # checked BEFORE the claim: not opening it
                    elif claim_launch(sid, decided_at=status.get("seen_at"), refresh=tables,
                                      decided_mono=status.get("seen_mono")):
                        if sid in _unrecorded():
                            # its claim could not be written: none of this
                            # restore will be sent - ask iTerm2 nothing
                            print(f"ccwho restore: {_not_recorded([sid])}", file=sys.stderr)
                            return 1
                        openable.append(e_)
                    else:
                        reason, left = why_held(sid)
                        held.get(reason, starting).append((e_, left))
                else:
                    unusable.append(e_)   # no resume line builds from it - say so, don't drop it
            for e_, action in running:
                if action == "program":         # running, but nothing of yours to open
                    print(f"{e_.get('project') or e_.get('sessionId')}: a program runs it")
                    continue
                print(f"already open: {e_.get('project') or e_.get('sessionId')}"
                      f" - focus it with `ccwho open {e_.get('sessionId', '')}`")
            for e_, _ in starting:
                print(f"already starting: {e_.get('project') or e_.get('sessionId')}"
                      " - another ccwho is opening it")
            for e_, why in gone:
                print(f"ccwho restore: not reopening {e_.get('project') or e_.get('sessionId')}"
                      f" - {why}", file=sys.stderr)
            for e_ in unusable:
                print(f"ccwho restore: {e_.get('project') or e_.get('sessionId') or '?'}"
                      " cannot be reopened from this manifest", file=sys.stderr)
            # each line leads with a word the list's `o` counts it by
            for reason, lead in (("unresolved", "still waiting on"), ("sending", "still waiting on"),
                                 ("cut off", "holding"),
                                 ("unreadable", "blocked"), ("newer", "changed")):
                for e_, left in held[reason]:
                    print(f"ccwho restore: {lead} {e_.get('project') or e_.get('sessionId')}"
                          f" - {_why_text(reason, e_.get('sessionId', ''), left)}", file=sys.stderr)
            if held["old list"]:
                print(f"ccwho restore: {OLD_LIST}.", file=sys.stderr)
                return 1
            waiting = (held["unresolved"] or held["sending"] or held["cut off"] or held["unreadable"]
                       or held["newer"])
            fill = {}
            if openable and any(e_.get("pane") or e_.get("tabTitle") for e_ in openable):
                # the panes iTerm2 restored: resume each session where it was
                fill = engine.match_panes(openable, engine.panes_snapshot(direct=True),
                                          engine.idle_snapshot(), saved=entries)
            script = engine.iterm_open_script(openable, fill=fill)
            if not script and (running or starting) and not (unusable or gone or waiting):
                print(f"all {len(running) + len(starting)} session(s) in that manifest"
                      " are already running or starting.")
                return 0
            if not script and waiting:
                return 1                  # said above, last: the list's `o` shows that line
            if not script:
                print("ccwho restore: nothing in that manifest can be reopened", file=sys.stderr)
                return 1
            n = script.count("create window with default profile")
            if fill:
                print(f"resuming {len(fill)} session(s) in the panes iTerm2 restored...")
            if n:
                print(f"opening {n} iTerm2 window(s)...")
            sending = True
            r, why = launch_in_iterm(script, restore_deadline(n + len(fill)),
                                     [e_.get("sessionId", "") for e_ in openable])
        finally:
            if not sending:
                drop_claims([e_.get("sessionId", "") for e_ in openable])
        if r is None:
            print(f"ccwho restore: {why}", file=sys.stderr)
            return 1
        wrote = set((r.stdout or "").split())
        filled = [e_ for e_ in openable if fill.get(e_.get("sessionId", "")) in wrote]
        missed = [e_ for e_ in openable
                  if fill.get(e_.get("sessionId", "")) not in wrote | {None}]
        if missed:
            # the script says it never wrote these: their panes closed in between
            for e_ in missed:
                print(f"{e_.get('project') or e_.get('sessionId')}: its pane closed before"
                      " ccwho could write to it - opening a new window")
            again = engine.iterm_open_script(missed)
            r2, why = launch_in_iterm(again, restore_deadline(len(missed)),
                                      [e_.get("sessionId", "") for e_ in missed])
            if r2 is None:
                print(f"ccwho restore: {why}", file=sys.stderr)
                return 1
            n += again.count("create window with default profile")
        if filled:
            print(f"filled {len(filled)} pane(s) iTerm2 restored.")
        if n:
            print(f"opened {n} window(s). each is at its project, resuming its own session.")
        return 0

    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and "--no-color" not in argv
    links = sys.stdout.isatty() and "--no-links" not in argv
    sys.stdout.write(engine.render_restore(man, color=color, links=links))
    sys.stdout.write("\nadd --open to reopen these as iTerm2 windows.\n")
    return 0


def _arg(argv, flag, default):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def _interruptible(command, args):
    """A command that may send a launch (open, restore): Ctrl-C ends it with
    exit 130 and a line, no traceback - one that came during a send says the
    launch may still run (a user who read only a traceback would take it as
    cancelled, and start the session by hand)."""
    _LOCAL.stopped_send = False
    try:
        return command(args)
    except KeyboardInterrupt:
        if _LOCAL.__dict__.pop("stopped_send", False):
            print(f"ccwho: stopped - {MAY_STILL_RUN}", file=sys.stderr)
        else:
            print("ccwho: stopped - nothing was sent", file=sys.stderr)
        return 130


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    # Bare `ccwho` on a terminal is the live list. Piped, redirected, or under
    # launchd it is the one-shot table, which is what scripts and the autosave
    # job read - and `ls` asks for the table by name.
    if not argv and sys.stdout.isatty():
        return run_ui()
    if argv and argv[0] == "jump":
        return jump(argv[1:])
    if argv and argv[0] == "reap":
        return reap(argv[1:])
    if argv and argv[0] == "kill":
        return kill_cli(argv[1:])
    if argv and argv[0] == "clean":
        return kill_cli(argv[1:], clean=True)
    if argv and argv[0] == "stop":
        return stop_cli(argv[1:])
    if argv and argv[0] == "save":
        rc = save(argv[1:])
        maybe_trim_log()          # after, so the save's own output is in what we bound
        return rc
    if argv and argv[0] == "restore":
        return _interruptible(restore, argv[1:])
    if argv and argv[0] == "ls":
        return ls(argv[1:])
    if argv and argv[0] == "ps":
        return ps(argv[1:])
    if argv and argv[0] == "statusline":
        return statusline(argv[1:])
    if argv and argv[0] == "accounts":
        return accounts(argv[1:])
    if argv and argv[0] == "doctor":
        return doctor(argv[1:])
    if argv and argv[0] == "setup":
        return setup_cmd(argv[1:])
    if argv and argv[0] == "hotkey":
        return hotkey_window(argv[1:])
    if argv and argv[0] == "show":
        return show(argv[1:])
    if argv and argv[0] == "open":
        return _interruptible(open_session, argv[1:])
    if argv and argv[0] == "url":
        return url(argv[1:])
    if "--help" in argv or "-h" in argv:
        print(__doc__.strip())
        print("\nusage: ccwho [--watch [secs]] [--blocked] [--prompt] [--json] [--no-color]")
        print("       ccwho jump <pid | tty | title substring>   focus that window (iTerm2)")
        print("       ccwho save                                 record the live fleet (BEFORE a reboot)")
        print("       ccwho restore [--open] [--from PATH]       list it back / reopen them, in their old panes")
        print("       ccwho restore --check                      would it restore? (run BEFORE you reboot)")
        print("       ccwho restore --list                       every saved manifest, and what it holds")
        print("       ccwho ls [words] [--all]                   the table, or every session matching")
        print("                                                  (--all includes sessions a program started)")
        print("       ccwho show <anything>                      what that session was working on")
        print("       ccwho ps [--port N] [--all] [--json] [--full]  what agents started, and their ports")
        print("       ccwho kill <pid>|:<port>|<session> [--dry-run] [--yes] [--force]  kill a tree, a port's holder or what a session started - lists, then asks")
        print("       ccwho stop <session> [--and-procs] [--dry-run] [--yes]  stop a background session (its conversation is kept) - asks")
        print("       ccwho clean [--mine] [--dry-run] [--yes] [--force]      kill what ended sessions left - lists, then asks")
        print("       ccwho reap [pattern] [--older-than 1h] [--kill]  leaked helpers, dry run unless --kill")
        print("       ccwho accounts [--json]                    subscription usage, per account")
        print("       ccwho accounts name <id> <label>           a display name for an account")
        print("       ccwho statusline                           Claude Code's statusLine command (records usage)")
        print("\nusage: 5h 42%↓/60% = 42% of the 5-hour budget used, 60% of the 5 hours gone;")
        print("       ↓ on pace, ↑ faster than time passes; ↻ when it resets")
        print("       ccwho doctor [--json]                      is everything ccwho needs in place?")
        print("       ccwho setup [--yes] [--hotkey KEY]         install what ccwho needs, once")
        print("       ccwho setup --no-usage | --usage           turn usage info off / back on")
        print("       ccwho open <session-id>                    focus that session, or reopen it if closed")
        print("       ccwho url  <ccwho://...>                   what the clickable links call")
        print("\nThe tty column is a clickable link when stdout is a terminal, and so is")
        print("each project name in `ccwho restore` - that one reopens the session if")
        print("its window is gone, and focuses it if it is still up.")
        print("Run install-handler.sh once to register the ccwho:// scheme; --no-links opts out.")
        return 0

    bad = unknown_flags(argv)
    if bad:
        print(f"ccwho: unknown option(s): {' '.join(bad)}", file=sys.stderr)
        print("usage: ccwho [--watch [secs]] [--blocked] [--prompt] [--json] [--no-color]",
              file=sys.stderr)
        return 2

    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and "--no-color" not in argv
    # Links are on for a terminal unless refused: a terminal that cannot render
    # OSC 8 shows the label anyway, so the downside is nil.
    links = sys.stdout.isatty() and "--no-links" not in argv
    state = {"ticks": 0, "engine_error": "", "watch": watch_requested(argv),
             "links": links}

    if "--json" in argv:
        rows, _ = scan(cache={})
        print(json.dumps(rows, indent=2))
        return 0

    if not state["watch"]:
        out, _ = tick(state, argv, color)
        sys.stdout.write(out)
        if sys.stdout.isatty():
            hint = "\033[2mone shot. `ccwho --watch` to keep it live.\033[0m\n" if color \
                else "one shot. `ccwho --watch` to keep it live.\n"
            sys.stdout.write(hint)
        return 0

    interval = parse_interval(argv)
    try:
        while True:
            started = time.time()
            out, _ = tick(state, argv, color)
            banner = ""
            if state["engine_error"]:
                banner = f"{RED if color else ''}engine reload failed: {state['engine_error']}{RESET if color else ''}\n"
            drift = doctor_banner_cached(state)
            if drift:
                # The failure this whole tool is about was silent. A watch that
                # can see something drifted and says nothing repeats it.
                banner += f"{AMBER if color else ''}⚠ {drift}{RESET if color else ''}\n"
            footer = (f"{DIM if color else ''}tick {state['ticks']} · every {interval:g}s · "
                      f"{time.strftime('%H:%M:%S')} · ctrl-c to stop{RESET if color else ''}\n")
            saved = ""
            if should_autosave(state.get("last_save"), time.time(), AUTOSAVE_SECS):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    if save([]) == 0:
                        state["last_save"] = time.time()
                saved = (f"{DIM if color else ''} · restore manifest saved"
                         f"{RESET if color else ''}")
            sys.stdout.write(CLEAR_HOME + banner + out + "\n" + footer.rstrip("\n") + saved + "\n")
            sys.stdout.flush()
            time.sleep(max(0.0, interval - (time.time() - started)))
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
