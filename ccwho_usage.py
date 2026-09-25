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

import ccwho_text

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
        # Only ccwho writes these, but a file on disk is not a promise: a bad
        # window is dropped here, so nothing downstream - the list - can trip on it.
        limits = clean_limits(rec.get("rate_limits"))
        if limits is None or not isinstance(rec.get("first_account"), dict):
            continue
        out.append(dict(rec, rate_limits=limits))
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
                    "resets_at": None, "age": now - last["measured_at"]}
        seen = [(r, w) for r, w in seen if _number(w.get("resets_at"))
                and newest - w["resets_at"] <= SAME_WINDOW]
    timed = [(r, w) for r, w in seen if _number(r.get("measured_at"))]
    if timed:
        r, w = max(timed, key=lambda rw: rw[0]["measured_at"])
    else:
        r, w = max(seen, key=lambda rw: rw[1]["used_percentage"])
    # each window keeps its own age: a fresh 5h must not vouch for an old 7d
    age = now - (r["measured_at"] if _number(r.get("measured_at")) else r["received_at"])
    reset = w.get("resets_at")
    if _number(reset) and reset <= now:
        return {"state": "expired", "resets_at": reset, "age": age}
    return {"state": "ok", "pct": w["used_percentage"], "resets_at": reset, "age": age}


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
                     "brand": "ant",       # Claude; Codex ("oai") arrives with #13
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


# ------------------------------------------------------------ what the list shows
# One line per account a live session spends (owner, 2026-09-25: all of them, no
# cap), in a fixed order, built as (text, style) spans. Cutting happens on the
# text; colour is added last (to_ansi, or Textual in the list), so a cut can
# never split an escape code and leak colour into the session rows below.

WINDOW_SECONDS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
STYLES = ("dim", "plain", "green", "red", "yellow", "byellow")
ANSI_CODES = {"dim": "\033[2m", "plain": "", "green": "\033[1;32m", "red": "\033[1;31m",
              "yellow": "\033[33m", "byellow": "\033[1;33m"}
ANSI_RE = ccwho_text.ANSI
STALE_AFTER = 15 * 60             # a window older than this says how old
LEAD, INDENT, SEP = "usage  ", "       ", "  │  "
EMPTY = {"not_set_up": "not set up - ccwho setup adds it",
         "waiting": "waiting - sessions report after their next reply",
         "unknown": "unknown"}


def elapsed_pct(name, resets_at, now):
    """How much of the window is gone, 0-100; None without a reset time."""
    span = WINDOW_SECONDS.get(name)
    if not span or not _number(resets_at):
        return None
    return max(0, min(100, round(100 * (1 - (resets_at - now) / span))))


def pace_arrow(used, elapsed):
    """Faster than time passes: red up. At or below it: green down."""
    if elapsed is None or not _number(used):
        return None
    # the numbers as shown: 60.4 prints as 60%, and 60%/60% is on pace
    return ("↑", "red") if int(f"{used:.0f}") > elapsed else ("↓", "green")


def used_style(pct, base):
    return "byellow" if pct >= 95 else "yellow" if pct >= 80 else base


def _base_name(row):
    if row.get("kind") == "token":
        return row["id"].split(":", 1)[1][:4]
    email = row.get("email") or ""
    return email.split("@", 1)[0] if email else row["id"]


def _longer(row, step):
    """The same account named longer, step by step, until names differ."""
    if row.get("kind") == "token":
        return row["id"].split(":", 1)[1][:4 + 2 * step]
    email = row.get("email") or ""
    if "@" not in email:
        return row["id"]
    local, domain = email.split("@", 1)
    return f"{local}@{domain.split('.', 1)[0]}" if step == 1 else email


def short_names(rows, labels):
    """id -> the shortest name that still names one account."""
    names = {r["id"]: _base_name(r) for r in rows}
    for step in range(1, 9):
        seen = {}
        for aid, n in names.items():
            seen.setdefault(n, []).append(aid)
        clash = [aid for group in seen.values() if len(group) > 1 for aid in group]
        if not clash:
            break
        by_id = {r["id"]: r for r in rows}
        for aid in clash:
            names[aid] = _longer(by_id[aid], step)
    for aid, n in list(names.items()):
        if len([a for a in names if names[a] == n]) > 1:
            names[aid] = aid          # past every step: the id itself is unique
    out = {}
    label_count = {}
    for aid in names:
        lab = labels.get(aid)
        if lab:
            label_count[lab] = label_count.get(lab, 0) + 1
    for aid, n in names.items():
        lab = labels.get(aid)
        out[aid] = (f"{lab} {n}" if label_count[lab] > 1 else lab) if lab else n
    # a label can equal another account's own name: the labelled one gives way
    for _ in range(2):
        seen = {}
        for aid, n in out.items():
            seen.setdefault(n, []).append(aid)
        clash = [aid for group in seen.values() if len(group) > 1 for aid in group]
        if not clash:
            break
        for aid in clash:
            if labels.get(aid) and out[aid] == labels[aid]:
                out[aid] = f"{labels[aid]} {names[aid]}"
            elif _ == 1:
                out[aid] = aid
    return out


