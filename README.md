# ccwho

Which Claude Code session needs you, and what is it about.

```
16 sessions: 2 blocked · 12 stopped · 2 busy
usage  ant:work 5h 42%↓/60% ↻15:30 · 7d 12%↓/30% ↻Thu

liveapp-4e       s071  NEEDS YOU   ✳ Fix the gate       Bash: run the gate        4d
liveapp-06       s012  busy        ◐ Landing page       Edit: index.html         23h
chiefofstaff-08  s040  STOPPED     ✳ Commit and push    Bash: git push           10d
```

No daemon, no hooks, no tmux, no config. Reads only what Claude Code already writes
to disk. The engine is stdlib Python; only the live list needs a library (Textual),
and `uv` fetches that for you.

On a terminal, `ccwho` opens the **live list**: every session, what needs you at the
top, the harness's own recap under each row, and Enter to go to its window. Piped,
or with options, it prints the table above.

## Install

macOS, Claude Code, and iTerm2 or Terminal.app (see [Terminal.app](#terminalapp)).

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
handler, the autosave job, the usage statusLine in each config dir where you run
claude (it shows the change and asks first), the iTerm2 hotkey window, and the Accessibility
permission the hotkey needs to reach you from another app (asked for when setup runs
directly in an iTerm2 window, not in tmux: macOS gives it to the app setup runs in). It writes nothing on a
machine that cannot work, and a second run changes nothing that is already right.
`ccwho doctor` checks the same things later, read-only. The iTerm2 steps apply only
while you use iTerm2 - it runs, or ccwho's hotkey is installed; otherwise setup and
doctor say "not used" and what to do to add it ([Adding iTerm2 later](#adding-iterm2-later)).

## Terminal.app

ccwho runs in Terminal.app as it does in iTerm2 - the table, the live list, `setup`
and `doctor` - and knows the sessions in Terminal.app's tabs:

- a row in a Terminal.app tab has a window to go to, and Enter (or `ccwho jump`)
  brings that tab to the front;
- a new window - to attach a background session, or to reopen one - opens in the
  app the session was last in; with nothing to say which, in the app ccwho runs in,
  else in one that runs (iTerm2 first), else in iTerm2 if it is installed, else in
  Terminal.app;
- `ccwho save` records which app each session was in, and `restore --open` reopens
  each in that app.

A tab's first process is the `login` Terminal.app starts on its tty, so which tabs
it shows is read from `ps`, like iTerm2's panes - no Apple Event.

**The Automation prompt.** The first time an app asks Terminal.app something, macOS
asks you whether that app may control Terminal. Allow it. Which app that is: the
terminal ccwho runs in, or ccwho-jump.app for a click on a link.

- In the background, the list and its commands ask Terminal.app only for its tab
  names (the title Claude Code gives a tab, never the text on its screen), and only
  while a Claude Code session runs in one of its tabs. The autosave job never asks
  it.
- A jump, a new window and a restore ask it because you asked for them.

If you did not allow it, the rows show no tab names and a jump to a tab fails:
System Settings > Privacy & Security > Automation, then the app that asked, then
Terminal. `ccwho doctor` says so, and how long ago, from what the list's last ask
found - doctor asks Terminal.app nothing itself, so after you allow it, it reads
the same until the list asks again.

ccwho never starts an app by asking it something: a background ask first checks,
inside the same script, that the app runs.

**Known limits.**

- `restore --open` opens each Terminal.app session in a new window. After a restart
  Terminal.app also reopens its old windows at a bare prompt, so you get both
  ([#30](https://github.com/lukaso/ccwho/issues/30)).
- Each ccwho process asks each app for its tab names at most once a minute while it
  answers, again 30 s after an ask that failed, and 10 minutes after an ask it did
  not answer in 2 minutes (doubling to 2 hours while it stays stuck; `ccwho doctor`
  says when). Several open lists and commands ask more often
  ([#31](https://github.com/lukaso/ccwho/issues/31)).
- The hotkey window is an iTerm2 feature.

### Adding iTerm2 later

Install iTerm2, start it, then run `ccwho setup` again directly in an iTerm2 window
(not in tmux): it adds the hotkey window and asks for the permissions it needs - macOS
gives Accessibility to the app setup runs in, and it is iTerm2 that needs it. There is nothing to undo
first. From then on `ccwho doctor` checks the iTerm2 steps too.

## Why

Agent view and every third-party fleet tool show you a session **name**. When your
sessions are auto-named `liveapp-4e`, `liveapp-b3`, `liveapp-cd` — two of them
sometimes identical — the name tells you nothing, and switching tools does not fix
it because it is a data problem, not a display problem.

`ccwho` shows what each session is about: its title and Claude Code's own recap -
and, with → or `ccwho show`, the **last thing you actually said** to it, pulled from
its transcript.

## ccwho and herdr

[herdr](https://github.com/herdrdev/herdr) is for the same problem: many agent
sessions, and which one needs you. It is a terminal multiplexer, like tmux: your
agents run in herdr panes, and herdr owns their terminals. ccwho does not run your
agents: they stay in your usual terminal windows. To know their state, ccwho only
reads - what Claude Code writes, `claude agents --json`, `ps` and `lsof`.
It acts only when you ask: it brings a window to the front, kills processes after it
shows you the list, and reopens sessions after a reboot.

Compared with herdr v0.9.3 and [its docs](https://herdr.dev/docs/), on 2026-10-02.

| Area | herdr | ccwho |
|---|---|---|
| Sessions it sees | Only agents in its panes | Claude Code sessions in any terminal and from every config dir, [`claude --bg` sessions](#background-sessions) and [SDK sessions](#started-by-a-program). Open Codex threads, in the live list |
| Agents | Claude Code, Codex, Cursor, OpenCode, Grok, Copilot CLI and more | Claude Code. Codex: its open threads, their processes and its usage |
| How it gets state | It reads the bottom of the pane screen and the terminal title, and compares them with rules. It shows `blocked` only when a known approval, question or permission prompt is on screen. The Claude hook tells herdr which session runs in the pane, for resume; it does not report state | It starts from Claude Code's own status (`claude agents --json`) and corrects it from the transcript: a tool call with no answer, the last line of a finished turn, turn-end records. It also reads the process tree ([what actually needs you](#what-actually-needs-you), [busy, but the turn is over](#busy-but-the-turn-is-over)) |
| States | blocked, working, done (not seen yet), idle, unknown | NEEDS YOU, ASKED YOU, FINISHED, STUCK, STOPPED, busy, running, program. FINISHED: a turn ended, nothing runs under it, and you have not gone to the session from the list since. The live list puts it in NEEDS YOU |
| What a row tells you | The state, the agent, and the workspace and tab names. A config line adds the agent's terminal title. Scripts and plugins can add fields | Live list: state, name, title, age, ports, account (with two or more Claude accounts), and Claude's recap with its age (with no recap, the last tool call; under SAID, the message that matched). → adds the last thing you said and what Claude said last. `ccwho ls` shows the last tool call, or the question it asks |
| Go to a session | Click an agent in the sidebar, or use the Goto picker (`prefix+g`), which filters agents by state | ⌥/ opens the list from any app (an iTerm2 hotkey window). Enter brings the session's window to the front, or opens a window that attaches a background session. Not for Codex threads ([finding the window](#finding-the-window)) |
| Client closes or SSH drops | Processes keep running in the herdr server | Closing ccwho does not touch the sessions. If the terminal app quits or SSH drops, the sessions in it end, as without ccwho. `claude --bg` sessions keep running |
| Reboot | Saves the layout and the directories a few seconds after each change, and restores them. With the herdr integration installed, resumes Claude (`claude --resume <id>`), Codex and other supported agents. Also keeps up to 48 older layouts (at most one per 15 min) to restore by hand | A launchd job saves every 15 min and keeps 20 saves. `restore --open` types the resume line into the panes that iTerm2 restored; other sessions open in a new window, and sessions that Claude Desktop or a program ran stay where they ran. `restore --check` says whether each saved session that `--open` would reopen can be reopened ([save / restore](#ccwho-save--restore---a-reboot-stops-being-a-one-way-door)) |
| Processes and ports | Not built in | `ps`, `ps --port`, `kill`, `clean`. It shows which session started each process ([processes and ports](#processes-and-ports)) |
| Stuck work | Not built in | Finds a wait loop on a task that has ended (`until grep …; do sleep …; done`) and stdin readers that can never end, and gives you a box to kill them ([busy, but the turn is over](#busy-but-the-turn-is-over)) |
| Old sessions | Not built in (the `memex` plugin searches transcripts) | `ls`, `show` and the live list's `/` search an index of every transcript in `~/.claude`, in the `CLAUDE_CONFIG_DIR` ccwho runs with, and in the dirs you list in `~/.ccwho/roots` (Claude Code keeps 30 days by default); `/` also greps what was said ([finding an old session](#finding-an-old-session)) |
| Usage limits | Plugins (`herdr-agent-usage`) | 5h and 7d use for each account used in the last two weeks, with pace and age, after `ccwho setup` adds its statusLine. Also Codex's ([subscription usage](#subscription-usage)) |
| Agents control agents | Yes. With the socket API and CLI, an agent can open panes, send prompts, read output, wait until another agent is blocked, and show notifications | No. An agent cannot send a prompt to a session or read its screen. It can read the state of every Claude Code session with `ccwho ls --json`. When an agent runs `kill` or `clean`, they take only what its own session started. An agent stops no session |
| Remote machines | Yes: several SSH hosts in one window. A phone works through any SSH client | No. Local Mac only |
| Platforms | macOS, Linux, Windows; any terminal | macOS. It lists sessions in any terminal; only iTerm2 and Terminal.app windows can be brought to the front |
| Extensions | Plugin marketplace, more than 1,400 plugins | None |

## The live list

```
ccwho                         # on a terminal
⌥/                            # from any app, after `ccwho setup`
```

| key | does |
|---|---|
| ↑ ↓ / `j` `k` | move |
| Enter | go to the session's window - or give it one (see below). On a Codex row it says where the thread runs |
| click | a click on a row goes to the session; a click on the `\ /` at the row's right edge opens its brief |
| → | the brief: what it was working on, and the processes it started - ↑ ↓ still move between sessions under it |
| → again | into the brief, as in Finder's column view: ↑ ↓ move between its values, Enter copies one, ← back to the list. In a window too narrow for both, the brief covers the list: there → goes straight in, and one ← closes it |
| ← / Esc | back |
| `/` | search every name a session has, plus what it is about - and `:3000` finds the session holding that port. Ended sessions it finds show after the running ones, under ENDED (not those a program started), found as `ccwho ls` finds them: type any part of the id a `claude --resume <id>` line printed. Enter on one reopens it in a new window, in its own folder. Half a second after the last key it also greps what was said in each session - your prompts and Claude's replies, not what tools printed or what Claude Code stores there (any case; every word in the same session; a word finds its forms too, `wake` finds woke, woken and waking) - and lists the sessions only that finds under SAID, last: the closest first, each row showing the message that matched |
| `p` | every process agents started, grouped: each session and open Codex thread, left behind, Codex, not sure. The keys go to the first process: ↑ ↓ move, Esc or ← goes back |
| `x` | on a process (after `p`, or in a Codex thread's brief): kill it and what runs under it. On the left-behind heading, or a click on the left-behind line: clean what ended sessions left. A box lists everything the kill takes first - see [ccwho kill](#ccwho-kill-and-ccwho-clean---you-see-the-list-then-you-decide). The footer says what `x` does on the current line |
| `x` | on a session row: a box of what fits it - its stuck loop or reader first, with why, then all its processes, then a stop for a background session. In the box: `l` the stuck items, `p` its processes, `s` stop. On a row with a stuck loop or reader (a STUCK row, or one that also needs you), a click on `[kill stuck process…]` opens the kill box about the stuck items alone: what each is, why it can never end, that the session keeps running - `y` kills them, and what the kill did stays in the box |
| `o` | a menu of every save, newest first, with how many of its sessions run now and how many it would reopen; the last save before the restart is marked. Running sessions, and those Claude Desktop or a program ran, are left alone |
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
iTerm2 (hold ⌥ while you drag for iTerm2's own selection). Instead, each value in
the detail - the id, project, title, recap, what it opened with, what you said,
what it said, the tty, pid, name, cwd, session id and the `claude --resume` line -
lights up under the mouse, as what you can click on the list does: a left click
copies it, whole even where the pane shows it cut, and the status line says what
it copied, and leaves the keys where they were. The keys do the same: → a second
time, ↑ ↓ to the value, Enter; while the keys are in the brief, the one light is
theirs and the mouse moves nothing. What you paste is what the pane shows,
without the terminal codes a session printed. Labels and progress steps copy
nothing.

It notices a session start needing you within a second, by stat-ing transcript
files - no program is started for that - and does a full scan every 20 seconds.
Collection runs in a worker thread, because `claude agents --json` can take 30
seconds when it is unhappy and a list that freezes when you reach for it is the
problem this exists to solve.

**The hotkey.** `ccwho setup` adds an iTerm2 hotkey window running the list, on ⌥/
by default (`ccwho setup --hotkey option-w` to change it: `option-slash`, `option-space` or `option-w`). A hotkey is only global if
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
window running `claude attach <short id>` - in the app the session was last in -
and the row blinks while it opens.
It is never resumed: `claude --resume` on a running session starts a second process
on one transcript.

`claude attach` takes the **short** id - the 8 hex that `claude agents` lists - not
the session UUID. Given the UUID it says `No job matching`, about a session that is
running fine, while `--resume` correctly refuses it: every way in fails and the
session looks lost.

To end one: `claude stop <short id>` (the conversation is kept, and `--resume` works
after), or `claude rm <short id>` to delete it. `ccwho stop <id>`, or `x` then `s` in
the list, does the same stop, and asks first. The session keeps the directory it
was started in wherever you attach from; `--resume` uses the directory you are in.

### Codex threads

An open Codex thread is a row in the live list: `Title (codex) · 3m · VS Code` - its
name, its age, where it was typed (VS Code, the codex CLI, the ChatGPT app), its
folder and its ports. A thread is open while a Codex process holds its lock file;
for its row ccwho reads only `session_index.jsonl`, the first line of its transcript
and its last turn. Its state: ASKED YOU, FINISHED or "waiting? (maybe an approval)"
(a turn quiet for 2 minutes on a tool call - Codex writes no approval request, so
ccwho cannot tell which) - all in the NEEDS YOU group - busy, STOPPED, or, for a
`codex exec` thread, program under PROGRAMS. Enter says
where it runs: ccwho cannot bring a Codex window forward. `x` kills its processes,
after the usual box; end the thread in its app. `ccwho ls` and `--json` list Claude
Code sessions only.

## Subscription usage

```
usage  ant:work 5h 42%↓/60% ↻15:30 · 7d 2%↓/30% ↻Thu  │  ant:1a2b 5h expired ↻Wed · 7d 67%↑/40% ↻Mon (2d ago)
```

One dim line under the header, one entry per account read in the last two weeks: the
machine login by the name in its email (`ant:work` for work@…; more of it when two
names clash), a `CLAUDE_CODE_OAUTH_TOKEN` by the first 4 hex of its fingerprint. An account stays when no session spends it any more - switch accounts and
the one you left is still there, with its age - and leaves two weeks after its last
reading. `5h 42%↓/60%` is 42% of the 5-hour budget used with 60% of the 5 hours
gone: `↓` (green) is on pace, `↑` (red) is faster than time passes. The used number
turns yellow at 80% and bold at 95%. A window past its reset says `expired`; a window
whose newest reading is older than 15 minutes says how old, and an entry whose windows
have all expired ends with the age of its newest reading. With two or more Claude accounts on
the line, each row ends with the account it spends, in the line's own words (`· ant:work`; `?`:
no reading yet), and the entry of the selected row's account is bright. The same line
and tags are in the live list and `ccwho ls`, and each session's own status bar
shows its entry.

Where it comes from: Claude Code hands a statusLine command the rate limits of the
account the session spends. `ccwho setup` offers to add `ccwho statusline` in each
config dir where you run claude yourself - it shows the change and asks - and
`ccwho statusline` records one small file per session in `~/.ccwho/usage/`. ccwho
never logs in. A token account is named by a hash of the token: Claude Code does not
pass the token to the statusLine, so `ccwho statusline` reads it from its claude
process's environment and keeps only the hash. A session reports after its next
reply - including sessions that were already running when setup added the line
(measured: they reload the settings). `ccwho usage` lists every account seen in the last
two weeks; `ccwho usage name <id> <label>` gives one a name. `ccwho setup --no-usage` turns it
off again: the line goes, Codex's entry with it, once no running session spends an account on it.

Codex has an entry too, after the Claude accounts, named by its limit:
`oai:codex 7d 69%↑/60% ↻Sun (42m ago)`. It is kept two weeks after its last reading, the same
as an account, a Codex thread open or not. ccwho reads it from Codex's own transcripts - the
`rate_limits` of their `token_count` events - with no app-server and no credential, and it
never parses what was said. `ccwho usage` lists it, and `ccwho usage name oai:codex
<label>` names it.

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

In the live list, `/` searches the same index: the ended sessions it finds show
under the running ones, under ENDED (ten at most, the heading says how many). Half a
second after the last key it also greps what was said - your prompts and Claude's
replies. The first search after the list starts reads every transcript once and keeps
what was said in memory; a later search reads only what was appended. Sessions only
that finds show under SAID (ten at most), the closest first: most of your words in one
message, then a message of yours before one of Claude's, then the words as you typed
them, then running before ended, then the newest. Each SAID row shows that message,
its age and who said it, in place of the recap.

A word also finds its forms, from the word to its forms: `wake` finds wakes, waking,
woke and woken; `merge` finds merging. Not the other way: `woken` finds only "woken",
and `coding` does not find every "Claude Code" - type the word itself. This holds in
`ls` and `show` too.

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
config without asking is how that happened in the first place. The live list
shows the first fault on its warning line, and looks every 5 minutes. It leaves
out two checks that `ccwho doctor` makes: Accessibility (that check is also
macOS's request for it, and the list does not ask for a permission unasked) and
iTerm2's own status hook (ccwho does not need it).

Right after the Mac wakes, it gives the autosave job one 15-minute window to run
before it says the job is not running: launchd does not run a job for the time
the Mac slept.

It has a Terminal.app line once the list has asked that app. After an ask of iTerm2
or Terminal.app timed out, its line says when ccwho asks again (`asks it again in 7m`).

## Sources

| What | Where |
|---|---|
| session list, status | `claude agents --json`, plus `sessions/<pid>.json` in every config dir |
| the topic | `~/.claude/projects/*/<sessionId>.jsonl` (head + tail read only) |
| the recap | the same transcript, `system` / `away_summary` records |
| old sessions | the index, `~/.ccwho/` |
| processes an agent started | the session id in each process's environment (`sysctl`) |
| ports | `lsof -iTCP -sTCP:LISTEN` |
| open Codex threads | the lock files in `~/.codex/thread-writer-locks` (`lsof`), `session_index.jsonl`, each transcript's first line and last turn |
| usage | `~/.ccwho/usage/` (from `ccwho statusline`); Codex's from its transcripts |
| a session's iTerm2 pane (save) | iTerm2, by Apple Event; while iTerm2 is not asked, `ITERM_SESSION_ID` in its process's environment, else the last save of this boot |
| Terminal.app tabs | `ps` (the `login` on each tty); their names by Apple Event |

`sessionId` from the agents feed **is** the transcript filename, which is what makes
the topic column possible.

`claude agents --json` only lists the sessions of the config dir it runs under, so a
session started with its own `CLAUDE_CONFIG_DIR` never showed up - measured, 2 of 17
running sessions were missing. Each running `claude` names its config dir in its
environment, and each config dir keeps a small file per live session, so ccwho reads
those as well (read-only: pointed at another dir, `claude agents` writes into it). A
file counts only while its process is alive and is still the same process - its start
time, as `ps` prints it in UTC, must match the one the file recorded. From the
environment ccwho keeps five named variables, the iTerm2 pane id and a hash of an
OAuth token, and drops the rest: it holds tokens, and ccwho's output is read by agents. `ccwho doctor` says if those files stop parsing.

## Processes and ports

Agents start dev servers and leave them running. The port you want is taken, and
the session that did it may not even know. Every process Claude Code starts carries
its session id in its environment (`CLAUDE_CODE_SESSION_ID`; Codex sets
`CODEX_THREAD_ID`), and the mark survives the process being orphaned. macOS hides the environment of its own programs (`/bin/zsh`,
`/bin/bash`, `/bin/cat`, Apple's `python3`), and a Bash tool shell is the user's
shell, one of them: so what runs under a listed live `claude` is that session's
work, mark or not. Once it has left that tree, a process with a hidden environment
is usually not known (unless its command line names the session) - `ccwho ps
--port` names the holder and exits 3 (`:5000 is held by ControlCenter (pid 1190)`).
So ccwho can say who started what:

```sh
ccwho ps                    # every process an agent started, its session, its ports
ccwho ps --port 3000        # who holds :3000 - exit 1 if no agent does, 3 if that is not known
ccwho ps --helpers          # ...including helpers (MCP servers and the like)
ccwho ps --json             # for scripts, and for agents told to clean up
ccwho ps --full             # the whole command line, not the short form
```

The live list and the table (`ccwho ls`) say it in one dim line
each, never louder than what needs you: `agents hold :3000 left behind · :5173 app` at the top, the
ports on each row, and at the bottom `left behind: N processes` (sessions that have
ended) and `codex: N processes` (processes of a Codex thread that is not open: whether
it still runs cannot be told cheaply, so they are never called left behind). An open
Codex thread is a row, and its processes and ports are its own: `:5173 app (codex)`. A
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
named variables are kept, with the iTerm2 pane id and a hash of an OAuth token - it
holds tokens, and ccwho's output is read by agents.
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

An ambiguous query lists the candidates rather than guessing. It goes to the app
whose tab shows that tty: iTerm2 (`tty of session` over its windows) or Terminal.app
(`tty` of each tab).

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

## ccwho kill and ccwho clean - you see the list, then you decide

```sh
ccwho kill :3000            # what holds the port, and what runs under it
ccwho kill 4412             # that process and its whole tree
ccwho clean                 # every process tree a session that ended left behind
ccwho kill :3000 --dry-run  # the list only
ccwho kill :3000 --yes      # no question (a script)
ccwho kill 4412 --force     # SIGKILL what SIGTERM did not end
ccwho kill liveapp-4e       # what that live session started (its id, name or tty)
ccwho kill 4412 --pid       # 4412 as a pid, not the start of a session id
ccwho stop liveapp-4e              # stop a background session; its conversation is kept
ccwho stop liveapp-4e --and-procs  # ...and kill what it started
```

Every kill lists each process it takes - who started it and what ccwho doubts
about it ("a claude runs under it", "Chrome is connected to :3000") - and asks.
With no terminal and no `--yes` it prints the list and exits 3. Some things are
never killed, for anyone: a live Claude session, an agent or what runs one,
ccwho itself and the shell that runs it, a process piped to an agent, and a pid
that is now a different process.

Right before each signal ccwho reads the machine again: it signals only what you
confirmed that is still the same process (start time, command, session), one pid
at a time, children first. SIGTERM first; what survives 3 s is named, and
`--force` sends it SIGKILL after the same check. Then it says whether the port
came free.

**Run by an agent** (a Claude Code or Codex session id in ccwho's environment),
ccwho never asks and takes only what that agent's own session started, and
nothing that carries a doubt: `ccwho clean --mine` cleans up after itself.

Exit codes: 0 all killed, 1 not all (or nothing to kill), 2 usage, 3 needs `--yes`,
130 interrupted before any signal (nothing killed). A `--dry-run` exits as the kill
would: 1 when part of the target would be refused.

`ccwho stop` stops only a background session, with `claude stop`; `claude attach`
brings it back. A session started in a window is never stopped: end it there
(`/exit`). A background session attached in a window is stopped, and the question
says it is open there. An agent stops no session. Exit codes: 0 stopped, 1 not (or
not all), 2 usage, several matches, or (with `--yes`) a word that is not its id, name
or tty, 3 needs `--yes`, 130 interrupted - after `claude stop` began, the session may
be stopped (run `ccwho ls`). A `--dry-run` exits as the stop would.

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
prompt, and clears a half-typed line there first. Every other terminal session - and
one whose pane closed before the write - opens in a new window, as before. `restore --check`
says how many of the saved panes are open now. A session saved from a Terminal.app
tab reopens in a new Terminal.app window ([#30](https://github.com/lukaso/ccwho/issues/30)).

The pane comes from iTerm2 when it answers, and from the session's own process when
it does not: iTerm2 puts `ITERM_SESSION_ID=w0t1p2:<pane id>` in every pane it starts,
so the save reads it there - no Apple Event. On 2026-10-03 iTerm2 left one ask
unanswered for 2 minutes and ccwho asked it nothing more for two days; the save
before the next reboot knew the pane of 3 of 12 sessions, and 9 opened in new windows.
Now a save finds the pane of each session whose own process runs in an iTerm2 pane.
One the Claude Code daemon runs (`claude --bg`, shown by `claude attach`) keeps the
pane the last save knew, and one in tmux has none. A stuck iTerm2 is asked again
after 10 minutes (doubling to 2 hours while it stays stuck; `ccwho doctor` says when).

**Not what Claude Desktop or a program ran.** Such a session is not reopened in a
terminal window. `restore --open` names it, with the `ccwho open <id>` that opens it
if you want it; `o` leaves it out of the sessions it would reopen and counts it in
what it says after; `ccwho restore` marks it "--open leaves it"; `restore --check`
names it as `--open` does and does not count it as restorable. On 2026-10-05 a
restore put two Claude Desktop sessions, idle for days, in new iTerm2 windows. A save
made before this version does not record what started a session, and reopens them
all, as before.

**It saves itself.** The reboot this exists for is usually the one you did not plan:
on 2026-09-05 a crash found a manifest five days old. So `ccwho setup` puts
`ccwho save` on a 15-minute launchd timer. The job is rendered from
`com.lukaso.ccwho.save.plist.template` for this machine - its home, its ccwho, the
directory its `claude` is in, because launchd's `PATH` has none of them - and
`ccwho doctor` says when the installed job no longer matches what setup would write.

`StartInterval` fires only while the Mac is awake and never wakes it; a firing
that falls in a sleep is missed, so after a wake the next run can be up to one
interval away (doctor allows for that). Manifests live in
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
- running, with no window -> **attach it** in a new window, with
  `claude attach` - the same guard, never a second process
- not running -> **reopen it** in a new window
- the live list could not be read -> **do nothing**: an empty list from a failed
  read looks exactly like "nothing is running"
- in neither the live fleet nor any manifest -> say so, do nothing

A new window opens in the app the session was last in (see
[Terminal.app](#terminalapp)).

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
ccwho ls [words] [--all]  # the table, or every session matching (with options, or piped, `ccwho` is this)
ccwho ls --needs-you    # only the Claude Code sessions the live list puts on top: NEEDS YOU and STUCK
ccwho ls --json         # the rows, for a script, a status line or an agent
ccwho show <anything>   # what that session was working on
ccwho ps [--port N] [--helpers] [--full]  # what agents started, and their ports
ccwho jump <tty|pid|title>  # focus that window
ccwho open <session-id> # focus it, give it a window, or reopen it
ccwho save              # record the live fleet (before a reboot)
ccwho restore [--open]  # list it back, or reopen the windows
ccwho restore --check   # would it restore? before a reboot
ccwho usage [name <id> <label>]  # usage per account, Codex too
ccwho kill <pid>|:<port>|<session> [--pid]  # a process tree, a port's holder, or what a session started - lists, then asks
ccwho clean [--mine]    # kill what ended sessions left behind - lists, then asks
ccwho stop <session> [--and-procs]  # stop a background session, keep its conversation - asks
ccwho doctor            # is everything ccwho needs in place?
ccwho setup             # install what it needs, once
```

## Columns

| Column | Where it comes from |
|---|---|
| name | the address you message the session by; its project when it has none |
| tty | where it runs; a link that jumps there |
| status | derived, see below - NEEDS YOU sorts to the top |
| title | the tab's name (iTerm2 or Terminal.app); `~` + Claude Code's own `ai-title` when no tab gives one. A renamed session's project comes first |
| doing | the last tool call, using Bash's human `description` when present |
| since | time since the last real **turn**, from its `timestamp` - see below |
| account | the account it spends, with two or more of one brand on the usage line |

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
| asks | the closing line of an **ended** turn is a question or a request - even while the harness says busy. Text in quotation marks does not count: a question put to someone else is not one put to you | ASKED YOU |
| stuck | not asking you, not mid-turn, and a wait loop or stdin reader under it can never end | STUCK |
| review | stopped, its turn ended, and you have not gone to it from the list since | FINISHED |
| stopped | not busy, and **nothing running under it** | STOPPED |
| busy | harness says busy, mid-turn | busy |
| running | background work is in flight: not busy, or busy with the turn over | running |
| program | a program started it (see [Started by a program](#started-by-a-program)), and it is not stuck | program |

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
**STUCK**, its own group after NEEDS YOU, and the row names the loop. A click on
`[kill stuck process…]`, or `x` then `l`, opens a box that lists it with why it can
never end; `y` there kills it and Esc kills nothing. The kill looks again first and
signals only a pid that is still that session's dead loop; what was not killed is
named with why - it still runs but did not look stuck, the signal was not
permitted, or the processes could not be read - never called "not stuck". Killing it reports the task
as failed to the session, which wakes the agent. A file that cannot be read, or
has no end line, is never called dead.

**A reader that waits for input no one sends** is stuck too. Claude Code runs a
Bash tool command with `< /dev/null` - except a command with a heredoc: then the
tool shell's stdin is a socket that the session's claude holds and never writes to.
A `cat $l` with `$l` empty, in such a command moved to the background, waited 20
hours while the row said `running`. So a stdin reader (`cat`, `tr`, `cut`, `head`,
`tail`, `wc`, `sort`, `uniq` with no file) in a Bash tool task, older than two
minutes, whose fd 0 lsof shows is the claude's socket, makes the row STUCK: `cat N
waits for input Claude Code never sends`. Never one under an MCP server (Claude Code
does write to that socket) or under an agent. Its kill box stops the reader's whole
task, parents first, so the tool shell cannot go on to its next command (a script
that traps TERM is not reached); ccwho and any agent in the task are spared.

A dead loop or reader makes the row STUCK whenever the session is not asking you
and not mid-turn - idle or waiting too, not only busy with the turn over.

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
the list answers "what does it want" without opening the session.

In the live list's NEEDS YOU group the kinds keep their order - a tool call waiting on you, then a
question, then a finished turn you have not looked at - and inside each kind the
**oldest** is first: you take the top one, so newest-first let each
fresh ask go above the one that had waited longest. An ask with no known time
comes after the ones that have one. The other groups, and the table (`ccwho ls`,
`--json`), sort each state most recent first.

### Started by a program

A session that a program started with the SDK (entrypoint `sdk-cli`, `sdk-py`,
`sdk-ts`, `sdk`) never needs you: mid tool call, its unanswered `tool_use` reads
like a permission prompt, but the program answers its own prompts, and it has no
window because it never had one. Such a session has its own state, `program`, in a
last, quiet PROGRAMS group. It is never in NEEDS YOU; `ccwho ls --needs-you` shows it
only when it is STUCK. Its row says
`· program` where a row would say `· no window`. Enter, `ccwho open` and the link handler do not attach it. A loop
that can never end still makes it STUCK. Every row in `--json` carries its
`entrypoint`. A Codex thread that `codex exec` started is a program's too: it shows
under PROGRAMS.

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
and both were regressions I had to fix after the old `--watch` loop burned half a core:

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

`ccwho.py` is the runner and the commands. The rules are in `ccwho_engine.py` and
the modules it imports (text, processes, terminal apps, brief, index, usage); all of
them are re-read on every tick of the live list. Edit the engine while the list is
open and the next tick picks it up - no restart. Same model as
`network_check_ruby`: thin loop, hot-reloaded engine, state carried between ticks
rather than held inside the engine.

A broken edit does not kill the loop: the reload error is caught, the last good
module keeps rendering, and the error shows as a banner until you fix it.

Implication, as with the Ruby version: keep the engine free of long-lived objects.
Pure functions and plain dicts only.

The runner itself is not reloaded: after an update that changes `ccwho.py`, restart
every open `ccwho` (list or watch). One left running reads what the new code writes,
such as a launch claim naming Terminal.app, by its old rules.

## Design notes

- **Transcripts reach hundreds of MB.** Never read one whole: head for the first
  prompt, tail for the last.
- **Harness-injected "user" turns are not topics.** Skill preambles, compaction
  notices, `<task-notification>` blocks and the local-command caveat are filtered.
  In strict mode the search window widens rather than printing noise.
- **macOS has hundreds of system daemons on PID 1.** A process is a session's work
  only by the session id in its environment, or by running under that session's
  claude - never because it sits on PID 1: counting PID 1's children reported 582
  and meant nothing.
- **`age()` guards epoch 0**, because `if not started_ms` swallows a valid timestamp.

## Tests

```sh
./test
```

The engine's tests use the standard library only; `uv` runs the live list's tests
with Textual. If uv cannot fetch Textual the UI tests FAIL rather than skip -
"OK (skipped=N)" while the screen is broken is a green light for nothing. Every guard has been mutation-checked: reverting the fix it
defends turns its test red. An assertion that cannot fail is not an assertion.

## License

MIT.
