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
from test_signal import DEAD, LIVE, OTHER, T, Machine, _end_own, _settled, live_at, world


_UNPIN = []


def setUpModule():
    import ccwho
    import testkit
    _UNPIN.append(testkit.pin_ccwho_dir(ccwho))


def tearDownModule():
    while _UNPIN:
        _UNPIN.pop()()

HERE = os.path.dirname(os.path.abspath(__file__))


class Cli:
    """One ccwho kill/clean run on a fake machine."""

    ROWS = [{"sessionId": LIVE, "name": "liveapp-b2", "title": "fix the gate", "pid": 10}]

    def __init__(self, w=None, tty=True, answer="y", env=None, ends=None, trouble=None,
                 rows=None):
        self.m = Machine([w or world()], ends=ends)
        self.rows = self.ROWS if rows is None else rows
        self.rows_read = 0
        self.log, self.stop_answer, self.stop_ends, self.slept = [], (0, ""), True, []
        self.t = 5000.0                     # a monotonic clock starts anywhere
        self.tty, self.answer, self.env = tty, answer, dict(env or {})
        self.trouble, self.asked, self.builds, self.carried = trouble, [], [], []

    def build(self, mine=None, status=None):
        self.builds.append(mine)
        if self.trouble:
            status["trouble"] = self.trouble
        w = self.m.worlds[0]
        if len(self.builds) == 1 and not self.stopped():    # the list: the world as given
            return dict(w, mine=mine) if mine is not None else w
        # after `claude stop`, or a later read: the machine as it is now
        self.m.builds = max(self.m.builds, 1)
        return self.m.build(mine=mine, status=status if status is not None else {})

    def read_rows(self):
        self.rows_read += 1
        if isinstance(self.rows, BaseException):
            raise self.rows
        return self.rows

    def ask(self, prompt):
        self.asked.append(prompt)
        if self.answer is None:
            raise AssertionError("an agent or --yes run must never be asked")
        return self.answer

    def carry(self, mode, target, confirmed, force=False, mine=None):
        self.log.append(("carry", mode))
        self.carried.append((mode, target, [e["pid"] for e in confirmed], force, mine))
        return engine.carry_out(mode, target, confirmed, force=force, mine=mine, act=self.m.act())

    def run(self, *argv, clean=False):
        out = io.StringIO()
        seams = {"build": self.build, "ask": self.ask, "tty": lambda: self.tty,
                 "env": self.env, "carry": self.carry, "rows": self.read_rows}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = runner.kill_cli(list(argv), clean=clean, seams=seams)
        return rc, out.getvalue()

    def claude_stop(self, sid, config_dir=None):
        self.log.append(("stop", sid))
        self.config_dirs = getattr(self, "config_dirs", []) + [config_dir]
        if isinstance(self.stop_answer, BaseException):
            raise self.stop_answer
        if self.stop_ends and self.stop_answer[0] == 0:
            self.m.alive.pop(10, None)          # the session's claude ends; 11 is launchd's
            self.m.builds = max(self.m.builds, 1)   # every read from here on sees it
        return self.stop_answer

    def gone(self, row):
        return row.get("pid") not in self.m.alive

    def sleep(self, secs):
        self.slept.append(secs)
        self.t += secs
        if sum(self.slept) > 100:               # a wait that never ends fails, not hangs
            raise AssertionError("the wait does not end")

    def stop(self, *argv):
        out = io.StringIO()
        seams = {"build": self.build, "ask": self.ask, "tty": lambda: self.tty,
                 "env": self.env, "carry": self.carry, "rows": self.read_rows,
                 "stop": self.claude_stop, "gone": self.gone,
                 "sleep": self.sleep, "clock": lambda: self.t}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = runner.stop_cli(list(argv), seams=seams)
        return rc, out.getvalue()

    def stopped(self):
        return [x[1] for x in self.log if x[0] == "stop"]

    def sent(self):
        return [p for p, _s in self.m.sent]


class TestUsage(unittest.TestCase):
    def test_what_is_no_target(self):
        for argv in ([], ["20", "21"], ["a b"], ["-x-"], [".."], [":99999"], [":"], ["-5"], ["--yes"],
                     ["20", "--bogus"], ["1.5"], ["20", "--mine"], ["²"], ["0"], [":0"],
                     ["99999999"], ["٣"], [":000080"], [":0003000"], ["00"], ["020"]):
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


