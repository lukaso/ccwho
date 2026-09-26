"""Tests for the world builder: what ccwho reads from ps, sysctl and lsof,
shaped for kill_plan. Every input is built here; nothing depends on what this
machine is running. The rule that ties it together: what the builder gives
passes procs.world_problem - the reader and the check agree."""
import unittest

import ccwho_engine as engine
import ccwho_procs as procs

T = "Tue Sep 22 13:45:15 2026"
SID = "aaaa1111-0000-4000-8000-000000000001"


def lsof_fds(pid, *fds):
    """lsof -F pftdn text for one pid: fds as (fd, type, own address, name)."""
    out = [f"p{pid}"]
    for fd, typ, dev, name in fds:
        out += [f"f{fd}", f"t{typ}"] + ([f"d{dev}"] if dev else []) + [f"n{name}"]
    return "\n".join(out) + "\n"


class TestParsePsWorld(unittest.TestCase):
    """One `ps -axo pid=,ppid=,lstart=,command=` under LC_ALL=C TZ=UTC: parent,
    start and command from ONE snapshot (two ps runs can race)."""

    def test_a_row_is_parent_start_and_command(self):
        text = (f"  1     0 {T} /sbin/launchd\n"
                f" 70     1 Tue  Sep 22 13:45:15 2026 node /a b/server.js --port 3000\n")
        self.assertEqual(procs.parse_ps_world(text),
                         {1: (0, T, "/sbin/launchd"), 70: (1, T, "node /a b/server.js --port 3000")})

    def test_what_is_no_row_is_left_out(self):
        text = (f"  ² 1 {T} x\n 12345678 1 {T} x\n 70 x {T} y\n 71 1 Tue Sep\n"
                f" 72 1 {T} z\n")
        self.assertEqual(procs.parse_ps_world(text), {72: (1, T, "z")})

    def test_its_table_passes_the_check(self):
        text = f"  1 0 {T} launchd\n 98 1 {T} zsh\n 99 98 {T} python3 ccwho.py\n"
        w = {"table": procs.parse_ps_world(text), "att": {}, "sessions": [], "own": 99}
        self.assertIsNone(procs.world_problem(w))


class TestParseLsofPipes(unittest.TestCase):
    """`lsof -nP -F pftdn` for the whole machine. A pipe or socket end has its
    own address (d) and names its peer's (n->); a FIFO is its path. Peers on
    fds 0-2 are "stdio"; a pipe or FIFO on fds 3+ is "other"; a unix socket on
    fds 3+ is left out (how every process talks to a daemon)."""

    def test_a_pipeline_links_both_ends_on_stdio(self):
        text = (lsof_fds(10, ("1", "PIPE", "0xa", "->0xb"))
                + lsof_fds(20, ("0", "PIPE", "0xb", "->0xa")))
        self.assertEqual(procs.parse_lsof_pipes(text),
                         {10: {"stdio": {20}, "other": set()}, 20: {"stdio": {10}, "other": set()}})

    def test_a_pipe_on_fd_3_is_other(self):
        text = (lsof_fds(10, ("3", "PIPE", "0xa", "->0xb"))
                + lsof_fds(20, ("4", "PIPE", "0xb", "->0xa")))
        got = procs.parse_lsof_pipes(text)
        self.assertEqual(got[10], {"stdio": set(), "other": {20}})

    def test_a_socket_on_stdio_counts_and_on_fd_3_is_left_out(self):
        # what an agent starts: its stdin is a socketpair claude keeps on fd 3+
        text = (lsof_fds(10, ("5", "unix", "0xc", "->0xd"))
                + lsof_fds(40, ("0", "unix", "0xd", "->0xc")))
        got = procs.parse_lsof_pipes(text)
        self.assertEqual(got[40], {"stdio": {10}, "other": set()})
        self.assertEqual(got[10], {"stdio": set(), "other": set()})        # claude: nothing

    def test_a_fifo_links_its_holders(self):
        text = (lsof_fds(10, ("1", "FIFO", "0x1", "/tmp/f"))
                + lsof_fds(20, ("0", "FIFO", "0x1", "/tmp/f")))
        got = procs.parse_lsof_pipes(text)
        self.assertEqual(got[10]["stdio"], {20})
        self.assertEqual(got[20]["stdio"], {10})

    def test_holders_of_the_same_end_are_no_peers(self):
        # a tool shell and the dev server it forked hold the SAME stdin end
        text = (lsof_fds(10, ("5", "unix", "0xc", "->0xd"))
                + lsof_fds(40, ("0", "unix", "0xd", "->0xc"))
                + lsof_fds(41, ("0", "unix", "0xd", "->0xc")))
        got = procs.parse_lsof_pipes(text)
        self.assertEqual(got[40]["stdio"], {10})
        self.assertEqual(got[41]["stdio"], {10})

    def test_every_listed_pid_is_read_and_others_are_left_out(self):
        text = (lsof_fds(10, ("cwd", "DIR", "", "/"), ("1", "REG", "", "/tmp/out"))
                + lsof_fds(20, ("0", "CHR", "", "/dev/ttys001")))
        self.assertEqual(procs.parse_lsof_pipes(text),
                         {10: {"stdio": set(), "other": set()}, 20: {"stdio": set(), "other": set()}})

    def test_an_end_named_by_a_path_is_still_a_holder(self):
        # the other end may show a path, not "->": whoever holds that address is the peer
        text = (lsof_fds(10, ("5", "unix", "0xc", "/var/run/x.sock"))
                + lsof_fds(40, ("0", "unix", "0xd", "->0xc")))
        self.assertEqual(procs.parse_lsof_pipes(text)[40]["stdio"], {10})

    def test_a_peer_nobody_holds_is_not_read_and_a_bound_socket_is_no_link(self):
        # review 1: a counted end whose peer nobody listed - not read, not "no link"
        text = lsof_fds(10, ("0", "PIPE", "0xa", "->0xgone"), ("4", "unix", "0xe", "/var/run/x.sock"))
        self.assertEqual(procs.parse_lsof_pipes(text), {})
        text = lsof_fds(10, ("0", "CHR", "", "/dev/null"), ("4", "unix", "0xe", "/var/run/x.sock"))
        self.assertEqual(procs.parse_lsof_pipes(text), {10: {"stdio": set(), "other": set()}})

    def test_odd_text_never_raises_and_passes_the_check(self):
        for text in ("p²\nf0\ntPIPE\nd0xa\nn->0xb\n", "p0\nf0\ntPIPE\n", "p99999999\n", "x\n",
                     "p10\nf²\ntPIPE\nd0xa\nn->0xb\np20\nf0\ntPIPE\nd0xb\nn->0xa\n", ""):
            with self.subTest(text=text):
                got = procs.parse_lsof_pipes(text)
                w = {"table": {10: (1, T, "x")}, "att": {}, "sessions": [], "own": 10, "pipes": got}
                self.assertIsNone(procs.world_problem(w))


