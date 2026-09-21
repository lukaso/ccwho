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
import importlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import time
import traceback

import ccwho_engine as engine
import ccwho_setup as setup

CLEAR_HOME = "\033[H\033[2J"
DIM, RESET, RED = "\033[2m", "\033[0m", "\033[31m"
AMBER = "\033[33;1m"


def _load_beside(module):
    """Import a module's file into a NEW module object, leaving the live one alone.

    importlib.reload() executes the new source INTO the module everyone is holding.
    An edit that parses but raises half way through - a typo in a rule, a bad
    constant - leaves half the new definitions live and half the old ones, and
    catching the exception does not undo that. The watch then extracts with a
    module that is neither version, which is worse than not reloading at all.
    Building the candidate beside the old one means a failure changes nothing.
    """
    spec = importlib.util.spec_from_file_location(module.__name__, module.__file__)
    candidate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(candidate)          # raises before anything is swapped
    return candidate


def reload_engine(state):
    """Re-read the engine. A broken edit keeps the last good module and says so.

    ccwho_brief goes first: it holds half the extraction rules and the engine
    imports it, so reloading only the engine would leave a running watch showing
    the OLD rules while its file on disk says otherwise - a fix that looks like
    it did nothing is worse than no hot reload at all.
    """
    try:
        new_brief = _load_beside(engine.brief)
        sys.modules[new_brief.__name__] = new_brief
        try:
            importlib.reload(engine)
        except Exception:
            sys.modules[engine.brief.__name__] = engine.brief   # put the old one back
            raise
        state["engine_error"] = ""
    except Exception:
        state["engine_error"] = traceback.format_exc(limit=2).strip().splitlines()[-1]
    return engine


def tick(state, argv, color):
    eng = reload_engine(state) if state["watch"] else engine
    rows, total_orphans = eng.collect(cache=state.setdefault("cache", {}))
    if "--blocked" in argv:
        rows = [r for r in rows if r["status"] == "waiting" or r["orphans"]]
    out = eng.render(rows, total_orphans, color=color,
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
    rows, _ = engine.collect(cache={})
    hits = engine.match_rows(rows, query) if query else rows
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
    row = hits[0]
    head, tail, _mtime = engine.read_windows(row.get("sessionId", ""))
    b = engine.brief.build(head, tail, session=row,
                           now=engine.now_iso(), as_records=engine.as_records)
    if want_json:
        print(json.dumps(b, indent=2))
        return 0
    print(engine.render_brief(b, row, color=sys.stdout.isatty()
                              and "NO_COLOR" not in os.environ))
    return 0


def doctor(argv):
    """What is installed, what drifted, and what to run about it.

    Read-only by design. The failure this exists for - iTerm2's own status hook
    vanishing from settings.json - was silent for weeks, and a tool that repairs
    another tool's config without asking is how that happened in the first place.
    """
    checks = setup.doctor_checks(setup.gather(ccwho_dir=ccwho_dir()))
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
        line = setup.doctor_banner(setup.doctor_checks(setup.gather(ccwho_dir=ccwho_dir())))
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

    Same idiom as ccgate's slots: O_CREAT|O_EXCL under $HOME, no daemon. A claim
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
    if action == "live-no-window":
        print(f"ccwho open: {sid} is running (pid {value or '?'}) but has no iTerm2"
              " window - not reopening it, that would fork the conversation.",
              file=sys.stderr)
        return 1
    if action == "unknown":
        print(f"ccwho open: {value} - not reopening anything.", file=sys.stderr)
        print("  Check that `claude` is on PATH and `claude agents --json` answers.",
              file=sys.stderr)
        return 1
    if action == "resume":
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
        print(f"  {h['pid']:<8} {h['age'] // 3600}h  {h['command'][:88]}")
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


def restore_dir():
    return os.path.join(ccwho_dir(), "restore")


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
    man = engine.manifest_from_rows(rows)
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
    note = f", {man['skipped']} not capturable" if man["skipped"] else ""
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
            man, cwd_exists=os.path.isdir, transcript_for=engine.transcript_path)
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
        for e_ in entries:
            sid = e_.get("sessionId", "")
            if sid in seen:
                continue          # one process per transcript, however the manifest got two
            seen.add(sid)
            action, _v = engine.resolve_open(sid, live, [e_], source_ok=True)
            if action in ("jump", "live-no-window"):
                running.append(e_)
            elif action == "resume":
                if claim_launch(sid):
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
        for e_ in unusable:
            print(f"ccwho restore: {e_.get('project') or e_.get('sessionId') or '?'}"
                  " cannot be reopened from this manifest", file=sys.stderr)
        script = engine.iterm_open_script(openable)
        if not script and (running or starting) and not unusable:
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
    if argv and argv[0] == "doctor":
        return doctor(argv[1:])
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
        print("       ccwho show <anything>                      what that session was working on")
        print("       ccwho doctor [--json]                      is everything ccwho needs in place?")
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
