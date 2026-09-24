#!/usr/bin/env python3
"""What the machine says about sessions and their processes, decided without
touching it.

Pure, like ccwho_brief: no subprocess, no clock, no file reads. The engine
gathers the facts (sysctl, ps, the session files) and hands them here, so the
rules that decide which sessions are live, and which environment values may
leave a read, are tested against exact inputs rather than against whatever this
machine runs today.
"""
from __future__ import annotations

import re
import struct

# The only environment variables ccwho ever keeps. Two ids (which session started
# a process) and two folder paths (where that session's files live). An
# environment also holds tokens and keys, and ccwho's output is read by LLMs:
# anything not named here is dropped inside parse_procargs and goes nowhere.
ENV_KEYS = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
            "CLAUDE_CONFIG_DIR", "CODEX_HOME")


def parse_procargs(buf):
    """The allowlisted variables in a KERN_PROCARGS2 buffer; None if unreadable.

    Layout: argc (int), the exec path, NUL padding, argv, then the environment.
    Keys are taken from every string after the exec path, not only after argc
    arguments: a node process that sets process.title overwrites argv in place
    while the kernel keeps the old argc, so counting arguments lands inside the
    environment. A string counts only if it STARTS with `KEY=` - one that merely
    mentions a key is an argument. Never raises, and never puts buffer contents
    into anything it returns: the rest of the buffer may be secrets.
    """
    if not isinstance(buf, (bytes, bytearray)) or len(buf) < 4:
        return None
    (argc,) = struct.unpack_from("i", buf, 0)
    end = buf.find(b"\0", 4)
    if argc < 0 or end < 0:
        return None
    wanted = tuple(k.encode() + b"=" for k in ENV_KEYS)
    out = {}
    for chunk in buf[end:].split(b"\0"):
        for prefix in wanted:
            if chunk.startswith(prefix):
                out[prefix[:-1].decode()] = chunk[len(prefix):].decode("utf-8", "replace")
    return out


# Control characters (C0, DEL, C1): escape sequences that retitle a window or
# repaint a list. Nothing ccwho prints or saves from these sources may carry one.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def config_dirs(process_dirs, roots):
    """Every Claude Code config dir to look in: the known roots first, then any a
    running session names for itself. `claude agents --json` sees only ONE of
    them, so a session in an isolated dir is otherwise never listed at all.

    A dir a process names is taken only when absolute: `work` or `~/x` would be
    resolved against ccwho's own directory, not the session's.
    """
    seen, out = set(), []
    # it is printed (rows, attach and resume lines) and saved (manifests): an
    # absolute path with no control characters, or nothing
    named = [d for d in (process_dirs or []) if isinstance(d, str)
             and d.startswith("/") and not _CONTROL.search(d)]
    for d in list(roots or []) + named:
        if not d:
            continue
        d = d.rstrip("/") or "/"
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _norm(start):
    return " ".join((start or "").split())


_KIND = {"bg": "background"}

# What a session id looks like. It goes on to be a glob pattern (the transcript
# lookup) and an argument in a printed command, and these files come from dirs
# that other processes name - so anything else is not a session id.
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
_TEXT_FIELDS = ("procStart", "cwd", "name", "status", "kind", "jobId", "configDir",
                "waitingFor")
# The file says `shell` while a session runs a shell command; the agents feed
# says `busy` for the same session (measured). Rows read the same either way.
_STATUS = {"shell": "busy"}


def entry_problem(e):
    """Why a session file cannot be used, or None. A file that fails this is
    counted as unreadable: it is not what Claude Code writes."""
    if not isinstance(e, dict):
        return "not an object"
    sid = e.get("sessionId")
    if not isinstance(sid, str) or not _UUID.match(sid):
        return "no session id"
    pid = e.get("pid")
    if not isinstance(pid, int) or pid <= 1:        # also rejects True/False
        return "no pid"
    for k in _TEXT_FIELDS:
        if k in e and e[k] is not None:
            if not isinstance(e[k], str):
                return f"{k} is not text"
            # name and cwd reach the terminal: no escape sequences, no control codes
            if _CONTROL.search(e[k]):
                return f"{k} has control characters"
    return None


def live_session_files(entries, starts):
    """Session files whose process is still the one that wrote them, in the shape
    `claude agents --json` uses, plus the `configDir` the file was found in.

    `starts` is pid -> start time as `ps -o lstart` prints it under LC_ALL=C TZ=UTC,
    the same text Claude Code stores as procStart. A dead pid is a session that
    ended; a live pid with another start time is a reused pid - somebody else. A
    file with no start time cannot tell those apart, so it does not count.
    """
    out, seen = [], set()
    for e in entries or []:
        if entry_problem(e):
            continue
        pid, sid = e["pid"], e["sessionId"]
        if sid in seen or pid not in (starts or {}):
            continue
        # no start time never matches a real one: such a file cannot prove it
        # is still the same process, so it does not count
        if _norm(e.get("procStart")) != _norm(starts[pid]):
            continue
        seen.add(sid)
        row = {k: e[k] for k in ("pid", "sessionId", "cwd", "startedAt", "name",
                                 "status", "waitingFor", "configDir")
               if e.get(k) is not None}
        if "status" in row:
            row["status"] = _STATUS.get(row["status"], row["status"])
        row["kind"] = _KIND.get(e.get("kind"), e.get("kind") or "interactive")
        if e.get("jobId"):
            row["id"] = e["jobId"]
        out.append(row)
    return out


def merge_sessions(agent_rows, file_rows):
    """The agents list, plus sessions only the files know about. A session in both
    keeps its agents row: that one is Claude Code's documented answer."""
    have = {r.get("sessionId") for r in agent_rows or []}
    return list(agent_rows or []) + [r for r in file_rows or []
                                     if r.get("sessionId") not in have]


def parse_ps_table(text):
    """pid -> (start, command) from `ps -axo pid=,lstart=,comm=` run under
    LC_ALL=C TZ=UTC. The start is five words ("Tue Sep 22 13:45:15 2026"),
    printed exactly as Claude Code stores a session's procStart."""
    out = {}
    for line in (text or "").splitlines():
        f = line.split()
        if len(f) < 7 or not f[0].isdigit():
            continue
        out[int(f[0])] = (" ".join(f[1:6]), " ".join(f[6:]))
    return out


def _is_claude(command):
    # `ps -o comm` is the executable's full path (spaces and all: the desktop app
    # ships Claude Code under ~/Library/Application Support), and a session that
    # retitles itself shows as `claude bg-spare` - so: last path part, first word
    base = (command.rsplit("/", 1)[-1].split() or [""])[0]
    # a daemon-run session executes the versioned binary itself (…/versions/2.1.280)
    return base == "claude" or (base.replace(".", "").isdigit() and base.count(".") == 2)


def claude_pids(table):
    """The pids running Claude Code, by binary name or versioned binary."""
    return sorted(pid for pid, (_start, cmd) in (table or {}).items() if _is_claude(cmd))


def claude_starts(table):
    """pid -> start time, for claude processes only. A pid and its start time are
    public; only a live claude process vouches for a session file."""
    return {pid: (table or {})[pid][0] for pid in claude_pids(table)}
