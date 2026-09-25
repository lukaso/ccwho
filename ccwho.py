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
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

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
        state["engine_error"] = ""
    except Exception:
        state["engine_error"] = traceback.format_exc(limit=2).strip().splitlines()[-1]
    return engine


def tick(state, argv, color):
    eng = reload_engine(state) if state["watch"] else engine
    rows, fleet = eng.collect(cache=state.setdefault("cache", {}))
    if "--blocked" in argv:
        rows = [r for r in rows if r["status"] == "waiting" or r["orphans"]]
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


def jump(argv):
    """Focus the terminal window holding a session. Needs iTerm2."""
    query = " ".join(a for a in argv if not a.startswith("-"))
    rows, _ = engine.collect(cache={})
    hits = engine.match_rows(rows, query)
    if not hits:
        print(f"ccwho: no session matches {query!r}", file=sys.stderr)
        return 1
    if len(hits) > 1:
        print(f"ccwho: {query!r} matches {len(hits)} sessions - be more specific:",
              file=sys.stderr)
        for r in hits:
            print(f"  {engine.short_tty(r.get('tty','')):<6} {r.get('title') or r.get('name')}",
                  file=sys.stderr)
        return 2
    row = hits[0]
    tty = row.get("tty", "")
    if not tty:
        print(f"ccwho: {row.get('title')} has no controlling terminal", file=sys.stderr)
        return 1
    script = os.path.join(os.path.dirname(os.path.realpath(__file__)), "jump.applescript")
    res = subprocess.run(["osascript", script, f"/dev/{tty}"],
                         capture_output=True, text=True)
    out = (res.stdout or res.stderr).strip()
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
    rows, _ = engine.collect(cache=cache)
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
        for r in hits:
            print(f"  {engine.brief.short_id(r.get('sessionId','')):<6}"
                  f" {engine.short_tty(r.get('tty','')):<6}"
                  f" {r.get('title') or r.get('name')}", file=sys.stderr)
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
        install_hotkey(home, hotkey, prove=False, iterm_ok=facts.get("iterm_ok"))
    else:
        rc = install_hotkey(home, hotkey, prove=not yes and facts.get("iterm_ok"),
                            iterm_ok=facts.get("iterm_ok"))
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


def install_hotkey(home, hotkey, prove=True, iterm_ok=True, deadline=30.0):
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
        if not iterm_ok:
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
    by_id = {r.get("sessionId"): r for r in rows}
    hits = list(engine.match_rows(rows, query))
    seen = {r.get("sessionId") for r in hits}
    now = engine.now_iso()
    ended = []
    for entry in index.search(fresh_index(), query, live_ids=set(by_id),
                              everything=everything):
        sid = entry.get("sessionId")
        if sid in seen:
            continue
        seen.add(sid)
        if sid in by_id:
            hits.append(by_id[sid])          # running, found by what it is about
        else:
            ended.append(_ended_row(entry, now))
    hits.sort(key=engine.sort_key)
    return hits, ended


def ls(argv):
    """The table. With words, the sessions that match - including ended ones."""
    query = " ".join(a for a in argv if not a.startswith("-"))
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and "--no-color" not in argv
    rows, fleet = engine.collect(cache={})
    if not query:
        sys.stdout.write(engine.render(rows, fleet, color=color))
        return 0
    live, ended = matches(query, rows, everything="--all" in argv)
    if not live and not ended:
        print(f"ccwho: no session matches {query!r}", file=sys.stderr)
        return 1
    if live:
        sys.stdout.write(engine.render(live, 0, color=color))
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


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def claim_launch(session_id, pid=None, now=None, alive=_pid_alive):
    """Claim the right to start THIS session, across processes. True if we got it.

    resolve_open() reads the world and then the caller launches. Another ccwho -
    a second click, a `restore --open` in another window - can read the same world
    in between and launch the same session, which is the fork the whole guard
    exists to prevent. A new session also takes a moment to appear in
    `claude agents --json`, so the window is real even for one hurried human.

    O_CREAT|O_EXCL under $HOME, no daemon. A claim
    whose owner died, whose record is unreadable, or that is older than the launch
    itself could take is reclaimed - a crashed launcher must not lock a session
    out forever.
    """
    now = time.time() if now is None else now
    pid = os.getpid() if pid is None else pid
    d = os.path.join(ccwho_dir(), "launching")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{session_id}.json")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            with open(path) as fh:
                rec = json.load(fh)
            held = alive(rec.get("pid")) and (now - float(rec.get("since", 0))
                                              < LAUNCH_CLAIM_SECONDS)
        except (OSError, ValueError, TypeError):
            held = False          # unreadable claim is no claim
        if held:
            return False
        try:
            os.unlink(path)
        except OSError:
            pass
        return claim_launch(session_id, pid=pid, now=now, alive=alive)
    except OSError:
        return True               # cannot claim at all: do not block the user
    with os.fdopen(fd, "w") as fh:
        json.dump({"pid": pid, "since": now, "sessionId": session_id}, fh)
    return True


