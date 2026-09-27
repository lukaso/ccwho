"""Tests for `ccwho kill` and `ccwho clean`: the list a person sees, the
question, and the report - over the fake machine of test_signal. The owner's
rules (2026-09-27): every kill by a person lists the processes with their
notes and asks on a terminal; --yes skips the question; with no terminal and
no --yes the list is printed and the exit is 3. An agent - a session id in
ccwho's own environment - is never asked, and takes only its own session's
work. TestTheRealCli runs ccwho for real, on processes it started itself."""
import contextlib
import io
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import ccwho as runner
import ccwho_engine as engine
import ccwho_procs as procs
from test_signal import DEAD, OTHER, T, Machine, _end_own, _settled, live_at, world

HERE = os.path.dirname(os.path.abspath(__file__))


class Cli:
    """One ccwho kill/clean run on a fake machine."""

    def __init__(self, w=None, tty=True, answer="y", env=None, ends=None, trouble=None):
        self.m = Machine([w or world()], ends=ends)
        self.tty, self.answer, self.env = tty, answer, dict(env or {})
        self.trouble, self.asked, self.builds, self.carried = trouble, [], [], []

    def build(self, mine=None, status=None):
        self.builds.append(mine)
        if self.trouble:
            status["trouble"] = self.trouble
        w = self.m.worlds[0]
        return dict(w, mine=mine) if mine is not None else w

    def ask(self, prompt):
        self.asked.append(prompt)
        if self.answer is None:
            raise AssertionError("an agent or --yes run must never be asked")
        return self.answer

    def carry(self, mode, target, confirmed, force=False, mine=None):
        self.carried.append((mode, target, [e["pid"] for e in confirmed], force, mine))
        return engine.carry_out(mode, target, confirmed, force=force, mine=mine, act=self.m.act())

    def run(self, *argv, clean=False):
        out = io.StringIO()
        seams = {"build": self.build, "ask": self.ask, "tty": lambda: self.tty,
                 "env": self.env, "carry": self.carry}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = runner.kill_cli(list(argv), clean=clean, seams=seams)
        return rc, out.getvalue()

    def sent(self):
        return [p for p, _s in self.m.sent]


