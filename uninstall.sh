#!/bin/bash
# myak uninstaller. Leaves the database untouched.
set -uo pipefail

MYAK_SHIM="$HOME/.local/bin/myak"
[ -x "$MYAK_SHIM" ] || MYAK_SHIM="$(command -v myak || true)"

echo "=== myak uninstaller ==="

if [ -n "$MYAK_SHIM" ]; then
    "$MYAK_SHIM" migrate-settings --mode remove
else
    echo "myak not found; skipping settings.json hook removal."
fi

if command -v claude >/dev/null 2>&1; then
    claude plugin uninstall myak@myak
    claude plugin marketplace remove myak || true
else
    echo "'claude' CLI not found; run later: claude plugin uninstall myak@myak"
fi

PLIST="$HOME/Library/LaunchAgents/com.myak.export.plist"
if [ -f "$PLIST" ]; then
    first_arg="$(PLIST="$PLIST" python3 -c '
import os, plistlib
with open(os.environ["PLIST"], "rb") as f:
    print(plistlib.load(f).get("ProgramArguments", [""])[0])
' 2>/dev/null || true)"
    case "$first_arg" in
        */myak-export*)
            launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
            rm -f "$PLIST"
            echo "Removed LaunchAgent: $PLIST"
            ;;
        *)
            echo "LaunchAgent $PLIST does not look like myak's; left in place."
            ;;
    esac
fi

LEGACY_LAUNCHER="$HOME/.claude/myak-state/bin/myak-hook"
if [ -f "$LEGACY_LAUNCHER" ]; then
    rm -f "$LEGACY_LAUNCHER"
    echo "Removed legacy launcher: $LEGACY_LAUNCHER"
fi

if command -v uv >/dev/null 2>&1; then
    uv tool uninstall myak
else
    echo "uv not found; run later: uv tool uninstall myak"
fi

echo ""
echo "Your data was NOT deleted:"
echo "  Database:      ~/.claude/memory/"
echo "  State/backups: ~/.claude/myak-state/"