class TestKillCleanAndStopNameThemselves(unittest.TestCase):
    """Every error line starts with `ccwho <command>:`, and a machine or a
    session list that could not be read is "could not tell": exit 4 (the
    owner's CLI revamp, 2026-10-06)."""

    def test_a_machine_not_read_whole(self):
        for clean, cmd, argv in ((False, "kill", ("20",)), (True, "clean", ("--yes",))):
            with self.subTest(cmd=cmd):
                c = Cli(trouble="a live session's pid could not be read")
                rc, out = c.run(*argv, clean=clean)
                self.assertEqual(rc, 4, out)
                self.assertIn(f"ccwho {cmd}: a live session's pid could not be read"
                              " - nothing killed", out)
        c = Cli(rows=BG, trouble="a live session's pid could not be read")
        rc, out = c.stop("liveapp-b2", "--and-procs", "--yes")        # it reads the machine then
        self.assertEqual(rc, 4, out)
        self.assertIn("ccwho stop: a live session's pid could not be read - nothing stopped", out)

    def test_why_nothing_is_killed_names_the_command(self):
        # the plan's own reason (engine.kill_list_lines), on the command line
        rc, out = Cli().run(":9", "--yes")
        self.assertEqual(rc, 1, out)
        self.assertIn("ccwho kill: nothing holds :9", out)

    def test_a_source_the_plan_could_not_read(self):
        # lsof, the environments (review 1 of the CLI revamp, slice 3b)
        def unread(c, **gone):
            real = c.build                       # the machine is whole; what kill reads is not
            c.build = lambda mine=None, status=None: dict(real(mine=mine, status=status), **gone)
            return c
        rc, out = unread(Cli(), ports=None).run(":3000", "--yes")
        self.assertEqual(rc, 4, out)
        self.assertIn("ccwho kill: the listening ports were not read", out)
        rc, out = unread(Cli(env={"CLAUDE_CODE_SESSION_ID": LIVE}, answer=None), marks=None).run(
            "--mine", "--yes", clean=True)
        self.assertEqual(rc, 4, out)
        self.assertIn("ccwho clean: the environments were not read", out)

    def test_the_machine_not_read_again_after_the_yes(self):
        # what was confirmed is read again before a signal: unread, nothing goes
        c = Cli()
        c.ask = lambda prompt: setattr(c.m, "trouble", ("the process table could not be read", None)) or "y"
        rc, out = c.run("20")
        self.assertEqual((rc, c.sent()), (4, []), out)
        self.assertIn("ccwho kill: the process table could not be read - nothing killed", out)

    def test_a_stop_whose_end_cannot_be_confirmed(self):
        c = Cli(rows=BG)
        c.gone = lambda row: None
        rc, out = c.stop("liveapp-b2", "--yes")
        self.assertEqual(rc, 4, out)
        self.assertIn("its end could not be confirmed", out)
        c = Cli(rows=BG)                                                 # control
        c.gone = lambda row: False
        rc, out = c.stop("liveapp-b2", "--yes")
        self.assertEqual(rc, 1, out)
        self.assertIn("the session still runs", out)

    def test_stop_s_own_lines_name_stop(self):
        # the plan's why and the report, through stop (kill_list_lines' lead)
        # its session left nothing running: the plan says why
        rc, out = Cli(w=world(drop=(11,)), rows=BG).stop("liveapp-b2", "--and-procs", "--dry-run")
        lines = out.splitlines()
        self.assertTrue(any(line.startswith("ccwho stop: ") for line in lines), out)
        self.assertFalse([line for line in lines if line.startswith("ccwho: ")], out)

    def test_stop_s_report_names_stop(self):
        # the machine not read again once the session stopped: its report
        c = Cli(rows=BG)
        stop = c.claude_stop

        def stopped_then_unread(sid, config_dir=None):
            c.m.trouble = ("the process table could not be read", None)
            return stop(sid, config_dir)
        c.claude_stop = stopped_then_unread
        rc, out = c.stop("liveapp-b2", "--and-procs", "--yes")
        self.assertEqual(rc, 4, out)
        self.assertIn("ccwho stop: the process table could not be read - nothing killed", out)
        self.assertFalse([line for line in out.splitlines() if line.startswith("ccwho: ")], out)

    def loose(self):
        # a parent that exited while ps ran left a subtree that holds a session's
        # work: the table was not read whole (procs.kill_plan)
        return world(extra={40: (777, T, "node loose.js")}, marks={40: ("claude", LIVE)})

    def test_a_process_table_not_read_whole(self):
        # review 2 of the CLI revamp, slice 3b
        for clean, cmd, argv in ((False, "kill", ("20", "--yes")), (True, "clean", ("--yes",))):
            with self.subTest(cmd=cmd):
                rc, out = Cli(w=self.loose()).run(*argv, clean=clean)
                self.assertEqual(rc, 4, out)
                self.assertIn(f"ccwho {cmd}: the process table was not read whole", out)
        rc, out = Cli().run("20", "--yes")                                     # control
        self.assertEqual(rc, 0, out)

    def test_stop_and_procs_on_a_table_not_read_whole(self):
        for argv in (("--dry-run",), ("--yes",)):
            with self.subTest(argv=argv):
                rc, out = Cli(w=self.loose(), rows=BG).stop("liveapp-b2", "--and-procs", *argv)
                self.assertEqual(rc, 4, out)
        rc, out = Cli(w=world(drop=(11,)), rows=BG).stop("liveapp-b2", "--and-procs",
                                                          "--dry-run")         # control
        self.assertEqual(rc, 0, out)

    def test_a_session_list_not_read(self):
        c = Cli(rows=OSError("claude agents failed"))
        rc, out = c.run("liveapp-b2", "--yes")
        self.assertEqual(rc, 4, out)
        self.assertIn("ccwho kill: the sessions could not be read", out)

    def test_no_session_matches(self):
        rc, out = Cli().run("nomatch-x", "--yes")
        self.assertEqual(rc, 1, out)
        self.assertIn("ccwho kill: no live session matches 'nomatch-x'", out)
        rc, out = Cli(rows=BG).stop("nomatch-x", "--yes")
        self.assertEqual(rc, 1, out)
        self.assertIn("ccwho stop: no live session matches 'nomatch-x'", out)


