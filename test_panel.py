"""The hotkey panel taking back the focus iTerm2 hands it and then loses.

The sequences below are the ones logged on a real machine (macOS 26.6, iTerm2
3.7.2), with the panel as window 24246 and a session window as 21541:

    Deactivated                    you clicked Chrome
    FocusedWindow 24246            the key: the panel comes up
    FocusedWindow 21541  +0.22s    macOS finishes activating iTerm2 and
    Activated            +0.001s   hands focus back to the window it had
"""
import unittest

import ccwho_panel as panel

PANEL, SESSION, OTHER = 24246, 21541, 21542


def watch(active=True):
    return panel.StealWatch(PANEL, active=active)


class TheSteal(unittest.TestCase):
    def test_the_logged_sequence_raises_the_panel(self):
        w = watch()
        w.deactivated()
        w.focused(PANEL, now=10.0, click_age=1.8)
        w.focused(SESSION, now=10.22, click_age=2.0)
        self.assertTrue(w.activated())

    def test_starting_while_another_app_is_in_front(self):
        # the panel can be started while iTerm2 is not the active app
        w = watch(active=False)
        w.focused(PANEL, now=10.0, click_age=5.0)
        w.focused(SESSION, now=10.28, click_age=5.3)
        self.assertTrue(w.activated())

    def test_it_raises_once(self):
        w = watch()
        w.deactivated()
        w.focused(PANEL, now=10.0, click_age=5.0)
        w.focused(SESSION, now=10.2, click_age=5.2)
        self.assertTrue(w.activated())
        self.assertFalse(w.activated())

    def test_nothing_happens_before_the_activation(self):
        # raising before macOS has finished loses to macOS; the raise is due
        # only at Activated, and focused() does not say it
        w = watch()
        w.deactivated()
        self.assertIsNone(w.focused(PANEL, now=10.0, click_age=5.0))
        self.assertIsNone(w.focused(SESSION, now=10.2, click_age=5.2))


class NotTheSteal(unittest.TestCase):
    def test_clicking_the_panel_from_another_app(self):
        w = watch()
        w.deactivated()
        w.focused(PANEL, now=10.0, click_age=0.01)
        self.assertFalse(w.activated())

    def test_clicking_a_session_window_from_another_app(self):
        w = watch()
        w.deactivated()
        w.focused(SESSION, now=10.0, click_age=0.01)
        self.assertFalse(w.activated())

    def test_going_to_a_session_from_the_panel(self):
        # Enter on a row: iTerm2 is already active, so this is a choice
        w = watch()
        w.focused(PANEL, now=10.0, click_age=5.0)
        w.focused(SESSION, now=10.1, click_age=5.1)
        self.assertFalse(w.activated())

    def test_a_click_between_the_key_and_the_steal_is_yours(self):
        w = watch()
        w.deactivated()
        w.focused(PANEL, now=10.0, click_age=5.0)
        w.focused(SESSION, now=10.2, click_age=0.05)   # clicked after the key
        self.assertFalse(w.activated())

    def test_leaving_for_another_app(self):
        # the panel in front, then Chrome: iTerm2 goes inactive, nothing else
        w = watch()
        w.focused(PANEL, now=10.0, click_age=5.0)
        w.deactivated()
        self.assertFalse(w.activated())

    def test_after_an_activation_the_panel_is_not_armed(self):
        w = watch()
        w.deactivated()
        w.focused(PANEL, now=10.0, click_age=5.0)
        self.assertFalse(w.activated())
        w.focused(SESSION, now=12.0, click_age=5.0)
        self.assertFalse(w.activated())

    def test_focus_going_nowhere(self):
        w = watch()
        w.deactivated()
        w.focused(PANEL, now=10.0, click_age=5.0)
        w.focused(None, now=10.2, click_age=5.2)
        self.assertFalse(w.activated())


class WhoIsThePanel(unittest.TestCase):
    def test_the_hotkey_profile(self):
        self.assertTrue(panel.is_panel({"ITERM_PROFILE": "ccwho",
                                        "ITERM_SESSION_ID": "w0t0p0:ABC"}))

    def test_ccwho_in_an_ordinary_window(self):
        self.assertFalse(panel.is_panel({"ITERM_PROFILE": "Default",
                                         "ITERM_SESSION_ID": "w0t0p0:ABC"}))

    def test_outside_iterm2(self):
        self.assertFalse(panel.is_panel({}))

    def test_session_uuid(self):
        self.assertEqual(panel.session_uuid(
            {"ITERM_SESSION_ID": "w0t0p0:3E57FBE9-5311-4B3A-9EAE-038FC76E7855"}),
            "3E57FBE9-5311-4B3A-9EAE-038FC76E7855")
        self.assertIsNone(panel.session_uuid({}))


class FakeAx:
    """Accessibility as it behaved in the log: by the time a notification is
    handled, the window that has focus NOW can already be the session again."""

    def __init__(self, focused_now):
        self.focused_now = focused_now
        self.raised = []

    def focused_window_id(self, app):
        return self.focused_now      # what asking again would say

    def window_id(self, element):
        return element            # the fake's elements ARE their ids

    def click_age(self):
        return 5.0

    def raise_window(self, app, window_id):
        self.raised.append(window_id)
        return True


