"""ccwho engine - all the logic. HOT-RELOADED by the live list before each scan.

Keep this module free of long-lived objects: the list calls reload_all() before
each scan so edits land in a running list without restarting it. Only pure
functions and plain dicts cross the boundary; state lives in the caller and is
passed back in, the way network_check_ruby carries @options between ticks.

Reads only what Claude Code already writes to disk. No daemon, no hooks, no tmux.

  claude agents --json      -> name, cwd, status, waitingFor, pid, sessionId
  ~/.claude/projects/**/    -> <sessionId>.jsonl transcript, for the real topic
  ps                        -> processes reparented to PID 1, matched by sessionId
"""
from __future__ import annotations

import calendar
import glob
import hashlib
import json
import os
import datetime
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time

import ccwho_brief as brief
import ccwho_index
import ccwho_text
from ccwho_text import ANSI as _ANSI, cells as _cells, cut as _cut  # noqa: E402,F401
from ccwho_text import plain_text  # noqa: E402,F401
import ccwho_usage  # noqa: F401 - in RELOAD_FIRST; the list reads usage through the engine
import ccwho_procs as procs
import ccwho_terms as terms

# The rule modules the engine imports, in the order they are re-read. One list,
# so a rule module cannot be left out of the reload: a module not named here
# is a fix that does not land in a running list.
# Dependencies first: ccwho_text before ccwho_usage (which imports it), procs
# before brief (which imports procs).
# ccwho_terms after ccwho_procs: it reads procs.ps_rows.
RELOAD_FIRST = ("ccwho_text", "ccwho_procs", "ccwho_terms", "ccwho_brief", "ccwho_index",
                "ccwho_usage")

# Status order: what needs you first, what is working next, what is parked last.
# stopped outranks busy: a stopped session will not progress without you, while a
# busy one is fine. `running` is not busy but has background work in flight - it is
# waiting on a machine, not on you, so it sorts last. `stuck` needs you too - to
# kill a loop that cannot end - but less than anything that finished or asked.
_RANK = {"blocked": 0, "waiting": 0, "asks": 1, "review": 2, "stuck": 2.5, "stopped": 3,
         "busy": 4, "ready": 5, "shell": 6, "idle": 7, "running": 8, "program": 9}

# Descendants that are session infrastructure rather than work. An idle session
# keeps its MCP servers alive; counting them would make every session look busy.
_INFRA = re.compile(r"(npm exec\s+\S*mcp|\S*-mcp\b|mcp-server|mcp_server)|_npx/", re.I)

# Closing-line requests that hand the decision back without a question mark.
# Deliberately short: every entry was validated against a live fleet, and a false
# positive costs a glance while a false negative loses the session entirely.
_ASK_PHRASES = ("tell me", "let me know", "your call", "say the word",
                "which do you want", "shall i", "want me to", "should i",
                "do you want", "confirm whether", "decision is yours")
_UNKNOWN_RANK = 4


# ---------------------------------------------------------------- pure helpers

def parse_sessions(raw):
    """Parse `claude agents --json`. None when it could not be parsed, never an exception.

    None and [] are different answers, one layer deeper than `agents_json()` draws
    the same line. "[]" is claude saying nothing is open; None is us not knowing -
    truncated output, a `{}`, anything that is not a list of sessions. Collapsing
    the two is how a caller decides a RUNNING session is closed and reopens it,
    which forks the conversation into two processes.
    """
    data = _agents_payload(raw)
    if data is None:
        return None
    usable = [d for d in data if isinstance(d, dict) and d.get("sessionId")]
    if data and not usable:
        return None               # a list of things that are not sessions tells us nothing
    return usable


def _agents_payload(raw):
    """The feed as a list, or None if it is not one."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, list) else None


def read_is_complete(raw):
    """Did we understand ALL of it? Separate from "what did we understand".

    A record shape we do not know (a future claude, a new kind of agent) must not
    blank the dashboard - the sessions we did parse are still worth showing. But a
    partial picture cannot authorise LAUNCHING anything: the session we skipped may
    be the very one a caller is about to reopen, and reopening a live session forks
    it. So display uses the rows; `open`, `restore --open` and `save` use this.
    """
    data = _agents_payload(raw)
    if data is None:
        return False
    return all(isinstance(d, dict) and d.get("sessionId") for d in data)


# One definition, in the pure module: the live row and the index both need it.
project_of = brief.project_of


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


# One rule, one place. The topic column, the brief's "you said" and search all ask
# the same question - "did a human type this?" - and two copies of the answer drift
# until the list and the brief disagree about the same session.
_BOILERPLATE = brief.MACHINE_PREFIXES


def is_boilerplate(text):
    return not brief.is_human_prompt(text)


def extract_topic(lines, strict=False):
    """First and last real user prompt. Skips tool noise and system reminders."""
    real, any_prompt = [], []
    for d in as_records(lines):
        if d.get("type") != "user":
            continue
        text = " ".join(_text_of(d.get("message", {}).get("content")).split())
        if not text:
            continue
        any_prompt.append(text)
        if not is_boilerplate(text):
            real.append(text)
    # Prefer a genuine prompt. In strict mode report nothing rather than harness noise,
    # so the caller can widen its search window instead of printing a task-notification.
    if strict:
        pool = real
    else:
        pool = real or [t for t in any_prompt if not t.startswith("<")] or any_prompt
    return {"first": pool[0] if pool else "", "last": pool[-1] if pool else ""}


def as_records(lines):
    """Accept raw JSONL strings or already-parsed dicts. Parsing a tail once per
    tick instead of once per extractor was a 5x cut; the extractors took 5.2
    passes over every line."""
    out = []
    for item in lines or ():
        if isinstance(item, dict):
            out.append(item)
            continue
        if not isinstance(item, str):
            continue
        try:
            d = json.loads(item)
        except (ValueError, TypeError):
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def cache_valid(entry, mtime, size):
    """A cached window is reusable only while the file is byte-for-byte unchanged."""
    if not isinstance(entry, dict):
        return False
    return entry.get("mtime") == mtime and entry.get("size") == size


def extract_title(lines):
    """Claude Code's own generated session title (`ai-title` entries)."""
    title = ""
    for d in as_records(lines):
        if d.get("type") == "ai-title" and d.get("aiTitle"):
            title = str(d["aiTitle"]).strip()
    return title


# Tools whose most useful hint is the file they touch.
def extract_doing(lines):
    """What the session last actually did - the final tool call, human-readable."""
    doing = ""
    for d in as_records(lines):
        if d.get("type") != "assistant":
            continue
        content = d.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                name = b.get("name", "?")
                hint = " ".join(str(brief._tool_hint(name, b.get("input", {}) or {})).split())
                doing = f"{name}: {hint}" if hint else name
    return doing


# Only real turns. Idle transcripts keep receiving metadata writes (atis-latch,
# bridge-session, mode), so file mtime says "4m" for a session last worked six days
# ago - wrong on exactly the parked sessions where recency matters.
_TURN_TYPES = ("assistant", "user")


def last_turn_ts(lines):
    """Epoch seconds of the last real turn, from its `timestamp` field."""
    latest = None
    for d in as_records(lines):
        if d.get("type") not in _TURN_TYPES:
            continue
        raw = d.get("timestamp")
        if not raw:
            continue
        try:
            latest = datetime.datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            continue
    return latest


def since(mtime, now=None):
    """Time since last transcript write - real activity, unlike session start age."""
    if mtime is None or not isinstance(mtime, (int, float)):
        return "?"
    secs = max(0, int((time.time() if now is None else now) - mtime))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


def _cmdline_orphans(ps_output, session_ids):
    """sessionId -> pids of PID-1 processes whose command line names it."""
    out = {sid: set() for sid in session_ids}
    for pid, ppid, cmd in procs.ps_rows(ps_output):
        if ppid != 1 or not pid.isdigit():
            continue
        for sid in session_ids:
            if sid and sid in cmd:
                out[sid].add(int(pid))
    return out


def _fleet(att, rows, ports_ok, procs_ok=True, sessions_ok=True):
    """What the whole machine says, for the header and the bottom lines."""
    project = {sid: r.get("project", "?") for r in rows for sid in answers_for(r)}
    held = []
    for sid, mine in att["sessions"].items():
        held += [(port, p["pid"], project.get(sid, "?")) for p in mine
                 if not p["helper"] for port in p["ports"]]
    held += [(port, p["pid"], "left behind") for p in att["left_behind"]
             if not p["helper"] for port in p["ports"]]
    held += [(port, p["pid"], "codex") for p in att["codex"]
             if not p["helper"] for port in p["ports"]]
    held += [(port, p["pid"], "not sure") for p in att.get("unsure", [])
             if not p["helper"] for port in p["ports"]]
    return {"left_behind": att["left_behind"], "codex": att["codex"],
            "unsure": att.get("unsure", []), "sessions_ok": sessions_ok,
            "by_session": att["sessions"], "ports_ok": ports_ok,
            # a ps that could not run leaves no marks: "unknown", not "nothing"
            "procs_ok": procs_ok,
            "agent_ports": [{"port": port, "pid": pid, "who": who}
                            for port, pid, who in sorted(set(held))]}


def answers_for(row):
    """The session ids a row stands for: its own, and those of the terminals
    that parked it (ctrl+b) - they have no row, and what they started is its."""
    return [row.get("sessionId")] + list(row.get("parked") or [])


def live_ids(rows):
    """Every session id the rows say is running, the parked terminals included."""
    return {sid for r in rows or [] for sid in answers_for(r) if sid}