def ordered(rows, names):
    """A fixed order, so an entry does not move when another account replies."""
    kind = {"login": 0, "token": 1}
    return sorted(rows, key=lambda r: (r.get("brand", ""), kind.get(r.get("kind"), 2),
                                       names.get(r["id"], r["id"])))


def session_accounts(readings):
    """session id -> the account it spends; unsure and unknown sessions: none."""
    out = {}
    for r in readings:
        acct = r.get("first_account") or {}
        if not r.get("unsure") and acct.get("id") and isinstance(r.get("session_id"), str):
            out[r["session_id"]] = acct["id"]
    return out


def snapshot(readings, now, live_ids, facts, labels=None):
    """What the list needs about usage, built once per collect."""
    labels = labels or {}
    rows = accounts(readings, now, labels)
    spent = session_accounts(readings)
    live_ids = set(live_ids or ())
    sessions = {sid: aid for sid, aid in spent.items() if sid in live_ids}
    live = [r for r in rows if r["id"] in set(sessions.values())]
    if live:
        state = "ok"
    else:
        # facts may be a callable: reading settings is only needed without data
        facts = facts() if callable(facts) else facts
        roots = (facts or {}).get("usage_roots") or []
        if any(r.get("state") == "ours" for r in roots):
            state = "waiting"
        elif roots and all(r.get("opted_out") for r in roots):
            state = "off"
        else:
            state = "not_set_up"
    names = short_names(live, labels)
    return {"state": state, "accounts": ordered(live, names), "names": names,
            "sessions": sessions, "now": now}


def _reset_label(epoch, now):
    t = time.localtime(epoch)
    return time.strftime("%H:%M" if epoch - now < 86400 else "%a", t)


def _window_spans(name, w, now, base, resets=True, age=True):
    tag = SHORT[name]
    if w["state"] == "expired":
        out = [(f"{tag} expired", base)]
        if resets:
            out.append((f" ↻{_reset_label(w['resets_at'], now)}", base))
        return out
    pct = w["pct"]
    out = [(f"{tag} ", base), (f"{pct:.0f}%", used_style(pct, base))]
    el = elapsed_pct(name, w.get("resets_at"), now)
    arrow = pace_arrow(pct, el)
    if arrow:
        out += [arrow, (f"/{el}%", base)]
    if resets and _number(w.get("resets_at")):
        out.append((f" ↻{_reset_label(w['resets_at'], now)}", base))
    if age and _number(w.get("age")) and w["age"] > STALE_AFTER:
        out.append((f" ({_age(w['age'])} ago)", base))
    return out


def entry_spans(row, name, now, selected=False, brand=True, resets=True, age=True):
    base = "plain" if selected else "dim"
    out = [((f"{row.get('brand', 'ant')} " if brand else "") + name, base)]
    first = True
    for wname in WINDOWS:
        w = row.get(wname)
        if not w:
            continue
        out.append((" " if first else " · ", base))
        out += _window_spans(wname, w, now, base, resets=resets, age=age)
        first = False
    return out


def span_cells(spans):
    return sum(ccwho_text.cells(t) for t, _ in spans)


def cut_spans(spans, width):
    """At most `width` cells, `…` marking a cut; styles never split."""
    if span_cells(spans) <= width:
        return list(spans)
    out, room = [], max(0, width - 1)
    for t, st in spans:
        if room <= 0:
            break
        piece = t if ccwho_text.cells(t) <= room else ccwho_text.cut(t, room + 1)[:-1]
        if piece:
            out.append((piece, st))
            room -= ccwho_text.cells(piece)
        if piece != t:
            break
    return out + [("…", "dim")] if width > 0 else []


def usage_lines(snap, width, selected=None):
    """The usage line(s) under the header, as span lists; [] for none."""
    if not isinstance(snap, dict):
        return [[(LEAD + EMPTY["unknown"], "dim")]]
    state = snap.get("state")
    if state == "off":
        return []
    if state != "ok":
        return [cut_spans([(LEAD + EMPTY.get(state, EMPTY["unknown"]), "dim")], width)]
    now, names = snap.get("now", time.time()), snap.get("names", {})
    rows = snap.get("accounts", [])
    full = [entry_spans(r, names.get(r["id"], r["id"]), now, selected=r["id"] == selected)
            for r in rows]
    one = [(LEAD, "dim")]
    for i, e in enumerate(full):
        one += ([(SEP, "dim")] if i else []) + e
    if span_cells(one) <= width:
        return [one]
    lines = []
    for i, r in enumerate(rows):
        lead = [(LEAD if i == 0 else INDENT, "dim")]
        name, sel = names.get(r["id"], r["id"]), r["id"] == selected
        tries = [dict(), dict(resets=False), dict(resets=False, age=False),
                 dict(resets=False, age=False, brand=False)]
        for opts in tries:
            line = lead + entry_spans(r, name, now, selected=sel, **opts)
            if span_cells(line) <= width:
                break
        lines.append(cut_spans(line, width))
    return lines


