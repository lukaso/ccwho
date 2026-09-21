on run argv
  set target to item 1 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          try
            if (tty of s) is target then
              -- Innermost first. A session here is usually a PANE inside a tab
              -- inside a window - measured on a real fleet: sixteen sessions in
              -- three windows, one tab each, up to eight panes in a tab. Only
              -- `select s` picks the pane; selecting the tab alone leaves you
              -- looking at whichever pane was last active in it.
              select s
              select t
              select w
              -- Then bring the app forward. Activating raises EVERY window of
              -- the app, so it has to happen around the selection, not instead
              -- of it - and index 1 is the one you end up looking at.
              activate
              try
                if (index of w) is not 1 then set index of w to 1
              end try
              -- Say where we actually landed. "select" can be overruled, and a
              -- jump that quietly lands somewhere else is worse than one that
              -- admits it.
              try
                set landed to tty of current session of current tab of current window
              on error
                set landed to "unknown"
              end try
              if landed is target then
                return "focused " & target
              end if
              return "asked for " & target & " but landed on " & landed
            end if
          end try
        end repeat
      end repeat
    end repeat
  end tell
  return "not found: " & target
end run
