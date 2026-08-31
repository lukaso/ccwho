# ccwho

Which Claude Code session needs you, and what is it about.

```
16 sessions: 2 waiting · 2 busy · 12 idle
16 detached processes under your home on PID 1

liveapp       NEEDS YOU   liveapp-4e     let's fix 1-3. 4 doesn't need a fix.        4d
liveapp       busy        liveapp-06     yes, let's do that. create a directory…    23h
chiefofstaff  idle        chiefofstaff-08  commit and push                          10d
```

No daemon, no hooks, no tmux, no config. One command, ~0.8s over 16 sessions.
Stdlib Python only. Reads only what Claude Code already writes to disk.

## Why

Agent view and every third-party fleet tool show you a session **name**. When your
sessions are auto-named `liveapp-4e`, `liveapp-b3`, `liveapp-cd` — two of them
sometimes identical — the name tells you nothing, and switching tools does not fix
it because it is a data problem, not a display problem.

`ccwho` shows the **last thing you actually said** to each session, pulled from its
transcript. That is the column no tool has.

## Sources

| What | Where |
|---|---|
| session list, status, `waitingFor` | `claude agents --json` |
| the topic | `~/.claude/projects/*/<sessionId>.jsonl` (head + tail read only) |
| detached work | `ps`, PID-1 orphans matched by sessionId in the cmdline |

`sessionId` from the agents feed **is** the transcript filename, which is what makes
the topic column possible.

## Detached work

Claude Code's Bash tool tracks background jobs and re-invokes the session when they
exit. Work launched with `&`, `nohup` or `disown` is not tracked: the child is
reparented to PID 1, and the session reports `idle` while the work runs on.

`ccwho` matches PID-1 orphans against each sessionId (scratchpad paths carry it) and
flags the session `+N detached`. That is the blind spot the status column cannot see.

## ccgate - stop every session gating at once

N sessions each decide to run the deploy gate. All N start together, the box
thrashes, several fail on timing, and then every session sits waiting for the
machine to quieten - having burned the work twice.

`ccgate` puts a slot semaphore and a load ceiling in front of the command:

```sh
ccgate -- bash scripts/check.sh                       # 2 slots, load ceiling cores*0.75
ccgate --slots 3 --label "liveapp gate" -- bash scripts/check.sh
ccgate --status                                       # who holds a slot, and the load
```

Slots are files claimed with `O_CREAT|O_EXCL` under `~/.ccwho/gates/`, so it works
across unrelated processes with no daemon. A slot whose holder died is reclaimed.
Waiters jitter their retry so they don't wake in lockstep, and announce who is
ahead of them. `ccwho` shows held slots in its header.

Measured: 5 concurrent gates against 2 slots never exceeded 2 at once, all 5 ran,
and every slot was released.

To adopt it without changing habits, point the repo's gate command at it - one line
in the project's CLAUDE.md:

    Run the gate as `ccgate -- bash scripts/check.sh`, never bare.

Env: `CCGATE_SLOTS`, `CCGATE_LOAD_FACTOR` (0 disables the ceiling), `CCGATE_DIR`.

## Finding the window

Sessions get lost, not ended. A session can look gone while its process is alive,
attached to a terminal you cannot find among thirty windows - and the title is no
help, because Claude Code renames sessions as the work moves on (one here went
`Restart from disk` -> `update-landing-page-whatsapp-faq`). Only `sessionId` is
stable.

So every row carries its **tty**, and:

```sh
ccwho jump s032           # focus that window
ccwho jump 19576          # ...by pid
ccwho jump "vitest"       # ...by title substring
```

An ambiguous query lists the candidates rather than guessing. Needs iTerm2; the
lookup is `tty of session` over its windows.

### Clickable, without a new UI

The tty column is emitted as an **OSC 8 hyperlink** when stdout is a terminal, so in
iTerm2 (3.x) it is genuinely clickable. Clicking hands `ccwho://jump/s032` to
LaunchServices, where a small applet turns it back into `ccwho jump s032`.

