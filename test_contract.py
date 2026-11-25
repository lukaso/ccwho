"""The shapes other things read: `--json` rows and the restore manifest.

These two are consumed by things this repo cannot see - a status line, a key
binding, a manifest written by a version of ccwho that is no longer installed.
So the rule is ADDITIVE ONLY: an existing key keeps its name and its meaning
forever, a new key is allowed and has to be listed here in the same commit that
adds it. A rename or a removal is a break, and it is silent everywhere except
here.

The allow-list is the test. Adding a field to the list is the deliberate act
that makes the new field legal; nothing enforces the meaning but this comment.

Stdlib only: python3 -m unittest -v
"""
import json
import os
import unittest

import ccwho_engine as ccwho

# Every key `ccwho --json` has ever emitted per row. Append only.
ROW_KEYS = {
    "project", "status", "attention", "waitingFor", "name", "title", "doing",
    "ask", "since", "ts", "topic", "first", "age", "orphans", "work", "tty",
    "pid", "sessionId", "cwd",
    "tab_title",          # added 2026-09-21: the name iTerm2 shows for that tty
    "recap", "recap_ts", "recap_age", "turns_since_recap",   # added 2026-09-21
    "windowed",           # added 2026-09-22: is there a window to go to at all
    "kind",               # added 2026-09-22: interactive, or a background session
    "configDir",          # added 2026-09-24: set for a session in another config dir
    "procs", "ports",     # added 2026-09-24: the session's work processes, their ports
    "dead_loops",         # added 2026-09-24: its wait loops that can never end
                          # 2026-09-27: and its stdin readers - each still has
                          # pid and tasks; a reader adds kind, program and root
    "entrypoint",         # added 2026-09-26: who started it - cli, or a program (sdk-*)
    "nameSource",         # added 2026-11-01: who named it - user, auto or derived
    "purpose",            # added 2026-11-02: what a role is for, "" for any other session
    "parked",             # added 2026-09-26: ids of terminals that parked this job
                          # (ctrl+b) - they show it, and have no row of their own
    "terminal",           # added 2026-09-30: the app whose tab shows it (iterm2,
                          # terminal), "" when no app ccwho knows does
    "park_note", "park_was",  # added 2026-10-08: parked in ccwho (attention
                          # "parked"): your note, and its state when not parked
    "cache", "cache_warn",  # added 2026-10-18: a large session's size and its
                          # cache's minutes ("640k ⧗19m"), and whether they run out
    "ctx", "cache_at", "compact_typed",  # added 2026-10-22: the context in tokens,
                          # when its cache's hour started, and whether a /compact was
                          # typed since (Compact at limit)
    "api_error",          # added 2026-11-24: the error it stopped on ("API Error:
                          # ..."), "" when none
}

# Every key a manifest session entry has ever carried. Append only.
MANIFEST_SESSION_KEYS = {
    "sessionId", "cwd", "project", "topic", "first", "ask", "attention",
    "status", "tty", "pid", "since",
    "configDir",          # added 2026-09-24: resume runs in that dir; older
                          # manifests lack it and resume in the default dir
    "pane", "tabTitle",   # added 2026-09-26: the iTerm2 pane and its title, so
                          # a restore resumes in the pane iTerm2 brought back;
                          # older manifests lack them and open new windows
    "terminal",           # added 2026-09-30: the app it was in; older manifests
                          # lack it: a pane id means iTerm2, else the default app
    "entrypoint",         # added 2026-10-05: what started it (cli, claude-desktop,
                          # sdk-*); `restore --open` leaves what Claude Desktop
                          # or a program ran; older manifests lack it and every
                          # entry reopens as before
}

MANIFEST_TOP_KEYS = {
    "version", "savedAt", "count", "skipped", "skippedWhy", "sessions",
    "boot",               # added 2026-10-01: the boot it was saved in; a save that
                          # could not ask iTerm2 copies panes only from a save of
                          # this boot; older manifests lack it and give none
}

SESSION = {"pid": 4242, "cwd": "/Users/x/projects/liveapp", "kind": "interactive",
           "startedAt": 1788200000000, "sessionId": "4f2b91ac-1111-4222-8333-abc",
           "name": "liveapp-4e", "status": "idle"}
HEAD = [json.dumps({"type": "user", "timestamp": "2026-09-01T10:00:00.000Z",
                    "message": {"role": "user", "content": "fix the gate"}})]
TAIL = [json.dumps({"type": "assistant", "timestamp": "2026-09-01T11:00:00.000Z",
                    "message": {"role": "assistant",
                                "content": [{"type": "text", "text": "done."}]}})]