class TestWorldSessions(unittest.TestCase):
    """The session rows as kill_plan takes them: an int pid >= 2 or None, a
    str sessionId. A row whose pid cannot be read is counted, not guessed."""

    def test_rows_are_normalised(self):
        rows = [{"sessionId": SID, "pid": 10, "name": "x"}, {"sessionId": "b", "pid": "20"},
                {"sessionId": "c"}, {"sessionId": "d", "pid": None}]
        got, bad = engine.world_sessions(rows)
        self.assertEqual(got, [{"sessionId": SID, "pid": 10}, {"sessionId": "b", "pid": 20},
                               {"sessionId": "c", "pid": None}, {"sessionId": "d", "pid": None}])
        self.assertEqual(bad, 0)

    def test_a_row_that_cannot_be_read_is_counted(self):
        for pid in ("ten", 0, 1, True, 30.5, "²", [1]):
            with self.subTest(pid=pid):
                got, bad = engine.world_sessions([{"sessionId": SID, "pid": pid}])
                self.assertEqual((got, bad), ([], 1))
        got, bad = engine.world_sessions([{"sessionId": 5, "pid": 10}])
        self.assertEqual(bad, 1)


class TestBuildWorld(unittest.TestCase):
    """build_world gathers the world from readers the caller can replace; what
    it gives passes world_problem, and "not read" stays "not read"."""

    def readers(self, **over):
        ps = (f"  1 0 {T} /sbin/launchd\n 10 1 {T} claude\n 98 10 {T} /bin/zsh -c ccwho kill\n"
              f" 99 98 {T} python3 ccwho.py kill :3000\n 40 1 {T} node server.js\n")
        r = {"ps": lambda: ps, "env": lambda pid: {"CLAUDE_CODE_SESSION_ID": SID} if pid in (40, 98, 99) else {},
             "sessions": lambda table: ([{"sessionId": SID, "pid": 10}], True),
             "ports": lambda: {40: [3000]}, "connections": lambda: [], "pipes": lambda: {},
             "own": lambda: 99}
        r.update(over)
        return r

    def test_the_world_passes_the_check(self):
        w = engine.build_world(readers=self.readers())
        self.assertIsNone(procs.world_problem(w))
        self.assertEqual(w["own"], 99)
        self.assertEqual(w["marks"][40], ("claude", SID))
        self.assertIn(40, [e["pid"] for e in w["att"]["sessions"][SID]])

    def test_a_reader_that_failed_is_not_read(self):
        w = engine.build_world(readers=self.readers(ports=lambda: None, connections=lambda: None,
                                                    pipes=lambda: None))
        self.assertEqual((w["ports"], w["connections"], w["pipes"]), (None, None, None))
        self.assertIsNone(procs.world_problem(w))

    def test_an_environment_that_could_not_be_read_is_left_out(self):
        w = engine.build_world(readers=self.readers(env=lambda pid: None if pid == 40 else {}))
        self.assertNotIn(40, w["marks"])
        self.assertIsNone(w["marks"][1])

    def test_mine_is_passed_on(self):
        w = engine.build_world(mine=SID, readers=self.readers())
        self.assertEqual(w["mine"], SID)
        self.assertNotIn("mine", engine.build_world(readers=self.readers()))

    def test_a_session_row_it_cannot_read_is_said(self):
        # the live-session guard is blind to it: the caller must refuse on this
        status = {}
        w = engine.build_world(readers=self.readers(
            sessions=lambda table: ([{"sessionId": SID, "pid": "ten"}], True)), status=status)
        self.assertEqual(w["sessions"], [])
        self.assertEqual(status.get("trouble"), "a live session's pid could not be read")
        status = {}
        engine.build_world(readers=self.readers(), status=status)                   # control
        self.assertIsNone(status.get("trouble"))

    def test_the_plan_runs_on_it(self):
        w = engine.build_world(readers=self.readers())
        p = procs.kill_plan("port", 3000, w)
        self.assertNotIn("not in its shape", p.get("why", ""))