class TestASession(unittest.TestCase):
    """`ccwho kill <session>`: what a live session started (the owner's D12),
    named as `ccwho open` names it - a short id or a name."""

    def test_by_short_id_and_by_name(self):
        for word in ("aaaa", "liveapp", "AAAA1111"):
            with self.subTest(word=word):
                c = Cli()
                rc, out = c.run(word)
                self.assertEqual(c.carried[0][0:2], ("session", LIVE))
                self.assertEqual(c.sent(), [11])
                self.assertIn("node server.js", out)
                self.assertEqual(rc, 0)

    def test_it_asks_first(self):
        c = Cli(answer="n")
        rc, out = c.run("liveapp")
        self.assertEqual((rc, c.sent()), (1, []))
        self.assertEqual(len(c.asked), 1)

    def test_two_sessions_match_lists_them_and_does_nothing(self):
        rows = Cli.ROWS + [{"sessionId": OTHER, "name": "liveapp-c3", "title": "t", "pid": 12}]
        c = Cli(rows=rows)
        rc, out = c.run("liveapp")
        self.assertEqual(rc, 2)
        self.assertIn("matches 2 sessions", out)
        self.assertIn("liveapp-c3", out)
        self.assertEqual((c.builds, c.sent()), ([], []))

    def test_no_session_matches(self):
        c = Cli()
        rc, out = c.run("nosuch")
        self.assertEqual(rc, 1)
        self.assertIn("no live session matches 'nosuch'", out)
        self.assertIn("ccwho clean", out)                  # what an ended one left
        self.assertEqual(c.builds, [])

    def test_the_sessions_not_read_kills_nothing(self):
        c = Cli(rows=OSError("no feed"))
        rc, out = c.run("liveapp")
        self.assertEqual(rc, 4, "could not tell")
        self.assertIn("nothing killed", out)
        self.assertNotIn("no feed", out)
        self.assertEqual(c.builds, [])

    def test_the_list_names_the_session_the_word_found(self):
        c = Cli(answer="n")
        rc, out = c.run("liveapp")
        self.assertIn(engine.pick_line(Cli.ROWS[0]), out)
        self.assertLess(out.index(engine.pick_line(Cli.ROWS[0])), out.index("node server.js"))

    DIGITS = "48210000-0000-4000-8000-000000000009"

    def digits_world(self, sid):
        w = world()
        w["sessions"] = [{"sessionId": sid, "pid": 10}]
        w["att"] = procs.attribute(w["table"], w["marks"], w["ports"], w["sessions"], own=99)
        return w

    def test_digits_that_are_also_a_short_id_ask_which(self):
        # the check uses the sessions the world already read: never the feed
        c = Cli(w=self.digits_world(self.DIGITS), rows=AssertionError("the feed was read"))
        rc, out = c.run("4821", "--yes")
        self.assertEqual((rc, c.carried), (2, []))
        self.assertIn(f"ccwho kill {self.DIGITS}", out)
        self.assertIn("ccwho kill 4821 --pid", out)
        c = Cli(w=self.digits_world(self.DIGITS))           # --pid: the pid
        rc, out = c.run("4821", "--pid", "--yes")
        self.assertEqual(rc, 1)
        self.assertIn("4821 already exited", out)
        c = Cli(w=self.digits_world("00004821-0000-4000-8000-000000000009"))
        rc, out = c.run("4821", "--yes")                    # control: in the id, not its start
        self.assertIn("4821 already exited", out)
        c = Cli(w=self.digits_world(self.DIGITS))           # control: a pid that is no id start
        c.run("20", "--yes")
        self.assertEqual(c.carried[0][0], "pid")

    def test_a_pid_or_port_kill_reads_no_session_feed(self):
        for argv in (("20", "--yes"), (":3000", "--yes"), ("20", "--dry-run")):
            with self.subTest(argv=argv):
                c = Cli()
                c.run(*argv)
                self.assertEqual((c.rows_read, len(c.builds)), (0, 1))

    def test_a_full_id_and_a_zero_led_short_id_name_a_session(self):
        for word in (self.DIGITS, "0123", "0020", "0123abcd-0000-4000-8000-000000000009"):
            with self.subTest(word=word):
                self.assertEqual(runner._kill_target([word], False), ("query", word))
        rows = [{"sessionId": self.DIGITS, "name": "calc-a1", "title": "t", "pid": 13}]
        c = Cli(rows=rows, answer="n")
        c.run(self.DIGITS)
        self.assertIn("calc-a1", c.run(self.DIGITS)[1])

    def test_a_loose_word_with_yes_names_the_session_and_does_nothing(self):
        # --yes: nobody reads the list, so the word must name the session exactly
        for word in ("gate", "e", "liveapp"):
            with self.subTest(word=word):
                c = Cli()
                rc, out = c.run(word, "--yes")
                self.assertEqual((rc, c.carried), (2, []))
                self.assertIn(engine.pick_line(Cli.ROWS[0]), out)
                self.assertIn("name it exactly", out)
        for word in ("liveapp-b2", "LIVEAPP-B2", "aaaa", LIVE):         # control: exact
            with self.subTest(word=word):
                c = Cli()
                c.run(word, "--yes")
                self.assertEqual(c.carried[0][0:2], ("session", LIVE))
        c = Cli(env={"CLAUDE_CODE_SESSION_ID": LIVE}, answer=None)      # an agent: the same
        rc, out = c.run("gate")
        self.assertEqual((rc, c.carried), (2, []))

    def test_a_tty_names_a_session_exactly(self):
        rows = [dict(Cli.ROWS[0], tty="ttys032")]
        for word in ("s032", "ttys032"):
            with self.subTest(word=word):
                c = Cli(rows=rows)
                c.run(word, "--yes")
                self.assertEqual(c.carried[0][0:2], ("session", LIVE))

    def test_a_zero_led_short_id_is_at_most_eight_long(self):
        self.assertEqual(runner._kill_target(["01234567"], False), ("query", "01234567"))
        self.assertIsNone(runner._kill_target(["012345678"], False))

    def test_pid_is_for_a_pid_only(self):
        for argv in (["liveapp", "--pid"], [":3000", "--pid"], [self.DIGITS, "--pid"]):
            with self.subTest(argv=argv):
                c = Cli()
                rc, out = c.run(*argv)
                self.assertEqual((rc, c.builds), (2, []))
                self.assertIn("usage", out)

    def test_an_interrupt_while_the_sessions_are_read(self):
        for word in ("liveapp",):
            with self.subTest(word=word):
                c = Cli(rows=KeyboardInterrupt())
                try:
                    rc, out = c.run(word)
                except KeyboardInterrupt:       # unittest would stop the whole run
                    self.fail("the interrupt escaped kill_cli")
                self.assertEqual((rc, c.builds), (130, []))
                self.assertIn("interrupted - nothing killed", out)

    def test_a_feed_not_read_is_not_no_match(self):
        real = engine.collect
        def collect(cache=None, status=None, **kw):
            status["source_ok"] = False
            return [], []
        engine.collect = collect
        self.addCleanup(setattr, engine, "collect", real)
        c = Cli()
        c.read_rows = runner._live_rows
        rc, out = c.run("liveapp")
        self.assertEqual(rc, 4, "could not tell")
        # the feed's own answer, not a fake that could not take the scan's words
        # (review 3: a TypeError said "could not be read" too)
        self.assertIn("could not be read (LookupError)", out)
        self.assertNotIn("no live session matches", out)

    def test_a_pid_is_never_a_session(self):                           # control
        c = Cli(rows=[{"sessionId": LIVE, "name": "20", "pid": 10}])
        c.run("20")
        self.assertEqual(c.carried[0][0], "pid")

    def test_an_agent_takes_only_its_own(self):
        rows = Cli.ROWS + [{"sessionId": DEAD, "name": "other-d4", "title": "t", "pid": 13}]
        c = Cli(env={"CLAUDE_CODE_SESSION_ID": LIVE}, answer=None, rows=rows)
        c.run("liveapp-b2")
        self.assertEqual(c.sent(), [11])
        c = Cli(env={"CLAUDE_CODE_SESSION_ID": LIVE}, answer=None, rows=rows)
        rc, out = c.run("other-d4")
        self.assertEqual((rc, c.sent()), (1, []))
        self.assertIn("only its own", out)


