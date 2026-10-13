#!/bin/sh
# myak hook launcher (POSIX sh). Resolves the interpreter without relying on PATH.
# Keep the resolve block byte-identical with hooks/run.sh (tested).
# Never falls back to `uv tool run` (it could resolve an unrelated PyPI package).
# Hooks must never fail the session: always exit 0.
[ -n "${HOME:-}" ] || exit 0
event="${1:-}"
[ -n "$event" ] || exit 0

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

if [ -n "$bin" ]; then
    "$bin" hook "$event" || exit 0
elif [ -n "$py" ]; then
    "$py" -I -m myak.cli hook "$event" || exit 0
fi
exit 0
