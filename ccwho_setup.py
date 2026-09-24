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
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
import xml.sax.saxutils

import ccwho_engine as engine

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

    # ccwho finds sessions in other config dirs by reading Claude Code's own
    # session files. Unreadable ones mean that format moved, and those sessions
    # quietly drop out of the list again.
    bad = facts.get("session_files_bad")
    out.append(_check(
        "session files", not bad,
        "not looked at" if bad is None else
        "readable" if not bad else
        "the scan itself could not run - sessions in other config dirs are missing"
        if bad == "error" else
        f"{bad} could not be read - sessions in other config dirs may be missing",
        "" if not bad else
        "update ccwho (brew upgrade ccwho); if it persists, report it" if bad == "error"
        else "update ccwho (brew upgrade ccwho); if it persists, Claude Code changed the"
        " file format - report it"))

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

    reach = hotkey_reach(facts)
    out.append(_check("hotkey reach", reach["ok"], reach["detail"], reach["fix"]))

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
        # not shutil.which: a hotkey window and a launchd job both run without
        # a login shell, and `claude` is not on the PATH either of them gets.
        "claude": engine.find_tool("claude"),
        "session_files_bad": _session_files_bad(),
        "iterm_ok": iterm_scriptable(),
        "handler_registered": handler_registered(),
        "launchd_loaded": launchd_loaded(),
        "last_run_age": last_run_age(ccwho_dir, now=now),
        "newest_manifest_age": newest_manifest_age(ccwho_dir, now=now),
        "cc_status_hook": cc_status_hook(settings_path),
        # the hotkey is only global if iTerm2 had Accessibility when it
        # registered the key grab - see hotkey_reach
        "accessibility": accessibility_ok(),
        "iterm_granted_at": accessibility_granted_at(),
        "iterm_started_at": iterm_started_at(),
        "settings_path": settings_path or os.path.expanduser("~/.claude/settings.json"),
    }


def _session_files_bad():
    """How many session files could not be used; "error" if the scan itself
    failed - doctor must still run, and say so, on exactly the machine where
    something is wrong."""
    try:
        return engine.live_file_sessions()[1]
    except Exception:
        return "error"


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


# ------------------------------------------------- the key that reaches you
# A hotkey that fires while another app is in front is a GLOBAL key grab, and
# macOS only gives one to an app with Accessibility - as of the moment the app
# registers it. Measured on this machine: iTerm2 started on the 18th, was
# granted Accessibility on the 21st, and the key worked only while iTerm2 was
# already frontmost. Nothing about the profile was wrong.

ITERM_BUNDLE = "com.googlecode.iterm2"
TCC_DB = "/Library/Application Support/com.apple.TCC/TCC.db"

# Asking System Events for anything at all requires the permission, so this
# doubles as the request: a process with no Accessibility gets the standard
# macOS dialog, attributed to the app it is running inside - which is iTerm2.
_AX_PROBE = 'tell application "System Events" to return name of first process'