BG = [dict(Cli.ROWS[0], kind="background")]


class TestStop(unittest.TestCase):
    """`ccwho stop <session> [--and-procs]` (the owner's D11, 2026-09-27): a
    background session is stopped with `claude stop <id>`, which keeps its
    conversation; an interactive one is never signalled."""

    def test_a_background_session_is_stopped_after_the_question(self):
        c = Cli(rows=BG)
        rc, out = c.stop("liveapp-b2")
        self.assertEqual((rc, c.stopped(), len(c.asked)), (0, [LIVE], 1))
        self.assertIn("stopped", out)
        self.assertIn("claude attach", out)                 # how to get it back
        self.assertEqual(c.sent(), [])                      # its processes: not without --and-procs

    def test_no_means_nothing(self):
        c = Cli(rows=BG, answer="n")
        rc, out = c.stop("liveapp-b2")
        self.assertEqual((rc, c.stopped()), (1, []))
        self.assertIn("nothing stopped", out)

    def test_no_terminal_needs_yes(self):
        c = Cli(rows=BG, tty=False)
        rc, out = c.stop("liveapp-b2")
        self.assertEqual((rc, c.stopped()), (3, []))
        self.assertIn("add --yes", out)
        c = Cli(rows=BG, tty=False, answer=None)            # control: --yes, never asked
        rc, out = c.stop("liveapp-b2", "--yes")
        self.assertEqual((rc, c.stopped()), (0, [LIVE]))

    def test_dry_run_stops_nothing(self):
        c = Cli(rows=BG)
        rc, out = c.stop("liveapp-b2", "--dry-run", "--and-procs")
        self.assertEqual((rc, c.stopped(), c.sent(), c.asked), (0, [], [], []))
        self.assertIn("dry run", out)
        self.assertIn("node server.js", out)                # what --and-procs would take

    def test_an_interactive_session_is_never_signalled(self):
        for rows in (Cli.ROWS, [dict(Cli.ROWS[0], kind="interactive", tty="ttys012")]):
            with self.subTest(kind=rows[0].get("kind")):
                c = Cli(rows=rows)
                rc, out = c.stop("liveapp-b2", "--and-procs", "--yes")
                self.assertEqual((rc, c.stopped(), c.sent(), c.builds), (1, [], [], []))
                self.assertIn("runs in a window", out)
                self.assertIn("ccwho open liveapp-b2", out)

    def test_an_agent_stops_no_session(self):
        c = Cli(rows=BG, env={"CLAUDE_CODE_SESSION_ID": LIVE}, answer=None)
        rc, out = c.stop(LIVE, "--yes")
        self.assertEqual((rc, c.stopped()), (1, []))
        self.assertIn("an agent does not stop", out)

    def test_and_procs_asks_once_stops_then_kills(self):
        c = Cli(rows=BG)
        rc, out = c.stop("liveapp-b2", "--and-procs")
        self.assertEqual(len(c.asked), 1)
        self.assertIn("node server.js", out)
        self.assertLess(out.index("node server.js"), out.index("stopped"))
        self.assertEqual(c.log[:2], [("stop", LIVE), ("carry", "session")])
        self.assertEqual(c.sent(), [11])
        self.assertEqual(rc, 0)

    def test_a_failed_stop_kills_nothing(self):
        for answer in ((1, "no such session"), OSError("no claude"),
                       subprocess.TimeoutExpired("claude", 30)):
            with self.subTest(answer=type(answer).__name__):
                c = Cli(rows=BG)
                c.stop_answer = answer
                rc, out = c.stop("liveapp-b2", "--and-procs")
                self.assertEqual((rc, c.sent()), (1, []))
                self.assertIn("could not stop", out)
                self.assertNotIn("no claude", out)

    def test_what_it_left_running_is_said(self):
        # S3: `claude stop` leaves a nohup server running - say so, with the kill
        c = Cli(rows=BG)
        rc, out = c.stop("liveapp-b2")
        self.assertIn("still running: 11 node server.js - ccwho kill 11", out)

    def test_the_word_rules_are_kills(self):
        c = Cli(rows=BG)
        rc, out = c.stop("gate", "--yes")                   # loose, no one reads
        self.assertEqual((rc, c.stopped()), (2, []))
        c = Cli(rows=BG + [dict(BG[0], sessionId=OTHER, name="liveapp-c3")])
        rc, out = c.stop("liveapp")
        self.assertEqual((rc, c.stopped()), (2, []))

    def test_usage(self):
        for argv in ([], ["20"], [":3000"], ["a", "b"], ["liveapp-b2", "--force"],
                     ["liveapp-b2", "--pid"]):
            with self.subTest(argv=argv):
                c = Cli(rows=BG)
                rc, out = c.stop(*argv)
                self.assertEqual((rc, c.stopped(), c.rows_read), (2, [], 0))
                self.assertIn("usage: ccwho stop", out)

    def test_an_interrupt_at_the_question_stops_nothing(self):
        c = Cli(rows=BG)
        def ask(prompt):
            raise KeyboardInterrupt
        c.ask = ask
        try:
            rc, out = c.stop("liveapp-b2")
        except KeyboardInterrupt:
            self.fail("the interrupt escaped stop_cli")
        self.assertEqual((rc, c.stopped()), (130, []))

    def test_the_job_is_named_as_claude_knows_it(self):
        # measured 2026-09-27: `claude stop <full id>` says "No job matching";
        # the job id is the session id's first eight, in the session's config dir
        c = Cli(rows=[dict(BG[0], configDir="/Users/x/.claude-work")])
        rc, out = c.stop("liveapp-b2", "--yes")
        self.assertEqual(c.config_dirs, ["/Users/x/.claude-work"])
        self.assertIn("claude stop aaaa1111", out)
        self.assertIn("CLAUDE_CONFIG_DIR=/Users/x/.claude-work claude attach aaaa1111", out)
        seen = []
        real = subprocess.run
        def run(argv, **kw):
            seen.append((argv, dict(kw["env"])))
            return subprocess.CompletedProcess(argv, 0, "", "")
        subprocess.run = run
        self.addCleanup(setattr, subprocess, "run", real)
        # ccwho's own shell may name another dir: a default-dir session has none
        os.environ["CLAUDE_CONFIG_DIR"] = "/Users/x/.claude-other"
        self.addCleanup(os.environ.pop, "CLAUDE_CONFIG_DIR", None)
        for conf in ("/Users/x/.claude-work", None, ""):
            runner._claude_stop(LIVE, conf)
        self.assertEqual([a for a, _ in seen], [["claude", "stop", "aaaa1111"]] * 3)
        self.assertEqual(seen[0][1]["CLAUDE_CONFIG_DIR"], "/Users/x/.claude-work")
        for _argv, env in seen[1:]:
            self.assertNotIn("CLAUDE_CONFIG_DIR", env)
            self.assertIn("PATH", env)

    def test_a_stop_that_leaves_the_session_running_kills_nothing(self):
        c = Cli(rows=BG)
        c.stop_ends = False                     # claude said 0, the session runs on
        rc, out = c.stop("liveapp-b2", "--and-procs")
        self.assertEqual((rc, c.sent()), (1, []))
        self.assertIn("still runs", out)
        self.assertNotIn("stopped - its conversation", out)
        self.assertGreater(len(c.slept), 0)
        self.assertLessEqual(sum(c.slept), 10)              # bounded

    def test_what_it_left_includes_what_is_named_apart(self):
        extra = {12: (10, T, "npm exec chrome-devtools-mcp@latest")}
        c = Cli(rows=BG, w=world(extra=extra, marks={12: ("claude", LIVE)}))
        rc, out = c.stop("liveapp-b2")
        self.assertIn("still running: 11 node server.js - ccwho kill 11", out)
        self.assertIn("still running: 12 ", out)
        self.assertIn("ccwho kill 12", out)

    def test_what_it_left_not_read_is_said(self):
        c = Cli(rows=BG)
        c.m.trouble = ("the sessions were not all read", 2)
        rc, out = c.stop("liveapp-b2")
        self.assertEqual(rc, 0)
        self.assertIn("what it left running: run ccwho ps", out)
        self.assertNotIn("still running:", out)
        c = Cli(rows=BG)                            # any error: its type at most, never its text
        real = c.m.build
        def build(mine=None, status=None):
            if c.stopped():
                raise RuntimeError("secret-path")
            return real(mine=mine, status=status)
        c.m.build = build
        rc, out = c.stop("liveapp-b2")
        self.assertEqual(rc, 0)
        self.assertIn("run ccwho ps", out)
        self.assertNotIn("secret-path", out)

    def test_a_window_on_it_is_named_in_the_question(self):
        c = Cli(rows=[dict(BG[0], tty="ttys012", windowed=True)], answer="n")
        rc, out = c.stop("liveapp-b2")
        self.assertIn("open in a window (s012)", c.asked[0])
        for row in (BG[0], dict(BG[0], tty="ttys012", windowed=False)):   # control: no window
            c = Cli(rows=[row], answer="n")
            c.stop("liveapp-b2")
            self.assertNotIn("window", c.asked[0])

    def test_an_interrupt_during_the_stop_or_the_kill(self):
        c = Cli(rows=BG)
        c.stop_answer = KeyboardInterrupt()
        try:
            rc, out = c.stop("liveapp-b2", "--and-procs")
        except KeyboardInterrupt:
            self.fail("the interrupt escaped stop_cli")
        self.assertEqual((rc, c.sent()), (130, []))
        self.assertIn("may have been stopped", out)
        c = Cli(rows=BG)
        def carry(*a, **k):
            raise KeyboardInterrupt
        c.carry = carry
        try:
            rc, out = c.stop("liveapp-b2", "--and-procs")
        except KeyboardInterrupt:
            self.fail("the interrupt escaped stop_cli")
        self.assertEqual(rc, 130)
        self.assertIn("interrupted", out)
        self.assertIn("may have been signalled", out)
        c = Cli(rows=BG)                            # during the read of what it left
        real = c.m.build
        def build(mine=None, status=None):
            if c.stopped():
                raise KeyboardInterrupt
            return real(mine=mine, status=status)
        c.m.build = build
        try:
            rc, out = c.stop("liveapp-b2")
        except KeyboardInterrupt:
            self.fail("the interrupt escaped stop_cli")
        self.assertEqual(rc, 130)
        self.assertIn("interrupted", out)
        self.assertIn("run ccwho ps", out)

    def test_a_refused_tree_exits_1_after_the_stop_as_in_the_dry_run(self):
        # 11 is piped to the session's claude: refused in the list, kill empty
        w = world()
        w["pipes"][11], w["pipes"][10] = {10}, {11}
        rc_dry, out = Cli(rows=BG, w=w).stop("liveapp-b2", "--and-procs", "--dry-run")
        c = Cli(rows=BG, w=w)
        rc, out = c.stop("liveapp-b2", "--and-procs", "--yes")
        self.assertEqual((rc_dry, rc), (1, 1))
        self.assertEqual(c.stopped(), [LIVE])
        self.assertIn("ccwho kill 11", out[out.index("stopped - "):])     # still named
        c = Cli(rows=BG)                                                    # control
        self.assertEqual(c.stop("liveapp-b2", "--yes")[0], 0)

    def test_processes_not_known_are_no_success(self):
        # the environments not read: nobody can tell what the session started
        c = Cli(rows=BG)
        c.m.worlds[0]["marks"] = None           # the list's read; later reads fail
        rc, out = c.stop("liveapp-b2", "--and-procs", "--dry-run")
        self.assertEqual(rc, 1)
        c = Cli(rows=BG)
        c.m.worlds[0]["marks"] = None
        rc, out = c.stop("liveapp-b2", "--and-procs", "--yes")
        self.assertEqual((rc, c.stopped()), (1, [LIVE]))
        self.assertIn("run ccwho ps", out[out.index("stopped - "):])
        c = Cli(rows=BG, w=world(drop=(11,)))   # control: it left nothing
        rc, out = c.stop("liveapp-b2", "--and-procs", "--yes")
        self.assertEqual(rc, 0)

    def test_what_it_left_not_known_after_the_stop_is_said(self):
        c = Cli(rows=BG)
        real = c.m.build
        def build(mine=None, status=None):
            w = real(mine=mine, status=status)
            return dict(w, marks=None) if c.stopped() else w
        c.m.build = build
        rc, out = c.stop("liveapp-b2")
        self.assertEqual(rc, 0)                 # the stop worked; it asked for nothing more
        self.assertIn("what it left running: not known - run ccwho ps", out)

    def test_one_tree_killed_and_one_refused_exits_1(self):
        extra = {13: (10, T, "python3 worker.py"), 14: (13, T, "esbuild --service")}
        w = world(extra=extra, marks={13: ("claude", LIVE), 14: ("claude", OTHER)})
        c = Cli(rows=BG, w=w)
        rc, out = c.stop("liveapp-b2", "--and-procs", "--yes")
        self.assertEqual((rc, c.sent()), (1, [11]))

    def test_a_kill_that_signalled_nothing_says_so(self):
        for exc, code in ((KeyboardInterrupt(), 130), (RuntimeError("x"), 1)):
            with self.subTest(exc=type(exc).__name__):
                c = Cli(rows=BG)
                def carry(*a, exc=exc, **k):
                    exc.ccwho_nothing_signalled = True
                    raise exc
                c.carry = carry
                try:
                    rc, out = c.stop("liveapp-b2", "--and-procs")
                except KeyboardInterrupt:
                    self.fail("the interrupt escaped stop_cli")
                self.assertEqual(rc, code)
                self.assertIn("nothing killed", out)
                self.assertNotIn("may have been signalled", out)

    def test_a_tree_still_refused_after_the_stop_is_named(self):
        # a child of 11 carries another session's mark: refused before the stop
        # and after it
        w = world(extra={12: (11, T, "esbuild --service")}, marks={12: ("claude", OTHER)})
        c = Cli(rows=BG, w=w)
        rc, out = c.stop("liveapp-b2", "--and-procs", "--yes")
        self.assertEqual((rc, c.sent()), (1, []))
        after = out[out.index("stopped - "):]
        self.assertIn("not killed: 11 node server.js", after)
        self.assertIn("another session's mark", after)

    def test_gone_means_unlisted_and_its_claude_ended(self):
        row = dict(BG[0], pid=4242)
        real_rows, real_run = runner._live_rows, subprocess.run
        self.addCleanup(setattr, runner, "_live_rows", real_rows)
        self.addCleanup(setattr, subprocess, "run", real_run)
        asked = []
        def ps(rc, out, err=""):
            def run(argv, **kw):
                asked.append(argv)
                if isinstance(rc, BaseException):
                    raise rc
                return subprocess.CompletedProcess(argv, rc, out, err)
            return run
        for listed, rc, out, err, gone in (
                (True, 1, "", "", False),                        # still listed
                (False, 0, " 4242\n", "", False),                # its claude runs
                (False, 1, " 4242\n", "", False),                # a line: it runs
                (False, 1, "", "", True),                        # both ended
                (False, 1, "", "ps: illegal option", None),      # ps failed: not known
                (False, OSError("no ps"), "", "", None)):
            with self.subTest(listed=listed, rc=rc, err=err):
                runner._live_rows = lambda: [row] if listed else []
                subprocess.run = ps(rc, out, err)
                self.assertIs(runner._session_gone(row), gone)
        self.assertIn(["ps", "-p", "4242", "-o", "pid="], asked)
        def broken():
            raise LookupError("feed")
        runner._live_rows = broken                              # not known: None
        self.assertIsNone(runner._session_gone(row))

    def test_the_kill_reads_the_machine_after_the_stop(self):
        c = Cli(rows=BG)
        seen = []
        real = c.m.build
        def build(mine=None, status=None):
            w = real(mine=mine, status=status)
            seen.append([x["pid"] for x in w["sessions"]])
            return w
        c.m.build = build
        c.stop("liveapp-b2", "--and-procs")
        self.assertEqual(c.sent(), [11])
        self.assertTrue(seen)
        self.assertNotIn([10], seen[-1:])              # carry_out saw the session gone

    def test_an_interrupt_during_the_wait(self):
        for where in ("gone", "sleep"):
            with self.subTest(where=where):
                c = Cli(rows=BG)
                c.stop_ends = False
                def boom(*a):
                    raise KeyboardInterrupt
                setattr(c, where, boom)
                try:
                    rc, out = c.stop("liveapp-b2", "--and-procs")
                except KeyboardInterrupt:
                    self.fail("the interrupt escaped stop_cli")
                self.assertEqual((rc, c.sent()), (130, []))
                self.assertIn("may have been stopped", out)

    def test_the_wait_is_bounded_by_the_clock(self):
        c = Cli(rows=BG)
        c.stop_ends = False
        start = c.t
        def slow(row):
            c.t += 1.0                          # each read of the feed takes a second
            if c.t - start > 100:
                raise AssertionError("the wait does not end")
            return False
        c.gone = slow
        rc, out = c.stop("liveapp-b2")
        self.assertEqual(rc, 1)
        self.assertLessEqual(c.t - start, runner.STOP_WAIT + 1.5)

    def test_an_end_that_cannot_be_read_is_not_still_runs(self):
        c = Cli(rows=BG)
        c.gone = lambda row: None               # the feed could not be read
        rc, out = c.stop("liveapp-b2", "--and-procs")
        self.assertEqual((rc, c.sent()), (4, []), "could not tell")
        self.assertIn("could not be confirmed", out)
        self.assertNotIn("still runs", out)

    def test_main_routes_stop(self):
        seen = []
        real = runner.stop_cli
        runner.stop_cli = lambda argv, seams=None: seen.append(argv) or 0
        self.addCleanup(setattr, runner, "stop_cli", real)
        runner.main(["stop", "x", "--yes"])
        self.assertEqual(seen, [["x", "--yes"]])


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
        self.assertEqual((rc, c.sent(), c.asked), (4, [], []), "could not tell")
        self.assertIn("a live session's pid could not be read - nothing killed", out)

    def test_a_stop_after_signals_is_said(self):
        c = Cli(ends=set())
        c.m.sleep = lambda s: (_ for _ in ()).throw(KeyboardInterrupt)
        rc, out = c.run("20")
        self.assertEqual(rc, 1)
        self.assertIn("ccwho kill: interrupted", out)


