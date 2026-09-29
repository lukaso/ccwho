"""Codex threads in the list (Phase 3, the owner's D16, 2026-09-29): each open
thread is a row under CODEX, last - its name, folder, where it runs, its ports.
Enter says where it runs (there is no window ccwho can bring forward); the
detail shows what it is and its processes; `x` offers "kill its processes"
(the usual box) and never a stop. The fake collector's world only."""
import unittest

import ccwho_ui as ui
from test_ui import LIVE, FakeAdapter, FakeCollector, UiTest
from test_ui_kill import KillCollector, RowBoxTest, prep

T = "01a0eca7-7b42-72f0-b19a-ff0ae32db6a3"
PROCS = {"ports_ok": True, "procs_ok": True, "sessions_ok": True, "agent_ports": [],
         "by_session": {}, "left_behind": [], "unsure": [],
         "codex": [{"pid": 30, "ports": [5173], "command": "vite --port 5173", "helper": False,
                    "orphan": False, "harness": "codex", "session": T}],
         "codex_threads": [{"thread": T, "name": "fix the navbar",
                            "cwd": "/Users/x/projects/liveapp", "host_pid": 4215,
                            "originator": "Codex Desktop", "source": "vscode",
                            "procs": 1, "ports": [5173], "pids": [30]}]}


def fleet(rows=(LIVE,), procs=PROCS):
    return ui.Fleet(list(rows), True, "12:00:00", procs=procs)


class CodexCollector(KillCollector):
    def __init__(self, procs=PROCS, rows=(LIVE,)):
        super().__init__()
        self.fleet_value = fleet(rows, procs)

    def kill_prepare(self, mode, target):
        self.prepared.append((mode, target))
        return dict(self.prep_value, mode=mode, target=target)


class TestTheCodexRows(UiTest):
    async def test_an_open_thread_is_a_row_under_codex_last(self):
        app = self.app(collector=CodexCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            text = self.screen_text(app)
            self.assertIn("CODEX", text)
            self.assertIn("fix the navbar", text)
            self.assertIn("Codex · VS Code", text)
            self.assertLess(text.index("NEEDS YOU"), text.index("CODEX"))

    async def test_the_header_counts_sessions_and_codex_apart(self):
        app = self.app(collector=CodexCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            header = str(app.query_one("#header").content)
            self.assertIn("1 sessions", header)
            self.assertIn("1 codex", header)

    async def test_no_threads_no_group(self):                            # control
        app = self.app(collector=CodexCollector(procs=dict(PROCS, codex_threads=None)))
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            self.assertNotIn("CODEX", self.screen_text(app))

    async def test_enter_says_where_it_runs_and_opens_nothing(self):
        adapter = FakeAdapter()
        app = self.app(collector=CodexCollector(), adapter=adapter)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j", "enter")
            await pilot.pause(0.2)
            self.assertEqual((adapter.asked, adapter.attached), ([], []))
            self.assertIn("VS Code", app.status)
            self.assertIn("open it there", app.status)

    async def test_its_detail_says_what_it_is_and_lists_its_processes(self):
        app = self.app(collector=CodexCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j", "right")
            await pilot.pause()
            text = str(app.query_one("#brief").content)
            for words in ("fix the navbar", "/Users/x/projects/liveapp", "VS Code", "30",
                          "vite --port 5173", ":5173"):
                self.assertIn(words, text)


class TestXOnACodexRow(RowBoxTest):
    async def test_x_offers_its_processes_and_no_stop(self):
        app = self.app(collector=CodexCollector())
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            await self.open_choices(app, pilot)
            text = self.choice_text(app)
            self.assertIn(T[-4:], app.screen.title_text)         # the row's own id
            self.assertNotIn(T[:4], app.screen.title_text)
            self.assertIn("p  kill its processes  :5173", text)
            self.assertNotIn("stop the session", text)
            self.assertIn("end it there", text)
            self.assertIn("VS Code", text)

    async def test_x_on_a_thread_with_no_processes_says_where_to_end_it(self):
        idle = dict(PROCS, codex=[], codex_threads=[dict(PROCS["codex_threads"][0],
                                                          procs=0, ports=[], pids=[])])
        c = CodexCollector(procs=idle)
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            await self.open_choices(app, pilot)
            self.assertIsNone(self.choices(app))
            self.assertIn("VS Code", app.status)
            self.assertNotIn("/exit", app.status)
            self.assertNotIn("interactive session", app.status)
            self.assertEqual(c.prepared, [])

    async def test_p_asks_about_the_threads_processes(self):
        c = CodexCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            await self.open_choices(app, pilot)
            await pilot.press("p")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [("session", T)])
            self.assertEqual(c.carried, [])
            title = str(app.screen.query_one("#killhead").content)
            self.assertIn("Codex thread", title)                # which one you confirm
            self.assertIn("fix the navbar", title)
            self.assertNotIn("session", title)
            self.assertIn(T[-4:], title)                         # the random end,
            self.assertNotIn(T[:4], title)                       # not the time bits

    async def test_x_on_a_process_in_its_detail_kills_that_process(self):
        c = CodexCollector()
        app = self.app(collector=c)
        async with app.run_test(size=(160, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j", "right", "right")
            await pilot.pause()
            brief = app.query_one("#brief")
            self.assertIs(app.focused, brief)
            procs_ = [p for p, f in brief.fields.items() if f == "proc"]
            self.assertTrue(procs_)
            brief.lit = procs_[0]
            brief.paint()
            await pilot.press("x")
            await self.settle(app, pilot)
            self.assertEqual(c.prepared, [("pid", 30)])


if __name__ == "__main__":
    unittest.main()
