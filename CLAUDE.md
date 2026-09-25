# CLAUDE.md

## Mutant and hang tests: stop the process itself, not its wrapper

A mutant that can hang must be stopped by a timeout on the `python3` process itself. A `kill` sent to a subshell or a pipeline leaves the Python process running with no parent, and it keeps running.

This crashed the machine on 2026-09-25. The "no cycle guard" mutant of `find_dead_loops` sent the process walk into a loop that never ended. The harness ran it as:

```sh
# WRONG: kill $P stops the subshell only; python3 keeps running
( python3 -m unittest $T 2>&1 | grep … ) & P=$!
(sleep 30; kill $P && echo "TIMEOUT (hang = caught)") & wait $P
```

It printed `TIMEOUT (hang = caught)`, but the Python process kept running. In 5 hours it grew to 67 GB, and the 64 GB Mac had a kernel panic (watchdog timeout, memory compressor full). The Mac then needed several forced restarts before it booted.

Do this instead:

```sh
# RIGHT: the timeout stops the python3 process itself (exit code 137 = it hung)
timeout -s KILL 30 python3 -m unittest $T 2>&1 | grep …
```

Rules:

- Put `timeout -s KILL N` directly in front of `python3`. Do not put it in front of a subshell, `bash -c`, `uv run` or a pipeline. (`timeout` is GNU coreutils: `brew install coreutils`.)
- Keep the timeout short (30 s or less). On macOS it is the only memory limit: `ulimit -v` fails there (`setrlimit failed: invalid argument`). The 2026-09-25 process grew by about 18 MB/s, so 30 s is about 0.5 GB.
- After each hang cell, confirm that the process stopped: `pgrep -fl unittest` must print nothing.
- A harness that says "caught" but leaves the process running has not caught the hang. Test a new harness on a known hang first, and confirm with `pgrep` that nothing is still running.