# ------------------------------------------------ what the list shares with it

class TestPsScreenParts(unittest.TestCase):
    """The process screen, line by line: which lines the keys can be on, and
    what `x` there would kill."""

    FLEET = {"ports_ok": True, "procs_ok": True}
    LISTING = [
        {"pid": 41, "ports": [3000], "command": "vite --port 3000", "group": "session",
         "who": "liveapp - Issue 362"},
        {"pid": 20, "ports": [], "command": "npm run dev", "group": "left behind",
         "who": "left behind"},
        {"pid": None, "ports": [], "command": "?", "group": "left behind", "who": "left behind"},
    ]

    def test_the_lines_are_the_screen(self):
        parts = engine.ps_screen_parts(self.LISTING, self.FLEET, width=80)
        self.assertEqual("\n".join(line for line, _v, _f in parts),
                         engine.render_ps_screen(self.LISTING, self.FLEET, width=80))

    def test_a_process_is_its_pid(self):
        # its own field: the brief's "pid" is the session's claude, never a kill target
        parts = engine.ps_screen_parts(self.LISTING, self.FLEET, width=80)
        self.assertEqual([(v, f) for _l, v, f in parts if f == "proc"], [("41", "proc"), ("20", "proc")])

    def test_left_behind_is_cleaned_whole(self):
        parts = engine.ps_screen_parts(self.LISTING, self.FLEET, width=80)
        clean = [(l, v) for l, v, f in parts if f == "clean"]
        self.assertEqual(len(clean), 1)
        self.assertIn("LEFT BEHIND", clean[0][0].upper())

    def test_a_session_heading_is_no_part(self):                        # control
        parts = engine.ps_screen_parts(self.LISTING, self.FLEET, width=80)
        self.assertEqual([f for l, v, f in parts if l.startswith("liveapp")], [None])

    def test_a_line_break_in_a_command_moves_no_pid(self):
        listing = [dict(self.LISTING[0], command="a\nb"), dict(self.LISTING[0], pid=42, command="c")]
        parts = engine.ps_screen_parts(listing, self.FLEET, width=80)
        by_pid = {v: l for l, v, f in parts if f == "proc"}
        self.assertIn("a", by_pid["41"])
        self.assertIn("c", by_pid["42"])
        self.assertTrue(all("\n" not in l for l, _v, _f in parts))

    def test_unknown_is_one_line_and_no_part(self):
        parts = engine.ps_screen_parts([], {"collected": False})
        self.assertEqual(parts, [("processes unknown - not collected yet", None, None)])


