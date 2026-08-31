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

## Install

```sh
git clone <this> ~/projects/ccwho
ln -s ~/projects/ccwho/ccwho.py  ~/.local/bin/ccwho
ln -s ~/projects/ccwho/ccgate.py ~/.local/bin/ccgate
```

## Use

```sh
ccwho                 # one shot, needs-you first
ccwho --watch         # live, redraws every 5s
ccwho --watch 2       # ...every 2s  (also -w 2, --watch=2)
ccwho --blocked       # only what needs you or holds detached work
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
| since | time since the transcript was last written - real activity, not session age |

The session *name* is deliberately not a column. `liveapp-4e` told you nothing,
which is what started this.

### NEEDS YOU vs ready

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

94 tests, stdlib only. Every guard has been mutation-checked: reverting the fix it
defends turns its test red. An assertion that cannot fail is not an assertion.
