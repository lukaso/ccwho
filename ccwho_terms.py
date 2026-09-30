"""The terminal apps ccwho shows and drives - one backend each - and the one gate
every background Apple Event goes through.

A rule module: imported by the engine and never the other way round, and
re-read before the engine on a hot reload (ccwho_engine.RELOAD_FIRST) - after
ccwho_procs, whose ps rows it reads. Nothing long-lived: an App is a table of facts
about one app, and a save or a claim records its `key`, never the object.

What a terminal app IS - whether it runs, which tty it shows - comes from ps
(App.pids, App.ttys, owners). Asking the app costs an Apple Event, and those
froze iTerm2 on 2026-09-29: see the gate below.
"""
from __future__ import annotations

import fcntl
import functools
import json
import os
import re
import subprocess
import time

import ccwho_procs as procs


# ------------------------------------------------------------------ osascript

OSASCRIPT = "osascript"
AE_TIMED_OUT = "-1712"          # errAETimeout: the app did not answer
AE_NOT_PERMITTED = "-1743"      # errAEEventNotPermitted: Automation refused
# the code that ENDS osascript's message: the message itself can quote data - a
# -1728 lists session names, and any program in a pane sets its own name
_AE_ERROR = re.compile(r"\((-\d+)\)\s*\Z")
# where in the script osascript's error came from: "<start>:<end>: execution error"
_AE_ERROR_AT = re.compile(r"(\d+):(\d+): execution error")
# Each launch script's first event: it writes nothing, so a -1743 that comes
# from it - Automation refused, or a consent prompt answered "Don't Allow" -
# means nothing of the script was sent (ae_error_at).
AE_PROBE = "count windows"


# ------------------------------------------------------------------ the apps

class App:
    """A terminal app. `key` is what saves and claims record; `label` what a
    message says; `script_name` the name AppleScript tells; `gate` the prefix
    of this app's gate files in STATE_DIR."""

    key = label = script_name = gate = ""

    def is_app(self, name):
        """Is a process of this name (ucomm: the kernel's name for the
        program, which argv[0] cannot fake) this app?"""
        return False

    def is_host(self, name):
        """Does a process of this name start the panes this app shows?"""
        return self.is_app(name)

    def pids(self, procs_output):
        """This app's processes of ours, lowest pid first. Another user's copy
        is not ours to ask."""
        me = os.getuid()
        return [pid for pid, (_tty, uid, name) in sorted(parse_procs(procs_output).items())
                if uid == me and self.is_app(name)]

    def pid(self, procs_output):
        """Our running copy of this app, or None."""
        pids = self.pids(procs_output)
        return pids[0] if pids else None

    def owners(self, ps_output, procs_output):
        """tty -> the pane's own process: the one this app of ours (or its
        daemon) started on it. A tab's identity - not the lowest pid on the
        tty: pids wrap at 99999, and a command started after the wrap would
        have a lower one."""
        table = parse_procs(procs_output)
        me = os.getuid()
        hosts = {pid for pid, (_tty, uid, name) in table.items()
                 if uid == me and self.is_host(name)}
        out = {}
        for pid, ppid, _cmd in procs.ps_rows(ps_output):
            tty = table.get(int(pid), ("",))[0] if pid.isdigit() else ""
            if tty and ppid in hosts:
                out[short_tty_full(tty)] = int(pid)
        return out

    def ttys(self, ps_output, procs_output, owners=None):
        """The terminals this app shows, from ps alone - or None when no copy
        of ours runs. `owners` is self.owners() of the same two tables, when
        the caller has it."""
        if self.pid(procs_output) is None:
            return None
        return set(self.owners(ps_output, procs_output) if owners is None else owners)

    def ask(self, args, **kw):
        """A background Apple Event to this app: through the gate (ask)."""
        return ask(args, app=self, **kw)

    def trouble(self, returncode, stderr):
        """What went wrong with an osascript to this app, in words a screen can
        show: its Apple Event code, never its message - which quotes paths and
        tab names. -1712 is the app not answering, not a refusal; a negative
        status is a signal, not a number to show."""
        code = ae_error_code(stderr)
        if code == AE_TIMED_OUT:
            return f"{self.label} did not answer ({code})"
        if code:
            return f"{self.label} refused ({code})"
        return "osascript was stopped by a signal" if returncode < 0 else f"osascript failed (exit {returncode})"

    def titles(self, timeout=5.0, procs=None):
        """Ask the app once: tty -> the name its tab shows, {} when it answered
        with no tabs, None when it was not asked or did not answer - the list is
        still worth showing without the names on the tabs. A scan never waits
        for the gate."""
        out = self.ask(["-e", self.titles_script()], timeout=timeout, procs=procs)
        return parse_titles(out) if out is not None else None