class TestUsage(unittest.TestCase):
    def test_what_is_no_target(self):
        for argv in ([], ["abc"], ["20", "21"], [":99999"], [":"], ["-5"], ["--yes"],
                     ["20", "--bogus"], ["1.5"], ["20", "--mine"], ["²"], ["0"], [":0"],
                     ["99999999"], ["٣"], [":000080"], [":0003000"], ["0020"]):
            c = Cli()
            rc, out = c.run(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertEqual(c.builds, [], argv)       # nothing read, nothing planned
            self.assertIn("usage", out)

    def test_clean_takes_no_target(self):
        rc, _out = Cli().run("20", clean=True)
        self.assertEqual(rc, 2)

    def test_a_good_target_is_no_usage_error(self):                   # control
        self.assertNotEqual(Cli(answer="n").run("20")[0], 2)
        self.assertNotEqual(Cli(answer="n").run(":3000")[0], 2)
        self.assertNotEqual(Cli(answer="n").run(clean=True)[0], 2)

    def test_main_routes_kill_and_clean(self):
        seen = []
        real = runner.kill_cli
        runner.kill_cli = lambda argv, clean=False, seams=None: seen.append((argv, clean)) or 0
        self.addCleanup(setattr, runner, "kill_cli", real)
        runner.main(["kill", "20", "--dry-run"])
        runner.main(["clean", "--mine"])
        self.assertEqual(seen, [(["20", "--dry-run"], False), (["--mine"], True)])


class TestTheList(unittest.TestCase):
    def test_every_process_is_listed_before_anything_is_signalled(self):
        c = Cli(answer="n")
        rc, out = c.run("20")
        for pid, cmd in ((20, "npm run dev"), (21, "node vite"), (22, "esbuild --service")):
            self.assertIn(f"{pid}", out)
            self.assertIn(cmd, out)
        self.assertIn(":3000", out)
        self.assertEqual(c.sent(), [])

    def test_a_note_is_shown(self):
        w = dict(world(), connections=[(22, "127.0.0.1", 3000, "8.8.8.8", 443)])
        rc, out = Cli(w=w, answer="n").run("20")
        self.assertIn("it talks to another machine", out)

    def test_what_is_spared_is_said(self):
        rc, out = Cli(w=live_at(world(), 21), answer="n").run("20")
        self.assertIn("not killed", out)
        self.assertIn("a claude runs under", out)

    def test_nothing_to_kill_exits_1_without_asking(self):
        c = Cli(w=world(drop=(20, 21, 22)))
        rc, out = c.run("20")
        self.assertEqual(rc, 1)
        self.assertIn("already exited", out)
        self.assertEqual(c.asked, [])

    def test_a_mark_that_is_no_session_id_is_not_shown(self):
        odd = "not-a-uuid-" + "Z" * 20
        w = world(marks={20: ("claude", odd), 21: ("claude", odd), 22: ("claude", odd)})
        rc, out = Cli(w=w, answer="n").run("20")
        self.assertNotIn("not-a-uu", out)
        self.assertIn("a session ccwho does not know", out)
        rc, out = Cli(answer="n").run("20")                              # control
        self.assertIn(f"session {DEAD[:8]}", out)

    def test_no_secret_reaches_the_output(self):
        secret = "sk-ant-" + "Q" * 30
        w = world(extra={22: (21, T, f"esbuild --service --token={secret}")})
        c = Cli(w=w)
        rc, out = c.run("20")
        self.assertNotIn(secret, out)
        self.assertIn("esbuild", out)                                   # control


class TestTheQuestion(unittest.TestCase):
    def test_yes_on_a_terminal_kills(self):
        c = Cli(answer="y")
        rc, out = c.run("20")
        self.assertEqual(len(c.asked), 1)
        self.assertIn("3", c.asked[0])
        self.assertEqual(sorted(c.sent()), [20, 21, 22])
        self.assertEqual(rc, 0)

    def test_anything_else_kills_nothing(self):
        for answer in ("n", "", "no", "yess", " "):
            c = Cli(answer=answer)
            rc, out = c.run("20")
            self.assertEqual(c.sent(), [], answer)
            self.assertEqual(rc, 1, answer)
            self.assertIn("nothing killed", out)

    def test_an_interrupted_question_kills_nothing(self):
        # the end of input is a "no" (1); Ctrl-C is an interrupt before any signal (130)
        for err, want in ((EOFError, 1), (KeyboardInterrupt, 130)):
            c = Cli()
            c.ask = lambda prompt, err=err: (_ for _ in ()).throw(err)
            rc, out = c.run("20")
            self.assertEqual((rc, c.sent()), (want, []), err)
            self.assertIn("nothing killed", out)

    def test_yes_in_any_case_and_spacing(self):
        for answer in (" y ", "Y", "YES\n", "yes"):
            c = Cli(answer=answer)
            c.run("20")
            self.assertEqual(sorted(c.sent()), [20, 21, 22], repr(answer))

    def test_no_terminal_needs_yes(self):
        c = Cli(tty=False, answer=None)
        rc, out = c.run("20")
        self.assertEqual(rc, 3)
        self.assertEqual(c.sent(), [])
        self.assertIn("would kill 3 processes (listed above) - add --yes to do it", out)
        self.assertIn("npm run dev", out)                               # the list is printed

    def test_yes_skips_the_question(self):                              # control
        c = Cli(tty=False, answer=None)
        rc, out = c.run("20", "--yes")
        self.assertEqual(sorted(c.sent()), [20, 21, 22])
        self.assertEqual(rc, 0)

    def test_dry_run_lists_and_signals_nothing(self):
        c = Cli(answer=None)
        rc, out = c.run("20", "--dry-run", "--yes")
        self.assertEqual((rc, c.sent(), c.carried), (0, [], []))
        self.assertIn("dry run - nothing killed", out)
        self.assertIn("npm run dev", out)

    def test_the_confirmed_list_is_what_goes_to_the_signaller(self):
        c = Cli()
        c.run("20")
        mode, target, pids, force, mine = c.carried[0]
        self.assertEqual((mode, target, sorted(pids), force, mine),
                         ("pid", {"pid": 20, "start": T}, [20, 21, 22], False, None))

    def test_force_is_passed_on(self):
        c = Cli(ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        c.run("20", "--force")
        self.assertTrue(c.carried[0][3])
        self.assertIn(22, [p for p, s in c.m.sent if s == signal.SIGKILL])


class TestCanAsk(unittest.TestCase):
    """A person answers only what they can see: the list and the question go to
    stdout, the answer comes from stdin - both must be the terminal."""

    class F:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

    def test_both_a_terminal(self):                                     # control
        self.assertTrue(runner._can_ask(self.F(True), self.F(True)))

    def test_output_to_a_file(self):
        # `ccwho kill :3000 > log`: the list and the prompt land in the file
        self.assertFalse(runner._can_ask(self.F(True), self.F(False)))

    def test_input_from_a_pipe(self):
        self.assertFalse(runner._can_ask(self.F(False), self.F(True)))

    def test_it_is_the_default(self):
        real = runner._can_ask
        self.addCleanup(setattr, runner, "_can_ask", real)
        for can, want in ((False, 3), (True, 1)):
            c = Cli(answer="n" if can else None)
            seams = {"build": c.build, "ask": c.ask, "env": {}, "carry": c.carry}
            runner._can_ask = lambda i, o, can=can: can
            with contextlib.redirect_stdout(io.StringIO()):
                rc = runner.kill_cli(["20"], seams=seams)
            self.assertEqual((rc, len(c.asked), c.sent()), (want, int(can), []), can)


class TestTheDefaultAsk(unittest.TestCase):
    """The question goes where the list went - stdout - and the answer is one
    line of stdin; an end of input is no."""

    def ask(self, typed):
        out, err = io.StringIO(), io.StringIO()
        real = sys.stdin
        sys.stdin = io.StringIO(typed)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                answer = runner._ask("kill these 3? [y/N] ")
        finally:
            sys.stdin = real
        return answer, out.getvalue(), err.getvalue()

    def test_the_question_is_on_stdout(self):
        answer, out, err = self.ask("y\n")
        self.assertEqual(answer.strip(), "y")
        self.assertIn("kill these 3? [y/N]", out)
        self.assertEqual(err, "")

    def test_end_of_input_is_no(self):
        self.assertRaises(EOFError, self.ask, "")

    def test_it_is_the_default(self):
        # input() puts its prompt on stderr on a real terminal: kill_cli asks with _ask
        c, asked = Cli(), []
        seams = {"build": c.build, "tty": lambda: True, "env": {}, "carry": c.carry}
        real = runner._ask
        runner._ask = lambda prompt: asked.append(prompt) or "y"
        self.addCleanup(setattr, runner, "_ask", real)
        with contextlib.redirect_stdout(io.StringIO()):
            runner.kill_cli(["20"], seams=seams)
        self.assertEqual(len(asked), 1)
        self.assertEqual(sorted(c.sent()), [20, 21, 22])

    def pty_ask(self, before, after):
        """_ask on a real terminal: `before` typed ahead, `after` once the
        question shows. The answer it took."""
        import pty
        import threading
        master, slave = pty.openpty()
        tty = os.fdopen(slave, "r", buffering=1)
        got = []

        class Screen(io.StringIO):
            """stdout: `after` is typed the moment the question shows."""
            def flush(self):
                if "[y/N]" in self.getvalue() and not self.typed:
                    self.typed = True
                    os.write(master, after.encode())
        out = Screen()
        out.typed = False
        real_in, real_out = sys.stdin, sys.stdout
        sys.stdin, sys.stdout = tty, out
        t = threading.Thread(target=lambda: got.append(runner._ask("kill these 3? [y/N] ")),
                             daemon=True)
        try:
            if before:
                os.write(master, before.encode())
                time.sleep(0.1)             # the line discipline has it
            t.start()
            t.join(5)
        finally:
            sys.stdin, sys.stdout = real_in, real_out
            os.close(master)                # first: a read still blocked gets EIO and ends
            t.join(1)
            tty.close()
        self.assertFalse(t.is_alive(), "_ask did not return")
        return got[0].strip() if got else None

    @unittest.skipUnless(sys.platform == "darwin", "a macOS pty")
    def test_typed_ahead_is_not_an_answer(self):
        self.assertEqual(self.pty_ask("y\n", "n\n"), "n")

    @unittest.skipUnless(sys.platform == "darwin", "a macOS pty")
    def test_typed_after_the_question_is(self):                        # control
        self.assertEqual(self.pty_ask("", "y\n"), "y")

    def test_a_pipe_is_read_without_a_flush(self):
        r, w = os.pipe()
        os.write(w, b"y\n")
        os.close(w)
        real = sys.stdin
        sys.stdin = os.fdopen(r, "r")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(runner._ask("? ").strip(), "y")
        finally:
            sys.stdin.close()
            sys.stdin = real

    def test_a_terminal_that_cannot_be_flushed_is_a_no(self):
        # an unflushed "y" could confirm the list: a failed flush answers no
        import termios
        real_flush, real_isatty = termios.tcflush, os.isatty
        termios.tcflush = lambda *a: (_ for _ in ()).throw(termios.error(5, "EIO"))
        os.isatty = lambda fd: True
        r, w = os.pipe()
        os.write(w, b"y\n")
        os.close(w)
        real = sys.stdin
        sys.stdin = os.fdopen(r, "r")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertRaises(EOFError, runner._ask, "? ")
        finally:
            termios.tcflush, os.isatty = real_flush, real_isatty
            sys.stdin.close()
            sys.stdin = real

    def test_no_stdin_cannot_ask(self):
        class Closed:
            def isatty(self):
                raise ValueError("I/O operation on closed file")
        self.assertFalse(runner._can_ask(None, sys.stdout))
        self.assertFalse(runner._can_ask(Closed(), sys.stdout))


class TestAnAgent(unittest.TestCase):
    """A session id in ccwho's own environment: an agent. Never asked; its
    session id is `mine`, so the plan takes only its own session's work."""

    def test_its_own_work_goes_without_a_question(self):
        c = Cli(answer=None, tty=False, env={"CLAUDE_CODE_SESSION_ID": DEAD})
        rc, out = c.run("20")
        self.assertEqual(c.builds[0], DEAD)
        self.assertEqual(sorted(c.sent()), [20, 21, 22])
        self.assertEqual((rc, c.asked), (0, []))

    def test_another_sessions_work_is_refused(self):
        c = Cli(answer=None, tty=False, env={"CLAUDE_CODE_SESSION_ID": OTHER})
        rc, out = c.run("20", "--yes")
        self.assertEqual((rc, c.sent()), (1, []))
        self.assertIn("your session did not start", out)

    def test_a_terminal_does_not_make_it_a_person(self):
        c = Cli(answer=None, tty=True, env={"CLAUDE_CODE_SESSION_ID": OTHER})
        rc, out = c.run("20")
        self.assertEqual((rc, c.sent(), c.asked), (1, [], []))

    def test_codex_is_an_agent_too(self):
        c = Cli(answer=None, tty=True, env={"CODEX_THREAD_ID": "019a1b2c-0000-4000-8000-00000000000c"})
        rc, out = c.run("20")
        self.assertEqual(c.builds[0], "019a1b2c-0000-4000-8000-00000000000c")
        self.assertEqual((c.sent(), c.asked), ([], []))

    def test_clean_mine_takes_its_own(self):
        c = Cli(answer=None, env={"CLAUDE_CODE_SESSION_ID": DEAD})
        rc, out = c.run("--mine", clean=True)
        self.assertEqual(c.carried[0][0:2], ("mine", DEAD))
        self.assertEqual(sorted(c.sent()), [20, 21, 22, 30])

    def test_clean_is_refused_for_an_agent(self):
        c = Cli(answer=None, env={"CLAUDE_CODE_SESSION_ID": DEAD})
        rc, out = c.run(clean=True)
        self.assertEqual((rc, c.sent()), (1, []))
        self.assertIn("clean --mine", out)

    def test_mine_is_for_an_agent(self):
        c = Cli(answer=None)
        rc, out = c.run("--mine", clean=True)
        self.assertEqual((rc, c.sent()), (1, []))
        self.assertIn("--mine is for an agent", out)

    def test_an_empty_id_is_no_agent(self):                             # control
        c = Cli(answer="n", env={"CLAUDE_CODE_SESSION_ID": ""})
        c.run("20")
        self.assertEqual((c.builds[0], len(c.asked)), (None, 1))


class TestTargets(unittest.TestCase):
    def test_a_port(self):
        c = Cli()
        rc, out = c.run(":3000")
        self.assertEqual(c.carried[0][0:2], ("port", "3000"))
        self.assertIn(22, c.sent())
        self.assertIn(":3000 is free", out)

    def test_clean(self):
        c = Cli()
        rc, out = c.run("--yes", clean=True)
        self.assertEqual(c.carried[0][0], "clean")
        self.assertEqual(sorted(c.sent()), [20, 21, 22, 30])

    def test_a_pid_is_planned_on_its_start_now(self):
        # the person names a pid; the list shows what it is now, and they confirm that
        c = Cli()
        c.run("22")
        self.assertEqual(c.carried[0][1], {"pid": 22, "start": T})


class TestTheReport(unittest.TestCase):
    def test_all_killed_exits_0(self):
        rc, out = Cli().run("20")
        self.assertEqual(rc, 0)
        self.assertIn("killed 3", out)

    def test_a_survivor_exits_1_and_says_what_to_do(self):
        c = Cli(ends={(21, signal.SIGTERM), (20, signal.SIGTERM)})
        rc, out = c.run("20")
        self.assertEqual(rc, 1)
        self.assertIn("still running: 22 esbuild --service", out)
        self.assertIn("ccwho kill 22 --force", out)
        self.assertIn(":3000 still held by 22", out)

    def test_a_spare_at_signal_time_exits_1(self):
        c = Cli()
        c.m.alive[21][0] = "Wed Sep 23 09:00:00 2026"           # reused after the list
        rc, out = c.run("20")
        self.assertEqual(rc, 1)
        self.assertIn("21", out)
        self.assertIn("now a different process", out)

    def test_new_since_the_list_is_said(self):
        c = Cli()
        real = c.carry

        def carry(*a, **k):
            c.m.worlds[0] = world(extra={23: (22, T, "esbuild worker")}, marks={23: ("claude", DEAD)})
            c.m.alive[23] = [T, "esbuild worker"]
            return real(*a, **k)
        c.carry = carry
        rc, out = c.run("20")
        self.assertIn("23", out)
        self.assertIn("not killed", out)
        self.assertNotIn(23, c.sent())
        self.assertEqual(rc, 1)             # not all of the target is gone

    def test_a_port_still_held_exits_1(self):
        # :3000 is held by 22 (left behind) and by 10, a live session: never killed
        w = world(ports={22: [3000], 10: [3000], 30: [5173]})
        c = Cli(w=w)
        rc, out = c.run(":3000")
        self.assertIn(22, c.sent())
        self.assertNotIn(10, c.sent())
        self.assertIn(":3000 still held by 10", out)
        self.assertEqual(rc, 1)

    def test_a_port_another_process_still_holds_exits_1(self):
        # kill 20's tree frees 22's hold on :3000, but 30 holds it too
        c = Cli(w=world(ports={22: [3000], 30: [3000, 5173]}))
        rc, out = c.run("20")
        self.assertNotIn(30, c.sent())
        self.assertIn(":3000 still held by 30", out)
        self.assertEqual(rc, 1)

    def test_clean_that_spares_the_unsure_still_exits_0(self):         # control
        w = world()
        w["att"]["unsure"].append({"pid": 30, "why": "x"})
        w["att"]["left_behind"] = [p for p in w["att"]["left_behind"] if p["pid"] != 30]
        c = Cli(w=w)
        rc, out = c.run("--yes", clean=True)
        self.assertIn("30 is not sure", out)
        self.assertEqual(rc, 0)

    def test_clean_with_a_refused_tree_exits_1(self):
        # 30 is piped to the live claude 10: a guard refuses its tree; 20's goes
        w = world()
        w["pipes"][30], w["pipes"][10] = {10}, {30}
        c = Cli(w=w)
        rc, out = c.run("--yes", clean=True)
        self.assertEqual(sorted(c.sent()), [20, 21, 22])
        self.assertIn("not killed: 30", out)
        self.assertEqual(rc, 1)

    def test_the_plan_tags_what_is_outside_the_target(self):
        w = world()
        w["att"]["unsure"].append({"pid": 30, "why": "x"})
        w["att"]["left_behind"] = [p for p in w["att"]["left_behind"] if p["pid"] != 30]
        p = procs.kill_plan("clean", None, w)
        self.assertEqual([(e["pid"], e.get("outside")) for e in p["spare"]], [(30, True)])
        w = world()                                                      # control: refused
        w["pipes"][30], w["pipes"][10] = {10}, {30}
        p = procs.kill_plan("clean", None, w)
        self.assertEqual([(e["pid"], e.get("outside")) for e in p["spare"]], [(30, None)])

    def test_ports_not_read_again_exits_1(self):
        c = Cli()
        c.m.ports = lambda: None
        rc, out = c.run("20")
        self.assertEqual(sorted(c.sent()), [20, 21, 22])
        self.assertIn("could not read the ports again", out)
        self.assertEqual(rc, 1)

    def test_an_interrupt_before_any_signal(self):
        for where in ("build", "carry"):
            c = Cli()

            def boom(*a, **k):
                err = KeyboardInterrupt()
                err.ccwho_nothing_signalled = True      # as carry_out marks it
                raise err
            setattr(c, where, boom)
            rc, out = c.run("20", "--yes")
            self.assertEqual((rc, c.sent()), (130, []), where)
            self.assertIn("interrupted - nothing killed", out)

    def test_a_stop_after_the_signals_is_never_nothing_killed(self):
        c = Cli()
        live = engine.procs                     # the module the engine calls (after any reload)
        self.addCleanup(setattr, live, "port_report", live.port_report)
        live.port_report = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt)
        rc, out = c.run("20")
        self.assertEqual(len(c.sent()), 3)
        self.assertEqual(rc, 1)
        self.assertIn("interrupted", out)
        self.assertNotIn("nothing killed", out)

    def test_anything_that_escapes_after_a_signal_is_may_have(self):
        # a second Ctrl-C, or a bug, after the signaller started: never "nothing killed"
        for err in (KeyboardInterrupt, RuntimeError):
            c = Cli()

            def carry(*a, err=err, **k):
                c.m.sent.append((22, signal.SIGTERM))
                raise err("x")
            c.carry = carry
            rc, out = c.run("20")
            self.assertEqual(rc, 1, err)
            self.assertNotIn("nothing killed", out)
            self.assertIn("may have been signalled", out)

    def test_a_stop_while_reporting_is_said(self):
        class Boom:
            def __iter__(self):
                raise KeyboardInterrupt
        c = Cli()
        real = c.carry
        c.carry = lambda *a, **k: dict(real(*a, **k), ports=Boom())
        rc, out = c.run("20")
        self.assertEqual(rc, 1)
        self.assertNotIn("nothing killed", out)
        self.assertIn("run ccwho ps", out)

    def test_an_error_before_any_signal_shows_no_text(self):
        secret = "sk-ant-" + "Q" * 30
        c = Cli()
        c.build = lambda **k: (_ for _ in ()).throw(RuntimeError(f"token {secret}"))
        rc, out = c.run("20", "--yes")
        self.assertEqual((rc, c.sent()), (1, []))
        self.assertIn("RuntimeError", out)
        self.assertIn("nothing killed", out)
        self.assertNotIn(secret, out)
        c = Cli()                                   # in the signaller, before any signal
        c.m.env = lambda pid: (_ for _ in ()).throw(RuntimeError(f"token {secret}"))
        rc, out = c.run("20", "--yes")
        self.assertEqual((rc, c.sent()), (1, []))
        self.assertIn("RuntimeError", out)
        self.assertNotIn(secret, out)

    def test_an_interrupt_with_nothing_signalled_is_130(self):
        # every pid was gone at signal time, then Ctrl-C while the report is made
        c = Cli()
        c.m.alive = {}
        live = engine.procs
        self.addCleanup(setattr, live, "port_report", live.port_report)
        live.port_report = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt)
        rc, out = c.run("20")
        self.assertEqual((rc, c.sent()), (130, []))

    def test_a_dry_run_with_a_refused_tree_exits_1(self):
        w = world()
        w["pipes"][30], w["pipes"][10] = {10}, {30}
        c = Cli(w=w, answer=None)
        rc, out = c.run("--dry-run", clean=True)
        self.assertEqual((rc, c.sent()), (1, []))
        self.assertIn("dry run - nothing killed", out)

    def test_a_dry_run_with_only_outside_spares_exits_0(self):          # control
        w = world()
        w["att"]["unsure"].append({"pid": 30, "why": "x"})
        w["att"]["left_behind"] = [p for p in w["att"]["left_behind"] if p["pid"] != 30]
        rc, out = Cli(w=w, answer=None).run("--dry-run", clean=True)
        self.assertEqual(rc, 0)

    def test_trouble_kills_nothing(self):
        c = Cli(trouble="a live session's pid could not be read")
        rc, out = c.run("20")
        self.assertEqual((rc, c.sent(), c.asked), (1, [], []))
        self.assertIn("a live session's pid could not be read - nothing killed", out)

    def test_a_stop_after_signals_is_said(self):
        c = Cli(ends=set())
        c.m.sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt)
        rc, out = c.run("20")
        self.assertEqual(rc, 1)
        self.assertIn("ccwho: interrupted", out)