class TestKillPrepare(unittest.TestCase):
    """What the list asks about: the machine read now, the plan, and who asks."""

    def build(self, w=None, trouble=None):
        seen = []

        def build(mine=None, status=None):
            seen.append(mine)
            if trouble:
                status["trouble"] = trouble
            ww = w or world()
            return dict(ww, mine=mine) if mine is not None else ww
        return build, seen

    def test_a_pid_is_planned_on_its_start_now(self):
        build, seen = self.build()
        p = engine.kill_prepare("pid", 20, env={}, build=build)
        self.assertEqual((p["target"], p["mine"], seen), ({"pid": 20, "start": T}, None, [None]))
        self.assertEqual(sorted(e["pid"] for e in p["plan"]["kill"]), [20, 21, 22])
        self.assertFalse(p["refused"])

    def test_an_agent_is_its_session(self):
        build, seen = self.build()
        p = engine.kill_prepare("clean", None, env={"CLAUDE_CODE_SESSION_ID": DEAD}, build=build)
        self.assertEqual((p["mine"], seen), (DEAD, [DEAD]))
        self.assertIn("clean --mine", p["plan"]["why"])

    def test_trouble_plans_nothing(self):
        build, _ = self.build(trouble="a live session's pid could not be read")
        p = engine.kill_prepare("pid", 20, env={}, build=build)
        self.assertEqual(p["plan"]["kill"], [])
        self.assertEqual(p["why"], "a live session's pid could not be read - nothing killed")

    def test_an_error_names_its_type_only(self):
        secret = "sk-ant-" + "Q" * 30

        def build(**k):
            raise RuntimeError(secret)
        p = engine.kill_prepare("pid", 20, env={}, build=build)
        self.assertEqual(p["plan"]["kill"], [])
        self.assertEqual(p["why"], "failed (RuntimeError) - nothing killed")

    def test_a_refused_tree_is_said(self):
        w = world()
        w["pipes"][30], w["pipes"][10] = {10}, {30}
        build, _ = self.build(w)
        self.assertTrue(engine.kill_prepare("clean", None, env={}, build=build)["refused"])


class TestKillText(unittest.TestCase):
    """The CLI and the list say the same thing: one list, one report."""

    def test_the_list(self):
        w = dict(world(), connections=[(22, "127.0.0.1", 3000, "8.8.8.8", 443)])
        lines = engine.kill_list_lines(procs.kill_plan("pid", {"pid": 20, "start": T}, w))
        self.assertEqual(lines[0], "3 processes to kill:")
        self.assertTrue(any(ln.lstrip().startswith("! ") and "another machine" in ln for ln in lines))
        self.assertTrue(any("npm run dev" in ln for ln in lines))

    def test_the_report(self):
        r = {"killed": [{"pid": 22}, {"pid": 21}], "survivors": [], "spare": [], "new": [],
             "ports": [":3000 is free"], "held": []}
        lines, rc = engine.kill_report_lines(r, refused=False)
        self.assertEqual((lines, rc), (["killed 2: 22 21", ":3000 is free"], 0))
        self.assertEqual(engine.kill_report_lines(r, refused=True)[1], 1)       # control


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
