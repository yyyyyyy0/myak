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
else
    echo "'claude' CLI not found; run later: claude plugin uninstall myak@myak"
fi

if [ -f "$HOME/Library/LaunchAgents/com.myak.export.plist" ]; then
    echo "LaunchAgent left in place; remove with:"
    echo "  launchctl unload ~/Library/LaunchAgents/com.myak.export.plist && rm ~/Library/LaunchAgents/com.myak.export.plist"
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