```sh
bash install-handler.sh      # once; builds ~/Applications/ccwho-jump.app
```

The applet is ~10 lines of AppleScript, has no Dock icon (`LSUIElement`), and runs
no daemon. `--no-links` opts out; a terminal that cannot render OSC 8 shows the
plain label, so there is no downside to leaving it on.

**On trusting the URL.** A `ccwho://` URL can be handed to the applet by anything on
the machine, so the target is validated against `^[A-Za-z]?[0-9]{1,8}$` before use -
a tty or a pid, nothing else. The applet also swallows failures, because a non-zero
`do shell script` raises a modal dialog. The scheme prefix is checked as well as the
target shape: `https://evil/s032` is exactly as long as `ccwho://jump/`, so without
that check it would slice to a valid-looking target.

## ccwho reap - killing only the stale ones

Leaked helper processes accumulate: liveapp's vitest PTY-guard tests spawn a
`script` holding a real pseudo-terminal per run and never reap it. After a few days
that is hundreds of processes and a large share of the machine's ptys.

Killing them by name is dangerous, because a current test run looks identical to a
two-day-old one. `reap` filters by age and is a **dry run by default**:

```sh
ccwho reap                          # what would go, older than 1h
ccwho reap --older-than 6h          # be stricter
ccwho reap --older-than 6h --kill   # actually do it
ccwho reap somepattern --older-than 1d
```

Orphaned roots (ppid 1) are signalled first so their children die with them.

**Why the age parsing has its own tests.** `ps` ELAPSED has four shapes - `SS`,
`MM:SS`, `HH:MM:SS`, `DD-HH:MM:SS` - and getting the day field wrong means reaping
live work. Anything unparseable returns 0, so the "older than N" filter spares it
rather than killing it. An empty pattern matches nothing, deliberately.