class TestTheRealWorld(unittest.TestCase):
    """The readers and the check agree on THIS machine: what build_world reads
    from the real ps, sysctl and lsof passes world_problem. Nothing is printed
    and nothing is signalled."""

    @unittest.skipUnless(__import__("sys").platform == "darwin", "reads macOS ps and lsof")
    def test_the_real_world_is_in_shape(self):
        status = {}
        w = engine.build_world(status=status)
        self.assertIsNone(procs.world_problem(w))
        self.assertIn(w["own"], w["table"])
        self.assertIsNotNone(w["pipes"], "lsof could not be asked")


class TestWorldReview1(unittest.TestCase):
    """Review 1 of the builder: a counted end whose peer nobody listed is not
    read; each side of a link is classed by its own fd; lsof failures and a
    pid whose fds were not listed are not read; an unreadable session row
    cannot be dropped silently."""

    def test_a_peer_lsof_did_not_list_makes_the_pid_not_read(self):
        # another user's claude: lsof lists none of its fds
        text = "p52\nf0\ntPIPE\nd0xb\nn->0xa\n"
        self.assertNotIn(52, procs.parse_lsof_pipes(text))
        for extra in ("f4\ntFIFO\nn/tmp/lonely\n", "f1\ntunix\nd0xe\nn->0xf\n"):
            with self.subTest(end=extra):
                self.assertNotIn(52, procs.parse_lsof_pipes("p52\n" + extra))
        got = procs.parse_lsof_pipes(text + "p51\nf1\ntPIPE\nd0xa\nn->0xb\n")          # control
        self.assertEqual(got[52]["stdio"], {51})
        for text in ("p52\nf0\ntPIPE\nd0xb\nn\n", "p52\nf5\ntunix\nd0xe\nn->0xf\n"):  # closed; a daemon socket
            with self.subTest(read=text):
                self.assertEqual(procs.parse_lsof_pipes(text), {52: {"stdio": set(), "other": set()}})

    def test_each_side_is_classed_by_its_own_fd(self):
        text = "p51\nf1\ntunix\nd0xc\nn/tmp/claude-out.sock\np60\nf0\ntunix\nd0xd\nn->0xc\n"
        got = procs.parse_lsof_pipes(text)
        self.assertEqual(got[51]["stdio"], {60})
        self.assertEqual(got[60]["stdio"], {51})
        got = procs.parse_lsof_pipes(text.replace("p51\nf1", "p51\nf5"))                # control: fd 5
        self.assertEqual(got[51], {"stdio": set(), "other": set()})
        self.assertEqual(got[60]["stdio"], {51})

    def test_two_sockets_on_fd_3_plus_are_no_link(self):
        text = "p51\nf4\ntunix\nd0xa\nn->0xb\np52\nf4\ntunix\nd0xb\nn->0xa\n"
        self.assertEqual(procs.parse_lsof_pipes(text)[51], {"stdio": set(), "other": set()})
        got = procs.parse_lsof_pipes(text.replace("p52\nf4", "p52\nf0"))              # control
        self.assertEqual(got[52]["stdio"], {51})

    def test_a_fifo_on_fd_3_plus_is_other(self):
        got = procs.parse_lsof_pipes("p10\nf4\ntFIFO\nn/tmp/f\np20\nf0\ntFIFO\nn/tmp/f\n")
        self.assertEqual(got[10], {"stdio": set(), "other": {20}})
        self.assertEqual(got[20]["stdio"], {10})                                        # control

    def test_a_pid_whose_fds_were_not_listed_is_not_read(self):
        self.assertNotIn(60, procs.parse_lsof_pipes("p60\nfNOFD\nnpermission denied\n"))
        self.assertNotIn(60, procs.parse_lsof_pipes("p60\nfcwd\ntDIR\nn/\n"))
        self.assertIn(60, procs.parse_lsof_pipes("p60\nf0\ntCHR\nn/dev/ttys001\n"))      # control

    def test_an_lsof_that_failed_is_not_read(self):
        import subprocess
        from unittest import mock
        for rc, err, want in ((1, "lsof: unsupported option", None), (2, "", None), (1, "", [])):
            with self.subTest(rc=rc, err=err):
                done = subprocess.CompletedProcess([], rc, stdout="", stderr=err)
                with mock.patch.object(engine.subprocess, "run", return_value=done):
                    self.assertEqual(engine.established_connections(), want)
                    self.assertEqual(engine.pipe_links(), None if want is None else {})

    def test_an_unreadable_session_row_cannot_be_dropped_silently(self):
        r = TestBuildWorld.readers(self, sessions=lambda table: ([{"sessionId": SID, "pid": "ten"}], True))
        with self.assertRaises(ValueError):
            engine.build_world(readers=r)
        engine.build_world(readers=TestBuildWorld.readers(self))                       # control: no row

    def test_known_sessions_need_every_claude_read(self):
        r = TestBuildWorld.readers(self, env=lambda pid: None if pid == 10 else
                                   ({"CLAUDE_CODE_SESSION_ID": SID} if pid in (40, 98, 99) else {}))
        w = engine.build_world(readers=r)
        self.assertNotIn(40, [e["pid"] for e in w["att"]["left_behind"]])
        self.assertIn(40, [e["pid"] for e in w["att"]["sessions"][SID]])            # still its session's

    def test_the_plan_on_a_built_world_takes_the_holder(self):
        w = engine.build_world(readers=TestBuildWorld.readers(self))
        p = procs.kill_plan("port", 3000, w)
        self.assertEqual([k["pid"] for k in p["kill"]], [40])
        self.assertIn("work of a live session", p["kill"][0]["note"])

    def test_in_and_out_mean_stdio(self):
        base = {"table": {10: (1, T, "claude -p x"), 20: (1, T, "tee x"), 99: (1, T, "python3 ccwho.py")},
                "att": {}, "sessions": [], "own": 99, "marks": {}}
        plans = [procs.kill_plan("pid", {"pid": 20, "start": T},
                                 dict(base, pipes={10: {key: {20}}, 20: {"stdio": {10}}, 99: set()}))
                 for key in ("in", "out", "stdio")]
        self.assertEqual(plans[0], plans[1])
        self.assertEqual(plans[1], plans[2])
        self.assertEqual(plans[0]["kill"], [])


