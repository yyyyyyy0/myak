# myak

> **myak** — from Japanese **脈** (myaku), meaning "pulse" or "vein". As in 文脈 (context) and 脈絡 (coherence) — the threads of meaning that connect conversations over time.

Persistent memory for Claude Code. Indexes session transcripts into SQLite FTS5 and makes past conversations searchable across projects and sessions.

## What it does

1. **Auto-index.** `Stop` indexes the transcript after every turn, incrementally. `SessionEnd` does a final flush. Subagent transcripts are indexed too.
2. **Auto-recall.** `UserPromptSubmit` searches memory and injects related context via `additionalContext`.
3. **Project summary.** `SessionStart` (`startup|clear|compact`) injects the most recent sessions of the current project.
4. **MCP tools.** `myak_search` and `myak_save`, for Claude Code and Claude Desktop.
5. **Manual search.** `myak query` from the terminal.
6. **Obsidian export.** `myak export` writes Markdown notes per project.

## Requirements

- Python 3.12+, managed by [uv](https://docs.astral.sh/uv/)
- Claude Code with plugin support (`claude plugin ...`). The exact minimum version is not verified here; use a current release.
- macOS is optional: a LaunchAgent can run the Obsidian export daily.

## Install

```bash
git clone https://github.com/yyyyyyy0/myak.git
cd myak
./install.sh
```

The installer:

1. Runs `uv tool install` (shim at `~/.local/bin/myak`).
2. Runs `myak install-hooks` (machine-local hook launcher).
3. Adds the repo as a plugin marketplace and installs `myak@myak`.
4. Optionally removes legacy `settings.json` hooks.
5. Optionally installs a LaunchAgent for Obsidian export (macOS).
6. Optionally adds a `claude-reload` shell alias.
7. Runs an initial `myak backfill`.

Switches:

| Variable | Effect |
|---|---|
| `MYAK_EDITABLE=1` | Editable install. The clone must stay in place. |
| `MYAK_REMOVE_SETTINGS_HOOKS=1` | Run `myak migrate-settings --mode remove`. |
| `MYAK_SHELL_ALIAS=1` | Add the `claude-reload` alias to `~/.zshrc`. |
| `MYAK_OBSIDIAN_VAULT=<path>` | Install a daily 9:17 export LaunchAgent. |

If both the plugin and legacy `settings.json` hooks are active, `UserPromptSubmit` injects context twice. Remove the legacy hooks.

### Manual install

```bash
uv tool install . --python 3.12
claude plugin marketplace add /path/to/myak
claude plugin install myak@myak
```

The plugin ships the hooks, the MCP server (`.mcp.json`) and the `myak-recall` skill.

If you prefer `settings.json` hooks over the plugin, run `myak install-hooks`. It writes the launcher and prints a snippet to paste. Do not use both.

## Upgrading from 0.1 / 0.2

Short version:

```bash
myak migrate --dry-run
myak migrate
myak migrate-settings --mode remove --dry-run
myak migrate-settings --mode remove
myak clean-legacy --dry-run
myak clean-legacy
```

Hooks go quiet on an old database until you run `myak migrate`. Syncthing users: migrate on one machine while the others are idle. Full steps in [docs/MIGRATION.md](docs/MIGRATION.md).

## CLI reference

All commands are subcommands of `myak`.

| Command | Purpose |
|---|---|
| `myak hook {stop,session-end,session-start,prompt}` | Hook handler. Reads JSON on stdin. Always exits 0. |
| `myak index [--file F] [--session S] [--project P] [--final] [--embed]` | Index one transcript. |
| `myak query [--limit N] [--hook] [--codex] [--hybrid\|--no-hybrid] <query>` | Search. `--hook` emits hook JSON, `--codex` compact text. Hybrid is on by default when `myak[vec]` is available. |
| `myak backfill [project] [--rebuild-fts]` | Index all transcripts, including subagents. `project` is a partial slug match. |
| `myak export [--vault V] [--since DAYS] [--project P]` | Export to Obsidian. |
| `myak mcp` | MCP server over stdio. |
| `myak migrate [--dry-run]` | Migrate the schema. Explicit only. |
| `myak reindex --full` | Reset cursors and re-run backfill. `--full` is required. |
| `myak install-hooks` | Write the launcher and print a settings snippet. |
| `myak migrate-settings --mode {rewrite,remove} [--dry-run] [--path P] [--launcher L]` | Edit myak entries in `settings.json`. Writes a `.bak`. |
| `myak clean-legacy [--dry-run]` | Apply noise filters to old rows without uuid. |
| `myak embed [--limit N] [--model M] [--rebuild]` | Embed pending segments (needs `myak[vec]`). `--model` accepts known models only; switching an existing index to another model requires `--rebuild`. |

Legacy aliases, kept for one release: `myak-index`, `myak-query`, `myak-backfill`, `myak-export`, `myak-mcp`.

### MCP server

With the plugin, the server starts automatically. For Claude Desktop, use the uv shim:

```json
{
  "mcpServers": {
    "myak": { "command": "~/.local/bin/myak-mcp" }
  }
}
```

Use the absolute path if your client does not expand `~`. Tools: `myak_search`, `myak_save`.

## How it works

### Storage

| Path | Synced? | Content |
|---|---|---|
| `~/.claude/memory/memory.db` | yes | The only file to sync. Sessions, segments, FTS5 (trigram). |
| `~/.claude/myak-state/` | no | Machine-local state. |
| `  state.db` | no | Incremental cursors. |
| `  hook.log` | no | Hook log (rotating, 0600). |
| `  bin/`, `venv-python` | no | Launcher and recorded interpreter. |
| `  backups/` | no | Pre-migration backups (newest 3). |
| `  models/` | no | Embedding model cache. |
| `  memory.vec.db` | no | Vector cache. Rebuildable. |

SQLite runs with `journal_mode=DELETE`, so there are no `-wal`/`-shm` files.

### Schema versions

Tracked with `PRAGMA user_version`.

- **v0** — original schema.
- **v1** — adds `uuid`, `agent_id`, `agent_type` on segments, `cwd` on sessions, and a partial unique index `(session_id, agent_id, uuid)`.
- **v2** — adds the `sessions(cwd, ended_at)` index for SessionStart.

Hooks never migrate. On an older schema they log once and exit 0. Code refuses to open a newer schema.

### Indexing

- Dedupe key is `uuid` (inserts use `ON CONFLICT DO NOTHING`). Records without a uuid get a synthetic key from file name and offset.
- Cursors (path, offset, size, first-4KB hash) live in `state.db`, not in the synced DB.
- A file is fully rescanned when there is no cursor, the fingerprint changed, the offset exceeds the size, or the byte before the offset is not `\n`. Rescans add no duplicates.
- An unterminated last line waits for the next run. `SessionEnd`, `index` and `backfill` accept it if it parses.
- Noise is dropped: system reminders, command tags (including `local-command-caveat`), meta records, tool-result-only turns. `last_assistant_message` is not stored.
- Subagent transcripts are stored under the parent session with `agent_id` and `agent_type`.

### Hook timeouts

`SessionEnd` has a 1.5 s default budget, and plugin hooks cannot raise it. So `myak hook session-end` does no indexing itself. It starts a detached `index --final --embed` process and exits in under 200 ms. Set `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` if you want to raise the budget for settings-based hooks. Plugin hooks ignore per-hook timeouts for this event.

`Stop` runs async (60 s). `UserPromptSubmit` has 10 s, `SessionStart` 5 s.

## Recall quality

Hook recall filters aggressively:

- Prompts starting with `/` are skipped.
- Absolute score floor and a relative floor (35% of the best hit).
- Queries with 3+ tokens need at least 2 matching tokens.
- At most one hit per session. Up to 3 hits, 220 chars each.
- Time decay (half-life 30 days). Subagent hits are weighted by `SUBAGENT_WEIGHT` (0.5).
- Injected text is wrapped in `<past-memory untrusted="true">` with escaping. It is data, not instructions.

`SessionStart` lists up to 5 recent sessions of the current project (title and date), capped at 1500 chars. Subagent rows are excluded and control characters are stripped.

## Hybrid search (optional)

Adds vector search on top of FTS5, fused with RRF.

myak is not on PyPI, so install the extra from the clone (or from git):

```bash
# from your clone (replaces an existing install)
uv tool install '.[vec]' --python 3.12 --force
# or straight from GitHub, pinned to a release
uv tool install 'myak[vec] @ git+https://github.com/yyyyyyy0/myak.git@v0.3.0' --python 3.12 --force
myak embed
```

- Model: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` via fastembed (384-dim).
- Download is about 0.22 GB and happens only when you run `myak embed`. Hooks never download anything.
- Vectors live in `memory.vec.db`. It is a derived cache: local, not synced, rebuildable. The model you embedded with sticks for search. Only `myak embed` (never search or hooks) writes or rebuilds it.
- `models/<model>.ready` is written after the model loads once; hooks and search treat the model as available only if it exists (fastembed's own cache dir names do not match the model id).
- Hybrid hits farther than cosine distance 0.6 (`VEC_MAX_DISTANCE`) are dropped. Vector-only hits skip the keyword-match guards.
- `embed.lock` in `myak-state/` serialises embedding; a second run says "another embed is running". `SessionEnd` embeds at most 500 segments per run, niced.
- The `UserPromptSubmit` hook stays FTS-only. Hybrid applies to `myak query`, MCP and backfill. `SessionEnd` embeds after indexing.
- Without the extra, or if the model is missing, search falls back to FTS-only.
- Kill switch: `MYAK_DISABLE_VEC` is on for `1`, `true`, `yes`, `on` (case-insensitive); anything else leaves vectors enabled.

## Cross-machine sync

Sync only `~/.claude/memory/` (just `memory.db`). Never sync `~/.claude/myak-state/`.

Use [Syncthing](https://syncthing.net/). Copy `.stignore.example` to `~/.claude/memory/.stignore` on each machine. `.stignore` is per device. It ignores `*-journal`, `*-wal`, `*-shm`.

Caveats:

- The DB is synced as a whole file. Writes on two machines at once can produce a `*.sync-conflict-*` copy. Do not delete it blindly; it may hold sessions only the other machine indexed.
- Per-turn indexing writes the DB more often than the old per-session indexing, so conflicts are likelier. Avoid running two machines at the same time.
- Per-machine DBs and uuid merge are out of scope.
- Migrate on one machine only (see [docs/MIGRATION.md](docs/MIGRATION.md)).

## Coexistence with Claude Code auto memory (MEMORY.md)

They do different jobs.

| | MEMORY.md (auto memory) | myak |
|---|---|---|
| Content | Curated, small notes | Large archive of past conversations |
| Loading | Always loaded at session start | On demand: hook injection, MCP search |
| Best for | Durable preferences and rules | "What did we decide / how did we fix X" |

Guidance:

- Put stable rules and preferences in MEMORY.md. Do not copy them into myak, and do not mirror chat history into MEMORY.md.
- Use `myak_save` for facts that are useful but too specific or too bulky for MEMORY.md.
- Token cost: MEMORY.md is paid every session. myak costs tokens only when a hook injects (at most 3 short hits per prompt, up to 1500 chars at session start) or when a tool is called.
- If a fact appears in both, the always-loaded one wins. Remove the duplicate from myak's side by not saving it.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `CLAUDE_DIR` | `~/.claude` | Claude Code config directory |
| `MYAK_DIR` | `$CLAUDE_DIR/memory` | Synced DB directory |
| `MYAK_STATE_DIR` | `$CLAUDE_DIR/myak-state` | Machine-local state |
| `MYAK_OBSIDIAN_VAULT` | (none) | Default vault for export; enables the LaunchAgent in `install.sh` |
| `MYAK_BIN` | (none) | Explicit `myak` binary for the hook launcher |
| `MYAK_DISABLE_VEC` | (unset) | `1`/`true`/`yes`/`on` disables vector search |
| `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` | Claude Code default (1500) | Claude Code's SessionEnd budget. Not read by myak. |

The launcher resolves the binary in this order: `$MYAK_BIN`, `~/.local/bin/myak`, the recorded venv python, `myak` on PATH. It never uses `uv tool run`.

## Uninstall

```bash
./uninstall.sh
```

Removes settings hooks, the plugin and marketplace, the LaunchAgent, the legacy launcher and the uv tool. Your data is not deleted: `~/.claude/memory/` and `~/.claude/myak-state/` stay.

## License

MIT
