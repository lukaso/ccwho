"""Tests for the signaller: what ccwho does between "yes" and the report.

A person confirmed a list from one scan. Right before any signal ccwho reads
the world again and plans again with the same target: a pid is signalled only
when the fresh plan takes it too, with the same start and the same mark. It
signals pids, never a group, leaves first; SIGTERM, then it says what
survived; SIGKILL only with force. Every signal here goes to a fake, except in
TestTheRealSignal, which signals only processes it started itself."""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import ccwho_engine as engine
import ccwho_procs as procs

T = "Tue Sep 22 13:45:15 2026"
T2 = "Wed Sep 23 09:00:00 2026"
LIVE = "aaaa1111-0000-4000-8000-000000000001"
DEAD = "bbbb2222-0000-4000-8000-000000000002"
OTHER = "cccc3333-0000-4000-8000-000000000003"


def world(extra=None, marks=None, drop=(), ports=None):
    """A live session 10 (with its work 11), an orphan tree it left behind
    20 -> 21 -> 22 holding :3000 at 22, a second orphan 30 on :5173, and
    ccwho at 99."""
    table = {1: (0, T, "/sbin/launchd"),
             10: (1, T, "/Users/x/.local/bin/claude"),
             11: (10, T, "node server.js"),
             20: (1, T, "npm run dev"),
             21: (20, T, "node vite"),
             22: (21, T, "esbuild --service"),
             30: (1, T, "python3 -m http.server 5173"),
             99: (1, T, "python3 ccwho.py kill 20")}
    table.update(extra or {})
    for p in drop:
        table.pop(p, None)
    m = {1: None, 10: None, 11: ("claude", LIVE), 20: ("claude", DEAD), 21: ("claude", DEAD),
         22: ("claude", DEAD), 30: ("claude", DEAD), 99: None}
    m.update(marks or {})
    m = {k: v for k, v in m.items() if k in table}
    ports = {22: [3000], 30: [5173]} if ports is None else ports
    ports = {k: v for k, v in ports.items() if k in table}
    sessions = [{"sessionId": LIVE, "pid": 10}]
    att = procs.attribute(table, m, ports, sessions, own=99)
    return {"table": table, "att": att, "sessions": sessions, "own": 99, "ports": ports,
            "pipes": {q: set() for q in table}, "marks": m, "connections": []}


def plan(mode="pid", target=None, w=None):
    return procs.kill_plan(mode, target or {"pid": 20, "start": T}, w or world())


class TestTheFixture(unittest.TestCase):
    def test_the_tree_is_what_a_pid_kill_takes(self):                  # control for all below
        self.assertEqual(sorted(e["pid"] for e in plan()["kill"]), [20, 21, 22])
        self.assertIsNone(procs.world_problem(world()))


