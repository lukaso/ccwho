#!/usr/bin/env python3
"""Every session on disk, findable - not only the ones running right now.

"Sometimes I need to find sessions from a while ago, so all sessions are fair
game." The live fleet is fifteen; the disk holds every session of the last month.
Measured 2026-09-19: 1,480 transcripts, 1 GB, oldest 29 days (Claude Code deletes
them after `cleanupPeriodDays`, 30 by default).

Reading a gigabyte per tick is not an option, so this keeps a small index and
reads only what was APPENDED since last time. Transcripts only grow at the end,
so (inode, size, offset) is enough to resume, and it also fixes the head/tail
window's blind spot: a recap in the unread middle of a long transcript is invisible
to the brief's windows but is seen exactly once by this.

Rules change - `is_human_prompt` learns a new machine prefix - so every entry
carries the version of the rules that built it. A bump re-reads from byte zero:
an upgrade that leaves old entries wrong is an upgrade that fixed nothing.
"""
from __future__ import annotations

import glob
import json
import os
import tempfile

import ccwho_brief as brief

# Bump when a rule in ccwho_brief changes what an entry would say. Entries built
# by an older version are re-read from the start.
EXTRACT_VERSION = 1

# An index is a finding aid, not an archive. The first build over the real 1,402
# transcripts made a 177 MB file, because people paste whole logs into prompts and
# every one was stored whole. You recognise a line by its front.
TEXT_CAP = 300
RECAP_CAP = 800
TITLE_CAP = 120

# An unfinished last line is carried to the next read. A record being written
# right now can be megabytes (a pasted file), and carrying that forever - copied
# into the index file on every save - is not worth the one record it completes.
PENDING_CAP = 64 * 1024

_FIELDS = ("sessionId", "path", "project", "title", "opened", "you_said",
           "recap", "recap_ts", "turns_since_recap", "last_ts", "inode", "size",
           "offset", "pending", "v", "last_any", "cwd", "entrypoint")


def config_roots(roots_file=None):
    """Every Claude Code config directory to read transcripts from.

    Sessions do not all run the same way: some use the login in ~/.claude, some a
    CLAUDE_CODE_OAUTH_TOKEN, and CLAUDE_CONFIG_DIR moves a session's whole
    directory elsewhere. ccwho never logs in and never calls the API - it reads
    files - so the only thing that can tie it to one login is a hard-coded path.

    ~/.ccwho/roots lists extra directories, one per line, # for comments.
    """
    roots = [os.environ.get("CLAUDE_CONFIG_DIR", ""),
             os.path.expanduser("~/.claude")]
    path = roots_file or os.path.expanduser("~/.ccwho/roots")
    try:
        with open(path) as fh:
            roots += [line.strip() for line in fh
                      if line.strip() and not line.strip().startswith("#")]
    except OSError:
        pass
    seen, out = set(), []
    for r in roots:
        r = os.path.expanduser(r)
        if r and r not in seen:
            seen.add(r)
            out.append(r)
    return out


def transcripts(claude_dir=None, roots=None):
    """Every transcript in every root, newest first."""
    where = [claude_dir] if claude_dir else (roots or config_roots())
    paths = []
    for root in where:
        paths += glob.glob(os.path.join(root, "projects", "*", "*.jsonl"))
    return sorted(set(paths), key=_mtime, reverse=True)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def update(idx, paths, limit=None):
    """Fold every transcript into the index, reading only what is new.

    Returns a NEW dict - entries whose transcript is gone are dropped, because
    Claude Code deletes them after a month and a search that offers a session
    with no transcript offers nothing.
    """
    out, seen = dict(idx or {}), set()
    for path in paths[:limit] if limit else paths:
        sid = os.path.basename(path)[:-len(".jsonl")]
        seen.add(sid)
        entry = out.get(sid)
        try:
            st = os.stat(path)
        except OSError:
            continue                      # deleted between listing and stat
        if _is_current(entry, st):
            continue
        # A copy, never the caller's dict: mutating in place made "did anything
        # change?" answer no, so nothing was ever saved and every append was
        # re-read on the next run.
        entry = _scan(path, sid, dict(entry) if entry else None, st)
        if entry:
            out[sid] = entry
        else:
            out.pop(sid, None)            # nothing in it worth remembering yet
    for gone in set(out) - seen:
        out.pop(gone, None)
    return out