def accessibility_ok(timeout=10.0):
    """True, False, or None when the answer was neither.

    None matters: "we could not tell" must never be reported as "it is broken".
    """
    try:
        done = subprocess.run(["osascript", "-e", _AX_PROBE],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode == 0:
        return True
    if "assistive access" in (done.stderr or "") or "-1728" in (done.stderr or ""):
        return False
    return None


def ask_for_accessibility(timeout=60.0):
    """Raise the macOS dialog, the way any app asks for it: by trying.

    macOS shows it once per app until the person answers; afterwards the
    attempt simply fails. So the caller says where the switch is as well.
    """
    return accessibility_ok(timeout=timeout)


def accessibility_granted_at(bundle_id=ITERM_BUNDLE, db=None, timeout=10.0):
    """WHEN the permission was granted, or None when we could not look.

    Reading it needs Full Disk Access, which most machines will not have given
    us, so None is the ordinary answer and must stay harmless.
    """
    try:
        done = subprocess.run(
            ["sqlite3", db or TCC_DB,
             "select last_modified from access where service="
             "'kTCCServiceAccessibility' and auth_value=2 and client="
             f"'{bundle_id}'"],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    line = (done.stdout or "").strip().splitlines()
    if done.returncode != 0 or not line:
        return None
    try:
        return float(line[0])
    except ValueError:
        return None


def iterm_started_at(pid=None, timeout=10.0):
    """When the running iTerm2 started, in epoch seconds. None if it is not."""
    if pid is None:
        try:
            found = subprocess.run(["pgrep", "-x", "iTerm2"],
                                   capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None
        pids = (found.stdout or "").split()
        if not pids:
            return None
        pid = int(pids[0])
    try:
        done = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    stamp = (done.stdout or "").strip()
    if done.returncode != 0 or not stamp:
        return None
    try:
        import email.utils
        parsed = time.strptime(stamp, "%a %d %b %H:%M:%S %Y")
        return time.mktime(parsed)
    except ValueError:
        return None


def hotkey_reach(facts):
    """Can the hotkey reach you from another app?

    Three answers, because they need three different things from you: ask for
    the permission, restart iTerm2 so it can use one it has, or nothing.
    """
    allowed = facts.get("accessibility")
    if allowed is False:
        return _reach(False, "ask",
                      "iTerm2 has no Accessibility permission, so the hotkey"
                      " can only work while iTerm2 is already in front",
                      "grant it when macOS asks, or System Settings >"
                      " Privacy & Security > Accessibility > iTerm")
    granted, started = facts.get("iterm_granted_at"), facts.get("iterm_started_at")
    if allowed and granted and started and granted > started:
        return _reach(False, "restart",
                      "iTerm2 was granted Accessibility after it started, so"
                      " the hotkey only works while iTerm2 is in front",
                      "restart iTerm2 once (ccwho save first: it ends every"
                      " session running in it)")
    return _reach(True, "", "the hotkey can reach you from any app", "")


def _reach(ok, do, detail, fix):
    return {"ok": ok, "do": do, "detail": detail, "fix": fix}


# ---------------------------------------------------------------- installing
# `ccwho setup` is the one command a new machine needs. Everything it decides
# lives here as a pure function over facts; everything it WRITES is in the
# runner, where the user can see it happen and say no.

PLIST_TEMPLATE_NAME = "com.lukaso.ccwho.save.plist.template"
PLIST_LABEL = "com.lukaso.ccwho.save"

# launchd starts with almost nothing on PATH. These are the directories every
# Mac has; the one that matters - wherever `claude` actually lives - is added in
# front of them, because without it the save finds no sessions and says it
# worked.
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def render_plist(template, home, ccwho, claude, python=None):
    """The autosave job, addressed to this machine.

    Everything the job needs is passed in rather than looked up, because the
    thing that breaks is a path that was true where the file was written and
    false where it runs. Every value is XML-escaped on the way in: a home like
    /Users/sam/R&D is a perfectly good directory and a broken plist.
    """
    claude_dir = os.path.dirname(claude) if claude else ""
    path = (claude_dir + ":" + SYSTEM_PATH) if claude_dir else SYSTEM_PATH
    out = template
    for key, value in (("{{PYTHON}}", python or sys.executable or "/usr/bin/python3"),
                       ("{{CCWHO}}", ccwho),
                       ("{{PATH}}", path),
                       ("{{LOG}}", os.path.join(home, ".ccwho", "autosave.log"))):
        out = out.replace(key, xml.sax.saxutils.escape(value))
    return out


def plist_problems(text, home, claude):
    """What is wrong with a rendered job, before it is installed.

    Both faults here have happened: a home directory belonging to whoever wrote
    the file, and a PATH that cannot find `claude`. Both produce a job that
    loads, runs, finds nothing, and exits 0.
    """
    problems = []
    try:
        plistlib.loads(text.encode())
    except Exception as exc:
        problems.append(f"not a plist launchd can read: {exc}")
    for match in re.findall(r"/Users/[^<:\s\"]+", text):
        if not match.startswith(home.rstrip("/") + "/") and match != home:
            problems.append(f"path belongs to another user: {match}")
    if "{{" in text:
        problems.append("a placeholder was never filled in")
    claude_dir = os.path.dirname(claude) if claude else ""
    for path in re.findall(r"<string>([^<]*:[^<]*)</string>", text):
        if claude_dir and claude_dir not in path.split(":"):
            problems.append(f"PATH cannot find claude ({claude_dir} is not in it)")
    return problems


# What the job actually DOES - not the comments around it. Two installs that
# run the same program with the same environment are the same job.
_JOB_KEYS = ("ProgramArguments", "EnvironmentVariables", "StandardOutPath",
             "StandardErrorPath", "StartInterval", "RunAtLoad", "Label")


def plist_matches(installed, rendered):
    """Is the job on this machine the job we would write today?

    `launchctl list` says "loaded" for a job whose paths moved out from under
    it - it loads, runs, finds nothing and exits 0. This is the check that
    catches that, so a stale job reads as work to do rather than as done.
    """
    def read(text):
        try:
            doc = plistlib.loads((text or "").encode())
        except Exception:
            return None
        if not isinstance(doc, dict):
            return None
        return {k: doc.get(k) for k in _JOB_KEYS}

    a, b = read(installed), read(rendered)
    return a is not None and a == b


def handler_target(script):
    """Which ccwho the ccwho:// applet calls, read from its own source.

    An applet can claim the scheme while calling a copy that has been deleted or
    renamed: that reads as "registered" and does nothing when clicked.
    """
    m = re.search(r'quoted form of "((?:[^"\\]|\\.)+)"', script or "")
    if not m:
        return ""
    # What comes back is an AppleScript literal: a path holding a quote or a
    # backslash is escaped inside it. Undo that, or this never matches the real
    # path and setup rebuilds the applet on every run.
    return re.sub(r"\\(.)", r"\1", m.group(1))


# ------------------------------------------------------------- the hotkey
# Key names read out of iTerm.app 3.7 (`strings`), not guessed: iTerm2 ignores
# a key it does not know, which would look exactly like a hotkey that does not
# work.
OPTION = 524288                  # NSEventModifierFlagOption
PROFILE_GUID = "ccwho-hotkey-window"
PROFILE_NAME = "ccwho"

HOTKEYS = {
    # instead_of: what the key used to type. Setup says this out loud before it
    # takes the key away from you.
    "option-slash": {"label": "⌥/", "code": 44, "plain": "/",
                     "chars": "÷", "instead_of": "÷"},
    "option-space": {"label": "⌥space", "code": 49, "plain": " ",
                     "chars": " ", "instead_of": "a non-breaking space"},
    "option-w":     {"label": "⌥w", "code": 13, "plain": "w",
                     "chars": "∑", "instead_of": "∑"},
}
DEFAULT_HOTKEY = "option-slash"


# The window runs `ccwho hotkey`, not `ccwho`: started by a key rather than by
# a person, it has to hold itself open long enough to show what went wrong.
HOTKEY_VERB = "hotkey"


def window_command(ccwho):
    """What the hotkey window runs once the key is proved."""
    return f"{shlex.quote(ccwho)} {HOTKEY_VERB}"


def hotkey_profile(command, hotkey=DEFAULT_HOTKEY, name=PROFILE_NAME,
                   guid=PROFILE_GUID):
    """A Dynamic Profile iTerm2 loads by itself - no preferences to edit.

    The Guid is fixed, so running setup again REPLACES this profile instead of
    leaving a second one behind with the same key.
    """
    key = HOTKEYS[hotkey]
    return {"Profiles": [{
        "Name": name,
        "Guid": guid,
        "Dynamic Profile Parent Name": "Default",
        "Custom Command": "Yes",
        "Command": command,
        "Has Hotkey": True,
        "HotKey Key Code": key["code"],
        "HotKey Characters": key["chars"],
        "HotKey Characters Ignoring Modifiers": key["plain"],
        "HotKey Modifier Flags": OPTION,
        "HotKey Activated By Modifier": False,
        # False: floating pins it above every window, so the session you
        # just opened comes up and the panel drops straight back on top
        # of it. The key brings it forward; it does not need to hover.
        "HotKey Window Floats": False,
        # False: this is a control panel. Enter takes you to a session's
        # window, and AutoHides would close the list at exactly that
        # moment - every time you used it. It goes when you press the key
        # again, or q.
        "HotKey Window AutoHides": False,
        "HotKey Window Animates": False,
        # False: the list appears because you pressed the key, never
        # because iTerm2 happened to come to the front.
        "HotKey Window Reopens On Activation": False,
        "Prevent Opening in a Tab": True,      # never swallowed by a window
        # False: Default's toolbelt (Session Status) is a sidebar the panel
        # has no room for, and Settings cannot change a Dynamic Profile.
        "Open Toolbelt": False,
        "Space": -1,                           # all spaces, or useless on space 2
        "Screen": -1,                          # wherever the cursor is
        "Window Type": 0,
    }]}


def profile_path(home=None):
    home = home or os.path.expanduser("~")
    return os.path.join(home, "Library", "Application Support", "iTerm2",
                        "DynamicProfiles", "ccwho.json")


def hotkey_current(home, wanted):
    """Is OUR profile on this machine the one we would write today?

    Same rule as the autosave job: "it is installed" is not "it works the way
    this version means it to". Without this, a settings change never reaches a
    machine that ran setup once - the profile has our Guid and a hotkey, so it
    reads as done for ever.
    """
    mine = _our_profile(profile_path(home))
    return bool(mine) and mine == wanted["Profiles"][0]


def _our_profile(path):
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    for p in doc.get("Profiles", []):
        if p.get("Guid") == PROFILE_GUID:
            return p
    return None


def hotkey_installed(home=None, hotkey=None):
    """Is OUR profile there, with a hotkey on it? Not "is there a profile"."""
    try:
        with open(profile_path(home)) as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return False
    for p in doc.get("Profiles", []):
        if p.get("Guid") == PROFILE_GUID and p.get("Has Hotkey"):
            if hotkey and p.get("HotKey Key Code") != HOTKEYS[hotkey]["code"]:
                return False
            return True
    return False


# --------------------------------------------------------- proving the hotkey
# Writing the profile proves nothing. Another app can already own the key, the
# profile may not be reloaded, Accessibility may be refused. So the profile is
# installed with a one-shot command carrying a nonce, and setup waits to see
# that nonce appear. If it does not, the key did not work, and setup says so
# instead of congratulating you.

def new_nonce():
    return uuid.uuid4().hex[:12]


def proof_path(ccwho_dir, nonce):
    return os.path.join(ccwho_dir, "proof", nonce)


def proof_command(ccwho, nonce):
    """What the hotkey window runs while setup is watching. It records the
    nonce and then becomes the list, so the first press is already useful."""
    return f"{shlex.quote(ccwho)} setup --proof {shlex.quote(nonce)}"


def write_proof(ccwho_dir, nonce):
    path = proof_path(ccwho_dir, nonce)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(str(int(time.time())))
    return path


def wait_for_proof(ccwho_dir, nonce, deadline=30.0, poll=0.2, now=None):
    """True when that exact nonce lands. A deadline of 0 is one look, so the
    caller can test the landing without waiting for anything."""
    clock = now or time.time
    end = clock() + deadline
    while True:
        if os.path.exists(proof_path(ccwho_dir, nonce)):
            return True
        if clock() >= end:
            return False
        time.sleep(poll)


# ------------------------------------------------------------- the setup plan
# Same facts doctor reads, plus the two things only setup cares about. Each step
# says done or to-do, so the second run is a no-op that tells you so rather than
# a second install.

def setup_plan(facts):
    steps = []

    claude = facts.get("claude") or ""
    steps.append(_step("claude", not claude, "found at " + claude if claude
                       else "not on PATH - ccwho has no session list without it",
                       "install Claude Code, then run ccwho setup again",
                       blocking=not claude))

    uv = facts.get("uv") or ""
    steps.append(_step("uv", not uv, "found at " + uv if uv
                       else "not on PATH - the live list cannot start",
                       "brew install uv"))

    # Registered-but-wrong and loaded-but-stale are the interesting states: both
    # look installed and neither does anything.
    registered = facts.get("handler_registered")
    current = facts.get("handler_current", True)
    steps.append(_step("ccwho:// handler", not (registered and current),
                       "registered" if registered and current else
                       "registered, but it points at another copy of ccwho"
                       if registered else
                       "not registered - the clickable links do nothing",
                       "setup builds and registers the applet"))

    loaded = facts.get("launchd_loaded")
    fresh = facts.get("autosave_current", True)
    steps.append(_step("autosave job", not (loaded and fresh),
                       "loaded" if loaded and fresh else
                       # Careful with this claim: a text difference means the
                       # job is not the one we would write, which is not the
                       # same as a job that no longer works.
                       "loaded, but not the job setup would write now"
                       if loaded else
                       "not loaded - a reboot would be a one-way door",
                       "setup renders the plist for this Mac and loads it"))

    steps.append(_step("hotkey", not facts.get("hotkey_installed"),
                       "installed" if facts.get("hotkey_installed")
                       else "no hotkey - the list is only ever a command away",
                       "setup writes an iTerm2 profile and asks you to press it"))

    # Asking for a permission is something setup can do; restarting iTerm2 is
    # not, because it ends every session running in it.
    reach = hotkey_reach(facts)
    steps.append(_step("hotkey reach", reach["do"] == "ask", reach["detail"],
                       reach["fix"], report=not reach["ok"]))

    # Reported, never repaired: this is another tool's file, and a tool that
    # rewrites one without asking is the fault doctor exists for. report=True
    # keeps its advice even though setup will not act on it.
    steps.append(_step("iTerm2 status hook", False,
                       "present" if facts.get("cc_status_hook")
                       else "missing - iTerm2's own session status is not updating",
                       "" if facts.get("cc_status_hook") else
                       "re-run iTerm2's Claude Code integration setup"
                       " (ccwho will not write that file for you)", report=True))
    return steps


def step_mark(step):
    """How a step reads in the list setup prints.

    Three states, not two: "note" is something that IS wrong and that setup will
    not touch - another tool's config. Printing `ok` beside a detail that says
    "missing" is the false all-clear this whole module exists to avoid.
    """
    if step["todo"]:
        return "todo"
    return "note" if step["fix"] else "ok  "


def _step(name, todo, detail, fix, blocking=False, report=False):
    # A fix is advice about something that is WRONG. Carrying one on a finished
    # step is how "ok" ends up printed next to a line telling you what to run.
    return {"name": name, "todo": bool(todo), "detail": detail,
            "fix": fix if (todo or report) else "", "blocking": bool(blocking)}