class ITerm2(App):
    key, label, script_name, gate = "iterm2", "iTerm2", "iTerm2", "iterm-ae"

    def is_app(self, name):
        return name == "iTerm2"                   # whatever the bundle is called

    def is_host(self, name):
        # its session daemon keeps every shell alive after iTerm2 quits, with the
        # same parent - and not one of them has a window then (ttys: None).
        # ucomm keeps 16 characters
        return self.is_app(name) or name.startswith("iTermServer-")

    def ttys(self, ps_output, procs_output, owners=None):
        """The terminals iTerm2 shows, from ps alone: a pane's first process is
        started by iTerm2's session daemon (iTermServer-*), or by iTerm2 itself
        when it runs without one. Measured 2026-09-29: 28 of 28 panes, the same
        set the AppleScript answer gave. Parents are known by the kernel's name
        for their program, from `procs_output` (`ps -eo pid,tty,uid,ucomm`),
        never by a command line or argv[0].

        This used to come from asking iTerm2. Those asks, each leaving a handler
        thread stuck inside a wedged iTerm2, used up its 512 worker threads and
        froze it - so what a window IS no longer costs an Apple Event.

        None when no iTerm2 app of ours is running: the daemon keeps every shell
        alive after iTerm2 quits, with the same parent, and not one has a window.
        """
        return super().ttys(ps_output, procs_output, owners)

    # every launch script starts with AE_PROBE, which writes nothing
    head = 'tell application "iTerm2"\n  %s\n  activate\n' % AE_PROBE

    def titles_script(self):
        """AppleScript for "what does each tab SAY", in one round trip.

        Bulk on purpose. Asking each session for its `tab.title` variable costs
        a round trip apiece - 1.6s for 25 sessions against 0.18s for this - and
        it does not even answer the question: measured 2026-09-21, both
        `tab.title` and `current session of tab` resolve to the WINDOW's current
        tab, so ten sessions in ten different tabs all reported one title.

        A session's own `name` is what the tab displays whenever that session is
        the tab's active pane, which is every tab that was never split. In a
        split tab the inactive pane reports its own name rather than the tab's -
        still the truth about that session, just not the string painted on the
        tab.
        """
        return (
            'tell application "iTerm2"\n'
            '  set d to (ASCII character 9)\n'
            '  set out to ""\n'
            '  repeat with w in windows\n'
            '    set ttyGroups to tty of sessions of every tab of w\n'
            '    set nameGroups to name of sessions of every tab of w\n'
            '    repeat with i from 1 to count of ttyGroups\n'
            '      set ts to item i of ttyGroups\n'
            '      set ns to item i of nameGroups\n'
            '      repeat with j from 1 to count of ts\n'
            '        set out to out & (item j of ts) & d & (item j of ns) & linefeed\n'
            '      end repeat\n'
            '    end repeat\n'
            '  end repeat\n'
            '  return out\n'
            'end tell\n'
        )

    def panes_script(self):
        """AppleScript listing every pane as tty<TAB>unique id<TAB>name, one a
        line.

        The unique id is what a restore needs: iTerm2 gives a restored pane the
        id it had before the restart (PTYSession.m adopts the saved "Session
        GUID" on system window restoration and on the startup arrangement), so a
        save can name the pane a session was in and a restore can put it back
        there.
        """
        return (
            'tell application "iTerm2"\n'
            '  set d to (ASCII character 9)\n'
            '  set out to ""\n'
            '  repeat with w in windows\n'
            '    set ttyGroups to tty of sessions of every tab of w\n'
            '    set idGroups to unique id of sessions of every tab of w\n'
            '    set nameGroups to name of sessions of every tab of w\n'
            '    repeat with i from 1 to count of ttyGroups\n'
            '      set ts to item i of ttyGroups\n'
            '      set us to item i of idGroups\n'
            '      set ns to item i of nameGroups\n'
            '      repeat with j from 1 to count of ts\n'
            '        set out to out & (item j of ts) & d & (item j of us) & d & (item j of ns) & linefeed\n'
            '      end repeat\n'
            '    end repeat\n'
            '  end repeat\n'
            '  return out\n'
            'end tell\n'
        )

    def panes(self, timeout=5.0, direct=False):
        """Ask iTerm2 for its panes (parse_panes). In the background (a save)
        through the gate: None when it was not asked, and the save keeps the
        panes it knew. `direct` is for a restore - something you do, which asks
        iTerm2 itself and gets {} when it cannot, and then opens windows, as it
        always did."""
        if direct:
            try:
                done = subprocess.run(["osascript", "-e", self.panes_script()], capture_output=True,
                                      text=True, errors="replace", timeout=timeout)
            except (OSError, subprocess.SubprocessError):
                return {}
            return parse_panes(done.stdout) if done.returncode == 0 else {}
        out = self.ask(["-e", self.panes_script()], timeout=timeout, wait=PANES_WAIT)
        return parse_panes(out) if out is not None else None

    def run_script(self, cmd):
        """One iTerm2 window running one command. The single-session case of the
        restore script, used when a click lands on a session that is no longer
        up."""
        if not cmd:
            return ""
        return (self.head +
                '  set w to (create window with default profile)\n'
                '  tell current session of w\n'
                '    write text %s\n  end tell\nend tell\n' % applescript_str(cmd))

    def open_script(self, items):
        """AppleScript that reopens sessions: `items` is [(pane unique id or
        None, command)] - into that pane when an id is given, else in a new
        window.

        The script returns the ids of the panes it wrote into, one a line, so
        the caller can tell a pane that closed in the meantime from one that was
        filled. A pane is idle by `ps`, which cannot see a line typed and not
        sent: ^E ^U clears it first (^E because ^U clears only left of the
        cursor in bash, fish and zsh's vi-insert), so the resume line is never
        appended to "rm -rf ". A shell in vi command mode reads the line as
        commands; that is not handled.

        Each pane is tried on its own and written at most once: a pane closing
        in the middle must not stop the script before it returns what it wrote.
        """
        lines, fills = [], []
        for uid, cmd in items or []:
            if uid:
                fills.append((uid, cmd))
                continue
            # the window by name: `current window` is asked for as an event of
            # its own, and a click on another window in between - seconds, when
            # iTerm2 is slow - would put the resume line into that window's pane
            lines.append("  set w to (create window with default profile)")
            lines.append("  tell current session of w")
            lines.append("    write text %s" % applescript_str(cmd))
            lines.append("  end tell")
        if not lines and not fills:
            return ""
        head = ['  set done to ""']
        if fills:
            head += ["  repeat with w in windows", "    repeat with t in tabs of w",
                     "      repeat with s in sessions of t",
                     # a pane it cannot even read had nothing written to it:
                     # passed over, whatever the error
                     "        set u to \"\"",
                     "        try",
                     "          set u to unique id of s",
                     "        end try",
                     "        try",
                     "          if done does not contain u then"]
            for i, (uid, cmd) in enumerate(fills):
                head.append("            %s u is %s then" % ("if" if i == 0 else "else if",
                                                            applescript_str(uid)))
                head.append("              tell s to write text"
                            " ((ASCII character 5) & (ASCII character 21)) newline no")
                head.append("              tell s to write text %s" % applescript_str(cmd))
                head.append("              set done to done & u & linefeed")
            # only a pane that is gone (-1728, -1719) is passed over: anything
            # else - a write that timed out (-1712), iTerm2 dying under it (-609,
            # -600) - may come after the resume line went in. Raised, or it reads
            # as a closed pane and the session is launched a second time
            head += ["            end if", "          end if",
                     "        on error errMsg number errNum",
                     "          if errNum is not in {-1728, -1719} then error errMsg number errNum",
                     "        end try",
                     "      end repeat", "    end repeat", "  end repeat"]
        body = "\n".join(head + lines + ["  return done"])
        return self.head + '%s\nend tell\n' % body


