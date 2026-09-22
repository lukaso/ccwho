"""Tests for ccwho_setup: what is installed, what drifted, what to do about it.

The checks are pure over injected facts. Gathering the facts touches the world
(PATH, launchd, LaunchServices, iTerm2); deciding what they MEAN does not, and
that is the half that has to be right.

python3 -m unittest test_setup -v
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import ccwho_setup as setup

HEALTHY = {
    "claude": "/Users/x/.local/bin/claude",
    "iterm_ok": True,
    "handler_registered": True,
    "launchd_loaded": True,
    "last_run_age": 600.0,                 # the job wrote to its log 10 minutes ago
    "newest_manifest_age": 600.0,
    "cc_status_hook": True,
    "settings_path": "/Users/x/.claude/settings.json",
}


def facts(**over):
    f = dict(HEALTHY)
    f.update(over)
    return f


def by_name(results):
    return {r["name"]: r for r in results}


class TestHealthy(unittest.TestCase):
    def test_a_healthy_machine_reports_no_problems(self):
        results = setup.doctor_checks(facts())
        self.assertTrue(all(r["ok"] for r in results), [r for r in results if not r["ok"]])

    def test_every_check_says_what_it_looked_at(self):
        for r in setup.doctor_checks(facts()):
            self.assertTrue(r["name"], r)
            self.assertTrue(r["detail"], r["name"])


class TestEachFault(unittest.TestCase):
    """Every fault names the thing to run. A check that says "broken" without the
    fix just moves the search from the machine to the README."""

    def bad(self, name, **over):
        r = by_name(setup.doctor_checks(facts(**over)))[name]
        self.assertFalse(r["ok"], f"{name} should have failed")
        self.assertTrue(r["fix"], f"{name} reported a fault with no fix")
        return r

    def test_claude_off_path_is_the_first_thing_to_know(self):
        r = self.bad("claude", claude="")
        results = setup.doctor_checks(facts(claude=""))
        self.assertEqual(results[0]["name"], "claude",
                         "nothing else matters if the session list cannot be read")
        self.assertIn("PATH", r["detail"] + r["fix"])

    def test_iterm_not_scriptable_costs_the_names_and_the_jumps(self):
        r = self.bad("iterm2", iterm_ok=False)
        self.assertIn("tab", r["detail"].lower())

    def test_an_unregistered_handler_means_the_links_do_nothing(self):
        r = self.bad("ccwho:// handler", handler_registered=False)
        self.assertIn("install-handler.sh", r["fix"])

    def test_the_autosave_job_not_loaded_is_a_reboot_you_cannot_undo(self):
        r = self.bad("autosave job", launchd_loaded=False)
        self.assertIn("launchctl", r["fix"])

    def test_a_job_that_stopped_running_is_the_failure_that_happened(self):
        # a crash once found a manifest five days old: loaded, not running
        r = self.bad("autosave freshness", last_run_age=5 * 86400,
                     newest_manifest_age=5 * 86400)
        self.assertIn("5d", r["detail"])

    def test_a_job_that_never_ran_is_a_fault_not_a_blank(self):
        r = self.bad("autosave freshness", last_run_age=None,
                     newest_manifest_age=None)
        self.assertIn("never", r["detail"].lower())

    def test_a_run_inside_two_timer_windows_is_fine(self):            # control
        r = by_name(setup.doctor_checks(facts(last_run_age=25 * 60)))
        self.assertTrue(r["autosave freshness"]["ok"])

    def test_an_empty_fleet_is_not_a_broken_job(self):
        # `save` deliberately writes NOTHING when no session is restorable, so an
        # old manifest with a job that ran five minutes ago means "you had nothing
        # open", not "the timer is dead". Crying wolf here is how a real fault
        # gets ignored later.
        r = by_name(setup.doctor_checks(facts(last_run_age=300.0,
                                              newest_manifest_age=4 * 86400)))
        self.assertTrue(r["autosave freshness"]["ok"], r["autosave freshness"])
        self.assertIn("4d", r["autosave freshness"]["detail"],
                      "still say how old the newest manifest is")

    def test_the_iterm_status_hook_drift_is_reported_not_repaired(self):
        r = self.bad("iTerm2 status hook", cc_status_hook=False)
        self.assertIn("settings.json", r["detail"] + r["fix"])
        self.assertNotIn("--fix", r["fix"], "R1 doctor never writes another tool's file")


class TestHandlerDetection(unittest.TestCase):
    """The first version of this check asked LaunchServices for a bundle id that
    install-handler.sh never sets, so it reported "not registered" on a machine
    where the links work. A check that cries wolf gets ignored, and then the real
    fault is ignored with it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = os.path.join(self.tmp, "ccwho-jump.app")
        os.makedirs(os.path.join(self.app, "Contents"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plist(self, schemes):
        body = "".join(f"<string>{s}</string>" for s in schemes)
        with open(os.path.join(self.app, "Contents", "Info.plist"), "w") as fh:
            fh.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                     '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                     '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                     '<plist version="1.0"><dict>'
                     '<key>CFBundleURLTypes</key><array><dict>'
                     '<key>CFBundleURLSchemes</key><array>' + body + '</array>'
                     '</dict></array></dict></plist>')

    def test_the_installed_app_counts_as_registered(self):
        self._plist(["ccwho"])
        self.assertTrue(setup.handler_registered(app_path=self.app))

    def test_no_app_is_not_registered(self):
        self.assertFalse(setup.handler_registered(
            app_path=os.path.join(self.tmp, "nothing.app")))

    def test_an_app_that_does_not_claim_the_scheme_is_not_registered(self):
        self._plist(["something-else"])
        self.assertFalse(setup.handler_registered(app_path=self.app))

    def test_an_unreadable_plist_is_not_registered_and_does_not_raise(self):
        with open(os.path.join(self.app, "Contents", "Info.plist"), "w") as fh:
            fh.write("not a plist")
        self.assertFalse(setup.handler_registered(app_path=self.app))


class TestFactGatherersSurviveTheMachine(unittest.TestCase):
    """A doctor that dies on the machine it is diagnosing is no use, and neither
    is one that takes the watch loop down with it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmp, "restore"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_manifest_that_vanishes_mid_scan_is_not_a_crash(self):
        # the autosave prunes while doctor reads: listdir saw it, stat will not.
        # This used to escape doctor and kill --watch.
        d = os.path.join(self.tmp, "restore")
        real_listdir = os.listdir

        def lying_listdir(path):
            return list(real_listdir(path)) + ["2026-01-01T0000.json"]

        os.listdir = lying_listdir
        try:
            self.assertIsNone(setup.newest_manifest_age(self.tmp))
        finally:
            os.listdir = real_listdir

    def test_a_real_manifest_is_still_measured(self):          # control
        p = os.path.join(self.tmp, "restore", "2026-01-01T0000.json")
        open(p, "w").write("{}")
        os.utime(p, (1000.0, 1000.0))
        self.assertAlmostEqual(setup.newest_manifest_age(self.tmp, now=1600.0), 600.0)

    def test_the_log_age_is_how_we_know_the_job_ran(self):
        log = os.path.join(self.tmp, "autosave.log")
        open(log, "w").write("saved 15 session(s)\n")
        os.utime(log, (1000.0, 1000.0))
        self.assertAlmostEqual(setup.last_run_age(self.tmp, now=1300.0), 300.0)

    def test_no_log_means_the_job_never_ran_here(self):
        self.assertIsNone(setup.last_run_age(self.tmp))

    def test_the_handler_is_looked_for_where_it_was_installed(self):
        # install-handler.sh takes a path argument, so the default is not the only
        # supported place. Asserting on the candidate LIST keeps this test honest:
        # the first version passed because this machine happens to have the app in
        # the default location, which proved nothing about the env override.
        os.environ["CCWHO_JUMP_APP"] = "/somewhere/else/ccwho-jump.app"
        try:
            got = setup.handler_candidates()
        finally:
            os.environ.pop("CCWHO_JUMP_APP", None)
        self.assertEqual(got[0], "/somewhere/else/ccwho-jump.app",
                         "an explicit install path wins")
        self.assertIn(os.path.expanduser("~/Applications/ccwho-jump.app"), got)
        self.assertIn("/Applications/ccwho-jump.app", got)

    def test_an_explicit_path_is_the_only_candidate(self):
        self.assertEqual(setup.handler_candidates("/tmp/x.app"), ["/tmp/x.app"])

    def test_no_env_no_surprises(self):                    # control
        os.environ.pop("CCWHO_JUMP_APP", None)
        self.assertNotIn("", setup.handler_candidates())

    def test_the_iterm_probe_asks_iterm_not_the_process_list(self):
        # "iTerm2 is running" is not "we may script it": Automation access can be
        # denied, which is exactly when the names and the jumps stop working
        seen = {}

        def fake_run(cmd, **kw):
            seen["script"] = cmd[-1]

            class R:
                returncode, stdout, stderr = 0, "3", ""
            return R()

        real = setup.subprocess.run
        setup.subprocess.run = fake_run
        try:
            self.assertTrue(setup.iterm_scriptable())
        finally:
            setup.subprocess.run = real
        self.assertIn('tell application "iTerm2"', seen["script"])
        self.assertNotIn("System Events", seen["script"])

    def test_a_refused_automation_prompt_is_not_scriptable(self):
        class R:
            returncode, stdout, stderr = 1, "", "Not authorized to send Apple events"

        real = setup.subprocess.run
        setup.subprocess.run = lambda *a, **k: R()
        try:
            self.assertFalse(setup.iterm_scriptable())
        finally:
            setup.subprocess.run = real


class TestVerdict(unittest.TestCase):
    def test_all_clear_is_zero(self):
        self.assertEqual(setup.doctor_verdict(setup.doctor_checks(facts())), 0)

    def test_any_fault_is_nonzero(self):
        self.assertEqual(setup.doctor_verdict(setup.doctor_checks(facts(claude=""))), 1)

    def test_the_banner_names_the_worst_one_only(self):
        # a watch header has room for one line, not six
        line = setup.doctor_banner(setup.doctor_checks(facts(claude="",
                                                             handler_registered=False)))
        self.assertIn("claude", line)
        self.assertNotIn("handler", line)
        self.assertIn("ccwho doctor", line)

    def test_a_healthy_machine_has_no_banner(self):
        self.assertEqual(setup.doctor_banner(setup.doctor_checks(facts())), "")


class TestAgeWords(unittest.TestCase):
    def test_it_reads_like_the_rest_of_ccwho(self):
        self.assertEqual(setup.age_words(45), "45s")
        self.assertEqual(setup.age_words(600), "10m")
        self.assertEqual(setup.age_words(7200), "2h")
        self.assertEqual(setup.age_words(5 * 86400), "5d")
        self.assertEqual(setup.age_words(None), "never")


if __name__ == "__main__":
    unittest.main()


class TestThePlistIsRenderedForThisMachine(unittest.TestCase):
    """The committed plist used to hold one developer's home directory. It is a
    template now, because a hard-coded path is a job that loads, runs, finds
    nothing, and reports success."""

    TEMPLATE = setup.PLIST_TEMPLATE_NAME

    def render(self, **over):
        args = dict(home="/Users/sam", ccwho="/Users/sam/src/ccwho/ccwho.py",
                    claude="/Users/sam/.local/bin/claude",
                    python="/usr/bin/python3")
        args.update(over)
        with open(os.path.join(os.path.dirname(os.path.abspath(setup.__file__)),
                               self.TEMPLATE)) as fh:
            return setup.render_plist(fh.read(), **args)

    def test_the_committed_template_has_nobodys_home_in_it(self):
        path = os.path.join(os.path.dirname(os.path.abspath(setup.__file__)),
                            self.TEMPLATE)
        with open(path) as fh:
            body = fh.read()
        self.assertNotIn("/Users/", body,
                         "a template with a real home is the bug this replaces")

    def test_it_runs_the_ccwho_it_was_installed_from(self):
        out = self.render()
        self.assertIn("/Users/sam/src/ccwho/ccwho.py", out)
        self.assertIn("<string>save</string>", out)

    def test_the_path_contains_the_directory_claude_is_in(self):
        out = self.render(claude="/opt/somewhere/bin/claude")
        self.assertIn("/opt/somewhere/bin", out,
                      "launchd's PATH does not include ~/.local/bin: without the"
                      " real directory the save finds no sessions")

    def test_the_log_goes_under_this_users_home(self):
        out = self.render(home="/Users/sam")
        self.assertIn("/Users/sam/.ccwho/autosave.log", out)

    def test_nothing_is_left_unrendered(self):
        self.assertNotIn("{{", self.render())

    def test_a_rendered_plist_has_no_complaints(self):          # control
        self.assertEqual(
            setup.plist_problems(self.render(), home="/Users/sam",
                                 claude="/Users/sam/.local/bin/claude"), [])

    def test_someone_elses_home_is_a_complaint(self):
        bad = self.render(home="/Users/sam").replace("/Users/sam/.ccwho",
                                                     "/Users/other/.ccwho")
        problems = setup.plist_problems(bad, home="/Users/sam",
                                        claude="/Users/sam/.local/bin/claude")
        self.assertTrue(any("/Users/other" in p for p in problems), problems)

    def test_a_path_that_cannot_find_claude_is_a_complaint(self):
        out = self.render(claude="/opt/somewhere/bin/claude")
        problems = setup.plist_problems(out, home="/Users/sam",
                                        claude="/elsewhere/bin/claude")
        self.assertTrue(any("claude" in p for p in problems), problems)


class TestTheHotkeyProfile(unittest.TestCase):
    """A Dynamic Profile is a JSON file iTerm2 picks up by itself. The key names
    below are not guesses: they were read out of iTerm.app 3.7."""

    def profile(self, **over):
        args = dict(command="/Users/sam/.local/bin/ccwho")
        args.update(over)
        return setup.hotkey_profile(**args)["Profiles"][0]

    def test_it_is_a_dynamic_profile_document(self):
        doc = setup.hotkey_profile(command="/x/ccwho")
        self.assertEqual(list(doc), ["Profiles"])
        self.assertEqual(len(doc["Profiles"]), 1)

    def test_the_guid_is_stable_so_re_running_replaces_rather_than_adds(self):
        a = self.profile()["Guid"]
        b = self.profile(command="/somewhere/else/ccwho")["Guid"]
        self.assertEqual(a, b)

    def test_it_runs_the_command_it_was_given_and_not_a_login_shell(self):
        p = self.profile(command="/Users/sam/.local/bin/ccwho hotkey")
        self.assertEqual(p["Command"], "/Users/sam/.local/bin/ccwho hotkey")
        self.assertEqual(p["Custom Command"], "Yes")

    def test_the_window_command_names_the_verb_that_holds_it_open(self):
        self.assertEqual(setup.window_command("/Users/sam/bin/ccwho"),
                         "/Users/sam/bin/ccwho hotkey")

    def test_a_path_with_a_space_is_quoted_for_the_window(self):
        self.assertEqual(setup.window_command("/Users/sam/my tools/ccwho"),
                         "'/Users/sam/my tools/ccwho' hotkey")

    def test_option_slash_is_the_default_and_carries_every_hotkey_key(self):
        p = self.profile()
        self.assertTrue(p["Has Hotkey"])
        self.assertEqual(p["HotKey Key Code"], 44)              # the / key
        self.assertEqual(p["HotKey Modifier Flags"], setup.OPTION)
        self.assertEqual(p["HotKey Characters Ignoring Modifiers"], "/")
        self.assertEqual(p["HotKey Characters"], "÷")

    def test_another_key_can_be_chosen(self):
        p = self.profile(hotkey="option-space")
        self.assertEqual(p["HotKey Key Code"], setup.HOTKEYS["option-space"]["code"])
        self.assertNotEqual(p["HotKey Key Code"], 44)

    def test_an_unknown_key_is_refused_not_silently_dropped(self):
        with self.assertRaises(KeyError):
            self.profile(hotkey="option-banana")

    def test_it_does_not_reappear_just_because_you_switched_to_iterm(self):
        # Reopens On Activation means the list pops up whenever iTerm2 comes to
        # the front for any reason - a window you did not ask for, over the work
        # you did ask for. The key is how you open it.
        self.assertFalse(self.profile()["HotKey Window Reopens On Activation"])

    def test_it_stays_open_when_you_go_to_a_session(self):
        # It is a control panel, not a popup: pressing Enter takes you to a
        # session's window, and the list has to still be there when you come
        # back. AutoHides made it vanish the moment anything else took focus -
        # including the window ccwho had just put you in.
        self.assertFalse(self.profile()["HotKey Window AutoHides"],
                         "it goes away when you ask it to, not by itself")

    def test_it_does_not_sit_on_top_of_the_session_it_just_opened(self):
        # Floats pins the panel above every other window. With AutoHides off it
        # would then cover the session ccwho had just put you in: reported as
        # "it raises for a flash and then another window pops on top".
        self.assertFalse(self.profile()["HotKey Window Floats"])

    def test_it_is_on_whichever_space_you_are_on(self):
        self.assertEqual(self.profile()["Space"], -1,
                         "all spaces, or it is useless on space 2")

    def test_what_the_key_used_to_type_is_stated(self):
        self.assertEqual(setup.HOTKEYS["option-slash"]["instead_of"], "÷")
        self.assertEqual(setup.HOTKEYS["option-slash"]["label"], "⌥/")


class TestTheHotkeyIsProvedNotAssumed(unittest.TestCase):
    """Writing the profile proves nothing: another app can own the key, or
    iTerm2 may not reload. So the profile runs a one-time command with a nonce
    in it, and setup waits for that nonce to appear on disk."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_each_run_asks_for_a_different_nonce(self):
        self.assertNotEqual(setup.new_nonce(), setup.new_nonce())

    def test_the_proof_command_carries_the_nonce_and_the_real_ccwho(self):
        cmd = setup.proof_command("/Users/sam/.local/bin/ccwho", "abc123")
        self.assertIn("/Users/sam/.local/bin/ccwho", cmd)
        self.assertIn("abc123", cmd)

    def test_a_path_with_a_space_survives_the_command(self):
        cmd = setup.proof_command("/Users/sam/my tools/ccwho", "abc")
        self.assertIn("'/Users/sam/my tools/ccwho'", cmd)

    def test_waiting_ends_the_moment_the_nonce_lands(self):
        setup.write_proof(self.dir, "n1")
        self.assertTrue(setup.wait_for_proof(self.dir, "n1", deadline=0.0))

    def test_a_nonce_that_never_lands_gives_up_rather_than_hanging(self):
        self.assertFalse(setup.wait_for_proof(self.dir, "n2", deadline=0.05))

    def test_a_different_nonce_does_not_count(self):
        setup.write_proof(self.dir, "other")
        self.assertFalse(setup.wait_for_proof(self.dir, "n3", deadline=0.05))


class TestSetupSaysWhatItWillDoAndIsSafeToRerun(unittest.TestCase):
    """Every step reports done / to-do from the same facts doctor reads, so a
    second run is a no-op that says so rather than a second install."""

    def plan(self, **over):
        f = facts(uv="/opt/homebrew/bin/uv", hotkey_installed=True)
        f.update(over)
        return {s["name"]: s for s in setup.setup_plan(f)}

    def test_on_a_finished_machine_every_step_is_already_done(self):    # control
        self.assertEqual([s for s in self.plan().values() if s["todo"]], [])

    def test_no_claude_stops_everything_after_it(self):
        steps = setup.setup_plan(facts(claude="", uv="/x/uv"))
        self.assertTrue(steps[0]["blocking"])
        self.assertEqual(steps[0]["name"], "claude")

    def test_no_uv_is_a_step_with_the_line_to_run(self):
        step = self.plan(uv="")["uv"]
        self.assertTrue(step["todo"])
        self.assertIn("brew install uv", step["fix"])

    def test_a_missing_handler_is_to_do(self):
        self.assertTrue(self.plan(handler_registered=False)["ccwho:// handler"]["todo"])

    def test_a_missing_autosave_job_is_to_do(self):
        self.assertTrue(self.plan(launchd_loaded=False)["autosave job"]["todo"])

    def test_a_missing_hotkey_is_to_do(self):
        self.assertTrue(self.plan(hotkey_installed=False)["hotkey"]["todo"])

    def test_the_status_hook_is_reported_but_never_written(self):
        step = self.plan(cc_status_hook=False)["iTerm2 status hook"]
        self.assertFalse(step["todo"], "ccwho does not write another tool's settings")
        self.assertIn("ccwho will not write", step["fix"])


class TestAStepThatIsNotOursIsNotReportedAsFine(unittest.TestCase):
    """The iTerm2 status hook is another tool's file: setup reports it and never
    writes it. That is not the same as "ok", and printing `ok  ` beside the word
    "missing" is the exact false-all-clear doctor exists to prevent."""

    def steps(self, **over):
        f = facts(uv="/x/uv", hotkey_installed=True)
        f.update(over)
        return {s["name"]: s for s in setup.setup_plan(f)}

    def test_something_missing_we_will_not_fix_reads_as_a_note(self):
        step = self.steps(cc_status_hook=False)["iTerm2 status hook"]
        self.assertEqual(setup.step_mark(step), "note")

    def test_the_same_step_is_ok_when_it_is_actually_there(self):        # control
        step = self.steps(cc_status_hook=True)["iTerm2 status hook"]
        self.assertEqual(setup.step_mark(step), "ok  ")

    def test_on_a_finished_machine_every_step_reads_ok(self):          # control
        marks = {n: setup.step_mark(s) for n, s in self.steps().items()}
        self.assertEqual(set(marks.values()), {"ok  "}, marks)

    def test_a_step_that_is_done_carries_no_leftover_fix(self):
        self.assertEqual(self.steps()["autosave job"]["fix"], "",
                         "a fix printed beside a finished step reads as a to-do")

    def test_something_setup_will_install_reads_as_to_do(self):
        self.assertEqual(setup.step_mark(self.steps(launchd_loaded=False)
                                         ["autosave job"]), "todo")


class TestARenderedJobIsValidBeforeAnythingIsTouched(unittest.TestCase):
    """A path with an & in it - /Users/sam/R&D/ccwho - is not valid XML. The old
    order wrote the file and booted the working job out before launchd rejected
    it, so the check happens on the text, first."""

    def render(self, **over):
        args = dict(home="/Users/sam", ccwho="/Users/sam/R&D/cc who/ccwho.py",
                    claude="/Users/sam/.local/bin/claude", python="/usr/bin/python3")
        args.update(over)
        path = os.path.join(os.path.dirname(os.path.abspath(setup.__file__)),
                            setup.PLIST_TEMPLATE_NAME)
        with open(path) as fh:
            return setup.render_plist(fh.read(), **args)

    def test_an_ampersand_in_a_path_still_parses_as_a_plist(self):
        import plistlib
        doc = plistlib.loads(self.render().encode())
        self.assertIn("/Users/sam/R&D/cc who/ccwho.py", doc["ProgramArguments"])

    def test_a_less_than_in_a_path_survives_unchanged(self):
        import plistlib
        doc = plistlib.loads(self.render(ccwho="/Users/sam/a<b/ccwho.py").encode())
        self.assertIn("/Users/sam/a<b/ccwho.py", doc["ProgramArguments"])

    def test_text_that_is_not_a_plist_is_a_complaint(self):
        problems = setup.plist_problems("<plist><dict><key>oops",
                                        home="/Users/sam",
                                        claude="/Users/sam/.local/bin/claude")
        self.assertTrue(any("plist" in p.lower() for p in problems), problems)

    def test_a_good_one_has_no_complaints(self):                      # control
        self.assertEqual(setup.plist_problems(
            self.render(), home="/Users/sam",
            claude="/Users/sam/.local/bin/claude"), [])


class TestALoadedJobIsNotAWorkingJob(unittest.TestCase):
    """The failure this whole repo exists for: a job that is loaded, runs, finds
    nothing and exits 0. `launchctl list` says loaded either way, so setup
    compares what is INSTALLED with what it would write today."""

    def rendered(self, ccwho="/Users/sam/src/ccwho/ccwho.py"):
        path = os.path.join(os.path.dirname(os.path.abspath(setup.__file__)),
                            setup.PLIST_TEMPLATE_NAME)
        with open(path) as fh:
            return setup.render_plist(fh.read(), home="/Users/sam", ccwho=ccwho,
                                      claude="/Users/sam/.local/bin/claude",
                                      python="/usr/bin/python3")

    def test_the_same_job_matches(self):                              # control
        self.assertTrue(setup.plist_matches(self.rendered(), self.rendered()))

    def test_a_job_pointing_at_a_moved_repo_does_not_match(self):
        self.assertFalse(setup.plist_matches(
            self.rendered(ccwho="/Users/sam/old/ccwho/ccwho.py"), self.rendered()))

    def test_a_job_whose_path_cannot_find_claude_does_not_match(self):
        stale = self.rendered().replace("/Users/sam/.local/bin:", "")
        self.assertFalse(setup.plist_matches(stale, self.rendered()))

    def test_a_missing_or_unreadable_job_never_matches(self):
        self.assertFalse(setup.plist_matches("", self.rendered()))
        self.assertFalse(setup.plist_matches("not a plist at all", self.rendered()))

    def test_a_comment_change_is_not_a_difference(self):
        self.assertTrue(setup.plist_matches(
            self.rendered().replace("<!--", "<!-- x "), self.rendered()),
            "only what the job DOES counts")


class TestTheAppletIsAskedWhereItPoints(unittest.TestCase):
    """An applet can claim ccwho:// while calling a copy that was deleted. That
    reads as "registered" and does nothing when clicked."""

    def test_the_path_is_read_out_of_the_script(self):
        script = ('on open location this_URL\n  try\n'
                  '    do shell script quoted form of "/Users/sam/.local/bin/ccwho"'
                  ' & " url " & quoted form of this_URL\n  end try\nend open location')
        self.assertEqual(setup.handler_target(script), "/Users/sam/.local/bin/ccwho")

    def test_an_escaped_path_reads_back_as_the_real_path(self):
        # The applet holds an AppleScript literal: a path with a quote or a
        # backslash is escaped in it. Read back unescaped, or setup compares it
        # with the real path, sees a difference, and rebuilds the applet forever.
        real = '/Users/sam/we"ird\\path/ccwho'        # the directory as it is
        escaped = real.replace("\\", "\\\\").replace('"', '\\"')
        self.assertEqual(
            setup.handler_target(f'do shell script quoted form of "{escaped}"'),
            real)

    def test_a_script_we_cannot_read_gives_nothing_not_a_wrong_answer(self):
        self.assertEqual(setup.handler_target(""), "")
        self.assertEqual(setup.handler_target("display dialog \"hi\""), "")


class TestThePlanKnowsStaleFromMissing(unittest.TestCase):
    def plan(self, **over):
        f = facts(uv="/x/uv", hotkey_installed=True,
                  handler_current=True, autosave_current=True)
        f.update(over)
        return {s["name"]: s for s in setup.setup_plan(f)}

    def test_a_current_machine_has_nothing_to_do(self):               # control
        self.assertEqual([s for s in self.plan().values() if s["todo"]], [])

    def test_a_job_that_is_loaded_but_stale_is_to_do(self):
        step = self.plan(autosave_current=False)["autosave job"]
        self.assertTrue(step["todo"])
        self.assertIn("loaded", step["detail"].lower())

    def test_a_stale_job_is_not_accused_of_being_broken(self):
        # It may be an older job that still works, written by an older ccwho.
        # Saying it "runs a ccwho that is not here" is a claim we cannot make
        # from a text comparison.
        step = self.plan(autosave_current=False)["autosave job"]
        self.assertNotIn("not here", step["detail"])
        self.assertIn("would write", step["detail"])

    def test_an_applet_pointing_at_another_copy_is_to_do(self):
        step = self.plan(handler_current=False)["ccwho:// handler"]
        self.assertTrue(step["todo"])
        self.assertIn("points", step["detail"].lower())


class TestTheHandlerInstallerKeepsWhatWorks(unittest.TestCase):
    """install-handler.sh builds the ccwho:// applet. `ccwho setup` now runs it
    unattended, so the two ways it could leave a machine with no handler at all
    are covered here. osacompile, PlistBuddy and lsregister are stubbed: this is
    about the script's ORDER, not about AppleScript."""

    SCRIPT = os.path.join(os.path.dirname(os.path.abspath(setup.__file__)),
                          "install-handler.sh")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.app = os.path.join(self.tmp, "ccwho-jump.app")

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/bash\n" + body + "\n")
        os.chmod(path, 0o755)
        return path

    def run_install(self, ccwho="/Users/sam/.local/bin/ccwho", compiles=True):
        # A fake osacompile that keeps the SOURCE, so the test can read what
        # would have been compiled; it makes the bundle PlistBuddy needs.
        keep = os.path.join(self.tmp, "source.applescript")
        self.stub("osacompile", f"""
            [ "{int(compiles)}" = "1" ] || exit 7
            out="$2"; src="$3"
            mkdir -p "$out/Contents"
            cp "$src" "{keep}"
            printf '%s' '<?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
             "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0"><dict/></plist>' > "$out/Contents/Info.plist"
        """)
        self.stub("plistbuddy", 'exit 0')
        self.stub("lsregister", 'exit 0')
        env = dict(os.environ, CCWHO_BIN=ccwho,
                   OSACOMPILE=os.path.join(self.bin, "osacompile"),
                   PLISTBUDDY=os.path.join(self.bin, "plistbuddy"),
                   LSREGISTER=os.path.join(self.bin, "lsregister"))
        done = subprocess.run(["bash", self.SCRIPT, self.app], env=env,
                              capture_output=True, text=True)
        source = ""
        if os.path.exists(keep):
            with open(keep) as fh:
                source = fh.read()
        return done, source

    def test_a_build_that_fails_leaves_the_working_applet_alone(self):
        os.makedirs(os.path.join(self.app, "Contents"))
        marker = os.path.join(self.app, "Contents", "marker")
        with open(marker, "w") as fh:
            fh.write("the one that works")
        done, _ = self.run_install(compiles=False)
        self.assertNotEqual(done.returncode, 0)
        self.assertTrue(os.path.exists(marker),
                        "a failed build must not delete a handler that works")

    def test_a_build_that_works_replaces_it(self):                    # control
        os.makedirs(os.path.join(self.app, "Contents"))
        with open(os.path.join(self.app, "Contents", "marker"), "w") as fh:
            fh.write("the old one")
        done, _ = self.run_install()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.app, "Contents",
                                                     "marker")))
        self.assertFalse(os.path.exists(self.app + ".old"), "and nothing is left over")

    def test_a_path_with_a_quote_in_it_stays_that_path(self):
        odd = '/Users/sam/we"ird\\path/ccwho'
        done, source = self.run_install(ccwho=odd)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(setup.handler_target(source), odd,
                         "an AppleScript literal, escaped and read back whole")


