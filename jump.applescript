on run argv
  set target to item 1 of argv
  tell application "iTerm2"
    -- Find the window and keep its ID. `repeat with w in windows` hands back an
    -- INDEX-based reference - "window 3 of application iTerm2" - and the first
    -- `select` reorders the windows, which silently re-points that reference at
    -- a different window. The jump then raised the window NEXT to the one asked
    -- for: reported as "clicked e0e2 and got 560a", whose windows were index 3
    -- and index 2. An id is an id whatever moves.
    set winId to missing value
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          try
            if (tty of s) is target then set winId to id of w
          end try
        end repeat
      end repeat
    end repeat
    if winId is missing value then return "not found: " & target

    -- Outside in, ending on the PANE. A session is usually a pane inside a tab
    -- inside a window - measured on a real fleet: eleven panes in one tab - and
    -- selecting the tab alone leaves you looking at whichever pane was last
    -- active in it.
    tell window id winId
      repeat with t in tabs
        repeat with s in sessions of t
          try
            if (tty of s) is target then
              select t
              select s
            end if
          end try
        end repeat
      end repeat
    end tell

    -- Then the app comes forward. Activating raises EVERY window it has, so the
    -- chosen one is put at the front afterwards, by id.
    select window id winId
    activate
    try
      if (index of window id winId) is not 1 then
        set index of window id winId to 1
      end if
    end try

    -- Say where it actually landed. A jump that quietly goes somewhere else is
    -- worse than one that admits it: this took three reports to find.
    try
      set landed to tty of current session of current tab of current window
    on error
      set landed to "unknown"
    end try
    if landed is target then
      return "focused " & target
    end if
    return "asked for " & target & " but landed on " & landed
  end tell
end run