class TestTheRealPipes(unittest.TestCase):
    @unittest.skipUnless(__import__("sys").platform == "darwin", "reads macOS lsof")
    def test_a_real_pipe_to_a_child_is_seen(self):
        import os
        import subprocess
        child = subprocess.Popen(["sleep", "20"], stdin=subprocess.PIPE)
        quiet = subprocess.Popen(["sleep", "20"], stdin=subprocess.DEVNULL)
        try:
            got = engine.pipe_links()
            self.assertIn(os.getpid(), got)
            self.assertIn(os.getpid(), got[child.pid]["stdio"])
            self.assertNotIn(os.getpid(), got[quiet.pid]["stdio"])                     # control
        finally:
            for p in (child, quiet):
                p.kill()
                p.wait()


class TestWorldReview1Gaps(unittest.TestCase):
    """What the review-1 mutants found untested."""

    def test_a_self_pipe_is_no_peer(self):
        # an event loop wakes itself through a pipe it holds both ends of
        text = ("p10\nf3\ntPIPE\nd0xa\nn->0xb\nf4\ntPIPE\nd0xb\nn->0xa\n"
                "f1\ntPIPE\nd0xc\nn->0xd\np20\nf0\ntPIPE\nd0xd\nn->0xc\n")
        got = procs.parse_lsof_pipes(text)
        self.assertEqual(got[10], {"stdio": {20}, "other": set()})

    def test_nofd_beside_a_numeric_fd_is_still_not_read(self):
        self.assertNotIn(60, procs.parse_lsof_pipes("p60\nf0\ntCHR\nn/dev/null\nfNOFD\nnerror\n"))
        self.assertIn(60, procs.parse_lsof_pipes("p60\nf0\ntCHR\nn/dev/null\n"))       # control