class TestGatherFindsClaudeTheSameWayTheEngineDoes(unittest.TestCase):
    """doctor and setup both read the `claude` fact. A window opened by the
    hotkey, and a launchd job, both run with PATH=/usr/bin:/bin:/usr/sbin:/sbin,
    where claude is not - and then doctor says "not on PATH: no session list at
    all" about a machine where ccwho works perfectly."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for name, answer in (("iterm_scriptable", True),
                             ("handler_registered", True),
                             ("launchd_loaded", True),
                             ("cc_status_hook", True)):
            self.addCleanup(setattr, setup, name, getattr(setup, name))
            setattr(setup, name, lambda *a, **k: answer)
        self.addCleanup(setattr, setup.shutil, "which", setup.shutil.which)
        setup.shutil.which = lambda n: ""          # nothing on PATH at all

    def test_claude_is_found_where_it_installs_itself(self):
        real = setup.engine.find_tool
        self.addCleanup(setattr, setup.engine, "find_tool", real)
        setup.engine.find_tool = lambda n: "/Users/sam/.local/bin/" + n
        self.assertEqual(setup.gather(ccwho_dir=self.tmp)["claude"],
                         "/Users/sam/.local/bin/claude")

    def test_a_machine_without_claude_still_reports_none(self):       # control
        real = setup.engine.find_tool
        self.addCleanup(setattr, setup.engine, "find_tool", real)
        setup.engine.find_tool = lambda n: ""
        self.assertEqual(setup.gather(ccwho_dir=self.tmp)["claude"], "")


class TestAnInstalledProfileIsNotNecessarilyTheRightProfile(unittest.TestCase):
    """The same lesson as the launchd job, one layer up: "there is a profile
    with our Guid and a hotkey on it" is not "the profile we would write now".
    A settings change would otherwise never reach a machine that already ran
    setup once."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        self.path = setup.profile_path(self.home)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

    def write(self, doc):
        with open(self.path, "w") as fh:
            json.dump(doc, fh)

    def current(self, command="/x/ccwho hotkey"):
        return setup.hotkey_profile(command)

    def test_the_profile_we_would_write_matches_itself(self):          # control
        self.write(self.current())
        self.assertTrue(setup.hotkey_current(self.home, self.current()))

    def test_one_changed_setting_is_not_current(self):
        stale = self.current()
        stale["Profiles"][0]["HotKey Window Reopens On Activation"] = True
        self.write(stale)
        self.assertFalse(setup.hotkey_current(self.home, self.current()))

    def test_a_profile_running_another_command_is_not_current(self):
        self.write(self.current(command="/somewhere/else/ccwho hotkey"))
        self.assertFalse(setup.hotkey_current(self.home, self.current()))

    def test_no_profile_at_all_is_not_current(self):
        self.assertFalse(setup.hotkey_current(self.home, self.current()))

    def test_other_profiles_beside_ours_do_not_matter(self):
        doc = self.current()
        doc["Profiles"].insert(0, {"Guid": "someone-elses", "Name": "theirs"})
        self.write(doc)
        self.assertTrue(setup.hotkey_current(self.home, self.current()),
                        "we compare ours, not the file")


