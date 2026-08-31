#!/bin/bash
# Registers a ccwho:// URL handler so the tty column in ccwho output is clickable.
# Builds a tiny AppleScript applet; no daemon, no login item.
set -euo pipefail
APP="${1:-$HOME/Applications/ccwho-jump.app}"
CCWHO="${CCWHO_BIN:-$HOME/.local/bin/ccwho}"
mkdir -p "$(dirname "$APP")"
tmp=$(mktemp -d)
cat > "$tmp/handler.applescript" <<APPLESCRIPT
on open location this_URL
  set AppleScript's text item delimiters to "ccwho://jump/"
  set parts to text items of this_URL
  if (count of parts) < 2 then return
  set target to item 2 of parts
  set AppleScript's text item delimiters to ""
  -- validated again by ccwho itself before use. Swallow failures: a non-zero
  -- exit from do shell script raises a modal dialog, which is worse than silence.
  try
    do shell script quoted form of "$CCWHO" & " jump " & quoted form of target
  end try
end open location
APPLESCRIPT
rm -rf "$APP"
osacompile -o "$APP" "$tmp/handler.applescript"
PL="$APP/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :CFBundleURLTypes array" \
  -c "Add :CFBundleURLTypes:0 dict" \
  -c "Add :CFBundleURLTypes:0:CFBundleURLName string ccwho jump" \
  -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes array" \
  -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string ccwho" \
  "$PL" >/dev/null
# LSUIElement keeps the applet out of the Dock; Add, not Set - the key is absent.
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PL" >/dev/null 2>&1 || true
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$APP"
rm -rf "$tmp"
echo "registered: $APP"
echo "ccwho --links now emits clickable ttys (iTerm2 renders OSC 8)."