class TestWorldReview2(unittest.TestCase):
    """Review 2 of the builder: a unix end whose peer closed is read, with no
    link (lsof shows "->(none)" or a stale address; the kernel's socket list
    says Conn 0); a failed ps is not an empty machine."""

    STALE = "p60\nf0\ntunix\nd0x8397\nn->0x2ce9\n"

    def test_a_closed_socketpair_is_no_link(self):
        got = procs.parse_lsof_pipes("p60\nf0\ntunix\nd0x8397\nn->(none)\n")
        self.assertEqual(got, {60: {"stdio": set(), "other": set()}})
        got = procs.parse_lsof_pipes(self.STALE, sockets={"8397": "0"})
        self.assertEqual(got, {60: {"stdio": set(), "other": set()}})

    def test_a_live_peer_nobody_listed_is_still_not_read(self):
        self.assertNotIn(60, procs.parse_lsof_pipes(self.STALE, sockets={"8397": "2ce9"}))
        self.assertNotIn(60, procs.parse_lsof_pipes(self.STALE))                       # no socket list

    def test_the_kernels_socket_list_is_read(self):
        text = ("Active LOCAL (UNIX) domain sockets\n"
                "Address          Type   Recv-Q Send-Q            Inode             Conn         Refs  Nextref Addr\n"
                "83976e3f9485a142 stream      0      0                0 2ce99ba8ac0b4559                0                0\n"
                "2ce99ba8ac0b4559 stream   1810      0                0                0                0                0\n"
                "aaaa000000000001 stream      0      0 bbbb000000000002                0                0                0 /var/run/x\n")
        self.assertEqual(procs.parse_netstat_unix(text),
                         {"83976e3f9485a142": "2ce99ba8ac0b4559", "2ce99ba8ac0b4559": "0",
                          "aaaa000000000001": "0"})
        self.assertEqual(procs.parse_netstat_unix(""), {})                              # control

    def test_a_failed_ps_is_no_empty_machine(self):
        r = TestBuildWorld.readers(self, ps=lambda: None)
        with self.assertRaises(ValueError):
            engine.build_world(readers=r)
        status = {}
        engine.build_world(readers=r, status=status)
        self.assertEqual(status.get("trouble"), "the process table could not be read")
        self.assertIn(99, engine.build_world(readers=TestBuildWorld.readers(self))["table"])   # control

    @unittest.skipUnless(__import__("sys").platform == "darwin", "reads macOS lsof and netstat")
    def test_a_real_closed_socketpair_is_read(self):
        import socket
        import subprocess
        s1, s2 = socket.socketpair()
        child = subprocess.Popen(["sleep", "20"], stdin=s2.fileno(),       # its stdout: not the runner's pipe
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        s2.close()
        try:
            self.assertIn(child.pid, engine.pipe_links())                               # control: open
            s1.close()
            got = engine.pipe_links()
            self.assertIn(child.pid, got)
            self.assertEqual(got[child.pid]["stdio"], set())
        finally:
            child.kill()
            child.wait()


class TestPsReader(unittest.TestCase):
    def test_a_ps_that_failed_is_not_read(self):
        import subprocess
        from unittest import mock
        for rc, out, want in ((1, "", None), (1, f" 1 0 {T} launchd\n", None), (0, f" 1 0 {T} x\n", f" 1 0 {T} x\n")):
            with self.subTest(rc=rc, out=out):
                done = subprocess.CompletedProcess([], rc, stdout=out, stderr="")
                with mock.patch.object(engine.subprocess, "run", return_value=done):
                    self.assertEqual(engine.ps_world_text(), want)

    def test_the_first_trouble_is_kept(self):
        r = TestBuildWorld.readers(self, ps=lambda: None,
                                   sessions=lambda table: ([{"sessionId": SID, "pid": "ten"}], True))
        status = {}
        engine.build_world(readers=r, status=status)
        self.assertEqual(status["trouble"], "the process table could not be read")


if __name__ == "__main__":
    unittest.main()