class TestTheHotkeyOnlyReachesYouWithPermission(unittest.TestCase):
    """A global hotkey - one that fires while another app is in front - needs
    Accessibility at the moment the app registers the key grab. Found the hard
    way: iTerm2 was granted it three days after it started, so the key worked
    only while iTerm2 was already frontmost and looked broken from Chrome.

    Two different faults with two different answers, and setup has to tell them
    apart: not granted (ask for it, the way any app asks) and granted too late
    (restart iTerm2 once - which ccwho says and never does, because it would
    end every session running in it).
    """

    def facts(self, **over):
        f = {"accessibility": True,
             "iterm_granted_at": 1000.0,
             "iterm_started_at": 2000.0}      # granted BEFORE it started
        f.update(over)
        return f

    def test_granted_before_it_started_is_fine(self):                 # control
        check = setup.hotkey_reach(self.facts())
        self.assertTrue(check["ok"])
        self.assertFalse(check["fix"])

    def test_granted_after_it_started_needs_a_restart(self):
        check = setup.hotkey_reach(self.facts(iterm_granted_at=3000.0))
        self.assertFalse(check["ok"])
        self.assertEqual(check["do"], "restart")
        self.assertIn("in front", check["detail"].lower(),
                      "say what the symptom is, or nobody connects the two")

    def test_not_granted_is_something_to_ask_for(self):
        check = setup.hotkey_reach(self.facts(accessibility=False))
        self.assertFalse(check["ok"])
        self.assertEqual(check["do"], "ask")

    def test_asking_comes_before_restarting(self):
        # no point restarting into a permission that is still not there
        check = setup.hotkey_reach(self.facts(accessibility=False,
                                              iterm_granted_at=3000.0))
        self.assertEqual(check["do"], "ask")

    def test_a_machine_we_cannot_read_is_not_accused_of_anything(self):
        # the grant TIME comes from a database that needs Full Disk Access;
        # plenty of machines will not have it, and crying wolf is how a real
        # fault gets ignored later
        check = setup.hotkey_reach(self.facts(iterm_granted_at=None))
        self.assertTrue(check["ok"])

    def test_iterm_not_running_is_not_a_fault_either(self):
        check = setup.hotkey_reach(self.facts(iterm_started_at=None))
        self.assertTrue(check["ok"])


