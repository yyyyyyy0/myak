#!/bin/sh
# myak hook launcher (POSIX sh). Resolves the interpreter without relying on PATH.
# Hooks must never fail the session: if nothing is found, exit 0 silently.
event="$1"
[ -n "$event" ] || exit 0

if [ -n "${MYAK_BIN:-}" ] && [ -x "$MYAK_BIN" ]; then
    exec "$MYAK_BIN" hook "$event"
fi
if [ -x "$HOME/.local/bin/myak" ]; then
    exec "$HOME/.local/bin/myak" hook "$event"
fi
if [ -x "$HOME/.local/bin/uv" ]; then
    exec "$HOME/.local/bin/uv" tool run myak hook "$event"
fi
if [ -x /opt/homebrew/bin/uv ]; then
    exec /opt/homebrew/bin/uv tool run myak hook "$event"
fi
venv_file="${MYAK_STATE_DIR:-$HOME/.claude/myak-state}/venv-python"
if [ -f "$venv_file" ]; then
    py="$(head -n 1 "$venv_file" 2>/dev/null)"
    if [ -n "$py" ] && [ -x "$py" ]; then
        exec "$py" -m myak.cli hook "$event"
    fi
fi
if command -v myak >/dev/null 2>&1; then
    exec myak hook "$event"
fi
exit 0
