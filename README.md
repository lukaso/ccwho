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

## Install

```sh
git clone <this> ~/projects/ccwho
ln -s ~/projects/ccwho/ccwho.py ~/.local/bin/ccwho
```

## Use

```sh
ccwho              # everything, needs-you first
ccwho --blocked    # only sessions waiting on you or holding detached work
ccwho --json       # machine-readable, for a status line or a key binding
ccwho --no-color
```

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

33 tests, stdlib only. Every guard has been mutation-checked: reverting the fix it
defends turns its test red. An assertion that cannot fail is not an assertion.
