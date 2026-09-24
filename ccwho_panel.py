"""The hotkey panel keeping the focus iTerm2 gives it.

Pressed from another app, with the panel open behind a session window, the key
shows the panel for a fifth of a second and then the session window is back on
top. iTerm2 does the right thing in the wrong order: it asks to be activated,
then brings the panel forward - and on current macOS activation finishes AFTER
that, handing focus back to the window iTerm2 had before. Logged (macOS 26.6,
iTerm2 3.7.2):

    Deactivated                    you clicked Chrome
    FocusedWindow  the panel       the key: the panel comes up
    FocusedWindow  a session       +0.22s  activation finishes, focus goes back
    Activated                      +0.001s

The terminal says nothing about it: iTerm2 sends the panel a focus-out and never
a focus-in, so this watches iTerm2 through Accessibility instead - which iTerm2
already has, and which a process running inside iTerm2 is allowed to use.

Stdlib only (ctypes), like the engine: no dependency for one window.
"""
import ctypes
import ctypes.util
import os
import subprocess
import threading
import time

import ccwho_setup as setup


def is_panel(env):
    """Only the hotkey window watches. `ccwho` in an ordinary window is a list
    you opened yourself, and nothing about the key applies to it."""
    return env.get("ITERM_PROFILE") == setup.PROFILE_NAME


def session_uuid(env):
    """iTerm2's id for this session: ITERM_SESSION_ID is `w0t0p0:<uuid>`."""
    sid = env.get("ITERM_SESSION_ID") or ""
    return sid.partition(":")[2] or None


class StealWatch:
    """The rule, with nothing in it that touches the machine.

    The steal is the one sequence in which the panel gets focus while iTerm2 is
    NOT active, and loses it to another iTerm2 window before iTerm2 reports
    that it is. A click on the panel from another app gets focus the same way
    but keeps it; Enter on a row moves focus while iTerm2 is active. No timer:
    the order of the events is the whole signal.

    The raise is due at Activated, not when the focus goes: before activation
    finishes, macOS takes it away again.
    """

    def __init__(self, panel_id, active=True):
        self.panel_id = panel_id
        self.active = active
        self.gained_at = None      # the panel came up while iTerm2 was inactive
        self.stolen = False
        self.thief = None          # the window that took it

    def deactivated(self):
        self.active = False
        self.gained_at = None
        self.stolen = False

    def focused(self, window_id, now, click_age):
        if self.active:
            return None
        if window_id == self.panel_id:
            self.gained_at = now
        elif self.gained_at is not None:
            # a click after the panel came up is you choosing another window
            clicked_since = click_age < now - self.gained_at
            self.stolen = window_id is not None and not clicked_since
            self.thief = window_id if self.stolen else None
            self.gained_at = None
        return None

    def activated(self):
        """True when the panel should be raised, now."""
        stolen = self.stolen
        self.active = True
        self.gained_at = None
        self.stolen = False
        return stolen


# ------------------------------------------------------------ the machine
# Everything below reaches macOS. It never raises into the list: a panel that
# cannot watch is the panel as it was before, not a panel that crashed.

_AX = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
_UTF8 = 0x08000100
_LEFT_MOUSE_DOWN = 1
_COMBINED_SESSION = 0
_NOTES = ("AXFocusedWindowChanged", "AXApplicationActivated",
          "AXApplicationDeactivated")


def panel_window_id(uuid, timeout=5.0):
    """The window this session is in, by iTerm2's id - the same number macOS
    gives the window, so it can be matched against Accessibility's."""
    script = f'''tell application "iTerm2"
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        if (unique id of s) is "{uuid}" then return id of w
      end repeat
    end repeat
  end repeat
end tell'''
    try:
        done = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (done.stdout or "").strip()
    return int(out) if done.returncode == 0 and out.isdigit() else None


