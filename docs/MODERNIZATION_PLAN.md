# myak modernization plan (2026-10-13)

Status: approved by owner. Three phases, one branch series each. This file is the SSOT for implementers.

## Why
Verified on the owner's machine:
- `index_session()` is idempotent per session_id → Stop hook (fires every turn) indexes only the first turn (session with 62 msgs had 2 rows).
- `~/.claude/settings.json` UserPromptSubmit hook read `message` instead of `prompt` → recall never ran.
- Editable install lives in mise python 3.10.4; shims absent from non-interactive PATH → `myak-index` not found from hooks. No Claude Code session indexed since 2026-09-24.
- Transcripts are kept 30 days (`cleanupPeriodDays`), so missed indexing is permanent loss.
- Subagent transcripts (`<slug>/<sid>/subagents/agent-*.jsonl` + `.meta.json`) are ignored.
- Noise (`<system-reminder>`, `<command-name>`, `<local-command-stdout>`, `<task-notification>`, `<side-sessions-event>`, `isMeta`) is indexed.
- MCP server is hand-rolled JSON-RPC (protocol 2024-11-05).

## Current Claude Code contract (official hooks.md, 2026-10)
- Common stdin: `session_id`, `transcript_path`, `cwd`, `hook_event_name`. Transcript file may lag the in-memory conversation.
- `Stop`: + `last_assistant_message`, `stop_hook_active`. Fires every turn.
- `SessionEnd`: + `reason` (clear|resume|logout|prompt_input_exit|other). Default budget 1.5s. Per-hook `timeout` in settings raises budget (≤60s) but **plugin-provided hook timeouts do NOT raise it** → plugin SessionEnd must detach work and exit fast.
- `UserPromptSubmit`: stdin `prompt`. Output `{"hookSpecificOutput":{"hookEventName":"UserPromptSubmit","additionalContext":"..."}}`. Plain stdout also becomes context.
- `SessionStart`: stdin `source` (startup|resume|clear|compact); output `hookSpecificOutput.additionalContext`.
- `SubagentStop`: + `agent_id`, `agent_type`, `agent_transcript_path`.
- Hook command fields: `command`, `async`, `timeout`, `statusMessage`.
- Plugins: `.claude-plugin/plugin.json`, `hooks/hooks.json` (top-level `"hooks"` key wrapping events — verify against docs), `.mcp.json` with `${CLAUDE_PLUGIN_ROOT}`, `skills/<name>/SKILL.md`. Install: `claude plugin marketplace add`, `claude plugin install name@marketplace`, dev: `claude --plugin-dir <repo>`.
- Transcript JSONL is declared internal/unstable → defensive parsing, tolerate unknown types.