def _closing_line(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    return re.sub(r"[*_`#>]", "", lines[-1]).strip()


# A question in quotation marks is one the session reports - put to a reviewer,
# or one a user might ask - not one it puts to you. Measured over 5388 ended
# turns: dropping quoted text turned 7 asks into none, and all 7 were quotes.
# A quote runs to the next mark, never the last. A " after a digit is inches
# (13"), not the start of a quote.
_QUOTED = re.compile(r'(?<!\d)"[^"]*"|“[^”]*”')


def asks_user(text):
    """Does this message hand the decision back to the human?

    The harness reports `idle` for a session that ended its turn with a question,
    so status alone loses them. Only the closing line is considered: a question
    earlier in a long report is usually one the message goes on to answer.
    """
    closing = _QUOTED.sub("", _closing_line(text).lower())
    if not closing.strip():
        return False
    return "?" in closing or any(p in closing for p in _ASK_PHRASES)


def last_assistant_text(lines):
    text = ""
    for d in as_records(lines):
        if d.get("type") != "assistant":
            continue
        content = d.get("message", {}).get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                    text = b["text"]
    return text


def extract_ask(lines):
    """The closing line, when it is a request. Empty when the session is not asking."""
    text = last_assistant_text(lines)
    return _closing_line(text) if asks_user(text) else ""


# The tools that hand the decision to a person. A pending tool call on its own
# means nothing - this session was sitting on a running Bash while it worked -
# but a pending QUESTION means the session is waiting for you, whatever the
# session feed says. Measured over the newest 200 transcripts on a real
# machine: AskUserQuestion is the only tool ever left unanswered at the end of
# one, and a running Bash is what "busy" looks like.
HUMAN_TOOLS = ("AskUserQuestion", "ExitPlanMode")


def pending_tools(lines):
    """Tool calls with no result yet, newest last, by name."""
    pending = {}
    for d in as_records(lines):
        content = d.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        if d.get("type") == "assistant":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    pending[b.get("id")] = b.get("name", "")
        elif d.get("type") == "user":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    pending.pop(b.get("tool_use_id"), None)
    return list(pending.values())


def is_asking_you(lines):
    """Is this session sitting on a question it put to you?

    Read from the transcript, which is written the moment the question is asked
    and again the moment you answer it - both faster, and both more certain,
    than the session feed noticing.
    """
    return any(name in HUMAN_TOOLS for name in pending_tools(lines))


# What Claude Code writes when a turn ends. Measured over the newest 300
# transcripts: 2235 of 2251 ended turns carry turn_duration, and 0 of 44860
# steps inside a turn carry either.
_TURN_END = ("turn_duration", "stop_hook_summary")


def turn_ended(lines):
    """Has the last turn ended, with nothing said since?

    The feed says `busy` while any background task runs, turn or no turn. This is
    how to tell a session that is working from one that has handed back to you
    and left a task running.
    """
    ended = False
    for d in as_records(lines):
        if d.get("type") in ("assistant", "user"):
            ended = False
        elif d.get("type") == "system" and d.get("subtype") in _TURN_END:
            ended = True
    return ended


def waiting_kind(lines):
    """Split Claude Code's `waiting` into what it actually means.

    `waitingFor: "input needed"` covers two very different states: a session
    genuinely blocked on a permission prompt or question, and one that simply
    finished its turn and is sitting at the prompt. Only the first needs you.
    The tell is an unanswered tool_use - a tool call with no matching tool_result.
    """
    pending = set()
    for d in as_records(lines):
        content = d.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        if d.get("type") == "assistant":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    pending.add(b.get("id"))
        elif d.get("type") == "user":
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    pending.discard(b.get("tool_use_id"))
    return "blocked" if pending else "ready"


# How long a finished task output must stay unchanged before a loop on it counts
# as dead: longer than any loop sleeps between looks (the real ones slept 5s).
DEAD_LOOP_GRACE = 120
_TASK_TAIL = 400


def task_file(path, now=None):
    """(the last bytes, seconds since it changed) of a task output, or None.

    A regular file only: a subagent's task output is a symlink to its whole
    transcript. Checked on the file that was opened, not on the path - a FIFO
    swapped in between a check and the open would hang the scan.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        os.lseek(fd, max(0, st.st_size - _TASK_TAIL), os.SEEK_SET)
        tail = os.read(fd, _TASK_TAIL).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)
    return tail, (time.time() if now is None else now) - st.st_mtime


# what a wait loop runs while it waits: between looks nothing, or its sleep, and
# during one its grep. Anything else under it means it is past the loop. One
# that just exited, not yet reaped, ps prints as "(sleep)": still waiting - a
# kill that looked then found no stuck loop (2026-09-29, 2 of 226 scans)
_WAITING = re.compile(r"^(?:(?:\S*/)?(?:sleep|grep)(?:\s|\Z)|\((?:sleep|grep)\)\Z)")


# One grep, nothing from ccwho's own environment (GREP_OPTIONS, a gnubin PATH):
# the same question gets the same answer on every scan. Asked in both locales a
# loop's shell may have run in - `.` is one byte in C and one character in UTF-8
_GREP_LOCALES = ("C", "en_US.UTF-8")
_GREP_KEEP = 256


def grep_matches(argv, files, timeout=5, run=None):
    """Ask a loop's own grep again: True if its line is there, False if not,
    None when grep could not say. `argv` ends in `-e PATTERN`, so neither the
    pattern nor a file can be read as an option."""
    said = set()
    for locale in _GREP_LOCALES:
        try:
            done = (run or subprocess.run)(
                ["/usr/bin/grep", *argv, "--", *files], capture_output=True,
                timeout=timeout, env={"LC_ALL": locale, "PATH": "/usr/bin:/bin"})
        except (OSError, subprocess.SubprocessError):
            return None
        said.add({0: True, 1: False}.get(done.returncode))
    # found in either is found: the loop may have seen it that way
    return None if None in said else True in said


def find_dead_loops(pid, ptable, read=task_file, grace=DEAD_LOOP_GRACE,
                    matches=None, cache=None):
    """A session's wait loops that cannot end: [{"pid", "tasks"}].

    Walked down the session's own process tree, not taken from procs.attribute:
    a Bash tool shell is the claude's own child and carries no session mark
    (measured 2026-09-24), so that list never has it. `ptable` is pid -> (ppid,
    start, command); `tasks` are the ids of the task outputs the loop polls.
    """
    kids = {}
    for cpid, (ppid, _start, cmd) in (ptable or {}).items():
        kids.setdefault(ppid, []).append((cpid, cmd))
    ask = matches or grep_matches
    # a finished file does not change, so neither does grep's answer: asked
    # once, not on every scan (a slow pattern or a big file costs each time)
    kept = {} if cache is None else cache.setdefault("_greps", {})
    if len(kept) > _GREP_KEEP:
        kept.clear()
    found, stack, seen = [], [pid], {pid}
    while stack:
        parent = stack.pop()
        parent_cmd = (ptable or {}).get(parent, (0, "", None))[2]
        for cpid, cmd in sorted(kids.get(parent, [])):
            if cpid in seen:
                continue
            seen.add(cpid)
            stack.append(cpid)
            if cmd == parent_cmd:
                continue                # a fork of the loop is the same loop
            loop = procs.wait_loop(cmd)
            # a fork of it that exited, not yet reaped, is "(zsh)" to ps
            fork = f"({(cmd.split(None, 1) or [''])[0].rsplit('/', 1)[-1]})"
            if not loop or not all(_WAITING.match(c) for _, c in kids.get(cpid, [])
                                   if c not in (cmd, fork)):
                continue
            # it has had its look: the file ended longer ago than it sleeps
            wait = max(grace, loop["sleep"] + 60)
            facts = [read(x) for x in loop["files"]]
            if not procs.cannot_end(facts, wait):
                continue
            # and its line is not there: a loop whose line IS there has ended,
            # and its shell has gone on to whatever came after it
            key = (tuple(loop["grep"]), tuple(loop["files"]), tuple(t for t, _ in facts))
            answer = kept.get(key)
            if answer is None:              # never asked, or it could not say
                answer = kept[key] = ask(loop["grep"], loop["files"])
            if answer is False:
                found.append({"pid": cpid, "tasks": [
                    os.path.basename(x)[:-len(".output")] for x in loop["files"]]})
    return found


def kill_dead_loops(session_pid, pids, ps=None, read=task_file, kill=None,
                    matches=None, unix=None, own=None, started=None, starts=None,
                    report=None, fds=None, stack=None, cache=None, now=None):
    """SIGTERM each of `pids` that is STILL a dead loop of that session - and
    for a dead reader, its whole task: the tool shell under the claude and all
    below it, parents first. Never ccwho (`own`) or what runs it, and never an
    agent the task started, nor anything under one: those go to report["spared"].

    Looked for again, not taken from the last scan: by the time you press the
    key the loop may have ended and its pid gone to something else. A reader
    must still have the start it was listed with (`started`, pid -> lstart).
    Returns the pids of `pids` that got the signal, or whose task did. What
    was not killed is not "not stuck": report["unconfirmed"] has each asked-for
    loop or reader that still runs but did not look stuck, and report["unread"]
    is True when ps, or the start table, said nothing; report["failed"] has
    each stuck loop the signal was not permitted to reach. `cache`: a dict of the
    caller's that learns what this kill's new look saw (adopt_verdicts) - a
    reader no longer reading is not offered again on the list's older verdict
    (review 2). The list adopts it in its scan thread (review 3).
    """
    rows = [(int(pid), ppid, cmd) for pid, ppid, cmd in procs.ps_rows((ps or ps_snapshot)())
            if pid.isdigit()]
    ptable = {pid: (ppid, "", cmd) for pid, ppid, cmd in rows}
    wanted = set(pids or ())
    send = kill or os.kill
    begun = []

    def table():
        """The start table, read once for the whole kill: two reads can disagree."""
        if not begun:
            begun.append((starts or ps_table)())
        return begun[0]
    killed, hit, failed = [], set(), []
    # both looked for on the one snapshot, before anything is signalled
    loops = [d["pid"] for d in find_dead_loops(session_pid, ptable, read, matches=matches)
             if d["pid"] in wanted]
    # a program not named a reader is sampled again - only one asked about. Its
    # verdicts go to the list's cache under the keys of the list's scan: with
    # the start times (the table this kill reads anyway), as the scan has them
    seen = None if cache is None else {}
    keyed = ptable if cache is None else {
        pid: (ppid, (table().get(pid) or ("",))[0], cmd) for pid, (ppid, _s, cmd) in ptable.items()}
    readers = [d for d in find_dead_readers(session_pid, keyed, unix=unix, grace=0, cache=seen,
                                            fds=fds, stack=stack, only=wanted, now=now)
               if d["pid"] in wanted]
    if seen:
        adopt_verdicts(cache, seen)
    if readers:
        killed = _stop_reader_tasks(session_pid, readers, rows, send, own, started,
                                    table, report, hit)
    # a reader's task first: a loop killed alone lets its shell run on, and a
    # loop in a task already stopped got its TERM there
    for pid in loops:
        if pid in hit:
            killed.append(pid)
            continue
        try:
            send(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError:
            failed.append(pid)
            continue
        killed.append(pid)
    # a listed pid that went with a reader's task got the signal too: said -
    # if it is still the one listed (a reader has a start; a loop is not listed
    # with one). One that got a new process is not "the one you stopped"
    killed += [p for p in sorted(wanted & hit) if p not in killed
               and (p not in (started or {}) or (table().get(p) or ("",))[0] == started[p])]
    if report is not None:
        report["unread"] = report.get("unread") or not rows
        report["failed"] = failed
        # what was stopped with a reader's task is not "still runs"
        report["unconfirmed"] = _unconfirmed(
            session_pid, wanted - set(loops) - {d["pid"] for d in readers} - hit, ptable,
            started, table, report)
    return killed


def _unconfirmed(session_pid, pids, ptable, started, table, report):
    """Of `pids` - asked for, not judged stuck now - the ones that still run as
    what was listed: in the session's tree, still a wait loop or a reader, and
    a reader still with the start it was listed with. Not "not stuck". A start
    table that said nothing leaves a listed reader unknown: report["unread"]."""
    tree, stack = set(), [session_pid]
    kids = {}
    for cpid, (ppid, _start, _cmd) in ptable.items():
        kids.setdefault(ppid, []).append(cpid)
    while stack:
        for c in kids.get(stack.pop(), []):
            if c not in tree:
                tree.add(c)
                stack.append(c)
    # a reader listed by its stack is listed with its start, as every reader is
    out = [p for p in sorted(pids) if p in tree
           and (procs.wait_loop(ptable[p][2]) or procs.stdin_reader(ptable[p][2])
                or p in (started or {}))]
    listed = {p: s for p, s in (started or {}).items() if p in out}
    if listed:
        begun = table()
        if not begun:
            report["unread"] = True
        out = [p for p in out if p not in listed
               or (begun.get(p) or ("",))[0] == listed[p] != ""]
    return out


def _stop_reader_tasks(session_pid, readers, rows, send, own, started, starts, report, hit):
    """kill_dead_loops' part for readers: each listed reader's whole task,
    once, parents first. Every pid signalled goes into `hit`."""
    killed = []
    begun = starts()
    if not begun and report is not None:
        report["unread"] = True         # no reader can be matched to the one listed
    ptable = {pid: (ppid, (begun.get(pid) or ("",))[0], cmd) for pid, ppid, cmd in rows}
    own = os.getpid() if own is None else own
    spared = {own, *procs._ancestors(ptable, own)}
    kids = {}
    for cpid, (ppid, _start, _cmd) in ptable.items():
        kids.setdefault(ppid, []).append(cpid)

    def below(root):
        out, stack = set(), [root]
        while stack:
            p = stack.pop()
            if p not in out:
                out.add(p)
                stack += kids.get(p, [])
        return out
    # the start each was listed with, or nothing: a reader is killed only as
    # the one you saw
    by_root = {}
    for d in readers:
        if (started or {}).get(d["pid"]) == ptable[d["pid"]][1] != "":
            by_root.setdefault(d["root"], []).append(d["pid"])
    for root, wanted_here in sorted(by_root.items()):
        task = below(root)
        # a claude or codex the task started is a session of its own
        agents = sorted(p for p in task if procs._is_an_agent(ptable[p][2]))
        out = set().union(*(below(a) for a in agents)) if agents else set()
        if report is not None:
            report.setdefault("spared", []).extend(
                a for a in agents if not any(b in agents for b in procs._ancestors(ptable, a)))
        # parents first: a shell whose child dies while it runs goes on to its
        # next command - one this list never saw. Its parent gone, it cannot.
        # A script that traps TERM, or what forks after the snapshot, is not
        # reached: no process-group kill, which would reach the spared too
        for p in reversed(procs.leaves_first(sorted(task - spared - out), ptable)):
            try:
                send(p, signal.SIGTERM)
                hit.add(p)
            except (ProcessLookupError, PermissionError):
                continue
        killed += [p for p in wanted_here if p in hit]
    return killed


def stdin_sockets(pids):
    """parse_lsof_unix for `pids`, or None when lsof could not say."""
    text = _lsof(["-nP", "-a", "-U", "-p", ",".join(str(p) for p in pids), "-F", "pfdn"])
    return None if text is None else procs.parse_lsof_unix(text)


def open_fds(pids):
    """parse_lsof_fds for `pids`, or None when lsof could not say."""
    text = _lsof(["-nP", "-a", "-p", ",".join(str(p) for p in pids), "-F", "pfatn"])
    return None if text is None else procs.parse_lsof_fds(text)


CPU_WHILE_WAITING = 0.05      # seconds of CPU a process blocked in read may show in a sample


def _cpu_seconds(pid):
    """The CPU `pid` has used (ps -o time=), or None when ps could not say."""
    return procs.parse_cpu_time(_ps(["-o", "time=", "-p", str(int(pid))]))


def stack_sample(pid):
    """Is `pid` waiting in read? A one-second `sample` of it (every 10 ms) with
    every thread in the kernel's read (procs.stack_reads), and no CPU spent
    meanwhile: a read that loops - on a file, /dev/null - uses CPU, a read that
    waits does not (review 3). None when it could not be told: no such
    process, not permitted, too slow. sample stops the process for each look,
    about a millisecond. Apple's /usr/bin/sample, never the first `sample` on
    PATH (review 3): its report is the one stack_reads reads."""
    try:
        before = _cpu_seconds(pid)
        done = subprocess.run(["/usr/bin/sample", str(int(pid)), "1", "10", "-mayDie",
                               "-file", "/dev/stdout"],
                              capture_output=True, text=True, errors="replace", timeout=10,
                              stdin=subprocess.DEVNULL)
        after = _cpu_seconds(pid)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if before is not None and after is not None and after - before > CPU_WHILE_WAITING:
        return False                    # working, whatever its stack looked like
    # a sample that failed has no stack to read: stack_reads says unknown
    said = procs.stack_reads(done.stdout)
    return None if said and (before is None or after is None) else said


def _started(start):
    """Seconds since the epoch of a `ps -o lstart` text (C locale, UTC), or None."""
    try:
        return calendar.timegm(time.strptime(start or "", "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


VERDICTS_KEEP = 512    # verdicts kept, in a scan's memory and in the file
STACK_RESAMPLE = 300    # seconds before a process not seen reading is sampled again
STACK_PER_SCAN = 2      # samples a scan may take, every session's together: each
                        # takes about 1.5 s (the process stops about 1 ms a look)


def verdicts_path():
    """Where a stuck-reader verdict waits for the next ccwho process."""
    return os.path.expanduser("~/.cache/ccwho/readers.json")


def _verdict_key(claude, pid, start, cmd):
    """One process, named without its command: a command can hold a secret."""
    return hashlib.sha256(repr((claude, pid, start, cmd)).encode()).hexdigest()[:24]


def _verdicts(raw):
    """The well-formed entries of `raw`: key -> [verdict, when], a verdict
    True, False or None (unknown). Only True is ever acted on."""
    out = {}
    for k, v in (raw.items() if isinstance(raw, dict) else ()):
        if (isinstance(k, str) and isinstance(v, (list, tuple)) and len(v) == 2
                and (v[0] is None or isinstance(v[0], bool)) and isinstance(v[1], (int, float))):
            out[k] = [v[0], float(v[1])]
    return out


def _newest(entries):
    """The newest VERDICTS_KEEP of key -> [verdict, when], in place."""
    if len(entries) > VERDICTS_KEEP:
        keep = sorted(entries, key=lambda k: entries[k][1], reverse=True)[:VERDICTS_KEEP]
        kept = {k: entries[k] for k in keep}
        entries.clear()
        entries.update(kept)
    return entries


_VERDICT_PARTS = (("_fd0", "fd0"), ("_stacks", "stacks"))


def load_verdicts(cache, path=None, now=None):
    """Put the file's verdicts in `cache`: "_fd0" (fd 0 is the claude's socket)
    and "_stacks" (every thread in read). Never raises: a file that cannot be
    read holds no verdict, which only means a process is asked again. One
    stamped in the future - a clock set back - is left out: it would come back
    through this cache to the file (review 4)."""
    later = (time.time() if now is None else now) + 60
    try:
        with open(path or verdicts_path()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    for part, name in _VERDICT_PARTS:
        cache[part] = {k: v for k, v in _verdicts(data.get(name)).items() if v[1] <= later}


def adopt_verdicts(cache, learned):
    """Put what `learned` knows - its definite verdicts, never an unknown - in
    `cache`, and mark them to be kept. A kill learns in a dict of its own, and
    the list's scan thread adopts it: the kill runs in a thread of its own,
    beside a scan that may be walking the cache (review 3)."""
    took = False
    for part, _name in _VERDICT_PARTS:
        mine = cache.setdefault(part, {})
        for k, v in _verdicts(learned.get(part)).items():
            # never over a newer look (a scan's, after the kill's: review 4)
            if v[0] is not None and (k not in mine or v[1] >= mine[k][1]):
                mine[k] = v
                took = True
    if took or learned.get("_verdicts_new"):
        cache["_verdicts_new"] = True


def save_verdicts(cache, path=None, now=None):
    """Merge `cache`'s verdicts into the file and write it whole: a temp file,
    then a rename. Another ccwho may have written it since it was read: the
    newer verdict of each process wins, and the newest VERDICTS_KEEP stay. Two
    writing at the same moment can lose one's new verdicts - not locked, as a
    lost verdict is only asked again (review 1). A verdict stamped in the
    future - a clock set back - is dropped, or it would win every merge after
    (review 3). Never raises."""
    path = path or verdicts_path()
    later = (time.time() if now is None else now) + 60
    disk = {}
    load_verdicts(disk, path, now=later - 60)
    out = {}
    for part, name in _VERDICT_PARTS:
        merged = disk[part]
        for k, v in _verdicts(cache.get(part)).items():
            if v[1] > later:
                continue                # this process's own, stamped before a clock went back
            if k not in merged or v[1] >= merged[k][1]:
                merged[k] = v
        out[name] = _newest(merged)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            json.dump(out, fh)
        os.replace(tmp, path)
    except OSError:
        pass                            # a verdict not kept is only asked again


def find_dead_readers(pid, ptable, unix=None, now=None, grace=DEAD_LOOP_GRACE, cache=None,
                      fds=None, stack=None, only=None, names=None):
    """A session's readers that cannot end: [{"pid", "tasks", "kind", "program",
    "root", "start"}] - `tasks` empty, there for a script reading dead_loops as
    loops; `start` so a kill knows it is still the reader that was listed.

    A program reading its stdin in the session's own tree, whose fd 0 is a
    socket the session's claude holds. Claude Code never writes to it: a command
    with a heredoc gets no `< /dev/null`, and a `cat` in one moved to the
    background waited 20 hours (2026-09-26). `root` is the tool shell under the
    claude: the task a person stops. Younger than `grace` or of unknown age, it
    is not judged; lsof is asked only about candidates.

    A program procs.stdin_reader names reads its stdin by its command line. Any
    other one with no children of its own is proven the long way (2026-10-07: a
    python3 read that socket for 5.5 hours): no other fd its read could be on
    (`fds`, procs.only_stdin_can_block), and every thread in the kernel's read
    (`stack`, a sample). Every verdict is asked again after STACK_RESAMPLE
    seconds: some reads do return (`read -t`, an alarm). A scan samples at
    most STACK_PER_SCAN - all its sessions together, when collect puts the
    budget in `cache` ("_sample_budget"); the rest stay due for the next scan.
    `only`: the pids a kill asked about - no other is sampled, and each of them
    is. `names`: pid -> `ps -o comm`, the name a row prints. What `cache`
    gains is marked "_verdicts_new", for collect to keep (save_verdicts).
    """
    kids = {}
    for cpid, (ppid, _start, _cmd) in (ptable or {}).items():
        kids.setdefault(ppid, []).append(cpid)
    now = time.time() if now is None else now
    # only in a Bash tool's task: an MCP server's stdin is a claude socket too,
    # and Claude Code writes to that one
    found, seen = [], {pid}
    todo = [(c, c) for c in sorted(kids.get(pid, [])) if procs.is_tool_shell(ptable[c][2])]
    cands, others = {}, {}
    while todo:
        cpid, root = todo.pop()
        if cpid in seen:
            continue
        seen.add(cpid)
        _ppid, start, cmd = ptable[cpid]
        if procs._is_an_agent(cmd):
            continue            # a claude the task started: its socket, its work
        todo += [(k, root) for k in sorted(kids.get(cpid, []))]
        if procs._ZOMBIE.match(cmd or ""):
            continue            # ended, not reaped: it holds no fd, it reads nothing
        began = _started(start)
        if not (grace <= 0 or began is not None and now - began >= grace):
            continue
        if procs.stdin_reader(cmd):
            cands[cpid] = root
        elif (not [k for k in kids.get(cpid, []) if not procs._ZOMBIE.match(ptable[k][2] or "")]
              and (only is None or cpid in only)):
            # a shell or wrapper waits on its child, not on its stdin - a child
            # that ended and was never reaped is no child it waits on (review 4)
            others[cpid] = root
    if not cands and not others:
        return []
    kept = {} if cache is None else cache.setdefault("_fd0", {})
    stacks = {} if cache is None else cache.setdefault("_stacks", {})
    key = {c: _verdict_key(pid, c, ptable[c][1], ptable[c][2]) for c in {**cands, **others}}
    # a kill samples each process it asked about; a scan, what its budget allows
    left = None if only is not None else (cache or {}).get("_sample_budget", STACK_PER_SCAN)
    # with no sample left, a program not proven yet waits: its proof is a
    # sample's first step - save, open and ps ask lsof nothing for it (review 5)
    if left is not None and left <= 0:
        others = {c: r for c, r in others.items() if key[c] in kept}
    # its fd 0 does not change while it runs, nor does the claude's end close
    # while the reader lives: asked once per (reader, claude)
    every = {**cands, **others}
    fresh = set()                   # proven by this call's own lsof
    if any(key[c] not in kept for c in every):
        sockets = (unix or stdin_sockets)(sorted(set(every) | {pid}))
        # lsof could not say: what was proven before still stands
        for c in every if sockets is not None else ():
            kept[key[c]] = [procs.stdin_from(sockets, c, pid), now]
            fresh.add(c)
        if sockets is not None and cache is not None:
            cache["_verdicts_new"] = True

    def proven(c):
        return (kept.get(key[c]) or [False])[0] is True
    on_socket = [c for c in sorted(others) if proven(c)]

    def due(c):
        said = stacks.get(key[c])
        # a stamp later than now: a clock set back (review 3)
        return said is None or not 0 <= now - said[1] < STACK_RESAMPLE
    asked = [c for c in on_socket if due(c)]
    if asked and (left is None or left > 0):     # no sample left: not even lsof
        # fd 0 proven again before a new sample: a dup2 may have put another
        # socket there since (review 4); one this call just proved stands
        stale = [c for c in asked if c not in fresh]
        again = (unix or stdin_sockets)(sorted(set(stale) | {pid})) if stale else {}
        listing = (fds or open_fds)(asked)
        for c in asked:
            if c in stale and again is not None and not procs.stdin_from(again, c, pid):
                kept[key[c]] = [False, now]
                said = False        # its stdin is no longer the claude's socket
            elif (c in stale and again is None) or listing is None:
                said = None         # lsof could not say: unknown, never no (review 3)
            elif not procs.only_stdin_can_block(listing, c):
                said = False        # a read that could be on another fd is not its stdin's
            elif left is None or left > 0:
                said = (stack or stack_sample)(c)
                left = None if left is None else left - 1
            else:
                continue            # no sample left this scan: due on the next
            if said is None and stacks.get(key[c]):
                said = stacks[key[c]][0]        # unknown, never no: the last verdict stands
            stacks[key[c]] = [said, now]
            if cache is not None:
                cache["_verdicts_new"] = True
    if left is not None and cache is not None and "_sample_budget" in cache:
        cache["_sample_budget"] = left
    for c in sorted(every):
        if proven(c) and (c in cands or (stacks.get(key[c]) or (None,))[0] is True):
            found.append({"pid": c, "tasks": [], "kind": "reader",
                          "program": procs.reader_name(ptable[c][2], (names or {}).get(c)),
                          "root": every[c], "start": ptable[c][1]})
    # what this scan found stays: it is the newest
    _newest(kept)
    _newest(stacks)
    return found


def work_descendants(ps_output, pid):
    """How many non-infrastructure processes this session has running under it.

    Zero means the session has stopped: nothing is in flight, so it will not move
    again without you. Non-zero means it is waiting on a machine, not on a human.
    """
    if not pid:
        return 0
    kids = {}
    for cpid, ppid, cmd in procs.ps_rows(ps_output):
        try:
            kids.setdefault(ppid, []).append((int(cpid), cmd))
        except (TypeError, ValueError):
            continue
    count, stack, seen = 0, [int(pid)], {int(pid)}
    while stack:
        for cpid, cmd in kids.get(stack.pop(), []):
            if cpid in seen:
                continue
            seen.add(cpid)
            stack.append(cpid)
            if not _INFRA.search(cmd):
                count += 1
    return count


def sort_key(row):
    """Rank first, then most-recent-first so a fresh ask surfaces above parked ones."""
    key = row.get("attention") or row.get("status")
    ts = row.get("ts")
    return (_RANK.get(key, _UNKNOWN_RANK), -(ts if ts is not None else 0),
            row.get("project", ""), row.get("name", ""))


def age(started_ms, now=None):
    # `not started_ms` would swallow epoch 0, which is a valid timestamp.
    if started_ms is None or not isinstance(started_ms, (int, float)):
        return "?"
    now = int(time.time() * 1000) if now is None else now
    mins = max(0, (now - started_ms) // 60000)
    if mins < 60:
        return f"{mins}m"
    if mins < 1440:
        return f"{mins // 60}h"
    return f"{mins // 1440}d"


def truncate(s, width):
    s = s or ""
    return s if len(s) <= width else s[: max(0, width - 1)] + "…"


def _printable(value):
    # a newline in a name or a title would paint a third line into a row of two
    return "".join(c if c.isprintable() else " " for c in str(value or "")).strip()


def session_handle(row):
    """The session's name: the address another session sends a message to
    (`ccwho-b5`). Its project when it has none."""
    handle = _printable(row.get("name"))
    project = _printable(row.get("project"))
    return handle if handle not in ("", "?") else (project or "?")


def renamed(row, handle):
    """The project, when the name does not already say it: an auto-name is the
    project and a dash; a renamed one (`update-landing-page-faq`) is not."""
    project = _printable(row.get("project"))
    project = "" if project == "?" else project
    return project if (project and handle != project
                       and not handle.startswith(project + "-")) else ""


def pick_lines(rows):
    """The sessions a query matched, one line each, their columns in line."""
    w = max((len(session_handle(r)) for r in rows), default=0)
    return [pick_line(r, w) for r in rows]


def pick_line(row, w=0):
    """One session in the list a query matched, with every name it is known by."""
    handle = session_handle(row)
    return (f"{handle:<{w}}  {brief.short_id(row.get('sessionId', '')):<4}  "
            f"{short_tty(row.get('tty', '')) or '-':<5}  "
            f"{row.get('title') or renamed(row, handle)}").rstrip()


# ------------------------------------------------------------------ disk reads

def all_roots(cache=None):
    """The known config roots, plus the dirs running sessions named for
    themselves on the last scan - kept in the caller's cache by collect(),
    because an isolated session's transcript lives under ITS dir."""
    extra = (cache or {}).get("_extra_roots", [])
    return procs.config_dirs(extra, ccwho_index.config_roots())


def transcript_path(session_id, roots=None):
    """Where this session's transcript lives, in ANY config root.

    Not every session runs under the login in ~/.claude: some use a
    CLAUDE_CODE_OAUTH_TOKEN, and CLAUDE_CONFIG_DIR moves a session's whole
    directory. ccwho never logs in - it reads files - so a hard-coded path was
    the one thing that could tie it to a single login. (index.config_roots lists
    them: $CLAUDE_CONFIG_DIR, ~/.claude, and anything in ~/.ccwho/roots.)
    """
    if not session_id:
        return None
    for root in roots or ccwho_index.config_roots():
        hits = glob.glob(os.path.join(glob.escape(root), "projects", "*",
                                      f"{glob.escape(session_id)}.jsonl"))
        if hits:
            return hits[0]
    return None


def read_windows(session_id, tail_bytes=1024 * 1024, head_lines=400, cache=None):
    """Head and tail of a transcript as PARSED records, plus its mtime.

    `cache` is owned by the runner (it survives hot reload) and keyed on
    sessionId, holding (mtime, size). An unchanged transcript is not re-read or
    re-parsed - which is most of them, most ticks.
    """
    path = transcript_path(session_id, roots=all_roots(cache))
    if not path:
        return [], [], None
    try:
        st = os.stat(path)
    except OSError:
        return [], [], None
    if cache is not None:
        hit = cache.get(session_id)
        if cache_valid(hit, st.st_mtime, st.st_size):
            return hit["head"], hit["tail"], hit["mtime"]
    try:
        head = []
        with open(path, "r", errors="ignore") as fh:
            for _ in range(head_lines):
                line = fh.readline()
                if not line:
                    break
                head.append(line)
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail_bytes))
            tail = fh.read().decode("utf-8", "ignore").splitlines()[1:]
        head, tail = as_records(head), as_records(tail)
        if cache is not None:
            cache[session_id] = {"mtime": st.st_mtime, "size": st.st_size,
                                 "head": head, "tail": tail}
        return head, tail, st.st_mtime
    except OSError:
        return [], [], None


def topic_for(session_id, tail_bytes=4 * 1024 * 1024, head_lines=400):
    """Head+tail read only. Transcripts reach hundreds of MB; never read the whole file."""
    path = transcript_path(session_id)
    if not path:
        return {"first": "", "last": ""}
    try:
        head = []
        with open(path, "r", errors="ignore") as fh:
            for _ in range(head_lines):
                line = fh.readline()
                if not line:
                    break
                head.append(line)
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail_bytes))
            tail = fh.read().decode("utf-8", "ignore").splitlines()[1:]
    except OSError:
        return {"first": "", "last": ""}
    first = extract_topic(head, strict=True)["first"] or extract_topic(head)["first"]
    # Tail first, then head, then anything at all - never print harness noise as a topic.
    last = (extract_topic(tail, strict=True)["last"]
            or extract_topic(head, strict=True)["last"]
            or first
            or extract_topic(tail)["last"])
    return {"first": first, "last": last}


def parse_tty_map(ps_output):
    """pid -> controlling terminal. `??` means no terminal, so it is omitted."""
    out = {}
    for line in (ps_output or "").splitlines():
        f = line.strip().split()
        if len(f) < 2 or not f[0].isdigit():
            continue
        if f[1] in ("??", "?", "-"):
            continue
        out[int(f[0])] = f[1]
    return out


# A jump target is a tty like s032 or a bare pid. Nothing else is accepted, so a
# crafted URL cannot smuggle anything into the handler.
_JUMP_TARGET = re.compile(r"^[A-Za-z]?[0-9]{1,8}\Z")


def visible_len(text):
    """Length as rendered: escape sequences occupy no columns."""
    return len(_ANSI.sub("", text or ""))


def osc8(label, url, enabled=True):
    """Wrap a label as an OSC 8 hyperlink. iTerm2 renders it clickable."""
    if not enabled or not url:
        return label
    return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"


def jump_url(row):
    target = short_tty(row.get("tty", "")) or (str(row.get("pid")) if row.get("pid") else "")
    return f"ccwho://jump/{target}" if target else ""


# A link names a session by its UUID. (It had the same name as the looser
# _SESSION_ID further down, which replaced it: links accepted any id-shaped text.)
_LINK_SESSION_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                              r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")


def open_url(entry):
    """ccwho://open/<sessionId> - "get me to this session", verb decided on click.

    Deliberately NOT ccwho://jump/: a jump names a window, and the restore list is
    read after a reboot, when no window exists. The id is validated here so a
    manifest can never talk the handler into a shell argument of its choosing.
    """
    sid = str((entry or {}).get("sessionId", "") or "")
    return "ccwho://open/" + sid if _LINK_SESSION_ID.match(sid) else ""


def parse_ccwho_url(url):
    """(verb, target) for a ccwho:// URL we understand, else ("", "").

    One dispatcher for every verb, so the registered handler never has to learn a
    new one - it forwards the whole URL and this decides. Both targets are pattern
    validated: the URL arrives from LaunchServices, which anyone can call.
    """
    if not isinstance(url, str):
        return ("", "")
    for verb, pattern in (("open", _LINK_SESSION_ID), ("jump", _JUMP_TARGET)):
        prefix = "ccwho://%s/" % verb
        if url.startswith(prefix):
            target = url[len(prefix):].strip().rstrip("/")
            return (verb, target) if pattern.match(target) else ("", "")
    return ("", "")


def daemon_short(session_id):
    """The id `claude attach` knows a job by: the first eight hex of the session.

    `claude agents --json` reports it as `id`, and on every job measured it is
    the session id's prefix. Given the full session id, claude 2.1.280 says
    "No job matching" - about a session that is running fine.
    """
    return (session_id or "")[:8]


def attach_command(session_id, config_dir=None):
    """Put a running background session in a terminal.

    `claude attach <id>`: "Open the background session in this terminal." The
    session feed marks these `kind: background` - on a live fleet of fifteen,
    fourteen interactive and one background.

    `claude attach` looks in the config dir it runs under, so a session found in
    another dir is attached there: under the default one it is `No job matching`.
    """
    if not session_id:
        return ""
    cmd = f"claude attach {shlex.quote(daemon_short(session_id))}"
    if config_dir:
        cmd = f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} {cmd}"
    return cmd


def is_attach_to(command, session_id):
    """Is this `claude attach` for this session, by its short id or its full one?"""
    if not session_id:
        return False
    ids = "|".join(re.escape(i) for i in {session_id, daemon_short(session_id)})
    return re.search(r"\battach\s+(%s)(\s|$)" % ids, command or "") is not None


