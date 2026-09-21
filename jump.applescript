on run argv
  set target to item 1 of argv
  tell application "iTerm2"
    -- activate FIRST. Activating an app raises its windows, and whichever
    -- window macOS raises last is the one you end up looking at - so selecting
    -- before activating lets the activation undo the selection. Select after,
    -- and the choice is the last thing that happens.
    activate
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          try
            if (tty of s) is target then
              select w
              select t
              select s
              -- Say where we ACTUALLY landed. "select" can be overruled - by a
              -- window that refuses to come forward, by a pane that is not the
              -- active one - and a jump that silently lands somewhere else is
              -- worse than one that says it failed.
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
