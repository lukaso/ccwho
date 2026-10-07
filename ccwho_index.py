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

import functools
import glob
import hashlib
import json
import os
import re
import tempfile
import threading

import ccwho_brief as brief
import ccwho_text

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
    # A temp file per writer: the list, a one-shot and the launchd save can all be
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


# ------------------------------------------------------------------ word forms

# The owner remembered "wake"; what was said was "it hasn't woken the loop"
# (2026-10-07). A word also finds its forms - forward only, from the word to its
# forms. The other way, from a form to its word, is where a form is another word:
# measured on the owner's 125 sessions, `settings` took "set" (+51 sessions),
# `coding` "Claude Code" (25 -> 102), `willing` "will" (7 -> 105), `said` "say".
# Type the word to find its forms; a form finds itself.
TYPED, FORM = 2, 1

# Irregular forms, each a whole word: "ran" inside "branch" is no run. Closed on
# purpose. Left out, each a common word of its own: won (won't), stuck, left, bit,
# led, saw, made, met, sat, shot, lit, fed, wore, tore - and go, do, see and get,
# whose forms are in nearly every session (review-plan1 F10).
_IRREGULAR_GROUPS = (
    "wake woke woken", "break broke broken", "choose chose chosen", "write wrote written",
    "freeze froze frozen", "speak spoke spoken", "steal stole stolen", "drive drove driven",
    "ride rode ridden", "rise rose risen", "hide hid hidden", "forget forgot forgotten",
    "begin began begun", "ring rang rung", "sing sang sung", "sink sank sunk",
    "swim swam swum", "throw threw thrown", "grow grew grown", "know knew known",
    "draw drew drawn", "fly flew flown", "blow blew blown", "shake shook shaken",
    "take took taken", "give gave given", "fall fell fallen", "eat ate eaten", "run ran",
    "find found", "send sent", "build built", "catch caught", "think thought",
    "bring brought", "buy bought", "teach taught", "seek sought", "fight fought",
    "tell told", "sell sold", "hold held", "keep kept", "sleep slept", "feel felt",
    "mean meant", "spend spent", "lose lost", "pay paid", "say said", "hang hung",
    "stand stood", "understand understood", "use used using uses", "try tries tried trying")

_LETTERS = re.compile(r"[a-z]+")
_TEXT_LETTERS = re.compile(r"[A-Za-z]+")


def _consonant(w, i):
    if w[i] in "aeiou":
        return False
    return w[i] != "y" or i == 0 or not _consonant(w, i - 1)


def _measure(stem):
    """Porter's m: the number of vowel-consonant runs in [C](VC)^m[V]."""
    runs = [_consonant(stem, i) for i in range(len(stem))]
    m, i = 0, 0
    while i < len(runs) and runs[i]:
        i += 1
    while i < len(runs):
        while i < len(runs) and not runs[i]:
            i += 1
        if i == len(runs):
            break
        while i < len(runs) and runs[i]:
            i += 1
        m += 1
    return m


def _vowel_in(stem):
    return any(not _consonant(stem, i) for i in range(len(stem)))


def _cvc(w):
    """Porter's *o: consonant, vowel, consonant - the last not w, x or y."""
    n = len(w)
    return (n >= 3 and _consonant(w, n - 3) and not _consonant(w, n - 2)
            and _consonant(w, n - 1) and w[-1] not in "wxy")


def _step1ab(w):
    """Porter's 1a and 1b - plurals, -ed and -ing: (the stem, whether one went)."""
    was = w
    if w.endswith("sses") or w.endswith("ies"):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    cut = False
    if w.endswith("eed"):
        if _measure(w[:-3]) > 0:
            w = w[:-1]
    elif w.endswith("ed") and _vowel_in(w[:-2]):
        w, cut = w[:-2], True
    elif w.endswith("ing") and _vowel_in(w[:-3]):
        w, cut = w[:-3], True
    if cut:
        if len(w) >= 2 and w[-1] == w[-2] and _consonant(w, len(w) - 1) and w[-1] not in "lsz":
            w = w[:-1]
        elif _measure(w) == 1 and _cvc(w):
            w += "e"
    return w, w != was


def _porter(w):
    """Porter's step 1 (1c as Porter2 has it) and 5a: plurals, -ed and -ing, a
    final y and a final e. Not the rest: -ation, -ness and the like make words of
    other meanings meet, and a search should not."""
    w, _ = _step1ab(w)
    if len(w) > 2 and w[-1] == "y" and _consonant(w, len(w) - 2):
        w = w[:-1] + "i"
    if w.endswith("e"):
        m = _measure(w[:-1])
        if m > 1 or (m == 1 and not _cvc(w[:-1])):
            w = w[:-1]
    return w