class TestTheGatherersForThatSurviveAnyMachine(unittest.TestCase):
    """Asking the machine these questions must never raise, and must never
    answer confidently when it could not look."""

    def test_asking_whether_we_have_accessibility_never_raises(self):
        self.assertIn(setup.accessibility_ok(), (True, False, None))

    def test_a_missing_database_reads_as_unknown(self):
        self.assertIsNone(setup.accessibility_granted_at(
            "com.googlecode.iterm2", db="/nowhere/TCC.db"))

    def test_iterm_start_time_is_none_when_it_is_not_running(self):
        self.assertIsNone(setup.iterm_started_at(pid=999999))

    def test_iterm_start_time_is_a_number_when_it_is(self):           # control
        pid = subprocess.run(["pgrep", "-x", "iTerm2"], capture_output=True,
                             text=True).stdout.split()
        if not pid:
            self.skipTest("iTerm2 is not running on this machine")
        self.assertIsInstance(setup.iterm_started_at(pid=int(pid[0])), float)


class TestBothCommandsKnowAboutIt(unittest.TestCase):
    def facts(self, **over):
        f = facts(uv="/x/uv", hotkey_installed=True, handler_current=True,
                  autosave_current=True, accessibility=True,
                  iterm_granted_at=1000.0, iterm_started_at=2000.0)
        f.update(over)
        return f

    def test_doctor_reports_a_hotkey_that_cannot_reach_you(self):
        checks = {c["name"]: c for c in setup.doctor_checks(
            self.facts(iterm_granted_at=3000.0))}
        self.assertIn("hotkey reach", checks)
        self.assertFalse(checks["hotkey reach"]["ok"])
        self.assertIn("restart", checks["hotkey reach"]["fix"].lower())

    def test_doctor_is_quiet_when_it_can(self):                       # control
        checks = {c["name"]: c for c in setup.doctor_checks(self.facts())}
        self.assertTrue(checks["hotkey reach"]["ok"])

    def test_setup_has_a_step_for_asking(self):
        step = {s["name"]: s for s in setup.setup_plan(
            self.facts(accessibility=False))}["hotkey reach"]
        self.assertTrue(step["todo"])

    def test_setup_leaves_it_alone_when_it_is_right(self):            # control
        step = {s["name"]: s for s in setup.setup_plan(self.facts())}["hotkey reach"]
        self.assertFalse(step["todo"])

    def test_a_restart_is_reported_but_never_done(self):
        step = {s["name"]: s for s in setup.setup_plan(
            self.facts(iterm_granted_at=3000.0))}["hotkey reach"]
        self.assertFalse(step["todo"], "ccwho does not quit iTerm2 for you")
        self.assertIn("restart", step["fix"].lower())
