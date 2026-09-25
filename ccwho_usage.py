"""Subscription usage per account, as each session's statusLine is handed it.

Claude Code gives a statusLine command `rate_limits` - the 5-hour and 7-day
windows of the account THAT session spends (measured 2026-09-25, 2.1.282: a
machine login and a CLAUDE_CODE_OAUTH_TOKEN session both get it, with different
numbers). ccwho never logs in and never calls the usage endpoint, which needs the
keychain token, 403s for setup-tokens and 429s when polled. It records what it is
handed, one file per session, and the list reads them back.

Three things are easy to get wrong, and each has a rule here:

- WHICH ACCOUNT. The harness spends an env token before the machine login, so a
  token session is the token's fingerprint (sha256, 16 hex - the token itself is
  never kept). A login session is the login in ITS config dir's .claude.json.
  A new token is a new account; nothing is ever merged.
- WHICH READING. Every session reports the numbers from its OWN last reply, so an
  idle session hands over an old 5% long after a busy one saw 60%. Receipt time
  orders nothing: the reading's measured time is its session's last reply in the
  transcript. Without one, usage in a window only rises, so the highest wins.
- AFTER THE RESET. A window past its resets_at is expired, not 0%: another
  session or machine may already have spent the new one.

Pure over its inputs apart from reading the transcript tail and .claude.json it
is pointed at. The runner owns stdin, the environment and every write.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import time

VERSION = 1
WINDOWS = ("five_hour", "seven_day")
SHORT = {"five_hour": "5h", "seven_day": "7d"}
KEEP_SECONDS = 8 * 86400          # the 7-day window, and a day of grace
SAME_WINDOW = 60                  # resets this close are one window, reported twice
TAIL_BYTES = 256 * 1024
# Session ids are UUIDs. \Z, not $: $ lets a trailing newline through.
_SID = re.compile(r"[0-9A-Za-z-]{1,64}\Z")
_READING = re.compile(r"[0-9A-Za-z-]{1,64}\.json\Z")
# What Claude Code writes after a /login inside the session. A plain string -
# the same words inside a tool call or its output are not a login.
_LOGIN_OK = "<local-command-stdout>Login successful"


def fingerprint(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def account_of(env, home):
    """The account a session spends, named without its credential."""
    token = env.get("CLAUDE_CODE_OAUTH_TOKEN") or ""
    if token:
        return {"kind": "token", "id": "token:" + fingerprint(token)}
    base = env.get("CLAUDE_CONFIG_DIR") or home
    try:
        with open(os.path.join(os.path.expanduser(base), ".claude.json")) as fh:
            acct = json.load(fh).get("oauthAccount") or {}
    except (OSError, ValueError, AttributeError):
        acct = {}
    uuid = acct.get("accountUuid") if isinstance(acct, dict) else None
    if not isinstance(uuid, str) or not uuid:
        return {"kind": "unknown", "id": None}
    email = acct.get("emailAddress")
    return {"kind": "login", "id": "login:" + uuid,
            "email": email if isinstance(email, str) else ""}


def _epoch(stamp):
    try:
        return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def transcript_facts(path, tail_bytes=TAIL_BYTES):
    """(time of the last reply, time of the last /login) from a transcript tail.

    The last reply is when this session's rate_limits were measured. Either is
    None when the transcript cannot be read or holds no such record.
    """
    if not isinstance(path, str) or not path:
        return None, None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - tail_bytes))
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None, None
    replied = login = None
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        when = _epoch(rec.get("timestamp"))
        if when is None:
            continue
        if rec.get("type") == "assistant":
            replied = when if replied is None else max(replied, when)
        elif rec.get("type") == "user":
            content = (rec.get("message") or {}).get("content") \
                if isinstance(rec.get("message"), dict) else None
            if isinstance(content, str) and content.startswith(_LOGIN_OK):
                login = when if login is None else max(login, when)
    return replied, login


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def clean_limits(rate_limits):
    """Only the known windows and their two numbers. None if nothing usable."""
    if not isinstance(rate_limits, dict):
        return None
    out = {}
    for name in WINDOWS:
        w = rate_limits.get(name)
        if not isinstance(w, dict) or not _number(w.get("used_percentage")):
            continue
        reset = w.get("resets_at")
        # An idle window is (0, null): a reading, not a missing one.
        out[name] = {"used_percentage": w["used_percentage"],
                     "resets_at": reset if _number(reset) else None}
    return out or None


def record(payload, env, home, now, previous=None):
    """What to store for one statusLine call, or None to store nothing."""
    if not isinstance(payload, dict):
        return None
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not _SID.match(sid):
        return None
    limits = clean_limits(payload.get("rate_limits"))
    if limits is None:
        return None
    acct = account_of(env, home)
    measured, login = transcript_facts(payload.get("transcript_path"))
    prev = previous if isinstance(previous, dict) else {}
    first = prev.get("first_account")
    first_seen = prev.get("first_seen")
    # An unknown account is never kept as the first one: .claude.json read
    # mid-write on the first call would make the session unsure for life.
    if not isinstance(first, dict) or not first.get("id") or not _number(first_seen):
        first, first_seen = acct, now
    old_login = prev.get("login_at")
    if _number(old_login):
        login = old_login if login is None else max(login, old_login)
    unsure = False
    # An unreadable login later is no switch: the session keeps its account.
    if acct.get("id") and acct["id"] != first.get("id"):
        # The login changed under an open session. Only a /login made INSIDE it,
        # after its first reading, says which account it spends now.
        if acct["kind"] == "login" and login is not None and login > first_seen:
            first, first_seen = acct, now
        else:
            unsure = True
    return {"v": VERSION, "session_id": sid, "received_at": now, "measured_at": measured,
            "rate_limits": limits, "account": acct, "first_account": first,
            "first_seen": first_seen, "login_at": login, "unsure": unsure}


def load_readings(directory, now):
    """Every usable reading on disk. Corrupt, foreign and stale files are skipped."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    out = []
    for name in names:
        if not _READING.match(name):
            continue
        try:
            with open(os.path.join(directory, name)) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or not _number(rec.get("received_at")):
            continue
        if now - rec["received_at"] > KEEP_SECONDS:
            continue
        out.append(rec)
    return out


