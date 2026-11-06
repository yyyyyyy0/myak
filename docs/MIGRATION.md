# Migrating from 0.1 / 0.2 to 0.3

English | [日本語](MIGRATION.ja.md)

0.3 changes the schema, the install method and the hook wiring. Nothing is migrated automatically.

## Before you start

- Old pip/mise editable installs must go. Hooks cannot find them from a non-interactive PATH, and they shadow the new one. Uninstall inside the environment that owns them, for example `pip uninstall myak`.
- Syncthing users: pick one machine. Keep the others idle (Claude Code closed) until sync finishes.

## Steps

1. Install 0.3: `./install.sh` (see README).
2. Preview the schema change: `myak migrate --dry-run`.
3. Migrate: `myak migrate`. It checks integrity, writes a backup to `~/.claude/myak-state/backups/` (newest 3 kept), then migrates in one transaction. On failure the DB stays unchanged.
4. Until step 3 runs, hooks log once and do nothing. This is intended: hooks never migrate.
5. Remove legacy hooks: `myak migrate-settings --mode remove --dry-run`, then without `--dry-run`. A `.bak` is written. Only myak entries are touched. Skip this and the plugin plus legacy hooks inject twice.
6. Clean old noise rows: `myak clean-legacy --dry-run`, review, then `myak clean-legacy`. It only touches rows without a uuid, writes its own `bak-clean-*` backup (pruned separately from migrate backups), and drops affected vectors so `myak embed` recomputes them. It also removes `local-command-caveat` rows.
7. Let Syncthing finish, then start the other machines. Run `myak migrate` there too only if the DB did not arrive migrated. Check with `myak migrate --dry-run`. Each machine still needs its own install and plugin.
8. Claude Desktop: the MCP command is now `~/.local/bin/myak-mcp` (absolute path if needed). Update `claude_desktop_config.json`.

## Notes

- If you used vector search before 0.3.x, run `myak embed` once: it writes the `models/*.ready` marker that search needs. Switching models later requires `myak embed --model M --rebuild`.
- Cursors, backups and the vector cache are machine-local. Do not sync `~/.claude/myak-state/`.
- Add `.stignore` on every machine (`.stignore.example`).
- Old sessions whose transcript is gone are never changed. Transcripts older than `cleanupPeriodDays` (30 by default) cannot be re-indexed.
- A newer schema than the code is refused. Upgrade every machine before using one that has migrated.


# Schema v2 to v3 (agent source column)

v3 adds one nullable column, `segments.source`, so each memory records which agent wrote it. It only adds the column: no row is rewritten, so the FTS index is untouched.

1. Update myak on every synced machine first. Code from before v3 refuses to write a v3 database and its hooks go quiet by design, so update before any machine migrates.
2. Pick one machine and keep the others idle (Claude Code closed) until sync finishes.
3. On that machine: `myak migrate --dry-run`, then `myak migrate`. It checks integrity, writes a `bak-v2-*` backup to `~/.claude/myak-state/backups/`, adds the column in one transaction and verifies row counts.
4. Let Syncthing finish before starting the other machines. They need no migration if the DB arrived at v3 (`myak migrate --dry-run` shows it).
5. Until a machine's DB is v3, its hooks log once and do nothing, and `myak save` / `myak_save` refuse to write. This is intended.
