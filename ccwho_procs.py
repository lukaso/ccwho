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
import shlex
import struct

# The only environment variables ccwho ever keeps. Two ids (which session started
# a process) and two folder paths (where that session's files live). An
# environment also holds tokens and keys, and ccwho's output is read by LLMs:
# anything not named here is dropped inside parse_procargs and goes nowhere.
ENV_KEYS = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID",
            "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CLAUDE_PID")


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


# what may stay: a line break and a tab start no escape sequence, and a recap
# is paragraphs
_UNPRINTABLE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def printable(text):
    """`text` with every character that could start an escape sequence as `?`."""
    return _UNPRINTABLE.sub("?", text)


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


# ------------------------------------------------------------- what agents run

def mark_of(env):
    """(harness, session id) that started a process, from its allowlisted env -
    plus, for Claude, the pid of the claude that started it when that is a number."""
    env = env or {}
    if env.get("CLAUDE_CODE_SESSION_ID"):
        pid = env.get("CLAUDE_PID", "")
        if pid.isascii() and pid.isdigit() and len(pid) < 8:
            return ("claude", env["CLAUDE_CODE_SESSION_ID"], int(pid))
        return ("claude", env["CLAUDE_CODE_SESSION_ID"])
    if env.get("CODEX_THREAD_ID"):
        return ("codex", env["CODEX_THREAD_ID"])
    return None


def parse_lsof_listen(text):
    """pid -> sorted listening TCP ports, from `lsof -nP -iTCP -sTCP:LISTEN -Fpn`."""
    out, pid = {}, None
    for line in (text or "").splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n") and pid is not None:
            port = line.rsplit(":", 1)[-1]
            if port.isdigit():
                out.setdefault(pid, set()).add(int(port))
    return {pid: sorted(ports) for pid, ports in out.items()}


# Secrets a command line can carry. Agents read ccwho's output, and a node process
# that renames itself can spill its whole environment into its command line. Each
# rule masks a VALUE and keeps what it belonged to, so the line still reads.
_QUOTED = r"""("[^"]*"|'[^']*'|\S+)"""
_SECRET_WORD = re.compile(r"(?i)token|key|secret|pass|auth|credential|cookie|session|pat")
_FLAG = re.compile(r"(?i)(--?(?:token|api[-_]?key|apikey|password|passwd|secret|auth|"
                   r"credentials?|key|pat)s?)(=|\s+)" + _QUOTED)