The dry-run default earned itself immediately: the first version took `1h` as the
*pattern* (a flag's value is not a positional argument) and matched
`PeopleViewService`. It printed that instead of killing it.

## ccwho save / restore - a reboot stops being a one-way door

The sessions always survived a reboot. `~/.claude/projects/<slug>/<sessionId>.jsonl`
is still there and `claude --resume <id>` reopens it. What did not survive was
knowing **which** sessions were open, so a fleet of seventeen was unrecoverable in
practice - and the machine therefore never got rebooted. That is how swap reached
**32.4 GB of 33.8 GB used**, which is what suspends every app, and what makes the
timing suites in liveapp fail their bounds and report a red gate that is really a
red machine.

```sh
ccwho save                 # capture the live fleet
ccwho restore              # ...what was open, about what, how to reopen it
ccwho restore --open       # ...actually reopen them, one iTerm2 window each
ccwho restore --from PATH  # an older manifest
```

```
17 sessions to restore (saved Mon 31 Aug 22:28)
 1. liveapp  was ttys071
      opened: I've updated the LIVEAPP_BOT_GH_TOKEN in blabberate, but for some reason...
      latest: does this still need a review? If not, land it.
      ASKED YOU: Tell me which and I'll finish it and land. Or say land as-is...
      claude --resume a1acd3cd-848b-...
```

**Both halves, because neither identifies a session alone.** Measured on the real
fleet: `latest` read `/compact`, `go ahead` and `let's fix 1-3` for three of the
seventeen, while `opened` read `restart from disk` for another. Rows are in
dashboard order, so what was waiting on you is at the top and still carries its ask.

**It saves itself.** `ccwho --watch` writes a manifest every 5 minutes
(`CCWHO_AUTOSAVE` seconds, `0` disables), because the reboot this exists for is
usually the one you did not plan. Manifests live in `~/.ccwho/restore/` - under
`$HOME`, never a temp dir, since outliving the reboot is the entire point - and the
newest 20 are kept, because a tool built for a full disk does not get to fill one.

### What it is careful about

The write goes through `os.replace`. A half-written manifest read after a reboot is
worse than none, and a reboot is exactly when a partial write happens; a failed save
leaves the last good manifest intact.

`cwd` arrives from `claude agents --json`, off the machine rather than from us, and
both consumers build a command from it. The shell line quotes it; the reopen path
escapes it into an AppleScript literal, where an unescaped `"` **ends the string and
the rest is parsed as code**, and where backslash must be escaped first or it eats
the quote escape. Both are asserted by checking the payload survives as ONE argument
- a substring check would pass vacuously.

Two of these assertions only exist because the mutation battery caught them passing
for the wrong reason: an empty-topic case that no test covered, and a `keep=0` guard
that was invisible because `found[:-0]` is `found[:0]`. The battery carries a control
cell that must stay green - a run where every cell agrees is a broken harness, not a
result.

## Install

```sh
git clone <this> ~/projects/ccwho
ln -s ~/projects/ccwho/ccwho.py  ~/.local/bin/ccwho
ln -s ~/projects/ccwho/ccgate.py ~/.local/bin/ccgate
```

## Use

```sh
ccwho                 # one shot, needs-you first
ccwho --watch         # live, redraws every 5s (and saves a restore manifest)
ccwho --watch 2       # ...every 2s  (also -w 2, --watch=2)
ccwho --blocked       # only what needs you or holds detached work
ccwho save            # record the live fleet (before a reboot)
ccwho restore [--open]  # list it back, or reopen the windows
ccwho --prompt        # add the last thing you said, under each row
ccwho --json          # machine-readable, for a status line or key binding
```

## Columns

| Column | Where it comes from |
|---|---|
| project | cwd, with worktrees reported under their parent repo |
| status | derived, see below - NEEDS YOU sorts to the top |
| title | Claude Code's own `ai-title` entry, not the auto-generated session name |
| doing | the last tool call, using Bash's human `description` when present |
| since | time since the last real **turn**, from its `timestamp` - see below |

The session *name* is deliberately not a column. `liveapp-4e` told you nothing,
which is what started this.

### What actually needs you

The harness status field cannot answer this on its own, in either direction.

**It over-reports.** `claude agents --json` has one status, `waiting`, with
`waitingFor: "input needed"`. That covers a session blocked on an outstanding tool
call *and* one that simply finished its turn. Only the first needs you.

**It under-reports, which is worse.** A session that ends its turn with a direct
question is reported `idle`, indistinguishable from one that finished and went
quiet. Measured on a live fleet of 15: six sessions were parked awaiting a decision
and *every one of them* was reported `idle`.

So `ccwho` derives the state instead:

| state | how it is decided | label |
|---|---|---|
| blocked | an unanswered `tool_use` in the transcript | NEEDS YOU |
| asks | the closing line is a question or a request | ASKED YOU |
| stopped | not busy, and **nothing running under it** | STOPPED |
| busy | harness says busy | busy |
| running | not busy, but background work is in flight | running |

`waiting` with nothing pending is not a state of its own - it is decided the same
way as any other non-busy session, by what is in flight.

### Stopped, or waiting on a machine

The signal that matters most is not what the last message said - it is whether
anything is still in flight. A session that is not busy and has no work under it has
**stopped**: it will not progress without you. One with background work is waiting
on a machine, not on a human, so it sorts last and shows its count as `[3 bg]`.

`work_descendants()` walks the process tree from the session pid and excludes
infrastructure. Measured on a live fleet: every non-busy session had exactly three
descendants and all three were `chrome-devtools-mcp`; every busy session had 4-21
real ones. Counting MCP servers would make every session look busy forever.

STOPPED outranks busy, because a stopped session is the one that needs a human.

An ASKED YOU row shows **the question itself** in place of its last tool call, so
the list answers "what does it want" without opening the session. Within each state,
rows sort **most recent first**, so a fresh ask lands above ones you have already
seen and parked.

### Why `since` does not use file mtime

Idle transcripts keep receiving metadata writes - `atis-latch`, `bridge-session`,
`mode` - roughly every four minutes. File mtime therefore reports "4m" for a session
whose last actual turn was six days ago, and it is wrong on exactly the parked
sessions where recency matters. Measured skew on a live fleet:

    status   mtime   last turn
    idle     4m      6d          Add second email account function
    idle     12h     2d          Docker high CPU usage
    busy     7s      7s          (accurate only while active)

`since` reads the `timestamp` of the last `assistant`/`user` entry instead, falling
back to mtime only when no turn carries one.

### Cost

A tick costs **~0.05s warm, ~0.26s cold** over 15 sessions. Two things make that so,
and both were regressions I had to fix after `--watch` burned half a core:

- **One parse per tick, not per extractor.** The extractors took 5.2 full JSON
  passes over every tail line. They now accept pre-parsed records.
- **A window cache keyed on (mtime, size).** An unchanged transcript is not re-read
  or re-parsed. The cache lives in the *runner's* state, not the engine, because the
  engine is reloaded every tick and module globals would be discarded.

Before: 48 MB parsed per tick, 5.09s of work on a 5s interval - a full core.

### Known gap: announced-then-stopped

A session can need you without asking anything. One closed with "Gate next, then
land." and simply stopped - no question, no request phrase, reported `idle`. That
pattern is **not** detected. It is a third shape after the question and the
request, and no rule for it has been validated yet.

Only the closing line is considered - a question earlier in a long report is
usually one the message goes on to answer. The phrase list ("tell me", "your call",
"say the word", ...) is deliberately short and every entry was validated against a
live fleet: 6 flagged, 6 genuine, 9 correctly quiet.

### The older NEEDS YOU vs ready split

`claude agents --json` reports one status, `waiting`, with `waitingFor: "input
needed"`. That covers two very different states, and conflating them cries wolf:

- **blocked** - a tool call is outstanding with no result. It genuinely needs you.
- **ready** - the last turn ended normally and it is sitting at the prompt.

`ccwho` splits them by looking for an unanswered `tool_use` in the transcript. Only
blocked sessions get NEEDS YOU; ready sorts below busy.

Measured on a live fleet: the one session Claude Code called `waiting` had nothing
pending and `stop_reason: end_turn` - the same shape as every `idle` session. It did
not need anything. The blocked case is covered by constructed tests, since a live
fleet may go days without producing one.

## Hot reload

`ccwho.py` is a thin runner; all logic is in `ccwho_engine.py`, which is
`importlib.reload`ed on every tick of `--watch`. Edit the engine while a watch is
running and the next tick picks it up - no restart. Same model as
`network_check_ruby`: thin loop, hot-reloaded engine, state carried between ticks
rather than held inside the engine.

A broken edit does not kill the loop: the reload error is caught, the last good
module keeps rendering, and the error shows as a banner until you fix it.

Implication, as with the Ruby version: keep the engine free of long-lived objects.
Pure functions and plain dicts only.

## Design notes

- **Transcripts reach hundreds of MB.** Never read one whole: head for the first
  prompt, tail for the last.
- **Harness-injected "user" turns are not topics.** Skill preambles, compaction
  notices, `<task-notification>` blocks and the local-command caveat are filtered.
  In strict mode the search window widens rather than printing noise.
- **macOS has hundreds of system daemons on PID 1.** The orphan count is scoped to
  `$HOME`, or it reports 582 and means nothing.
- **`age()` guards epoch 0**, because `if not started_ms` swallows a valid timestamp.

## Tests

```sh
python3 -m unittest -v
```

208 tests, stdlib only. Every guard has been mutation-checked: reverting the fix it
defends turns its test red. An assertion that cannot fail is not an assertion.