class TestStillToKill(unittest.TestCase):
    """Only what the person confirmed AND the fresh plan takes, unchanged."""

    def check(self, fresh_world, confirmed=None, fresh=None):
        confirmed = plan()["kill"] if confirmed is None else confirmed
        fresh = plan(w=fresh_world) if fresh is None else fresh
        return procs.still_to_kill(confirmed, fresh, fresh_world["table"])

    def go(self, r):
        return [e["pid"] for e in r["go"]]

    def spared(self, r):
        return {e["pid"]: e["why"] for e in r["spare"]}

    def test_nothing_changed_all_go_leaves_first(self):
        r = self.check(world())
        self.assertEqual(self.go(r), [22, 21, 20])
        self.assertEqual((r["spare"], r["new"]), ([], []))

    def test_a_gone_pid_is_said_so(self):
        r = self.check(world(drop=(22,)))
        self.assertEqual(self.go(r), [21, 20])
        self.assertEqual(self.spared(r), {22: "already exited - nothing to do"})

    def test_a_reused_pid_is_not_signalled(self):
        r = self.check(world(extra={21: (20, T2, "node vite")}))
        self.assertNotIn(21, self.go(r))
        self.assertIn("now a different process - not killed", self.spared(r)[21])

    def test_a_changed_mark_is_not_signalled(self):
        r = self.check(world(marks={21: ("claude", OTHER)}))
        self.assertNotIn(21, self.go(r))
        self.assertIn(21, self.spared(r))

    def test_a_mark_that_is_gone_is_not_signalled(self):
        r = self.check(world(marks={22: None}))
        self.assertNotIn(22, self.go(r))
        self.assertIn(22, self.spared(r))

    def test_a_guard_that_now_holds_spares_with_its_reason(self):
        # 21 became a live session since the list: the fresh plan refuses it
        w = world()
        w["sessions"].append({"sessionId": OTHER, "pid": 21})
        w["att"] = procs.attribute(w["table"], w["marks"], w["ports"], w["sessions"], own=99)
        fresh = plan(w=w)
        r = self.check(w, fresh=fresh)
        self.assertEqual(self.go(r), [])
        fresh_why = {e["pid"]: e["why"] for e in fresh["spare"]}
        self.assertEqual(self.spared(r)[21], fresh_why[21])

    def test_a_whole_plan_refusal_spares_all(self):
        w = world()
        w["own"] = "99"                          # out of shape: the fresh plan refuses whole
        fresh = procs.kill_plan("pid", {"pid": 20, "start": T}, w)
        r = procs.still_to_kill(plan()["kill"], fresh, world()["table"])
        self.assertEqual(self.go(r), [])
        self.assertEqual(sorted(self.spared(r)), [20, 21, 22])
        self.assertTrue(all("not in its shape" in why for why in self.spared(r).values()))

    def test_no_longer_part_of_the_kill(self):
        # 22 moved under 30 (reparented): alive, same start, not in the fresh tree
        r = self.check(world(extra={22: (30, T, "esbuild --service")}))
        self.assertEqual(self.go(r), [21, 20])
        self.assertEqual(self.spared(r), {22: "no longer part of this kill - not killed"})

    def test_a_command_changed_since_the_list_is_not_signalled(self):
        # an exec keeps pid, start and mark: the command is what tells it
        r = self.check(world(extra={21: (20, T, "python3 other.py")}))
        self.assertNotIn(21, self.go(r))
        self.assertEqual(self.spared(r)[21],
                         "now runs another command - not killed; run ccwho ps again")

    def test_new_since_the_list_is_not_signalled(self):
        r = self.check(world(extra={23: (22, T, "esbuild worker")}, marks={23: ("claude", DEAD)}))
        self.assertEqual(self.go(r), [22, 21, 20])
        self.assertEqual([e["pid"] for e in r["new"]], [23])

    def test_a_reused_pid_that_is_new_is_new(self):
        # pid 22 reused by a fresh child: not confirmed, not signalled
        w = world(extra={22: (21, T2, "esbuild --service")})
        r = self.check(w)
        self.assertNotIn(22, self.go(r))

    def test_only_confirmed_pids_go(self):
        # the person was shown 20 and 21 only
        confirmed = [e for e in plan()["kill"] if e["pid"] != 22]
        r = self.check(world(), confirmed=confirmed)
        self.assertEqual(self.go(r), [21, 20])
        self.assertEqual([e["pid"] for e in r["new"]], [22])

    def test_an_empty_start_is_no_match(self):
        confirmed = [dict(e, start="") if e["pid"] == 21 else e for e in plan()["kill"]]
        r = self.check(world(), confirmed=confirmed)
        self.assertNotIn(21, self.go(r))

    def test_an_empty_start_in_both_is_no_match(self):
        # kill_plan refuses a pid without a start today; this does not lean on it
        e = dict(plan()["kill"][0], start="")
        r = procs.still_to_kill([e], {"kill": [e], "spare": []}, {e["pid"]: (1, "", "x")})
        self.assertEqual(r["go"], [])
        self.assertIn("now a different process", r["spare"][0]["why"])

    def test_what_goes_is_the_fresh_entry(self):
        # the report and the port check read the fresh ports, not the listed ones
        r = self.check(world(ports={22: [3000, 3001], 30: [5173]}))
        self.assertEqual([e["ports"] for e in r["go"] if e["pid"] == 22], [[3000, 3001]])


class TestMarkedOf(unittest.TestCase):
    """The signaller reads a mark again in the form the plan listed it."""

    def test_the_form_kill_plan_lists(self):
        odd = "x\u202e" + "y" * 80
        w = world(marks={20: ("claude", odd), 21: ("claude", odd), 22: ("claude", odd)})
        listed = {e["pid"]: e["marked"] for e in plan(w=w)["kill"]}
        self.assertEqual(listed[21], procs.marked_of({"CLAUDE_CODE_SESSION_ID": odd}))

    def test_no_mark(self):
        self.assertIsNone(procs.marked_of({}))
        self.assertEqual(procs.marked_of({"CODEX_THREAD_ID": "t1"}), "t1")


class TestIdentityOf(unittest.TestCase):
    """How identity_of reads `ps -o stat=,lstart=,command= -p PID`: (start,
    command) - an exec keeps pid and start, not the command. Gone is seen,
    never assumed."""

    def ps(self, rc, out, err=""):
        real = engine.subprocess.run
        engine.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a[0], rc, out, err)
        self.addCleanup(setattr, engine.subprocess, "run", real)

    def test_a_running_process(self):
        self.ps(0, f"Ss   {T}    node  vite --port 5173\n")
        self.assertEqual(engine.identity_of(70), (T, "node vite --port 5173"))

    def test_no_such_process(self):
        self.ps(1, "")
        self.assertIsNone(engine.identity_of(70))

    def test_a_zombie_has_ended(self):
        self.ps(0, f"Z    {T} (node)\n")
        self.assertIsNone(engine.identity_of(70))

    def test_ps_said_something_else(self):
        self.ps(1, "", "ps: process id too large: 99999999\n")
        self.assertRaises(OSError, engine.identity_of, 70)
        self.ps(0, f"Ss {T}\n")
        self.assertRaises(OSError, engine.identity_of, 70)
        self.ps(2, f"Ss {T} node\n")
        self.assertRaises(OSError, engine.identity_of, 70)

    def test_ps_could_not_run(self):
        real = engine.subprocess.run

        def boom(*a, **k):
            raise FileNotFoundError("ps")
        engine.subprocess.run = boom
        self.addCleanup(setattr, engine.subprocess, "run", real)
        self.assertRaises(OSError, engine.identity_of, 70)


