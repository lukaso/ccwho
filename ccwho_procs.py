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
                     r"|@modelcontextprotocol/server-[\w.-]+", re.I)


def _ancestors(table, pid):
    """The parents of `pid`, nearest first, up to (not including) launchd."""
    seen = {pid}
    while True:
        pid = (table or {}).get(pid, (0,))[0]
        if pid in seen or pid <= 1 or pid not in table:
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


def _unsure_why(table, pid, cmd, sessions_known, starter=None, over_claude=()):
    """Why a process whose session is gone may still not be litter - or None.
    `left behind` is the group a clean-up agent kills: anything in doubt is not."""
    if not sessions_known:
        return "the session list is incomplete"
    # after /clear the claude runs on under a new session id; its work keeps the old
    if starter and starter in table and _CLAUDE_IN_COMMAND.search(table[starter][2]):
        return "the claude that started it is still running"
    if _CLAUDE_IN_COMMAND.search(cmd):
        return "it is a claude that ccwho does not list"
    # `caffeinate -i claude`, `script … claude`: killing it kills the claude under it
    if pid in over_claude:
        return "a claude runs under it"
    parents = [table[a][2] for a in _ancestors(table, pid)]
    if any(_CLAUDE_IN_COMMAND.search(c) for c in parents):
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
    # a live session is never anybody's work: a claude started from another
    # session's shell carries that session's mark, and listing it as work - or
    # as left behind - would hand a clean-up agent a live session to kill
    session_pids = {pid for pid in live.values() if pid}
    out = {"sessions": {sid: [] for sid in live}, "left_behind": [], "codex": [],
           "unsure": []}
    # every process with a running claude somewhere under it
    over_claude = {a for p, (_pp, _st, c) in (table or {}).items()
                   if _CLAUDE_IN_COMMAND.search(c) for a in _ancestors(table, p)}
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
        starter = mark[2] if len(mark) > 2 else None
        p = {"pid": pid, "ppid": ppid, "start": start, "command": safe_command(cmd),
             "command_full": redact_command(cmd),
             "ports": list((ports or {}).get(pid, [])), "orphan": ppid == 1,
             "helper": is_helper(cmd), "harness": harness,
             "session": sid}
        if _APP_BUNDLE.search(cmd):
            # an app an agent opened (measured: Docker Desktop, from Codex) is
            # the user's app now: never work to stop, never litter to clean
            out["unsure"].append(dict(p, why="it is an app"))
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
