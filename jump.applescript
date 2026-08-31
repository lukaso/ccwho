on run argv
  set target to item 1 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          try
            if (tty of s) is target then
              select w
              select t
              select s
              activate
              return "focused " & target
            end if
          end try
        end repeat
      end repeat
    end repeat
  end tell
  return "not found: " & target
end run