class TestLeavesFirst(unittest.TestCase):
    def test_deeper_first(self):
        table = {1: (0, T, "l"), 5: (1, T, "a"), 6: (5, T, "b"), 7: (6, T, "c"), 8: (5, T, "d")}
        self.assertEqual(procs.leaves_first([5, 6, 7, 8], table), [7, 6, 8, 5])

    def test_a_pid_not_in_the_table_is_last(self):
        table = {1: (0, T, "l"), 5: (1, T, "a"), 6: (5, T, "b")}
        self.assertEqual(procs.leaves_first([9, 5, 6], table), [6, 5, 9])

    def test_a_loop_does_not_hang(self):
        table = {5: (6, T, "a"), 6: (5, T, "b")}
        self.assertEqual(sorted(procs.leaves_first([5, 6], table)), [5, 6])


class TestPortReport(unittest.TestCase):
    TABLE = {21: (20, T, "node vite"), 22: (21, T, "esbuild --service"), 40: (1, T, "caddy run")}

    def test_a_free_port(self):
        self.assertEqual(procs.port_report([3000], {}, self.TABLE, [20, 21, 22]), [":3000 is free"])

    def test_a_port_still_held(self):
        self.assertEqual(procs.port_report([3000], {22: [3000]}, self.TABLE, [20, 21]),
                         [":3000 still held by 22 esbuild --service (child of 21)"])

    def test_a_parent_not_signalled_is_not_named(self):                # control
        self.assertEqual(procs.port_report([3000], {22: [3000]}, self.TABLE, [20]),
                         [":3000 still held by 22 esbuild --service"])

    def test_held_by_another(self):
        self.assertEqual(procs.port_report([3000], {40: [3000]}, self.TABLE, [22]),
                         [":3000 still held by 40 caddy run"])

    def test_ports_not_read(self):
        self.assertEqual(procs.port_report([3000, 5173], None, self.TABLE, [22]),
                         [":3000 :5173 - ccwho could not read the ports again"])

    def test_each_port_once_in_order(self):
        self.assertEqual(procs.port_report([5173, 3000, 5173], {}, self.TABLE, []),
                         [":3000 is free", ":5173 is free"])

    def test_no_ports_no_lines(self):
        self.assertEqual(procs.port_report([], None, self.TABLE, []), [])

    def test_a_reused_pid_is_not_named_by_its_old_command(self):
        # 22 was seen gone after the kill; a new process has its pid and the port
        self.assertEqual(procs.port_report([3000], {22: [3000]}, self.TABLE, [21, 22], ended=[22]),
                         [":3000 still held by 22"])

    def test_a_holder_not_in_the_table(self):
        self.assertEqual(procs.port_report([3000], {77: [3000]}, self.TABLE, []),
                         [":3000 still held by 77"])


class Machine:
    """A fake machine for carry_out: the worlds it reads, one per build (the
    last repeats), each process as (start, command), and which signals end
    which pids."""

    def __init__(self, worlds, ends=None, trouble=None):
        self.worlds, self.ends, self.trouble = list(worlds), ends, trouble
        self.alive = {p: [v[1], v[2]] for p, v in self.worlds[0]["table"].items()}
        self.envs = {p: {"CLAUDE_CODE_SESSION_ID": m[1]} if m else {}
                     for p, m in self.worlds[0]["marks"].items()}
        self.sent, self.now, self.builds = [], 0.0, 0

    def build(self, mine=None, status=None):
        """The first read is the world as given (a test may change the machine
        after it); each later one is the machine now, as the kernel shows it:
        a pid that ended is gone and its children are under launchd."""
        self.builds += 1
        w = self.worlds.pop(0) if len(self.worlds) > 1 else self.worlds[0]
        if self.trouble and (self.trouble[1] is None or self.builds == self.trouble[1]):
            status["trouble"] = self.trouble[0]
        if mine is not None:
            w = dict(w, mine=mine)          # as build_world does
        if self.builds == 1:
            return w
        table = {}
        for p, (pp, st, cmd) in w["table"].items():
            if p in self.alive:
                table[p] = (pp if pp in self.alive or pp == 0 else 1, st, cmd)
        marks = {p: v for p, v in w["marks"].items() if p in table}
        ports = {p: v for p, v in w["ports"].items() if p in table}
        sessions = [x for x in w["sessions"] if x["pid"] in table]
        return dict(w, table=table, marks=marks, ports=ports, sessions=sessions,
                    pipes={q: set() for q in table},
                    att=procs.attribute(table, marks, ports, sessions, own=99))

    def identity_of(self, pid):
        return tuple(self.alive[pid]) if pid in self.alive else None

    def send(self, pid, sig):
        self.sent.append((pid, sig))
        if pid not in self.alive:
            raise ProcessLookupError(pid)
        if self.ends is None or (pid, sig) in self.ends or sig == signal.SIGKILL:
            self.alive.pop(pid)

    def env(self, pid):
        return self.envs.get(pid) if pid in self.alive else None

    def sleep(self, s):
        self.now += s

    def clock(self):
        return self.now

    def ports(self):
        return {p: v for p, v in self.worlds[-1]["ports"].items() if p in self.alive}

    def act(self):
        return {"build": self.build, "identity_of": self.identity_of, "env": self.env,
                "send": self.send, "sleep": self.sleep, "clock": self.clock, "ports": self.ports}


