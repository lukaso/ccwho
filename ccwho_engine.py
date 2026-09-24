#!/usr/bin/env python3
"""ccwho engine - all the logic. HOT-RELOADED by the runner on every tick.

Keep this module free of long-lived objects: the runner calls importlib.reload()
each iteration so edits land in a running watch without restarting it. Only pure
functions and plain dicts cross the boundary; state lives in the runner and is
passed back in, the way network_check_ruby carries @options between ticks.

Reads only what Claude Code already writes to disk. No daemon, no hooks, no tmux.

  claude agents --json      -> name, cwd, status, waitingFor, pid, sessionId
  ~/.claude/projects/**/    -> <sessionId>.jsonl transcript, for the real topic
  ps                        -> processes reparented to PID 1, matched by sessionId
"""
from __future__ import annotations

import glob
import json
import os
import datetime
import re
import shlex
import shutil
import subprocess
import sys
import time

import ccwho_brief as brief
import ccwho_index

# Status order: what needs you first, what is working next, what is parked last.
# stopped outranks busy: a stopped session will not progress without you, while a
# busy one is fine. `running` is not busy but has background work in flight - it is
# waiting on a machine, not on you, so it sorts last.
_RANK = {"blocked": 0, "waiting": 0, "asks": 1, "review": 2, "stopped": 3,
         "busy": 4, "ready": 5, "shell": 6, "idle": 7, "running": 8}

# Descendants that are session infrastructure rather than work. An idle session
# keeps its MCP servers alive; counting them would make every session look busy.
_INFRA = re.compile(r"(npm exec\s+\S*mcp|\S*-mcp\b|mcp-server|mcp_server)|_npx/", re.I)

# Closing-line requests that hand the decision back without a question mark.
# Deliberately short: every entry was validated against a live fleet, and a false
# positive costs a glance while a false negative loses the session entirely.
_ASK_PHRASES = ("tell me", "let me know", "your call", "say the word",
                "which do you want", "shall i", "want me to", "should i",
                "do you want", "confirm whether")
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
_FILE_TOOLS = ("Edit", "Read", "Write", "NotebookEdit")


def _tool_hint(name, inp):
    if name == "Bash":
        return inp.get("description") or inp.get("command") or ""
    if name in _FILE_TOOLS:
        return os.path.basename(inp.get("file_path") or "")
    return inp.get("pattern") or inp.get("description") or inp.get("query") or ""


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
                hint = " ".join(str(_tool_hint(name, b.get("input", {}) or {})).split())
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


def attribute_orphans(ps_output, session_ids):
    """Map sessionId -> count of processes reparented to PID 1 whose cmdline names it.

    This is the detached-work blind spot: work a session launched with `&` or nohup
    outlives it, is adopted by PID 1, and the session then reports idle.
    """
    counts = {sid: 0 for sid in session_ids}
    for pid, ppid, cmd in _ps_rows(ps_output):
        if ppid != 1:
            continue
        for sid in session_ids:
            if sid and sid in cmd:
                counts[sid] += 1
    return counts


def count_orphans(ps_output, home=None):
    """Orphans reparented to PID 1. With `home`, count only work under it -
    macOS has hundreds of system daemons on PID 1 and none of them are yours."""
    total = 0
    for _pid, ppid, cmd in _ps_rows(ps_output):
        if ppid != 1:
            continue
        if home and home not in cmd:
            continue
        total += 1
    return total


def _ps_rows(ps_output):
    for line in (ps_output or "").splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 3 or not fields[1].isdigit():
            continue
        yield fields[0], int(fields[1]), fields[2]