## Decisions (closed)
| # | Decision |
|---|---|
| D1 | **(revised after mob-critic)** Dedupe key is `uuid` only: `segments.uuid TEXT`, `segments.agent_id TEXT NOT NULL DEFAULT ''`, partial unique index `UNIQUE(session_id, agent_id, uuid) WHERE uuid IS NOT NULL`. Records without uuid get a synthetic key `f:<file-basename>:<start_offset>`. Inserts use `INSERT ... ON CONFLICT DO NOTHING` (never `INSERT OR IGNORE`, which also hides NOT NULL violations). No content_hash, no hash backfill. Verified on a copy of the real DB: 0 duplicate groups, integrity_check ok, FTS integrity-check ok → no dedupe step in the migration. |
| D2 | **(revised)** Incremental cursors live OUTSIDE the synced DB, in a machine-local store `MYAK_STATE_DIR` (default `~/.claude/myak-state/state.db`, never synced), one row per transcript file: `path, offset, size, first4k_sha1`. Files are read in binary mode and split on `\n`. Full rescan when: no cursor, fingerprint mismatch, offset > size, or byte at offset-1 is not `\n`. A terminated but invalid-JSON line is skipped and the offset advanced. An unterminated tail is deferred, except when `final=True` (SessionEnd, CLI `index`, backfill) where it is accepted if it parses. Cursor update is monotonic (`MAX(offset, ?)`). `myak reindex --full` resets cursors. |
| D3 | **(revised)** Schema versioning via `PRAGMA user_version`; `myak/db.py::migrate(conn)` runs ONLY from the explicit `myak migrate` command. Hooks that see `user_version < CURRENT` log once and exit 0 (never auto-migrate, to avoid Syncthing divergence). Migration: integrity_check + FTS integrity-check before, backup via sqlite backup API (`conn.backup()`) to `STATE_DIR/backups/memory.db.bak-v<N>-<ts>` (machine-local, 0600, newest 3 kept; synced `MEMORY_DIR` holds only memory.db), `isolation_level=None` + `BEGIN IMMEDIATE ... COMMIT` with `PRAGMA user_version` set inside the transaction, integrity checks after. Refuse if user_version newer than code. `journal_mode=DELETE` stays. Added columns nullable/defaulted so older code on synced machines keeps working. Fault-injection test: failure mid-migration leaves v0 with unchanged counts. |
| D4 | **(revised)** Subagents: `session_id` = parent session; `agent_id` (from filename `agent-<id>`), `agent_type` (from `.meta.json`, `unknown` if absent). No `is_sidechain` column (redundant with agent_id). Role stays user/assistant. Ranking weight `SUBAGENT_WEIGHT=0.5`. Excluded from SessionStart injection. No SubagentStop hook. Each subagent file has its own cursor (D2). |
| D5 | Stop = primary indexer (async). SessionEnd = final flush with `final=True`; detaches (`Popen(start_new_session=True, cwd=STATE_DIR)` running `python -I -m myak.cli index --file=<path>`) and exits <200ms. `transcript_path` must resolve (non-symlink) to a `.jsonl` under `PROJECTS_DIR`, else logged and skipped; injected snippets are wrapped as escaped `<past-memory untrusted="true">` data; `last_assistant_message` is NOT stored; Stop does one short retry (≤1s) when the transcript tail lacks it. **Legacy rows**: when a non-`desktop-` session whose rows all have NULL uuid is indexed and its transcript exists (only 4 of 334 on the owner's machine), delete that session's NULL-uuid rows and reinsert from the transcript in ONE transaction. Sessions whose transcript is gone are never touched. **Concurrency**: all connections `isolation_level=None`, `busy_timeout=5000` (query path too); parse before `BEGIN IMMEDIATE`; sessions upsert via `ON CONFLICT DO UPDATE`; lock timeout → log, exit 0. `save_memory`: sessions insert `ON CONFLICT DO NOTHING`, uuid = `save:<sha1(content)[:16]>`. |
| D6 | Hook launcher: POSIX sh script that resolves the interpreter without PATH, in order: `$MYAK_BIN`, `$HOME/.local/bin/myak` (uv tool shim), recorded venv python (`python -m myak.cli`), `command -v myak`, else exit 0; never `uv tool run` (supply-chain risk: would resolve an unrelated PyPI package when myak is not installed). Phase 1: `STATE_DIR/bin/myak-hook` (machine-local, written atomically, never in the synced dir); Phase 2: `${CLAUDE_PLUGIN_ROOT}/hooks/run.sh`. Hooks never fail the session: catch-all, exit 0, log to `STATE_DIR/hook.log` (rotating, 0600, 200 chars/line); launcher exits 0 if `$HOME` is unset and runs python with `-I`. |
| D7 | New single CLI `myak` (`myak/cli.py`, argparse subcommands: `hook stop\|session-end\|session-start\|prompt`, `index`, `query`, `backfill`, `export`, `mcp`, `migrate-settings`, `embed`). Keep `myak-*` console scripts as aliases for one release. |
| D8 | Settings migration helper in Python (no jq): `myak migrate-settings --mode rewrite\|remove [--dry-run]`, atomic write, `.bak`, touches only entries whose command contains `myak-index`/`myak-query`/`myak-hook`. |
| D9 | Phase 2: `requires-python >=3.12`, version 0.3.0 (read via importlib.metadata), `uv tool install`, `uv.lock`, `[dependency-groups] dev`, ruff target py312, setuptools kept. CI matrix 3.12/3.13/3.14 + one macOS job, `astral-sh/setup-uv`, actions pinned by SHA. |
| D10 | MCP: official `mcp` SDK (FastMCP, stdio). Tool names/args unchanged (`myak_search`, `myak_save`). Logging to stderr only. |
| D11 | Plugin ships hooks (Stop, SessionEnd, UserPromptSubmit, SessionStart), `.mcp.json`, skill `myak-recall`. Owner's `~/.claude/settings.json` myak hooks are removed after plugin verified (cutover order: uv tool install → plugin install → verify → migrate-settings remove → pip uninstall in mise env). |
| D12 | Phase 3 SessionStart: matcher `startup\|clear\|compact`, per-project recent sessions (title = first user msg or ai-title, date), cap 1500 chars, <1s, framed as untrusted history, control chars stripped. |
| D13 | Hybrid search: optional extra `myak[vec]` = `sqlite-vec` + `fastembed` with `intfloat/multilingual-e5-small` (384-dim). Vectors in sibling file `memory.vec.db` (derived cache, not synced, rebuildable). RRF (k=60) fusion, then time decay and subagent weight. Hook path (UserPromptSubmit) stays FTS-only; hybrid used by MCP/CLI. `myak embed` batch runs from detached SessionEnd job and backfill. Graceful FTS-only fallback when extra/extension missing. `embedding_meta(model, dim)`; mismatch → re-embed. |
| D14 | install.sh keeps LaunchAgent step (absolute shim path) and `claude-reload` alias step (alias step opt-in via MYAK_SHELL_ALIAS=1). Add `uninstall.sh`. |

## Phase 1 — restore function (branch `feat/modernize-p1-incremental-indexing`)
TDD per step; one commit per step (`feat:`/`fix:`/`test:`).
1. Fixtures: `tests/fixtures/*.jsonl` covering all record kinds listed above + sidechain + partial last line + no-uuid record; `db` fixture monkeypatching `myak.config.DB_PATH`/`MEMORY_DIR`.
2. `myak/transcript.py`: `iter_records()`, `clean_text()`, `extract_segments()` → dicts with uuid, role, content, timestamp, is_sidechain, agent_id, cwd, end_offset. Strip tagged blocks (DOTALL); drop tool_result-only, isMeta, non user/assistant, `<local-command-stdout>` records.
3. `myak/db.py`: `migrate()` v0→v1 (adds `segments.uuid`, `segments.agent_id`, `segments.agent_type`, `sessions.cwd`; partial unique index per D1), explicit-only per D3. New `myak/state.py` for the machine-local cursor store (D2). Tests: counts preserved exactly, FTS still finds old rows, idempotent, journal_mode delete, backup written, fault injection → v0 unchanged, refuse on newer version.
4. `indexer.index_session(conn, path, *, final=False)` incremental per D2/D5: cursor from state store, binary tailing, parse before lock, `BEGIN IMMEDIATE`, ON CONFLICT DO NOTHING inserts, legacy-replace per D5, counters from COUNT(*), cursor stored after commit (monotonic). Tests: append → grows, rerun → 0 new, rewritten-same-size → rescan with no dupes, unterminated tail deferred vs accepted with final, CRLF, NULL role row raises not silently drops, legacy noisy rows replaced not doubled, two concurrent runs → no dupes and monotonic cursor, hooks on v0 DB → no-op.
5. Subagent discovery `discover_transcripts()`; used by indexer and backfill.
6. `query.search` selects `agent_type`, applies SUBAGENT_WEIGHT.
7. `format_hook` → hookSpecificOutput; skip prompts starting with `/`; non-string prompt guard.
8. `myak/cli.py`, `myak/hooks.py`, launcher template `templates/myak-hook.sh`, `templates/hooks.json` (Stop async+timeout+statusMessage, SessionEnd timeout, UserPromptSubmit). Subprocess tests incl. `env -i` PATH test.
9. `myak/settings_migrate.py` (D8) with fixture tests (other hooks byte-equal after parse, idempotent, .bak).
10. backfill uses discovery, `--rebuild-fts`; idempotent run test.
Acceptance: segment count grows across Stop runs; zero dupes on rerun; real-DB rehearsal on a copy preserves exactly 385 sessions / 3039 segments; hooks work with empty PATH; changed modules ≥80% coverage.
Human gates: (a) before migrating the real memory.db (show rehearsal output), (b) before editing real settings.json.

## Phase 2 — distribution (branch `feat/modernize-p2-plugin-uv`)
1. pyproject per D9; `uv.lock`.
2. FastMCP server (D10) + protocol round-trip test via in-memory client.
3. Plugin layout (D11) + `tests/test_plugin_layout.py` (JSON parses, events exact, `${CLAUDE_PLUGIN_ROOT}` used, run.sh executable, manifest version == pyproject version).
4. install.sh rewrite (uv tool install, migrate-settings remove, plugin install commands; print if `claude` missing), `uninstall.sh`.
5. CI per D9; coverage gate ratcheted to measured floor (target 80).
6. Real-machine cutover per D11 (human gate before settings removal). E2E: start Claude Code with plugin, run a prompt, confirm new rows + injected context.

## Phase 3 — search quality (branch `feat/modernize-p3-sessionstart-hybrid`)
1. SessionStart hook (D12) + tests (project scoping, empty DB → no output, cap, excludes subagents).
2. `myak/search.py` split from query.py; `myak/vec.py`; schema v2 for `embedding_meta`; sibling vec DB (D13). Tests with deterministic fake embedder; fallback test with sqlite_vec import failing.
3. `myak embed` incremental batch; hooked into detached SessionEnd job + backfill.
4. README: install via uv/plugin, schema versions + Syncthing note, `myak[vec]`, hook timeouts, "Coexistence with Claude Code auto memory (MEMORY.md)".
5. Recall regression set (8–10 query→session pairs from real corpus, kept out of repo) run before/after.

## Risks (top)
- Real DB migration (High): backup + rehearsal on copy + transaction + count assertions. mob-critic review before implementing step 3–4.
- Syncthing drift (High): migrate on one machine with the other idle, wait for sync; nullable columns; hooks never auto-migrate; cursors and vec cache live outside the synced dir. `.stignore` is per-device, so document it for each machine (`*-journal`, `*.bak-*`). Residual: whole-file sync of a DB written on two machines can still conflict; per-machine DBs or uuid merge are out of scope and noted in README.
- Plugin SessionEnd 1.5s cap (Medium): detach.
- Prompt injection via injected memories (Medium): frame as untrusted, strip control chars, cap.
- settings.json edit (Medium): dry-run, backup, exact match.