def resume_problem(entry):
    """Why `claude --resume` would fail for this saved session, or "".

    Asked by everything that launches one - `open` and `restore --open` - right
    before it does. A saved cwd in a temp dir is gone after the next cleanup, and
    a headless worker leaves no transcript: both opened a window onto an error.
    """
    finder = engine.manifest_transcript_finder({"sessions": [entry]})
    return engine.entry_problem(entry, os.path.isdir, finder)


def open_session(argv):
    """Get me to this session: focus its window if it is up, reopen it if not.

    The verb is decided HERE, against the world at click time, not baked into the
    link when the list was printed. After a reboot every link in a restore list
    resolves to "reopen"; ten minutes later the same link focuses the window it
    just made.
    """
    sid = (argv[0] if argv else "").strip()
    status = {}
    rows, _ = engine.collect(cache={}, status=status)
    action, value = engine.resolve_open(sid, rows, known_entries(),
                                        source_ok=status.get("source_ok", False))
    if action == "jump":
        return jump([value])
    if action == "attach":
        # Running, with no window: give it one. `claude attach` opens a session
        # that is already running; reopening it would fork the conversation.
        res = subprocess.run(["osascript", "-e", engine.iterm_run_script(value)],
                             capture_output=True, text=True)
        if res.returncode != 0:
            print("ccwho open: could not open a window: %s"
                  % (res.stderr or "").strip(), file=sys.stderr)
            return 1
        print(f"attached {sid} in a new window")
        return 0
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
        if not claim_launch(sid):
            print(f"ccwho open: {sid} is already starting in another window"
                  " - not launching it twice.", file=sys.stderr)
            return 1
        res = subprocess.run(["osascript", "-e", engine.iterm_run_script(value)],
                             capture_output=True, text=True)
        if res.returncode != 0:
            print("ccwho open: could not open a window: %s"
                  % (res.stderr or "").strip(), file=sys.stderr)
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
    rows, fleet = engine.collect(cache={})
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
            print(f"ccwho ps: :{port} is held by a process whose environment could not"
                  f" be read - who started it is not known", file=sys.stderr)
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
        rec = usage.record(payload, os.environ, setup_home(), time.time(), previous)
        if rec is None:
            return 0
        os.makedirs(d, exist_ok=True)
        write_atomic(path, json.dumps(rec))
    except Exception:             # noqa: BLE001 - see the docstring
        pass
    return 0


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


def usage_facts(now=None):
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
            rc |= guarded_write(path, before, new)
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
    rows, _ = engine.collect(cache={}, status=status)
    if not status.get("source_ok", True):
        print("ccwho save: cannot reach `claude agents` - nothing written.", file=sys.stderr)
        print("  A save that cannot ask must not answer: an empty manifest would be",
              file=sys.stderr)
        print("  kept as the newest and would evict the record of what you had open.",
              file=sys.stderr)
        print("  Check that `claude` is on PATH for whoever ran this.", file=sys.stderr)
        return 1
    man = engine.manifest_from_rows(rows, why_not=save_problem)
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


