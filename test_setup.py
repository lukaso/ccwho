"""Tests for ccwho_setup: what is installed, what drifted, what to do about it.

The checks are pure over injected facts. Gathering the facts touches the world
(PATH, launchd, LaunchServices, iTerm2); deciding what they MEAN does not, and
that is the half that has to be right.

python3 -m unittest test_setup -v
"""
import os
import shutil
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
