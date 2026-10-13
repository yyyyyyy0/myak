#!/bin/sh
# myak plugin launcher (POSIX sh). Resolves the interpreter without relying on PATH.
# Usage: run.sh <hook-event> | run.sh mcp
# Never falls back to `uv tool run` (it could resolve an unrelated PyPI package).
# Hooks must never fail the session: if nothing is found, exit 0 silently.
[ -n "${HOME:-}" ] || exit 0
[ -n "${1:-}" ] || exit 0
if [ "$1" != "mcp" ]; then
    set -- hook "$1"
fi

if [ -n "${MYAK_BIN:-}" ] && [ -x "$MYAK_BIN" ]; then
    exec "$MYAK_BIN" "$@"
fi
if [ -x "$HOME/.local/bin/myak" ]; then
    exec "$HOME/.local/bin/myak" "$@"
fi
venv_file="${MYAK_STATE_DIR:-$HOME/.claude/myak-state}/venv-python"
if [ -f "$venv_file" ]; then
    py="$(head -n 1 "$venv_file" 2>/dev/null)"
    if [ -n "$py" ] && [ -x "$py" ]; then
        exec "$py" -I -m myak.cli "$@"
    fi
fi
if command -v myak >/dev/null 2>&1; then
    exec myak "$@"
fi
exit 0