def iterm_pid(timeout=5.0):
    try:
        done = subprocess.run(["pgrep", "-x", "iTerm2"],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    pids = (done.stdout or "").split()
    return int(pids[0]) if pids else None


class _Ax:
    """The dozen calls this needs, bound once."""

    def __init__(self):
        V = ctypes.c_void_p
        ax = ctypes.cdll.LoadLibrary(_AX)
        cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
        cf.CFStringCreateWithCString.restype = V
        cf.CFStringCreateWithCString.argtypes = [V, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFRunLoopGetCurrent.restype = V
        cf.CFRunLoopAddSource.argtypes = [V, V, V]
        cf.CFRunLoopRemoveSource.argtypes = [V, V, V]
        cf.CFRunLoopRunInMode.argtypes = [V, ctypes.c_double, ctypes.c_bool]
        cf.CFArrayGetCount.argtypes = [V]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [V, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = V
        cf.CFBooleanGetValue.argtypes = [V]
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [V]
        ax.AXUIElementCreateApplication.restype = V
        ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
        ax.AXUIElementCopyAttributeValue.argtypes = [V, V, ctypes.POINTER(V)]
        ax.AXUIElementSetAttributeValue.argtypes = [V, V, V]
        ax.AXUIElementPerformAction.argtypes = [V, V]
        ax._AXUIElementGetWindow.argtypes = [V, ctypes.POINTER(ctypes.c_uint32)]
        self.Callback = ctypes.CFUNCTYPE(None, V, V, V, V)
        ax.AXObserverCreate.argtypes = [ctypes.c_int, self.Callback, ctypes.POINTER(V)]
        ax.AXObserverAddNotification.argtypes = [V, V, V, V]
        ax.AXObserverGetRunLoopSource.restype = V
        ax.AXObserverGetRunLoopSource.argtypes = [V]
        ax.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
        ax.CGEventSourceSecondsSinceLastEventType.argtypes = [ctypes.c_int32,
                                                              ctypes.c_uint32]
        self.ax, self.cf, self.V = ax, cf, V
        self.mode = V.in_dll(cf, "kCFRunLoopDefaultMode")
        self.true = V.in_dll(cf, "kCFBooleanTrue")
        self._strings = {}

    def s(self, text):
        if text not in self._strings:        # kept for the life of the process
            self._strings[text] = self.cf.CFStringCreateWithCString(
                None, text.encode(), _UTF8)
        return self._strings[text]

    def attr(self, element, name):
        out = self.V()
        if self.ax.AXUIElementCopyAttributeValue(element, self.s(name),
                                                 ctypes.byref(out)):
            return None
        return out.value

    def window_id(self, window):
        n = ctypes.c_uint32()
        if self.ax._AXUIElementGetWindow(window, ctypes.byref(n)):
            return None
        return n.value

    def frontmost(self, app):
        value = self.attr(app, "AXFrontmost")
        if value is None:
            return True               # unknown: the safe answer arms nothing
        try:
            return bool(self.cf.CFBooleanGetValue(value))
        finally:
            self.cf.CFRelease(value)

    def click_age(self):
        return self.ax.CGEventSourceSecondsSinceLastEventType(_COMBINED_SESSION,
                                                              _LEFT_MOUSE_DOWN)

    def raise_window(self, app, window_id):
        """Bring the panel forward and make it the key window, by its id."""
        windows = self.attr(app, "AXWindows")
        if not windows:
            return False
        try:
            for i in range(self.cf.CFArrayGetCount(windows)):
                w = self.cf.CFArrayGetValueAtIndex(windows, i)
                if self.window_id(w) == window_id:
                    self.ax.AXUIElementPerformAction(w, self.s("AXRaise"))
                    self.ax.AXUIElementSetAttributeValue(w, self.s("AXMain"),
                                                         self.true)
                    return True
            return False
        finally:
            self.cf.CFRelease(windows)


def keep_in_front(env):
    """Start watching, on a thread of its own, if this is the hotkey panel.
    Returns the thread, or None when there is nothing to watch or no way to."""
    if not is_panel(env):
        return None
    thread = threading.Thread(target=_watch, args=(env,),
                              name="ccwho-panel", daemon=True)
    thread.start()
    return thread


def handle(watch, ax, app, name, element, now, note=None):
    """One notification into the rule; True when the panel was raised.

    `note` is told about each raise. The raise hides the steal, so without a
    record nobody would know that iTerm2 is doing it again."""
    if name == "AXApplicationDeactivated":
        watch.deactivated()
    elif name == "AXFocusedWindowChanged":
        # The window the notification NAMES, not the one that has focus by
        # the time it is handled: logged, the panel's fifth of a second can
        # be over by then, and asking again said "the session" both times.
        watch.focused(ax.window_id(element) if element else None, now=now,
                      click_age=ax.click_age())
    elif name == "AXApplicationActivated":
        thief = watch.thief
        if watch.activated():
            if note:
                note(f"took the focus back from window {thief}")
            return ax.raise_window(app, watch.panel_id)
    return False


LOG_PATH = os.path.expanduser("~/.ccwho/panel.log")


def write_note(path, text, stamp=None):
    """One line per raise. A record that cannot be written is not worth the
    panel: it is dropped."""
    stamp = stamp or time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(f"{stamp} {text}\n")
    except OSError:
        pass


# How often the watcher checks that the iTerm2 it watches is still there, and
# how long it waits before looking again when there is nothing to watch yet.
CHECK_EVERY = 5.0
RETRY_EVERY = 5.0


def supervise(resolve, observe, wait):
    """Watch whichever iTerm2 is running, for as long as the panel lives.

    Found once is not found for good: logged, iTerm2 crashed and restarted,
    the panel's session survived into a new window of the new process, and
    the watcher went on watching the dead one. So each time `observe` returns
    - that iTerm2 is gone, or watching it failed - the process and the window
    are looked up again. A lookup that finds nothing is tried again later:
    iTerm2 may not answer yet, or the window may not be placed yet.
    """
    while True:
        found = None
        try:
            found = resolve()
            if found:
                observe(*found)
                continue
        except Exception:
            pass                     # a watcher is never worth the list
        wait(RETRY_EVERY)


def run_while(alive, run_slice):
    """Run the notification loop in slices, checking between them."""
    while alive():
        run_slice(CHECK_EVERY)


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                  # there, just not ours to signal
    return True


def _resolve(env):
    uuid, pid = session_uuid(env), iterm_pid()
    panel = panel_window_id(uuid) if uuid and pid else None
    return (pid, panel) if pid and panel else None


def _observe(ax, pid, panel):
    """Watch one iTerm2 process until it is gone."""
    app = ax.ax.AXUIElementCreateApplication(pid)
    watch = StealWatch(panel, active=ax.frontmost(app))

    # Each notification is told apart by the number it was registered
    # with: the name macOS hands back is its own string, not ours.
    def on_note(_observer, element, _note, ref):
        try:
            name = _NOTES[ref - 1] if ref else None
            handle(watch, ax, app, name, element, time.monotonic(),
                   note=lambda text: write_note(LOG_PATH, text))
        except Exception:
            pass                     # a watcher is never worth the list

    callback = ax.Callback(on_note)
    observer = ax.V()
    if ax.ax.AXObserverCreate(pid, callback, ctypes.byref(observer)):
        raise OSError(f"no Accessibility observer for pid {pid}")
    for i, name in enumerate(_NOTES, start=1):
        ax.ax.AXObserverAddNotification(observer.value, app, ax.s(name), ax.V(i))
    loop = ax.cf.CFRunLoopGetCurrent()
    source = ax.ax.AXObserverGetRunLoopSource(observer.value)
    ax.cf.CFRunLoopAddSource(loop, source, ax.mode)
    try:
        run_while(lambda: pid_alive(pid),
                  lambda seconds: ax.cf.CFRunLoopRunInMode(ax.mode, seconds, False))
    finally:
        ax.cf.CFRunLoopRemoveSource(loop, source, ax.mode)
        ax.cf.CFRelease(observer.value)
        ax.cf.CFRelease(app)


def _watch(env):
    try:
        ax = _Ax()
    except Exception:
        return                       # no Accessibility library: nothing to do
    supervise(lambda: _resolve(env),
              lambda pid, panel: _observe(ax, pid, panel),
              time.sleep)