class TheNotifications(unittest.TestCase):
    def test_the_window_is_the_one_the_notification_names(self):
        # Logged: two FocusedWindowChanged in the same millisecond, and asking
        # afterwards said the session both times. The first one WAS the panel.
        ax, w = FakeAx(focused_now=SESSION), watch()
        panel.handle(w, ax, "app", "AXApplicationDeactivated", None, now=10.0)
        panel.handle(w, ax, "app", "AXFocusedWindowChanged", PANEL, now=10.0)
        panel.handle(w, ax, "app", "AXFocusedWindowChanged", SESSION, now=10.01)
        self.assertTrue(panel.handle(w, ax, "app", "AXApplicationActivated",
                                     None, now=10.02))
        self.assertEqual(ax.raised, [PANEL])

    def test_each_raise_is_written_down(self):
        # The raise hides the bug: without a record nobody knows it came back
        noted = []
        ax, w = FakeAx(focused_now=SESSION), watch()
        panel.handle(w, ax, "app", "AXApplicationDeactivated", None, now=10.0,
                     note=noted.append)
        panel.handle(w, ax, "app", "AXFocusedWindowChanged", PANEL, now=10.0,
                     note=noted.append)
        panel.handle(w, ax, "app", "AXFocusedWindowChanged", SESSION, now=10.2,
                     note=noted.append)
        panel.handle(w, ax, "app", "AXApplicationActivated", None, now=10.21,
                     note=noted.append)
        self.assertEqual(len(noted), 1)
        self.assertIn(str(SESSION), noted[0], "which window took the focus")

    def test_nothing_is_written_when_nothing_was_stolen(self):
        noted = []
        ax, w = FakeAx(focused_now=PANEL), watch()
        for name, element in (("AXApplicationDeactivated", None),
                              ("AXFocusedWindowChanged", PANEL),
                              ("AXApplicationActivated", None)):
            panel.handle(w, ax, "app", name, element, now=10.0, note=noted.append)
        self.assertEqual(noted, [])

    def test_an_activation_with_nothing_stolen_raises_nothing(self):
        ax, w = FakeAx(focused_now=PANEL), watch()
        panel.handle(w, ax, "app", "AXApplicationDeactivated", None, now=10.0)
        panel.handle(w, ax, "app", "AXFocusedWindowChanged", PANEL, now=10.0)
        self.assertFalse(panel.handle(w, ax, "app", "AXApplicationActivated",
                                      None, now=10.1))
        self.assertEqual(ax.raised, [])


class TheRecord(unittest.TestCase):
    def test_lines_are_appended_with_the_time(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "panel.log")
            panel.write_note(path, "took the focus back from 21541",
                             stamp="2026-09-24 16:50:00")
            panel.write_note(path, "took the focus back from 24786",
                             stamp="2026-09-24 16:51:00")
            with open(path) as fh:
                self.assertEqual(fh.read().splitlines(), [
                    "2026-09-24 16:50:00 took the focus back from 21541",
                    "2026-09-24 16:51:00 took the focus back from 24786"])

    def test_a_record_that_cannot_be_written_is_not_an_error(self):
        panel.write_note("/dev/null/cannot/exist.log", "x", stamp="t")


class Stop(BaseException):      # not Exception: the loop must survive those
    pass


def run_supervise(found, observe=None, rounds=10):
    """Drive the loop with a scripted lookup; it stops when the script ends."""
    found = list(found)
    watched, waits = [], []

    def resolve():
        if not found:
            raise Stop
        return found.pop(0)

    def default_observe(pid, window_id):
        watched.append((pid, window_id))

    try:
        panel.supervise(resolve, observe or default_observe, waits.append)
    except Stop:
        pass
    return watched, waits


class ItermComesAndGoes(unittest.TestCase):
    # Logged: iTerm2 crashed and restarted, the panel's session survived into
    # a new window, and the watcher went on watching the dead process - the
    # steal came back with nothing to undo it.

    def test_a_new_iterm2_is_watched_after_the_old_one_is_gone(self):
        watched, _ = run_supervise([(27269, 24246), (1178, 25033)])
        self.assertEqual(watched, [(27269, 24246), (1178, 25033)])

    def test_a_failed_lookup_is_tried_again_not_the_end(self):
        # iTerm2 not answering yet, or the window not placed yet
        watched, waits = run_supervise([None, None, (1178, 25033)])
        self.assertEqual(watched, [(1178, 25033)])
        self.assertEqual(len(waits), 2)

    def test_a_failure_while_watching_is_not_the_end(self):
        calls = []

        def observe(pid, window_id):
            calls.append(pid)
            if len(calls) == 1:
                raise OSError("observer could not be made")

        _, waits = run_supervise([(1, 10), (2, 20)], observe=observe)
        self.assertEqual(calls, [1, 2])
        self.assertEqual(len(waits), 1)


class WatchingOneIterm(unittest.TestCase):
    def test_it_watches_while_the_process_lives(self):
        answers, slices = [True, True, False], []
        panel.run_while(lambda: answers.pop(0), slices.append)
        self.assertEqual(len(slices), 2)

    def test_a_process_that_exists(self):
        import os
        self.assertTrue(panel.pid_alive(os.getpid()))

    def test_a_process_that_is_gone(self):
        import subprocess
        done = subprocess.Popen(["true"])
        done.wait()
        self.assertFalse(panel.pid_alive(done.pid))


if __name__ == "__main__":
    unittest.main()