def plain(lines):
    return ["".join(t for t, _ in line) for line in lines]


def to_ansi(spans):
    out = []
    for t, st in spans:
        code = ANSI_CODES.get(st, "")
        out.append(f"{code}{t}\033[0m" if code else t)
    return "".join(out)


def row_tag(snap, session_id):
    """The account a row spends, shortly; "" while only one account is seen."""
    if not isinstance(snap, dict) or snap.get("state") != "ok":
        return ""
    rows = snap.get("accounts", [])
    if len(rows) < 2:
        return ""
    aid = snap.get("sessions", {}).get(session_id)
    by_id = {r["id"]: r for r in rows}
    if aid not in by_id:
        return "?"
    name = snap.get("names", {}).get(aid, aid)
    brands = {r.get("brand") for r in rows}
    return f"{by_id[aid].get('brand', 'ant')} {name}" if len(brands) > 1 else name


def status_text(rec, now, labels=None, names=None):
    """What `ccwho statusline` prints in the session's own status bar."""
    if not isinstance(rec, dict):
        return ""
    limits = clean_limits(rec.get("rate_limits"))
    if not limits:
        return ""
    acct = rec.get("first_account") if isinstance(rec.get("first_account"), dict) else {}
    when = rec["measured_at"] if _number(rec.get("measured_at")) else rec.get("received_at", now)
    row = {"id": acct.get("id") or "?", "kind": acct.get("kind"), "email": acct.get("email", ""),
           "brand": "ant"}
    for name, w in limits.items():
        reset = w.get("resets_at")
        row[name] = ({"state": "expired", "resets_at": reset, "age": now - when}
                     if _number(reset) and reset <= now else
                     {"state": "ok", "pct": w["used_percentage"], "resets_at": reset,
                      "age": now - when})
    if rec.get("unsure") or not acct.get("id"):
        name = "?"
    elif names and isinstance(names.get(row["id"]), str):
        name = names[row["id"]]       # the name the list used, clashes resolved
    else:
        name = short_names([row], labels or {})[row["id"]]
    # Claude is what a status bar is assumed to show: a brand is named there
    # only when it is not Anthropic (owner, design review of the built list)
    return to_ansi(entry_spans(row, name, now, selected=True, brand=row["brand"] != "ant"))


def shown_records(snap):
    """What the usage line said, per account and window - for the log that
    decides when an old window should lose its arrow (owner, 2026-09-25)."""
    if not isinstance(snap, dict) or snap.get("state") != "ok":
        return []
    now, out = snap.get("now", time.time()), []
    for r in snap.get("accounts", []):
        for name in WINDOWS:
            w = r.get(name)
            if not w:
                continue
            el = elapsed_pct(name, w.get("resets_at"), now) if w["state"] == "ok" else None
            arrow = pace_arrow(w.get("pct"), el) if w["state"] == "ok" else None
            out.append({"account": snap.get("names", {}).get(r["id"], r["id"]),
                        "window": SHORT[name], "state": w["state"], "used": w.get("pct"),
                        "elapsed": el, "age": round(w.get("age") or 0),
                        "arrow": arrow[0] if arrow else ""})
    return out


SHOWN_LINE_BYTES = 100            # a shown-log line is about this long


def append_shown(path, records, last, now=None, keep=5000):
    """Append what changed; keep the file bounded. Returns the new key. A write
    that fails is dropped: this log must never cost the list anything.

    The last key is kept beside the log, so the list, --watch and `ccwho ls`
    (separate processes) do not each write the same lines. Appending never
    reads the log; only a log past its size bound is read, once, to trim it."""
    key = json.dumps([{k: v for k, v in r.items() if k != "age"} for r in records],
                     sort_keys=True)
    if not records:
        return key
    # the file, not this process's memory: the list and --watch each run for
    # hours, and a change one of them logged is not news to the other
    try:
        with open(path + ".key", encoding="utf-8") as fh:
            last = fh.read()
    except OSError:
        pass
    if key == last:
        return key
    now = time.time() if now is None else now
    try:
        with open(path, "a", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(dict(r, t=round(now))) + "\n")
        tmp = f"{path}.key.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(key)
        os.replace(tmp, path + ".key")         # a reader never sees half a key
        if os.path.getsize(path) > 2 * keep * SHOWN_LINE_BYTES:
            with open(path, encoding="utf-8") as fh:
                lines = fh.readlines()[-keep:]
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(lines)
            os.replace(tmp, path)
    except OSError:
        pass
    return key