def _is_current(entry, st):
    return bool(entry) and (entry.get("inode") == st.st_ino
                            and entry.get("size") == st.st_size
                            and entry.get("v") == EXTRACT_VERSION)


def _scan(path, sid, entry, st):
    """Read the new bytes and fold them into the entry."""
    fresh = (not entry or entry.get("inode") != st.st_ino
             or entry.get("v") != EXTRACT_VERSION
             or st.st_size < entry.get("size", 0))     # shrank: replaced, not appended
    if fresh:
        entry = _blank(sid, path)
    offset = 0 if fresh else entry.get("offset", 0)
    try:
        chunk = _read_from(path, offset)          # BYTES: see _read_from
    except OSError:
        return entry if entry and entry.get("last_ts") else None
    entry["offset"] = offset + len(chunk)
    entry["inode"], entry["size"], entry["v"] = st.st_ino, st.st_size, EXTRACT_VERSION
    raw = _unpend(entry.get("pending", "")) + chunk
    pieces = raw.split(b"\n")
    pending = pieces.pop()                        # no newline yet: not a record
    entry["pending"] = _pend(pending)
    _fold(entry, [p.decode("utf-8", "replace") for p in pieces])
    return entry if entry.get("last_ts") or entry.get("opened") else None


def _blank(sid, path):
    return {"sessionId": sid, "path": path, "project": _project_of(path), "cwd": "",
            "title": "", "opened": "", "you_said": "", "last_any": "",
            "recap": "", "recap_ts": "",
            "turns_since_recap": 0, "last_ts": "", "inode": 0, "size": 0,
            "offset": 0, "pending": "", "v": EXTRACT_VERSION, "entrypoint": ""}


def _read_from(path, offset):
    """The bytes from `offset` to the end.

    Binary, deliberately. Reading text and counting the encoded length to find
    the new offset walks past bytes that were never read when a multi-byte
    character straddles the boundary - "fix cafe rendering" came back with the
    accent replaced and the next word eaten. Bytes are split on newlines and
    decoded per line, so a character cut in half simply waits in `pending`.
    """
    with open(path, "rb") as fh:
        fh.seek(offset)
        return fh.read()


def _pend(raw):
    """Carry an unfinished line as text the index can store, bounded."""
    if len(raw) > PENDING_CAP:
        return ""                # too big to be worth completing; the next
                                 # newline resynchronises the read
    return raw.decode("utf-8", "surrogateescape")


def _unpend(text):
    return (text or "").encode("utf-8", "surrogateescape")


def _clip(text, cap):
    text = " ".join(str(text or "").split())
    return text if len(text) <= cap else text[:cap - 1] + "\u2026"


def _fold(entry, lines):
    """Apply records to an entry. Called once per record over the session's life,
    which is why the turn count can be kept running instead of recomputed."""
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue                      # a torn or foreign line is not data
        if not isinstance(rec, dict):
            continue
        ep = rec.get("entrypoint")
        if isinstance(ep, str) and ep:
            entry["entrypoint"] = ep
        cwd = rec.get("cwd")
        if isinstance(cwd, str) and cwd and cwd != entry.get("cwd"):
            entry["cwd"] = cwd
            entry["project"] = brief.project_of(cwd)
        ts = rec.get("timestamp")
        if isinstance(ts, str) and ts > entry["last_ts"]:
            entry["last_ts"] = ts
        if rec.get("type") == "ai-title" and rec.get("aiTitle"):
            entry["title"] = _clip(rec["aiTitle"], TITLE_CAP)
            continue
        if rec.get("type") == "system" and rec.get("subtype") == "away_summary":
            r = brief.recap([rec])
            if r["text"]:
                entry["recap"] = _clip(r["text"], RECAP_CAP)
                entry["recap_ts"] = r["ts"]
                entry["turns_since_recap"] = 0     # the count starts again here
            continue
        prompt = brief.last_human_prompt([rec])
        if prompt:
            # Two fields, because "continue" is yours and says nothing: the last
            # SUBSTANTIVE prompt is what identifies the session, and the last
            # human one is the fallback when you only ever said "go ahead".
            entry["last_any"] = _clip(prompt, TEXT_CAP)
            if brief.is_substantive(prompt):
                entry["you_said"] = _clip(prompt, TEXT_CAP)
                if not entry["opened"]:
                    entry["opened"] = entry["you_said"]
        if brief._is_turn(rec):
            entry["turns_since_recap"] += 1


