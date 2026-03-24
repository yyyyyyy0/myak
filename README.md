# myak

> **myak** — from Japanese **脈** (myaku), meaning "pulse" or "vein". As in 文脈 (context) and 脈絡 (coherence) — the threads of meaning that connect conversations over time.

Persistent memory for Claude Code. Automatically indexes session transcripts into SQLite FTS5 and makes past conversations searchable — across projects, across sessions.

## What it does

1. **Auto-index** — A Claude Code hook runs `myak-index` at the end of every session, extracting conversation segments into a local SQLite database with full-text search (FTS5 trigram).
2. **Auto-recall** — On each prompt, a `UserPromptSubmit` hook searches memory for related context and injects it as a system message. Claude sees relevant past conversations without you doing anything.
3. **Manual search** — `myak-query` lets you search memory from the terminal.
4. **Backfill** — `myak-backfill` indexes all existing session transcripts at once.
5. **Obsidian export** — `myak-export` turns your session history into Markdown notes for Obsidian, organized by project.
6. **MCP server** — `myak-mcp` exposes `myak_search` and `myak_save` tools over the Model Context Protocol for use with Claude Desktop.

## Requirements

- Python 3.10+
- Claude Code (with `~/.claude/settings.json` and `~/.claude/projects/`)
- `jq` (for hook installation)
- macOS recommended (LaunchAgent support for scheduled Obsidian export)

## Install

```bash
git clone https://github.com/yyyyyyy0/myak.git
cd myak
./install.sh
```

The installer does 5 things:

1. `pip install -e .` — installs myak and its CLI commands
2. Adds `Stop` and `UserPromptSubmit` hooks to `~/.claude/settings.json` (backs up first)
3. Optionally sets up a macOS LaunchAgent for daily Obsidian export
4. Adds a `claude-reload` shell alias for reloading settings across tmux panes
5. Runs an initial backfill of all existing sessions

To enable Obsidian export during install:

```bash
MYAK_OBSIDIAN_VAULT=~/path/to/vault ./install.sh
```

## CLI commands

### `myak-query <query>`

Search past conversations.

```bash
myak-query "authentication flow"
myak-query "デプロイ手順"
```

Options:
- `--limit N` — max results (default: 5)
- `--hook` — hook mode, reads query from stdin JSON, outputs JSON for Claude
- `--codex` — compact output for Codex prompt injection

### `myak-index`

Index a session transcript.

```bash
# From Claude Code Stop hook (reads JSON from stdin)
myak-index

# From CLI
myak-index --file ~/.claude/projects/some-project/abc123.jsonl
myak-index --session abc123 --project some-project
```

### `myak-backfill [project]`

Bulk-index all existing session transcripts.

```bash
myak-backfill              # all projects
myak-backfill myproject    # filter by project slug (partial match)
```

### `myak-export`

Export sessions to Obsidian vault as Markdown.

```bash
myak-export --vault ~/obsidian/vault
myak-export --vault ~/obsidian/vault --since 7     # last 7 days
myak-export --vault ~/obsidian/vault --project foo  # filter by project
```

### `myak-mcp`

Run the MCP server (stdio transport). Intended for use with Claude Desktop.

Add to your Claude Desktop config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "myak": {
      "command": "myak-mcp"
    }
  }
}
```

Exposes two tools:
- **myak_search** — search past conversation memory
- **myak_save** — persist important information to long-term memory

## How it works

### Storage

All data lives in `~/.claude/memory/memory.db` (SQLite). Two tables:

- **sessions** — one row per conversation session (ID, project, timestamps, segment count)
- **segments** — individual conversation turns (role, content, timestamp), linked to sessions
- **segments_fts** — FTS5 virtual table with trigram tokenizer for substring search

### Search ranking

Results are ranked by FTS5 relevance score, then re-ranked with exponential time decay (half-life: 30 days). Recent conversations surface higher.

### Hooks

The installer adds two hooks to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{
      "matcher": "*",
      "hooks": [{ "type": "command", "command": "myak-index", "async": true }]
    }],
    "UserPromptSubmit": [{
      "matcher": "*",
      "hooks": [{
        "type": "command",
        "command": "query=$(cat | python3 -c \"...\"); myak-query --hook \"$query\""
      }]
    }]
  }
}
```

- **Stop** — indexes the session transcript after Claude finishes
- **UserPromptSubmit** — searches memory on each prompt and injects matching context

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `CLAUDE_DIR` | `~/.claude` | Claude Code config directory |
| `MYAK_DIR` | `~/.claude/memory` | Database and memory directory |
| `MYAK_OBSIDIAN_VAULT` | (none) | Obsidian vault path for export |

## Uninstall

```bash
pip uninstall myak
```

Then remove the myak hooks from `~/.claude/settings.json` (search for `myak-index` and `myak-query`) and optionally:

```bash
# Remove LaunchAgent
launchctl unload ~/Library/LaunchAgents/com.myak.export.plist
rm ~/Library/LaunchAgents/com.myak.export.plist

# Remove database
rm -rf ~/.claude/memory/memory.db

# Remove shell alias from ~/.zshrc
```

## License

MIT
