#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"

echo "=== myak installer ==="
echo ""

# 1. pip install
echo "[1/5] Installing myak..."
pip install -e "$SCRIPT_DIR" --quiet
echo "  Installed: $(which myak-index)"

# 2. Merge hooks into settings.json
echo "[2/5] Configuring Claude Code hooks..."
if [ -f "$SETTINGS" ]; then
    HOOKS_TEMPLATE="$SCRIPT_DIR/templates/hooks.json"

    # Check if myak hooks already exist
    if grep -q 'myak-index' "$SETTINGS" 2>/dev/null; then
        echo "  Hooks already configured, skipping."
    else
        # Backup
        cp "$SETTINGS" "$SETTINGS.bak"

        # Merge Stop hook
        jq '.hooks.Stop += [input.Stop[0]]' "$SETTINGS" "$HOOKS_TEMPLATE" > "$SETTINGS.tmp" \
            && mv "$SETTINGS.tmp" "$SETTINGS"

        # Merge UserPromptSubmit hook
        if jq -e '.hooks.UserPromptSubmit' "$SETTINGS" > /dev/null 2>&1; then
            jq '.hooks.UserPromptSubmit += [input.UserPromptSubmit[0]]' "$SETTINGS" "$HOOKS_TEMPLATE" > "$SETTINGS.tmp" \
                && mv "$SETTINGS.tmp" "$SETTINGS"
        else
            jq '.hooks.UserPromptSubmit = [input.UserPromptSubmit[0]]' "$SETTINGS" "$HOOKS_TEMPLATE" > "$SETTINGS.tmp" \
                && mv "$SETTINGS.tmp" "$SETTINGS"
        fi

        echo "  Hooks added to $SETTINGS (backup: $SETTINGS.bak)"
    fi
else
    echo "  WARNING: $SETTINGS not found. Skipping hook setup."
    echo "  See templates/hooks.json for manual configuration."
fi

# 3. LaunchAgent (macOS only, optional)
echo "[3/5] LaunchAgent setup..."
if [ "$(uname)" = "Darwin" ] && [ -n "${MYAK_OBSIDIAN_VAULT:-}" ]; then
    PLIST_TEMPLATE="$SCRIPT_DIR/templates/com.myak.export.plist.template"
    PLIST_DEST="$LAUNCH_AGENTS/com.myak.export.plist"
    EXPORT_BIN="$(which myak-export)"

    # Unload old if exists
    launchctl unload "$PLIST_DEST" 2>/dev/null || true

    sed -e "s|__MYAK_EXPORT_BIN__|$EXPORT_BIN|g" \
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

# 4. Shell alias
echo "[4/5] Shell alias..."
ZSHRC="$HOME/.zshrc"
if [ -f "$ZSHRC" ] && ! grep -q 'claude-reload' "$ZSHRC" 2>/dev/null; then
    cat >> "$ZSHRC" << 'ALIAS'

# myak: reload Claude Code settings across all tmux panes
alias claude-reload='for pane in $(tmux list-panes -a -F "#{pane_id} #{pane_current_command}" | grep -i claude | awk "{print \$1}"); do tmux send-keys -t "$pane" "/reload" Enter; done'
ALIAS
    echo "  Added claude-reload alias to $ZSHRC"
else
    echo "  Alias already exists or no .zshrc, skipping."
fi

# 5. Initial backfill
echo "[5/5] Running initial backfill..."
myak-backfill

echo ""
echo "=== Done ==="
echo "Commands: myak-index, myak-query, myak-backfill, myak-export"
