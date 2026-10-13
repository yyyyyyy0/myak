#!/bin/bash
# myak installer: uv tool + Claude Code plugin (D9/D11/D14).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
MYAK_SHIM="$HOME/.local/bin/myak"
EXPORT_SHIM="$HOME/.local/bin/myak-export"

echo "=== myak installer ==="
echo ""

# 1. uv tool install
echo "[1/6] Installing myak with uv..."
if ! command -v uv >/dev/null 2>&1; then
    echo "  ERROR: uv is required but not found." >&2
    echo "  Install it: curl -LsSf https://astral.sh/uv/install.sh | sh   (or: brew install uv)" >&2
    exit 1
fi
if [ "${MYAK_EDITABLE:-}" = "1" ]; then
    echo "  WARNING: editable install; the clone at $SCRIPT_DIR must stay in place."
    uv tool install --editable "$SCRIPT_DIR" --python 3.12 --force
else
    uv tool install "$SCRIPT_DIR" --python 3.12 --force
fi
if [ ! -x "$MYAK_SHIM" ]; then
    MYAK_SHIM="$(command -v myak || true)"
fi
[ -n "$MYAK_SHIM" ] || { echo "  ERROR: myak shim not found after install (is ~/.local/bin on PATH? try: uv tool update-shell)" >&2; exit 1; }
echo "  Installed: $MYAK_SHIM"

# 2. Hook launcher (machine-local fallback used by settings.json hooks)
echo "[2/6] Installing hook launcher..."
"$MYAK_SHIM" install-hooks

# 3. Claude Code plugin
echo "[3/6] Claude Code plugin..."
if command -v claude >/dev/null 2>&1; then
    claude plugin marketplace add "$SCRIPT_DIR" || echo "  marketplace already added"
    claude plugin install myak@myak || echo "  plugin already installed (try: claude plugin update myak@myak)"
else
    echo "  'claude' CLI not found. Run these when it is available:"
    echo "    claude plugin marketplace add \"$SCRIPT_DIR\""
    echo "    claude plugin install myak@myak"
fi

# 4. Legacy settings.json hooks (the plugin supersedes them)
echo "[4/6] Legacy settings.json hooks..."
if [ "${MYAK_REMOVE_SETTINGS_HOOKS:-}" = "1" ]; then
    "$MYAK_SHIM" migrate-settings --mode remove
else
    echo "  WARNING: legacy settings.json hooks were left in place. While they coexist"
    echo "  with the plugin, UserPromptSubmit injects context TWICE."
    echo "  Remove them with:  myak migrate-settings --mode remove      (preview: add --dry-run)"
    echo "  Or re-run with MYAK_REMOVE_SETTINGS_HOOKS=1."
fi

# 5. LaunchAgent (macOS only, optional)
echo "[5/6] LaunchAgent setup..."
if [ "$(uname)" = "Darwin" ] && [ -n "${MYAK_OBSIDIAN_VAULT:-}" ]; then
    PLIST_TEMPLATE="$SCRIPT_DIR/myak/templates/com.myak.export.plist.template"
    PLIST_DEST="$LAUNCH_AGENTS/com.myak.export.plist"

    mkdir -p "$LAUNCH_AGENTS"
    launchctl unload "$PLIST_DEST" 2>/dev/null || true

    sed -e "s|__MYAK_EXPORT_BIN__|$EXPORT_SHIM|g" \
        -e "s|__OBSIDIAN_VAULT__|$MYAK_OBSIDIAN_VAULT|g" \
        "$PLIST_TEMPLATE" > "$PLIST_DEST"

    launchctl load "$PLIST_DEST"
    echo "  LaunchAgent installed: daily 9:17 AM export to $MYAK_OBSIDIAN_VAULT"
else
    if [ "$(uname)" != "Darwin" ]; then
        echo "  Not macOS, skipping LaunchAgent."
    else
        echo "  Set MYAK_OBSIDIAN_VAULT to enable daily Obsidian export."
        echo "  Example: MYAK_OBSIDIAN_VAULT=~/obsidian/vault ./install.sh"
    fi
fi

# 6. Shell alias
echo "[6/6] Shell alias..."
ZSHRC="$HOME/.zshrc"
if [ "${MYAK_SHELL_ALIAS:-}" != "1" ]; then
    echo "  Skipped (opt in with MYAK_SHELL_ALIAS=1 to add the claude-reload alias)."
elif [ -f "$ZSHRC" ] && ! grep -q 'claude-reload' "$ZSHRC" 2>/dev/null; then
    cat >> "$ZSHRC" << 'ALIAS'

# myak: reload Claude Code settings across all tmux panes
alias claude-reload='for pane in $(tmux list-panes -a -F "#{pane_id} #{pane_current_command}" | grep -i claude | awk "{print \$1}"); do tmux send-keys -t "$pane" "/reload" Enter; done'
ALIAS
    echo "  Added claude-reload alias to $ZSHRC"
else
    echo "  Alias already exists or no .zshrc, skipping."
fi

echo ""
echo "Existing database? Preview the schema migration first:  myak migrate --dry-run"
echo "Running initial backfill..."
"$MYAK_SHIM" backfill || echo "WARN: backfill failed; run 'myak backfill' later"

echo ""
echo "=== Done ==="
echo "Commands: myak {hook,backfill,reindex,migrate,migrate-settings,mcp,...}, myak-query, myak-export"