def reopen_saved():
    """What `o` in the list does: the same restore the command line runs.

    Not a second implementation - every guard that stops a bulk reopen forking
    live conversations lives in restore(), and one of those written twice is
    one of them wrong.
    """
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = restore(["--open"])
    if rc:
        tail = [l for l in out.getvalue().splitlines() if l.strip()]
        return "could not reopen: " + (tail[-1] if tail else "see `ccwho restore --open`")
    text = out.getvalue()
    m = re.search(r"^opened (\d+) window", text, re.M)
    left = sum(1 for l in text.splitlines() if l.startswith("ccwho restore: not reopening"))
    said = f"reopened {m.group(1)} session(s)" if m else "reopened the last save"
    return said + (f", {left} left out - they could not resume" if left else "")


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
        print(f"ccwho restore: cannot read {path}: {ex}", file=sys.stderr)
        return 1

    if "--check" in argv:
        # Deliberately BEFORE --open: a check must never launch anything, even if
        # both flags are given. Answering "would this work?" cannot be the thing
        # that opens seventeen windows.
        ok, problems = engine.check_manifest(
            man, cwd_exists=os.path.isdir,
            transcript_for=engine.manifest_transcript_finder(man))
        n = len(engine.manifest_entries(man))
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
        live, _ = engine.collect(cache={}, status=status)
        source_ok = status.get("source_ok", False)
        if not source_ok:
            print("ccwho restore: cannot read the live session list - not reopening"
                  " anything.", file=sys.stderr)
            print("  Reopening a session that is still running forks its conversation,"
                  " and here it would be every one of them.", file=sys.stderr)
            return 1
        openable, running, unusable, starting, seen = [], [], [], [], set()
        gone = []
        for e_ in entries:
            sid = e_.get("sessionId", "")
            if sid in seen:
                continue          # one process per transcript, however the manifest got two
            seen.add(sid)
            action, _v = engine.resolve_open(sid, live, [e_], source_ok=True)
            if action in ("jump", "attach"):
                running.append(e_)
            elif action == "resume":
                why = resume_problem(e_)
                if why:
                    gone.append((e_, why))    # checked BEFORE the claim: not opening it
                elif claim_launch(sid):
                    openable.append(e_)
                else:
                    starting.append(e_)   # another ccwho is already opening this one
            else:
                unusable.append(e_)   # no resume line builds from it - say so, don't drop it
        for e_ in running:
            print(f"already open: {e_.get('project') or e_.get('sessionId')}"
                  f" - focus it with `ccwho open {e_.get('sessionId', '')}`")
        for e_ in starting:
            print(f"already starting: {e_.get('project') or e_.get('sessionId')}"
                  " - another ccwho is opening it")
        for e_, why in gone:
            print(f"ccwho restore: not reopening {e_.get('project') or e_.get('sessionId')}"
                  f" - {why}", file=sys.stderr)
        for e_ in unusable:
            print(f"ccwho restore: {e_.get('project') or e_.get('sessionId') or '?'}"
                  " cannot be reopened from this manifest", file=sys.stderr)
        script = engine.iterm_open_script(openable)
        if not script and (running or starting) and not (unusable or gone):
            print(f"all {len(running) + len(starting)} session(s) in that manifest"
                  " are already running or starting.")
            return 0
        if not script:
            print("ccwho restore: nothing in that manifest can be reopened", file=sys.stderr)
            return 1
        n = script.count("create window with default profile")
        print(f"opening {n} iTerm2 window(s)...")
        try:
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        except (OSError, subprocess.SubprocessError) as ex:
            print(f"ccwho restore: could not drive iTerm2: {ex}", file=sys.stderr)
            return 1
        if r.returncode != 0:
            print(f"ccwho restore: iTerm2 refused: {r.stderr.strip()}", file=sys.stderr)
            return r.returncode
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
    if argv and argv[0] == "save":
        rc = save(argv[1:])
        maybe_trim_log()          # after, so the save's own output is in what we bound
        return rc
    if argv and argv[0] == "restore":
        return restore(argv[1:])
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
        return open_session(argv[1:])
    if argv and argv[0] == "url":
        return url(argv[1:])
    if "--help" in argv or "-h" in argv:
        print(__doc__.strip())
        print("\nusage: ccwho [--watch [secs]] [--blocked] [--prompt] [--json] [--no-color]")
        print("       ccwho jump <pid | tty | title substring>   focus that window (iTerm2)")
        print("       ccwho save                                 record the live fleet (BEFORE a reboot)")
        print("       ccwho restore [--open] [--from PATH]       list it back / reopen the windows")
        print("       ccwho restore --check                      would it restore? (run BEFORE you reboot)")
        print("       ccwho restore --list                       every saved manifest, and what it holds")
        print("       ccwho ls [words] [--all]                   the table, or every session matching")
        print("                                                  (--all includes sessions a program started)")
        print("       ccwho show <anything>                      what that session was working on")
        print("       ccwho ps [--port N] [--all] [--json] [--full]  what agents started, and their ports")
        print("       ccwho accounts [--json]                    subscription usage, per account")
        print("       ccwho accounts name <id> <label>           a display name for an account")
        print("       ccwho statusline                           Claude Code's statusLine command (records usage)")
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
        rows, _ = engine.collect(cache={})
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