class TestJsonRowShape(unittest.TestCase):
    def setUp(self):
        self.row = ccwho.build_row(SESSION, HEAD, TAIL, mtime=1788203600, now=1788207200)

    def test_no_key_was_removed_or_renamed(self):
        missing = ROW_KEYS - set(self.row)
        self.assertEqual(missing, set(),
                         "a consumer of --json reads these by name")

    def test_a_new_key_has_to_be_declared_here(self):
        undeclared = set(self.row) - ROW_KEYS
        self.assertEqual(undeclared, set(),
                         "adding a field is fine - add it to ROW_KEYS in the same commit")

    def test_the_row_is_json_serialisable(self):
        json.loads(json.dumps(self.row))          # --json would die on anything else


# Keys `ccwho ls --json` adds to every running row, next to ROW_KEYS. Append only.
LS_KEYS = {
    "account", "accountId", "provider",   # added 2026-11-17: the account the session
                                          # spends, its id, and who sells it (null: unknown)
    "ended", "errors",                    # added 2026-11-19: false on a running row; why a
                                          # null field is null ({"account": {code, fix}})
    "now", "you", "running",              # added 2026-11-20: its `ccwho now` line; true on
                                          # the caller's own session; false for a named
                                          # role with no session (null: not known)
}

# The record of one session: every session object in every --json has these
# keys, always (null when unknown), with these types (plan F). A command adds
# its own keys around them.
STR, OPT_STR, OPT_INT = (str,), (str, type(None)), (int, type(None))
SESSION_CORE = {
    # sessionId null: a named role with no session (kind "role")
    "sessionId": OPT_STR, "name": STR, "project": STR, "cwd": STR, "title": STR,
    "attention": STR, "status": STR, "purpose": STR, "kind": OPT_STR,
    "configDir": OPT_STR, "account": OPT_STR, "accountId": OPT_STR, "provider": STR,
    "tty": STR, "pid": OPT_INT, "since": STR, "ended": (bool, type(None)),
    "errors": (dict,), "now": STR, "you": (bool,), "running": (bool, type(None)),
}


def assert_record(case, rec, where):
    """`rec` is a session record: the core keys, each of its type."""
    case.assertEqual(set(SESSION_CORE) - set(rec), set(), f"{where}: a core key is missing")
    for key, types in SESSION_CORE.items():
        case.assertIsInstance(rec[key], types, f"{where}: {key} = {rec[key]!r}")
        if bool not in types:                       # a bool is an int to isinstance
            case.assertNotIsInstance(rec[key], bool, f"{where}: {key}")
    for field, why in rec["errors"].items():
        case.assertIn(field, SESSION_CORE, f"{where}: errors names a core key")
        case.assertEqual(set(why), {"code", "fix"}, f"{where}: errors.{field}")


def own_ccwho_dir(case):
    """A ccwho dir of the test's own: a scan lets go of launch claims in it."""
    import shutil
    import tempfile
    from unittest import mock
    tmp = tempfile.mkdtemp()
    case.addCleanup(shutil.rmtree, tmp, True)
    env = mock.patch.dict(os.environ, {"CCWHO_DIR": tmp})
    env.start()
    case.addCleanup(env.stop)


class TestLsJsonShape(unittest.TestCase):
    """What `ccwho ls --json` prints, not only what build_row gives: a key the
    command adds is as much a contract as a row key."""

    def setUp(self):
        own_ccwho_dir(self)

    def test_ls_json_rows_have_exactly_the_declared_keys(self):
        import contextlib
        import io
        import ccwho as runner
        row = ccwho.build_row(SESSION, HEAD, TAIL, mtime=1788203600, now=1788207200)
        real = (runner.engine.collect, runner.usage_snapshot)
        runner.engine.collect = lambda cache=None, status=None, **k: ([dict(row)], {})
        runner.usage_snapshot = lambda rows, now=None, record=True, codex_threads=(): {
            "state": "unknown"}
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(runner.ls(["--json"]), 0)
        finally:
            runner.engine.collect, runner.usage_snapshot = real
        got = json.loads(out.getvalue())[0]
        self.assertEqual(set(got) - ROW_KEYS, LS_KEYS,
                         "a key ls adds is declared in LS_KEYS in the same commit")
        self.assertEqual(ROW_KEYS - set(got), set(), "a consumer of --json reads these by name")