# ---------------------------------------------------------------- real runs

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _server(tmp, name, port, session=None):
    """A python3 HTTP server on 127.0.0.1:`port` under launchd, started by this
    test only, no pipe to it; marked by `session` when given. Its pid."""
    pidfile = os.path.join(tmp, name)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_PID")}
    if session:
        env["CLAUDE_CODE_SESSION_ID"] = session
    # it ends by itself after 60 s: a cleanup that fails cannot leave it running
    serve = (f"import http.server as h, threading as t; "
             f"s = h.HTTPServer(('127.0.0.1', {port}), h.SimpleHTTPRequestHandler); "
             f"t.Timer(60, s.shutdown).start(); s.serve_forever()")
    subprocess.run(["/bin/sh", "-c", f'{sys.executable} -c "{serve}"'
                                     f" </dev/null >/dev/null 2>&1 & echo $! > {pidfile}"],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, env=env, check=True, cwd=tmp)
    with open(pidfile) as f:
        return _settled(int(f.read()))


def _answers(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _ccwho(*argv):
    """ccwho as a person with no terminal would run it: no session id."""
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")}
    done = subprocess.run([sys.executable, os.path.join(HERE, "ccwho.py"), *argv],
                          capture_output=True, text=True, env=env, timeout=30,
                          stdin=subprocess.DEVNULL)
    return done.returncode, done.stdout + done.stderr


class TestTheRealCli(unittest.TestCase):
    """Real ps, lsof and signals - on two servers this test started: one
    marked by a session that is not running (left behind), one unmarked.
    `clean` only lists (--dry-run): a real `clean --yes` would take every
    process any ended session left on this machine."""

    GONE_SESSION = "dddd4444-0000-4000-8000-00000000dead"

    @unittest.skipUnless(sys.platform == "darwin", "reads macOS ps and lsof")
    def test_clean_lists_it_and_kill_frees_its_port(self):
        started = {}
        with tempfile.TemporaryDirectory() as tmp:
            try:
                p1, p2 = _free_port(), _free_port()
                for name, port, sid in (("marked", p1, self.GONE_SESSION), ("plain", p2, None)):
                    pid = _server(tmp, name, port, sid)
                    started[name] = (pid, None, port)           # recorded before anything can fail
                    started[name] = (pid, engine.identity_of(pid), port)
                deadline = time.monotonic() + 5
                while not (_answers(p1) and _answers(p2)) and time.monotonic() < deadline:
                    time.sleep(0.1)
                self.assertTrue(_answers(p1) and _answers(p2), "the servers did not start")
                marked, plain = started["marked"][0], started["plain"][0]

                # taken, or "not sure" when this machine's session list is not whole
                # (a claude no session lists, as Chrome's helper): never unnamed
                rc, out = _ccwho("clean", "--dry-run")
                self.assertIn(rc, (0, 1))
                listed = [ln.split()[0] for ln in out.splitlines()
                          if ln.startswith("  ") and ln.split()[0].isdigit()]
                sure = [ln for ln in out.splitlines() if ln.startswith(f"not killed: {marked} is not sure")]
                self.assertTrue(str(marked) in listed or sure, "clean did not name the server left behind")
                named = set(listed) | {ln.split()[2] for ln in out.splitlines()
                                       if ln.startswith("not killed: ") and len(ln.split()) > 2}
                self.assertNotIn(str(plain), named)                     # control: no agent started it

                rc, out = _ccwho("kill", f":{p1}")                      # no terminal, no --yes
                self.assertEqual(rc, 3)
                self.assertTrue(_answers(p1))
                listed = {ln.split()[0] for ln in out.splitlines()
                          if ln.startswith("  ") and ln.split()[0].isdigit()}
                self.assertNotIn(str(plain), listed)                    # control
                if listed != {str(marked)}:
                    # someone else took the port between the check and the bind
                    self.skipTest("the port is not held by this test's server alone")

                rc, out = _ccwho("kill", f":{p1}", "--yes")
                self.assertEqual(rc, 0, "kill did not report success")
                self.assertFalse(_answers(p1))
                self.assertTrue(_answers(p2))                           # control
            finally:
                for pid, ident, _port in started.values():
                    _end_own(pid, ident)


if __name__ == "__main__":
    unittest.main()