ITERM2 = ITerm2()
APPS = (ITERM2,)


def owners(ps_output, procs_output):
    """tty -> the pane's own process, for every app: App.owners, joined. A tty
    is one app's at a time."""
    out = {}
    for app in APPS:
        out.update(app.owners(ps_output, procs_output))
    return out


# ------------------------------------------------------------ process tables

def parse_procs(procs_output):
    """pid -> (tty, uid, executable name) from `ps -eo pid,tty,uid,ucomm`, or
    from the cheap `ps -eo pid,uid,ucomm` (tty ""). ucomm is the kernel's name
    for the program that runs: argv[0] - and so `comm` - a process sets itself
    (`exec -a iTerm2 sleep 9` is sleep)."""
    lines = (procs_output or "").splitlines()
    header = lines[0].split() if lines else []
    has_tty = len(header) > 1 and header[1].startswith("TT")
    out = {}
    for line in lines:
        f = line.split(None, 3 if has_tty else 2)
        if len(f) < (4 if has_tty else 3) or not f[0].isdigit():
            continue
        tty, uid, name = (f[1], f[2], f[3]) if has_tty else ("", f[1], f[2])
        if not uid.isdigit():
            continue
        out[int(f[0])] = ("" if tty in ("??", "?", "-") else tty, int(uid), name.strip())
    return out