class TestEveryCommandGivesTheRecord(unittest.TestCase):
    """`ls --json` (running and ended rows) and `show --json`: each session is
    one record, the same keys of the same types (plan F)."""

    def setUp(self):
        import ccwho as runner
        self.runner = runner
        own_ccwho_dir(self)
        row = ccwho.build_row(SESSION, HEAD, TAIL, mtime=1788203600, now=1788207200)
        ended = {"sessionId": "77770000-0000-4000-8000-000000000000", "project": "liveapp",
                 "title": "Gate", "you_said": "fix the gate", "last_ts": "2026-09-01T11:00:00Z"}
        saved = (runner.engine.collect, runner.usage_snapshot, runner.fresh_index,
                 runner.engine.read_windows)
        self.addCleanup(lambda: (setattr(runner.engine, "collect", saved[0]),
                                 setattr(runner, "usage_snapshot", saved[1]),
                                 setattr(runner, "fresh_index", saved[2]),
                                 setattr(runner.engine, "read_windows", saved[3])))

        def collect(cache=None, status=None, **k):
            if status is not None:
                status.update(source_ok=True, session_files_bad=0)
            return [dict(row)], {}
        runner.engine.collect = collect
        runner.usage_snapshot = lambda rows, now=None, record=True, codex_threads=(): {
            "state": "unknown"}
        runner.fresh_index = lambda quiet=False: {ended["sessionId"]: ended}
        runner.engine.read_windows = lambda sid, **kw: (HEAD, TAIL, 1788203600)
        live = {"sessionId": SESSION["sessionId"]}
        self.search = lambda idx, q, live_ids=(), everything=False: [live, ended]

    def run_cmd(self, fn, argv):
        import contextlib
        import io
        out = io.StringIO()
        real = self.runner.index.search
        self.runner.index.search = self.search
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                rc = fn(argv)
        finally:
            self.runner.index.search = real
        self.assertEqual(rc, 0)
        return json.loads(out.getvalue())

    def test_ls_running_and_ended_rows_are_records(self):
        got = self.run_cmd(self.runner.ls, ["gate", "--json"])
        self.assertEqual([r["ended"] for r in got], [False, True])
        for r in got:
            assert_record(self, r, f"ls {r['sessionId']}")

    def test_a_text_key_that_is_null_in_the_row_is_empty_in_the_record(self):
        rec = ccwho.session_record({"sessionId": "x", "title": None, "cwd": None},
                                   {"state": "unknown"}, ended=True)
        assert_record(self, rec, "a row with nulls")
        self.assertEqual((rec["title"], rec["cwd"]), ("", ""))

    def test_show_is_a_record(self):
        got = self.run_cmd(self.runner.show, ["liveapp-4e", "--json"])
        assert_record(self, got, "show")
        self.assertNotIn("aka", got, "its contents are core keys")

    def test_the_readme_example_has_the_keys_show_gives(self):
        # a key show adds or drops makes the README's example stale
        got = self.run_cmd(self.runner.show, ["liveapp-4e", "--json"])
        self.assertEqual(sorted(json_example(readme_section(readme()))), sorted(got))


# Keys a Codex thread's record has beside ROW_KEYS and LS_KEYS. Append only.
CODEX_KEYS = {
    "where", "pids",      # added 2026-11-19: where its thread runs, in words (the
                          # codex CLI, VS Code, the ChatGPT app); its processes' pids
}

CODEX_ID = "019a0000-1111-7222-8333-444455556666"


def codex_thread(**over):
    """An open Codex thread as collect()'s fleet carries it (engine.codex_fleet)."""
    return dict({"thread": CODEX_ID, "cwd": "/Users/x/projects/parser",
                 "name": "fix the parser", "turn": "done", "ts": 5.0,
                 "mtime": 1788207000, "host_pid": 7777, "source": "cli",
                 "procs": 1, "ports": [], "pids": [7778]}, **over)


class _CommandCase(unittest.TestCase):
    """A command run on a fake scan: one Claude session and one Codex thread."""

    def setUp(self):
        import ccwho as runner
        self.runner = runner
        own_ccwho_dir(self)
        self.row = ccwho.build_row(SESSION, HEAD, TAIL, mtime=1788203600, now=1788207200)
        self.fleet = {"codex_threads": [codex_thread()]}
        self.source_ok = True
        saved = (runner.engine.collect, runner.usage_snapshot, runner.fresh_index)
        self.addCleanup(lambda: (setattr(runner.engine, "collect", saved[0]),
                                 setattr(runner, "usage_snapshot", saved[1]),
                                 setattr(runner, "fresh_index", saved[2])))

        def collect(cache=None, status=None, **k):
            if status is not None:
                status.update(source_ok=self.source_ok, session_files_bad=0)
            return [dict(self.row)], dict(self.fleet)
        runner.engine.collect = collect
        self.snap = {"state": "unknown"}
        runner.usage_snapshot = lambda rows, now=None, record=True, codex_threads=(): self.snap
        runner.fresh_index = lambda quiet=False: {}

    def run_cmd(self, fn, argv, rc=0):
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            got = fn(argv)
        self.err = err.getvalue()
        self.assertEqual(got, rc, self.err)
        return json.loads(out.getvalue())