def prune(directory, now):
    """Names in the usage dir untouched for longer than a reading is kept."""
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    out = []
    for name in names:
        try:
            if now - os.stat(os.path.join(directory, name)).st_mtime > KEEP_SECONDS:
                out.append(name)
        except OSError:
            pass
    return out


def _window(readings, name, now):
    """The current value of one window across one account's readings."""
    seen = [(r, r["rate_limits"][name]) for r in readings
            if isinstance(r.get("rate_limits"), dict)
            and isinstance(r["rate_limits"].get(name), dict)]
    if not seen:
        return None
    resets = [w["resets_at"] for _, w in seen if _number(w.get("resets_at"))]
    if resets:
        # A later reset is a later window, whatever its number.
        newest = max(resets)
        # An idle window (0, null) measured after that reset IS the new window.
        idle = [r for r, w in seen if w.get("resets_at") is None
                and _number(r.get("measured_at")) and r["measured_at"] > newest]
        if idle:
            last = max(idle, key=lambda r: r["measured_at"])
            return {"state": "ok", "pct": last["rate_limits"][name]["used_percentage"],
                    "resets_at": None}
        seen = [(r, w) for r, w in seen if _number(w.get("resets_at"))
                and newest - w["resets_at"] <= SAME_WINDOW]
    timed = [(r, w) for r, w in seen if _number(r.get("measured_at"))]
    if timed:
        _, w = max(timed, key=lambda rw: rw[0]["measured_at"])
    else:
        _, w = max(seen, key=lambda rw: rw[1]["used_percentage"])
    reset = w.get("resets_at")
    if _number(reset) and reset <= now:
        return {"state": "expired", "resets_at": reset}
    return {"state": "ok", "pct": w["used_percentage"], "resets_at": reset}


def accounts(readings, now, labels=None):
    """One row per account id: its windows, its sessions, and the reading's age."""
    labels = labels or {}
    groups = {}
    for r in readings:
        acct = r.get("first_account")
        if r.get("unsure") or not isinstance(acct, dict) or not acct.get("id"):
            continue
        groups.setdefault(acct["id"], (acct, []))[1].append(r)
    rows = []
    for aid, (acct, rs) in groups.items():
        when = max((r.get("measured_at") if _number(r.get("measured_at"))
                    else r["received_at"]) for r in rs)
        default = acct.get("email") or (
            "token " + aid.split(":", 1)[1][:8] if acct.get("kind") == "token" else aid)
        rows.append({"id": aid, "kind": acct.get("kind"), "email": acct.get("email", ""),
                     "label": labels.get(aid) or default,
                     "sessions": len({r.get("session_id") for r in rs}),
                     "age": now - when,
                     "five_hour": _window(rs, "five_hour", now),
                     "seven_day": _window(rs, "seven_day", now)})
    rows.sort(key=lambda row: row["age"])
    return rows


def resolve_id(query, ids):
    """An account id from an exact id, a unique prefix, or a unique id body."""
    if not query:
        return None
    if query in ids:
        return query
    hits = [i for i in ids if i.startswith(query) or i.split(":", 1)[-1].startswith(query)]
    return hits[0] if len(hits) == 1 else None


def _age(secs):
    secs = max(0, int(secs))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= size:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def _clock(epoch, now):
    t = time.localtime(epoch)
    if epoch - now < 86400:
        return time.strftime("%H:%M", t)
    return time.strftime("%a %H:%M", t)


def format_window(name, w, now):
    tag = SHORT[name]
    if not w:
        return f"{tag} —"
    if w["state"] == "expired":
        return f"{tag} expired (reset {_clock(w['resets_at'], now)})"
    pct = w["pct"]
    text = f"{tag} {pct:.0f}%" if _number(pct) else f"{tag} ?"
    if _number(w.get("resets_at")):
        text += f" ↻{_clock(w['resets_at'], now)}"
    return text


def format_row(row, now):
    parts = [row["label"]] + [format_window(n, row.get(n), now) for n in WINDOWS]
    sessions = row.get("sessions", 0)
    parts.append(f"{sessions} session{'s' if sessions != 1 else ''}")
    parts.append(f"{_age(row['age'])} ago")
    return "  ".join(parts)