def short_tty_full(tty):
    """`/dev/ttys022` -> `ttys022`. The form `ps` reports, so the two maps join."""
    return (tty or "").rsplit("/", 1)[-1]


def app_snapshot():
    """The cheap process table: no tty column (0.03 s of ps, where the tty
    column costs 0.2 s) - enough to find an app outside a scan. "" when ps
    could not be run, or did not exit cleanly: one killed part way may have
    printed part of the table."""
    try:
        r = subprocess.run(["ps", "-eo", "pid,uid,ucomm"], capture_output=True, text=True,
                           errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout if r.returncode == 0 else ""


@functools.cache
def boot_id():
    """This boot's id (kern.bootsessionuuid), or None. An id, not a time: a
    wall clock stepped after the boot moves every time compared with it."""
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        buf, size = ctypes.create_string_buffer(64), ctypes.c_size_t(64)
        ok = libc.sysctlbyname(b"kern.bootsessionuuid", buf, ctypes.byref(size), None, 0) == 0
    except (ImportError, OSError, AttributeError, ValueError):
        return None
    return buf.value.decode() if ok and buf.value else None


def above_std(fd):
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


def ae_error_code(err):
    """The Apple Event error code osascript ended its message with, or None."""
    m = _AE_ERROR.search(err or "")
    return m.group(1) if m else None


def ae_error_start(err):
    """Where in the script osascript's last error came from, or None."""
    found = _AE_ERROR_AT.findall(err or "")
    return int(found[-1][0]) if found else None


def ae_error_at(err, script, statement):
    """Did osascript's last error come from `statement` - the first line of
    `script` that is it? By the source range osascript puts before its
    message (it may be a part of the statement; it starts at the tell target
    when the statement is the last of its block); False when there is none."""
    start, at = ae_error_start(err), script.find(statement)
    return start is not None and at >= 0 and at <= start < at + len(statement)


def applescript_str(text):
    """Quote a Python string as an AppleScript literal.

    AppleScript has no alternative quoting: an unescaped `"` ends the literal and
    everything after it is parsed as CODE. Backslash is escaped FIRST, or the
    backslash we add for the quote could itself be eaten by a trailing backslash
    in the input. Newlines are dropped - a literal cannot span lines, and a
    newline is how a second statement would be smuggled in.
    """
    t = str(text).replace("\\", "\\\\").replace('"', '\\"')
    t = t.replace("\n", " ").replace("\r", " ")
    return '"%s"' % t


# ------------------------------------------------------------------ the gate
#
# Every BACKGROUND Apple Event goes through ask(), one gate per app. Measured
# 2026-09-29: a killed osascript does not cancel its event - iTerm2 keeps a
# handler thread stuck on it - and 509 such threads filled iTerm2's 512-thread
# pool and froze it, main thread and all. So, for each app:
#   - one event in flight, across every ccwho process: a lock file, held by the
#     osascript child itself until it exits. It is never killed.
#   - an event that timed out INSIDE the app (-1712) means that app is stuck:
#     no background ask reaches it again until it is a different process.
#   - a caller that gave up waiting, or any other failure, waits
#     ASK_WAIT_AFTER_ERROR: a slow app must not always have an event in flight.
# Things you do (jump, open, restore) are not background asks and do not come
# here: they are one at a time by nature.

STATE_DIR = os.path.expanduser("~/.cache/ccwho")
ASK_WAIT_AFTER_ERROR = 30.0


def _ask_sh(app):
    """$0 is osascript, $1 the state dir. Output goes to files: a pipe nobody
    reads any more fills at 64 KB, and the child - holding the lock - never
    exits. The status is written last, so it exists only for an ask that ran
    to its end. The answers hold tab names - paths, commands, hosts - so they
    are ours alone."""
    g = app.gate
    return (f'umask 077; d="$1"; shift; "$0" "$@" >"$d/{g}.out" 2>"$d/{g}.err"; '
            f'echo $? >"$d/{g}.status"')


def _gate_lock(app):
    """The app's gate lock file, opened above stdin, stdout and stderr: the
    ask's wrapper - which holds it for the ask - gets DEVNULL for those."""
    return above_std(os.open(_gate_path(app, "lock"), os.O_RDWR | os.O_CREAT, 0o600))


def _gate_path(app, name):
    return os.path.join(STATE_DIR, app.gate + "." + name)


def _gate_text(app, name):
    try:
        with open(_gate_path(app, name), errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _read_gate(app, now):
    """A damaged or missing state file is a closed gate: asking once is cheap,
    never asking again is not. So is any field of the wrong type, and a wait
    longer than any this code sets."""
    try:
        state = json.loads(_gate_text(app, "json") or "{}")
    except ValueError:
        return {}
    if not isinstance(state, dict):
        return {}
    clean = {}
    for key in ("quarantine", "asked_pid"):
        v = state.get(key)
        if type(v) is int or (type(v) is list and 0 < len(v) <= 64
                              and all(type(p) is int and p > 0 for p in v)):
            clean[key] = v
    for key in ("pending", "slow"):
        if state.get(key) is True:
            clean[key] = True
    wait = state.get("wait_until")
    if type(wait) in (int, float) and wait <= now + ASK_WAIT_AFTER_ERROR:
        clean["wait_until"] = wait
    if isinstance(state.get("last_error"), str) and re.fullmatch(r"-?\w{1,12}", state["last_error"]):
        clean["last_error"] = state["last_error"]
    if isinstance(state.get("boot"), str) and len(state["boot"]) <= 64:
        clean["boot"] = state["boot"]
    return clean


def _write_gate(app, state):
    """True when the state is on disk."""
    tmp = _gate_path(app, "json.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, _gate_path(app, "json"))
    except OSError:
        return False
    return True


def _settle(app, state, now):
    """Fold in how the last ask ended, and save it. Returns (state, its output
    if it answered, ok). The evidence is removed only once the state that
    records it is saved: a quarantine that cannot be written must not vanish.

    An ask marked pending with no status had its wrapper killed: its osascript
    may still have written why it failed, and that counts."""
    status = _gate_text(app, "status")
    pending = state.pop("pending", False)
    if status is None and not pending:
        return state, None, True
    out, err = _gate_text(app, "out") or "", _gate_text(app, "err") or ""
    answered = status is not None and status.strip() == "0"
    slow = state.pop("slow", False)
    if answered:
        state.pop("quarantine", None)
        state.pop("last_error", None)
        if slow:        # a late answer: the pause runs from when it was seen
            state["wait_until"] = now + ASK_WAIT_AFTER_ERROR
    else:
        state["last_error"] = ae_error_code(err) or "failed"
        if state["last_error"] == AE_TIMED_OUT:
            # of the boot the ask was sent in: settled after a reboot, the
            # quarantine is dropped by ask - its pid is nobody's now
            state["quarantine"] = state.get("asked_pid")
            state["boot"] = state.get("boot") or boot_id()
        else:
            state["wait_until"] = now + ASK_WAIT_AFTER_ERROR
    if not _write_gate(app, state):
        return state, None, False
    for name in ("status", "out", "err"):
        try:
            os.remove(_gate_path(app, name))
        except OSError:
            pass
    return state, (out if answered else None), True


def _take_lock(fd, wait, give_up=lambda: False):
    """True with the lock; False after `wait` seconds, or as soon as
    `give_up()` says the wait is for nothing. Only another holder is waited
    for: any other flock error - a file system without it - is raised."""
    end = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= end or give_up():
                return False
            time.sleep(0.05)


def ask(args, *, app, timeout=5.0, procs=None, now=None, wait=0.0, why=None):
    """Run `osascript *args` against `app` as a background ask. Its output, or
    None when it was not asked or did not answer in `timeout` - the list is
    still worth showing without what the app would have said.

    `procs` is a process table the caller already has (collect()'s); without
    one, the cheap one is taken - after the lock, like the clock: a wait set
    while this caller queued for the lock is a wait for it too. `wait` is how
    long to queue behind an ask in flight; a scan queues for nothing.

    `why`, a dict, is told what happened: "asked" (an event was sent),
    "refused" (no-iterm - no copy of the app of ours runs -, no-table - the
    process list could not be read -, io - its state, lock or spawn failed -,
    busy, stuck, waiting) and "error" (the Apple Event error code that ended
    osascript's message, "timeout", or "failed" when it ended with none).
    """
    if why is None:
        why = {}
    why.update({"asked": False, "refused": None, "error": None})
    fixed_now = now
    try:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        os.chmod(STATE_DIR, 0o700)
        fd = _gate_lock(app)
    except OSError:
        why["refused"] = "io"
        return None
    try:
        try:
            free = _take_lock(fd, 0)
        except OSError:
            why["refused"] = "io"           # no lock can be had: not "busy"
            return None
        if not free:
            # an ask in flight, maybe in another process. Queue behind it only
            # while it is a healthy one: behind one that already timed out, or
            # into a pause, the answer is a refusal anyway - waiting only
            # freezes us. Looked at on every try: it can time out while we wait
            ahead = {}

            def hopeless():
                early = time.time() if fixed_now is None else fixed_now
                ahead.clear()
                ahead.update(_read_gate(app, early))
                ahead["hopeless"] = bool(ahead.get("slow") or early < ahead.get("wait_until", 0))
                return ahead["hopeless"]
            try:
                queued = not hopeless() and wait and _take_lock(fd, wait, give_up=hopeless)
            except OSError:
                why["refused"] = "io"
                return None
            if not queued:
                why.update(refused="waiting" if wait and ahead["hopeless"] else "busy",
                           error=ahead.get("last_error"))
                return None
        now = time.time() if fixed_now is None else fixed_now
        table = app_snapshot() if procs is None else procs
        if not parse_procs(table):
            why["refused"] = "no-table"     # ps failed: not "no app", nor a stuck one
            return None
        pids = app.pids(table)
        if not pids:
            why["refused"] = "no-iterm"     # `tell application` would launch it
            return None
        state, _, saved = _settle(app, _read_gate(app, now), now)
        if not saved:
            why["refused"] = "io"           # cannot record what happens next: do not send
            return None
        if (state.get("quarantine") is not None and state.get("boot") is not None
                and boot_id() is not None and state["boot"] != boot_id()):
            state.pop("quarantine")         # of a copy before a reboot: its pid is nobody's now
                                            # (a boot not recorded, or not readable: it holds)
            state.pop("last_error", None)
        stuck = state.get("quarantine")
        stuck = [] if stuck is None else ([stuck] if type(stuck) is int else stuck)
        if stuck and not set(stuck) & set(pids):
            state.pop("quarantine")         # restarted: none that may be stuck runs
            state.pop("last_error", None)
        elif stuck:
            # one of them still runs - beside another too: `tell application`
            # may reach it yet
            why.update(refused="stuck", error=state.get("last_error"))
            return None
        elif now < state.get("wait_until", 0):
            why.update(refused="waiting", error=state.get("last_error"))
            return None
        # every copy of ours it may reach: `tell application` picks one. One is
        # written as its pid: a reader from before lists (a hotkey panel keeps
        # its engine) keeps that quarantine
        state.update(asked_pid=pids[0] if len(pids) == 1 else pids, pending=True, boot=boot_id())
        if not _write_gate(app, state):
            why["refused"] = "io"
            return None
        try:
            child = subprocess.Popen(
                ["/bin/sh", "-c", _ask_sh(app), OSASCRIPT, STATE_DIR, *args],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, pass_fds=(fd,), start_new_session=True)
        except OSError:
            state.pop("pending", None)
            _write_gate(app, state)
            why["refused"] = "io"
            return None
        why["asked"] = True
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            # it keeps the lock until it exits; the next ask reads how. Until
            # then and a while after, leave a slow app alone
            state.update(wait_until=now + ASK_WAIT_AFTER_ERROR, slow=True)
            _write_gate(app, state)
            why["error"] = "timeout"
            return None
        # it came back now: a pause after an error runs from here, not from
        # before the ask
        done = time.time() if fixed_now is None else fixed_now
        if _gate_text(app, "status") is None:
            # its wrapper died, and its osascript may not have: that one still
            # holds the lock and may yet write why it failed (-1712). Whoever
            # takes the lock after it ends settles it - as a slow one till then
            state.update(wait_until=done + ASK_WAIT_AFTER_ERROR, slow=True)
            _write_gate(app, state)
            why["error"] = "failed"
            return None
        state, out, _saved = _settle(app, state, done)
        if out is None:
            why["error"] = state.get("last_error")
        return out
    finally:
        os.close(fd)


# ------------------------------------------------------ tab names and panes

def parse_titles(dump):
    """tty -> the name the app shows for it. Junk lines are skipped, never
    fatal: this is another application's output, and a list must still
    render."""
    out = {}
    for line in (dump or "").splitlines():
        tty, sep, name = line.partition("\t")
        if not sep or not name.strip():
            continue
        out[short_tty_full(tty.strip())] = name.strip()
    return out


def parse_panes(dump):
    """tty -> {"pane": unique id, "name": name}. A line with no id is skipped:
    a pane that cannot be found again is no use to a restore."""
    out = {}
    for line in (dump or "").splitlines():
        f = line.split("\t")
        if len(f) < 3 or not f[0].strip() or not f[1].strip():
            continue
        out[short_tty_full(f[0].strip())] = {"pane": f[1].strip(), "name": f[2].strip()}
    return out


PANES_WAIT = 10.0      # a save every 15 minutes may wait out the list's ask


# Tab names change when you rename a tab or a session's title updates - minutes,
# not seconds. They are only a label: which terminals have a window comes from
# ps (App.ttys). So once a minute, whatever the tabs do - each ask is an Apple
# Event, and those froze iTerm2 on 2026-09-29. A new tab shows its "~" fallback
# until the next ask.
TITLES_TTL = 60.0


def titles_cached(cache, now=None, procs=None, owners=None):
    """The tab names, at most once per TITLES_TTL. `cache` is the runner's dict,
    the same idiom as the transcript window cache; None means always ask.

    An answer - even "no tabs" - is kept for the minute. No answer is not: the
    names it had stay up and the next tick asks again, which costs nothing
    while the app is not answering - ask() refuses without sending anything.

    A tty whose pane changed since the ask (`owners`, owners(): tty -> the
    pane's process) loses its name - it shows its "~" fallback, never the old
    session's - with no new ask.
    """
    now = time.time() if now is None else now
    if cache is None:
        return ITERM2.titles(procs=procs) or {}
    owners = owners or {}
    entry = cache.get("_titles")
    # any other shape was written by the engine before a hot reload: a miss
    ts, titles, seen = (entry if isinstance(entry, tuple) and len(entry) == 3
                        and isinstance(entry[2], dict) else (None, {}, {}))
    kept = {t: name for t, name in titles.items() if seen.get(t) == owners.get(t)}
    if ts is not None and now - ts < TITLES_TTL:
        return kept
    fresh = ITERM2.titles(procs=procs)
    if fresh is None:
        return kept     # not asked: the names it had, and ask again next tick
    cache["_titles"] = (now, fresh, owners)
    return fresh
