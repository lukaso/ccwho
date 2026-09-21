#!/bin/bash
# Registers a ccwho:// URL handler: the tty column in `ccwho` output and the
# project names in `ccwho restore` become clickable.
# Builds a tiny AppleScript applet; no daemon, no login item.
set -euo pipefail
APP="${1:-$HOME/Applications/ccwho-jump.app}"
CCWHO="${CCWHO_BIN:-$HOME/.local/bin/ccwho}"
# The three tools this needs, overridable so the tests can watch what the script
# does without compiling an applet or touching LaunchServices.
OSACOMPILE="${OSACOMPILE:-osacompile}"
PLISTBUDDY="${PLISTBUDDY:-/usr/libexec/PlistBuddy}"
LSREGISTER="${LSREGISTER:-/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister}"
mkdir -p "$(dirname "$APP")"
tmp=$(mktemp -d)
# AppleScript string literals, not shell ones: a path containing a backslash or
# a double quote would otherwise end the literal early and compile into a
# different command. Escape both, in that order.
ESCAPED=${CCWHO//\\/\\\\}
ESCAPED=${ESCAPED//\"/\\\"}
cat > "$tmp/handler.applescript" <<APPLESCRIPT
on open location this_URL
  -- Forwards the WHOLE url and lets ccwho decide the verb, so adding one never
  -- means rebuilding this applet. Every target is pattern validated on the far
  -- side; anything unrecognised reaches no verb at all. Swallow failures: a
  -- non-zero exit from do shell script raises a modal dialog, which is worse
  -- than silence.
  try
    do shell script quoted form of "$ESCAPED" & " url " & quoted form of this_URL
  end try
end open location
APPLESCRIPT
# Build the new applet beside the old one and only then swap. The old order
# deleted a WORKING handler first, so a compile failure left the machine with no
# handler at all and clicks that quietly did nothing.
NEW="$tmp/new.app"
"$OSACOMPILE" -o "$NEW" "$tmp/handler.applescript"
PL="$NEW/Contents/Info.plist"
"$PLISTBUDDY" -c "Add :CFBundleURLTypes array" \
  -c "Add :CFBundleURLTypes:0 dict" \
  -c "Add :CFBundleURLTypes:0:CFBundleURLName string ccwho jump" \
  -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes array" \
  -c "Add :CFBundleURLTypes:0:CFBundleURLSchemes:0 string ccwho" \
  "$PL" >/dev/null
# LSUIElement keeps the applet out of the Dock; Add, not Set - the key is absent.
"$PLISTBUDDY" -c "Add :LSUIElement bool true" "$PL" >/dev/null 2>&1 || true
rm -rf "$APP.old"
[ -e "$APP" ] && mv "$APP" "$APP.old"
mv "$NEW" "$APP"
rm -rf "$APP.old"
"$LSREGISTER" -f "$APP"
rm -rf "$tmp"
echo "registered: $APP"
echo "clickable ttys in \`ccwho\`, clickable projects in \`ccwho restore\` (OSC 8)."