def _irregular():
    out = {}
    for group in _IRREGULAR_GROUPS:
        forms = group.split()
        base = _porter(forms[0])
        for form in forms:
            out[form] = base
    return out


IRREGULAR = _irregular()
_IRREGULAR_BASES = frozenset(g.split()[0] for g in _IRREGULAR_GROUPS)


# A run of letters longer than this is no word (the longest in the owner's said
# text, 2026-10-07: 43 letters): it is its own key - never stemmed, never kept in
# the memo. Porter's y-rule recurses once per "y", and a pasted blob of them
# raised and cost the grep every session (review-build3 F1).
LONGEST_WORD = 64


def word_key(token):
    """What a word and its forms have in common: wake, waking, woke -> "wake";
    merge, merged, merging -> "merg"."""
    return token if len(token) > LONGEST_WORD else _word_key(token)


@functools.lru_cache(maxsize=65536)
def _word_key(token):
    if token in IRREGULAR:
        return IRREGULAR[token]
    return _porter(token)


def _is_word(w):
    """A word itself, not one of its forms: no plural, -ed or -ing to take off,
    and no irregular form (woke, ran) - the base of a table group is a word."""
    if w in IRREGULAR:
        return w in _IRREGULAR_BASES
    return not _step1ab(w)[1]


class Word:
    """One word of a search, and how a text holds it: TYPED - inside it as you
    typed it (any part of a word, as always: `flam` finds flamingo) - or FORM:
    a word of the text is one of its forms (wake: wakes, waking, woke, woken).
    Forms only for an a-z word of three letters or more that is a word itself,
    not a form: `woken` finds "woken" and not "wake" (see TYPED, FORM). Nor
    for a word whose key is under three letters (age: ag; eye: ey): what its
    forms have in common is in too many words to look for."""

    def __init__(self, word):
        self.word = word
        self.key = (word_key(word) if 3 <= len(word) <= LONGEST_WORD
                    and _LETTERS.fullmatch(word) and _is_word(word) else None)
        # what every form contains, so a text without any is passed over
        # unsplit: the key (merging: merg); the key less its e (waking: wak);
        # the table's forms (woke). The word itself is TYPED before this is asked.
        pats = set()
        if self.key is not None:
            pats = {self.key, self.key[:-1] if self.key.endswith("e") else self.key}
            pats |= {f for f, k in IRREGULAR.items() if k == self.key}
        self.patterns = tuple(p for p in pats if len(p) >= 3)

    def formed_by(self, token):
        return word_key(token) == self.key

    def match(self, low):
        """TYPED, FORM or None: how `low` (lowercased) holds this word."""
        if self.word in low:
            return TYPED
        if self.key is None or not any(p in low for p in self.patterns):
            return None
        if any(self.formed_by(t) for t in set(_LETTERS.findall(low))):
            return FORM
        return None

    def where(self, text):
        """Where in `text` (any case) this word is first held - typed or by a
        form - or -1."""
        at = text.lower().find(self.word)
        if self.key is not None:
            for m in _TEXT_LETTERS.finditer(text):
                if 0 <= at <= m.start():
                    break
                if self.formed_by(m.group().lower()):
                    return m.start()
        return at


