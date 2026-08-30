#!/usr/bin/env python3
"""ccwho - which Claude Code session needs you, and what is it about.

This file is the RUNNER and is meant to stay small. All logic lives in
ccwho_engine.py, which is reloaded on every tick of --watch, so you can edit the
engine while a watch is running and the next tick picks it up without a restart.
Same model as network_check_ruby: thin loop, hot-reloaded engine, state carried
between iterations rather than held inside the engine.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import time
import traceback

import ccwho_engine as engine

CLEAR_HOME = "\033[H\033[2J"
DIM, RESET, RED = "\033[2m", "\033[0m", "\033[31m"


def reload_engine(state):
    """Re-read the engine. A broken edit keeps the last good module and says so."""
    try:
        importlib.reload(engine)
        state["engine_error"] = ""
    except Exception:
        state["engine_error"] = traceback.format_exc(limit=2).strip().splitlines()[-1]
    return engine


def tick(state, argv, color):
    eng = reload_engine(state) if state["watch"] else engine
    rows, total_orphans = eng.collect()
    if "--blocked" in argv:
        rows = [r for r in rows if r["status"] == "waiting" or r["orphans"]]
    out = eng.render(rows, total_orphans, color=color,
                     show_prompt="--prompt" in argv or "-p" in argv)
    state["ticks"] += 1
    return out, rows


def parse_interval(argv, default=5.0):
    for i, a in enumerate(argv):
        if a == "--watch" and i + 1 < len(argv):
            try:
                return max(1.0, float(argv[i + 1].rstrip("s")))
            except ValueError:
                return default
    return default


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--help" in argv or "-h" in argv:
        print(__doc__.strip())
        print("\nusage: ccwho [--watch [secs]] [--blocked] [--prompt] [--json] [--no-color]")
        return 0

    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and "--no-color" not in argv
    state = {"ticks": 0, "engine_error": "", "watch": "--watch" in argv}

    if "--json" in argv:
        rows, _ = engine.collect()
        print(json.dumps(rows, indent=2))
        return 0

    if not state["watch"]:
        out, _ = tick(state, argv, color)
        sys.stdout.write(out)
        return 0

    interval = parse_interval(argv)
    try:
        while True:
            started = time.time()
            out, _ = tick(state, argv, color)
            banner = ""
            if state["engine_error"]:
                banner = f"{RED if color else ''}engine reload failed: {state['engine_error']}{RESET if color else ''}\n"
            footer = (f"{DIM if color else ''}tick {state['ticks']} · every {interval:g}s · "
                      f"{time.strftime('%H:%M:%S')} · ctrl-c to stop{RESET if color else ''}\n")
            sys.stdout.write(CLEAR_HOME + banner + out + "\n" + footer)
            sys.stdout.flush()
            time.sleep(max(0.0, interval - (time.time() - started)))
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
