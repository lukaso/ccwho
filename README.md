# ccwho

Which Claude Code session needs you, and what is it about.

```
16 sessions: 2 waiting · 2 busy · 12 idle
16 detached processes under your home on PID 1

liveapp       NEEDS YOU   liveapp-4e     let's fix 1-3. 4 doesn't need a fix.        4d
liveapp       busy        liveapp-06     yes, let's do that. create a directory…    23h
chiefofstaff  idle        chiefofstaff-08  commit and push                          10d
```

No daemon, no hooks, no tmux, no config. Reads only what Claude Code already writes
to disk. The engine is stdlib Python; only the live list needs a library (Textual),
and `uv` fetches that for you.

On a terminal, `ccwho` opens the **live list**: every session, what needs you at the
top, the harness's own recap under each row, and Enter to go to its window. Piped,
or with a subcommand, it is the one-shot table above.

## Install

macOS, iTerm2 and Claude Code.

```sh
brew install lukaso/ccwho/ccwho
ccwho setup
```

`brew upgrade ccwho` takes you to the newest release. Brew brings `uv`, which the
live list runs under.

To work on ccwho itself, run it from a clone instead - the engine hot-reloads, so
edits show up in a running list:

```sh
git clone https://github.com/lukaso/ccwho ~/projects/ccwho
ln -s ~/projects/ccwho/ccwho.py  ~/.local/bin/ccwho
ccwho setup
```

`ccwho setup` does the rest once, and says what it did: the `ccwho://` link
handler, the autosave job, the iTerm2 hotkey window, and the Accessibility
permission the hotkey needs to reach you from another app. It writes nothing on a
machine that cannot work, and a second run changes nothing that is already right.
`ccwho doctor` checks the same things later, read-only.

## Why

Agent view and every third-party fleet tool show you a session **name**. When your
sessions are auto-named `liveapp-4e`, `liveapp-b3`, `liveapp-cd` — two of them
sometimes identical — the name tells you nothing, and switching tools does not fix
it because it is a data problem, not a display problem.

`ccwho` shows the **last thing you actually said** to each session, pulled from its
transcript. That is the column no tool has.

## The live list

```
ccwho                         # on a terminal
⌥/                            # from any app, after `ccwho setup`
```

| key | does |
|---|---|
| ↑ ↓ / `j` `k` | move |
| Enter | go to the session's window - or give it one (see below) |
| → | the brief: what it was working on, and the processes it started |
| ← / Esc | back |
| `/` | search every name a session has, plus what it is about - and `:3000` finds the session holding that port |
| `p` | every process agents started, grouped: each session, left behind, Codex, not sure |
| `x` `x` | on a STUCK row: kill its wait loop that cannot end (or click `[kill loop]` twice) |
| `o` | reopen the last saved fleet - only offered when nothing is running |
| `r` | restart the list |
| `q` | quit |

The second line of every row is the **recap** Claude Code writes into the
transcript itself (`away_summary`), always with its age: one session's newest recap
was 2 days and 23 turns old, and a recap without its age reads as the current state.

The first line names the session: its short id, then its **name** (`liveapp-40`),
whole wherever the line can hold it - it is the address another session sends a
message to, and a cut name is no address. The title says what it is about, and it
is what gives way, down to nothing, before the name or the age does. A session with
no name shows its project there. A renamed session's name no longer starts with
its project, so the project follows in the dim part of the line, where there is
room for it.