class TestACodexThreadIsARecord(_CommandCase):
    """D9: `ls --json` lists the open Codex threads as records of their own -
    the account unknown and saying why, never a Codex limit id as an account."""

    def codex(self, got):
        return [r for r in got if r.get("kind") == "codex"]

    def test_ls_json_lists_the_thread_as_a_record(self):
        got = self.run_cmd(self.runner.ls, ["--json"])
        self.assertEqual([r["sessionId"] for r in got], [SESSION["sessionId"], CODEX_ID])
        rec = self.codex(got)[0]
        assert_record(self, rec, "a Codex thread")
        self.assertEqual(set(rec) - ROW_KEYS - LS_KEYS, CODEX_KEYS,
                         "a key a Codex record adds is declared in CODEX_KEYS")
        self.assertEqual((rec["account"], rec["accountId"], rec["provider"], rec["ended"]),
                         (None, None, "openai", False))
        self.assertEqual(rec["errors"]["account"]["code"], "codex-unknown")
        self.assertEqual(rec["pid"], 7777, "the process that holds the thread")
        self.assertEqual(rec["title"], "fix the parser")

    def test_a_codex_limit_is_never_an_account_id(self):
        # the usage snapshot names the thread's limit; the record does not take it
        self.snap = {"state": "ok", "accounts": [{"id": "codex:codex", "brand": "oai"}],
                     "names": {"codex:codex": "codex"}, "sessions": {CODEX_ID: "codex:codex"}}
        rec = self.codex(self.run_cmd(self.runner.ls, ["--json"]))[0]
        self.assertEqual((rec["account"], rec["accountId"]), (None, None))

    def test_words_find_a_thread_as_they_find_a_session(self):
        self.assertEqual([r["sessionId"] for r in
                          self.run_cmd(self.runner.ls, ["parser", "--json"])], [CODEX_ID])
        self.assertEqual(self.codex(self.run_cmd(self.runner.ls, ["liveapp", "--json"])), [])

    def test_threads_that_could_not_be_read_exit_4_and_say_so(self):
        self.fleet = {"codex_threads": None}
        got = self.run_cmd(self.runner.ls, ["--json"], rc=4)
        self.assertEqual([r["sessionId"] for r in got], [SESSION["sessionId"]])
        self.assertIn("Codex threads could not be read", self.err)

    def test_a_codex_read_failure_leaves_ended_sessions_ended(self):
        # the threads say nothing of a Claude session: exit 4 for the threads,
        # and an ended session the index found is still ended
        self.fleet = {"codex_threads": None}
        ended = {"sessionId": "77770000-0000-4000-8000-000000000000", "project": "liveapp",
                 "title": "Gate", "you_said": "fix the gate",
                 "last_ts": "2026-09-01T11:00:00Z"}
        self.runner.fresh_index = lambda quiet=False: {ended["sessionId"]: ended}
        real = self.runner.index.search
        self.runner.index.search = lambda idx, q, live_ids=(), everything=False: [ended]
        self.addCleanup(setattr, self.runner.index, "search", real)
        got = self.run_cmd(self.runner.ls, ["gate", "--json"], rc=4)
        rec = next(r for r in got if r["sessionId"] == ended["sessionId"])
        self.assertIs(rec["ended"], True)
        self.assertEqual(set(rec["errors"]), {"account"})
        self.assertEqual(rec["errors"]["account"]["code"], "ended")
        self.assertIn("Codex threads could not be read", self.err)

    def test_the_text_table_is_unchanged(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.runner.ls(["--no-color"]), 0)
        self.assertNotIn("parser", out.getvalue(), "slice 5 brings the text")


def _proc(pid, session, **over):
    return dict({"pid": pid, "ports": [], "command": f"node {pid}.js", "session": session},
                **over)


