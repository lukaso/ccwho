#!/usr/bin/env python3
"""ccgate - admission control for expensive commands run by many sessions at once.

The problem it solves: N Claude Code sessions each decide to run the deploy gate.
All N start at once, the box thrashes, several fail on timing, and then every
session sits waiting for the machine to quieten - having burned the work twice.

ccgate puts a slot semaphore and a load ceiling in front of the command. Slots are
files claimed with O_CREAT|O_EXCL under ~/.ccwho/gates/, so it works across
unrelated processes with no daemon. A slot whose holder died is reclaimed.

    ccgate -- bash scripts/check.sh
    ccgate --slots 2 --label "liveapp gate" -- bash scripts/check.sh
    ccgate --status

Env: CCGATE_SLOTS, CCGATE_LOAD_FACTOR (0 disables the load ceiling), CCGATE_DIR.
"""
from __future__ import annotations

import json
import os
import random
import signal
import subprocess
import sys
import time

DEFAULT_SLOTS = int(os.environ.get("CCGATE_SLOTS", "2"))
DEFAULT_FACTOR = float(os.environ.get("CCGATE_LOAD_FACTOR", "0.75"))
GATE_DIR = os.environ.get("CCGATE_DIR", os.path.expanduser("~/.ccwho/gates"))


# ------------------------------------------------------------------ primitives

def load_ok(load1, cores, factor=DEFAULT_FACTOR):
    """Admit only when the 1-minute load is under cores*factor. factor 0 disables."""
    if not factor or not cores:
        return True
    return load1 <= cores * factor


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def claim_slot(gate_dir, n_slots, pid, label):
    """Atomically claim a free slot. Returns its path, or None if all are held."""
    os.makedirs(gate_dir, exist_ok=True)
    for i in range(n_slots):
        path = os.path.join(gate_dir, f"slot-{i}.json")
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        except OSError:
            continue
        with os.fdopen(fd, "w") as fh:
            json.dump({"pid": pid, "label": label, "since": time.time()}, fh)
        return path
    return None


def release_slot(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def reap_stale(gate_dir, alive=pid_alive):
    """Reclaim slots whose holder is gone, or whose record is unreadable."""
    reaped = 0
    try:
        names = os.listdir(gate_dir)
    except OSError:
        return 0
    for name in names:
        if not name.startswith("slot-"):
            continue
        path = os.path.join(gate_dir, name)
        try:
            with open(path) as fh:
                rec = json.load(fh)
            holder_ok = alive(rec.get("pid"))
        except (OSError, ValueError, TypeError):
            holder_ok = False
        if not holder_ok:
            release_slot(path)
            reaped += 1
    return reaped


def gate_status(gate_dir):
    out = []
    try:
        names = sorted(os.listdir(gate_dir))
    except OSError:
        return out
    for name in names:
        if not name.startswith("slot-"):
            continue
        try:
            with open(os.path.join(gate_dir, name)) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        rec["slot"] = name
        rec["alive"] = pid_alive(rec.get("pid"))
        out.append(rec)
    return out


# ------------------------------------------------------------------------- CLI

def _cores():
    try:
        return os.cpu_count() or 0
    except Exception:
        return 0


def _load1():
    try:
        return os.getloadavg()[0]
    except (OSError, AttributeError):
        return 0.0


def _arg(argv, flag, default):
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--help" in argv or "-h" in argv or not argv:
        print(__doc__.strip())
        return 0

    if "--status" in argv:
        reap_stale(GATE_DIR)
        held = gate_status(GATE_DIR)
        cores, load1 = _cores(), _load1()
        print(f"load {load1:.2f} / ceiling {cores * DEFAULT_FACTOR:.1f} ({cores} cores)")
        if not held:
            print("no gate slots held")
        for h in held:
            print(f"  {h['slot']}  pid {h['pid']:<7} {h.get('label', '')}"
                  f"  held {int(time.time() - h.get('since', time.time()))}s")
        return 0

    slots = int(_arg(argv, "--slots", DEFAULT_SLOTS))
    factor = float(_arg(argv, "--load-factor", DEFAULT_FACTOR))
    label = _arg(argv, "--label", os.path.basename(os.getcwd()))
    for flag in ("--slots", "--load-factor", "--label"):
        if flag in argv:
            i = argv.index(flag)
            del argv[i:i + 2]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        print("ccgate: nothing to run", file=sys.stderr)
        return 2

    cores = _cores()
    held = None
    waited = 0.0
    announced = False
    try:
        while True:
            reap_stale(GATE_DIR)
            load1 = _load1()
            if load_ok(load1, cores, factor):
                held = claim_slot(GATE_DIR, slots, os.getpid(), label)
                if held:
                    break
                reason = f"all {slots} gate slots busy"
            else:
                reason = f"load {load1:.1f} over ceiling {cores * factor:.1f}"
            if not announced or waited % 60 < 5:
                others = ", ".join(f"{h['pid']}:{h.get('label', '')}" for h in gate_status(GATE_DIR))
                print(f"ccgate: waiting ({reason}) {int(waited)}s"
                      + (f" · holders: {others}" if others else ""), file=sys.stderr, flush=True)
                announced = True
            nap = 5 + random.uniform(0, 3)  # jitter so queued waiters don't wake in lockstep
            time.sleep(nap)
            waited += nap

        if waited:
            print(f"ccgate: admitted after {int(waited)}s", file=sys.stderr, flush=True)

        def _bail(signum, _frame):
            release_slot(held)
            raise SystemExit(128 + signum)

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                signal.signal(sig, _bail)
            except (OSError, ValueError):
                pass
        return subprocess.call(argv)
    finally:
        if held:
            release_slot(held)


if __name__ == "__main__":
    raise SystemExit(main())