def resolve_open(session_id, live_rows, entries, source_ok=True):
    """What a click on an open-link should DO, decided against the world right now.

    ("jump", tty)             it is running and has a window - focus it.
    ("attach", cmd)           it is running with no window we can find - a background
                              session. `claude attach <id>` opens it in a terminal.
                              NOT a resume: "I cannot see a window" is not "it is not
                              running", and resuming a live session forks the
                              conversation into two processes writing one transcript.
    ("program", why)          a program runs it, with no window - nothing to open.
    ("resume", cmd)           it is not running - reopen it.
    ("unknown", why)          we could not read the live fleet at all. An empty list
                              from a failed read looks exactly like "nothing is
                              running"; every caller that can launch must be told
                              the difference. Nothing is opened on a guess.
    ("missing", "")           neither the live fleet nor the manifest has heard of it.

    Every caller that can start `claude --resume` - `open`, the url handler,
    `restore --open` - goes through here, so the guard is written once.
    """
    if not source_ok:
        return ("unknown", "cannot read the live session list")
    for r in live_rows or []:
        # a terminal that parked a job shows it: the row that answers for it
        # (parked_terminals) lists it in `parked`
        if r.get("sessionId") == session_id or session_id in (r.get("parked") or []):
            tty = short_tty(r.get("tty", ""))
            if tty:
                return ("jump", tty)
            # A program runs it, and was never a job the daemon can attach: a
            # window on it would fail, or take it from the program
            if is_program(r):
                return ("program", no_window_note(r))
            # Running, with no window we can find - which is a session to OPEN,
            # not one to reopen. `claude attach` puts a running session in a
            # terminal; `claude --resume` would start a second process on a
            # live transcript.
            return ("attach", attach_command(r.get("sessionId") or session_id,
                                             r.get("configDir")))
    for e_ in entries or []:
        if e_.get("sessionId") == session_id:
            cmd = restore_command(e_)
            if cmd:
                return ("resume", cmd)
    return ("missing", "")


def short_tty(tty):
    """ttys032 -> s032. Short enough for a column, still unambiguous."""
    t = (tty or "").rsplit("/", 1)[-1]
    return t[3:] if t.startswith("tty") and len(t) > 3 else t


def match_rows(rows, query):
    """Rows matching ANY name a session answers to.

    A session has five: the tab title, a short id, the full session id, the
    message name (`liveapp-f0`) and a tty or pid. Whichever one you remember has
    to be the one that works - and `show` prints short ids in its own ambiguity
    list, which used to match nothing at all when typed back in.

    An id is matched as a PREFIX, like a git short sha, so two sessions sharing
    four hex stay ambiguous and are listed rather than guessed between.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    hits = []
    for r in rows:
        sid = (r.get("sessionId") or "").lower()
        tty = r.get("tty", "")
        if q == str(r.get("pid")) or q in (tty, short_tty(tty)) or (tty and short_tty(q) == short_tty(tty)):
            hits.append(r)
        elif sid and sid.startswith(q):
            hits.append(r)
        elif any(p.lower().startswith(q) for p in r.get("parked") or []):
            hits.append(r)          # the terminal that parked it shows it
        elif q in (r.get("name") or "").lower():
            hits.append(r)
        elif q in (r.get("tab_title") or "").lower():
            hits.append(r)          # the name on the tab is the one you can see
        elif q in (r.get("title") or "").lower() or q in (r.get("project") or "").lower():
            hits.append(r)
    return hits


def match_terminal(rows, target):
    """The rows on that tty, or with that pid - and nothing else. A ccwho://jump
    link names a terminal, and any app can send one: a title word or an id's
    start is not what it named."""
    t = (target or "").strip().lower()
    return [r for r in rows or [] if t and (
        t == str(r.get("pid")) or (r.get("tty") and short_tty(t) == short_tty(r["tty"].lower())))]


# ------------------------------------------------------------ process tables

def _ps(args, env=None):
    """ps's output - waited for 20 s at most - or None when it could not be
    run or did not exit cleanly: one killed part way may have printed part of
    the table, and a process missing from it is not gone (nor a pane idle)."""
    try:
        r = subprocess.run(["ps", *args], capture_output=True, text=True, errors="replace",
                           timeout=20, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


_SHELLS = {"sh", "bash", "zsh", "fish", "ksh", "tcsh", "csh", "dash", "nu", "xonsh"}


def idle_ttys(ps_output):
    """The terminals where nothing runs but a shell at its prompt.

    From `ps -eo pid,tty,command`. Measured on an iTerm2 pane: `/usr/bin/login
    -fpl ...` and `-zsh`, nothing else. A shell with arguments is running a
    script, and anything else is a program the person is using - a restore
    writes its command only where it cannot land in either.
    """
    busy, seen = set(), set()
    for line in (ps_output or "").splitlines():
        f = line.split(None, 2)
        if len(f) < 3 or not f[0].isdigit() or f[1] in ("??", "?", "-"):
            continue
        tty, argv = f[1], f[2].split()
        seen.add(tty)
        head = os.path.basename(argv[0]).lstrip("-")
        if head == "login" or (head in _SHELLS and len(argv) == 1):
            continue
        busy.add(tty)
    return seen - busy


def idle_snapshot():
    out = _ps(["-eo", "pid,tty,command"])
    return set() if out is None else idle_ttys(out)


def tty_snapshot():
    # the executable last: it may hold spaces ("Application Support")
    return _ps(["-eo", "pid,tty,uid,ucomm"]) or ""


def ps_snapshot():
    return _ps(["-eo", "pid,ppid,command"]) or ""


# Where the tools ccwho shells out to actually live. A process with no login
# shell - a launchd job, or a window opened by a global hotkey, because iTerm2
# itself was started by the Dock - has PATH=/usr/bin:/bin:/usr/sbin:/sbin, and
# none of these are in it. The symptom is never "command not found": it is an
# empty fleet, or a window that closes before you can read why.
TOOL_PLACES = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin",
               "~/.cargo/bin", "~/bin")


def find_tool(name):
    """A tool's full path: PATH first, then where it installs itself. "" when
    it is not on this machine at all."""
    found = shutil.which(name)
    if found:
        return found
    for place in TOOL_PLACES:
        candidate = os.path.join(os.path.expanduser(place), name)
        if os.path.exists(candidate):
            return candidate
    return ""


def ps_table():
    """pid -> (start, command) for every process, the start in the exact text a
    session file stores as procStart. The C locale and UTC are what make them
    comparable: a local, localised `ps` prints "Tue 22 Sep 14:45:15"."""
    text = _ps(["-axo", "pid=,lstart=,comm="], env=dict(os.environ, LC_ALL="C", TZ="UTC"))
    return {} if text is None else procs.parse_ps_table(text)


def read_procargs(pid):
    """The allowlisted environment of one of our own processes, or None.

    The kernel hands back the whole environment - tokens included - and it goes
    straight into procs.parse_procargs, which keeps the named keys and nothing
    else. Nothing else here looks at the buffer.
    """
    return procs.parse_procargs(procargs_buffer(pid))


def read_auth(pid):
    """Which account a claude process spends (procs.parse_auth), or None.

    The same buffer as read_procargs, handed straight to procs.parse_auth, which
    keeps the token's fingerprint and the config dir and nothing else (#28)."""
    return procs.parse_auth(procargs_buffer(pid))


def read_iterm_pane(pid):
    """The iTerm2 pane a process runs in (procs.parse_iterm_pane): "" when its
    environment names none, None if unreadable. The same buffer, handed
    straight to the parse, which keeps the pane's id and nothing else."""
    return procs.parse_iterm_pane(procargs_buffer(pid))


def procargs_buffer(pid):
    """The raw KERN_PROCARGS2 answer for a pid, or None. It holds every secret in
    that environment: only procs.parse_procargs, procs.parse_auth and
    procs.parse_iterm_pane read it."""
    import ctypes
    import ctypes.util
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        # declared, not guessed: newlen is a size_t, and an undeclared 0 goes
        # over as a 32-bit int with whatever sits in the register's upper half
        libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
                                ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
                                ctypes.c_void_p, ctypes.c_size_t]
        libc.sysctl.restype = ctypes.c_int
        mib = (ctypes.c_int * 3)(1, 49, int(pid))          # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0:
            return None
        # sized by the kernel's own answer; one too big for ARG_MAX fails with
        # EINVAL above rather than coming back cut short
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        return buf.raw[:size.value]
    except (OSError, ValueError, AttributeError, TypeError):
        return None


SESSION_FILE_MAX = 64 * 1024        # a session file is a few hundred bytes


def _read_small_file(path):
    """The bytes of a small regular file, or None. One open that neither follows a
    symlink nor blocks on a FIFO, and every check made on THAT handle - checking a
    path and then opening it again leaves room for the file to be swapped."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > SESSION_FILE_MAX:
            return None
        data = os.read(fd, SESSION_FILE_MAX + 1)
        return None if len(data) > SESSION_FILE_MAX else data
    finally:
        os.close(fd)


def _read_session_file(path):
    """A session file's object, or None if it cannot be used. A file caught while
    Claude Code writes it parses on the second look; one that never parses - or
    nests deep enough to exhaust the parser - is a real problem."""
    for _attempt in (1, 2):
        data = _read_small_file(path)
        if data is None:
            return None
        try:
            return json.loads(data)
        except (ValueError, RecursionError):
            continue
    return None


def read_session_files(dirs, default=None):
    """Every usable `<dir>/sessions/*.json`, each tagged with its `configDir`; and
    how many could not be used.

    The dirs are named by other processes, so a scan must finish whatever is in
    them: only small regular files are read (see _read_small_file), glob
    characters in a dir name are literal, and an entry that is not what Claude
    Code writes (procs.entry_problem) counts as unusable.
    """
    entries, bad = [], 0
    for d in dirs:
        for path in glob.glob(os.path.join(glob.escape(d), "sessions", "*.json")):
            try:
                data = _read_session_file(path)
            except OSError:                 # a symlink (ELOOP), gone, unreadable
                data = None
            if data is None or procs.entry_problem(data):
                bad += 1
                continue
            # the default dir stays unnamed: its sessions run with
            # CLAUDE_CONFIG_DIR unset, and setting it can move the login
            entries.append(dict(data, configDir="" if d == default else d))
    return entries, bad


def config_dirs_now(table=None):
    """Every config dir: the known roots, and any a running claude names."""
    table = ps_table() if table is None else table
    own = []
    for pid in procs.claude_pids(table):
        env = read_procargs(pid) or {}
        own.append(env.get("CLAUDE_CONFIG_DIR", ""))
    return procs.config_dirs(own, ccwho_index.config_roots())


def live_file_sessions(table=None):
    """Live sessions from every config dir, as `claude agents --json` rows; and
    how many session files could not be read (for `ccwho doctor`).

    `claude agents --json` only answers for the config dir it runs under, and
    pointed at another one it WRITES into it. So: each running claude process
    names its own CLAUDE_CONFIG_DIR, and each dir keeps a small file per session.
    """
    table = ps_table() if table is None else table
    dirs = config_dirs_now(table)
    entries, bad = read_session_files(dirs, default=os.path.expanduser("~/.claude"))
    return procs.live_session_files(entries, procs.claude_starts(table)), bad


def listen_ports():
    """pid -> listening TCP ports, or None when lsof could not be asked. None and
    {} are different answers, as with agents_json: "ports unknown" is not "no
    ports", and a view that says nobody holds :3000 had better have looked."""
    try:
        done = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"],
                              capture_output=True, text=True, errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    # lsof exits 1 both when nothing matched and on real errors; only the first
    # comes with nothing on stderr
    if done.returncode not in (0, 1) or (done.returncode == 1 and
                                         (done.stderr or "").strip()):
        return None
    return procs.parse_lsof_listen(done.stdout)


def proc_marks(table, cache=None):
    """pid -> the session mark in its environment, from ps_table()'s
    pid -> (start, command); None for one that was read and names no session,
    absent for one that could not be read. An environment is fixed at exec, so each
    (pid, start, command) is read once and kept in the caller's cache: a reused
    pid has another start, and an exec keeps pid and start but not the command.
    A read that failed is not remembered - it is tried again next scan."""
    cache = {} if cache is None else cache
    known = cache.get("_marks", {})
    now, out = {}, {}
    for pid, (start, command) in (table or {}).items():
        key = (pid, start, command)
        if key in known:
            mark = known[key]
        else:
            env = read_procargs(pid)
            if env is None:
                continue
            mark = procs.mark_of(env)
        now[key] = mark
        out[pid] = mark
    cache["_marks"] = now
    return out