class TestPsJsonNamesTheSessionByReference(_CommandCase):
    """`ps --json`: each process keeps its own keys and its own pid; its session
    is a reference {sessionId, name} - never the session's keys flattened in
    (a process's pid is not the session's)."""

    def test_each_process_has_a_session_reference(self):
        self.fleet = dict(self.fleet, procs_ok=True, ports_ok=True,
                          by_session={SESSION["sessionId"]: [_proc(501, SESSION["sessionId"])]},
                          codex=[_proc(7778, CODEX_ID)],
                          left_behind=[_proc(601, "dead0000-0000-4000-8000-000000000000")],
                          unsure=[_proc(701, None, why="an app")])
        got = {p["pid"]: p for p in self.run_cmd(self.runner.ps, ["--json"])}
        self.assertEqual(got[501]["session"], {"sessionId": SESSION["sessionId"],
                                               "name": "liveapp-4e"})
        self.assertEqual(got[7778]["session"], {"sessionId": CODEX_ID, "name": ""})
        self.assertEqual(got[601]["session"],
                         {"sessionId": "dead0000-0000-4000-8000-000000000000", "name": ""})
        self.assertIsNone(got[701]["session"])
        for p in got.values():
            self.assertNotIn("sessionId", p, "the session is a reference")


class TestUsageJsonSpeaksAsTheRecords(_CommandCase):
    """`usage --json`: each account as a record names it - account, accountId,
    provider - and `sessions` are the live sessions that spend it now (the map
    `ls` reads), as references; the count of every session seen is `seen` (E5)."""

    def setUp(self):
        super().setUp()
        u = self.runner.usage
        saved = (u.accounts, u.load_readings, u.codex_rows)
        self.addCleanup(lambda: (setattr(u, "accounts", saved[0]),
                                 setattr(u, "load_readings", saved[1]),
                                 setattr(u, "codex_rows", saved[2])))
        u.load_readings = lambda directory, now, bad=None: []
        u.accounts = lambda readings, now, labels=None: [
            {"id": "login:abc", "kind": "login", "email": "a@example.com", "brand": "ant",
             "label": "a@example.com", "sessions": 3, "age": 5.0,
             "five_hour": None, "seven_day": None}]
        self.threads_asked = []

        def codex_rows(home, now, cache=None, threads=(), every=False):
            self.threads_asked.append(list(threads))
            return [{"id": "codex:codex", "kind": "codex", "brand": "oai", "email": "",
                     "label": "codex", "sessions": 9, "age": 1.0,
                     "threads": [t for t in threads if t == CODEX_ID],
                     "five_hour": None, "seven_day": None}]
        u.codex_rows = codex_rows
        self.snap = {"state": "ok", "accounts": [{"id": "login:abc", "brand": "ant"}],
                     "names": {"login:abc": "work"},
                     "sessions": {SESSION["sessionId"]: "login:abc"}}

    def test_accounts_name_their_live_sessions(self):
        got = {r["id"]: r for r in self.run_cmd(self.runner.accounts, ["--json"])}
        ant = got["login:abc"]
        self.assertEqual((ant["account"], ant["accountId"], ant["provider"]),
                         ("work", "login:abc", "anthropic"), "as ls --json names it")
        self.assertEqual(ant["sessions"], [{"sessionId": SESSION["sessionId"],
                                            "name": "liveapp-4e"}])
        self.assertEqual(ant["seen"], 3)
        self.assertNotIn("label", ant)
        self.assertNotIn("brand", ant)

    def test_a_codex_limit_has_no_account_id_and_names_its_open_threads(self):
        got = {r["id"]: r for r in self.run_cmd(self.runner.accounts, ["--json"])}
        oai = got["codex:codex"]
        self.assertEqual((oai["accountId"], oai["provider"]), (None, "openai"))
        self.assertEqual(oai["sessions"], [{"sessionId": CODEX_ID, "name": ""}])
        self.assertEqual(oai["seen"], 9)
        self.assertNotIn("threads", oai)
        self.assertIn([CODEX_ID], self.threads_asked)

    def test_threads_that_could_not_be_read_are_unknown_not_none(self):
        self.fleet = {"codex_threads": None}
        got = {r["id"]: r for r in self.run_cmd(self.runner.accounts, ["--json"], rc=4)}
        self.assertIsNone(got["codex:codex"]["sessions"], "null: not known")
        self.assertEqual(len(got["login:abc"]["sessions"]), 1, "the Claude map still holds")
        self.assertIn("ccwho usage: the Codex threads could not be read", self.err)

    def test_a_usage_snapshot_that_failed_names_no_spenders(self):
        self.snap = {"state": "unknown"}
        got = {r["id"]: r for r in self.run_cmd(self.runner.accounts, ["--json"])}
        self.assertIsNone(got["login:abc"]["sessions"], "null: not known, not none")
        self.assertEqual(got["codex:codex"]["sessions"], [{"sessionId": CODEX_ID, "name": ""}])

    def test_a_list_that_could_not_be_read_whole_exits_4(self):
        self.source_ok = False
        self.run_cmd(self.runner.accounts, ["--json"], rc=4)
        self.assertIn("ccwho usage: `claude agents --json` could not be read whole", self.err)


