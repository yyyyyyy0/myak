#!/bin/sh
# myak plugin launcher (POSIX sh). Resolves the interpreter without relying on PATH.
# Usage: run.sh <hook-event> | run.sh mcp
# Keep the resolve block byte-identical with myak/templates/myak-hook.sh (tested).
# Never falls back to `uv tool run` (it could resolve an unrelated PyPI package).
# Hook mode must never fail the session: always exit 0. MCP mode exits 1 if nothing is found.
[ -n "${HOME:-}" ] || exit 0
[ -n "${1:-}" ] || exit 0
mode="$1"

# --- resolve begin ---
bin=""
py=""
if [ -n "${MYAK_BIN:-}" ] && [ -x "$MYAK_BIN" ]; then
    bin="$MYAK_BIN"
elif [ -x "$HOME/.local/bin/myak" ]; then
    bin="$HOME/.local/bin/myak"
else
    venv_file="${MYAK_STATE_DIR:-$HOME/.claude/myak-state}/venv-python"
    if [ -f "$venv_file" ]; then
        py="$(head -n 1 "$venv_file" 2>/dev/null)"
        if [ -n "$py" ] && [ -x "$py" ]; then
            :
        else
            py=""
        fi
    fi
    if [ -z "$py" ] && command -v myak >/dev/null 2>&1; then
        bin="$(command -v myak)"
    fi
fi
# --- resolve end ---

if [ "$mode" = "mcp" ]; then
    if [ -n "$bin" ]; then
        exec "$bin" mcp
    elif [ -n "$py" ]; then
        exec "$py" -I -m myak.cli mcp
    fi
    echo "myak: no interpreter found; run install.sh" >&2
    exit 1
fi

if [ -n "$bin" ]; then
    "$bin" hook "$mode" || exit 0
elif [ -n "$py" ]; then
    "$py" -I -m myak.cli hook "$mode" || exit 0
fi
exit 0