def _lsof(args, env=None):
    """lsof's text, or None when it could not be asked. lsof exits 1 with
    nothing on stderr when nothing matched; exit 1 with an error, any other
    exit, or a timeout is None. Another user's processes are simply not listed
    (the whole-machine read still exits 0): "not read", as kill_plan wants."""
    exe = find_tool("lsof") or "/usr/sbin/lsof"
    try:
        done = subprocess.run([exe] + args, capture_output=True, text=True,
                              errors="replace", timeout=20, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode == 0 or (done.returncode == 1 and not (done.stderr or "").strip()):
        return done.stdout
    return None


CODEX_LOCK_TTL = 30.0  # seconds lsof's answer is kept while the lock files stay the same


def _forget_closed_threads(cache, home, held):
    """Drop what was kept per thread under `home` that no lock holds now: its
    rollout path, its missed transcript, its state read - a list stays open for
    days (item 4, 2026-10-01). A held lock with no transcript keeps its miss,
    or it is looked for again every scan."""
    paths = {cache.get(f"_codex_rollout:{home}:{t}") for t in held}
    for key in list(cache):
        if key.startswith((f"_codex_rollout:{home}:", f"_codex_miss:{home}:")):
            if key.rsplit(":", 1)[1] not in held:
                del cache[key]
        elif key.startswith("_codex_turn:"):
            path = key[len("_codex_turn:"):]
            if path.startswith(os.path.join(home, "")) and path not in paths:
                del cache[key]


def codex_threads(home, lsof=None, cache=None, clock=None):
    """The Codex threads open now, under the CODEX_HOME `home`: [{"thread",
    "name", "cwd", "host_pid", "originator", "source"}] - or None when lsof
    could not be asked ("not known" is never "none open"). A thread is open
    while a process holds its lock file (procs.parse_lsof_locks); that process
    serves it (measured: the ChatGPT app's codex serves the threads typed in VS
    Code too - `source` says where it was typed). Its name is the newest in
    session_index.jsonl; its folder, host and source come from its transcript's
    FIRST line (session_meta) only - the rest is what was said, and is never
    read. A lock with no transcript is no row: each thread came with such a
    second lock, taken in the same second - Codex's own (measured 2026-09-29).
    Only this CODEX_HOME is read: ccwho's own, else ~/.codex (codex_home)."""
    # lsof escapes the bytes of a non-ASCII path in a C locale (and ccwho may
    # start without a UTF-8 one): the paths it names must match those asked
    lsof = lsof or (lambda args: _lsof(args, env=dict(os.environ, LC_ALL="en_US.UTF-8")))
    cache = {} if cache is None else cache
    clock = clock or time.monotonic
    # lsof names files by their resolved path: a symlinked or relative home too
    lock_dir = os.path.realpath(os.path.join(home, "thread-writer-locks"))

    def listed():
        try:
            return sorted(f for f in os.listdir(lock_dir)
                          if f.endswith(".lock") and procs._UUID.fullmatch(f[:-len(".lock")]))
        except OSError:
            return []
    locks = listed()
    if not locks:
        _forget_closed_threads(cache, home, set())
        cache[f"_codex_watch:{home}"] = []      # no row: no rollout to watch
        return []
    # lsof reads every process's files: 0.5 s here in any form (measured
    # 2026-09-29). Its answer is kept for a TTL while the lock files are the
    # same - a new thread's lock is a new file, so it shows at once; a thread
    # closed shows open for up to the TTL, and nothing is killed on it (D17)
    key, now = f"_codex_locks:{lock_dir}", clock()
    kept = cache.get(key)
    if kept and kept[0] == locks and now - kept[1] < CODEX_LOCK_TTL:
        held = kept[2]
    else:
        text = lsof(["-nP", "-Fpn", "--"] + [os.path.join(lock_dir, f) for f in locks])
        if text is None and listed() != locks:
            # a thread closed between the listing and lsof ("status error"):
            # list again and ask once more
            locks = listed()
            if not locks:
                _forget_closed_threads(cache, home, set())
                cache[f"_codex_watch:{home}"] = []
                return []
            text = lsof(["-nP", "-Fpn", "--"] + [os.path.join(lock_dir, f) for f in locks])
        asked = {os.path.join(lock_dir, f) for f in locks}
        if text is None or any(line.startswith("n") and line[1:] not in asked
                               for line in text.splitlines()):
            # failed, or it named a file another way than it was asked (lsof
            # escapes bytes it cannot print): not known - never "none open"
            cache.pop(key, None)        # not known is never kept
            return None
        held = procs.parse_lsof_locks(text, lock_dir)
        cache[key] = (locks, now, held)
    names = {}
    try:
        with open(os.path.join(home, "session_index.jsonl"), errors="replace") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"] in held:
                    names[e["id"]] = e.get("thread_name") if isinstance(
                        e.get("thread_name"), str) else ""
    except OSError:
        pass
    out = []
    for thread in sorted(held):
        meta = _codex_meta(home, thread, cache, now)
        if meta is None:
            continue                    # no transcript: not a thread you can see
        if isinstance(meta.get("source"), dict) or meta.get("agent_path"):
            continue                    # a subagent's thread: its parent is the row
        def text(v):
            # plain printable text, as every other row: a name can hold escapes,
            # and Codex writes an object `source` for a subagent
            return procs.printable(v) if isinstance(v, str) else ""
        # its state, from its transcript's end (D19)
        turn = codex_turn(cache.get(f"_codex_rollout:{home}:{thread}") or "", cache)
        out.append({"thread": thread, "name": text(names.get(thread, "")),
                    "cwd": text(meta.get("cwd")), "host_pid": held[thread],
                    "originator": text(meta.get("originator")),
                    "source": text(meta.get("source")),
                    "turn": turn["turn"], "ask": text(turn["ask"]), "ts": turn["ts"],
                    "pending_tool": turn["pending_tool"], "mtime": turn["mtime"]})
    _forget_closed_threads(cache, home, set(held))
    # the rows' rollouts, for the list's quick check (watch_digest)
    cache[f"_codex_watch:{home}"] = [cache[f"_codex_rollout:{home}:{t['thread']}"] for t in out
                                     if cache.get(f"_codex_rollout:{home}:{t['thread']}")]
    return out


CODEX_TAIL = 256 * 1024   # bytes of a transcript's end read for its state
CODEX_QUIET = 120          # seconds a turn in progress may write nothing and still be busy


_TOOL_CALLS = ("function_call", "custom_tool_call", "local_shell_call", "tool_search_call")
_TOOL_OUTPUTS = ("function_call_output", "custom_tool_call_output", "local_shell_call_output",
                 "tool_search_output")
_BOUNDARIES = (b'"task_started"', b'"task_complete"', b'"turn_aborted"')


def _codex_event(line):
    """(kind, payload) of a transcript line that says something about the
    turn - a boundary, or a tool call or its output, or the model's message -
    else None. Only lines that name one are parsed."""
    if not any(m in line for m in _BOUNDARIES + (b'"response_item"',)):
        return None
    try:
        e = json.loads(line)
    except ValueError:
        return None
    p = e.get("payload") if isinstance(e, dict) else None
    kind = p.get("type") if isinstance(p, dict) else None
    if e.get("type") == "event_msg" and kind in ("task_started", "task_complete", "turn_aborted"):
        return kind, p
    if e.get("type") == "response_item" and kind in _TOOL_CALLS + _TOOL_OUTPUTS + ("message",):
        return kind, p
    return None


def _codex_step(state, line):
    """The state after one line, read forward."""
    ev = _codex_event(line)
    if ev is None:
        return
    kind, p = ev
    if kind == "task_started":
        # an open turn has no completion time: a cold read stops here, so a
        # time carried from an older turn would make two reads disagree
        state.update(turn="open", ask="", ts=None, pending_tool=False)
    elif kind == "task_complete":
        text = p.get("last_agent_message")
        text = text if isinstance(text, str) else ""
        state.update(turn="done", ts=p.get("completed_at"), pending_tool=False,
                     ask=_closing_line(text) if asks_user(text) else "")
    elif kind == "turn_aborted":
        # no completion time, as a new turn has none: a cold read stops here
        state.update(turn="aborted", ask="", ts=None, pending_tool=False)
    elif kind in _TOOL_CALLS:
        state["pending_tool"] = True
    else:                                   # an output, or the model's own words
        state["pending_tool"] = False


def codex_turn(path, cache=None):
    """A Codex thread's state from its transcript's END (the owner's D19):
    {"turn": "done" | "aborted" | "open" | None, "ask": the closing question
    when a done turn asks one, "ts": when the last turn completed,
    "pending_tool": a tool call with no output yet, "mtime"}. Codex writes
    task_started / task_complete (with the agent's last words) / turn_aborted,
    and never an approval request: a turn waiting for one is an open turn
    gone quiet on a tool call. The first read goes back, a CODEX_TAIL at a
    time, only as far as the last boundary (a long turn writes megabytes after
    its start); later reads read only what was added. Of the words, only the
    closing question is kept."""
    cache = {} if cache is None else cache
    empty = {"turn": None, "ask": "", "ts": None, "pending_tool": False, "mtime": None}
    try:
        st = os.stat(path)
    except OSError:
        return dict(empty)
    key = f"_codex_turn:{path}"
    kept = cache.get(key)
    ident = (st.st_size, st.st_mtime, st.st_ino)
    if kept and kept[0] == ident:
        return kept[1]
    state = {k: v for k, v in empty.items() if k != "mtime"}
    lines, offset = [], 0
    try:
        with open(path, "rb") as fh:
            if kept and kept[0][2] == st.st_ino and st.st_size > kept[0][0]:
                # grown, the same file: carry the state on from the end of the
                # last WHOLE line read - a line half written then is read whole now
                state.update({k: kept[1][k] for k in state})
                offset = kept[2]
                fh.seek(offset)
                block = fh.read()
                cut = block.rfind(b"\n") + 1
                lines = block[:cut].split(b"\n")
                offset += cut
            else:
                # back, a CODEX_TAIL at a time, looking only at each new chunk
                # (and the part-line carried from the one before): linear
                pieces, carry, pos = [], b"", st.st_size
                while pos > 0:
                    n = min(CODEX_TAIL, pos)
                    pos -= n
                    fh.seek(pos)
                    block = fh.read(n) + carry
                    part = block.split(b"\n")
                    carry, body = (part[0], part[1:]) if pos > 0 else (b"", part)
                    pieces.append(body)
                    # only a whole line is a boundary: the file's last line,
                    # before its newline, is still being written
                    whole = body[:-1] if len(pieces) == 1 else body
                    if any(_codex_event(l) and _codex_event(l)[0] in (
                            "task_started", "task_complete", "turn_aborted")
                           for l in whole if any(m in l for m in _BOUNDARIES)):
                        break
                lines = [l for body in reversed(pieces) for l in body]
                # the last line may be half written: it is read next time, whole
                if not lines or lines[-1] != b"":
                    tail_len = len(lines[-1]) if lines else 0
                    lines = lines[:-1]
                else:
                    tail_len = 0
                offset = st.st_size - tail_len
    except OSError:
        lines = []
    for line in lines:
        _codex_step(state, line)
    out = dict(state, mtime=st.st_mtime)
    cache[key] = (ident, out, offset)
    return out


def codex_attention(t, now):
    """A Codex thread's row state: asks / review / stopped / busy / waiting -
    or program: `codex exec` is started by a program, as `claude -p` (D22)."""
    if t.get("source") == "exec":
        return "program"
    turn = t.get("turn")
    if turn == "done":
        if t.get("ask"):
            return "asks"
        return "stopped" if t.get("ts") is not None and t.get("seen_ts") == t.get("ts") else "review"
    if turn == "open":
        # quiet on a tool call: a long build or an approval - Codex never says
        # which; quiet after the model's own words is it thinking: busy
        quiet = now - t["mtime"] if isinstance(t.get("mtime"), (int, float)) else 0
        return "waiting" if quiet > CODEX_QUIET and t.get("pending_tool", True) else "busy"
    return "stopped"


def codex_where(thread):
    """Where a Codex thread was typed, in words: its `source`, else its host."""
    source = thread.get("source") or ""
    named = {"vscode": "VS Code", "cli": "the codex CLI", "exec": "codex exec"}.get(source)
    if named:
        return named
    return "the ChatGPT app" if thread.get("originator") == "Codex Desktop" else "Codex"


def codex_there(row, verb):
    """Where a Codex row's thread runs, and that you `verb` ("open", "end") it
    there - ccwho can do neither. A `codex exec` thread is a program's (D22):
    there is nothing to open, and it ends when that program does."""
    where = row.get("where") or "Codex"
    if row.get("attention") == "program":
        return f"a program runs it ({where}) - " + (
            "nothing to open" if verb == "open" else "it ends when that program does")
    return f"it runs in {where} - {verb} it there"


def _tilde(path):
    """A path as a person reads it: ~ for the home folder."""
    home, path = os.path.expanduser("~"), path or ""
    return ("~" + path[len(home):]) if path == home or path.startswith(home + "/") else path


_CODEX_WORD = " (codex)"


def cut_codex_title(text, room):
    """`text` in `room` cells, its closing " (codex)" whole or not at all: the one
    word that says a line is a Codex thread's, and "(co…" says nothing. The title
    gives way first, while 8 cells of it still fit beside the word. In cells, as
    a screen is: a CJK character is two, an accent typed as its own mark none."""
    cut, text = _cut, text or ""
    if not text.endswith(_CODEX_WORD) or _cells(text) <= room:
        return cut(text, room)
    title = text[:-len(_CODEX_WORD)]
    if room - _cells(_CODEX_WORD) >= 8:
        return cut(title, room - _cells(_CODEX_WORD)) + _CODEX_WORD
    return cut(title, room)


def _wrap_cells(text, width):
    """`text` in lines of at most `width` cells: broken at spaces where it can be,
    inside a word only where one is wider than a line (a path, a CJK title)."""
    width, out, line = max(1, width), [], ""
    for word in text.split(" "):
        if _cells(f"{line} {word}" if line else word) <= width:
            line = f"{line} {word}" if line else word
            continue
        if line:
            out.append(line)
        while _cells(word) > width:
            n, used = 0, 0
            while n < len(word) and used + _cells(word[n]) <= width:
                used, n = used + _cells(word[n]), n + 1
            out.append(word[:max(n, 1)])
            word = word[max(n, 1):]
        line = word
    if line or not out:
        out.append(line)
    return out


def codex_rows(fleet, now=None):
    """The list's rows for the open Codex threads (D16), from collect()'s fleet.
    Never collect()'s own rows: those are Claude sessions to everything that
    reads them (--json, open, show)."""
    threads = (fleet or {}).get("codex_threads") if isinstance(fleet, dict) else None
    now = time.time() if now is None else now
    out = []
    for t in threads or []:
        if not isinstance(t, dict) or not isinstance(t.get("thread"), str):
            continue            # an odd item is no row - and never the end of the others
        cwd = t.get("cwd") or ""
        # one line, and no mark that reorders what is read (U+202E): a name is text
        named = procs._shown_line(_one_line(t.get("name") or ""))
        name = named or "a new thread"
        folder = _tilde(cwd)
        # what is drawn: one line, no mark that reorders it (U+202E) or hides in it
        clean = (lambda text: procs._shown_line(_one_line(text)))
        # a UUIDv7 starts with its time (the same four hex for weeks): the short
        # id is its random end
        out.append({"sessionId": t["thread"], "short": t["thread"][-4:],
                    "kind": "codex", "attention": codex_attention(t, now),
                    "ask": clean(t.get("ask") or ""), "ts": t.get("ts"),
                    "project": clean(os.path.basename(cwd.rstrip("/"))) or "codex",
                    # "Title (codex)", as iTerm2 shows a Claude session's tab
                    # "✳ Title (claude)" (D21); its age as a Claude row's
                    "name": "", "title": name, "tab_title": f"{name} (codex)",
                    # "a new thread" is ccwho's word: shown, never copied as a name
                    "named": bool(named),
                    "since": since(t.get("mtime"), now=now),
                    "where": codex_where(t), "folder": clean(folder), "cwd": cwd,
                    "mtime": t.get("mtime"),
                    "host_pid": t.get("host_pid"), "procs": t.get("procs") or 0,
                    "ports": t.get("ports"), "pids": list(t.get("pids") or [])})
    return out


def codex_detail_parts(row, fleet, width=80):
    """The detail of a Codex row: what it is, where it runs, and its processes -
    each a part the keys can be on (`x` kills it, Enter copies its pid), as on
    the process screen. [(line, value, field)]."""
    fleet = fleet if isinstance(fleet, dict) else {}
    # in cells, as the pane crops: a cut in characters never shows its "…"
    one = (lambda text: _cut(procs._shown_line(_one_line(text)), width))
    clean = (lambda text: procs._shown_line(_one_line(text)))
    att = row.get("attention", "")
    # as a Claude brief starts: its id, project and state, then its title
    head = "  ".join(clean(t) for t in (row.get("short") or "?", row.get("project") or "?",
                                        _LABEL.get(att, att)))
    title, cwd = clean(row.get("title") or "a new thread"), clean(row.get("cwd") or "")
    out = [(_cut(head, width), None, None),
           # the title and the folder whole - wrapped, never cut - and copied
           # whole, as a Claude brief's: one part over as many lines as it takes.
           # No indent: a lit part lights its own words. An unnamed thread's
           # "a new thread" is no name to copy
           ("\n".join(_wrap_cells(title, width)),
            *((title, "title") if row.get("named", True) else (None, None))),
           ("\n".join(_wrap_cells(cwd, width)), cwd, "cwd") if cwd
           else ("(no folder yet)", None, None),
           (one(f"thread {row.get('sessionId') or '?'}"), row.get("sessionId") or None,
            "session_id"),
           (one(f"Codex: {codex_there(row, 'open')}"
                + ("" if row.get("attention") == "program"
                   else "; ccwho cannot bring it forward")), None, None), ("", None, None)]
    mine = [p for p in fleet.get("codex") or []
            if p.get("session") == row.get("sessionId") and not p.get("helper")]
    # as a Claude brief's (session_procs_lines): unknown is said - an empty list
    # reads as "it started nothing" - and the columns line up
    known = fleet.get("ports_ok", True)
    if not fleet.get("procs_ok", True):
        out.append(("processes unknown - this list may be incomplete", None, None))
    elif not mine:
        out.append(("no processes of its own", None, None))
    for p in mine:
        ports = (" ".join(f":{n}" for n in _held(p)) or "-") if known else "?"
        pid = p.get("pid")
        command = clean(p.get("command") or "")
        line = _cut(f"{'?' if pid is None else pid!s:<7} {ports:<13} {command}", width)
        out.append((line, str(pid), "proc") if isinstance(pid, int) else (line, None, None))
    return out


def codex_home(env=None):
    """The CODEX_HOME whose threads the list shows: ccwho's own, else ~/.codex."""
    env = os.environ if env is None else env
    return env.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def read_codex_threads(env=None, cache=None):
    """codex_threads for this machine, or None when they could not be read: a
    second source, whose failure never blanks the list."""
    try:
        return codex_threads(codex_home(env), cache=cache)
    except Exception:
        return None


def codex_fleet(threads, att, ports_ok, reviewed=None):
    """Each open Codex thread with its work: the processes whose Codex mark names
    it (helpers left out) and their ports - None when the ports were not read.
    None when the threads are not known. A Codex process whose thread is not
    open is in no thread: it stays in att["codex"], state unknown (D17)."""
    if threads is None:
        return None
    out = []
    for t in threads:
        mine = [p for p in att.get("codex") or [] if p.get("session") == t["thread"]]
        summary = procs.row_summary(mine)
        out.append(dict(t, procs=summary["procs"],
                        # the finished turn you looked at: it leaves NEEDS YOU
                        seen_ts=(reviewed or {}).get(t["thread"]),
                        ports=summary["ports"] if ports_ok else None,
                        pids=sorted(p["pid"] for p in mine if not p.get("helper"))))
    return out


def _codex_meta(home, thread, cache, now=0.0):
    """A thread's session_meta payload - the first line of its rollout - {} when
    that line is no session_meta, or None when it has no rollout."""
    # a text key: collect() sweeps its cache by key.startswith("_"). Only a
    # found path is kept, and looked for again once it is gone (moved)
    key = f"_codex_rollout:{home}:{thread}"
    path = cache.get(key)
    if not path or not os.path.exists(path):
        # every thread has a lock with no transcript: not looked for every scan
        missed = cache.get(f"_codex_miss:{home}:{thread}")
        if missed is not None and now - missed < CODEX_LOCK_TTL:
            return None
        found = glob.glob(os.path.join(home, "sessions", "*", "*", "*",
                                       f"rollout-*-{thread}.jsonl"))
        if not found:
            cache.pop(key, None)
            cache[f"_codex_miss:{home}:{thread}"] = now
            return None
        cache.pop(f"_codex_miss:{home}:{thread}", None)
        path = cache[key] = sorted(found)[-1]
    try:
        with open(path, errors="replace") as fh:
            first = json.loads(fh.readline())
    except (OSError, ValueError):
        return {}
    payload = first.get("payload") if isinstance(first, dict) else None
    return (payload if isinstance(first, dict) and first.get("type") == "session_meta"
            and isinstance(payload, dict) else {})


def established_connections():
    """[(pid, laddr, lport, raddr, rport)] for every ESTABLISHED TCP socket, or
    None when lsof could not be asked or a line did not parse."""
    text = _lsof(["-nP", "-iTCP", "-sTCP:ESTABLISHED", "-Fpn"])
    return None if text is None else procs.parse_lsof_established(text)


def unix_sockets():
    """The kernel's unix sockets (address -> Conn) from `netstat -f unix -n`, or
    None when netstat could not be asked."""
    try:
        done = subprocess.run(["netstat", "-f", "unix", "-n"], capture_output=True, text=True,
                              errors="replace", timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return procs.parse_netstat_unix(done.stdout) if done.returncode == 0 else None


def pipe_links():
    """pid -> {"stdio", "other"} pipe peers for the whole machine (about 0.4s),
    or None when lsof could not be asked. See procs.parse_lsof_pipes."""
    text = _lsof(["-nP", "-F", "pftdn"])
    return None if text is None else procs.parse_lsof_pipes(text, unix_sockets())


def ps_world_text():
    """`ps -axo pid=,ppid=,lstart=,command=` in the C locale and UTC - the start
    in the text a session file stores - or None when ps could not be run or
    failed: that is no empty machine."""
    try:
        done = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart=,command="],
                              capture_output=True, text=True, errors="replace", timeout=20,
                              env=dict(os.environ, LC_ALL="C", TZ="UTC"))
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def world_sessions(rows):
    """The live sessions as kill_plan takes them - {"sessionId": str, "pid": an
    int >= 2 or None} - and how many rows could not be read. A pid as ASCII
    digits is that pid; anything else is not guessed: the row is counted, and
    the caller refuses (the live-session guard would be blind to it)."""
    out, bad = [], 0
    for r in rows or []:
        sid, pid = r.get("sessionId"), r.get("pid")
        if type(pid) is str and pid.isascii() and pid.isdigit() and len(pid) < 8:
            pid = int(pid)
        if type(sid) is not str or not (pid is None or (type(pid) is int and 2 <= pid < 10 ** 7)):
            bad += 1
            continue
        out.append({"sessionId": sid, "pid": pid})
    return out, bad


def _world_session_rows(table):
    """The session rows collect() reads - the agents list and the session files -
    and whether they are whole. `table` is pid -> (ppid, start, command)."""
    raw = agents_json()
    parsed = parse_sessions(raw) if raw is not None else None
    starts = {pid: (start, cmd) for pid, (_pp, start, cmd) in table.items()}
    try:
        file_rows, bad = live_file_sessions(starts)
    except Exception:           # a second source: its failure is "not whole", never a crash
        file_rows, bad = [], None
    whole = parsed is not None and read_is_complete(raw) and bad == 0
    return procs.merge_sessions(parsed or [], file_rows), whole


WORLD_READERS = {
    "ps": ps_world_text, "env": read_procargs, "sessions": _world_session_rows,
    "ports": listen_ports, "connections": established_connections, "pipes": pipe_links,
    "own": os.getpid,
}


def build_world(mine=None, readers=None, status=None):
    """The world kill_plan plans on, read now: the process table (one ps), each
    process's session mark (its environment; left out when it could not be
    read), the live sessions, attribute()'s answer, listening ports, ESTABLISHED
    connections and the pipe links - None for any that could not be read.

    `readers` replaces any of WORLD_READERS (tests). `status` is a caller-owned
    dict: status["trouble"] says why the caller must refuse although the world
    is in shape (a live session's pid could not be read)."""
    r = dict(WORLD_READERS, **(readers or {}))
    text = r["ps"]()
    if text is None:                    # no empty machine: nothing to plan on
        if status is None:
            raise ValueError("the process table could not be read")
        status.setdefault("trouble", "the process table could not be read")
    table = procs.parse_ps_world(text or "")
    marks = {}
    for pid in table:
        if pid < 1:
            continue                    # the kernel: no environment, no pid a kill takes
        env = r["env"](pid)
        if env is not None:
            marks[pid] = procs.mark_of(env)
    rows, whole = r["sessions"](table)
    sessions, bad = world_sessions(rows)
    ports, own = r["ports"](), r["own"]()
    running = procs.claude_pids({pid: (start, cmd) for pid, (_pp, start, cmd) in table.items()})
    known = (whole and bad == 0 and all(pid in marks for pid in running)
             and set(running) <= {s["pid"] for s in sessions})
    att = procs.attribute(table, marks, ports or {}, sessions, sessions_known=known, own=own)
    world = {"table": table, "att": att, "sessions": sessions, "own": own, "ports": ports,
             "pipes": r["pipes"](), "marks": marks, "connections": r["connections"]()}
    if mine is not None:
        world["mine"] = mine
    if bad:
        # the live-session guard is blind to that session: a caller that does
        # not take the trouble must not get a world it could plan on
        if status is None:
            raise ValueError("a live session's pid could not be read")
        status.setdefault("trouble", "a live session's pid could not be read")
    return world


def identity_of(pid):
    """(start, command) of `pid` in the text ps_world_text gives, or None when
    no process has that pid (or only a zombie: it has ended). An exec keeps
    the pid and the start; the command tells it. Raises OSError when ps could
    not answer: "gone" must be seen, never assumed."""
    try:
        done = subprocess.run(["ps", "-o", "stat=,lstart=,command=", "-p", str(pid)],
                              capture_output=True, text=True, errors="replace", timeout=10,
                              env=dict(os.environ, LC_ALL="C", TZ="UTC"))
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError(f"ps could not be run: {e}") from e
    f = done.stdout.split()
    if done.returncode == 1 and not f and not done.stderr.strip():
        return None                     # ps -p matched no process
    if done.returncode != 0 or len(f) < 7:
        raise OSError("ps gave no start and command")
    return None if f[0].startswith("Z") else (" ".join(f[1:6]), " ".join(f[6:]))


KILL_GRACE = 3.0        # seconds a signal gets before what still runs is named
_UNCHECKED = "could not check it again - not killed"
_UNREAD = object()


def carry_out(mode, target, confirmed, force=False, mine=None, act=None):
    """Signal what a person confirmed - `confirmed`, the "kill" list of
    kill_plan(mode, target, world) as shown - and say what happened.

    Before each round of signals the world is read again and planned again
    with the same target: only what both plans take, unchanged, is signalled
    (procs.still_to_kill) - every guard runs again. Right before each signal
    the pid's start, command and mark are read once more against that world
    (the start again after the mark). Pids only, never a group, never pid 1
    or ccwho; leaves first; SIGTERM, then what survived KILL_GRACE is named.
    SIGKILL only with `force`, after a new read, plan and check. Then the ports
    the signalled processes held are read again.

    Returns {"killed" (seen gone), "survivors" (signalled, still running or
    not seen gone; "why" when SIGKILL was not sent), "spare" (never
    signalled, with "why"), "new" (taken by the fresh plan, never confirmed:
    not signalled), "ports" (lines), "held" (those ports not known free)} -
    each confirmed pid in exactly one of
    the first three - and "why" when it stopped short: the world could not be
    read whole (nothing signalled), an interrupt, or an error after a signal.
    `act` replaces the machine (tests): build, plan, identity_of, env, send,
    sleep, clock, ports."""
    a = {"build": lambda mine=None, status=None: build_world(mine, status=status),
         "plan": procs.kill_plan, "identity_of": identity_of, "env": read_procargs,
         "send": os.kill, "sleep": time.sleep, "clock": time.monotonic, "ports": listen_ports}
    a.update(act or {})
    out = {"killed": [], "survivors": [], "spare": [], "new": [], "ports": [], "held": []}

    def read(entries):
        """A world read now, and what of `entries` it still takes - or why not."""
        status = {}
        w = a["build"](mine=mine, status=status)
        if status.get("trouble"):
            return None, None, status["trouble"]
        return w, procs.still_to_kill(entries, a["plan"](mode, target, w), w["table"]), None

    world, d, trouble = read(confirmed)
    if trouble:
        why = f"{trouble} - nothing killed"
        # unread: the machine could not be read again - could not tell (exit 4)
        return dict(out, spare=[dict(e, why=why) for e in confirmed], why=why, unread=True)
    out["spare"], out["new"] = list(d["spare"]), d["new"]
    own = {os.getpid(), world["own"]}

    def identity(pid):
        try:
            return a["identity_of"](pid)
        except OSError:
            return _UNREAD

    def check(e, w):
        """Why `e` must not be signalled now, or None."""
        pid = e["pid"]
        if pid <= 1 or pid in own:
            return "ccwho never signals it - not killed"
        first = identity(pid)
        if first is None:
            return procs.EXITED
        row = w["table"].get(pid)
        if first is _UNREAD:
            return _UNCHECKED
        if row is None or procs.printable(first[0]) != e["start"] or first[0] != row[1]:
            return procs.OTHER
        if first[1] != row[2]:
            return procs.EXECED                  # an exec keeps pid and start: it still runs
        env = a["env"](pid)
        mark = procs.mark_of(env) if env is not None else None
        before = w["marks"].get(pid, _UNREAD) if w.get("marks") is not None else _UNREAD
        if env is None:
            if e.get("marked") is not None:
                return _UNCHECKED
        elif before is not _UNREAD:
            if (mark[1] if mark else None) != (before[1] if before else None):
                return "its session mark changed since the list - not killed"
        elif procs.marked_of(env) != e.get("marked"):
            return "its session mark changed since the list - not killed"
        second = identity(pid)
        if second != first:
            if second is None or second is _UNREAD:
                return procs.EXITED if second is None else _UNCHECKED
            return procs.OTHER if second[0] != first[0] else procs.EXECED
        return None

    # pid -> (state, entry): "sent", "gone" (seen gone after a signal), "held"
    # (sent, then refused SIGKILL), "spare" (never sent). One key per pid: an
    # interrupt cannot leave an entry in two lists.
    state, flight = {}, [None]

    def send(e, w, sig):
        """Signal `e` after check(); None when the signal went out, else why not."""
        why = check(e, w)
        if why is not None:
            return why
        flight[0] = e                       # from here its signal may have gone out
        try:
            a["send"](e["pid"], sig)
        except ProcessLookupError:
            why = procs.EXITED
        except PermissionError:
            why = "belongs to another user - not killed"
        except OSError as err:
            why = f"could not be signalled ({err.strerror or type(err).__name__}) - not killed"
        if why is None:
            state[e["pid"]] = ("sent", e)
        flight[0] = None
        return why

    def running(e):
        """True, False, or None when ps could not say."""
        now = identity(e["pid"])
        if now is _UNREAD:
            return None
        return now is not None and procs.printable(now[0]) == e["start"]

    def wait(entries):
        """Those of `entries` still running after KILL_GRACE - gone must be seen."""
        end, left = a["clock"]() + KILL_GRACE, list(entries)
        while left:
            still = []
            for e in left:
                if running(e) is False:
                    state[e["pid"]] = ("gone", e)
                else:
                    still.append(e)
            left = still
            if not left or a["clock"]() >= end:
                break
            a["sleep"](0.1)
        return left

    def sigkill(survivors):
        """Each survivor planned again as its own pid on a world read now - its
        root may have ended - so every guard runs again; then SIGKILL."""
        status = {}
        w2 = a["build"](mine=mine, status=status)
        by_pid = {e["pid"]: e for e in survivors}
        killing = []
        for pid in procs.leaves_first(list(by_pid), w2["table"]):
            e = by_pid[pid]
            if status.get("trouble"):
                why = status["trouble"]
            else:
                fresh = a["plan"]("pid", {"pid": pid, "start": e["start"]}, w2)
                d2 = procs.still_to_kill([e], fresh, w2["table"])
                # forked since: what neither the person confirmed nor "new" lists yet
                known = {(x["pid"], x["start"]) for x in confirmed + out["new"]}
                out["new"] += [x for x in d2["new"] if (x["pid"], x["start"]) not in known]
                why = send(d2["go"][0], w2, signal.SIGKILL) if d2["go"] else d2["spare"][0]["why"]
            if why is None:
                killing.append(e)
            elif why in (procs.EXITED, procs.OTHER) and running(e) is False:
                state[pid] = ("gone", e)                # seen: the process that got SIGTERM ended
            else:
                state[pid] = ("held", dict(e, why=f"SIGKILL not sent: {why}"))
        wait(killing)

    def stopped(err):
        """Signals went out and `err` stopped the rest: say so; what got a
        signal and was not seen gone is never "not killed"."""
        stop = "interrupted" if isinstance(err, KeyboardInterrupt) else \
            f"ccwho failed ({type(err).__name__}: {procs.safe_command(str(err), program=False)[:80]})"
        out["why"] = f"{stop} - run ccwho ps to see what still runs"
        for e in d["go"]:
            k, entry = state.get(e["pid"], (None, e))
            if k == "sent":
                state[e["pid"]] = ("held", dict(entry, why=f"not seen gone - {stop}"))
            elif k is None:
                maybe = flight[0] is not None and flight[0]["pid"] == e["pid"]
                state[e["pid"]] = (("held", dict(e, why=f"may have got SIGTERM - {stop}")) if maybe
                                   else ("spare", dict(e, why=f"not signalled - {stop}")))

    try:
        for e in d["go"]:
            why = send(e, world, signal.SIGTERM)
            if why is not None:
                state[e["pid"]] = ("spare", dict(e, why=why))
        survivors = wait([e for e in d["go"] if state.get(e["pid"], ("",))[0] == "sent"])
        if force and survivors:
            sigkill(survivors)
    except (Exception, KeyboardInterrupt) as err:
        signalled = flight[0] is not None or any(k in ("sent", "gone", "held")
                                                 for k, _e in state.values())
        if not signalled:
            err.ccwho_nothing_signalled = True  # the caller may say "nothing killed"
            raise
        stopped(err)
    base = list(out["spare"])

    def tally():
        by = {"gone": [], "sent": [], "held": [], "spare": []}
        for e in d["go"]:
            k, entry = state[e["pid"]]
            by[k].append(entry)
        out["killed"], out["survivors"] = by["gone"], by["sent"] + by["held"]
        out["spare"] = base + by["spare"]
        return by["gone"] + by["sent"] + by["held"]

    # whatever stops the rest from here, the report is returned
    try:
        termed = tally()
        if "why" in out:
            out["held"] = sorted({p for e in termed for p in e.get("ports") or []})
            return out
        try:
            listen = a["ports"]()
        except Exception as err:            # an interrupt is an interrupt: the outer handler
            out["why"] = f"the ports could not be read again ({type(err).__name__})"
            listen = None
        wanted = sorted({p for e in termed for p in e.get("ports") or []})
        # the ports not known to be free: still held, or not read again
        out["held"] = [p for p in wanted if listen is None or any(p in v for v in listen.values())]
        out["ports"] = procs.port_report([p for e in termed for p in e.get("ports") or []], listen,
                                         world["table"], {e["pid"] for e in termed},
                                         ended={e["pid"] for e in out["killed"]})
    except (Exception, KeyboardInterrupt) as err:
        stopped(err)
        termed = tally()
        out["held"] = sorted({p for e in termed for p in e.get("ports") or []})
    return out


def agents_json():
    """The live session list as JSON text, or None when the SOURCE is unavailable.

    None and "[]" are different answers and must never be collapsed together.
    "[]" is claude saying you have nothing open; None is us being unable to ask
    - binary off PATH (a launchd job's minimal environment does exactly this),
    a non-zero exit, a timeout. Returning "[]" for both is how a save writes
    "you had nothing open" over the record of what you did.
    """
    exe = find_tool("claude")
    if not exe:
        return None
    try:
        done = subprocess.run(
            [exe, "agents", "--json"], capture_output=True, text=True, errors="replace", timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


# ------------------------------------------------------------ restore manifest
#
# A reboot is a ONE-WAY DOOR today. The sessions themselves survive - the transcript
# is ~/.claude/projects/<slug>/<sessionId>.jsonl and `claude --resume <id>` reopens
# it - but nothing on disk records WHICH sessions were open, so a fleet of eleven is
# unrecoverable in practice and the machine never gets rebooted. That is how the swap
# file reached 96% full.
#
# `ccwho save` writes what is live to ~/.ccwho/restore/ (under $HOME: it must outlive
# the reboot, which is why it is not in a temp dir). `ccwho restore` reads it back.

MANIFEST_VERSION = 1

# A session id has to be safe in a shell line AND look like an id. shlex.quote alone
# would already make injection impossible; refusing a malformed id as well means the
# user is TOLD it was skipped instead of getting a command that fails obscurely.
_SESSION_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{7,63}\Z")
_MANIFEST_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}\.json\Z")

_MANIFEST_KEYS = ("sessionId", "cwd", "project", "topic", "first", "ask",
                  "attention", "status", "tty", "pid", "since", "configDir", "terminal",
                  "entrypoint")


def in_temp_dir(path, temp_roots):
    """Is this path inside one of temp_roots? Pure: the caller resolves symlinks
    (/tmp is /private/tmp) and names the roots. A root's sibling that shares its
    prefix - /private/tmpfoo - is not inside it."""
    if not path:
        return False
    for root in temp_roots or []:
        root = root.rstrip("/")
        if root and (path == root or path.startswith(root + "/")):
            return True
    return False


def same_process(row, was):
    """Is this live row the process a saved entry was? The same tty and the same
    pid: a process keeps both while it runs, and a pane keeps its tty while its
    process runs. A session resumed elsewhere, or after a reboot, has another
    pid. No pid or no tty (None, 0, "") is no answer, and never a match - and
    str(None) is "None", so the values are checked before they are compared."""
    tty, was_tty = row.get("tty"), was.get("tty")
    pid, was_pid = row.get("pid"), was.get("pid")
    if not (isinstance(tty, str) and isinstance(was_tty, str) and tty and was_tty
            and pid and was_pid):
        return False
    return (terms.short_tty_full(tty) == terms.short_tty_full(was_tty)
            and str(pid) == str(was_pid))


def manifest_from_rows(rows, now=None, why_not=None, panes=None, known=None, boot=None,
                       env_panes=None):
    """Capture the live fleet. Pure: the caller supplies the rows and the clock.

    Order is the caller's (collect() sorts needs-you first), so the restore list
    reads in the same order as the dashboard the user was looking at.

    why_not(row) -> "" or the reason this session could never be resumed. Such a
    row is counted in `skipped`, under its reason in `skippedWhy`, and not saved:
    a restore would only open a window onto the error.

    panes: tty -> {"pane", "name"} from terms.ITERM2.panes(). Each entry records the
    pane it was shown in and that tab's title, so a restore can fill the pane
    iTerm2 brings back instead of opening a window beside it.

    panes=None means iTerm2 was not asked: each entry then takes the pane its
    process names (`env_panes`: str(pid) -> procs.parse_iterm_pane), else the
    pane from `known` (sessionId -> that session's entry in the last save), and
    its title from `known` either way - only for the same process
    (same_process): a session id does not name a pane. The caller gives
    `known` only from a save of this boot.
    panes={} means iTerm2 answered with none: no pane is recorded.

    boot: this boot's id, recorded so the next save can tell a save made before
    a reboot, whose pids and ttys are given out again.
    """
    now = int(time.time() if now is None else now)
    kept, why_count = [], {}
    for r in rows or []:
        if not r.get("sessionId") or not r.get("cwd"):
            why = "no id or no cwd"   # no id to resume, or nowhere to cd
        else:
            why = why_not(r) if why_not else ""
        if why:
            why_count[why] = why_count.get(why, 0) + 1
            continue
        kept.append({k: r.get(k, "") for k in _MANIFEST_KEYS})
        tty = terms.short_tty_full(r.get("tty", ""))
        kept[-1]["pane"] = ((panes or {}).get(tty) or {}).get("pane", "") if tty else ""
        # the row's name, else the one iTerm2 gave with the panes: a save's names
        # ask can meet a busy gate while its panes ask, which waits, is answered
        from_panes = ((panes or {}).get(tty) or {}).get("name", "") if tty else ""
        kept[-1]["tabTitle"] = r.get("tab_title", "") or from_panes or ""
        if panes is None and known:
            # iTerm2 was not asked: what the last save knew beats knowing nothing
            # - five hours of such saves would push every good one out
            was = known.get(r.get("sessionId")) or {}
            # read off disk: only text is carried - anything else would ride
            # into every save and break the restore that reads it. A pane is
            # iTerm2's: never carried onto a row another app shows
            pane, title = was.get("pane"), was.get("tabTitle")
            ours = r.get("terminal", "") in ("", terms.ITERM2.key)
            same = same_process(r, was)
            kept[-1]["pane"] = pane if isinstance(pane, str) and ours and same else ""
            kept[-1]["tabTitle"] = kept[-1]["tabTitle"] or (
                title if isinstance(title, str) and same else "")
        own = (env_panes or {}).get(str(r.get("pid", ""))) if panes is None else None
        if isinstance(own, str) and own and r.get("terminal", "") == terms.ITERM2.key:
            # the pane the process itself names: it holds for a session no save
            # has seen, and one /clear gave a new id (2026-10-05: 9 of 12 had
            # none). Only on a row ps places in iTerm2: a terminal started from
            # an iTerm2 shell inherits the variable
            kept[-1]["pane"] = own
    return {"version": MANIFEST_VERSION, "savedAt": now, "boot": boot, "count": len(kept),
            "skipped": sum(why_count.values()), "skippedWhy": why_count,
            "sessions": kept}


def restore_command(entry):
    """The shell line that reopens one session, or None if it cannot be built.

    The `cd` is NOT needed to FIND the session - measured 2026-08-31, `claude
    --resume <id>` resolves globally, from any directory. It is needed so the
    resumed session operates in its own project: relative paths, the repo it edits,
    the CLAUDE.md it loads. Getting that wrong is silent, not an error.
    """
    entry = entry if isinstance(entry, dict) else {}
    # an id is text: a number off disk passes the pattern as str(), and every
    # lookup by it raises (review 8 of env-panes)
    sid = entry.get("sessionId", "")
    sid = sid if isinstance(sid, str) else ""
    cwd = str(entry.get("cwd", "") or "")
    if not cwd or not _SESSION_ID.match(sid):
        return None
    # a session from another config dir is only found there
    conf = str(entry.get("configDir", "") or "")
    env = "CLAUDE_CONFIG_DIR=%s " % shlex.quote(conf) if conf else ""
    return "cd %s && %sclaude --resume %s" % (shlex.quote(cwd), env, shlex.quote(sid))


def manifest_entries(manifest):
    """Sessions out of a manifest read off disk. Tolerates anything: a file that
    predates a format change must degrade to 'nothing saved', never a traceback."""
    if not isinstance(manifest, dict):
        return []
    sessions = manifest.get("sessions")
    if not isinstance(sessions, list):
        return []
    return [s for s in sessions if isinstance(s, dict)]


def manifest_name(now=None):
    """ISO-ish and lexicographically sortable, so newest_manifest needs no stat."""
    return time.strftime("%Y-%m-%dT%H%M.json",
                         time.localtime(time.time() if now is None else now))


def newest_manifest(names):
    found = sorted(n for n in (names or []) if n and _MANIFEST_NAME.match(n))
    return found[-1] if found else None


def boot_time(text):
    """When the Mac last started, from `sysctl -n kern.boottime`, or None."""
    m = re.search(r"\bsec\s*=\s*(\d+)", text or "")
    return int(m.group(1)) if m else None


def save_points(saves, live_ids=(), booted=None):
    """Every save `o` can reopen, newest first.

    `saves` is (file name, manifest) pairs; a manifest that could not be read is
    None, and is listed with count None rather than dropped - a save that has
    gone missing from the list is one nobody knows to look for.

    The autosave keeps writing after a restart, so the newest save can hold
    only what was started since (the loops, first). The last save from BEFORE
    the restart is the fleet the restart took away: it is marked.
    """
    live = set(live_ids or ())
    points = []
    for name, man in saves or ():
        if not (name and _MANIFEST_NAME.match(name)):
            continue
        at = man.get("savedAt") if isinstance(man, dict) else None
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            try:
                at = int(time.mktime(time.strptime(name, "%Y-%m-%dT%H%M.json")))
            except ValueError:
                at = None
        # a string, or it is not a session: one damaged entry must not stop the menu
        sids = ({e.get("sessionId") for e in manifest_entries(man)
                 if isinstance(e.get("sessionId"), str) and e.get("sessionId")}
                if isinstance(man, dict) else None)
        running = len(sids & live) if sids is not None else 0
        # what a restore leaves where it ran (no_window): not "to reopen" - by
        # the first entry of an id, as restore opens it
        first = {e["sessionId"]: e for e in first_entries(manifest_entries(man))
                 if isinstance(e.get("sessionId"), str) and e.get("sessionId")}
        away = {sid for sid, e in first.items() if left_by_restore(e)} - live
        points.append({"name": name, "at": at,
                       "count": len(sids) if sids is not None else None,
                       "running": running, "left": len(away),
                       "to_open": len(sids) - running - len(away) if sids is not None else 0,
                       "before_reboot": False})
    points.sort(key=lambda p: p["name"], reverse=True)
    if booted:
        for p in points:
            # a save it cannot read cannot bring the fleet back: not that one
            if p["at"] is not None and p["at"] < booted and p["count"] is not None:
                p["before_reboot"] = True
                break
    return points


def _when(at, now):
    """today 12:22, yesterday 18:04, or Thu 24 Sep 09:10 - local time."""
    t, day = time.localtime(at), time.localtime(now)
    if t[:3] == day[:3]:
        return "today " + time.strftime("%H:%M", t)
    if t[:3] == time.localtime(now - 86400)[:3]:
        return "yesterday " + time.strftime("%H:%M", t)
    return time.strftime("%a %d %b %H:%M", t)


def save_point_line(point, now=None, booted=None):
    """One line of the `o` menu: when, how many, and what reopening it would do."""
    now = time.time() if now is None else now
    when = _when(point["at"], now) if point.get("at") is not None else point.get("name", "?")
    n = point.get("count")
    if n is None:
        return f"{when}   unreadable"
    what = f"{n} session{'' if n == 1 else 's'}"
    running, to_open = point.get("running", 0), point.get("to_open", n)
    # what a restore leaves (Claude Desktop's, a program's) is not "to reopen",
    # and gets no words of its own: the menu shows 94 cells, and the mark of
    # the save before the restart must stay in them (review 3 of env-panes)
    left = point.get("left", 0)
    if n and not to_open and not left:
        what += " · all running"
    elif running:
        what += f" · {running} running, {to_open} to reopen"
    elif left:
        what += f" · {to_open} to reopen"
    mark = ""
    if point.get("before_reboot"):
        mark = "   ← last save before the restart" + (
            f" ({_when(booted, now)})" if booted else "")
    return f"{when}   {what}{mark}"


def manifest_transcript_finder(manifest):
    """transcript_for for check_manifest: a saved session from another config dir
    has its transcript under that dir, not under the known roots."""
    dirs = [e.get("configDir") for e in manifest_entries(manifest)
            if isinstance(e.get("configDir"), str) and e.get("configDir")]
    roots = all_roots({"_extra_roots": dirs})
    return lambda sid: transcript_path(sid, roots=roots)


def check_manifest(manifest, problem_of):
    """Would this manifest actually restore? Returns (ok, problems, left, ready):
    problems [(name, why)], left [(entry, why)] - what `restore --open` leaves
    where it ran - and ready, the entries it would reopen.

    Pure: the caller injects `problem_of(entry)` - why that one saved session
    would not resume, or "" (the caller's resume_problem: the one `--open` and
    `ccwho open` ask). Each entry is sorted by sort_saved, the first of each id
    (first_entries): `--check` says what `--open` does (the owner, 2026-10-06).
    Answers the only question that matters before a reboot, and answers it
    while you can still fix it.

    An EMPTY manifest fails. "Nothing to restore" is the exact shape of the bug
    this tool exists to prevent, so it must never read as a clean bill of health.
    """
    entries = first_entries(manifest_entries(manifest))
    if not entries:
        return (False, [("(manifest)", "no sessions in it - run `ccwho save` while they are open")],
                [], [])
    problems, left, ready = [], [], []
    for e_ in entries:
        kind, why = sort_saved(e_, problem_of)
        if kind == "left":
            left.append((e_, why))
        elif kind == "open":
            ready.append(e_)
        else:
            problems.append((saved_name(e_), why))
    return (not problems), problems, left, ready


def saved_name(entry):
    """A saved session as restore names it: its project, else its id."""
    return str(entry.get("project") or entry.get("sessionId") or "?")


def sort_saved(entry, problem_of):
    """What `restore --open` does with one saved session that is not running -
    and so what `--check` says it will: ("unusable", why) - no resume line
    builds; ("left", why) - Claude Desktop or a program ran it (no_window);
    ("gone", why) - `problem_of(entry)` says it would not resume; else
    ("open", "")."""
    if not restore_command(entry):
        return "unusable", "no resume line can be built (bad session id, or no cwd)"
    away = no_window(entry)
    if away:
        return "left", away
    why = problem_of(entry)
    return ("gone", why) if why else ("open", "")


def first_entries(entries):
    """The first saved entry of each session id, as `restore --open` takes them:
    one process per transcript, however the manifest got two. An id that
    cannot be a key - a damaged entry - is its own."""
    seen, out = set(), []
    for e_ in entries:
        sid = e_.get("sessionId", "")
        try:
            if sid in seen:
                continue
            seen.add(sid)
        except TypeError:
            pass
        out.append(e_)
    return out


def left_by_restore(entry):
    """Why `restore --open` leaves this saved session where it ran (no_window),
    or "". Only one it could reopen: one with no resume line is a problem,
    wherever it ran - so --check, the `o` menu and --open say the same."""
    return no_window(entry) if restore_command(entry) else ""


def no_window(entry):
    """Why a saved session had no terminal window to put back, or "".

    2026-10-05: `o` after a reboot opened two Claude Desktop sessions, idle for
    days, in new iTerm2 windows. A restore puts back what was in a terminal;
    the rest stays where it was, and `ccwho open <id>` opens one (choice A).
    Only what started it says so. An empty saved tty proves nothing: a save
    whose ps could not be read records every session with none, and they
    reopen as before (review 1 of env-panes)."""
    if not isinstance(entry, dict):
        return ""
    ep = entry.get("entrypoint")
    if ep == "claude-desktop":
        return "it ran in Claude Desktop"
    if ep in ccwho_index.AGENT_ENTRYPOINTS:
        return "a program ran it"
    return ""


def entry_problem(entry, cwd_exists, transcript_for):
    """Why this one saved session would NOT resume, or "" if it would.

    One answer for `--check` and `--open`: when only the check asked, a restore
    opened eight windows the check had already failed.
    """
    if not restore_command(entry):
        return "no resume line can be built (bad session id, or no cwd)"
    if not isinstance(entry.get("cwd", ""), str):
        return "its saved cwd is not a path"
    if not cwd_exists(entry.get("cwd", "")):
        return "cwd is gone: %s" % entry.get("cwd", "")
    if not transcript_for(entry.get("sessionId", "")):
        return "transcript is gone - nothing left to resume"
    return ""


def render_restore(manifest, color=True, width=None, links=False):
    """The post-reboot view: what was open, what it was about, how to get it back."""
    c = _C if color else {k: "" for k in _C}
    entries = manifest_entries(manifest)
    m = manifest if isinstance(manifest, dict) else {}
    when = m.get("savedAt")
    stamp = ""
    if isinstance(when, (int, float)):
        stamp = time.strftime(" (saved %a %d %b %H:%M)", time.localtime(when))
    out = []
    if not entries:
        return "ccwho restore: no sessions in the manifest%s\n" % stamp

    out.append("%s%d session%s to restore%s%s\n" % (
        c["bold"], len(entries), "" if len(entries) == 1 else "s", stamp, c["reset"]))
    for i, s_ in enumerate(entries, 1):
        cmd = restore_command(s_)
        name = _text(s_.get("project")) or "?"
        head = "%s%2d. %s%s" % (c["bold"], i, osc8(name, open_url(s_), links), c["reset"])
        was = _text(s_.get("tty"))
        away = left_by_restore(s_)
        out.append("%s  %s%s%s\n" % (head, c["dim"], f"{away} - --open leaves it" if away
                                      else ("was " + was) if was else "", c["reset"]))
        # BOTH halves, because neither alone identifies a session. Measured on a real
        # 17-session fleet: `topic` (the latest turn) read "/compact", "go ahead" and
        # "let's fix 1-3" for 3 of them, while `first` read "restart from disk" for 1.
        opened = _text(s_.get("first")).strip()
        latest = _text(s_.get("topic")).strip()
        w = (width or 100) - 14
        if opened and latest and opened != latest:
            out.append("      %sopened:%s %s\n" % (c["dim"], c["reset"], truncate(opened, w)))
            out.append("      %slatest:%s %s\n" % (c["dim"], c["reset"], truncate(latest, w)))
        elif opened or latest:
            out.append("      %s\n" % truncate(latest or opened, (width or 100) - 6))
        ask = _text(s_.get("ask"))
        if ask:
            out.append("      %sASKED YOU: %s%s\n" % (
                c["asks"], truncate(ask, (width or 100) - 17), c["reset"]))
        if cmd:
            out.append("      %s%s%s\n" % (c["dim"], cmd, c["reset"]))
        else:
            # kept visible rather than dropped: a session you cannot reopen from here
            # is exactly the one you would otherwise spend an hour hunting for.
            out.append("      %sno resume line (bad id or no cwd) - find it with: claude --resume%s\n"
                       % (c["dim"], c["reset"]))
    skipped = m.get("skipped") if type(m.get("skipped")) is int else 0
    why = m.get("skippedWhy")
    if skipped and isinstance(why, dict) and why:
        # a manifest from before skippedWhy only ever skipped for the one reason
        reasons = "; ".join("%s %s" % (n, w) for w, n in sorted(why.items()))
        out.append("\n%s%d live session(s) not saved - they could not be resumed: %s%s\n"
                   % (c["dim"], skipped, reasons, c["reset"]))
    elif skipped:
        out.append("\n%s%d live session(s) could not be captured (no id or no cwd)%s\n"
                   % (c["dim"], skipped, c["reset"]))
    return "".join(out)


def _text(value):
    """A saved value as text: a manifest is read off disk, and a value of
    another type is shown as nothing, never raised on (review 9 of env-panes)."""
    return value if isinstance(value, str) else ""


_TITLE_MARK = re.compile(r"^[^\w\s]+(\s+|\Z)")


def title_key(title):
    """A tab title without the status mark Claude Code puts in front of it.
    Measured: "✳ topic" at rest, "◐ topic" and "◑ topic" while it works - the
    mark when a save ran need not be the mark iTerm2 restored. A title off
    disk that is not text has none."""
    return _TITLE_MARK.sub("", (title if isinstance(title, str) else "").strip()).strip()


def match_panes(entries, panes, idle, saved=None):
    """sessionId -> the unique id of the live pane to resume it in.

    panes: tty -> {"pane", "name"}, iTerm2 now. idle: the ttys at a bare shell.
    Only an idle pane is ever chosen - a command typed into a program, or into
    a prompt someone is using, is worse than a new window.

    First by the pane id the save recorded, which iTerm2 keeps when it restores
    its windows. Then, for a pane restored some other way (a new id), by the tab
    title (title_key: the status mark in front does not count) - but only when
    exactly one saved session and exactly one pane, busy or not, have it, and
    that pane is idle. `saved` is the whole manifest (default: entries): a
    session that is not being opened still makes its title ambiguous.

    A pane id found on two ttys matches nothing: iTerm2 does not promise ids are
    unique, and one saved session written into two panes is two processes on
    one transcript.
    """
    ids = [p.get("pane") for p in (panes or {}).values() if p.get("pane")]
    names = {p["pane"]: title_key(p.get("name", "")) for p in (panes or {}).values()
             if p.get("pane") and ids.count(p["pane"]) == 1}
    live = {p["pane"] for tty, p in (panes or {}).items()
            if tty in (idle or ()) and p.get("pane") in names}
    saved_titles = [title_key(o.get("tabTitle")) for o in (saved if saved is not None
                                                          else entries) or []]
    got, used = {}, set()
    for e_ in entries or []:
        uid = e_.get("pane")
        uid = uid if isinstance(uid, str) else ""       # off disk: text, or no pane
        if uid in live and uid not in used:
            got[e_.get("sessionId", "")] = uid
            used.add(uid)
    for e_ in entries or []:
        if e_.get("sessionId", "") in got:
            continue
        title = title_key(e_.get("tabTitle"))
        if not title or saved_titles.count(title) != 1:
            continue
        same = [u for u, name in names.items() if name == title]
        if len(same) == 1 and same[0] in live and same[0] not in used:
            got[e_.get("sessionId", "")] = same[0]
            used.add(same[0])
    return got


def open_script(app, entries, fill=None):
    """AppleScript that reopens each restorable session in `app` (a terms.App):
    in the pane `fill` names for it (sessionId -> pane unique id; iTerm2 only),
    else in a new window (App.open_script, which says how).

    An entry whose resume line cannot be built is DROPPED rather than emitted
    broken: a window that opens onto a failed command is worse than no window.
    """
    fill = fill or {}
    items = [(fill.get(e_.get("sessionId", "")), cmd)
             for e_ in entries or [] for cmd in [restore_command(e_)] if cmd]
    return app.open_script(items)


# -------------------------------------------------------------------- assemble

def parent_map(ps_output):
    """pid -> ppid, from the ps output collect already takes."""
    out = {}
    for pid, ppid, _cmd in procs.ps_rows(ps_output):
        try:
            out[int(pid)] = int(ppid)
        except (TypeError, ValueError):
            continue
    return out


def command_map(ps_output):
    """pid -> command, so a window can be asked what it is showing."""
    out = {}
    for pid, _ppid, cmd in procs.ps_rows(ps_output):
        try:
            out[int(pid)] = cmd
        except (TypeError, ValueError):
            continue
    return out


# The daemon's own processes: they run a session, they do not show one.
_NOT_A_VIEWER = re.compile(r"\bclaude\s+(bg-spare|bg-pty-host|daemon)\b")


def is_viewer(command, session_id=""):
    """Is this process a terminal SHOWING a session?

    `claude --resume`, `claude attach <id>`, plain `claude` - yes. The daemon
    and its helpers run sessions without showing them, and a shell is just a
    shell: a session started with `claude --bg` from a prompt has that shell as
    an ancestor, and jumping to its window puts you in front of a terminal that
    knows nothing about the session.
    """
    cmd = (command or "").strip()
    if not cmd or _NOT_A_VIEWER.search(cmd):
        return False
    if is_attach_to(cmd, session_id):
        return True
    head = os.path.basename(cmd.split()[0].strip("-"))
    return head == "claude"


def attached_ttys(session_id, ttys, commands):
    """The terminals running `claude attach <id>`, in ps order.

    An attach can be anywhere - it is not an ancestor of the session it shows -
    so it is looked for by name rather than walked to.
    """
    if not session_id:
        return []
    return [ttys[pid] for pid, cmd in (commands or {}).items()
            if is_attach_to(cmd, session_id) and ttys.get(pid)]


def owning_tty(pid, parents, ttys, titles, depth=12, commands=None,
               session_id=""):
    """The terminal a session is DISPLAYED in, which is not always its own.

    Measured on a session running under the Claude Code daemon:

        28087  claude bg-spare      ttys042   <- what `claude agents` reports
        27890  claude bg-pty-host
        27713  claude daemon run
         1378  claude --resume      ttys000   <- the window on screen

    The feed names a background process on a tty no window owns, while the
    session is plainly in front of you. Its ancestor is the terminal showing
    it, so walk up to a tty a terminal app shows. A `claude attach` shows it
    too, from anywhere.

    `titles` is the set of ttys the terminal apps show - collect() passes what
    survey() says of the running ones, from ps. With none, nothing is known
    about windows and the session's own tty stands - never a guess dressed as
    an answer.

    Of the terminals showing it, the first in a window: an attach before an
    ancestor, in ps order. With none in a window, the first there is.
    """
    viewers = attached_ttys(session_id, ttys, commands)
    seen = set()
    while pid and pid not in seen and depth > 0:
        seen.add(pid)
        depth -= 1
        tty = ttys.get(pid, "")
        # Only a process that SHOWS the session counts. Without commands to
        # look at, the old rule stands rather than reporting no window.
        if tty and (commands is None or is_viewer(commands.get(pid, ""), session_id)):
            viewers.append(tty)
        pid = parents.get(pid)
    return next((tty for tty in viewers if not titles or terms.short_tty_full(tty) in titles),
                viewers[0] if viewers else "")


def windowed(tty, titles, blind=()):
    """Is there a terminal window to go to?

    True, False, or None for "we could not tell". The third matters: iTerm2 shut
    is not every session losing its window, and saying so would put "no window"
    on every row at the moment the answer is least reliable.

    `titles` is any collection of the ttys the terminal apps show - collect()
    passes what survey() says of the running ones, from ps; the tab names are
    only a label now. None of them showing a window is not knowing: an app that
    runs with no window cannot tell (so iTerm2 with no pane, and Terminal.app,
    which runs on with its last window closed). `blind`: ttys an app that is
    not running keeps (iTerm2's daemon keeps its shells) - not known, whatever
    the other apps show.

    Found by a session running as `claude bg-spare` - alive, with a tty, and no
    window anywhere. Clicking it did nothing and said nothing.
    """
    if not titles:
        return None
    if not tty:
        return False
    if terms.short_tty_full(tty) in blind:
        return None
    return terms.short_tty_full(tty) in titles


def entrypoint_of(session, head, tail):
    """Who started a session: the session file says, and so does every transcript
    record - the agents feed does not. The session file wins: it is the process
    running now. Else the newest record, as in the index: a session a program
    started and you resumed at a terminal is yours now."""
    if session.get("entrypoint"):
        return str(session["entrypoint"])
    for d in reversed(as_records(head) + as_records(tail)):
        if isinstance(d.get("entrypoint"), str) and d["entrypoint"]:
            return d["entrypoint"]
    return ""


def needs_you(rows):
    """`ccwho ls --needs-you`: the rows the live list shows in NEEDS YOU and
    STUCK, by ccwho's own state (UI_GROUPS) - a program's session waits on the
    program, and work left running alone is no question to you (the owner's
    CLI revamp, 2026-10-06)."""
    groups = dict(UI_GROUPS)
    wanted = set(groups.get("NEEDS YOU", ())) | set(groups.get("STUCK", ()))
    return [r for r in rows if r.get("attention") in wanted]


def is_program(row):
    """Did a program start this row's session? Its entrypoint says so, and a
    row's state may say only "stuck"; a row that already says "program" is one."""
    return (row.get("entrypoint") in ccwho_index.AGENT_ENTRYPOINTS
            or row.get("attention") == "program")


def no_window_note(row):
    """What Enter says on a live row with no window to go to."""
    sid = row.get("sessionId", "")
    if is_program(row):
        # resuming it would be a second process in a conversation the program runs
        return f"{brief.short_id(sid)} has no window - a program runs it."
    return (f"{brief.short_id(sid)} has no window - it runs in the background."
            f"  resume it: claude --resume {sid}")


def build_row(session, head, tail, mtime, now=None, orphan_count=0, work=0, tty="",
              tab_title="", reviewed=None, windowed=None, dead_loops=(), terminal=""):
    """One display row from one session's data. Pure: no disk, no subprocess.

    collect() used to inline this, which hid the wiring - notably which clock
    `since` reads from - behind functions that only run against a live machine.
    """
    first = extract_topic(head, strict=True)["first"] or extract_topic(head)["first"]
    last = (extract_topic(tail, strict=True)["last"]
            or extract_topic(head, strict=True)["last"] or first
            or extract_topic(tail)["last"])
    status = session.get("status", "?")
    ask = extract_ask(tail)
    # A question it asked you outranks whatever the feed says, in both
    # directions: the transcript gains the question the moment it is asked, and
    # gains your answer the moment you give it.
    if is_asking_you(tail):
        attention = "blocked"
    elif status == "waiting":
        attention = waiting_kind(tail)
        if attention == "ready":  # not a state of its own
            # not a state of its own: decided like any other non-busy session
            attention = "asks" if ask else ("stuck" if dead_loops else
                                            "running" if work else "stopped")
    elif status != "busy" and ask:
        attention = "asks"
    elif status == "busy" and turn_ended(tail):
        # The turn is over and a background task keeps the feed on `busy`. The
        # task will wake the session, so an ended turn alone is not news - only
        # a question is. Found on a wait loop that could never end, hiding a
        # question in BUSY for 26 hours. A loop that cannot end will never wake
        # it: then it is stuck, not running.
        attention = "asks" if ask else ("stuck" if dead_loops else "running")
    elif status == "busy":
        attention = "busy"
    else:
        # Not busy and not asking: either it stopped, or it is waiting on work -
        # work that can never end is stuck, whatever the feed says between turns
        attention = "stuck" if dead_loops else ("running" if work else "stopped")
    ts = last_turn_ts(tail)
    # Anything that finished a turn you have not looked at since is worth
    # reviewing - that is most of what a session ever asks of you. Looking at it
    # drops it out; the session doing more work brings it back, because its last
    # turn then lands after the one you dismissed.
    if attention == "stopped" and ts and (reviewed or {}).get(
            session.get("sessionId", "")) != ts:
        attention = "review"
    # A program started it, so the program answers it: an unanswered tool call is
    # the program's to answer, and no window is how it was born. Its own state,
    # so nothing that counts what needs you can count it.
    # A loop that can never end is the exception: the program waits on it too.
    ep = entrypoint_of(session, head, tail)
    if ep in ccwho_index.AGENT_ENTRYPOINTS and attention != "stuck":
        attention = "program"
    # The recap comes off the windows this function was already given: the list's
    # second line is "what is this about", and the harness already answered it.
    recs = as_records(head) + as_records(tail)
    r = brief.recap(recs)
    has_window = windowed
    row = {
        "windowed": has_window,
        "kind": session.get("kind", ""),
        "entrypoint": ep,
        "project": project_of(session.get("cwd", "")),
        "status": status,
        "attention": attention,
        "waitingFor": session.get("waitingFor", ""),
        "name": session.get("name", "?"),
        "title": extract_title(tail) or extract_title(head),
        "doing": extract_doing(tail),
        "ask": ask,
        "since": since(ts if ts is not None else mtime, now=now),
        "ts": ts,
        "topic": last,
        "first": first,
        "age": age(session.get("startedAt"), now=None if now is None else int(now * 1000)),
        "orphans": orphan_count,
        "dead_loops": list(dead_loops),
        "work": work,
        "tty": tty,
        "tab_title": tab_title,
        # the app whose tab shows it (terms.App.key), "" when no app ccwho knows does
        "terminal": terminal,
        "parked": [],           # collect fills it: terminals that parked this job
        "recap": r["text"],
        "recap_ts": r["ts"],
        "recap_age": brief.age_between(r["ts"], now_iso(now)) if r["ts"] else "",
        "turns_since_recap": brief.turns_since(recs, r["ts"]),
        "pid": session.get("pid"),
        "sessionId": session.get("sessionId", ""),
        "cwd": session.get("cwd", ""),
        # set only for a session found in another config dir: attach and resume
        # have to run there, or claude answers that it has no such session
        "configDir": session.get("configDir", ""),
        # the session's work processes and their ports; collect() fills them in
        "procs": 0,
        "ports": [],
    }
    # the text comes from transcripts, the agents feed and tab titles, and goes to
    # a terminal and to agents: nothing in it may start an escape sequence
    row = {k: procs.printable(v) if isinstance(v, str) else v for k, v in row.items()}
    # and a one-line field has one line: a newline in a title printed a line of
    # its own in `ccwho ps` - a fake pid for an agent to kill
    for k in _ONE_LINE:
        if isinstance(row.get(k), str):
            row[k] = " ".join(row[k].split())
    return row


# every row field but the prose ones (recap, topic, first, ask)
_ONE_LINE = ("kind", "entrypoint", "project", "status", "attention", "waitingFor", "name", "title",
             "doing", "since", "age", "tty", "tab_title", "recap_age", "sessionId", "cwd",
             "configDir")


def project_dirs(roots=None):
    """Every directory a transcript can appear in, across every config root."""
    out = []
    for root in (roots or ccwho_index.config_roots()):
        base = os.path.join(root, "projects")
        try:
            out += [os.path.join(base, name) for name in os.listdir(base)]
        except OSError:
            continue
    return out


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def watch_digest(session_ids, cache=None, roots=None, env=None):
    """What the files say, in one comparable value. Starts no program at all.

    This is what lets the list notice a session needing you within a second.
    Measured on sixteen live sessions: 0.3ms, against 0.23s of CPU to ask claude
    for the session list (it starts a Node process) and 0.57s for a full scan.

    A session that needs you has just written to its transcript - the question
    IS the write. A session that has only just started has no id we know yet,
    but its new file moves the mtime of the directory holding it.

    A session ENDING is not visible here, and does not need to be: nothing that
    has stopped is waiting for you. The scheduled scan picks that up.

    Codex too: an open thread's ask is a write to its rollout, and a new thread
    is a new lock file - its ask showed up to 20 s late, at the next full scan
    (2026-10-01). The rollouts are those of the last scan's Codex rows
    (codex_threads keeps the list): a subagent's thread is held and never a
    row, and its writes would start scans that change nothing. A stat each:
    no glob, nothing read.
    """
    parts = []
    for sid in session_ids:
        path = transcript_path_cached(sid, cache)
        parts.append((sid, _mtime(path) if path else 0))
    for directory in project_dirs(roots or all_roots(cache)):
        parts.append((directory, _mtime(directory)))
    home = codex_home(env)
    locks = os.path.realpath(os.path.join(home, "thread-writer-locks"))
    parts.append((locks, _mtime(locks)))
    watched = (cache or {}).get(f"_codex_watch:{home}")
    for path in watched if isinstance(watched, list) else ():
        if isinstance(path, str):
            parts.append((path, _mtime(path)))
    return tuple(sorted(parts))


def transcript_path_cached(session_id, cache=None):
    """transcript_path globs across every config root - 9.5ms for sixteen
    sessions, which is most of a check that is otherwise free. A transcript does
    not move once it exists, so the answer is worth keeping."""
    if cache is None:
        return transcript_path(session_id)
    paths = cache.setdefault("_paths", {})
    if session_id not in paths:
        paths[session_id] = transcript_path(session_id, roots=all_roots(cache))
    return paths[session_id]


# What you have already looked at, and up to when. Keyed by session, valued by
# the turn you dismissed - so the session doing more work brings it back all by
# itself, with no transition to track.
REVIEWED_CAP = 500          # sessions end; this file must not grow for ever


def reviewed_path():
    return os.path.join(os.path.expanduser("~"), ".ccwho", "reviewed.json")


def load_reviewed(path=None):
    """Never raises: an unreadable file means you have reviewed nothing, which
    shows you more than you wanted rather than less."""
    try:
        with open(path or reviewed_path()) as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def mark_reviewed(session_id, ts, path=None):
    """Remember that you have seen this session up to this turn."""
    path = path or reviewed_path()
    seen = load_reviewed(path)
    seen.pop(session_id, None)          # re-inserted last: newest at the end
    seen[session_id] = ts
    if len(seen) > REVIEWED_CAP:
        for gone in list(seen)[:len(seen) - REVIEWED_CAP]:
            seen.pop(gone)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(seen, fh)
        os.replace(tmp, path)
    except OSError:
        pass                            # a dismissal is not worth an exception
    return seen


def collect(cache=None, status=None, session_apps=True, samples=STACK_PER_SCAN):
    """`status` is a caller-owned dict, same idiom as `cache`. It carries out the
    one fact a caller cannot recover from the rows: whether the session source
    could be reached at all. An empty row list means nothing without it.
    `session_apps` False: no app asked for tab names only for its sessions
    (Terminal.app, D7) is asked at all - the autosave job's scan. `samples`:
    how many stack samples this scan may take (find_dead_readers) - 0 for a
    scan whose rows show no stuck work."""
    raw = agents_json()
    parsed = parse_sessions(raw) if raw is not None else None
    source_ok = parsed is not None and read_is_complete(raw)
    if status is not None:
        # reachable AND fully readable. Rows we understood are still rendered; a
        # picture with a hole in it is what must not reach a caller that launches.
        status["source_ok"] = source_ok
    table = ps_table()
    try:
        file_rows, bad = live_file_sessions(table)
    except Exception:           # a second source: its failure never blanks the list
        file_rows, bad = [], None
    sessions = procs.merge_sessions(parsed or [], file_rows)
    if status is not None:
        status["session_files_bad"] = bad
    # a one-shot caller has no cache of its own: give the scan one, or isolated
    # sessions' transcripts are never found
    if cache is None:
        cache = {}
    # what an earlier ccwho process proved about a stuck reader: ccwho ls starts
    # with an empty cache every time, and a sample takes about 1.5 s
    if "_fd0" not in cache:
        load_verdicts(cache)
    cache["_sample_budget"] = samples       # this scan's, every session's together
    extra = sorted({r["configDir"] for r in file_rows if r.get("configDir")})
    if extra != cache.get("_extra_roots", []):
        cache["_extra_roots"] = extra
        cache.pop("_paths", None)           # a miss may now be a hit
    ps_out = ps_snapshot()
    procs_out = tty_snapshot()
    ttys = parse_tty_map(procs_out)
    seen = terms.survey(ps_out, procs_out)
    panes = {key: app_panes for key, (app_panes, _running) in seen.items()}
    owners = {tty: pid for app_panes in panes.values() for tty, pid in app_panes.items()}
    # what has a window comes from ps, one app at a time: an app that is not
    # running cannot tell about the ttys its daemon keeps (blind) - never "no
    # window" because another app answered for its own (review O3)
    app_of_tty = {tty: key for key, app_panes in panes.items() for tty in app_panes}
    shown, blind = set(), set()
    for app_panes, running in seen.values():
        if running:
            shown |= set(app_panes)
        else:
            blind |= set(app_panes)
    ids = [s.get("sessionId", "") for s in sessions]
    # who started what: the env mark, which survives the process being orphaned;
    # built from the ps snapshot and start times this scan already has
    starts = {pid: start for pid, (start, _cmd) in table.items()}
    comms = {pid: comm for pid, (_start, comm) in table.items()}
    ptable = {}
    for pid, ppid, cmd in procs.ps_rows(ps_out):
        if pid.isdigit():
            ptable[int(pid)] = (ppid, starts.get(int(pid), ""), cmd)
    ports = listen_ports()
    marks = proc_marks(table, cache)
    # a command line that names the session still counts when the env could not
    # be read (the old rule, kept as the fallback: eng D8) - but never a live
    # session, a helper, or a process whose env was read (it names somebody
    # else, or nobody). attribute lists them with the session, so the row's
    # count and `ccwho ps` are the same processes
    session_pids = {s.get("pid") for s in sessions}
    named = {sid: {pid for pid in pids
                   if pid not in session_pids and pid not in marks
                   and not procs.is_helper(ptable.get(pid, (0, "", ""))[2])}
             for sid, pids in _cmdline_orphans(ps_out, ids).items()}
    # a session missing from the list may be running: while the list is not
    # whole, nothing is called left behind. `bad` is None when the file scan
    # crashed
    # nor while a session file could not be read, or a claude's environment (its
    # CLAUDE_CONFIG_DIR says where its session file is)
    # and every running claude must be a listed session: one that is not may
    # be the session a "left behind" process belongs to
    running = procs.claude_pids(table)
    sessions_ok = (source_ok and bad == 0 and all(pid in marks for pid in running)
                   and set(running) <= {s.get("pid") for s in sessions})
    att = procs.attribute(ptable, marks, ports or {}, sessions,
                          sessions_known=sessions_ok, own=os.getpid(), named=named)
    reviewed = load_reviewed()
    rows = []
    parents, commands = parent_map(ps_out), command_map(ps_out)
    # the spare and the parked terminal still count above - as live claude
    # processes, and as owners of what they started - but are not rows
    parked = procs.parked_terminals(sessions)
    # not ttys[pid]: a session served by the daemon reports a background
    # process, and the window showing it belongs to an ancestor
    listed = [(s, owning_tty(s.get("pid"), parents, ttys, shown, commands=commands,
                             session_id=s.get("sessionId", "")))
              for s in procs.shown_sessions(sessions)]
    # tab names: an app that asks for them only for its sessions (Terminal.app,
    # D7) is asked while one sits in its tabs - and never when the caller
    # says not (session_apps: the autosave job's save)
    in_use = {terms.short_tty_full(tty) for _s, tty in listed if tty}
    asked = [app for app in terms.APPS
             if not app.ask_only_for_sessions or (session_apps and set(panes[app.key]) & in_use)]
    titles = terms.titles_cached(cache, procs=procs_out, owners=owners, apps=asked)
    for s, tty in listed:
        sid = s.get("sessionId", "")
        head, tail, mtime = read_windows(sid, cache=cache)
        mine = att["sessions"].get(sid, [])
        summary = procs.row_summary(mine)
        dead = (find_dead_loops(s.get("pid"), ptable, cache=cache)
                + find_dead_readers(s.get("pid"), ptable, unix=stdin_sockets, cache=cache,
                                    names=comms))
        rows.append(build_row(s, head, tail, mtime, orphan_count=summary["detached"],
                              work=work_descendants(ps_out, s.get("pid")),
                              tty=tty, tab_title=titles.get(terms.short_tty_full(tty), ""),
                              reviewed=reviewed,
                              windowed=windowed(tty, shown, blind=blind),
                              terminal=app_of_tty.get(terms.short_tty_full(tty), "") if tty else "",
                              dead_loops=dead))
        rows[-1]["procs"] = summary["procs"]
        rows[-1]["parked"] = sorted(t for t, job in parked.items() if job == sid)
        # unknown is null, not [] - a script must not read "holds nothing"
        rows[-1]["ports"] = summary["ports"] if ports is not None else None
    rows.sort(key=sort_key)
    if cache.pop("_verdicts_new", False):
        save_verdicts(cache)
    if status is not None:
        # The same value the cheap check computes, so finishing a scan never
        # leaves the check thinking the world moved. Taken BEFORE the Codex
        # read: a write after this is the next check's change, never hidden in
        # this digest (a digest after the read hid one for up to 20 s; review
        # 2026-10-02) - at worst one more scan, once, when a thread opens
        status["watch"] = watch_digest(ids, cache=cache)
        status["reviewed"] = reviewed
    if cache is not None:                       # drop windows for sessions that ended
        # Keys that are not a session id belong to something else sharing this
        # dict (the tab names). Sweeping them out every tick is how a cache ends
        # up never hitting once while looking like it works.
        for gone in set(k for k in cache if not k.startswith("_")) - set(ids):
            cache.pop(gone, None)
    # either scan missing leaves no marks or no parents: "unknown", not "nothing"
    # a port held by a process whose environment could not be read: who started
    # it is not known, and "no agent holds it" would be a guess
    # - unless attribute placed it anyway: the tree gives a tool shell's
    # `/usr/bin/nc -l` to its session, unread or not
    placed = {p["pid"] for grp in [*att["sessions"].values(), att["left_behind"],
                                   att["codex"], att["unsure"]] for p in grp}
    unread = {pid: held for pid, held in (ports or {}).items()
              if pid not in marks and pid not in placed}
    unknown_ports = sorted({port for held in unread.values() for port in held})
    # and who holds it, by name: ControlCenter holds :5000, and macOS hides
    # its environment - a person knows it at once, a script gets "unknown"
    unknown_holders = {}
    for pid, held in sorted(unread.items()):
        for port in held:
            unknown_holders.setdefault(port, []).append(
                {"pid": pid, "name": procs.program_name(ptable.get(pid, (0, "", ""))[2])})
    # any scan missing leaves no marks or no parents: "unknown", not "nothing" -
    # and so does an environment read that failed for every process
    fleet = _fleet(att, rows, ports is not None,
                   procs_ok=bool(table) and bool(ps_out) and bool(marks),
                   sessions_ok=sessions_ok)
    fleet["unknown_ports"] = unknown_ports
    fleet["unknown_holders"] = unknown_holders
    # Codex threads open now (Phase 3, D15): their own list, never session rows
    fleet["codex_threads"] = codex_fleet(read_codex_threads(cache=cache), att,
                                         ports_ok=ports is not None, reviewed=reviewed)
    return rows, fleet


def _load_beside(module):
    """Import a module's file into a NEW module object, leaving the live one alone.

    importlib.reload() executes the new source INTO the module everyone is holding.
    An edit that parses but raises half way through - a typo in a rule, a bad
    constant - leaves half the new definitions live and half the old ones, and
    catching the exception does not undo that. Building the candidate beside the
    old one means a failure changes nothing.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(module.__name__, module.__file__)
    candidate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(candidate)          # raises before anything is swapped
    return candidate


def reload_all(engine):
    """Re-read the rule modules, then the engine; return the engine to use.

    The rule modules go first, because the engine imports them and reloading
    only the engine leaves a running window on the OLD rules. A failure puts the
    working modules back and raises, so the caller can say so and carry on.
    """
    import importlib
    old = {name: sys.modules[name] for name in RELOAD_FIRST if name in sys.modules}
    try:
        for name, module in list(old.items()):
            sys.modules[name] = _load_beside(module)
        importlib.reload(engine)
    except Exception:
        sys.modules.update(old)         # put the working ones back
        raise
    return engine


# --------------------------------------------------------------------- display

# Warm (yellow, magenta, red) means it needs you; the rest are green or dim.
# STOPPED in the yellow of FINISHED was read as a question waiting.
_C = {"blocked": "\033[33;1m", "asks": "\033[35;1m", "stopped": "\033[32m",
      "review": "\033[33m", "stuck": "\033[31;1m",
      "running": "\033[2m", "ready": "\033[32m", "waiting": "\033[33;1m",
      "busy": "\033[32m", "idle": "\033[2m",
      "shell": "\033[32m", "program": "\033[2m", "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m"}


def _paint(text, key, color):
    return f"{_C.get(key, '')}{text}{_C['reset']}" if color else text


def now_iso(now=None):
    """The clock, as the transcripts spell it. One place, so tests can pass one in."""
    t = time.time() if now is None else now
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


_LABEL = {"blocked": "NEEDS YOU", "waiting": "NEEDS YOU", "asks": "ASKED YOU",
          "review": "FINISHED", "ready": "stopped", "stuck": "STUCK",
          "stopped": "STOPPED", "busy": "busy", "running": "running",
          "ready": "ready", "shell": "shell", "idle": "idle", "program": "program"}



# The fields a brief's values are, in the order brief_parts gives them.
BRIEF_FIELDS = ("id", "project", "title", "recap", "goal", "you_said", "closing",
                "tty", "pid", "name", "cwd", "session_id", "resume")


def brief_parts(b, row=None, fields=False):
    """The brief as a human reads it: what it is about first, then how it got here.

    Order is the argument. The recap answers "is this the one?", so it leads; the
    machinery that lets you ACT on the answer (the other names of this session)
    goes last, where it is available without being in the way.

    Line by line, in parts of (text, style, value): the style a _paint key or
    None, the value what a click on the part copies - whole, where the text is
    cut - or None for a label, which is not something you would paste. With
    `fields`, a fourth: which field the value is ("closing", "tty") - a screen
    that follows a value across a refresh follows its field, as the value
    changes. Only on request: the engine is reloaded into a list that has been
    open for days, and a list from before fields reads three.
    """
    row = row or {}
    att = row.get("attention", "")
    aka = b["aka"]
    out = []
    project = row.get("project", "?")
    project = "?" if project is None else project
    style = None if att else "bold"
    head = [(aka["short_id"], style, aka["short_id"] or None, "id"), ("  ", style, None),
            (project, style, project if project not in ("", "?") else None, "project")]
    if att:
        head += [("  ", None, None), (_LABEL.get(att, att), att, None)]
    out.append(head)
    if b["title"]:
        out.append([("  ", "bold", None), (b["title"], "bold", b["title"], "title")])
    if b["recap"]:
        age = b["recap_age"] or "?"
        turns = b["turns_since_recap"]
        plural = "" if turns == 1 else "s"
        stale = f"recap {age} old" + (f" · {turns} turn{plural} since" if turns else "")
        out.append([("  ", None, None), (stale, "dim", None)])
        out.append([("  ", None, None), (b["recap"], None, b["recap"], "recap")])
    elif not (b["you_said"] or b["goal"] or b["progress"] or b["closing"]):
        out.append([("  ", None, None), ("nothing typed in this session yet", "dim", None)])
    else:
        out.append([("  ", None, None), ("(no recap yet)", "dim", None)])
    if b["goal"]:
        out.append([("  ", None, None), ("opened", "dim", None), ("    ", None, None),
                    (truncate(_as_shown(plain_text(b["goal"])), 100), None, b["goal"], "goal")])
    if b["you_said"]:
        out.append([("  ", None, None), ("you said", "dim", None), ("  ", None, None),
                    (truncate(_as_shown(plain_text(b["you_said"])), 100), None, b["you_said"],
                     "you_said")])
    if b["progress"]:
        out.append([("  ", None, None), ("progress", "dim", None)])
        for step in b["progress"]:
            # shown, not offered: a step is how it got here, not something to paste
            out.append([("    · ", None, None), (truncate(_as_shown(plain_text(step)), 96), None, None)])
    if b["closing"]:
        out.append([("  ", None, None), ("it said", "dim", None)])
        out.append([("    ", None, None), (truncate(_as_shown(plain_text(b["closing"])), 96), None, b["closing"],
                                              "closing")])
    tty = aka["tty"] or row.get("tty", "")
    bits = ([[("tty ", "dim", None), (short_tty(tty), "dim", tty, "tty")]] if tty else []) + (
        [[("pid ", "dim", None), (str(aka["pid"]), "dim", str(aka["pid"]), "pid")]]
        if aka["pid"] else []) + [
        [(aka[k], "dim", aka[k], k)] for k in ("name", "cwd", "session_id") if aka[k]]
    line = [("  ", None, None)]
    for i, bit in enumerate(bits):
        line += ([(" · ", "dim", None)] if i else []) + bit
    out.append(line if bits else line + [("", "dim", None)])
    resume = f"claude --resume {aka['session_id']}"
    out.append([("  ", None, None), ("resume", "dim", None), ("    ", None, None),
                (resume, None, resume, "resume")])
    # the text without a session's own terminal codes, which the pane draws
    # (cut above LAST, after the codes and the \r went: a cut through a code left
    # "31" behind, and one through "\r\n" left a \r that blanked the line); and
    # what you paste is what the pane shows, whole - no space at either end
    def part(t, s, v=None, field=None):
        v = (_as_shown(plain_text(v)).strip() or None) if v else None
        shown = plain_text(t).replace("\r\n", "\n")
        return (shown, s, v, field if v else None) if fields else (shown, s, v)
    return [[part(*p) for p in line] for line in out]


def _as_shown(text):
    """A Windows line end is a line end, and a bare \\r starts the line over:
    only what follows the last one is what the pane shows."""
    return "\n".join(line.rsplit("\r", 1)[-1] for line in text.replace("\r\n", "\n").split("\n"))


def brief_ansi(text, style):
    """One part of brief_parts, painted, for a screen that reads ANSI."""
    return _paint(text, style, True) if style else text


def render_brief(b, row=None, color=True):
    """brief_parts as text: one paint over each run of parts in one style, so the
    codes are the ones a line painted whole always had."""
    lines = []
    for parts in brief_parts(b, row):
        runs = []
        for text, style, _ in parts:
            if runs and runs[-1][1] == style:
                runs[-1][0] += text
            else:
                runs.append([text, style])
        lines.append("".join(_paint(t, s, color) if s else t for t, s in runs))
    return "\n".join(lines)


# --------------------------------------------------------------- the ui's model
#
# The TUI is a thin shell over these. Which group a session belongs in, what its
# two lines say, what a search matches: decided here, where it is testable
# without a terminal, and hot-reloaded like everything else in this module.

# One group for everything that has done something since you last looked, and
# the urgent kind first inside it. A session that finished its turn is not a
# lesser event than one that asked a question - you want to review it either
# way - it is just less pressing.
UI_GROUPS = (("NEEDS YOU", ("blocked", "waiting", "asks", "review")),
             # its own group: a loop to kill is not a question to answer
             ("STUCK", ("stuck",)),
             ("STOPPED", ("stopped", "ready", "shell", "idle")),
             ("BUSY", ("busy", "running")),
             # started by a program, which answers it: last, and never a question
             ("PROGRAMS", ("program",)))

UI_WIDE = 140            # below this, the detail replaces the list instead of
                         # sitting beside it


def _oldest_first(ts):
    """Sort key: a known time, oldest first; no time after every known one."""
    known = isinstance(ts, (int, float)) and not isinstance(ts, bool)
    return (not known, ts if known else 0)


def _newest_first(row):
    """Sort key: when a row last moved, newest first - a Claude session's last
    entry (collect() orders them so), a Codex thread's last write (its age)."""
    ts = row.get("mtime") if row.get("kind") == "codex" else row.get("ts")
    known = isinstance(ts, (int, float)) and not isinstance(ts, bool)
    return -ts if known else 0


def ui_groups(rows):
    """Rows under the heading that says what each one needs from you.

    A heading over nothing is noise, so an empty group is left out entirely.
    """
    out = []
    for heading, states in UI_GROUPS:
        members = [r for r in rows if r.get("attention") in states]
        # certain first: a question you can see beats one we inferred from the
        # English a session happened to end with
        if heading == "NEEDS YOU":
            # then oldest first: you take the top one, so newest-first let every
            # fresh ask jump the queue and the longest wait was never answered.
            # No time known waits behind the ones that have one.
            members.sort(key=lambda r: (
                _RANK.get(r.get("attention", ""), _UNKNOWN_RANK),
                _oldest_first(r.get("ts"))))
        else:
            # then newest first, as collect() orders a session's rows - a Codex
            # row among them by its age, not after them all
            members.sort(key=lambda r: (_RANK.get(r.get("attention", ""), _UNKNOWN_RANK),
                                        _newest_first(r)))
        if members:
            # a group needs you when its marks are amber: a heading drawn like
            # NEEDS YOU over rows that do not is read, at a glance, as a question
            # waiting (STOPPED was)
            needs = any(UI_STATE_STYLE.get(s) in ("needs", "review") for s in states)
            out.append({"heading": heading, "rows": members, "needs_you": needs})
    return out


# One mark per row, and one place that says which. Painting a whole row by its
# state made three of the four states the same colour over most of the screen:
# the colour stopped meaning anything and the text got harder to read. The mark
# carries the state; the words stay plain.
UI_STATE_MARK = {"blocked": "▲", "waiting": "▲", "asks": "▲", "review": "△", "stuck": "◆",
                 "stopped": "·", "ready": "·", "shell": "·", "idle": "·",
                 "busy": "●", "running": "●", "program": "·"}
UI_STATE_STYLE = {"blocked": "needs", "waiting": "needs", "asks": "needs",
                  "review": "review", "stuck": "needs",
                  "stopped": "quiet", "ready": "quiet", "shell": "quiet",
                  "idle": "quiet", "busy": "busy", "running": "busy", "program": "quiet"}
UI_UNKNOWN_MARK = "·"

# What a part of a row IS, so the screen can decide how to draw it. The engine
# never names a colour: a terminal's palette is not its business.
UI_ROLES = ("mark", "id", "project", "name", "meta", "age", "recap", "pad", "action",
            "detail")
UI_KILL = "[kill stuck process…]"
# A click on a row goes to the session; this opens its detail instead: \ at the
# right edge of line one over / on line two, one > the height of the row. ASCII:
# a symbol of "ambiguous" width is two cells in some terminals, and pushes the
# line past the edge.
UI_DETAIL = (" \\ ", " / ")


def ui_state_style(row):
    """The one word the screen needs about this row's state."""
    return UI_STATE_STYLE.get(row.get("attention", ""), "quiet")


def dead_words(d):
    """What one thing that cannot end is waiting for, in a few words."""
    if d.get("kind") == "reader":
        # the program is a short plain word (procs.reader_name), never free text
        return f"{d.get('program')} {d.get('pid')} waits for input Claude Code never sends"
    return f"loop {d.get('pid')} waits on {', '.join(d.get('tasks') or [])}, which has ended"


def loop_kill_offered(row):
    """Is a dead loop on this row the list's to kill? Not mid-turn: the agent
    may be about to deal with it - said, not offered. A program's session is
    never known to be past its turn (a loop it cannot end makes it stuck)."""
    return bool(row.get("dead_loops")) and row.get("attention") not in ("busy", "program")


def ui_row_cells(row, width=100, tag=""):
    """The two lines a session gets, in parts, each part saying what it is.

    Line one is what the session IS: a mark for its state, then plain text.
    Line two is the recap - the harness's own summary, which is the only line
    written to answer "what was this about" - never without its age. No recap
    yet, and it says so and shows the last thing the session did instead. A row
    found under SAID shows what was said that matched instead, its age and who
    said it first: that is why the row is there.
    """
    sid = row.get("short") or brief.short_id(row.get("sessionId", ""))
    tail = f" · {row.get('since', '')}"
    if row.get("attention") == "ended":
        # no process and no window: when it ended is what there is to say
        tail = f" · ended {row.get('since') or '?'} ago"
    elif row.get("kind") == "codex":
        # a Claude row's shape (the owner's D21): its age, then where it runs -
        # its app in place of a tty, never "no window"
        tail = f" · {row.get('since') or '?'} · {_printable(row.get('where') or 'Codex')}"
    elif row.get("kind") == "background" and row.get("windowed") is False:
        # not "no window", which reads as broken: this is a session you can
        # open, with `claude attach`
        tail += " · background"
    elif is_program(row) and (row.get("windowed") is False
                                                 or not row.get("tty")):
        # a program's session never had a window: that is what it is, not a fault
        tail += " · program"
    elif row.get("windowed") is False:
        # there is nothing to go to, and a row that does not say so is a row
        # that quietly does nothing when you press Enter on it
        tail += " · no window"
    elif row.get("tty"):
        tail += f" · {short_tty(row['tty'])}"
    # what it started: dim, in the meta part, never the mark or the order - and
    # only in the room the name leaves: the name is what you are reading
    ports = [p for p in row.get("ports") or [] if isinstance(p, int)] \
        if isinstance(row.get("ports"), list) else []
    held = (" · " + " ".join(f":{p}" for p in ports[:3])
            + (f" +{len(ports) - 3}" if len(ports) > 3 else "")) if ports else (
        f" · {_plural(row['procs'], 'proc')}" if row.get("procs") else "")
    glyph = UI_STATE_MARK.get(row.get("attention", ""), UI_UNKNOWN_MARK) + " "
    # The session's name, whole wherever the line can hold it: it is the address
    # another session sends a message to, and a cut name is no address. An
    # auto-name is the project and a dash; a renamed one is not, and says its
    # project in the meta - where the title keeps room to be read, like the ports.
    printable = _printable
    handle = session_handle(row)
    project = renamed(row, handle)
    head = f"{sid}  {handle}"
    # the title, unless it only says the name again (the tab's ✳ aside): the
    # name is already on the row
    said = [t for t in (printable(row.get("tab_title")), "~" + printable(row.get("title")))
            if re.sub(r"^[\W_]+", "", t) not in ("", handle)]
    name = said[0] if said else ""
    # the account this row spends (usage, 2+ accounts only): last, and the name
    # gives way for it - never the mark, the id or the project. Its brand gives
    # way first: "· lukaso" before a title cut to nothing (the tag says the usage
    # line's "ant:lukaso", owner 2026-10-01; the line drops its brand first too)
    tags = [t for t in (tag, tag.split(":", 1)[1] if ":" in tag else "") if t]
    account = [(f" · {tags[0]}", "account")] if tags else []

    def room_for_title(tail, gap=2):
        # after the name, two spaces before a title and one before the meta,
        # which brings its own; and the detail's room, with one cell before it
        return (width - _cells(glyph) - _cells(head) - gap - _cells(tail)
                - sum(_cells(t) for t, _ in account) - _cells(UI_DETAIL[0]) - 1)
    if project and room_for_title(f" · {project}" + tail) >= 20:
        tail = f" · {project}" + tail
    if len(tags) > 1:
        full = room_for_title(tail)
        short = full + _cells(tags[0]) - _cells(tags[1])
        # only where that buys something: a title of 8+ cells - for a row that
        # has one - or the account itself (below -1, "whole or not at all"
        # drops it); never room for the ports, which give way to the account
        if full < 8 and ((name and short >= 8) or full < -1 <= short):
            account = [(f" · {tags[1]}", "account")]
    # the title is what gives way, down to nothing: the name says which session
    # this is, and a title of two letters and a … says nothing
    room = room_for_title(tail)
    shown = ((cut_codex_title(name, room) if row.get("kind") == "codex" else _cut(name, room))
             if room >= 8 else "")
    gap = 2 if shown else 1
    if room_for_title(tail, gap) < _cells(shown):
        account = []            # whole or not at all: "· 1…" is no account
    # the ports after the account, in the room it leaves - or gives up
    if _cells(shown) + _cells(held) <= room_for_title(tail, gap):
        tail += held
    # and the meta in whole pieces, the last first: "…" after a whole name
    # reads as a cut name, and "· 5h…" is no age
    while tail and room_for_title(tail, gap) < _cells(shown):
        tail = tail[:tail.rfind(" · ")] if " · " in tail else ""
    if not (shown or tail or account):
        gap = 0                 # nothing follows the name: the arrow's cell will do
    first = [(glyph, "mark"), (f"{sid}  ", "id"),
             (handle + " " * gap, "project"),
             (shown, "name"), (tail, "meta")] + account

    # The age goes FIRST. At the end of a long recap it is the first thing
    # truncation eats, and a recap whose age you cannot see reads as the current
    # state of the work - which is exactly the mistake the age exists to prevent.
    if row.get("kind") == "codex":
        # what it waits for, when it waits; else where it works. A program's
        # question is the program's to answer (D22): never a line that reads
        # as a question to you
        mark = ""
        ask = "" if row.get("attention") == "program" else row.get("ask")
        body = ("waiting? (maybe an approval)" if row.get("attention") == "waiting"
                else ask or row.get("folder") or "(no folder yet)")
    elif row.get("said"):
        # found under SAID: the message that matched is why it is there
        said = row["said"]
        who = {"you": "you", "claude": "Claude"}.get(said.get("who"), said.get("who") or "?")
        mark = f"{said.get('age') or '?'} · {who}: "
        body = said.get("text") or ""
    elif row.get("recap"):
        age = row.get("recap_age") or "?"
        turns = row.get("turns_since_recap") or 0
        mark = f"{age}" + (f", {turns} turns" if turns else "") + " · "
        body = row["recap"]
    else:
        mark = ""
        body = "(no recap yet) " + (row.get("doing") or "")
    pad = "        "
    loops = row.get("dead_loops") or []
    # mid-turn the agent may be about to deal with it: said, not offered
    act = [("  ", "pad"), (UI_KILL, "action")] if loop_kill_offered(row) else []
    if loops:
        more = f" (+{len(loops) - 1} more)" if len(loops) > 1 else ""
        words, tail = dead_words(loops[0]), f"{more} · "
        mark = words + tail
    # in screen cells, as _fit cuts the line: a CJK recap counted in characters
    # ran twice as wide, and the cut dropped the kill at the end of the line
    room = (width - _cells(pad) - sum(_cells(t) for t, _ in act)
            - _cells(UI_DETAIL[1]) - 1)
    if loops:
        # what to do about it comes first; the recap only where it can be read,
        # and the words about the loop give way - to nothing - before the kill
        # the count stays: cut, it hid a second stuck item behind the first
        mark = (_cut(words, room - _cells(tail)) + tail if more and room - _cells(tail) >= 8
                else _cut(mark, max(0, room)))
        body = body if room - _cells(mark) >= 20 else ""
    second = [(pad, "pad"), (mark, "age")] + (
        [(_cut(body, max(8, room - _cells(mark))), "recap")] if body else []) + act
    return _to_the_edge(first, UI_DETAIL[0], width), \
        _to_the_edge(second, UI_DETAIL[1], width)


def _to_the_edge(line, half, width):
    """The line, then its half of the detail arrow at the right edge, with at
    least one cell between them."""
    line = _fit(line, width - _cells(half) - 1)
    gap = width - sum(_cells(t) for t, _ in line) - _cells(half)
    return _fit(line + [(" " * gap, "pad"), (half, "detail")], width)


def _fit_tag(tag, width, brand=True):
    """A row's account tag in `width` cells: whole (with `brand`), else without
    its brand, else its name cut - the brand goes first, as on the usage line:
    two accounts cut to "ant:cli…" name no one."""
    if brand and _cells(tag) <= width:
        return tag
    name = tag.split(":", 1)[1] if ":" in tag else tag
    return name if _cells(name) <= width else _cut(name, width)


def ui_action_at(row, width, line, col, tag=""):
    """What a click at (line, col) of a row's text does on its own, or None - in
    which case the click does what a click on the row always does. `tag` as the
    row is drawn: the zones are read off the same cells the screen shows."""
    at = 0
    for text, role in ui_row_cells(row, width, tag=tag)[line] if line in (0, 1) else ():
        # a click lands in screen cells, where a CJK character takes two
        if role in ("action", "detail") and at <= col < at + _cells(text):
            return "kill" if role == "action" else "detail"
        at += _cells(text)
    return None


def _fit(cells, width):
    """Drop what does not fit, so a part is never half drawn - counted in screen
    cells, so a wide name cannot push the line past the edge."""
    out, room = [], width
    for text, role in cells:
        if room <= 0:
            break
        if _cells(text) > room:
            text = _cut(text, room)
        out.append((text, role))
        room -= _cells(text)
    return out


def ui_row_lines(row, width=100):
    """The same two lines as plain text, for anything that cannot draw styles."""
    return tuple("".join(text for text, _ in line)
                 for line in ui_row_cells(row, width=width))


_UI_SEARCHED = ("tab_title", "title", "name", "project", "recap", "doing", "ask",
                "topic", "sessionId", "tty", "where")


def ui_filter(rows, query):
    """Rows matching every word of a simple keyword search.

    Every name a session has is searchable, and so is what it is about: the point
    is that whichever one you remember is the one that works.
    """
    words = [w for w in (query or "").lower().split() if w]
    if not words:
        return list(rows)
    out = []
    for r in rows:
        # the folder as ~ reads it, a Claude row's as a Codex row's: "projects/x"
        # finds both, and the home folder's name does not find every row
        hay = " ".join([*(str(r.get(f, "")) for f in _UI_SEARCHED),
                        _tilde(str(r.get("cwd") or ""))]).lower()
        hay += " " + short_tty(r.get("tty", "")).lower() + " " + str(r.get("pid", ""))
        held = {str(p) for p in r.get("ports") or [] if isinstance(p, int)} \
            if isinstance(r.get("ports"), list) else set()
        # `3000` and `:3000` find the row holding that port
        if all(w in hay or w.lstrip(":") in held for w in words):
            out.append(r)
    return out


# The most ended sessions the list's search shows, as `ccwho ls` shows ten: each
# is a widget, and the list is built again on every key you type.
UI_ENDED_SHOWN = 10


def ended_row(entry, now_iso):
    """An index entry in the shape a row is drawn from: a session that has
    ended, what it was about (its recap, else what you said) and when. Every
    text as printable as a live row's (build_row): a transcript holds pasted
    terminal output (review-build1 F3)."""
    entry = {k: procs.printable(v) if isinstance(v, str) else v for k, v in entry.items()}
    said, last = entry.get("you_said", ""), entry.get("last_ts", "")
    return {"sessionId": entry.get("sessionId", ""), "project": entry.get("project", "?"),
            "title": entry.get("title", ""), "tab_title": "", "name": "",
            "attention": "ended", "status": "ended", "tty": "", "pid": None,
            "cwd": entry.get("cwd", ""), "topic": said, "first": entry.get("opened", ""),
            "ask": "", "doing": said, "recap": entry.get("recap", ""),
            "recap_age": brief.age_between(entry.get("recap_ts", ""), now_iso),
            "turns_since_recap": entry.get("turns_since_recap") or 0,
            "orphans": 0, "work": 0, "ts": last, "since": brief.age_between(last, now_iso),
            "age": "", "waitingFor": ""}


def ui_found_running(idx, query, live_rows):
    """The running rows the session index finds by what they were about - words
    a row does not carry, as what you said in it - as `ccwho ls` shows them: a
    session the search found under ENDED and you reopened stays in the list."""
    if not (query or "").strip() or not idx:
        return []
    found = {e.get("sessionId") for e in ccwho_index.search(idx, query)}
    return [r for r in live_rows or [] if any(sid in found for sid in answers_for(r) if sid)]


def ui_ended_groups(idx, query, live_rows, now_iso, limit=UI_ENDED_SHOWN):
    """The ENDED group the list's search shows under the running sessions: the
    ended ones the session index finds as `ccwho ls` finds them - every word, in
    a session's names (any part of its id too) and what it was about - and, as
    there, not the ones a program started. The best matches first, then the
    newest; `limit` of them, the heading saying how many there are. None while
    you do not search."""
    if not (query or "").strip() or not idx:
        return []
    live = live_ids(live_rows)
    hits = [e for e in ccwho_index.search(idx, query) if e.get("sessionId") not in live]
    if not hits:
        return []
    heading = ("ENDED" if len(hits) <= limit
               else f"ENDED  {limit} of {len(hits)} - type more to narrow")
    return [{"heading": heading, "rows": [ended_row(e, now_iso) for e in hits[:limit]],
             "needs_you": False, "ended": True, "total": len(hits)}]


def ui_said_group(idx, query, live_rows, found, now_iso, limit=UI_ENDED_SHOWN):
    """The SAID group, last: the sessions the grep of what was said found
    (`found`, ccwho_index.grep: {id: Said}) and nothing else did. A session that
    matches by a name, or by what the index says it was about, shows where that
    puts it and never here as well: a common word was said in most sessions, and
    mixed in they pushed the name matches down (review 1).

    Each row carries `said` - the message that matched, who said it and its age
    - and the closest come first: more of the words in one message, then yours,
    then as typed (Said's key), then running before ended, then the newest. The
    owner's session was 3rd of 44 by age, its row about something else
    (2026-10-07). A running row is a copy of the live row, with its state; a row
    that parked others has the best of what each of them said. An id with no
    Said shows without one. `limit` of them, the heading saying how many there
    are. None while you do not search."""
    found = found if isinstance(found, dict) else dict.fromkeys(found or ())
    if not (query or "").strip() or not found:
        return []
    live_rows = list(live_rows or [])
    # by a name on the row, or by what the index says it was about - a running
    # one the index finds is in that search, its parked terminals with it
    shown = {sid for r in ui_filter(live_rows, query) for sid in answers_for(r) if sid}
    shown |= {e.get("sessionId") for e in ccwho_index.search(idx or {}, query)}

    def best(ids):
        saids = [found[sid] for sid in ids if isinstance(found.get(sid), dict)]
        return max(saids, key=lambda s: tuple(s.get("key") or ()), default=None)

    def row_of(base, said):
        if not said:
            return base
        return dict(base, said={"text": procs.printable(str(said.get("text") or "")),
                                "who": said.get("who", ""),
                                "age": brief.age_between(said.get("ts", ""), now_iso) or "?"})

    rows = []
    for r in live_rows:
        ids = [sid for sid in answers_for(r) if sid]
        if set(ids) & set(found) and not set(ids) & shown:
            said = best(ids)
            last = max((str((idx or {}).get(sid, {}).get("last_ts", "")) for sid in ids),
                       default="") or str(r.get("ts", ""))
            rows.append((said, True, last, row_of(r, said)))
    for sid in set(found) - shown - live_ids(live_rows):
        if sid in (idx or {}):
            said = best([sid])
            rows.append((said, False, str(idx[sid].get("last_ts", "")),
                         row_of(ended_row(idx[sid], now_iso), said)))
    if not rows:
        return []
    rows.sort(key=lambda r: (tuple((r[0] or {}).get("key") or ()), r[1], r[2]), reverse=True)
    total = len(rows)
    heading = "SAID" if total <= limit else f"SAID  {limit} of {total} - type more to narrow"
    return [{"heading": heading, "rows": [r[3] for r in rows[:limit]], "needs_you": False,
             "said": True, "total": total}]


def _plural(n, word):
    return f"{n} {word}" + ("" if n == 1 else ("es" if word.endswith("s") else "s"))


def render(rows, fleet=None, color=True, width=None, links=False):
    """The one-shot table. `fleet` is collect()'s second answer: the ports agents
    hold, and what was left behind; anything else (an old caller's 0) is none."""
    fleet = fleet if isinstance(fleet, dict) else {}
    if not rows:
        return "no Claude Code sessions found\n"
    width = width or shutil.get_terminal_size((150, 24)).columns
    # the name leads: it is the one you message the session by. The column is
    # as wide as the names, to a point; a longer name is never cut (a cut name
    # is no address) - its own row gives the room from its title
    handles = {id(r): session_handle(r) for r in rows}
    w_name = min(20, max(len(h) for h in handles.values()))
    w_tty = max(4, min(7, max(len(short_tty(r.get("tty", ""))) for r in rows)))
    fixed = w_name + w_tty + 14 + 7 + 6
    w_title = max(18, min(34, (width - fixed) // 2))
    w_doing = max(18, width - fixed - w_title)

    counts = {}
    for r in rows:
        k = r.get("attention") or r["status"]
        counts[k] = counts.get(k, 0) + 1
    summary = " · ".join(f"{n} {s}" for s, n in sorted(counts.items(), key=lambda kv: _RANK.get(kv[0], 9)))

    out = [_paint(f"{len(rows)} sessions: {summary}", "bold", color)]
    # usage: what each account has left, dim like the ports line. A usage
    # problem is said on its own line and never costs the rows (eng E-D7)
    snap, tags = fleet.get("usage"), {}
    if "usage" in fleet:
        try:
            lines = ccwho_usage.usage_lines(snap, width)
            tags = {r.get("sessionId", ""): ccwho_usage.row_tag(snap, r.get("sessionId", ""))
                    for r in rows}
        except Exception:
            lines, tags = [[("usage  unknown", "dim")]], {}
        out += [ccwho_usage.to_ansi(line) if color else "".join(t for t, _ in line)
                for line in lines]
    # in screen cells, and never wider than the doing column can give: a row
    # with tags is exactly as wide as it was without them
    tag_w = min(max((_cells(t) + 2 for t in tags.values() if t), default=0), w_doing - 8)
    tag_w = tag_w if tag_w >= 3 else 0          # "  q": the smallest tag there is
    w_doing -= tag_w
    # one column, one form: every tag with its brand, or none ("lukaso" over
    # "ant:d0a0" reads as two kinds of thing)
    branded = all(_cells(t) <= tag_w - 2 for t in tags.values() if t)
    # one dim line, never an alarm: NEEDS YOU owns this screen
    if held := ports_line(fleet, width):
        out.append(_paint(held, "dim", color))
    out.append("")

    for r in rows:
        att = r.get("attention") or r["status"]
        label = _LABEL.get(att, att)
        # The name on the tab is the one you can see, so it is the one the list
        # shows. When iTerm2 could not tell us (not running, not scriptable, a
        # session with no window), fall back to Claude Code's own title and MARK
        # it: a title we could not read off a tab must not pretend it came from one.
        title = r.get("tab_title") or ("~" + (r["title"] or r["name"]))
        handle = handles[id(r)]
        if project := renamed(r, handle):
            title = f"{project} · {title}"      # a renamed session still says whose it is
        over = max(0, len(handle) - w_name)
        w_t = max(0, w_title - over)
        w_d = w_doing - max(0, over - w_title)
        doing = (r.get("ask") if att == "asks" else r["doing"]) or "-"
        if att == "running" and r.get("work"):
            doing = f"[{r['work']} bg] {doing}"
        where = short_tty(r.get("tty", "")) or "-"
        where_cell = _paint(f"{where:<{w_tty}}", "dim", color)
        where_cell = osc8(where_cell, jump_url(r), enabled=links and where != "-")
        shown_doing = truncate(doing, w_d) if w_d > 0 else ""
        line = (f"{handle:<{w_name}}  "
                f"{where_cell}  "
                f"{_paint(f'{label:<10}', att, color)}  "
                f"{(truncate(title, w_t) if w_t else ''):<{w_t}}  "
                f"{_paint(shown_doing, 'dim', color)}"
                f"{' ' * max(0, w_d - len(shown_doing))}  "
                f"{r['since']:>5}")
        if tag_w:
            tag = _fit_tag(tags.get(r.get("sessionId", ""), ""), tag_w - 2, branded)
            line += _paint("  " + tag + " " * (tag_w - 2 - _cells(tag)), "dim", color)
        if r.get("ports"):
            line += _paint("  " + " ".join(f":{p}" for p in r["ports"]), "dim", color)
        if r["orphans"]:
            line += _paint(f"  +{r['orphans']} detached", "waiting", color)
        out.append(line)
    bottom = bottom_lines(fleet)
    if bottom:
        out.append("")
    out += [_paint(line, "dim", color) for line in bottom]
    return "\n".join(out) + "\n"


# ------------------------------------------------ what agents started, and hold
#
# One set of lines for every surface: the table (`ccwho ls`), `ccwho ps`
# and the live list all say it the same way, from the same fleet.

def ports_line(fleet, width=None):
    """The one dim line under the header: the ports agents hold, and whose. ""
    when none - and "unknown" is said, once, rather than shown as none."""
    fleet = fleet if isinstance(fleet, dict) else {}
    if fleet and not fleet.get("ports_ok", True):
        return truncate("ports unknown - lsof could not be asked", width or 99)
    # processes unknown: what is held may be only part of it, and the line says so
    lead = "processes unknown · " if fleet and not fleet.get("procs_ok", True) else ""
    held = [a for a in fleet.get("agent_ports") or [] if isinstance(a, dict)]
    if not held:
        return lead.rstrip(" ·") if width is None or len(lead) - 3 <= width else ""
    # an open Codex thread's port is its row's, by its folder, as a session's is
    # by its project - and says Codex: `ccwho ls` lists no Codex row, and a bare
    # folder reads as a Claude session's there. "codex" alone: a thread not open
    folders = {r["sessionId"]: f"{r['project']} (codex)" for r in codex_rows(fleet)}
    owner = {p.get("pid"): folders[p["session"]] for p in fleet.get("codex") or []
             if isinstance(p, dict) and isinstance(p.get("session"), str)
             and p["session"] in folders}
    named = (lambda a: (isinstance(a.get("pid"), int) and owner.get(a["pid"]))
             or a.get("who", "?"))
    parts = [f":{a.get('port', '?')} {_one_line(named(a))}" for a in held]
    for n in range(min(len(parts), 6), 0, -1):
        more = f" · +{len(parts) - n}" if len(parts) > n else ""
        line = lead + "agents hold " + " · ".join(parts[:n]) + more
        # in cells, as the screen crops: a wide folder name counted in
        # characters overran the line and lost its end - "(codex)" first
        if width is None or _cells(line) <= width:
            return line
    for line in (lead + f"agents hold {_plural(len(parts), 'port')}", lead.rstrip(" ·")):
        if line and len(line) <= width:
            return line
    # nothing fits that keeps "unknown": say nothing rather than a whole count
    return "" if lead else truncate(f"agents hold {_plural(len(parts), 'port')}", width)


def _one_line(text):
    """Text from a title or a tab, fit for one line: no escape codes, no breaks."""
    return " ".join(procs.printable(str(text)).split())


def bottom_lines(fleet, hint="ccwho ps", width=None):
    """One collapsed line each for what was left behind and for Codex, naming
    where to see them. None when there is nothing."""
    fleet = fleet if isinstance(fleet, dict) else {}
    left, codex = fleet.get("left_behind") or [], fleet.get("codex") or []
    # an open thread's processes are on its row (D16): this line is the rest -
    # the Codex processes whose thread is not open, state unknown (D17)
    # by the thread its mark names - its helpers too: they are that open thread's
    open_ = {t.get("thread") for t in fleet.get("codex_threads") or [] if isinstance(t, dict)}
    codex = [p for p in codex if p.get("session") not in open_ and not p.get("helper")]
    sep = " · " if len(hint) <= 2 else " - "
    see = f"{sep}{hint} to see" if len(hint) <= 2 else f"{sep}{hint}"
    out = []
    if left:
        n_ports = sum(len(_held(p)) for p in left)
        out.append(f"left behind: {_plural(len(left), 'process')}, "
                   f"{_plural(n_ports, 'port')}{see}")
    if codex:
        out.append(f"codex: {_plural(len(codex), 'process')}{see}")
    return [truncate(line, width) for line in out] if width else out


def ps_listing(rows, fleet, show_all=False):
    """Every process an agent started, each with its `group` and `who` - what
    `ccwho ps` prints and the process screen shows. Helpers (MCP servers and the
    like) only with show_all, except left behind: litter is litter."""
    fleet = fleet if isinstance(fleet, dict) else {}
    titles = {sid: (r.get("tab_title") or r.get("title") or r.get("name") or "?")
              for r in rows or [] for sid in answers_for(r)}
    project = {sid: r.get("project", "?") for r in rows or [] for sid in answers_for(r)}
    listed = []
    for sid, mine in (fleet.get("by_session") or {}).items():
        who = _one_line(f"{project.get(sid, '?')} · {titles.get(sid, '?')}")
        listed += [dict(p, group="session", who=who) for p in mine]
    # an open Codex thread's work is its row's, as a session's is: together,
    # under the row's id and words - two unnamed threads in one folder differ
    # only by the id. Only a thread that is not open leaves its processes
    # "not known" (D17)
    codex, threads = fleet.get("codex") or [], set()
    for r in codex_rows(fleet):
        threads.add(r["sessionId"])
        who = _one_line(f"{r['short']} {r['project']} · {r['tab_title']}")
        listed += [dict(p, group="session", who=who) for p in codex
                   if p.get("session") == r["sessionId"]]
    listed += [dict(p, group="left behind", who="left behind")
               for p in fleet.get("left_behind") or []]
    listed += [dict(p, group="codex", who="codex") for p in codex
               if p.get("session") not in threads]
    # doubt about whose it is (the list is incomplete, a claude ccwho does not
    # list runs it, or it is an app): never offered as litter
    listed += [dict(p, group="unsure", who=f"not sure: {p.get('why', '?')}")
               for p in fleet.get("unsure") or []]
    if not show_all:
        listed = [p for p in listed if not p.get("helper") or p["group"] == "left behind"]
    return listed


def ps_who(p, width):
    """Whose a process is, in `width` - an open Codex thread's " (codex)" whole."""
    who = " ".join(str(p.get("who") or "").splitlines())
    return (cut_codex_title(who, width) if p.get("harness") == "codex"
            else truncate(who, width))


_PS_HEADING = {"left behind": "LEFT BEHIND - their session ended",
               "codex": "CODEX - whether its session still runs is not known",
               "unsure": "NOT SURE - it may not be litter: each line says why"}


def render_ps_screen(listing, fleet, width=100):
    """The process screen: a heading per session and per group, then one line a
    process - pid, ports, the command in its short form."""
    return "\n".join(line for line, _v, _f in ps_screen_parts(listing, fleet, width=width))


def ps_screen_parts(listing, fleet, width=100):
    """The process screen as (line, value, field), each line made with its part
    in one loop - a pid can never land on another line. A process line is its
    pid (field "proc" - `x` there kills it; not "pid", which in the brief is
    the session's own claude), the left-behind heading is field "clean" (`x`
    there cleans it all), any other line is no part."""
    fleet = fleet if isinstance(fleet, dict) else {}
    if fleet.get("collected") is False:
        return [("processes unknown - not collected yet", None, None)]
    if not fleet.get("procs_ok", True):
        return [("processes unknown - " + fleet.get("why", "ps or the environment read failed"),
                 None, None)]
    if not listing:
        return [("no processes started by agents", None, None)]
    known = fleet.get("ports_ok", True)
    one = (lambda text: truncate(" ".join(str(text).splitlines()), width))
    out, heading = [], None
    for p in listing:
        head = p["who"] if p["group"] == "session" else _PS_HEADING[p["group"]]
        if head != heading:
            if out:
                out.append(("", None, None))
            out.append((one(head), "ccwho clean", "clean") if p["group"] == "left behind"
                       else (ps_who(p, width), None, None) if p["group"] == "session"
                       else (one(head), None, None))
            heading = head
        ports = (" ".join(f":{n}" for n in _held(p)) or "-") if known else "?"
        pid = p.get("pid")
        line = f"  {'?' if pid is None else pid!s:<7} {ports:<13} {p.get('command', '')}"
        if p["group"] == "unsure":
            line += f"  ({p.get('why', '?')})"
        out.append((one(line), str(pid), "proc") if isinstance(pid, int) else (one(line), None, None))
    return out


def agent_id(env):
    """The session an agent runs ccwho from - a Claude Code or Codex id in its
    environment - or None: a person."""
    mark = procs.mark_of({k: (env or {}).get(k) for k in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")})
    return mark[1] if mark else None


def kill_prepare(mode, target, env=None, build=None):
    """What a kill would take, read now: {"mode", "target", "mine", "plan",
    "refused", "why"}. A pid target gets its start from this read. The machine
    not read whole, or an error, plans nothing ("why"; an error by its type
    only: its text may hold a command line)."""
    build = build or (lambda mine=None, status=None: build_world(mine, status=status))
    mine = agent_id(env if env is not None else os.environ)
    out = {"mode": mode, "target": mine if mode == "mine" else target, "mine": mine,
           "plan": {"kill": [], "spare": []}, "refused": False, "why": None}
    try:
        status = {}
        world = build(mine=mine, status=status)
        if status.get("trouble"):
            out["why"] = f"{status['trouble']} - nothing killed"
            return out
        if mode == "pid":
            row = world["table"].get(target)
            out["target"] = {"pid": target, "start": row[1] if row else ""}
        out["plan"] = procs.kill_plan(mode, out["target"], world)
    except Exception as err:
        out["why"] = f"failed ({type(err).__name__}) - nothing killed"
        return out
    # a tree refused (not clean's "outside" spares): the kill could not take it all
    out["refused"] = any(not e.get("outside") for e in out["plan"]["spare"])
    return out


def _kill_line(e):
    """One process of a kill list: pid, ports, command, who started it."""
    ports = " ".join(f":{p}" for p in e.get("ports") or []) or "-"
    sid = e.get("session")
    # the mark is text from another process's environment: shown only as a
    # session id - a Codex thread's as its row's: a UUIDv7 starts with its time
    who = ("no agent started it" if not sid
           else "a session ccwho does not know" if not procs._UUID.match(sid)
           else f"Codex thread {sid[-4:]}" if e.get("harness") == "codex"
           else f"session {sid[:8]}")
    return f"  {e['pid']:<7} {ports:<12} {truncate(e.get('command') or '', 50):<50}  {who}"


def kill_list_lines(plan, lead="ccwho"):
    """What a kill would take and what it spares, as `ccwho kill` prints it.
    `lead` starts a line of why: a command names itself (`ccwho kill`)."""
    kill, out = plan.get("kill") or [], []
    if kill:
        out.append(f"{len(kill)} process{'es' if len(kill) != 1 else ''} to kill:")
        for e in kill:
            out.append(_kill_line(e))
            if e.get("note"):
                out.append(f"          ! {e['note']}")
    out += [f"not killed: {e['why']}" for e in plan.get("spare") or []]
    if plan.get("why"):
        out.append(f"{lead}: {plan['why']}")
    return out


def kill_report_lines(r, refused, lead="ccwho"):
    """What carry_out did, one line each, and the exit: 0 when all of the target
    is gone (and the plan refused no part of it), 130 when an interrupt stopped
    it before any signal, else 1."""
    out = []
    if r["killed"]:
        out.append(f"killed {len(r['killed'])}: " + " ".join(str(e["pid"]) for e in r["killed"]))
    for e in r["survivors"]:
        why = f" ({e['why']})" if e.get("why") else ""
        out.append(f"still running: {e['pid']} {e.get('command') or ''}{why}"
                   f" - ccwho kill {e['pid']} --force")
    out += [f"not killed: {e['pid']} {e.get('command') or ''} - {e['why']}" for e in r["spare"]]
    out += [f"not killed, new since the list: {e['pid']} {e.get('command') or ''}"
            f" - ccwho kill {e['pid']}" for e in r["new"]]
    out += list(r["ports"])
    if r.get("why"):
        out.append(f"{lead}: {r['why']}")
    if r.get("why", "").startswith("interrupted") and not (r["killed"] or r["survivors"]):
        return out, 130                     # nothing got a signal
    if r.get("unread") and not (r["killed"] or r["survivors"]):
        return out, 4                       # could not tell: nothing got a signal
    # all of the target is gone: nothing left, nothing new, no port not known free,
    # no tree refused
    whole = r["killed"] and not (r["survivors"] or r["spare"] or r["new"] or r.get("why")
                                 or r["held"] or refused)
    return out, 0 if whole else 1


def _held(p):
    """A process's ports, whatever shape they came in: only numbers count."""
    ports = p.get("ports") if isinstance(p, dict) else None
    return [n for n in ports if isinstance(n, int)] if isinstance(ports, list) else []


def session_procs_lines(fleet, sid):
    """A session's own work, for its brief: helpers left out."""
    fleet = fleet if isinstance(fleet, dict) else {}
    known = fleet.get("ports_ok", True)
    # unknown is said, or an empty list reads as "it started nothing"
    out = [] if fleet.get("procs_ok", True) else [
        "processes unknown - this list may be incomplete"]
    for p in (fleet.get("by_session") or {}).get(sid) or []:
        if p.get("helper"):
            continue
        ports = (" ".join(f":{n}" for n in _held(p)) or "-") if known else "?"
        pid = p.get("pid")
        out.append(f"{'?' if pid is None else pid!s:<7} {ports:<13} {p.get('command', '')}")
    return out