class TestANamedRoleIsARecord(_CommandCase):
    """D10: `ls --json` has the directory's data - each session's `now` line,
    `you`, `running` - and a named role with no session is a record of its own."""

    def setUp(self):
        super().setUp()
        import time
        import ccwho_directory as directory
        self.directory, self.time = directory, time
        self.folder = self.runner.engine.directory_dir()
        self.store("purpose", "coo", "suppliers, staff, costs")

    def store(self, kind, key, text):
        os.makedirs(self.folder, exist_ok=True)
        with open(self.directory.path_of(self.folder, kind, key), "w") as fh:
            fh.write(self.directory.entry_text(key, text, self.time.time()))

    def roles(self, got):
        return [r for r in got if r.get("kind") == "role"]

    def test_the_core_here_is_the_engine_s(self):
        # one list, two copies: a key the test drops is a key nothing checks
        self.assertEqual(set(SESSION_CORE), set(ccwho.SESSION_CORE))

    def test_a_role_with_no_session_is_a_record(self):
        got = self.run_cmd(self.runner.ls, ["--json"])
        role = self.roles(got)
        self.assertEqual(len(role), 1, got)
        rec = role[0]
        assert_record(self, rec, "a role")
        self.assertEqual((rec["sessionId"], rec["name"], rec["running"], rec["purpose"]),
                         (None, "coo", False, "suppliers, staff, costs"))
        self.assertEqual({k: v["code"] for k, v in rec["errors"].items()},
                         {"sessionId": "not-running", "account": "not-running",
                          "ended": "not-running"})
        self.assertIsNone(rec["ended"], "no session: none that ended either")
        for r in got:
            assert_record(self, r, r["name"])
        self.assertEqual([r["running"] for r in got if r["kind"] != "role"], [True, True])

    def test_on_a_partial_read_a_role_may_be_running(self):
        self.source_ok = False
        rec = self.roles(self.run_cmd(self.runner.ls, ["--json"], rc=4))[0]
        self.assertIsNone(rec["running"])
        self.assertEqual(rec["errors"]["sessionId"]["code"], "not-read-whole")

    def test_a_running_role_is_its_session_s_record(self):                 # control
        self.row = dict(self.row, name="coo", nameSource="user",
                        purpose="suppliers, staff, costs")      # as collect reads it
        got = self.run_cmd(self.runner.ls, ["--json"])
        self.assertEqual(self.roles(got), [])
        self.assertEqual(got[0]["purpose"], "suppliers, staff, costs")

    def test_a_role_parked_behind_another_row_is_running(self):
        # its terminal parked a job (ctrl+b): the job's row stands for it in the
        # table, and the role is a record of its own, running - found by its name
        parked = "cccc0000-0000-4000-8000-000000000000"
        self.fleet = dict(self.fleet, parked_sessions=[
            {"sessionId": parked, "name": "coo", "nameSource": "user",
             "kind": "interactive"}])
        self.assertEqual(self.roles(self.run_cmd(self.runner.ls, ["--json"])), [])
        got = self.run_cmd(self.runner.ls, ["coo", "--json"])
        self.assertEqual([(r["name"], r["sessionId"], r["running"], r["ended"], r["kind"])
                          for r in got], [("coo", parked, True, False, "interactive")])
        assert_record(self, got[0], "a parked role")
        self.assertEqual(got[0]["purpose"], "suppliers, staff, costs")

    def test_words_find_a_role(self):
        got = self.run_cmd(self.runner.ls, ["coo", "--json"])
        self.assertEqual([r["name"] for r in got], ["coo"])
        # and words that miss it leave it out
        self.assertEqual(self.roles(self.run_cmd(self.runner.ls, ["liveapp", "--json"])), [])

    def test_a_word_of_its_purpose_finds_a_role(self):
        # as `ls --named suppliers` does: what the role is for
        got = self.run_cmd(self.runner.ls, ["suppliers", "--json"])
        self.assertEqual([r["name"] for r in self.roles(got)], ["coo"])

    def test_you_and_now(self):
        from unittest import mock
        self.store("now", SESSION["sessionId"], "the gate")
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION["sessionId"]}):
            got = {r["name"] or r["sessionId"]: r for r in self.run_cmd(self.runner.ls, ["--json"])}
        mine = got["liveapp-4e"]
        self.assertEqual((mine["you"], mine["now"]), (True, "the gate"))
        self.assertEqual([r["you"] for k, r in got.items() if k != "liveapp-4e"], [False, False])

    def parked_caller(self, role=True):
        from unittest import mock
        parked = "cccc0000-0000-4000-8000-000000000000"
        self.row = dict(self.row, parked=[parked])
        self.fleet = dict(self.fleet, parked_sessions=[
            {"sessionId": parked, "name": "coo" if role else "app-7f",
             "nameSource": "user" if role else "derived", "kind": "interactive"}])
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": parked}):
            return [r for r in self.run_cmd(self.runner.ls, ["--json"]) if r["you"]]

    def test_a_parked_role_that_asks_is_its_own_record_alone(self):
        # its own record is the one it is: never also the job's row
        self.assertEqual([r["name"] for r in self.parked_caller()], ["coo"])

    def test_a_parked_caller_with_no_record_of_its_own_is_the_job_s_row(self):  # control
        self.assertEqual([r["name"] for r in self.parked_caller(role=False)], ["liveapp-4e"])

    def test_a_codex_thread_keeps_its_now_line(self):
        # `ccwho now` takes a Codex thread's id too (agent_id)
        self.store("now", CODEX_ID, "the parser")
        got = self.run_cmd(self.runner.ls, ["--json"])
        self.assertEqual(next(r for r in got if r["kind"] == "codex")["now"], "the parser")

    def test_show_has_the_same_keys(self):
        from unittest import mock
        self.store("now", SESSION["sessionId"], "the gate")
        real = self.runner.engine.read_windows
        self.runner.engine.read_windows = lambda sid, **kw: (HEAD, TAIL, 1788203600)
        self.addCleanup(setattr, self.runner.engine, "read_windows", real)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": SESSION["sessionId"]}):
            rec = self.run_cmd(self.runner.show, ["--json"])
        assert_record(self, rec, "show")
        self.assertEqual((rec["you"], rec["now"], rec["running"]), (True, "the gate", True))