def _words(query):
    """The distinct words of a search, in order: "wake wake" is "wake"."""
    return [Word(w) for w in dict.fromkeys(w for w in (query or "").lower().split() if w)]


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
    a month of sessions down to the one you mean - as typed or by a form (Word).
    Ranking is live before ended, because a session you can still walk into
    beats one you would have to reopen.
    """
    words = _words(query)
    if not words:
        return []
    live_ids = live_ids or set()
    hits = []
    for entry in idx.values():
        if not everything and not is_yours(entry):
            continue
        strong = " ".join(str(entry.get(f, "")) for f in _STRONG).lower()
        whole = strong + " " + " ".join(str(entry.get(f, "")) for f in _WEAK).lower()
        if not all(w.match(whole) for w in words):
            continue
        where = 0 if all(w.match(strong) for w in words) else 1
        hits.append(dict(entry, live=entry.get("sessionId") in live_ids, rank=where))
    hits.sort(key=lambda e: (not e["live"], e["rank"], _neg(e.get("last_ts", ""))))
    return hits


def _neg(ts):
    """Sort newest first on a string timestamp without reversing the whole key."""
    return tuple(-ord(c) for c in ts)


def grep(idx, query, stop=None, everything=False, store=None):
    """What each session said that holds every word of `query`: {sessionId:
    Said}, for the sessions in which every word was said - in your prompts or
    Claude's replies, in any case, as typed or by a form (Word). Not what Claude
    Code stores in a transcript (skills, CLAUDE.md, reminders), nor what a tool
    printed, nor the JSON around it: those are in every session, and a plain grep
    of the file found a common word in all of them (review 1). Only your
    sessions, as search() has it, unless `everything`.

    Said is the session's one message that says most of what you typed:
    {"text", "who" ("you" | "claude"), "ts", "key"}, key = (how many of the words
    of three letters or more it holds, yours, how many of those as typed) - the
    list ranks by it. "on" or "pr" must be said, but is inside almost every long
    message and ranks nothing (review-plan1 F1). The first of equals.

    `store` keeps what was said between searches (SaidStore): the list keeps one
    for its life, and a search is then a scan of what was said - measured
    2026-10-07 over the owner's 125 sessions: 0.04-0.26 s, against 1.5-3 s for
    a grep of the 1.2 GB of transcripts. A new one is read whole: 5-8 s.
    `stop()`, asked before each session, each line read and every 200 messages,
    ends it early with None: a newer search has started."""
    words = _words(query)
    if not words:
        return {}
    store = store if store is not None else SaidStore()
    ranked = [w for w in words if len(w.word) >= 3] or words
    found = {}
    for entry in list((idx or {}).values()):
        if not everything and not is_yours(entry):
            continue
        if stop and stop():
            return None
        if not entry.get("path"):
            continue
        messages = store.messages(entry["path"], stop)
        if messages is None:
            return None
        said = _best(messages, words, ranked, stop)
        if said is _STOPPED:
            return None
        if said:
            found[entry.get("sessionId")] = said
    # a stopped grep forgets nothing: it may not have seen every transcript
    store.prune({e.get("path") for e in (idx or {}).values() if e.get("path")})
    return found


_STOPPED = object()


def _best(messages, words, ranked, stop):
    """The Said of one session, None when a word was never said, _STOPPED."""
    seen, best = set(), None
    for i, (yours, ts, text) in enumerate(messages):
        if stop and i % 200 == 0 and stop():
            return _STOPPED
        low = text.lower()
        held = {}
        for w in words:
            how = w.match(low)
            if how:
                held[w] = how
        if not held:
            continue
        seen.update(held)
        key = (sum(1 for w in ranked if w in held), yours,
               sum(1 for w in ranked if held.get(w) == TYPED))
        if best is None or key > best[0]:
            best = (key, yours, ts, text, held)
    if best is None or len(seen) < len(words):
        return None
    key, yours, ts, text, held = best
    # where the text starts: a word that ranks - "on" is inside "Long" and
    # "content", and the word that matters was cut off (review-build1 F2)
    shown = [w for w in held if w in ranked]
    return {"text": _shown(text, shown), "who": "you" if yours else "claude", "ts": ts,
            "key": key}


# How much of what comes before the first word a shown message keeps: enough
# to read it as a sentence, little enough that the word is on a row.
LEAD = 30


def _shown(text, held):
    """The message from a little before the first word it holds, `…` where it
    was cut, at most TEXT_CAP."""
    at = min((p for p in (w.where(text) for w in held) if p >= 0), default=0)
    start = max(0, at - LEAD)
    if start:
        space = text.find(" ", start, at)
        start = space + 1 if space >= 0 else start
    return _clip(("\u2026" if start else "") + text[start:], TEXT_CAP)


def source_key(paths=None):
    """A hash of the bytes of `paths` (SOURCES): the same code, the same key."""
    digest = hashlib.sha1()
    for path in SOURCES if paths is None else paths:
        try:
            with open(path, "rb") as fh:
                digest.update(fh.read())
        except OSError:
            digest.update(b"unreadable " + str(path).encode())
    return digest.hexdigest()


# The code that says what was said, and so what a SaidStore holds: this module
# (_said, _typed, the store), ccwho_text (plain_text) and ccwho_brief (what a
# human prompt is). Not ccwho_procs: brief uses it for tool calls, never for
# what was said. SOURCE is the code as this module was loaded - a reload loads
# it again - not the files as they are now: a store made by old code must not
# carry the key of an edit not loaded yet. Left open on purpose (review-build6
# F1): an edit to ccwho_brief or ccwho_text that lands in the few to tens of ms
# between their reload and this one's is in SOURCE and not in the code - about
# 0.1% of landings; the store is right again at the next edit to one of SOURCES,
# or a restart. Closing it costs a disk read in ccwho_brief, which is pure, or
# a hash of the bytes _load_beside compiles - more code than the window is worth.
SOURCES = (__file__, ccwho_text.__file__, brief.__file__)
SOURCE = source_key()


class SaidStore:
    """What was said in each transcript, kept: read once, then only what was
    appended - as the index is - so a search reads what was said (13 MB for
    the owner's 125 sessions, 2026-10-07), not 1.2 GB of transcripts.

    `files`: path -> {"inode", "offset", "messages"}, one message per record
    _said gives text for: (yours, ts, text), the text as a terminal shows it,
    one line. `parsed` counts the lines parsed. A store is shared by the two
    greps the list may run at once: a lock. A last line is read once it ends
    with a newline, as the index reads it: one Claude Code never ended is not
    searched (none of 127 real transcripts, 2026-10-07).

    `source`: the code that made it (SOURCE). The list keeps its store across
    the engine reload of every scan while that is the code it runs
    (review-build1 F1); after an edit to it, a search reads into a new store
    (review-build5)."""

    def __init__(self):
        self.source = SOURCE
        self.files = {}
        self.parsed = 0
        self._lock = threading.Lock()

    def prune(self, paths):
        """Forget every transcript not in `paths`: Claude Code deletes them."""
        with self._lock:
            for gone in set(self.files) - set(paths):
                del self.files[gone]

    def messages(self, path, stop=None):
        """The messages of `path`, brought up to date - or None when `stop()`
        said so, and then nothing read is kept. A new inode, or a file shorter
        than what was read, is read again from the start; a last line not ended
        yet is read when it is."""
        try:
            st = os.stat(path)
        except OSError:
            return []
        with self._lock:
            have = self.files.get(path)
            if not have or have["inode"] != st.st_ino or st.st_size < have["offset"]:
                have = {"inode": st.st_ino, "offset": 0, "messages": []}
            offset, new = have["offset"], []
            try:
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    for line in fh:
                        if not line.endswith(b"\n"):
                            break               # not ended: read it when it is
                        if stop and stop():
                            return None
                        offset += len(line)
                        said = self._said(line)
                        if said:
                            new.append(said)
            except OSError:
                return have["messages"] if path in self.files else []
            have = {"inode": st.st_ino, "offset": offset, "messages": have["messages"] + new}
            self.files[path] = have
            return have["messages"]

    # A tool's output: most of a transcript's bytes, and never said. Its record
    # is parsed only if it may hold a text block too (none did in 8,966 measured,
    # but one that does is said).
    _TOOL = b'"tool_result"'
    _TEXT = re.compile(rb'"type": ?"text"')

    def _said(self, line):
        if self._TOOL in line and not self._TEXT.search(line):
            return None
        self.parsed += 1
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            return None                 # a torn or foreign line is not data
        try:
            text = _said(rec)
        except Exception:
            return None                 # a record of no known shape
        text = " ".join(ccwho_text.plain_text(text).split()) if text else ""
        if not text:
            return None
        return (rec.get("type") != "assistant", str(rec.get("timestamp") or ""), text)


def _said(rec):
    """What a person typed or Claude wrote in this record: a prompt - typed at
    the prompt, or while Claude worked (queued) - or the text of a reply. Not
    what the harness put there: a tool's result, a reminder, an interrupt, a
    record it marks meta, the tags around a slash command (review 2). "" for
    anything else."""
    if not isinstance(rec, dict) or rec.get("isMeta"):
        return ""
    if rec.get("type") == "attachment":
        queued = rec.get("attachment")
        if (isinstance(queued, dict) and queued.get("type") == "queued_command"
                and (queued.get("origin") or {}).get("kind") == "human"):
            return _typed(queued.get("prompt"))
        return ""
    if rec.get("type") not in ("user", "assistant"):
        return ""
    message = rec.get("message")
    if not isinstance(message, dict) or message.get("model") == "<synthetic>":
        return ""                   # a reply the harness wrote: "No response requested."
    text = brief._text_of(message.get("content"))
    return _typed(text) if rec["type"] == "user" else text


INTERRUPTED = "[Request interrupted by user"


def _typed(text):
    """A prompt as the person typed it - a slash command as `/review the gate`,
    not its tags - or "" when the harness wrote it."""
    if not isinstance(text, str) or not brief.is_human_prompt(text):
        return ""
    if text.strip().startswith(INTERRUPTED):
        return ""
    return brief.command_of(text) or text