def live_at(w, pid):
    """`w` with `pid` a live session now."""
    w["sessions"].append({"sessionId": OTHER, "pid": pid})
    w["att"] = procs.attribute(w["table"], w["marks"], w["ports"], w["sessions"], own=99)
    return w


class TestCarryOut(unittest.TestCase):
    TARGET = {"pid": 20, "start": T}

    def run_(self, m, force=False, confirmed=None):
        confirmed = plan()["kill"] if confirmed is None else confirmed
        r = engine.carry_out("pid", self.TARGET, confirmed, force=force, act=m.act())
        # every confirmed pid is in exactly one list
        lists = [self.pids(r[k]) for k in ("killed", "survivors", "spare")]
        self.assertEqual(sorted(p for ps in lists for p in ps), sorted(self.pids(confirmed)))
        return r

    def pids(self, entries):
        return [e["pid"] for e in entries]

    def why(self, entries):
        return {e["pid"]: e.get("why") for e in entries}

    def sigkills(self, m):
        return [p for p, s in m.sent if s == signal.SIGKILL]

    def during_wait(self, m, change):
        real = m.sleep

        def sleep(s):
            real(s)
            change()
        m.sleep = sleep

    def test_sigterm_leaves_first_pids_only(self):
        m = Machine([world()])
        r = self.run_(m)
        self.assertEqual(m.sent, [(22, signal.SIGTERM), (21, signal.SIGTERM), (20, signal.SIGTERM)])
        self.assertEqual(sorted(self.pids(r["killed"])), [20, 21, 22])
        self.assertEqual(r["survivors"], [])
        self.assertEqual(r["ports"], [":3000 is free"])
        self.assertNotIn("why", r)

    def test_nothing_but_the_confirmed_is_signalled(self):
        m = Machine([world(extra={23: (22, T, "esbuild worker")}, marks={23: ("claude", DEAD)})])
        r = self.run_(m)
        self.assertNotIn(23, [p for p, _ in m.sent])
        self.assertEqual(self.pids(r["new"]), [23])

    def test_a_survivor_is_reported_not_killed(self):
        m = Machine([world()], ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        r = self.run_(m)
        self.assertEqual(self.pids(r["survivors"]), [22])
        self.assertEqual(self.sigkills(m), [])
        self.assertEqual(r["ports"], [":3000 still held by 22 esbuild --service (child of 21)"])

    def test_force_sends_sigkill_to_survivors_only(self):                # control for H1
        m = Machine([world()], ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [22])
        self.assertEqual(r["survivors"], [])
        self.assertIn(22, self.pids(r["killed"]))
        self.assertEqual(m.builds, 2)       # the world is read again before SIGKILL

    def test_force_kills_a_survivor_whose_root_ended(self):
        # 20 and 21 end on SIGTERM; 22 lives on, now under launchd: the plan
        # for "20" no longer holds it - it is planned again as its own pid
        m = Machine([world()], ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [22])
        self.assertEqual(sorted(self.pids(r["killed"])), [20, 21, 22])

    def test_force_sigkills_a_root_that_survived(self):                # control
        m = Machine([world()], ends={(21, signal.SIGTERM), (22, signal.SIGTERM)})
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [20])
        self.assertEqual(sorted(self.pids(r["killed"])), [20, 21, 22])

    def test_a_child_forked_by_a_survivor_is_listed(self):
        w2 = world(extra={23: (22, T, "esbuild worker")}, marks={23: ("claude", DEAD)})
        m = Machine([world(), w2], ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})

        def fork():
            m.alive.setdefault(23, [T, "esbuild worker"])
            m.envs[23] = {"CLAUDE_CODE_SESSION_ID": DEAD}
        self.during_wait(m, fork)
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [22])
        self.assertNotIn(23, [p for p, _ in m.sent])
        self.assertEqual(self.pids(r["new"]), [23])

    def test_a_confirmed_child_is_never_new(self):
        # 20 and 21 survive; 20's own plan takes 21 again: 21 was confirmed
        m = Machine([world()], ends={(22, signal.SIGTERM)})
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [21, 20])
        self.assertEqual(r["new"], [])

    def test_a_confirmed_child_spared_at_sigterm_is_never_new(self):
        m = Machine([world()], ends={(22, signal.SIGTERM)})
        m.envs[21] = None                   # 21 cannot be checked: spared, 20 survives
        r = self.run_(m, force=True)
        self.assertIn(21, self.pids(r["spare"]))
        self.assertNotIn(21, self.pids(r["new"]))

    def test_a_survivor_missing_from_the_second_read_is_not_counted_gone(self):
        # a row ps gave that could not be parsed: gone must be seen, not inferred
        m = Machine([world(), world(drop=(22,))], ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [])
        self.assertEqual(self.pids(r["survivors"]), [22])
        self.assertNotIn(22, self.pids(r["killed"]))

    def test_force_plans_again_before_sigkill(self):
        # 22 became a live session while ccwho waited: the guards run again
        m = Machine([world(), live_at(world(), 22)], ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [])
        self.assertEqual(self.pids(r["survivors"]), [22])
        self.assertIn("is a live Claude session", self.why(r["survivors"])[22])
        self.assertIn("SIGKILL not sent", self.why(r["survivors"])[22])

    def test_force_after_an_exec_into_an_agent(self):
        m = Machine([world(), world(extra={22: (21, T, "/Users/x/.local/bin/claude")})],
                    ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        self.during_wait(m, lambda: m.alive[22].__setitem__(1, "/Users/x/.local/bin/claude"))
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [])
        self.assertEqual(self.pids(r["survivors"]), [22])

    def test_force_after_an_exec_the_second_world_missed(self):
        # the exec lands after the second read: the command check catches it
        m = Machine([world()], ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        real_build = m.build

        def build(**kw):
            w = real_build(**kw)
            if m.builds == 2:
                m.alive[22][1] = "/Users/x/.local/bin/claude"
            return w
        m.build = build
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [])
        self.assertIn(22, self.pids(r["survivors"]))

    def test_trouble_on_the_second_read_sends_no_sigkill(self):
        m = Machine([world()], ends={(21, signal.SIGTERM), (20, signal.SIGTERM)},
                    trouble=("a live session's pid could not be read", 2))
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [])
        self.assertIn("could not be read", self.why(r["survivors"])[22])

    def test_a_command_changed_before_sigterm_is_not_signalled(self):
        m = Machine([world()])
        m.alive[21][1] = "/Users/x/.local/bin/claude"      # exec after the fresh world was read
        r = self.run_(m)
        self.assertNotIn(21, [p for p, _ in m.sent])
        self.assertIn("now runs another command", self.why(r["spare"])[21])

    def test_force_after_an_exec_into_another_command(self):
        # 22 execs into a plain program while ccwho waits; the second plan still takes it
        m = Machine([world(), world(extra={22: (21, T, "python3 other.py")})],
                    ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        self.during_wait(m, lambda: m.alive[22].__setitem__(1, "python3 other.py"))
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [])
        self.assertIn("now runs another command", self.why(r["survivors"])[22])

    def test_a_survivor_that_exits_before_sigkill_was_killed(self):
        m = Machine([world()], ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})

        def send(pid, sig):
            if (pid, sig) == (22, signal.SIGKILL):
                m.alive.pop(22)
                raise ProcessLookupError(pid)
            Machine.send(m, pid, sig)
        m.send = send
        r = self.run_(m, force=True)
        self.assertIn(22, self.pids(r["killed"]))
        self.assertEqual(r["survivors"], [])

    def test_a_survivor_whose_pid_is_reused_before_sigkill_was_killed(self):
        # its pid has a new start: the process that got SIGTERM is seen gone
        m = Machine([world()], ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        real_build = m.build

        def build(**kw):
            w = real_build(**kw)
            if m.builds == 2:
                m.alive[22][0] = T2
            return w
        m.build = build
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [])
        self.assertIn(22, self.pids(r["killed"]))

    def test_a_survivor_the_second_world_sees_reused_was_killed(self):
        m = Machine([world(), world(extra={22: (21, T2, "esbuild --service")})],
                    ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        real_build = m.build

        def build(**kw):
            w = real_build(**kw)
            if m.builds == 2:
                m.alive[22][0] = T2
            return w
        m.build = build
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [])
        self.assertIn(22, self.pids(r["killed"]))

    def test_force_rechecks_the_mark_before_sigkill(self):
        m = Machine([world()], ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        self.during_wait(m, lambda: m.envs.__setitem__(22, {"CLAUDE_CODE_SESSION_ID": OTHER}))
        r = self.run_(m, force=True)
        self.assertEqual(self.sigkills(m), [])
        # it got SIGTERM: never "not killed" in spare
        self.assertEqual(self.pids(r["survivors"]), [22])
        self.assertIn("SIGKILL not sent", self.why(r["survivors"])[22])
        self.assertNotIn(22, self.pids(r["spare"]))

    def agent_force(self, mine):
        """An agent's `mine` kill of its session DEAD's work (20, 21, 22, 30)
        where 22 survives SIGTERM and, in the second read, talks to another
        machine - a note: an agent (no person to decide) must not SIGKILL it."""
        w1 = world()
        w2 = dict(world(), connections=[(22, "127.0.0.1", 3000, "8.8.8.8", 443)])
        confirmed = procs.kill_plan("mine", DEAD, dict(w1, mine=DEAD))["kill"]
        m = Machine([w1, w2], ends={(20, signal.SIGTERM), (21, signal.SIGTERM),
                                    (30, signal.SIGTERM)})
        r = engine.carry_out("mine" if mine else "pid", DEAD if mine else self.TARGET,
                             confirmed if mine else plan()["kill"], force=True,
                             mine=DEAD if mine else None, act=m.act())
        return m, r

    def test_an_agents_sigkill_is_planned_as_the_agent(self):
        m, r = self.agent_force(mine=True)
        self.assertIn(30, [p for p, _ in m.sent])                     # it did run
        self.assertEqual(self.sigkills(m), [])
        self.assertIn("SIGKILL not sent", self.why(r["survivors"])[22])

    def test_a_persons_sigkill_takes_it_with_the_note(self):          # control
        m, r = self.agent_force(mine=False)
        self.assertEqual(self.sigkills(m), [22])

    def test_the_wait_is_bounded(self):
        m = Machine([world()], ends=set())
        self.run_(m)
        self.assertLessEqual(m.now, 5)
        self.assertGreater(m.now, 0)

    def test_it_stops_waiting_when_all_are_gone(self):
        m = Machine([world()])
        self.run_(m)
        self.assertLess(m.now, 0.5)

    def test_the_start_is_read_again_right_before_the_signal(self):
        m = Machine([world()])
        m.alive[21][0] = T2                 # reused after the fresh world was read
        r = self.run_(m)
        self.assertNotIn(21, [p for p, _ in m.sent])
        self.assertIn("now a different process", self.why(r["spare"])[21])

    def test_the_start_is_read_again_after_the_mark(self):
        # reused between the first identity read and the mark read
        m = Machine([world()])
        real_env = m.env

        def env(pid):
            e = real_env(pid)                # the right mark: only the second read tells
            if pid == 21:
                m.alive[21] = [T2, "node vite"]
            return e
        m.env = env
        r = self.run_(m)
        self.assertNotIn(21, [p for p, _ in m.sent])
        self.assertIn(21, self.pids(r["spare"]))

    def test_the_mark_is_read_again_right_before_the_signal(self):
        m = Machine([world()])
        m.envs[21] = {"CLAUDE_CODE_SESSION_ID": OTHER}      # changed after the fresh world
        r = self.run_(m)
        self.assertNotIn(21, [p for p, _ in m.sent])
        self.assertEqual(self.why(r["spare"])[21],
                         "its session mark changed since the list - not killed")

    def test_the_mark_is_compared_whole(self):
        # two ids alike in their first 60 characters: the shown forms match, the ids do not
        a, b = "x" * 70 + "a", "x" * 70 + "b"
        w = world(marks={20: ("claude", a), 21: ("claude", a), 22: ("claude", a)})
        m = Machine([w])
        m.envs[21] = {"CLAUDE_CODE_SESSION_ID": b}
        engine.carry_out("pid", self.TARGET, plan(w=w)["kill"], act=m.act())
        self.assertNotIn(21, [p for p, _ in m.sent])
        self.assertIn(22, [p for p, _ in m.sent])                     # control

    def test_a_mark_that_cannot_be_read_again_is_not_signalled(self):
        m = Machine([world()])
        m.envs[21] = None
        r = self.run_(m)
        self.assertNotIn(21, [p for p, _ in m.sent])
        self.assertIn("could not check it again", self.why(r["spare"])[21])

    def test_an_unmarked_target_needs_no_mark(self):                   # control
        w = world(marks={20: None, 21: None, 22: None})
        confirmed = plan(w=w)["kill"]
        m = Machine([w])
        m.envs.update({20: None, 21: {}, 22: {}})           # 20's environment is not readable
        engine.carry_out("pid", {"pid": 20, "start": T}, confirmed, act=m.act())
        self.assertEqual([p for p, _ in m.sent], [22, 21, 20])

    def test_gone_right_before_the_signal(self):
        m = Machine([world()])
        m.alive.pop(22)
        r = self.run_(m)
        self.assertNotIn(22, [p for p, _ in m.sent])
        self.assertEqual(self.why(r["spare"])[22], "already exited - nothing to do")

    def test_gone_between_check_and_signal(self):
        m = Machine([world()])

        def send(pid, sig):
            if pid == 22:
                m.alive.pop(22, None)
                raise ProcessLookupError(pid)
            Machine.send(m, pid, sig)
        m.send = send
        r = self.run_(m)
        self.assertEqual(self.why(r["spare"])[22], "already exited - nothing to do")

    def test_another_users_pid(self):
        m = Machine([world()])

        def send(pid, sig):
            if pid == 22:
                raise PermissionError(pid)
            Machine.send(m, pid, sig)
        m.send = send
        r = self.run_(m)
        self.assertEqual(self.why(r["spare"])[22], "belongs to another user - not killed")

    def test_trouble_signals_nothing(self):
        m = Machine([world()], trouble=("a live session's pid could not be read", None))
        r = self.run_(m)
        self.assertEqual(m.sent, [])
        self.assertEqual(r["why"], "a live session's pid could not be read - nothing killed")
        self.assertEqual(sorted(self.pids(r["spare"])), [20, 21, 22])

    def test_an_identity_that_cannot_be_read_is_not_signalled(self):
        m = Machine([world()])

        def identity_of(pid):
            if pid == 21:
                raise OSError("ps failed")
            return Machine.identity_of(m, pid)
        m.identity_of = identity_of
        r = self.run_(m)
        self.assertNotIn(21, [p for p, _ in m.sent])
        self.assertIn("could not check it again", self.why(r["spare"])[21])

    def test_a_survivor_check_that_fails_is_a_survivor(self):
        # "gone" must be seen, never assumed
        m = Machine([world()])

        def identity_of(pid):
            if pid == 20 and (20, signal.SIGTERM) in m.sent:
                raise OSError("ps failed")
            return Machine.identity_of(m, pid)
        m.identity_of = identity_of
        r = self.run_(m)
        self.assertIn(20, self.pids(r["survivors"]))

    def test_never_pid_1_nor_ccwho(self):
        # kill_plan never takes them; the signaller does not lean on that
        m = Machine([world()])
        bad = [{"pid": 1, "start": T, "marked": None, "command": "/sbin/launchd", "ports": []},
               {"pid": 99, "start": T, "marked": None, "command": "python3 ccwho.py kill 20",
                "ports": []}]
        a = dict(m.act(), plan=lambda mode, target, w: {"kill": bad, "spare": []})
        r = engine.carry_out("pid", self.TARGET, bad, act=a)
        self.assertEqual(m.sent, [])
        self.assertEqual(sorted(self.pids(r["spare"])), [1, 99])

    def test_never_its_own_pid(self):
        m = Machine([world(extra={os.getpid(): (1, T, "python3 x")})])
        own = [{"pid": os.getpid(), "start": T, "marked": None, "command": "python3 x", "ports": []}]
        a = dict(m.act(), plan=lambda mode, target, w: {"kill": own, "spare": []})
        engine.carry_out("pid", self.TARGET, own, act=a)
        self.assertEqual(m.sent, [])

    def test_force_on_a_fresh_refusal_signals_nothing(self):
        m = Machine([live_at(world(), 20)])
        self.run_(m, force=True)
        self.assertEqual(m.sent, [])

    def test_an_interrupt_while_waiting_still_reports(self):
        m = Machine([world()], ends=set())

        def sleep(s):
            raise KeyboardInterrupt
        m.sleep = sleep
        r = self.run_(m)
        self.assertTrue(r["why"].startswith("interrupted"), r["why"])
        self.assertEqual(sorted(self.pids(r["survivors"])), [20, 21, 22])
        self.assertTrue(all(w.startswith("not seen gone") for w in self.why(r["survivors"]).values()))

    def test_an_interrupt_between_signals_spares_the_rest(self):
        m = Machine([world()])

        def send(pid, sig):
            if pid == 21:
                raise KeyboardInterrupt
            Machine.send(m, pid, sig)
        m.send = send
        r = self.run_(m)
        self.assertEqual([p for p, _ in m.sent], [22])
        self.assertTrue(r["why"].startswith("interrupted"))
        self.assertEqual(self.pids(r["spare"]), [20])
        # 21's signal may have gone out before the interrupt: never "not signalled"
        self.assertIn("may have got SIGTERM", self.why(r["survivors"])[21])

    def test_a_bug_before_any_signal_is_raised(self):
        # nothing was signalled: a crash says more than a report
        m = Machine([world()])

        def env(pid):
            raise TypeError("a bug")
        m.env = env
        with self.assertRaises(TypeError):
            engine.carry_out("pid", self.TARGET, plan()["kill"], act=m.act())
        self.assertEqual(m.sent, [])

    def test_a_bug_after_a_signal_spares_the_unsent(self):
        m = Machine([world()])
        real_env = m.env

        def env(pid):
            if pid == 21:
                raise TypeError("a bug")
            return real_env(pid)
        m.env = env
        r = self.run_(m)
        self.assertEqual([p for p, _ in m.sent], [22])
        self.assertIn("TypeError", r["why"])
        self.assertIn(21, self.pids(r["spare"]))            # its check failed: not signalled
        self.assertIn(22, self.pids(r["survivors"]))

    def test_an_interrupt_in_the_sigkill_wait_keeps_what_was_seen(self):
        m = Machine([world()], ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        real_sleep = m.sleep

        def sleep(s):
            real_sleep(s)
            if (22, signal.SIGKILL) in m.sent:
                raise KeyboardInterrupt
        m.sleep = sleep
        m.ends.add((22, signal.SIGKILL))
        m.send = lambda pid, sig: (m.sent.append((pid, sig)),
                                   m.alive.pop(pid) if (pid, sig) in m.ends - {(22, signal.SIGKILL)} else None)
        r = self.run_(m, force=True)
        self.assertTrue(r["why"].startswith("interrupted"))
        self.assertEqual(sorted(self.pids(r["killed"])), [20, 21])
        self.assertEqual(self.pids(r["survivors"]), [22])

    def test_the_reasons_are_read_from_procs_when_used(self):
        # the engine decides "seen gone" by procs' texts - as they are after a
        # reload, not as they were when the engine was imported
        for name in ("EXITED", "OTHER", "EXECED"):
            self.addCleanup(setattr, procs, name, getattr(procs, name))
            setattr(procs, name, getattr(procs, name) + " (reworded)")
        m = Machine([world(), world(extra={22: (21, T2, "esbuild --service")})],
                    ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        real_build = m.build

        def build(**kw):
            w = real_build(**kw)
            if m.builds == 2:
                m.alive[22][0] = T2
            return w
        m.build = build
        r = self.run_(m, force=True)
        self.assertIn(22, self.pids(r["killed"]))
        # and one that ended just before its SIGKILL
        m = Machine([world()], ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})

        def send(pid, sig):
            if (pid, sig) == (22, signal.SIGKILL):
                m.alive.pop(22)
                raise ProcessLookupError(pid)
            Machine.send(m, pid, sig)
        m.send = send
        r = self.run_(m, force=True)
        self.assertIn(22, self.pids(r["killed"]))

    def test_an_error_after_a_signal_still_reports(self):
        m = Machine([world()])

        def ports():
            raise RuntimeError("lsof broke")
        m.ports = ports
        r = self.run_(m)
        self.assertIn("RuntimeError", r["why"])
        self.assertEqual(sorted(self.pids(r["killed"])), [20, 21, 22])


def _orphan(tmp, name):
    """A python3 sleeping 30 s under launchd, started by this test only, no pipe
    to it and no session mark: its pid, read from a file."""
    pidfile = os.path.join(tmp, name)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    subprocess.run(["/bin/sh", "-c", f'{sys.executable} -c "import time; time.sleep(30)" '
                                     f'</dev/null >/dev/null 2>&1 & echo $! > {pidfile}'],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, env=env, check=True)
    with open(pidfile) as f:
        return int(f.read())


def _end_own(pid, identity, kill=os.kill, identity_of=None):
    """SIGKILL `pid` only while it is still the process this test started
    (same start and command): a pid reused since then is someone else's."""
    identity_of = identity_of or engine.identity_of
    if pid is None or identity is None or identity_of(pid) != identity:
        return False
    try:
        kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


class TestEndOwn(unittest.TestCase):
    def test_only_the_process_it_started(self):
        sent = []
        mine = (T, "python3 -c sleep")
        self.assertFalse(_end_own(70, mine, lambda p, s: sent.append(p), lambda p: (T2, "x")))
        self.assertFalse(_end_own(70, mine, lambda p, s: sent.append(p), lambda p: None))
        self.assertEqual(sent, [])
        self.assertTrue(_end_own(70, mine, lambda p, s: sent.append(p), lambda p: mine))  # control
        self.assertEqual(sent, [70])


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestTheRealSignal(unittest.TestCase):
    """One real kill, of processes this test started: the target ends, its
    neighbour (control) does not. Cleanup ends only what is still its own."""

    @unittest.skipUnless(sys.platform == "darwin", "reads macOS ps and lsof")
    def test_a_real_pid_kill(self):
        started = {}
        with tempfile.TemporaryDirectory() as tmp:
            try:
                for name in ("target", "neighbour"):
                    pid = _orphan(tmp, name)
                    started[name] = (pid, engine.identity_of(pid))
                (target, ident), (neighbour, _) = started["target"], started["neighbour"]
                time.sleep(0.3)             # the shell exits; both are under launchd
                self.assertIsNotNone(ident)
                w = engine.build_world(status={})
                p = procs.kill_plan("pid", {"pid": target, "start": ident[0]}, w)
                self.assertEqual([e["pid"] for e in p["kill"]], [target], p)
                r = engine.carry_out("pid", {"pid": target, "start": ident[0]}, p["kill"])
                self.assertEqual([e["pid"] for e in r["killed"]], [target], r)
                self.assertFalse(_alive(target))
                self.assertTrue(_alive(neighbour))
            finally:
                for pid, ident in started.values():
                    _end_own(pid, ident)

    @unittest.skipUnless(sys.platform == "darwin", "reads macOS ps")
    def test_identity_of_a_gone_pid_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid = _orphan(tmp, "c")
            ident = engine.identity_of(pid)
            try:
                self.assertIsNotNone(ident)
            finally:
                _end_own(pid, ident)
            deadline = time.monotonic() + 3
            while engine.identity_of(pid) is not None and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertIsNone(engine.identity_of(pid))

    @unittest.skipUnless(sys.platform == "darwin", "reads macOS ps")
    def test_identity_of_matches_the_world(self):
        own = os.getpid()
        w = engine.build_world(status={})
        self.assertEqual(engine.identity_of(own), w["table"][own][1:])


if __name__ == "__main__":
    unittest.main()