class TestManifestShape(unittest.TestCase):
    def setUp(self):
        row = ccwho.build_row(SESSION, HEAD, TAIL, mtime=1788203600, now=1788207200)
        row["tty"] = "ttys032"
        self.man = ccwho.manifest_from_rows([row], now=1788207200)

    def test_top_level_keys_are_stable(self):
        self.assertEqual(set(self.man), MANIFEST_TOP_KEYS)

    def test_session_entry_keys_are_stable(self):
        entry = self.man["sessions"][0]
        self.assertEqual(set(entry) - MANIFEST_SESSION_KEYS, set(),
                         "declare new manifest fields here in the same commit")
        self.assertEqual(MANIFEST_SESSION_KEYS - set(entry), set(),
                         "an older ccwho reading this manifest expects these")

    def test_version_is_the_one_restore_understands(self):
        self.assertEqual(self.man["version"], ccwho.MANIFEST_VERSION)

    def test_an_entry_still_builds_a_resume_command(self):
        cmd = ccwho.restore_command(self.man["sessions"][0])
        self.assertIn("claude --resume " + SESSION["sessionId"], cmd)
        self.assertTrue(cmd.startswith("cd "))


if __name__ == "__main__":
    unittest.main()


class TestNoNameIsDefinedTwice(unittest.TestCase):
    """A second top-level `def` of the same name replaces the first without a
    word. 2026-09-27: a new `unix_sockets(pids)` was shadowed by the netstat
    `unix_sockets()` further down - every test injected a fake, so only the
    live list raised TypeError."""

    def test_each_module_defines_each_name_once(self):
        import ast
        import collections
        import glob
        here = os.path.dirname(os.path.abspath(__file__))
        files = sorted(glob.glob(os.path.join(here, "ccwho*.py"))) + [os.path.join(here, "ccwho")]
        self.assertGreater(len(files), 5, "the modules were not found")
        for path in files:
            with open(path) as f:
                tree = ast.parse(f.read())
            names = collections.Counter(
                n.name for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
            with self.subTest(module=os.path.basename(path)):
                self.assertEqual({k: v for k, v in names.items() if v > 1}, {})


class TestNoTestIsDefinedTwice(unittest.TestCase):
    """A second method of one name in a class replaces the first without a
    word, and its test never runs (two slices each added
    test_after_the_setup_tests, 2026-09-30)."""

    def test_every_test_method_has_a_name_of_its_own(self):
        import ast
        import collections
        import glob
        import os
        here = os.path.dirname(os.path.abspath(__file__))
        twice, classes = [], 0
        for path in sorted(glob.glob(os.path.join(here, "test_*.py"))):
            with open(path) as fh:
                tree = ast.parse(fh.read())
            for c in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                classes += 1
                names = collections.Counter(f.name for f in c.body
                                            if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)))
                twice += [f"{os.path.basename(path)} {c.name}.{n}" for n, k in names.items() if k > 1]
        self.assertGreater(classes, 100)                                 # control: it read them
        self.assertEqual(twice, [])


