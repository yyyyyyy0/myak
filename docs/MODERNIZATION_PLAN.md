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
| D1 | Dedupe key: `segments.uuid` (jsonl record uuid) with `UNIQUE(session_id, uuid)` + `content_hash = sha1(role\|timestamp\|content)` with `UNIQUE(session_id, content_hash)` for legacy/no-uuid rows. Writes are `INSERT OR IGNORE`. |
| D2 | Incremental indexing via `sessions.indexed_offset` (byte offset). Offset > file size → full rescan (dedupe protects). Partial last line is deferred. |
| D3 | Schema versioning via `PRAGMA user_version`; `myak/db.py::migrate(conn)`; backup `memory.db.bak-v<N>-<ts>` before first migration; refuse if user_version newer than code. `journal_mode=DELETE` stays. Added columns nullable so older code on synced machines keeps working. |
| D4 | Subagents: `session_id` = parent session; new columns `agent_id`, `agent_type` (from `.meta.json`, `unknown` if absent), `is_sidechain`. Role stays user/assistant. Ranking weight `SUBAGENT_WEIGHT=0.5`. Excluded from SessionStart injection. No SubagentStop hook (parent Stop/SessionEnd discovery covers it). |
| D5 | Stop = primary indexer (async). SessionEnd = final flush; detaches (`Popen(start_new_session=True)`) and exits <200ms. `last_assistant_message` is NOT stored; used only to detect lag → one short retry (≤1s) in Stop. |
| D6 | Hook launcher: POSIX sh script that resolves the interpreter without PATH, in order: `$HOME/.local/bin/myak` (uv tool shim), `/opt/homebrew/bin/uv`/`$HOME/.local/bin/uv` + `uv tool run`, then recorded venv path. Phase 1: `~/.claude/memory/bin/myak-hook`; Phase 2: `${CLAUDE_PLUGIN_ROOT}/hooks/run.sh`. Hooks never fail the session: catch-all, exit 0, log to `~/.claude/memory/hook.log` (rotating). |
| D7 | New single CLI `myak` (`myak/cli.py`, argparse subcommands: `hook stop\|session-end\|session-start\|prompt`, `index`, `query`, `backfill`, `export`, `mcp`, `migrate-settings`, `embed`). Keep `myak-*` console scripts as aliases for one release. |
| D8 | Settings migration helper in Python (no jq): `myak migrate-settings --mode rewrite\|remove [--dry-run]`, atomic write, `.bak`, touches only entries whose command contains `myak-index`/`myak-query`/`myak-hook`. |
| D9 | Phase 2: `requires-python >=3.12`, version 0.3.0 (read via importlib.metadata), `uv tool install`, `uv.lock`, `[dependency-groups] dev`, ruff target py312, setuptools kept. CI matrix 3.12/3.13/3.14 + one macOS job, `astral-sh/setup-uv`, actions pinned by SHA. |
| D10 | MCP: official `mcp` SDK (FastMCP, stdio). Tool names/args unchanged (`myak_search`, `myak_save`). Logging to stderr only. |
| D11 | Plugin ships hooks (Stop, SessionEnd, UserPromptSubmit, SessionStart), `.mcp.json`, skill `myak-recall`. Owner's `~/.claude/settings.json` myak hooks are removed after plugin verified (cutover order: uv tool install → plugin install → verify → migrate-settings remove → pip uninstall in mise env). |
| D12 | Phase 3 SessionStart: matcher `startup\|clear\|compact`, per-project recent sessions (title = first user msg or ai-title, date), cap 1500 chars, <1s, framed as untrusted history, control chars stripped. |
| D13 | Hybrid search: optional extra `myak[vec]` = `sqlite-vec` + `fastembed` with `intfloat/multilingual-e5-small` (384-dim). Vectors in sibling file `memory.vec.db` (derived cache, not synced, rebuildable). RRF (k=60) fusion, then time decay and subagent weight. Hook path (UserPromptSubmit) stays FTS-only; hybrid used by MCP/CLI. `myak embed` batch runs from detached SessionEnd job and backfill. Graceful FTS-only fallback when extra/extension missing. `embedding_meta(model, dim)`; mismatch → re-embed. |
| D14 | install.sh keeps LaunchAgent step (absolute shim path) and `claude-reload` alias step (out of scope to remove). Add `uninstall.sh`. |

## Phase 1 — restore function (branch `feat/modernize-p1-incremental-indexing`)
TDD per step; one commit per step (`feat:`/`fix:`/`test:`).
1. Fixtures: `tests/fixtures/*.jsonl` covering all record kinds listed above + sidechain + partial last line + no-uuid record; `db` fixture monkeypatching `myak.config.DB_PATH`/`MEMORY_DIR`.
2. `myak/transcript.py`: `iter_records()`, `clean_text()`, `extract_segments()` → dicts with uuid, role, content, timestamp, is_sidechain, agent_id, cwd, end_offset. Strip tagged blocks (DOTALL); drop tool_result-only, isMeta, non user/assistant, `<local-command-stdout>` records.
3. `myak/db.py`: `migrate()` v0→v1 (columns in D1/D4 + `sessions.cwd`, `sessions.indexed_offset`), hash backfill of legacy rows, exact-dup removal via DELETE (FTS trigger) + FTS rebuild, unique indexes, backup, user_version. Tests: counts preserved, FTS still finds old rows, idempotent, journal_mode delete, backup written.
4. `indexer.index_session()` incremental (D2), `BEGIN IMMEDIATE` + busy_timeout 5s, counters from COUNT(*), offset stored after commit, sessions upsert. Legacy rows matched by hash get uuid filled.
5. Subagent discovery `discover_transcripts()`; used by indexer and backfill.
6. `query.search` selects `agent_type`, applies SUBAGENT_WEIGHT.
7. `format_hook` → hookSpecificOutput; skip prompts starting with `/`; non-string prompt guard.
8. `myak/cli.py`, `myak/hooks.py`, launcher template `templates/myak-hook.sh`, `templates/hooks.json` (Stop async+timeout+statusMessage, SessionEnd timeout, UserPromptSubmit). Subprocess tests incl. `env -i` PATH test.
9. `myak/settings_migrate.py` (D8) with fixture tests (other hooks byte-equal after parse, idempotent, .bak).
10. backfill uses discovery, `--rebuild-fts`; idempotent run test.
Acceptance: segment count grows across Stop runs; zero dupes on rerun; real-DB rehearsal on a copy preserves 385 sessions / 3039 segments (dups reported); hooks work with empty PATH; changed modules ≥80% coverage.
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
- Syncthing drift (High): migrate on one machine, wait for sync; nullable columns; `.stignore` for backups and `memory.vec.db`.
- Plugin SessionEnd 1.5s cap (Medium): detach.
- Prompt injection via injected memories (Medium): frame as untrusted, strip control chars, cap.
- settings.json edit (Medium): dry-run, backup, exact match.
