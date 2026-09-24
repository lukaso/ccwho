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

    def test_an_activation_with_nothing_stolen_raises_nothing(self):
        ax, w = FakeAx(focused_now=PANEL), watch()
        panel.handle(w, ax, "app", "AXApplicationDeactivated", None, now=10.0)
        panel.handle(w, ax, "app", "AXFocusedWindowChanged", PANEL, now=10.0)
        self.assertFalse(panel.handle(w, ax, "app", "AXApplicationActivated",
                                      None, now=10.1))
        self.assertEqual(ax.raised, [])


if __name__ == "__main__":
    unittest.main()