class TestEveryTestFileRunsInTheScript(unittest.TestCase):
    """`./test` is "every test, in one command": a test file it does not name
    never runs there, and a regression in it passes green (test_codex was
    missing, review 2026-09-29)."""

    @staticmethod
    def modules_run(script):
        """The modules `./test` runs whole: CORE, and the UI stage's bare ids -
        a stage that names one class of a module (module.Class) does not run
        the module (review 4 of the terminal-app slice 1)."""
        import re
        script = script.replace("\\\n", " ")
        core = re.search(r'^CORE="([^"]*)"', script, re.M).group(1).split()
        ui = re.search(r"python -m unittest ([\w. ]+?)\s+\"\$@\"", script).group(1).split()
        return set(core) | {t for t in ui if "." not in t}

    def script(self):
        import os
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test")) as fh:
            return fh.read()

    def test_every_test_module_is_named(self):
        import os
        import re
        here = os.path.dirname(os.path.abspath(__file__))
        run = self.modules_run(self.script())
        files = self.module_names(os.listdir(here))
        self.assertEqual(sorted(set(files) - run), [])
        self.assertIn("test_ui", run)                                    # control: it parsed

    @staticmethod
    def module_names(names):
        """The test modules among file names: every test_*.py."""
        import re
        return sorted(n[:-3] for n in names if re.fullmatch(r"test_\w+\.py", n))

    def test_a_name_with_digits_or_capitals_is_a_test_module(self):
        # test_[a-z_]+ skipped them: test_v2.py would never have run (2026-10-01)
        self.assertEqual(self.module_names(["test_a.py", "test_V2.py", "test_x_1.py",
                                            "testkit.py", "test_a.pyc", "test_.py.bak"]),
                         ["test_V2", "test_a", "test_x_1"])
        script = ('CORE="test_a test_b2"\n'
                  'exec uv run python -m unittest test_ui test_Ui3 test_x.Y \\\n  "$@"\n')
        self.assertEqual(self.modules_run(script), {"test_a", "test_b2", "test_ui", "test_Ui3"})

    def test_the_script_names_its_modules_once(self):
        # a list in a comment goes stale (it named eight of fourteen): CORE says
        comments = [l for l in self.script().splitlines() if l.startswith("#")]
        self.assertEqual([l for l in comments if "test_" in l], [])

    def test_a_class_a_stage_names_does_not_run_its_module(self):
        script = self.script()
        self.assertIn("test_terms.TestTheTestsPatchNamesThatExist", script)
        without = script.replace(" test_terms ", " ", 1)
        self.assertNotIn("test_terms", self.modules_run(without))
        self.assertIn("test_terms", self.modules_run(script))           # control


def readme_section(text, heading="## For scripts and agents"):
    """The README section under `heading`, up to the next `## ` heading."""
    start = text.index(heading + "\n")
    end = text.find("\n## ", start + len(heading))
    return text[start:end if end != -1 else len(text)]


def readme():
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")) as fh:
        return fh.read()


def unnamed(section, words):
    """The words `section` does not name in backticks."""
    return sorted(w for w in words if f"`{w}`" not in section)


def json_example(section):
    """The section's ```json block, parsed."""
    start = section.index("```json\n") + len("```json\n")
    return json.loads(section[start:section.index("\n```", start)])


class TestTheReadmeSaysTheContract(unittest.TestCase):
    """The README section "For scripts and agents" is what a script reads: every
    key of the record and every `errors` code is named there, and its example
    is a record (plan, slice 7). A key or a code added without a line there
    fails here."""

    def setUp(self):
        self.section = readme_section(readme())

    def test_every_key_of_the_record_is_named(self):
        self.assertEqual(unnamed(self.section, set(SESSION_CORE) | LS_KEYS | CODEX_KEYS), [])

    def test_every_errors_code_is_named(self):
        import ccwho_usage
        codes = set(ccwho_usage.ACCOUNT_ERRORS) | set(ccwho.RECORD_ERRORS)
        self.assertEqual(unnamed(self.section, codes), [])

    def test_a_section_without_a_key_is_caught(self):
        # control: the check reads the section, and a missing name is reported
        self.assertEqual(unnamed(self.section.replace("`pid`", "pid"), {"pid", "tty"}),
                         ["pid"])

    def test_the_example_is_a_record(self):
        assert_record(self, json_example(self.section), "the README example")

