"""The directory between sessions: what each session is for, as another session
reads it. A message goes by Claude Code's own SendMessage, to a session's name;
ccwho only says who is there.

Two kinds of line, in one small file each under ~/.ccwho/directory:
- purpose: what a role is for. Kept by the exact name you gave the session (a name
  stays over resume, another account and /clear; a session id does not), and
  changed only by /purpose.
- now: what a session works on. Kept by session id and written by the session
  itself, so it goes at /clear with the work it was about.

Rules and reads only: ccwho.py writes, with write_atomic. Each file is written
whole and has one writer, so there is no lock.
"""
import collections
import hashlib
import json
import math
import os
import time

import ccwho_procs as procs
from ccwho_setup import age_words
from ccwho_text import plain_text

KINDS = ("purpose", "now")
MAX_TEXT = 120              # text from one session lands in another's context
MAX_TITLE = 80
MAX_LINE = 190              # a model reads this list: its size is a cost on each lookup
NOW_KEPT = 30 * 86400       # a purpose stays: a role can sleep for a year
NOT_RUNNING = "not running"
UNKNOWN = "unknown (session list incomplete)"


def clean(text):
    """`text` as one printable line, MAX_TEXT at most. "" is no text."""
    one_line = " ".join(plain_text(str(text)).split())
    return procs.printable(one_line)[:MAX_TEXT].rstrip()


def path_of(folder, kind, key):
    """A hash of the exact key: a name can hold anything, and two spellings are two
    addresses, so nothing is folded."""
    digest = hashlib.sha256(key.encode("utf-8", "surrogateescape")).hexdigest()
    return os.path.join(folder, f"{kind}-{digest}.json")


def entry_text(key, text, updated):
    return json.dumps({"key": key, "text": text, "updated": updated})


def _entry(path):
    """The entry in a file, or None: a bad file costs its own line only."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    key, text, updated = data.get("key"), data.get("text"), data.get("updated")
    if not isinstance(key, str) or not isinstance(text, str):
        return None
    if isinstance(updated, bool) or not isinstance(updated, (int, float)) or not math.isfinite(updated):
        return None
    text = clean(text)
    return {"key": key, "text": text, "updated": updated} if text else None


def read_one(folder, kind, key):
    return _entry(path_of(folder, kind, key))


def read_all(folder):
    """{"purpose": {name: entry}, "now": {session id: entry}}"""
    out = {kind: {} for kind in KINDS}
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return out
    for name in names:
        kind = name.partition("-")[0]
        if kind in out:
            got = _entry(os.path.join(folder, name))
            if got:
                out[kind][got["key"]] = got
    return out


def prune_now(folder, now):
    """Take now lines older than NOW_KEPT. Housekeeping: it never fails."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.startswith("now-") and name.endswith(".json"):
            path = os.path.join(folder, name)
            try:
                if now - os.path.getmtime(path) > NOW_KEPT:
                    os.remove(path)
            except OSError:
                pass


def _is_role(row):
    """Only a name you gave counts: a derived name changes at each resume."""
    return row.get("nameSource") == "user"


def _line(handle, state, purpose="", now_entry=None, now=0.0, row=None, you=False, shared=1):
    row = row or {}
    return {"handle": clean(handle or "?"), "state": clean(state or "?"),
            "cache": clean(row.get("cache") or ""), "purpose": purpose,
            "now": now_entry["text"] if now_entry else "",
            "now_age": age_words(max(0.0, now - now_entry["updated"])) if now_entry else "",
            "title": clean(row.get("title") or row.get("topic") or ""),
            "you": you, "shared": shared}


def merge(rows, entries, handle_of, me=None, complete=True, now=None):
    """The rows and the stored lines as {"named", "others", "complete", "reminder"}.
    `handle_of`: engine.session_handle (the engine imports this module). `me`: the
    caller's session id, None for a person. `complete` False: a role not seen may
    be running, so it is never called not running."""
    now = time.time() if now is None else now
    purposes, now_lines = entries["purpose"], entries["now"]
    live = collections.Counter(r.get("name") for r in rows if _is_role(r))
    named, others, reminder = [], [], None
    for r in rows:
        you = r.get("sessionId") == me
        now_entry = now_lines.get(r.get("sessionId"))
        if _is_role(r):
            purpose = purposes.get(r.get("name"))
            named.append(_line(handle_of(r), r.get("status"), purpose["text"] if purpose else "",
                               now_entry, now, r, you, live[r.get("name")]))
            if you and not purpose:
                reminder = clean(r.get("name"))
        else:
            others.append(_line(handle_of(r), r.get("status"), "", now_entry, now, r, you))
    for name in sorted(n for n in purposes if n not in live):
        named.append(_line(name, NOT_RUNNING if complete else UNKNOWN, purposes[name]["text"]))
    return {"named": named, "others": others, "complete": bool(complete), "reminder": reminder}


def matching(merged, words):
    """The lines that have every word, as `ccwho ls` matches words: any case."""
    words = [w.casefold() for w in words]

    def fits(line):
        text = " ".join((line["handle"], line["purpose"], line["now"], line["title"])).casefold()
        return all(w in text for w in words)
    return dict(merged, named=[x for x in merged["named"] if fits(x)],
                others=[x for x in merged["others"] if fits(x)])


def _head(line, widths, text):
    handle = line["handle"] + (" (you)" if line["you"] else "")
    parts = (handle.ljust(widths[0]), line["state"].ljust(widths[1]), line["cache"].ljust(widths[2]), text)
    return ("  " + "  ".join(parts).rstrip())[:MAX_LINE]


def _widths(lines):
    return (max(len(x["handle"]) + (6 if x["you"] else 0) for x in lines),
            max(len(x["state"]) for x in lines), max(len(x["cache"]) for x in lines))


def _dated(line):
    return f"{line['now']} ({line['now_age']})" if line["now"] else line["title"][:MAX_TITLE]


def render(merged):
    """The directory as text, for a model to read and a person to check."""
    named, others = merged["named"], merged["others"]
    if not named and not others:
        return "no sessions"
    out = []
    if named:
        out.append("NAMED")
        widths = _widths(named)
        for x in named:
            out.append(_head(x, widths, x["purpose"] or "no purpose yet"))
            if x["now"] or x["title"]:
                out.append(f"    now: {_dated(x)}"[:MAX_LINE])
            if x["shared"] > 1:
                out.append(f"    {x['shared']} sessions have this name: ListAgents shows each one's [ref]")
    if others:
        out.append("OTHER SESSIONS")
        widths = _widths(others)
        out += [_head(x, widths, _dated(x)) for x in others]
    if merged["reminder"]:
        out += ["", f"This session is {merged['reminder']} and has no purpose yet."
                    " Suggest one line, and ask the person to run /purpose with it."]
    return "\n".join(out + ["", "To ask one, use SendMessage with its name."])