**Copying.** The list takes the mouse, so dragging over it selects nothing for
iTerm2 (hold ⌥ while you drag for iTerm2's own selection). Instead, every value in
the detail - the id, project, title, recap, what it opened with, what you said,
each progress step, what it said, the tty, pid, name, cwd, session id and the
`claude --resume` line - is underlined and lights up under the mouse: a left
click copies it, whole even where the pane shows it cut, and the status line says
what it copied. What you paste is what the pane shows, without the terminal codes
a session printed. Labels copy nothing.

It notices a session start needing you within a second, by stat-ing transcript
files - no program is started for that - and does a full scan every 20 seconds.
Collection runs in a worker thread, because `claude agents --json` can take 30
seconds when it is unhappy and a list that freezes when you reach for it is the
problem this exists to solve.

**The hotkey.** `ccwho setup` adds an iTerm2 hotkey window running the list, on ⌥/
by default (`ccwho setup --hotkey option-w` to change it). A hotkey is only global if
iTerm2 had Accessibility when it registered the key; setup asks for it, and says
when iTerm2 must be restarted for it to take.

If the hotkey stops working after a crash, an app has probably left macOS Secure
Input on, and then no global hotkey works. The live list (click iTerm2, or run
`ccwho`) and `ccwho doctor` both name the app. The fix is always the same: lock
the screen (Ctrl+Cmd+Q) and log back in. iTerm2's own Secure Keyboard Entry is
not reported: iTerm2 holds Secure Input only while it is in front.

### Background sessions

A session started with `claude --bg`, or sent to the background from agent view,
runs under the Claude Code daemon with no window at all. Enter on one opens a new
iTerm2 window running `claude attach <short id>`, and the row blinks while it opens.
It is never resumed: `claude --resume` on a running session starts a second process
on one transcript.

`claude attach` takes the **short** id - the 8 hex that `claude agents` lists - not
the session UUID. Given the UUID it says `No job matching`, about a session that is
running fine, while `--resume` correctly refuses it: every way in fails and the
session looks lost.

To end one: `claude stop <short id>` (the conversation is kept, and `--resume` works
after), or `claude rm <short id>` to delete it. The session keeps the directory it
was started in wherever you attach from; `--resume` uses the directory you are in.

## Subscription usage

```
usage  ant work 5h 42%↓/60% ↻15:30 · 7d 2%↓/30% ↻Thu  │  ant 1a2b 5h 0% · 7d 67%↑/40% ↻Mon
```

One dim line under the header, one entry per account that a live session spends:
the machine login by its email, a `CLAUDE_CODE_OAUTH_TOKEN` by the first hex of its
fingerprint. `5h 42%↓/60%` is 42% of the 5-hour budget used with 60% of the 5 hours
gone: `↓` (green) is on pace, `↑` (red) is faster than time passes. The used number
turns yellow at 80% and bold at 95%. A window past its reset says `expired`; a window
whose newest reading is older than 15 minutes says how old. With two or more accounts,
each row ends with the account it spends (`?`: no reading yet), and the entry of the
selected row's account is bright. The same line and tags are in `ccwho`, `ccwho ls`
and `--watch`, and each session's own status bar shows its entry.

Where it comes from: Claude Code hands a statusLine command the rate limits of the
account the session spends. `ccwho setup` offers to add `ccwho statusline` in each
config dir where you run claude yourself - it shows the change and asks - and
`ccwho statusline` records one small file per session in `~/.ccwho/usage/`. ccwho
never logs in and never reads a credential: a token account is a fingerprint of the
token, computed inside the session and never stored. A session reports after its next
reply - including sessions that were already running when setup added the line
(measured: they reload the settings). `ccwho accounts` lists every account seen in the last 8 days;
`ccwho accounts name <id> <label>` gives one a name. `ccwho setup --no-usage` turns it
off again.

## Finding an old session

```sh
ccwho ls                   # the table
ccwho ls hotkey            # every session matching - running or ended
ccwho ls hotkey --all      # ...including sessions a program started
ccwho show hotkey          # what that session was working on
```

The live fleet is fifteen sessions; the disk holds a month of them (1,480
transcripts, 1 GB, measured 2026-09-19). `ls` and `show` search a small index in
`~/.ccwho/` that reads only what was **appended** since last time - transcripts only
grow at the end, so (inode, size, offset) is enough to resume. The first build
stored pasted logs whole and came to 177 MB; entries are now capped, since you
recognise a line by its front.

## ccwho doctor

```sh
ccwho doctor          # ok / BAD per dependency, and the line to run when BAD
ccwho doctor --json
```

It exists because iTerm2's own Claude Code integration stopped working and nothing
said so: its hook had been dropped from `~/.claude/settings.json` by another tool's
rewrite. Every dependency ccwho has can fail that quietly - an autosave job loaded
but not run since Tuesday, a link handler calling a copy of ccwho that was deleted -
so each gets a check. Read-only by design: a tool that rewrites another tool's
config without asking is how that happened in the first place. `ccwho --watch`
shows the first fault in its header.

## Sources

| What | Where |
|---|---|
| session list, status | `claude agents --json`, plus `sessions/<pid>.json` in every config dir |
| the topic | `~/.claude/projects/*/<sessionId>.jsonl` (head + tail read only) |
| the recap | the same transcript, `system` / `away_summary` records |
| old sessions | the index, `~/.ccwho/` |
| processes an agent started | the session id in each process's environment (`sysctl`) |
| ports | `lsof -iTCP -sTCP:LISTEN` |

`sessionId` from the agents feed **is** the transcript filename, which is what makes
the topic column possible.

`claude agents --json` only lists the sessions of the config dir it runs under, so a
session started with its own `CLAUDE_CONFIG_DIR` never showed up - measured, 2 of 17
running sessions were missing. Each running `claude` names its config dir in its
environment, and each config dir keeps a small file per live session, so ccwho reads
those as well (read-only: pointed at another dir, `claude agents` writes into it). A
file counts only while its process is alive and is still the same process - its start
time, as `ps` prints it in UTC, must match the one the file recorded. From the
environment ccwho keeps five named variables and drops the rest: it holds tokens, and
ccwho's output is read by agents. `ccwho doctor` says if those files stop parsing.

## Processes and ports

Agents start dev servers and leave them running. The port you want is taken, and
the session that did it may not even know. Every process Claude Code starts carries
its session id in its environment (`CLAUDE_CODE_SESSION_ID`; Codex sets
`CODEX_THREAD_ID`), and the mark survives the process being orphaned - so ccwho can
say who started what:

```sh
ccwho ps                    # every process an agent started, its session, its ports
ccwho ps --port 3000        # who holds :3000 - exit 1 if no agent does, 3 if that is not known
ccwho ps --all              # ...including helpers (MCP servers and the like)
ccwho ps --json             # for scripts, and for agents told to clean up
ccwho ps --full             # the whole command line, not the short form
```

The live list and the table (`ccwho ls`, `ccwho --watch`) say it in one dim line
each, never louder than what needs you: `agents hold :3000 left behind · :5173 app` at the top, the
ports on each row, and at the bottom `left behind: N processes` (sessions that have
ended) and `codex: N processes` (Codex's own group: whether a Codex session is still
running cannot be told cheaply, so its processes are never called left behind). A
row's `+N detached` counts its processes that were orphaned (`&`, `nohup`) - the
ones the session reports `idle` over while they run on. In the live list, the bottom
line says `· p to see`, and `p` shows the same list `ccwho ps` prints.

`left behind` is the group a clean-up would kill, so nothing in doubt goes in it.
A process whose session is gone is `not sure` instead when the session list could
not be read in full (the agents list, a session file, a claude's environment, or a
running `claude` that no source lists), when the claude that started it still runs
(`CLAUDE_PID` - after `/clear` a claude keeps running under a new session id), when
it is a `claude` itself or runs under one, or when it runs inside an app or tmux
that an agent opened - the mark is inherited, and the user may work on in there. An app (`….app/Contents/…`) is `not sure` whoever started it: an agent that
opens Docker Desktop has not made Docker its work. Exit 3 means "not known": ps or
lsof failed, no environment could be read, or the port's holder could not be read.

What ccwho can and cannot see: the environment is read with `sysctl` and only five
named variables are kept - it holds tokens, and ccwho's output is read by agents.
A command line is printed short: the program, and only the arguments whose place
says they are harmless - flags, file names with a code or config extension, short
words of letters and small numbers before the first flag (`npm run dev`), the value
of `--port`/`--host`/`-m` and the like, `PORT=3000`, a package at `@latest`, a URL's
host; the rest is `…`. A number after any other flag is `…`: after `-P` it is a PIN
as often as a port, and the ports column already shows the ports. It fails closed:
after the first sign of a secret - a password flag (`-a`, `-k`, `--password`, …), a
secret-named word or key (`token`, `DB_PW=`), a header name (`Cookie:`), a program
like `sshpass`, `echo` (its arguments are data), a flag ccwho cannot parse - nothing
more of the line is shown. A command that reads a password
from stdin (`--password-stdin`, `sudo -S`) shows only its program. The same short
form is used for what a session is doing (its command, description or search). One
shape it cannot tell from a word: a password of plain letters placed before any flag
(`mytool hunter`) is printed. `--full` prints the whole line with secret-looking values masked - a best
effort, so it is never the default. macOS hides the
environment of its own system binaries, so a `/bin/sleep` an agent starts carries
no mark ccwho can read; claude, node and Python dev servers do. A server inside a
Docker container is not a host process, and its port belongs to Docker.

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
ccwho setup                  # once; builds ~/Applications/ccwho-jump.app
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
ccwho restore --open       # ...actually reopen them, in their old panes where iTerm2 restored them
ccwho restore --check      # would it restore? run this BEFORE you reboot
ccwho restore --list       # every saved manifest and what it holds
ccwho restore --from PATH  # an older manifest
ccwho open <session-id>    # focus that session, or reopen it if its window is gone
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

**Back where it was.** A save records the iTerm2 pane each session is in, and that
tab's title. When iTerm2 restores its windows after a restart, it gives each pane
the id it had before - `PTYSession.m` adopts the saved "Session GUID" on window
restoration - so `restore --open` (and `o` in the list) writes each session's resume
line into its own pane, in the same window, tab and split. A pane that came back
with a new id is found by its title instead, but only when exactly one saved session
and exactly one pane have it; the status mark Claude Code puts in front (`✳`, `◐`)
does not count. It writes only into a pane where nothing runs but a shell at its
prompt, and clears a half-typed line there first. Every other session - and one
whose pane closed before the write - opens in a new window, as before. `restore --check`
says how many of the saved panes are open now.

**It saves itself.** Two ways, because the reboot this exists for is usually the one
you did not plan. `ccwho --watch` writes a manifest every 5 minutes
(`CCWHO_AUTOSAVE` seconds, `0` disables) - but only while a watch is open in some
window, which on 2026-09-05 meant a crash found a manifest five days old. So
`ccwho setup` puts it on a 15-minute launchd timer as well. The job is rendered from
`com.lukaso.ccwho.save.plist.template` for this machine - its home, its ccwho, the
directory its `claude` is in, because launchd's `PATH` has none of them - and
`ccwho doctor` says when the installed job no longer matches what setup would write.

`StartInterval` fires only while the Mac is awake and never wakes it, and after a
sleep launchd runs the job once if the interval elapsed. Manifests live in
`~/.ccwho/restore/` - under `$HOME`, never a temp dir, since outliving the reboot is
the entire point - and the newest 20 are kept, because a tool built for a full disk
does not get to fill one.

The job's own log, `~/.ccwho/autosave.log`, is bounded the same way: newest
`CCWHO_KEEP_LOG_LINES` lines (default 1000), `keep<=0` meaning no bound rather than
"empty it", rewritten through a temp + `os.replace`. Checked once a day, not once a
run - 96 runs a day need not each rewrite the file to decide it is already short
enough - and always AFTER the save, because `os.replace` leaves the caller's open
stdout pointing at the replaced inode and anything still to print would go nowhere.

**A save that cannot ask must not answer.** Scheduling the save is what exposed
this. `agents_json()` used to return `"[]"` both when claude reported no sessions
and when it could not be reached at all - and launchd's minimal `PATH` has no
`~/.local/bin`, so every timed run found no binary, captured nothing, wrote a
0-session manifest and printed success. At 20 kept, twenty ticks is five hours to
evict every manifest that had anything in it: the tool would have deleted the exact
record it exists to keep. Now an unreachable source is `None`, distinct from a
genuine `[]`; `save` exits 1 and writes nothing, and it names `PATH` as the thing to
check. A working source with nothing open writes nothing either - an empty manifest
has no restore value and can only push out one that has.

### The links resolve when you click them, not when the list was printed

Each project name in `ccwho restore` is an OSC 8 link to `ccwho://open/<sessionId>`,
and the verb is decided against the world at click time:

- running, with a window -> **focus it** (reopening a live session would fork the
  conversation into two processes)
- running, with no window -> **attach it** in a new iTerm2 window, with
  `claude attach` - the same guard, never a second process
- not running -> **reopen it** in a new iTerm2 window
- the live list could not be read -> **do nothing**: an empty list from a failed
  read looks exactly like "nothing is running"
- in neither the live fleet nor any manifest -> say so, do nothing

So after a reboot every link in the list reopens, and ten minutes later the same
link focuses the window it just made. The lookup spans **every** saved manifest,
newest record of each session winning, because a link is clicked from whatever list
is on screen - with 20 kept, often not the newest one.

`ccwho://jump/<tty>` (the tty column) answers "where is this window", which is a
different question the moment the window is gone - and after a reboot every window
is gone, which is exactly when the restore list is what you are reading.

The registered handler forwards the **whole** URL to `ccwho url`, so adding a verb
never means rebuilding the applet. Both targets are pattern validated on the far
side - a session id must be a UUID, a jump target a tty or pid - because the URL
arrives from LaunchServices and anything on the machine can hand you one. Nothing
unrecognised reaches a verb.

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

## Use

```sh
ccwho                   # the live list (on a terminal)
ccwho ls [words] [--all]  # the one-shot table, or every session matching
ccwho show <anything>   # what that session was working on
ccwho ps [--port N] [--full]  # what agents started, and their ports
ccwho jump <tty|pid|title>  # focus that window
ccwho open <session-id> # focus it, give it a window, or reopen it
ccwho --watch           # the plain table, redrawn every 5s (and autosaving)
ccwho --watch 2         # ...every 2s  (also -w 2, --watch=2)
ccwho --blocked         # only what needs you or holds detached work
ccwho --prompt          # add the last thing you said, under each row
ccwho --json            # machine-readable, for a status line or key binding
ccwho save              # record the live fleet (before a reboot)
ccwho restore [--open]  # list it back, or reopen the windows
ccwho reap              # leaked helper processes, dry run
ccwho doctor            # is everything ccwho needs in place?
ccwho setup             # install what it needs, once
```

## Columns

| Column | Where it comes from |
|---|---|
| project | cwd, with worktrees reported under their parent repo |
| status | derived, see below - NEEDS YOU sorts to the top |
| title | Claude Code's own `ai-title` entry, not the auto-generated session name |
| doing | the last tool call, using Bash's human `description` when present |
| since | time since the last real **turn**, from its `timestamp` - see below |

The session *name* is deliberately not a column here. `liveapp-4e` told you
nothing about the work, which is what started this. The live list does show it,
next to the title: there it is not a description but the address you message the
session by.

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
| asks | the closing line of an **ended** turn is a question or a request - even while the harness says busy | ASKED YOU |
| stuck | harness says busy, the turn has ended, and a wait loop under it can never end | STUCK |
| stopped | not busy, and **nothing running under it** | STOPPED |
| busy | harness says busy, mid-turn | busy |
| running | background work is in flight: not busy, or busy with the turn over | running |

`waiting` with nothing pending is not a state of its own - it is decided the same
way as any other non-busy session, by what is in flight.

### Busy, but the turn is over

The harness says `busy` while any background task runs - even after the turn has
ended. Two sessions ended their turns with a question and sat in BUSY, one for 26
hours, behind a wait loop like `until grep -q "Test Files" <task>.output; do sleep
5; done` on a test run that had ended without printing that line. So a turn is
over when `turn_duration` or `stop_hook_summary` follows the last message
(measured over 300 transcripts: 2235 of 2251 ended turns carry one, and 0 of 44860
steps inside a turn do). Then a question is ASKED YOU, whatever is running; a
finished turn without one stays in BUSY as running - the task will wake it, so it
is not news.

Unless the task cannot end. Only one loop shape is judged: `until grep ...
<task>.output; do sleep N; done` (or `while ! grep`), where the grep is the whole
condition, its files are all task outputs, the sleep is the whole body, and
nothing but that sleep or grep runs under it. When every file it polls ends in
Claude Code's `[exited with code N]` or `[killed]`, has not changed for two
minutes - or the loop's own sleep plus one, if that is longer - and the loop's
own grep, asked again, still does not find its line, it can never stop: the line
it waits for can no longer arrive. (If grep does find it, the loop has ended and
its shell has moved on.) Only grep's `-q -E -F -i -s -e` are allowed, and a
pattern the shell would expand (`$X`, `$(..)`, backticks), or that uses an escape
only some greps know (`\d`, `\s`, `\w`, `\|`), is not judged - so the question
asked again, by `/usr/bin/grep` in both the C and UTF-8 locales (found in either
is found), is the same question. The answer
is kept: a finished file does not change. Any other shape is unknown,
because the process a loop runs in is the Bash tool's shell, and it runs the rest
of the command too. That session is
**STUCK**, its own group after NEEDS YOU, and the row names the loop. `x` twice,
or `[kill loop]` clicked twice, kills it; the first only asks, for ten seconds, and
moving away cancels it; the kill looks again first and signals only a pid that
is still that session's dead loop. Killing it reports the task
as failed to the session, which wakes the agent. A file that cannot be read, or
has no end line, is never called dead.

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
./test
```

881 tests: 767 for the engine, runner, brief, index and setup (stdlib only), and
114 for the live list, which `uv` runs with Textual. If uv cannot fetch Textual the
UI tests FAIL rather than skip - "OK (skipped=12)" while the screen is broken is a
green light for nothing. Every guard has been mutation-checked: reverting the fix it
defends turns its test red. An assertion that cannot fail is not an assertion.

## License

MIT.
