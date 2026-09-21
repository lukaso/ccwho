#!/usr/bin/env python3
"""What is installed, what drifted, and what to run about it.

`ccwho doctor` exists because of a measured failure: iTerm2's own Claude Code
integration stopped working and nothing said so. Its hook had been dropped from
~/.claude/settings.json by some other tool's rewrite, and the only symptom was a
feature quietly not happening. Every dependency ccwho has can fail that way - a
launchd job that is loaded but has not run since Tuesday, a URL handler nobody
registered on this machine, iTerm2 not scriptable - so each one gets a check that
says what it looked at, and the line to run when it is wrong.

The checks are PURE over injected facts. Gathering facts touches the world;
deciding what they mean does not, which is the half worth testing.

R1 doctor is read-only. It never writes another tool's settings file: that is a
Release 2 decision with its own diff-and-confirm rules.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

# The launchd timer is 15 minutes. Two windows of grace before calling it stale:
# StartInterval only fires while the Mac is awake, so one skipped window is normal.
AUTOSAVE_STALE_AFTER = 31 * 60

_AGE_UNITS = ((86400.0, "d"), (3600.0, "h"), (60.0, "m"))


def age_words(secs):
    """Seconds as ccwho spells ages elsewhere. None is "never", not blank."""
    if secs is None:
        return "never"
    for size, unit in _AGE_UNITS:
        if secs >= size:
            return f"{int(secs // size)}{unit}"
    return f"{int(secs)}s"


def doctor_checks(facts):
    """Every dependency, in the order that matters if more than one is broken.

    `claude` first: without it there is no session list, and every other answer
    is about a tool that cannot do its job anyway.
    """
    out = []

    claude = facts.get("claude") or ""
    out.append(_check(
        "claude", bool(claude),
        f"found at {claude}" if claude else "not on PATH - no session list at all",
        "install Claude Code, or add it to PATH (launchd jobs get a minimal PATH)"))

    out.append(_check(
        "iterm2", bool(facts.get("iterm_ok")),
        "scriptable" if facts.get("iterm_ok") else
        "not running or not scriptable - no tab names, and `go` cannot focus a window",
        "start iTerm2, and allow it under System Settings > Privacy > Automation"))

    out.append(_check(
        "ccwho:// handler", bool(facts.get("handler_registered")),
        "registered" if facts.get("handler_registered") else
        "not registered - the clickable links in the list do nothing",
        "bash install-handler.sh"))

    out.append(_check(
        "autosave job", bool(facts.get("launchd_loaded")),
        "loaded" if facts.get("launchd_loaded") else
        "not loaded - nothing records the fleet, and a reboot is a one-way door",
        "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/"
        "com.lukaso.ccwho.save.plist"))

    # "Did the job RUN", not "is there a recent manifest". `save` writes nothing
    # when no session is restorable, so an old manifest on a quiet machine means
    # you had nothing open - not that the timer is dead. The job writes a line to
    # its log either way, so the log's age is the honest signal.
    ran = facts.get("last_run_age")
    fresh = ran is not None and ran < AUTOSAVE_STALE_AFTER
    man = facts.get("newest_manifest_age")
    out.append(_check(
        "autosave freshness", fresh,
        f"job ran {age_words(ran)} ago, newest manifest {age_words(man)} old"
        + ("" if fresh else " - the job is loaded but not running"),
        "check the job: launchctl print gui/$(id -u)/com.lukaso.ccwho.save"))

    out.append(_check(
        "iTerm2 status hook", bool(facts.get("cc_status_hook")),
        "present in " + str(facts.get("settings_path") or "settings.json")
        if facts.get("cc_status_hook") else
        "missing from " + str(facts.get("settings_path") or "settings.json")
        + " - iTerm2's own session status stopped updating",
        "re-run iTerm2's Claude Code integration setup (it rewrites settings.json;"
        " ccwho will not write that file for you)"))

    return out


def _check(name, ok, detail, fix):
    return {"name": name, "ok": bool(ok), "detail": detail, "fix": "" if ok else fix}


def doctor_verdict(results):
    """0 when everything is fine, 1 when anything is not."""
    return 0 if all(r["ok"] for r in results) else 1


def doctor_banner(results):
    """One line for a header that has room for one line: the first fault, in the
    order doctor_checks puts them, plus how to see the rest."""
    for r in results:
        if not r["ok"]:
            return f"{r['name']}: {r['detail']} - run `ccwho doctor`"
    return ""


# ------------------------------------------------------- gathering the facts
# Everything below reaches the world. Each one answers with a plain value and
# never raises: a doctor that dies on the machine it is diagnosing is no use.

def gather(ccwho_dir=None, settings_path=None, now=None):
    return {
        "claude": shutil.which("claude") or "",
        "iterm_ok": iterm_scriptable(),
        "handler_registered": handler_registered(),
        "launchd_loaded": launchd_loaded(),
        "last_run_age": last_run_age(ccwho_dir, now=now),
        "newest_manifest_age": newest_manifest_age(ccwho_dir, now=now),
        "cc_status_hook": cc_status_hook(settings_path),
        "settings_path": settings_path or os.path.expanduser("~/.claude/settings.json"),
    }


def iterm_scriptable(timeout=5.0):
    """Can we ASK iTerm2 something? Not "is it running".

    Automation access is granted per application pair and can be refused; a
    process list says nothing about it, and refused automation is exactly when
    the tab names and `go` stop working. So ask iTerm2 itself, for the cheapest
    thing it knows, and treat any failure as no.
    """
    try:
        done = subprocess.run(
            ["osascript", "-e", 'tell application "iTerm2" to count windows'],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and done.stdout.strip().isdigit()


def handler_candidates(app_path=None):
    """Where the applet may live, best first. install-handler.sh takes a path
    argument, so the default location is not the only supported one."""
    if app_path:
        return [app_path]
    return [p for p in (os.environ.get("CCWHO_JUMP_APP", ""),
                        os.path.expanduser("~/Applications/ccwho-jump.app"),
                        "/Applications/ccwho-jump.app") if p]


def handler_registered(app_path=None, timeout=5.0):
    """Is the ccwho:// applet installed and claiming the scheme?

    Read the bundle, not LaunchServices. The first version of this check asked
    for a bundle id that install-handler.sh never sets, and reported "not
    registered" on a machine where the links worked - a check that cries wolf is
    how a real fault gets ignored later.

    It proves the applet exists and claims `ccwho`. LaunchServices could still
    prefer another handler; the honest cheap answer is the one we can get.
    """
    plist = ""
    for app in handler_candidates(app_path):
        p = os.path.join(app, "Contents", "Info.plist")
        if os.path.exists(p):
            plist = p
            break
    if not plist:
        return False
    try:
        done = subprocess.run(["plutil", "-extract", "CFBundleURLTypes", "json",
                               "-o", "-", plist],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    if done.returncode != 0:
        return False
    try:
        types = json.loads(done.stdout)
    except ValueError:
        return False
    for t in types if isinstance(types, list) else []:
        if "ccwho" in (t or {}).get("CFBundleURLSchemes", []):
            return True
    return False


def launchd_loaded(timeout=5.0):
    try:
        done = subprocess.run(["launchctl", "list"], capture_output=True,
                              text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and "com.lukaso.ccwho.save" in done.stdout


def newest_manifest_age(ccwho_dir=None, now=None):
    """Seconds since the newest saved manifest, or None if there is not one.

    The autosave prunes while this reads: a name from listdir can be gone before
    stat sees it. That used to escape doctor and take the watch loop with it.
    """
    d = os.path.join(ccwho_dir or os.path.expanduser("~/.ccwho"), "restore")
    try:
        names = [os.path.join(d, n) for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        return None
    return _newest_age(names, now)


def last_run_age(ccwho_dir=None, now=None):
    """Seconds since the autosave job last RAN, or None if it never has here.

    The launchd job redirects its output to this log, and `save` prints a line
    whether it wrote a manifest or found nothing to write - so the log's age is
    "did the timer fire", which a manifest's age is not.
    """
    log = os.path.join(ccwho_dir or os.path.expanduser("~/.ccwho"), "autosave.log")
    return _newest_age([log], now)


def _newest_age(paths, now=None):
    times = []
    for p in paths:
        try:
            times.append(os.path.getmtime(p))
        except OSError:
            continue              # pruned between listing and stat, or never there
    if not times:
        return None
    return max(0.0, (time.time() if now is None else now) - max(times))


def cc_status_hook(settings_path=None):
    """Is iTerm2's own Claude Code hook still in settings.json?

    Not ccwho's feature, and ccwho does not depend on it - but it is the exact
    failure that started all of this, and a tool that can see a neighbour's
    silent breakage should say so.
    """
    path = settings_path or os.path.expanduser("~/.claude/settings.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return False
    return "cc-status" in json.dumps(data.get("hooks", {}))