def _project_of(path):
    """Fallback only: Claude Code's folder name, which is the cwd with slashes
    turned into dashes. Its last segment is a worktree's BRANCH, so a real cwd
    from the records replaces this as soon as one is read."""
    slug = os.path.basename(os.path.dirname(path))
    return slug.rsplit("-", 1)[-1] or "?"


# ------------------------------------------------------------------ persistence

def save(idx, path):
    """One entry per line, through a temp + os.replace: an index read while this
    runs is the old one or the new one, never half of each."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    # A temp file per writer: a watch, a one-shot and the launchd save can all be
    # writing at once, and a shared name means one truncates the other's file and
    # publishes half an index.
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".",
                                   suffix=".tmp")
        with os.fdopen(fd, "w") as fh:
            for entry in idx.values():
                fh.write(json.dumps(entry) + "\n")
        os.replace(tmp, path)
    except OSError:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def load(path):
    """The index, or an empty one. A torn line loses that session, not the file:
    the index is a cache, and the cost of a bad line is one re-read."""
    out = {}
    try:
        with open(path) as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(entry, dict) and entry.get("sessionId"):
                    out[entry["sessionId"]] = entry
    except OSError:
        return {}
    return out


# ---------------------------------------------------------------------- search

# Where a word matched says how much it means. A title or a recap is ABOUT the
# session; a pasted log that happens to contain the word is not, and burying the
# session you meant under three that quote it is how search stops being used.
_STRONG = ("title", "recap", "project", "sessionId")
_WEAK = ("you_said", "last_any", "opened")
_SEARCHED = _STRONG + _WEAK


# `entrypoint` says who started a session: "cli" is you at a terminal, "sdk-cli"
# and "sdk-py" are a program. Sampled 400 transcripts: 356 were programs. They are
# real sessions and stay in the index; they are just not what "find my session
# from last week" means.
AGENT_ENTRYPOINTS = ("sdk-cli", "sdk-py", "sdk-ts", "sdk")


def is_yours(entry):
    """Did a human start this one? Unknown counts as yours: the field is missing
    from older transcripts, which are exactly the ones this index is for."""
    return (entry.get("entrypoint") or "") not in AGENT_ENTRYPOINTS


def search(idx, query, live_ids=None, everything=False):
    """Entries matching every word, live ones first, then newest.

    Every word has to match somewhere in the entry - two words are how you narrow
    a month of sessions down to the one you mean. Ranking is live before ended,
    because a session you can still walk into beats one you would have to reopen.
    """
    words = [w for w in (query or "").lower().split() if w]
    if not words:
        return []
    live_ids = live_ids or set()
    hits = []
    for entry in idx.values():
        if not everything and not is_yours(entry):
            continue
        strong = " ".join(str(entry.get(f, "")) for f in _STRONG).lower()
        whole = strong + " " + " ".join(str(entry.get(f, "")) for f in _WEAK).lower()
        if not all(w in whole for w in words):
            continue
        where = 0 if all(w in strong for w in words) else 1
        hits.append(dict(entry, live=entry.get("sessionId") in live_ids, rank=where))
    hits.sort(key=lambda e: (not e["live"], e["rank"], _neg(e.get("last_ts", ""))))
    return hits


def _neg(ts):
    """Sort newest first on a string timestamp without reversing the whole key."""
    return tuple(-ord(c) for c in ts)