_ASSIGN = re.compile(r"(?<![\w\-?&/])([A-Za-z_][\w./:-]*)=" + _QUOTED)
_REDACT = (
    # one-letter password flags, only for the tools that mean it (ls -a is not)
    (re.compile(r"(\b(?:mysql|mysqldump|mariadb)\b(?:\s+\S+)*?\s-p)(\S+)"), r"\1***"),
    (re.compile(r"(\bredis-cli\b(?:\s+\S+)*?\s-a\s+)(\S+)"), r"\1***"),
    (re.compile(r"(\bsshpass\b(?:\s+\S+)*?\s-p\s+)(\S+)"), r"\1***"),
    (re.compile(r"(\s(?:-u|--user)\s+[^\s:]+:)(\S+)"), r"\1***"),
    (re.compile(r"(?i)((?:proxy-)?authorization\s*:\s*|x-api-key\s*:\s*|api-key\s*:\s*|"
                r"cookie\s*:\s*)([^'\"]+)"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)\S+"), r"\1***"),
    (re.compile(r"(\w+://)[^/\s@]+@"), r"\1***@"),
    (re.compile(r"(?i)([?&](?:access_token|api_key|apikey|key|token|secret|password|"
                r"auth)=)[^&\s'\"]+"), r"\1***"),
    (re.compile(r"(?i)(\"(?:token|apikey|api_key|secret|password|auth|access_token)\""
                r"\s*:\s*\")[^\"]*\""), r'\1***"'),
    (re.compile(r"\b(?:sk-[\w-]{8,}|sk_(?:live|test)_\w+|rk_(?:live|test)_\w+|"
                r"gh[pousr]_\w{8,}|github_pat_\w+|glpat-[\w-]{8,}|xox[a-z]-[\w-]+|"
                r"(?:AKIA|ASIA)[0-9A-Z]{12,}|AIza[\w-]{20,}|npm_\w{20,}|hf_\w{16,}|"
                r"eyJ[\w-]+\.eyJ[\w-]+\.[\w-]+)"), "***"),
)


def _mask_assign(m):
    # an environment-shaped KEY=value: every upper-case one (a spilled
    # environment), and any whose name says secret, whatever its case
    key = m.group(1)
    if key.isupper() or _SECRET_WORD.search(key):
        return f"{key}=***"
    return m.group(0)


def redact_command(cmd):
    """A command line safe to print: secret-looking values become `***`, and
    control characters `?` - any process can set its own title, and an escape
    sequence in it would otherwise retitle the window or repaint the list."""
    # line breaks and tabs are spacing, the rest of the control codes are not
    out = _CONTROL.sub("?", re.sub(r"[\t\n\r]", " ", cmd or ""))
    out = _FLAG.sub(r"\1\2***", out)
    for pattern, repl in _REDACT:
        out = pattern.sub(repl, out)
    return _ASSIGN.sub(_mask_assign, out)


# A session's helpers rather than its work: MCP servers, and the npm proxy some
# setups put in front of every npx. Hidden on a live row (an idle session keeps
# them alive); kept in Left behind (once the session is gone they are garbage).
# For display only - which sessions count as working is the engine's _INFRA.
# the program or package name of an MCP server, or the npm proxy - not any path
# that contains "mcp" or runs from the npx cache (a dev server can do both)
_HELPER = re.compile(r"(?:^|[\s/])(?:[\w.-]*-mcp(?:-server)?|mcp-server[\w.-]*|mcp_server"
                     r"[\w.-]*|safe-chain)(?:@\S*)?(?=\s|\Z)"
                     r"|@modelcontextprotocol/server-[\w.-]+"
                     # an installed MCP package, whatever file of it runs: not a
                     # project folder that is merely named *-mcp
                     r"|node_modules/(?:@[\w.-]+/)?[\w.-]*-mcp(?:-server)?/", re.I)


def _ancestors(table, pid):
    """The parents of `pid`, nearest first, up to (not including) launchd."""
    seen = {pid}
    while True:
        pid = (table or {}).get(pid, (0,))[0]
        if not isinstance(pid, int) or pid in seen or pid <= 1 or pid not in table:
            return
        seen.add(pid)
        yield pid


_APP_BUNDLE = re.compile(r"\.app/Contents/")


# a claude in a full `ps -o command` line (attribute's table), not in `comm`: the
# path can hold spaces (~/Library/Application Support), arguments follow
_CLAUDE_IN_COMMAND = re.compile(r"(?:^|/)(?:claude|\d+\.\d+\.\d+)(?:\s|\Z)")
# a terminal multiplexer server: what runs in it is the user's, whoever started it
# (ps shows the GNU screen server as SCREEN)
_MULTIPLEXER = re.compile(r"^(?:\S*/)?(?:tmux|screen|zellij)(?:\s|\Z)", re.I)


# per-user services a command starts on first use (`git commit -S`, `git push`
# with ControlPersist, the credential cache, jest's watchman, `colima start`,
# ollama): they inherit the agent's mark and then serve the whole login - its
# passphrases, keys, containers, models. The program itself, not a file that
# mentions one. Not ssh-agent: an agent's `eval $(ssh-agent)` is its own, and
# the login's agent is launchd's, which carries no mark
_SHARED_DAEMON = re.compile(
    r"^(?:\S*/)?(?:gpg-agent|dirmngr|scdaemon|keyboxd|watchman)(?:\s|\Z)"
    r"|^(?:\S*/)?git[ -]credential-cache--daemon(?:\s|\Z)"
    r"|^(?:\S*/)?(?:limactl hostagent|colima daemon)(?:\s|\Z)"
    r"|^(?:\S*/)?ollama serve(?:\s|\Z)"
    r"|^(?:\S*/)?adb\s.*fork-server|org\.gradle\.launcher\.daemon"
    r"|^(?:\S*/)?(?:gvproxy|vfkit|sccache)(?:\s|\Z)|^(?:\S*/)?qemu-system-\S+"
    r"|^(?:\S*/)?emacs\s(?:.*\s)?--(?:bg-|fg-)?daemon|^(?:\S*/)?mutagen daemon"
    r"|^ssh: .+ \[mux\]\Z")


# the transport of an ssh ControlMaster: its ProxyJump (`ssh -W host:port`) or
# ProxyCommand (`nc host port`, `cloudflared access ssh`). Measured: it is NOT
# the master's child but an orphan at ppid 1, so only its own shape tells.
# Killing it drops the master and every session on it
_SSH_JUMP = re.compile(r"^(?:\S*/)?ssh\s(?:.*\s)?-W\s*\S")
_SSH_PROXY = re.compile(r"^(?:\S*/)?cloudflared access ssh(?:\s|\Z)")
_NC = re.compile(r"^(?:\S*/)?nc\s")


def _ssh_transport(cmd):
    if _SSH_JUMP.search(cmd) or _SSH_PROXY.search(cmd):
        return True
    # nc connecting, not listening: `nc -l 3000` is a server an agent started
    return bool(_NC.search(cmd)) and not any(
        re.fullmatch(r"-\w*l\w*", t) for t in cmd.split()[1:])


def _unsure_why(table, pid, cmd, sessions_known, starter=None, over_claude=()):
    """Why a process whose session is gone may still not be litter - or None.
    `left behind` is the group a clean-up agent kills: anything in doubt is not."""
    if not sessions_known:
        return "the session list is incomplete"
    if _SHARED_DAEMON.search(cmd):
        return "it is a shared user daemon"
    if _ssh_transport(cmd):
        return "it carries an ssh connection"
    parents = [table[a][2] for a in _ancestors(table, pid)]
    # what one runs is part of it: pinentry under gpg-agent, a model runner
    # under ollama, a port forward under a VM's host agent
    if any(_SHARED_DAEMON.search(c) for c in parents):
        return "it runs under a shared user daemon"
    # after /clear the claude runs on under a new session id; its work keeps the old
    if starter and starter in table and _is_a_claude(table[starter][2]):
        return "the claude that started it is still running"
    if _is_a_claude(cmd):
        return "it is a claude that ccwho does not list"
    # `caffeinate -i claude`, `script … claude`: killing it kills the claude under it
    if pid in over_claude:
        return f"a {over_claude[pid] if isinstance(over_claude, dict) else 'claude'} runs under it"
    if any(_is_a_claude(c) for c in parents):
        return "it runs under a claude that ccwho does not list"
    # the mark is inherited: in an app or tmux an agent opened, the user works on
    if _MULTIPLEXER.match(cmd) or any(_APP_BUNDLE.search(c) or _MULTIPLEXER.match(c)
                                      for c in parents):
        return "it runs in an app or tmux"
    return None


def attribute(table, marks, ports, sessions, sessions_known=True, own=None, named=None):
    """Who started what. `table` is pid -> (ppid, start, command), `marks` pid ->
    mark_of(...), `ports` pid -> [ports], `sessions` the live Claude sessions.

    Returns {"sessions": {sessionId: [proc]}, "left_behind": [proc], "codex": [proc],
    "unsure": [proc]}. A Claude mark naming a session that is not live is left
    behind - unless there is doubt (`unsure`, with `why`): the session list is
    incomplete (`sessions_known` False) or a running claude is an ancestor. An
    app is `unsure` in every group, whoever started it. A Codex mark is always its own group, because a Codex session's
    liveness is not known. A session's own process is not its work, an unmarked
    process is nobody's, and neither is `own` (ccwho) or anything it runs.

    `named` is sessionId -> pids whose command line names that session, for
    processes whose environment could not be read: listed with the session, so
    the row's count and `ccwho ps` are the same processes.
    """
    live = {s.get("sessionId"): s.get("pid") for s in sessions or []}
    by_pid = {p: s for s, p in live.items() if p}
    # a live session is never anybody's work: a claude started from another
    # session's shell carries that session's mark, and listing it as work - or
    # as left behind - would hand a clean-up agent a live session to kill
    session_pids = {pid for pid in live.values() if pid}
    out = {"sessions": {sid: [] for sid in live}, "left_behind": [], "codex": [],
           "unsure": []}
    # every process with a running claude somewhere under it: one that looks
    # like claude, or a live session whatever its command (a dev build, a
    # symlink name) - killing the `script` above one hangs it up
    over_claude = {}
    for p, (_pp, _st, c) in (table or {}).items():
        kind = _agent_kind(c) or ("claude" if p in session_pids else None)
        for a in _ancestors(table, p) if kind else ():
            over_claude.setdefault(a, kind)
    marks = dict(marks or {})
    for sid, pids in (named or {}).items():
        if sid in live:
            for pid in pids:
                marks.setdefault(pid, ("claude", sid))
    for pid in sorted(marks):
        mark = marks[pid]
        if not mark or pid not in (table or {}) or pid in session_pids:
            continue
        if own and (pid == own or own in _ancestors(table, pid)):
            continue
        ppid, start, cmd = table[pid]
        harness, sid = mark[:2]
        # one rule: a process belongs to the NEAREST agent above it - a listed
        # live session, or any claude or Codex agent ccwho does not list (the
        # Agent SDK, `claude -p`, codex). The process tree outranks the mark:
        # an agent started from a session's shell passes its mark on
        nearest = None
        for a in _ancestors(table, pid):
            if a in by_pid or _is_an_agent(table[a][2]):
                nearest = a
                break
        marked = sid
        if harness == "claude" and nearest in by_pid:
            sid = by_pid[nearest]
        starter = mark[2] if len(mark) > 2 else None
        p = {"pid": pid, "ppid": ppid, "start": start, "command": safe_command(cmd),
             "command_full": redact_command(cmd),
             "ports": list((ports or {}).get(pid, [])), "orphan": ppid == 1,
             "helper": is_helper(cmd), "harness": harness,
             "session": sid, "marked": marked}
        if _APP_BUNDLE.search(cmd):
            # an app an agent opened (measured: Docker Desktop, from Codex) is
            # the user's app now: never work to stop, never litter to clean
            out["unsure"].append(dict(p, why="it is an app"))
        elif _is_an_agent(cmd):
            out["unsure"].append(dict(p, why=f"it is a {_agent_kind(cmd)} that ccwho does not list"))
        elif nearest is not None and nearest not in by_pid:
            kind = _agent_kind(table[nearest][2])
            if harness == "codex" and kind == "Codex agent":
                out["codex"].append(p)      # Codex's own work: the codex line says it
            else:
                out["unsure"].append(dict(p, why=f"it runs under a {kind} that ccwho"
                                                 f" does not list"))
        elif harness == "codex":
            out["codex"].append(p)
        elif sid in live:
            out["sessions"][sid].append(p)
        elif why := _unsure_why(table, pid, cmd, sessions_known, starter, over_claude):
            out["unsure"].append(dict(p, why=why))
        else:
            out["left_behind"].append(p)
    return out


def is_helper(command):
    return bool(_HELPER.search(command or ""))


# ------------------------------------------------ a wait loop that cannot end

# A background Bash task's output file. Absolute, with no `.` or `..` part: a
# relative one would be read against ccwho's own directory, not the loop's.
_TASK_OUTPUT = re.compile(r"^/(?:[^/\s]+/)*tasks/[\w-]+\.output\Z")
_UP = re.compile(r"(?:^|/)\.\.?(?:/|\Z)")
# the one loop shape that can be judged: a condition, and a body of one sleep
_LOOP = re.compile(r"\b(until|while)\s+(.+?);\s*do\s+sleep\s+(\S+?)\s*;\s*done\b", re.S)
# an assignment the shell makes as written: at the start of a command, and a
# value with nothing in it to expand - `echo F=/x` sets nothing, `F=/$D/x` sets
# something ccwho cannot see
_ASSIGN_PATH = re.compile(r"(?:^|(?<=[;&|\n]))\s*(?:export\s+)?([A-Za-z_]\w*)="
                          r"(/[^\s;'\"$`&|()<>]+)(?=[\s;&|]|\Z)")
_SECONDS = re.compile(r"^\d+(?:\.\d+)?\Z")
# the grep options that keep the question plain - "is this line in the file" -
# and so can be asked again by ccwho. -v, -L, -c, -z, -r and the rest ask
# something else, and a condition ccwho cannot re-ask is not judged
_GREP_PLAIN = set("qEFis")
# the last line Claude Code writes when a background task is over. Measured on
# 587 task files: 472 end in `exited with code N`, 24 in `killed`, the other 91
# in neither (still running, or never closed) - and those stay unknown
_TASK_END = re.compile(r"(?:^|\n)\[(?:exited with code -?\d+|killed)\]\s*\Z")


_VAR = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")
# escapes one grep knows and another does not (GNU's \d \s \w \b \| \+ ...):
# asked again by a different grep, the answer could differ
_UNPORTABLE_ESCAPE = re.compile(r"\\[A-Za-z0-9|+?<>{}`']")


def _expand(cond, names):
    """`cond` with each known $NAME replaced by its path, or None when anything
    else would be expanded by the shell - `$X`, `$'..'`, `$(..)`, backticks. The
    shell's value of those is not in the command line, and a pattern asked again
    without it asks a different question."""
    out, i, quote = [], 0, ""
    while i < len(cond):
        c = cond[i]
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "\\":
            out.append(cond[i:i + 2])
            i += 2
            continue
        elif c in "'\"":
            quote = "" if quote == c else (quote or c)
        elif c == "`":
            return None
        elif c == "$":
            m = _VAR.match(cond, i)
            name = m and (m.group(1) or m.group(2))
            if name not in names:
                return None
            out.append(names[name])
            i = m.end()
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _grep(cond, names):
    """(grep argv without files, files) for a condition that is ONE plain grep
    and nothing else, or None."""
    cond = _expand(cond, names)
    if cond is None:
        return None
    try:
        lex = shlex.shlex(cond, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        return None
    # a redirect of grep's noise is all that may follow it
    while len(words) >= 3 and words[-2] == ">" and words[-1] == "/dev/null":
        words = words[:-3] if words[-3] == "2" else words[:-2]
    # an operator, or a second command, is one more "file": it fails the check
    # that every file is a task output
    if words[:1] != ["grep"]:
        return None
    args, flags, pattern, files, i = words[1:], [], None, [], 0
    while i < len(args):
        w = args[i]
        if w.startswith("-") and len(w) > 1 and pattern is None:
            letters = w[1:]
            last_e = letters.endswith("e")
            if not set(letters.rstrip("e") if last_e else letters) <= _GREP_PLAIN \
                    or "e" in letters[:-1]:
                return None
            flags += ["-" + c for c in (letters[:-1] if last_e else letters)]
            if last_e:
                if i + 1 >= len(args):
                    return None
                pattern, i = args[i + 1], i + 2
                continue
        elif pattern is None:
            pattern = w
        else:
            files.append(w)
        i += 1
    if pattern is None or _UNPORTABLE_ESCAPE.search(pattern) \
            or ("-i" in flags and not pattern.isascii()):
        return None
    return flags + ["-e", pattern], files


def _names_before(cmd, at):
    """Variables set to a path before `at`, where set exactly once: the loop
    reads the value it had when it started, and two values is a guess."""
    seen = {}
    for m in _ASSIGN_PATH.finditer(cmd[:at]):
        seen.setdefault(m.group(1), []).append(m.group(2))
    return {k: v[0] for k, v in seen.items() if len(v) == 1}


_EVAL = re.compile(r"\beval\s+(?=')")


def _unwrap_eval(cmd):
    """The command inside the Bash tool's `eval '...'`, quoting undone - the
    grep ccwho asks again must get the pattern the loop's grep got."""
    m = _EVAL.search(cmd)
    if not m:
        return cmd
    try:
        lex = shlex.shlex(cmd[m.end():], posix=True)
        lex.whitespace_split = True
        return lex.get_token() or cmd
    except ValueError:
        return cmd


def wait_loop(cmd):
    """{"files", "sleep", "grep"} for a loop that waits on task outputs, else
    None. `grep` is the condition's own options and pattern, to ask it again.

    Only `until grep ... <task>.output; do sleep N; done`, or `while ! grep`:
    one grep on task outputs as the whole condition, one plain sleep as the whole
    body. Found 2026-09-24 - two such loops polled for a vitest summary that
    their test runs never printed, and kept their sessions `busy` for a day. Any
    other shape is not judged: the process a loop runs in is the Bash tool's
    shell, and it runs everything else in the command too.
    """
    if not cmd:
        return None
    cmd = _unwrap_eval(cmd)
    for m in _LOOP.finditer(cmd):
        kind, cond, secs = m.groups()
        # `done &`: the shell ccwho would signal is not the one looping
        after = cmd[m.end():].lstrip()
        if after.startswith("&") and not after.startswith("&&"):
            continue
        cond = cond.strip()
        if kind == "while":
            if not cond.startswith("! "):
                continue                # waits while the line IS there
            cond = cond[2:].strip()
        elif cond.startswith("!"):
            continue
        if not _SECONDS.match(secs):
            continue
        got = _grep(cond, _names_before(cmd, m.start()))
        if not got:
            continue
        argv, files = got
        if files and all(_TASK_OUTPUT.match(f) and not _UP.search(f) for f in files):
            return {"files": files, "sleep": float(secs), "grep": argv}
    return None


def cannot_end(files, grace):
    """Can a loop on these files never stop? `files` is one (tail, age seconds)
    per file it polls, or None where the file could not be read.

    Only when every file is finished and has stayed so for `grace` - longer than
    a loop sleeps, so it has had its look. Unknown is never "cannot end": the
    cost of a wrong yes is a live loop called dead.
    """
    if not files or any(f is None for f in files):
        return False
    return all(_TASK_END.search(tail or "") and age >= grace for tail, age in files)


def row_summary(mine):
    """What a live row says about its processes: work only, helpers left out."""
    work = [p for p in mine or [] if not p.get("helper")]
    return {"procs": len(work),
            "ports": sorted({port for p in work for port in p.get("ports", [])}),
            "detached": sum(1 for p in work if p.get("orphan"))}


# ------------------------------------------------ what ccwho prints of a command

# An allowlist, not a list of secrets: two reviews of redact_command each found
# secret shapes it missed, and a third found them in the allowlist's own "short
# plain word" - which is exactly what a short password looks like. So a value is
# printed only where its PLACE says it is harmless: a number, a file name, the
# value of a flag or key known to be harmless. Anything else becomes `…`.
# redact_command stays for `ccwho ps --full`.
_SECRET_PARTS = {"token", "tok", "secret", "password", "passwd", "pass", "pwd", "pw",
                 "auth", "authorization", "bearer", "basic", "credential",
                 "credentials", "cookie", "session", "sessionid", "sid", "key",
                 "apikey", "pat", "signature", "sig", "private", "jwt", "otp", "pin"}
_SECRET_WORDS = ("token", "secret", "pass", "apikey", "auth", "cred", "bearer", "cookie",
                 "session", "jwt")
# flags whose value is shown when it is a plain word: what a dev server listens on
# and how it runs
_SAFE_VALUE_FLAGS = {"--port", "--host", "--hostname", "--mode", "--env", "-m",
                     "--config", "--project", "--filter", "--workspace", "--target",
                     "--log-level", "--format", "--type", "--name"}
_SAFE_KEYS = {"PORT", "HOST", "HOSTNAME", "NODE_ENV", "ENV", "MODE", "CI", "TZ",
              "LANG", "LOG_LEVEL", "RAILS_ENV", "FLASK_ENV", "APP_ENV", "DEBUG",
              "PYTHONUNBUFFERED", "FORCE_COLOR", "NO_COLOR", "BROWSER", "VITE_PORT"}
# flags that take a password: after one, nothing more of the line is shown -
# a number included (redis-cli -a, openssl -k, ssh-keygen -N, plink -pw, curl -u,
# security -w)
_PASSWORD_FLAGS = {"-a", "-k", "-K", "-N", "-u", "-U", "-pw", "-w"}
# programs where -p or -P is a password rather than a port
_P_IS_PASSWORD = {"sshpass", "mysql", "mysqldump", "mariadb", "mysqladmin", "mongosh",
                  "mongo", "mongodump", "mongorestore", "sqlcmd", "zip", "unzip", "7z"}
# a password read from stdin: the words before it are the password
_STDIN_PASSWORD = {"--password-stdin", "--passwd-stdin", "--stdin"}
# programs whose arguments are data - piped into whatever reads a password
_DATA_PROGRAMS = {"echo", "printf", "yes"}
# a port, or a dotted number (an address, a version): never a long run of digits,
# which is what a PIN, a card or a token looks like
_NUMBER = re.compile(r"^(?:[0-9]{1,5}|[0-9]{1,3}(?:\.[0-9]{1,3}){1,3}(?::[0-9]{1,5})?)\Z")
_PLAIN = re.compile(r"^[A-Za-z][a-z._-]{0,15}\Z")         # letters only: `dev`, `Run`
# a file: a name with an extension code and config files use. A password or a
# signature can end in `.ab` or `.sig`; and capitals mixed with digits
# (`P4ssw0rd.js`) is a password's shape, not a file's (see _file)
_FILE = re.compile(r"^[A-Za-z0-9_][\w.-]{0,40}\.(?:js|mjs|cjs|ts|mts|cts|jsx|tsx|py|rb|go|"
                   r"rs|java|kt|swift|c|h|cc|cpp|sh|zsh|json|jsonl|yaml|yml|toml|md|txt|"
                   r"log|html|css|scss|sql|lock|conf|cfg|ini|xml|csv|vue|svelte|astro)\Z")
_PROGRAM = re.compile(r"^[A-Za-z0-9][\w.+-]{0,39}\Z")
_LONG_FLAG = re.compile(r"^(--[a-z][a-z0-9-]{1,30})(?:=(.*))?\Z", re.S)
_SHORT_FLAG = re.compile(r"^-([A-Za-z]{1,2}|[0-9]{1,3})\Z")     # -x, -pw, -5
# an npm package at a version: `chrome-devtools-mcp@latest`, `@scope/x@1.2.0`
# only at a tag: `welcome@123` and `summer@2024` are passwords as often as packages
_PACKAGE = re.compile(r"^(@[a-z][a-z-]{0,30}/)?[a-z][a-z-]{0,40}@(latest|next)\Z")
_ASSIGNMENT = re.compile(r"^([^=]{1,41})=(.*)\Z", re.S)
# a key that looks like an environment variable, or a plain lower-case name
_KEY = re.compile(r"^(?:[A-Z][A-Z0-9_]{0,40}|[a-z][a-z_]{0,20})\Z")
_VERSION = re.compile(r"^\(v?[0-9]{1,4}(\.[0-9]{1,4}){0,3}\)\Z")
_URL = re.compile(r"^[a-z][a-z0-9+.-]{1,10}://([^/?#]*)(.*)\Z", re.S)
_HOST = re.compile(r"^[a-z0-9.-]{1,40}(:[0-9]{1,5})?\Z")
_TOKEN_SHAPE = re.compile(r"sk-|sk_|rk_|gh[pousr]_|github_pat_|glpat-|xox[a-z]-|"
                          r"AKIA|ASIA|AIza|npm_|hf_|eyJ")


def _secret_name(name):
    lower = name.lower()
    parts = set(re.split(r"[-_./:]+", lower))
    parts |= {w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", name)}
    return bool(parts & _SECRET_PARTS) or any(w in lower for w in _SECRET_WORDS)


def _plain(word):
    """A short word of letters: `dev`, `http.server`. No digits - `hunter2`,
    `abcd1234` - and nothing long enough to be a key."""
    return bool(_PLAIN.match(word)) and not _secret_name(word)


def _file(word):
    stem = word.rsplit(".", 1)[0]
    mixed = any(c.isupper() for c in stem) and any(c.isdigit() for c in stem)
    return bool(_FILE.match(word)) and not mixed and not _TOKEN_SHAPE.search(word) \
        and not _secret_name(stem)


def _harmless_value(tok):
    """A flag's value that is safe whatever the flag: a package, a file (by its
    name). Or None. Not a number: after `-P` it is a PIN as often as a port, and
    the ports column already says which ports a process holds."""
    if _PACKAGE.match(tok):
        return tok
    base = tok.rstrip("/").rsplit("/", 1)[-1]
    return base if _file(base) else None


def _url(tok):
    m = _URL.match(tok)
    if not m:
        return None
    if "@" in tok:          # user:password@ - the password may hold a `/` or a `?`
        return f"{tok.split('://', 1)[0]}://…"
    host = m.group(1)
    if not _HOST.match(host):
        return "…"
    return f"{tok.split('://', 1)[0]}://{host}" + ("/…" if m.group(2) not in ("", "/") else "")


def _standalone(tok, subcommand):
    """A token that is not a flag's value: shown, or None. A plain word counts
    only in subcommand position (before the first flag): after flags, a word of
    letters is as likely a user name or a password as anything else."""
    if _NUMBER.match(tok) or _VERSION.match(tok) or _file(tok) or _PACKAGE.match(tok):
        return tok
    if subcommand and _plain(tok):
        return tok
    # a process title node sets, split across tokens: `(vitest 1)`, `(worker)` -
    # before any flag, where a title stands
    inner = tok.strip("()")
    if subcommand and inner != tok and (_NUMBER.match(inner) or _plain(inner)):
        return tok
    if (u := _url(tok)) is not None:
        return None if u == "…" else u
    if "/" in tok:
        base = tok.rstrip("/").rsplit("/", 1)[-1]
        if _file(base) or (subcommand and _plain(base)):
            return base
    return None


def _closes(tok):
    """A word after which nothing more of the line is shown: a secret-named word,
    or a header name (`Cookie:`) - what follows is its value, however many words."""
    return tok.endswith(":") or _secret_name(tok)


def safe_command(cmd, program=True):
    """The program and its harmless-looking arguments; `…` for the rest.

    Fails closed: after the first sign of a secret - a secret-named program, flag,
    key or word, a header name, a password flag - nothing more of the line is
    shown. program=False shapes text that has no program word (a Grep pattern).
    """
    if not isinstance(cmd, str):
        cmd = str(cmd) if isinstance(cmd, (int, float)) and not isinstance(cmd, bool) else ""
    text = re.sub(r"[\t\n\r]", " ", cmd)
    tokens = _CONTROL.sub("?", text).split()
    names = {t.rsplit("/", 1)[-1] for t in tokens}
    stdin_password = bool(names & _STDIN_PASSWORD) or ("sudo" in names and "-S" in tokens)
    out, program_seen, flag_seen, value_of = [], not program, False, None
    for i, tok in enumerate(tokens):
        shown, close = None, False
        flag, value_of = value_of, None
        if flag is not None and not tok.startswith("-"):   # this token is a flag's value
            if flag in _SAFE_VALUE_FLAGS:
                shown = _standalone(tok, True)
            else:
                shown = _harmless_value(tok)
            close = _closes(tok)
        elif (m := _ASSIGNMENT.match(tok)) and not tok.startswith("-") \
                and "/" not in m.group(1):
            key, value = m.groups()
            # a key of a sane shape, with a value: `JBSWY3DPEHPK3PXP=` is base32
            if not _KEY.match(key) or _TOKEN_SHAPE.search(key) or not value \
                    or value.startswith("=") or sum(c.isdigit() for c in key) > 2 \
                    or ("_" not in key and len(key) > 12):
                shown = None
            elif _secret_name(key):
                shown, close = f"{key}=…", True
            elif key.upper() in _SAFE_KEYS and (_NUMBER.match(value) or _plain(value)):
                shown = tok
            else:
                shown = f"{key}=…"
        elif not program_seen:
            name = tok.rsplit("/", 1)[-1].lstrip("-")       # `-zsh`: a login shell
            if _PROGRAM.match(name) and not _TOKEN_SHAPE.search(name):
                shown = name
                # sshpass, htpasswd - and echo or printf, whose arguments are data
                close = _secret_name(name) or stdin_password or name in _DATA_PROGRAMS
            program_seen = True
        elif m := _LONG_FLAG.match(tok):
            flag_seen = True
            name, value = m.groups()
            if _secret_name(name[2:]):
                shown, close = name if value is None else f"{name}=…", True
            elif value is None:
                shown, value_of = name, name
            elif name in _SAFE_VALUE_FLAGS:
                shown = f"{name}={value}" if _standalone(value, True) == value else f"{name}=…"
            else:
                shown = f"{name}=…"
        elif _SHORT_FLAG.match(tok):
            flag_seen = True
            shown, value_of = tok, tok
            if tok in _PASSWORD_FLAGS or (tok in ("-p", "-P") and names & _P_IS_PASSWORD):
                close = True
        elif tok.startswith("-"):
            # a flag of a shape ccwho does not parse (`--admin_code`, `-xyz`, `--`,
            # a look-alike letter): what it means is not known, so it ends the line
            shown, close = None, True
        else:
            shown = _standalone(tok, not flag_seen)
            close = _closes(tok)
        out.append(shown or "…")
        if close:
            if i + 1 < len(tokens):
                out.append("…")
            break
    collapsed = []
    for w in out:
        if not (w == "…" and collapsed and collapsed[-1] == "…"):
            collapsed.append(w)
    return " ".join(collapsed)


# ------------------------------------------------------------ what a kill takes

# every way Claude Code runs: the `claude` binary or its versioned copy
# (_CLAUDE_IN_COMMAND), and the npm package under node or npx
# An agent is known by the program that runs, never by a path among its
# arguments: `cd ~/src/codex && npm run dev` is no agent.
#  - an executable: claude, claude-code, claude.exe, .../claude/versions/N.N.N,
#    codex, or a file inside the agent's npm package (codex's vendor binary);
#  - a runtime (node, tsx, deno, bun) running the agent's script: the package's
#    cli.js, or .../bin/claude or .../bin/codex, anywhere among its arguments
#    (`node --require x.js .../cli.js` too);
#  - a package runner (npx, bunx; npm, pnpm, yarn with exec or dlx) running the
#    agent's package spec, after the flags and the values they take.
_RUNTIMES = {"node", "tsx", "ts-node", "deno", "bun"}
_PKG_RUNNERS = {"npx", "bunx", "npm", "pnpm", "yarn"}
_RUN_VERBS = {"exec", "dlx", "x"}
_RUNTIME_VALUE_FLAGS = {"-r", "--require", "--import", "--loader", "--experimental-loader",
                        "--max-old-space-size", "--inspect-port", "--conditions", "-C",
                        "--env-file", "--config"}
_VALUE_FLAGS = {"--prefix", "--cwd", "-C", "--filter", "-F", "-w", "--workspace", "--dir",
                "-p", "--package", "--registry", "--userconfig", "--cache"}
_CLAUDE_EXEC = re.compile(r"^claude(?:-code|\.exe)?\Z")
_CLAUDE_VERSION = re.compile(r"/claude/versions/\d+\.\d+\.\d+\Z")
_CLAUDE_PKG = re.compile(r"@anthropic-ai/(?:claude-code|claude-agent-sdk)(?:@[\w.^~-]+)?(?:/|\Z)")
_CODEX_PKG = re.compile(r"@openai/codex(?:@[\w.^~-]+)?(?:/|\Z)")
_AGENT_BIN = re.compile(r"/\.?bin/(claude|codex)\Z")


def _program(toks):
    """The executable, and how many words it takes: `ps` shows a path with
    spaces unquoted (~/Library/Application Support/...), so an absolute path
    goes on across words that start with a capital - `git clone https://...`
    stays git."""
    head = [toks[0]]
    if toks[0].startswith("/"):
        run = []
        for t in toks[1:8]:
            if not t[:1].isupper():
                break
            run.append(t)
        # `claude Fix the login bug` is a prompt, not a path with spaces
        while run and "/" not in " ".join(run):
            run.pop()
        while run and "/" not in run[-1] and not run[-1][:1].isupper():
            run.pop()
        head += run
    return " ".join(head), len(head)


def _kind_of_package(word):
    if _CLAUDE_PKG.search(word):
        return "claude"
    if _CODEX_PKG.search(word):
        return "Codex agent"
    return None


def _kind_of_script(word):
    m = _AGENT_BIN.search(word)
    if m:
        return "claude" if m.group(1) == "claude" else "Codex agent"
    return _kind_of_package(word)


def _agent_kind(cmd):
    """"claude" or "Codex agent" when the program that runs is one - else None."""
    toks = (cmd or "").split()
    if not toks:
        return None
    prog, used = _program(toks)
    base = prog.rsplit("/", 1)[-1]
    if _CLAUDE_EXEC.match(base) or _CLAUDE_VERSION.search(prog):
        return "claude"
    if base == "codex":
        return "Codex agent"
    if kind := _kind_of_package(prog):
        return kind
    rest = toks[used:]
    if base in _PKG_RUNNERS or (base == "bun" and rest[:1] == ["x"]):
        words, skip, verb_seen = [], None, base in ("npx", "bunx")
        for t in rest:
            if skip:
                # `-p`/`--package` names the package itself: `npx -p pkg cmd`
                if skip in ("-p", "--package") and (kind := _kind_of_package(t)):
                    return kind
                skip = None
            elif t in _VALUE_FLAGS:
                skip = t
            elif not t.startswith("-"):
                words.append(t)
        if not verb_seen:
            if not words or words[0] not in _RUN_VERBS:
                return None
            words = words[1:]
        return _kind_of_package(words[0]) if words else None
    if base in _RUNTIMES:
        skip = False
        for t in rest:
            if skip:
                skip = False
            elif t in _RUNTIME_VALUE_FLAGS:
                skip = True
            elif t.startswith("-") or t in ("run", "x"):
                continue
            else:
                return _kind_of_script(t)       # the script, and only it
    return None


def _is_an_agent(cmd):
    return _agent_kind(cmd) is not None


def _is_a_claude(cmd):
    return _agent_kind(cmd) == "claude"


def _doubt(table, pid, cmd, below=None):
    """Why a process is not litter by its own shape, whatever its session: a
    shared daemon, an ssh connection, what a daemon runs, an app, tmux - or
    None. A LIVE session's work gets no other doubt from attribute().

    `below`: the session's own claude. Only what is between it and the process
    counts: the terminal app above every claude is no reason to doubt its work."""
    if _SHARED_DAEMON.search(cmd):
        return "it is a shared user daemon"
    if _ssh_transport(cmd):
        return "it carries an ssh connection"
    if _APP_BUNDLE.search(cmd):
        return "it is an app"
    parents = []
    for a in _ancestors(table, pid):
        if a == below:
            break
        parents.append(table[a][2])
    if any(_SHARED_DAEMON.search(c) for c in parents):
        return "it runs under a shared user daemon"
    if _MULTIPLEXER.match(cmd) or any(_APP_BUNDLE.search(c) or _MULTIPLEXER.match(c)
                                      for c in parents):
        return "it runs in an app or tmux"
    return None


_SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "mksh", "tcsh", "csh", "nu", "xonsh",
           "pwsh"}
_SHELL_VALUE_OPTS = {"--rcfile", "--init-file", "-o", "+o", "-O", "+O"}


def _person_shell(cmd):
    """A shell someone may be typing in: a login shell (`-zsh`), or one run
    with only options - no script, no `-c`. Only its LEADING options count: a
    tool shell's eval text (`grep -i`, `sed -i`) is not its options."""
    toks = (cmd or "").split()
    if not toks:
        return False
    if toks[0].startswith("-") and toks[0][1:].rsplit("/", 1)[-1] in _SHELLS:
        return True
    if toks[0].rsplit("/", 1)[-1] not in _SHELLS:
        return False
    args, k = toks[1:], 0
    while k < len(args):
        a = args[k]
        if a in _SHELL_VALUE_OPTS:
            k += 2
        elif a.startswith("--"):
            k += 1                      # --norc, --login: `c` in a long option is no -c
        elif a[:1] in "-+" and len(a) > 1:
            if "c" in a[1:]:
                return False            # a command: a tool call
            if "i" in a[1:]:
                return True
            k += 1
        else:
            return False                # a script
    return True


# refusal only: an agent's package or binary anywhere in a member's command. A
# false positive costs a refusal; a miss would cost an agent
_ANY_AGENT = re.compile(r"@anthropic-ai/(?:claude-code|claude-agent-sdk)|@openai/codex"
                        r"|/claude-code/|/claude/versions/|/\.?bin/(?:claude|codex)(?:\s|\Z)")


def _name(cmd):
    return (safe_command(cmd).split() or ["?"])[0]


def kill_plan(mode, target, world):
    """What a kill would signal, and why everything else is spared - from one
    scan, before anything is signalled. Returns {"kill": [proc], "spare": [proc
    with "why"]} and, for a plan that takes nothing for one reason, "why"; each
    proc has pid, start, command (short form), ports, and the mark (harness,
    session) the signaller checks again before it signals. A kill may carry a
    "note" to say with it.

    mode "pid": `target` is {"pid", "start"} as the caller listed it. Named:
        it acts, unless the pid is gone, cannot be proved the same process, or
        is one of the never-killed below; what killing it means is its note.
    mode "port": `target` is a port. Its agent-started holders; an unmarked or
        not-sure holder, or a live session's helper, is named, never killed.
    mode "clean": everything left behind.
    mode "session": `target` is a live session id; its work, helpers out.

    Never, in any mode: a live session, any claude (the binary, its versions,
    the npm package), a process with a claude under it, ccwho, or a process
    above or below ccwho (the shell that runs it). Never in a computed mode or
    by port: what _doubt() protects - shared daemons, ssh connections, apps,
    tmux - even in a live session's work. A computed mode takes nothing when
    ccwho cannot find itself in the scan, or when the pipes were not read.

    `world`: "table" pid -> (ppid, start, command), "att" (attribute's answer
    for the same scan), "sessions" (live), "own" (ccwho's pid), "ports" pid ->
    [ports], "pipes" pid -> pids holding the other end of its stdin/stdout,
    read at kill time (None: not read). A process whose pipe leads to a live
    process not also being killed is spared (an ssh mux's ProxyCommand is an
    orphan feeding the master, and no list of names covers every tool); in
    "pid" mode that is said as a note.
    """
    # one clean view of the world: a malformed row or entry is skipped, never raised
    table = {k: (v[0] if isinstance(v[0], int) else 0, str(v[1] or ""), str(v[2] or ""))
             for k, v in (world.get("table") or {}).items()
             if isinstance(k, int) and isinstance(v, (tuple, list)) and len(v) >= 3}
    raw_att = world.get("att") if isinstance(world.get("att"), dict) else {}

    def entries(items):
        return [p for p in items or [] if isinstance(p, dict) and isinstance(p.get("pid"), int)]
    att = {name: entries(raw_att.get(name)) for name in ("left_behind", "codex", "unsure")}
    att["sessions"] = {sid: entries(mine) for sid, mine in
                       (raw_att.get("sessions") or {}).items()}
    ports = {k: v for k, v in (world.get("ports") or {}).items() if isinstance(v, (list, tuple))}
    own = world.get("own")
    pipes = world.get("pipes")
    marks = world.get("marks")
    sessions = [s for s in world.get("sessions") or [] if isinstance(s, dict)]
    live = {s.get("pid") for s in sessions if s.get("pid")}
    live_ids = {s.get("sessionId") for s in sessions if s.get("sessionId")}
    group = {}
    for sid, mine in (att.get("sessions") or {}).items():
        for p in mine:
            group[p["pid"]] = ("session", sid, p)
    for name in ("left_behind", "codex", "unsure"):
        for p in att.get(name) or []:
            group[p["pid"]] = (name, p.get("session"), p)
    above_ccwho = set(_ancestors(table, own)) if own in table else set()
    # an ancestor of any agent, or of a live session whatever its command:
    # killing it ends or hangs up what runs under it
    over_agent = {}
    for q, (_pp, _st, c) in table.items():
        kind = _agent_kind(c) or ("claude" if q in live else None)
        for a in _ancestors(table, q) if kind else ():
            over_agent.setdefault(a, kind)
    session_pid = {s.get("sessionId"): s.get("pid") for s in sessions}

    def doubt_of(pid):
        # a live session's work: only what is below its claude counts
        kind = group.get(pid)
        below = session_pid.get(kind[1]) if kind and kind[0] == "session" else None
        return _doubt(table, pid, cmd_of(pid), below)
    codex_note = "ccwho cannot tell whether the Codex session that started it is still running"

    def nothing(why):
        return {"kill": [], "spare": [], "why": why}

    def cmd_of(pid):
        return table.get(pid, (0, "", ""))[2]

    def proc(pid):
        _ppid, start, cmd = table.get(pid, (0, "", ""))
        kind = group.get(pid)
        return {"pid": pid, "ppid": _ppid, "start": start, "command": safe_command(cmd),
                "ports": list(ports.get(pid) or []),
                "harness": kind[2].get("harness") if kind else None,
                "session": kind[1] if kind else None,
                # the mark in its environment: what the signaller checks again
                "marked": kind[2].get("marked", kind[1]) if kind else None}

    def spare(pid, why):
        return dict(proc(pid), why=why)

    def untouchable(pid):
        if pid in live:
            return f"{pid} is a live Claude session - ccwho does not kill a session"
        if own and (pid == own or own in _ancestors(table, pid)):
            return f"{pid} is part of this ccwho run"
        if pid in above_ccwho:
            return f"{pid} runs ccwho - killing it would end this command"
        kind = _agent_kind(cmd_of(pid))
        if kind:
            return f"{pid} looks like a {kind} - ccwho does not kill an agent"
        if pid in over_agent:
            return f"a {over_agent[pid]} runs under {pid} - killing it would end it"
        return None

    def live_peers(pid, kill):
        # pipes are read at kill time: a peer there is alive, in the scan or
        # not - and a peer that is not a pid at all is not known to be gone.
        # Any live peer, an agent included: `claude -p | tee` dies with its tee
        return [q for q in sorted((pipes or {}).get(pid) or (), key=str)
                if q != pid and q not in kill]

    def feeds(pid, q):
        name = _name(cmd_of(q)) if q in table else "a process newer than the scan"
        q = printable(str(q))[:12]
        return f"{pid} feeds {q} ({name}), which is still running"

    if own is None or own not in table:
        # the shell that runs ccwho is only known through ccwho's own process
        return nothing("ccwho cannot find its own process in the scan - nothing killed")
    if mode == "pid":
        target = target if isinstance(target, dict) else {"pid": target}
        raw = target.get("pid")
        # a whole number, or text of one: 20.9 is not a pid (True is 1, refused below)
        text = raw.strip() if isinstance(raw, str) else ""
        if not (isinstance(raw, int) or (text.isascii() and text.isdigit() and len(text) < 8)):
            return nothing("no pid given")
        pid = int(raw)
        if pid <= 1:
            return nothing("that is no process ccwho would kill")
        if pid not in table:
            return {"kill": [], "spare": [dict(pid=pid, start="", command="", ports=[],
                                               why=f"{pid} already exited - nothing to do")]}
        listed, now = _norm(str(target.get("start") or "")), _norm(table[pid][1])
        if not listed or not now:
            return {"kill": [], "spare": [spare(pid, f"cannot prove {pid} is the same process"
                                                     " - not killed; run ccwho ps again")]}
        if listed != now:
            return {"kill": [], "spare": [spare(pid, f"{pid} is now a different process"
                                                     " - not killed; run ccwho ps again")]}
        why = untouchable(pid)
        if why:
            return {"kill": [], "spare": [spare(pid, why)]}
        notes = []
        kind = group.get(pid) or ("",)
        if kind[0] == "codex":
            notes.append(codex_note)
        # attribute's own doubt first (a /clear starter, an incomplete list),
        # then the shape of the process itself
        doubt = (kind[2].get("why") if kind[0] == "unsure" else None) or doubt_of(pid)
        if doubt:
            notes.append(f"not sure: {doubt}")
        alive = live_peers(pid, {pid})
        if alive:
            notes.append(feeds(pid, alive[0]))
        elif pipes is None:
            notes.append("its pipes were not read - it may feed a process that is still running")
        k = proc(pid)
        if notes:
            k["note"] = "; ".join(notes)
        return {"kill": [k], "spare": []}

    if mode == "session":
        # v1 (after 8 review rounds): which processes a LIVE session owns has too
        # long a tail of shapes to kill them unnamed
        return nothing("ccwho does not kill a live session's work yet - name each"
                       " process: ccwho kill <pid>")
    if mode not in ("port", "clean"):
        return nothing(f"unknown kind of kill: {printable(str(mode))[:20]}")
    if not isinstance(marks, dict):
        # "read, no mark" (someone else started it) cannot be told from "not
        # readable" without them
        return nothing("the environments were not read - nothing killed")
    spared = []
    # v1: unnamed, ccwho takes only what an ended session left as a whole: an
    # ORPHAN (reparented to launchd, its session gone, no agent above it) and
    # everything under it. SIGTERM to a `zsh -c` tool shell is not passed on,
    # so killing the orphan alone left its dev server holding the port
    litter = {p["pid"] for p in att.get("left_behind") or []
              if table.get(p["pid"], (0,))[0] == 1}
    children = {}
    for q, row in table.items():
        children.setdefault(row[0] if row else None, []).append(q)

    def subtree(root):
        out, todo, seen = [], [root], set()
        while todo:
            q = todo.pop(0)
            if q in seen:
                continue
            seen.add(q)
            out.append(q)
            todo += sorted(children.get(q, []))
        return out

    def marked_of(q):
        kind = group.get(q)
        return kind[2].get("marked", kind[1]) if kind else None

    def verdict(root):
        """(members, None) when the whole tree may go; (None, why) when not."""
        members = subtree(root)
        mark = marked_of(root)
        for q in members:
            cmd = cmd_of(q)
            kind = group.get(q)
            why = untouchable(q)
            if why is None and not (table.get(q, (0, ""))[1] or "").strip():
                why = f"cannot prove {q} is the same process"
            if why is None and (pipes is None or q not in pipes):
                why = f"{q}: its pipes were not read"
            if why is None and kind is None and q in marks and not marks[q]:
                # its environment was read and names no session: a person or
                # another program started it, under the orphan or not
                why = f"{q} {_name(cmd)}: no agent started it"
            if why is None and _ANY_AGENT.search(cmd):
                why = f"{q} may be an agent"
            if why is None and (reason := _doubt(table, q, cmd)):
                why = f"{q} is not sure: {reason}"
            if why is None and _person_shell(cmd):
                why = f"{q} is a shell someone may be working in"
            if why is None and kind and kind[0] in ("session", "codex", "unsure"):
                why = (f"{q} is not sure: {kind[2].get('why', '?')}" if kind[0] == "unsure"
                       else f"{q} belongs to " + ("a live session" if kind[0] == "session"
                                                  else "Codex"))
            if why is None and marked_of(q) not in (None, mark):
                why = f"an agent runs here - {q} carries another session's mark"
            if why:
                return None, why
        return members, None

    def root_of(pid):
        chain = [pid] + list(_ancestors(table, pid))
        return chain[-1] if table.get(chain[-1], (0,))[0] == 1 else None

    trees, notes = {}, {}
    if mode == "port":
        try:
            text = str(target).lstrip(":")
            port = int(text) if text.isascii() and text.isdigit() else None
        except (TypeError, ValueError):
            port = None
        if port is None:
            return nothing("not a port number")
        for pid in sorted(p for p, held in ports.items() if port in (held or [])):
            kind = group.get(pid)
            name = _name(cmd_of(pid))
            root = root_of(pid)
            why = untouchable(pid)
            if why is None and "com.docker" in cmd_of(pid):
                why = (f":{port} is held by Docker (a container) - ccwho cannot tell which"
                       f" agent started it")
            elif why is None and root in litter:
                members, bad = verdict(root)
                if members:
                    trees[root] = members
                    continue
                why = (f":{port} is held by {pid} {name}, which ccwho will not take: {bad}"
                       f" - ccwho kill {pid} if you mean it")
            elif why is None and kind is None:
                why = (f":{port} is held by {pid} {name}, which no agent started"
                       f" - ccwho kill {pid} if you mean it")
            elif why is None and (kind[0] == "unsure" or doubt_of(pid)):
                reason = kind[2].get("why") if kind[0] == "unsure" else doubt_of(pid)
                why = (f":{port} is held by {pid} {name}, which is not sure: {reason}"
                       f" - ccwho kill {pid} if you mean it")
            elif why is None and kind[0] == "session":
                why = (f":{port} is held by {pid} {name}, work of a live session"
                       f" - ccwho kill {pid} if you mean it")
            elif why is None and kind[0] == "codex":
                why = (f":{port} is held by {pid} {name}, started by Codex - {codex_note}"
                       f" - ccwho kill {pid} if you mean it")
            elif why is None:
                why = (f":{port} is held by {pid} {name}, which is not an orphan"
                       f" - ccwho kill {pid} if you mean it")
            spared.append(spare(pid, why))
    else:                                               # clean
        refused = set()
        for root in sorted(litter):
            members, bad = verdict(root)
            if members:
                trees[root] = members
            else:
                spared.append(spare(root, f"{root} and what runs under it: {bad}"))
                for q in subtree(root)[1:]:
                    refused.add(q)
                    spared.append(spare(q, f"{q} is in {root}'s tree, which is spared: {bad}"))
        in_trees = {q for m in trees.values() for q in m} | litter | refused
        spared += [spare(p["pid"], f"{p['pid']} is not sure: {p.get('why', '?')}")
                   for p in att.get("unsure") or [] if p["pid"] not in in_trees]
        spared += [spare(p["pid"], f"{p['pid']} was started by Codex - {codex_note};"
                                   f" ccwho kill {p['pid']} one at a time")
                   for p in att.get("codex") or [] if p["pid"] not in in_trees]
        spared += [spare(p["pid"], f"{p['pid']} is not an orphan - ccwho kill {p['pid']} if"
                                   f" you mean it")
                   for p in att.get("left_behind") or [] if p["pid"] not in in_trees]
    # the pipe rule, per tree and to a fixed point: a member whose pipe leads out
    # of everything being killed spares its whole tree - and a tree spared
    # stays alive, so what feeds IT is spared too
    changed = True
    while changed:
        changed = False
        taking = {q for m in trees.values() for q in m}
        for root, members in list(trees.items()):
            for q in members:
                alive = live_peers(q, taking)
                if alive:
                    del trees[root]
                    spared.append(spare(root, f"{root} and what runs under it: "
                                              f"{feeds(q, alive[0])} - not killed"))
                    changed = True
                    break
            if changed:
                break
    kill = [(root, q) for root in sorted(trees) for q in trees[root]]
    plan = {"kill": [dict(proc(q), root=root, **({"note": notes[q]} if q in notes else {}))
                     for root, q in kill], "spare": spared}
    if not kill and not spared:         # nothing to do still says so
        plan["why"] = f"nothing holds :{port}" if mode == "port" else "nothing was left behind"
    return plan