def _closing_line(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    return re.sub(r"[*_`#>]", "", lines[-1]).strip()


def asks_user(text):
    """Does this message hand the decision back to the human?

    The harness reports `idle` for a session that ended its turn with a question,
    so status alone loses them. Only the closing line is considered: a question
    earlier in a long report is usually one the message goes on to answer.
    """
    closing = _closing_line(text).lower()
    if not closing:
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


def work_descendants(ps_output, pid):
    """How many non-infrastructure processes this session has running under it.

    Zero means the session has stopped: nothing is in flight, so it will not move
    again without you. Non-zero means it is waiting on a machine, not on a human.
    """
    if not pid:
        return 0
    kids = {}
    for cpid, ppid, cmd in _ps_rows(ps_output):
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


# ------------------------------------------------------------------ disk reads

def transcript_path(session_id):
    """Where this session's transcript lives, in ANY config root.

    Not every session runs under the login in ~/.claude: some use a
    CLAUDE_CODE_OAUTH_TOKEN, and CLAUDE_CONFIG_DIR moves a session's whole
    directory. ccwho never logs in - it reads files - so a hard-coded path was
    the one thing that could tie it to a single login. (index.config_roots lists
    them: $CLAUDE_CONFIG_DIR, ~/.claude, and anything in ~/.ccwho/roots.)
    """
    if not session_id:
        return None
    for root in ccwho_index.config_roots():
        hits = glob.glob(os.path.join(root, "projects", "*", f"{session_id}.jsonl"))
        if hits:
            return hits[0]
    return None


def read_windows(session_id, tail_bytes=1024 * 1024, head_lines=400, cache=None):
    """Head and tail of a transcript as PARSED records, plus its mtime.

    `cache` is owned by the runner (it survives hot reload) and keyed on
    sessionId, holding (mtime, size). An unchanged transcript is not re-read or
    re-parsed - which is most of them, most ticks.
    """
    path = transcript_path(session_id)
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


_ETIME = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$|^(\d+)$")


def etime_seconds(etime):
    """ps ELAPSED -> seconds. Shapes: SS, MM:SS, HH:MM:SS, DD-HH:MM:SS.

    Anything unparseable is 0, so a filter of "older than N" spares it rather
    than reaping it. Wrong here means killing live work.
    """
    m = _ETIME.match((etime or "").strip())
    if not m:
        return 0
    if m.group(5):
        return int(m.group(5))
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    return ((days * 24 + hours) * 60 + int(m.group(3))) * 60 + int(m.group(4))


def reap_candidates(ps_output, pattern, min_age=0):
    """Processes whose command contains `pattern` and that are at least min_age old.

    An empty pattern matches nothing - a reaper that defaults to everything is a
    footgun, not a convenience.
    """
    if not pattern:
        return []
    out = []
    for line in (ps_output or "").splitlines():
        f = line.strip().split(None, 3)
        if len(f) < 4 or not f[0].isdigit() or not f[1].isdigit():
            continue
        pid, ppid, etime, cmd = int(f[0]), int(f[1]), f[2], f[3]
        if pattern not in cmd:
            continue
        age = etime_seconds(etime)
        if age < min_age:
            continue
        out.append({"pid": pid, "ppid": ppid, "age": age, "command": cmd})
    return out


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


_ANSI = re.compile(r"\033(?:\][^\007\033]*(?:\007|\033\\)|\[[0-9;]*[A-Za-z])")
# A jump target is a tty like s032 or a bare pid. Nothing else is accepted, so a
# crafted URL cannot smuggle anything into the handler.
_JUMP_TARGET = re.compile(r"^[A-Za-z]?[0-9]{1,8}$")


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


_SESSION_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
                         r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def open_url(entry):
    """ccwho://open/<sessionId> - "get me to this session", verb decided on click.

    Deliberately NOT ccwho://jump/: a jump names a window, and the restore list is
    read after a reboot, when no window exists. The id is validated here so a
    manifest can never talk the handler into a shell argument of its choosing.
    """
    sid = str((entry or {}).get("sessionId", "") or "")
    return "ccwho://open/" + sid if _SESSION_ID.match(sid) else ""


def parse_ccwho_url(url):
    """(verb, target) for a ccwho:// URL we understand, else ("", "").

    One dispatcher for every verb, so the registered handler never has to learn a
    new one - it forwards the whole URL and this decides. Both targets are pattern
    validated: the URL arrives from LaunchServices, which anyone can call.
    """
    if not isinstance(url, str):
        return ("", "")
    for verb, pattern in (("open", _SESSION_ID), ("jump", _JUMP_TARGET)):
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


def attach_command(session_id):
    """Put a running background session in a terminal.

    `claude attach <id>`: "Open the background session in this terminal." The
    session feed marks these `kind: background` - on a live fleet of fifteen,
    fourteen interactive and one background.
    """
    return (f"claude attach {shlex.quote(daemon_short(session_id))}"
            if session_id else "")


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
        if r.get("sessionId") == session_id:
            tty = short_tty(r.get("tty", ""))
            if tty:
                return ("jump", tty)
            # Running, with no window we can find - which is a session to OPEN,
            # not one to reopen. `claude attach` puts a running session in a
            # terminal; `claude --resume` would start a second process on a
            # live transcript.
            return ("attach", attach_command(session_id))
    for e_ in entries or []:
        if e_.get("sessionId") == session_id:
            cmd = restore_command(e_)
            if cmd:
                return ("resume", cmd)
    return ("missing", "")


def parse_jump_url(url):
    """Extract the target from ccwho://jump/<target>, or "" if it is not one."""
    if not isinstance(url, str) or not url.startswith("ccwho://jump/"):
        return ""
    target = url[len("ccwho://jump/"):].strip().rstrip("/")
    return target if _JUMP_TARGET.match(target) else ""


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
        if q == str(r.get("pid")) or q in (r.get("tty", ""), short_tty(r.get("tty", ""))):
            hits.append(r)
        elif sid and sid.startswith(q):
            hits.append(r)
        elif q in (r.get("name") or "").lower():
            hits.append(r)
        elif q in (r.get("tab_title") or "").lower():
            hits.append(r)          # the name on the tab is the one you can see
        elif q in (r.get("title") or "").lower() or q in (r.get("project") or "").lower():
            hits.append(r)
    return hits


def iterm_titles_script():
    """AppleScript for "what does each tab SAY", in one round trip.

    Bulk on purpose. Asking each session for its `tab.title` variable costs a
    round trip apiece - 1.6s for 25 sessions against 0.18s for this - and it does
    not even answer the question: measured 2026-09-21, both `tab.title` and
    `current session of tab` resolve to the WINDOW's current tab, so ten sessions
    in ten different tabs all reported one title.

    A session's own `name` is what the tab displays whenever that session is the
    tab's active pane, which is every tab that was never split. In a split tab the
    inactive pane reports its own name rather than the tab's - still the truth
    about that session, just not the string painted on the tab.
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


def parse_titles(dump):
    """tty -> the name iTerm2 shows for it. Junk lines are skipped, never fatal:
    this is another application's output, and a list must still render."""
    out = {}
    for line in (dump or "").splitlines():
        tty, sep, name = line.partition("\t")
        if not sep or not name.strip():
            continue
        out[short_tty_full(tty.strip())] = name.strip()
    return out


def short_tty_full(tty):
    """`/dev/ttys022` -> `ttys022`. The form `ps` reports, so the two maps join."""
    return (tty or "").rsplit("/", 1)[-1]


def titles_snapshot(timeout=5.0):
    """Ask iTerm2 once. Empty when it is not running, not scriptable, or slow -
    the list is still worth showing without the names on the tabs."""
    try:
        done = subprocess.run(["osascript", "-e", iterm_titles_script()],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_titles(done.stdout) if done.returncode == 0 else {}


# Tab names change when you rename a tab or a session's title updates - minutes,
# not seconds. The osascript round trip is ~0.5s of a 1.2s run, and --watch has
# burned a core before by repeating per-tick work that did not need repeating.
TITLES_TTL = 15.0


def titles_cached(cache, ttys=None, now=None):
    """The tab names, at most once per TITLES_TTL. `cache` is the runner's dict,
    the same idiom as the transcript window cache; None means always ask.

    An empty answer is never cached: iTerm2 starting up, or one slow call, would
    otherwise show "~" fallbacks for the next quarter minute.
    """
    now = time.time() if now is None else now
    if cache is None:
        return titles_snapshot()
    ts, titles, seen = cache.get("_titles", (0.0, {}, None))
    # An empty map is never a hit: iTerm2 starting up, or one slow call, would
    # otherwise show "~" fallbacks for the rest of the window.
    # A changed set of terminals is never a hit either: a tty is reused when one
    # tab closes and another opens, and the new session would wear the old
    # session's name until the window expired.
    if titles and now - ts < TITLES_TTL and (ttys is None or seen == ttys):
        return titles
    fresh = titles_snapshot()
    cache["_titles"] = (now, fresh, set(ttys) if ttys is not None else None)
    return fresh


def tty_snapshot():
    try:
        return subprocess.run(["ps", "-eo", "pid,tty"], capture_output=True,
                              text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def ps_snapshot_elapsed():
    try:
        return subprocess.run(["ps", "-eo", "pid,ppid,etime,command"],
                              capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def ps_snapshot():
    try:
        return subprocess.run(
            ["ps", "-eo", "pid,ppid,command"], capture_output=True, text=True, timeout=20
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


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
            [exe, "agents", "--json"], capture_output=True, text=True, timeout=30
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
_SESSION_ID = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{7,63}$")
_MANIFEST_NAME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{4}\.json$")

_MANIFEST_KEYS = ("sessionId", "cwd", "project", "topic", "first", "ask",
                  "attention", "status", "tty", "pid", "since")


def manifest_from_rows(rows, now=None):
    """Capture the live fleet. Pure: the caller supplies the rows and the clock.

    Order is the caller's (collect() sorts needs-you first), so the restore list
    reads in the same order as the dashboard the user was looking at.
    """
    now = int(time.time() if now is None else now)
    kept, skipped = [], 0
    for r in rows or []:
        if not r.get("sessionId") or not r.get("cwd"):
            skipped += 1          # no id to resume, or nowhere to cd: not restorable
            continue
        kept.append({k: r.get(k, "") for k in _MANIFEST_KEYS})
    return {"version": MANIFEST_VERSION, "savedAt": now,
            "count": len(kept), "skipped": skipped, "sessions": kept}


def restore_command(entry):
    """The shell line that reopens one session, or None if it cannot be built.

    The `cd` is NOT needed to FIND the session - measured 2026-08-31, `claude
    --resume <id>` resolves globally, from any directory. It is needed so the
    resumed session operates in its own project: relative paths, the repo it edits,
    the CLAUDE.md it loads. Getting that wrong is silent, not an error.
    """
    entry = entry if isinstance(entry, dict) else {}
    sid = str(entry.get("sessionId", "") or "")
    cwd = str(entry.get("cwd", "") or "")
    if not cwd or not _SESSION_ID.match(sid):
        return None
    return "cd %s && claude --resume %s" % (shlex.quote(cwd), shlex.quote(sid))


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


def check_manifest(manifest, cwd_exists, transcript_for):
    """Would this manifest actually restore? Returns (ok, [(project, reason), ...]).

    Pure: the caller injects the two disk predicates. Answers the only question
    that matters before a reboot, and answers it while you can still fix it.

    An EMPTY manifest fails. "Nothing to restore" is the exact shape of the bug
    this tool exists to prevent, so it must never read as a clean bill of health.
    """
    entries = manifest_entries(manifest)
    if not entries:
        return False, [("(manifest)", "no sessions in it - run `ccwho save` while they are open")]
    problems = []
    for e_ in entries:
        who = e_.get("project") or e_.get("sessionId", "?")[:8] or "?"
        if not restore_command(e_):
            problems.append((who, "no resume line can be built (bad session id, or no cwd)"))
            continue
        if not cwd_exists(e_.get("cwd", "")):
            problems.append((who, "cwd is gone: %s" % e_.get("cwd", "")))
            continue
        if not transcript_for(e_.get("sessionId", "")):
            problems.append((who, "transcript is gone - nothing left to resume"))
    return (not problems), problems


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
        name = s_.get("project") or "?"
        head = "%s%2d. %s%s" % (c["bold"], i, osc8(name, open_url(s_), links), c["reset"])
        was = s_.get("tty") or ""
        out.append("%s  %s%s%s\n" % (head, c["dim"], ("was " + was) if was else "", c["reset"]))
        # BOTH halves, because neither alone identifies a session. Measured on a real
        # 17-session fleet: `topic` (the latest turn) read "/compact", "go ahead" and
        # "let's fix 1-3" for 3 of them, while `first` read "restart from disk" for 1.
        opened = (s_.get("first") or "").strip()
        latest = (s_.get("topic") or "").strip()
        w = (width or 100) - 14
        if opened and latest and opened != latest:
            out.append("      %sopened:%s %s\n" % (c["dim"], c["reset"], truncate(opened, w)))
            out.append("      %slatest:%s %s\n" % (c["dim"], c["reset"], truncate(latest, w)))
        elif opened or latest:
            out.append("      %s\n" % truncate(latest or opened, (width or 100) - 6))
        ask = s_.get("ask") or ""
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
    skipped = m.get("skipped") or 0
    if skipped:
        out.append("\n%s%d live session(s) could not be captured (no id or no cwd)%s\n"
                   % (c["dim"], skipped, c["reset"]))
    return "".join(out)


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


def iterm_run_script(cmd):
    """One iTerm2 window running one command. The single-session case of the
    restore script, used when a click lands on a session that is no longer up."""
    if not cmd:
        return ""
    return ('tell application "iTerm2"\n  activate\n'
            '  create window with default profile\n'
            '  tell current session of current window\n'
            '    write text %s\n  end tell\nend tell\n' % applescript_str(cmd))


def iterm_open_script(entries):
    """AppleScript that reopens one iTerm2 window per restorable session.

    An entry whose resume line cannot be built is DROPPED rather than emitted
    broken: a window that opens onto a failed command is worse than no window.
    """
    lines = []
    for e_ in entries or []:
        cmd = restore_command(e_)
        if not cmd:
            continue
        lines.append("  create window with default profile")
        lines.append("  tell current session of current window")
        lines.append("    write text %s" % applescript_str(cmd))
        lines.append("  end tell")
    if not lines:
        return ""
    return 'tell application "iTerm2"\n  activate\n%s\nend tell\n' % "\n".join(lines)


# -------------------------------------------------------------------- assemble

def parent_map(ps_output):
    """pid -> ppid, from the ps output collect already takes."""
    out = {}
    for pid, ppid, _cmd in _ps_rows(ps_output):
        try:
            out[int(pid)] = int(ppid)
        except (TypeError, ValueError):
            continue
    return out


def command_map(ps_output):
    """pid -> command, so a window can be asked what it is showing."""
    out = {}
    for pid, _ppid, cmd in _ps_rows(ps_output):
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


def attached_tty(session_id, ttys, commands):
    """The terminal running `claude attach <id>`, if one is.

    An attach can be anywhere - it is not an ancestor of the session it shows -
    so it is looked for by name rather than walked to.
    """
    if not session_id:
        return ""
    for pid, cmd in (commands or {}).items():
        if is_attach_to(cmd, session_id):
            tty = ttys.get(pid, "")
            if tty:
                return tty
    return ""


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
    it, so walk up until a tty iTerm2 knows appears.

    Without iTerm2 to ask, nothing is known about windows and the session's own
    tty stands - never a guess dressed as an answer.
    """
    shown = attached_tty(session_id, ttys, commands)
    if shown:
        return shown
    seen, own = set(), ""
    while pid and pid not in seen and depth > 0:
        seen.add(pid)
        depth -= 1
        tty = ttys.get(pid, "")
        if tty:
            # Only a process that SHOWS the session counts. Without commands to
            # look at, the old rule stands rather than reporting no window.
            viewing = commands is None or is_viewer(commands.get(pid, ""), session_id)
            if viewing:
                own = own or tty
                if not titles or short_tty_full(tty) in titles:
                    return tty
        pid = parents.get(pid)
    return own


def windowed(tty, titles):
    """Is there a terminal window to go to?

    True, False, or None for "we could not ask". The third matters: iTerm2 shut
    or unscriptable is not every session losing its window, and saying so would
    put "no window" on every row at the moment the answer is least reliable.

    Found by a session running as `claude bg-spare` - alive, with a tty, and no
    window anywhere. Clicking it did nothing and said nothing.
    """
    if not titles:
        return None
    if not tty:
        return False
    return short_tty_full(tty) in titles


def build_row(session, head, tail, mtime, now=None, orphan_count=0, work=0, tty="",
              tab_title="", reviewed=None, windowed=None):
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
            attention = "asks" if ask else ("running" if work else "stopped")
    elif status != "busy" and ask:
        attention = "asks"
    elif status == "busy":
        attention = "busy"
    else:
        # Not busy and not asking: either it stopped, or it is waiting on work.
        attention = "running" if work else "stopped"
    ts = last_turn_ts(tail)
    # Anything that finished a turn you have not looked at since is worth
    # reviewing - that is most of what a session ever asks of you. Looking at it
    # drops it out; the session doing more work brings it back, because its last
    # turn then lands after the one you dismissed.
    if attention == "stopped" and ts and (reviewed or {}).get(
            session.get("sessionId", "")) != ts:
        attention = "review"
    # The recap comes off the windows this function was already given: the list's
    # second line is "what is this about", and the harness already answered it.
    recs = as_records(head) + as_records(tail)
    r = brief.recap(recs)
    has_window = windowed
    return {
        "windowed": has_window,
        "kind": session.get("kind", ""),
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
        "work": work,
        "tty": tty,
        "tab_title": tab_title,
        "recap": r["text"],
        "recap_ts": r["ts"],
        "recap_age": brief.age_between(r["ts"], now_iso(now)) if r["ts"] else "",
        "turns_since_recap": brief.turns_since(recs, r["ts"]),
        "pid": session.get("pid"),
        "sessionId": session.get("sessionId", ""),
        "cwd": session.get("cwd", ""),
    }


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


def watch_digest(session_ids, cache=None, roots=None):
    """What the files say, in one comparable value. Starts no program at all.

    This is what lets the list notice a session needing you within a second.
    Measured on sixteen live sessions: 0.3ms, against 0.23s of CPU to ask claude
    for the session list (it starts a Node process) and 0.57s for a full scan.

    A session that needs you has just written to its transcript - the question
    IS the write. A session that has only just started has no id we know yet,
    but its new file moves the mtime of the directory holding it.

    A session ENDING is not visible here, and does not need to be: nothing that
    has stopped is waiting for you. The scheduled scan picks that up.
    """
    parts = []
    for sid in session_ids:
        path = transcript_path_cached(sid, cache)
        parts.append((sid, _mtime(path) if path else 0))
    for directory in project_dirs(roots):
        parts.append((directory, _mtime(directory)))
    return tuple(sorted(parts))


def transcript_path_cached(session_id, cache=None):
    """transcript_path globs across every config root - 9.5ms for sixteen
    sessions, which is most of a check that is otherwise free. A transcript does
    not move once it exists, so the answer is worth keeping."""
    if cache is None:
        return transcript_path(session_id)
    paths = cache.setdefault("_paths", {})
    if session_id not in paths:
        paths[session_id] = transcript_path(session_id)
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


def collect(cache=None, status=None):
    """`status` is a caller-owned dict, same idiom as `cache`. It carries out the
    one fact a caller cannot recover from the rows: whether the session source
    could be reached at all. An empty row list means nothing without it."""
    raw = agents_json()
    parsed = parse_sessions(raw) if raw is not None else None
    if status is not None:
        # reachable AND fully readable. Rows we understood are still rendered; a
        # picture with a hole in it is what must not reach a caller that launches.
        status["source_ok"] = parsed is not None and read_is_complete(raw)
    sessions = parsed or []
    ps_out = ps_snapshot()
    ttys = parse_tty_map(tty_snapshot())
    titles = titles_cached(cache, ttys=set(ttys.values()))
    ids = [s.get("sessionId", "") for s in sessions]
    orphans = attribute_orphans(ps_out, ids)
    reviewed = load_reviewed()
    rows = []
    parents, commands = parent_map(ps_out), command_map(ps_out)
    for s in sessions:
        sid = s.get("sessionId", "")
        head, tail, mtime = read_windows(sid, cache=cache)
        # not ttys[pid]: a session served by the daemon reports a background
        # process, and the window showing it belongs to an ancestor
        tty = owning_tty(s.get("pid"), parents, ttys, titles,
                         commands=commands, session_id=sid)
        rows.append(build_row(s, head, tail, mtime, orphan_count=orphans.get(sid, 0),
                              work=work_descendants(ps_out, s.get("pid")),
                              tty=tty, tab_title=titles.get(short_tty_full(tty), ""),
                              reviewed=reviewed,
                              windowed=windowed(tty, titles)))
    rows.sort(key=sort_key)
    if status is not None:
        # The same value the cheap check computes, so finishing a scan never
        # leaves the check thinking the world moved.
        status["watch"] = watch_digest(ids, cache=cache)
        status["reviewed"] = reviewed
    if cache is not None:                       # drop windows for sessions that ended
        # Keys that are not a session id belong to something else sharing this
        # dict (the tab names). Sweeping them out every tick is how a cache ends
        # up never hitting once while looking like it works.
        for gone in set(k for k in cache if not k.startswith("_")) - set(ids):
            cache.pop(gone, None)
    return rows, count_orphans(ps_out, home=os.path.expanduser("~"))


# --------------------------------------------------------------------- display

_C = {"blocked": "\033[33;1m", "asks": "\033[35;1m", "stopped": "\033[33m",
      "review": "\033[33m",
      "running": "\033[2m", "ready": "\033[33m", "waiting": "\033[33;1m",
      "busy": "\033[36m", "idle": "\033[2m",
      "shell": "\033[35m", "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m"}


def _paint(text, key, color):
    return f"{_C.get(key, '')}{text}{_C['reset']}" if color else text


def now_iso(now=None):
    """The clock, as the transcripts spell it. One place, so tests can pass one in."""
    t = time.time() if now is None else now
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


_LABEL = {"blocked": "NEEDS YOU", "waiting": "NEEDS YOU", "asks": "ASKED YOU",
          "review": "FINISHED", "ready": "stopped",
          "stopped": "STOPPED", "busy": "busy", "running": "running",
          "ready": "ready", "shell": "shell", "idle": "idle"}


def render_brief(b, row=None, color=True):
    """The brief as a human reads it: what it is about first, then how it got here.

    Order is the argument. The recap answers "is this the one?", so it leads; the
    machinery that lets you ACT on the answer (the other names of this session)
    goes last, where it is available without being in the way.
    """
    row = row or {}
    att = row.get("attention", "")
    out = []
    head = f"{b['aka']['short_id']}  {row.get('project', '?')}"
    if att:
        head += "  " + _paint(_LABEL.get(att, att), att, color)
    out.append(_paint(head, "bold", color) if not att else head)
    if b["title"]:
        out.append(_paint(f"  {b['title']}", "bold", color))
    if b["recap"]:
        age = b["recap_age"] or "?"
        turns = b["turns_since_recap"]
        plural = "" if turns == 1 else "s"
        stale = f"recap {age} old" + (f" · {turns} turn{plural} since" if turns else "")
        out.append(f"  {_paint(stale, 'dim', color)}")
        out.append(f"  {b['recap']}")
    elif not (b["you_said"] or b["goal"] or b["progress"] or b["closing"]):
        out.append(f"  {_paint('nothing typed in this session yet', 'dim', color)}")
    else:
        out.append(f"  {_paint('(no recap yet)', 'dim', color)}")
    if b["goal"]:
        out.append(f"  {_paint('opened', 'dim', color)}    {truncate(b['goal'], 100)}")
    if b["you_said"]:
        out.append(f"  {_paint('you said', 'dim', color)}  {truncate(b['you_said'], 100)}")
    if b["progress"]:
        out.append("  " + _paint("progress", "dim", color))
        for step in b["progress"]:
            out.append(f"    · {truncate(step, 96)}")
    if b["closing"]:
        out.append("  " + _paint("it said", "dim", color))
        out.append(f"    {truncate(b['closing'], 96)}")
    aka = b["aka"]
    bits = [f"tty {short_tty(aka['tty'] or row.get('tty', ''))}" if (aka["tty"] or row.get("tty")) else "",
            f"pid {aka['pid']}" if aka["pid"] else "",
            aka["name"], aka["cwd"], aka["session_id"]]
    out.append("  " + _paint(" · ".join(x for x in bits if x), "dim", color))
    out.append(f"  {_paint('resume', 'dim', color)}    claude --resume {aka['session_id']}")
    return "\n".join(out)


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
             ("STOPPED", ("stopped", "ready", "shell", "idle")),
             ("BUSY", ("busy", "running")))

UI_WIDE = 140            # below this, the detail replaces the list instead of
                         # sitting beside it


def ui_groups(rows):
    """Rows under the heading that says what each one needs from you.

    A heading over nothing is noise, so an empty group is left out entirely.
    """
    out = []
    for heading, states in UI_GROUPS:
        members = [r for r in rows if r.get("attention") in states]
        # certain first: a question you can see beats one we inferred from the
        # English a session happened to end with
        members.sort(key=lambda r: _RANK.get(r.get("attention", ""), _UNKNOWN_RANK))
        if members:
            out.append({"heading": heading, "rows": members})
    return out


# One mark per row, and one place that says which. Painting a whole row by its
# state made three of the four states the same colour over most of the screen:
# the colour stopped meaning anything and the text got harder to read. The mark
# carries the state; the words stay plain.
UI_STATE_MARK = {"blocked": "▲", "waiting": "▲", "asks": "▲", "review": "△",
                 "stopped": "·", "ready": "·", "shell": "·", "idle": "·",
                 "busy": "●", "running": "●"}
UI_STATE_STYLE = {"blocked": "needs", "waiting": "needs", "asks": "needs",
                  "review": "review",
                  "stopped": "quiet", "ready": "quiet", "shell": "quiet",
                  "idle": "quiet", "busy": "busy", "running": "busy"}
UI_UNKNOWN_MARK = "·"

# What a part of a row IS, so the screen can decide how to draw it. The engine
# never names a colour: a terminal's palette is not its business.
UI_ROLES = ("mark", "id", "project", "name", "meta", "age", "recap", "pad")


def ui_state_style(row):
    """The one word the screen needs about this row's state."""
    return UI_STATE_STYLE.get(row.get("attention", ""), "quiet")


def ui_row_cells(row, width=100):
    """The two lines a session gets, in parts, each part saying what it is.

    Line one is what the session IS: a mark for its state, then plain text.
    Line two is the recap - the harness's own summary, which is the only line
    written to answer "what was this about" - never without its age. No recap
    yet, and it says so and shows the last thing the session did instead.
    """
    sid = brief.short_id(row.get("sessionId", ""))
    name = row.get("tab_title") or ("~" + (row.get("title") or row.get("name") or ""))
    tail = f" · {row.get('since', '')}"
    if row.get("kind") == "background" and row.get("windowed") is False:
        # not "no window", which reads as broken: this is a session you can
        # open, with `claude attach`
        tail += " · background"
    elif row.get("windowed") is False:
        # there is nothing to go to, and a row that does not say so is a row
        # that quietly does nothing when you press Enter on it
        tail += " · no window"
    elif row.get("tty"):
        tail += f" · {short_tty(row['tty'])}"
    glyph = UI_STATE_MARK.get(row.get("attention", ""), UI_UNKNOWN_MARK) + " "
    head = f"{sid}  {row.get('project', '?')[:12]}  "
    room = max(8, width - visible_len(glyph) - visible_len(head) - visible_len(tail))
    first = [(glyph, "mark"), (f"{sid}  ", "id"),
             (f"{row.get('project', '?')[:12]}  ", "project"),
             (truncate(name, room), "name"), (tail, "meta")]

    # The age goes FIRST. At the end of a long recap it is the first thing
    # truncation eats, and a recap whose age you cannot see reads as the current
    # state of the work - which is exactly the mistake the age exists to prevent.
    if row.get("recap"):
        age = row.get("recap_age") or "?"
        turns = row.get("turns_since_recap") or 0
        mark = f"{age}" + (f", {turns} turns" if turns else "") + " · "
        body = row["recap"]
    else:
        mark = ""
        body = "(no recap yet) " + (row.get("doing") or "")
    pad = "        "
    second = [(pad, "pad"), (mark, "age"),
              (truncate(body, max(8, width - len(pad) - visible_len(mark))), "recap")]
    return (_fit(first, width), _fit(second, width))


def _fit(cells, width):
    """Drop what does not fit, so a part is never half drawn."""
    out, room = [], width
    for text, role in cells:
        if room <= 0:
            break
        if visible_len(text) > room:
            text = truncate(text, room)
        out.append((text, role))
        room -= visible_len(text)
    return out


def ui_row_lines(row, width=100):
    """The same two lines as plain text, for anything that cannot draw styles."""
    return tuple("".join(text for text, _ in line)
                 for line in ui_row_cells(row, width=width))


_UI_SEARCHED = ("tab_title", "title", "name", "project", "recap", "doing", "ask",
                "topic", "sessionId", "tty")


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
        hay = " ".join(str(r.get(f, "")) for f in _UI_SEARCHED).lower()
        hay += " " + short_tty(r.get("tty", "")).lower() + " " + str(r.get("pid", ""))
        if all(w in hay for w in words):
            out.append(r)
    return out


def render(rows, total_orphans, color=True, width=None, show_prompt=False, links=False):
    if not rows:
        return "no Claude Code sessions found\n"
    width = width or shutil.get_terminal_size((150, 24)).columns
    w_proj = min(13, max(len(r["project"]) for r in rows))
    w_tty = max(4, min(7, max(len(short_tty(r.get("tty", ""))) for r in rows)))
    fixed = w_proj + w_tty + 14 + 7 + 6
    w_title = max(18, min(34, (width - fixed) // 2))
    w_doing = max(18, width - fixed - w_title)

    counts = {}
    for r in rows:
        k = r.get("attention") or r["status"]
        counts[k] = counts.get(k, 0) + 1
    summary = " · ".join(f"{n} {s}" for s, n in sorted(counts.items(), key=lambda kv: _RANK.get(kv[0], 9)))

    out = [_paint(f"{len(rows)} sessions: {summary}", "bold", color)]
    if total_orphans:
        out.append(_paint(f"{total_orphans} detached processes under your home on PID 1", "dim", color))
    out.append("")

    for r in rows:
        att = r.get("attention") or r["status"]
        label = _LABEL.get(att, att)
        # The name on the tab is the one you can see, so it is the one the list
        # shows. When iTerm2 could not tell us (not running, not scriptable, a
        # session with no window), fall back to Claude Code's own title and MARK
        # it: a title we could not read off a tab must not pretend it came from one.
        title = r.get("tab_title") or ("~" + (r["title"] or r["name"]))
        doing = (r.get("ask") if att == "asks" else r["doing"]) or "-"
        if att == "running" and r.get("work"):
            doing = f"[{r['work']} bg] {doing}"
        where = short_tty(r.get("tty", "")) or "-"
        where_cell = _paint(f"{where:<{w_tty}}", "dim", color)
        where_cell = osc8(where_cell, jump_url(r), enabled=links and where != "-")
        shown_doing = truncate(doing, w_doing)
        line = (f"{r['project']:<{w_proj}}  "
                f"{where_cell}  "
                f"{_paint(f'{label:<10}', att, color)}  "
                f"{truncate(title, w_title):<{w_title}}  "
                f"{_paint(shown_doing, 'dim', color)}"
                f"{' ' * max(0, w_doing - len(shown_doing))}  "
                f"{r['since']:>5}")
        if r["orphans"]:
            line += _paint(f"  +{r['orphans']} detached", "waiting", color)
        out.append(line)
        if show_prompt and r["topic"]:
            out.append(_paint(f"{' ' * (w_proj + w_tty + 16)}\u21b3 {truncate(r['topic'], width - w_proj - 18)}", "dim", color))
    return "\n".join(out) + "\n"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    rows, total_orphans = collect()
    if "--json" in argv:
        print(json.dumps(rows, indent=2))
        return 0
    if "--blocked" in argv:
        rows = [r for r in rows if r["status"] == "waiting" or r["orphans"]]
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and "--no-color" not in argv
    sys.stdout.write(render(rows, total_orphans, color=color))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
