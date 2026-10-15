# Search quality evaluation

`scripts/eval_search.py` replays a fixed set of queries against your real memory database and reports how well each search path finds the segment you meant. It is a developer tool: it is not installed with the package and never runs in CI.

## What it measures

Each query names one target segment (and its session). The harness runs it through the three production paths, calling the same functions they use:

| Pipeline | Path | Notes |
| --- | --- | --- |
| `hook` | `UserPromptSubmit` auto-injection | FTS only, hook filters, one result per session, capped at `HOOK_MAX_RESULTS`. Hit@5 here is effectively Hit@3: that is all the hook injects. |
| `fts` | `myak query --no-hybrid` | FTS plus the standard threshold filters. |
| `hybrid` | MCP `myak_search`, `myak query --hybrid` | FTS plus vector RRF. Falls back to FTS when vectors are unavailable, exactly like production; the report says so. |

Per pipeline, overall and per query type (`lexical` / `semantic`):

- segment Hit@1, Hit@5 and MRR@k (k is `--k`, default 10)
- session Hit@5: any result from the target session counts. Results only carry an 8-character session prefix, so prefixes are compared.
- empty-result rate
- latency p50/p95 per query, measured warm. The first query is run once beforehand; that cold time (model load, caches) is reported separately.
- vector coverage (embedded rows vs segments) when `hybrid` runs

Time decay uses a frozen clock (`frozen_now`, default `2026-10-15T00:00:00Z`) so results do not drift as your history ages. Production code is not changed for this; the harness patches the clock for the run only. The databases are opened read-only (`mode=ro`, `query_only`), including the attached vector index.

`fts` and `hybrid` use `limit = --k`. The hook keeps its production limit.

## Build the query set

The eval directory defaults to `~/.claude/myak-state/eval/`. Put `queries.jsonl` there, one JSON object per line:

```json
{"qid": "q001", "segment_id": 1234, "session_id": "<full session uuid>", "type": "lexical", "query": "<what you would type>"}
```

- `segment_id` and `session_id` come from the database. Browse it read-only: `sqlite3 -readonly ~/.claude/memory/memory.db`.
- `lexical`: a few keywords you would plausibly remember from the segment.
- `semantic`: the same intent in different words, with little word overlap. This is what vector search is for.
- Do not paste the segment text as the query; that tests nothing.
- Lines with `"skip": true` and blank lines are ignored. `qid` must be unique.
- Aim for 50+ queries, split across both types. With fewer, the metrics are noisy.

Optional `meta.json`: `{"frozen_now": "2026-10-15T00:00:00Z"}`. Change it only when you start a new eval set.

Segment ids survive re-indexing only while the rows do. Queries whose segment is gone, or now belongs to another session, are excluded and counted as `stale`. Rebuild those queries if the number grows. Queries whose session prefix is shared by several sessions (for example every `desktop-` session) are counted as `ambiguous`; their session hits can be inflated.

## Privacy

The query set is derived from your private sessions. It must never be committed or shared. Keep it in the machine-local state dir (`~/.claude/myak-state/`, not synced) or another path outside the repository. Reports in `runs/` contain only query ids, ranks and timings, no query or segment text, but treat them as private too. Directories are created `0700`, files `0600`.

## Run

```bash
uv run --extra vec python scripts/eval_search.py                  # all pipelines, k=10
uv run --extra vec python scripts/eval_search.py --pipelines fts  # one pipeline
```

`hybrid` needs the vec extra and a downloaded model (`myak embed`). Without them the run still completes, with a warning, and `hybrid` equals `fts`.

Each run writes `<eval-dir>/runs/<UTC timestamp>-<git sha>.json` and prints a compact table.

## Before a release

1. On the last release (or `main`), record a baseline: `scripts/eval_search.py --save-baseline`. This writes `<eval-dir>/baseline.json`.
2. On the release candidate, run the harness again. It compares against `baseline.json` (or `--baseline PATH`) and prints the deltas.
3. Exit status `1` means a pipeline's session Hit@5 or MRR@k dropped by more than `--threshold` (absolute, default `0.03`). Investigate before releasing. `2` means bad input (missing files, stale-only query set, outdated schema).
4. If a drop is an intended trade-off, run again with `--save-baseline` to accept it. Old numbers stay in `runs/`.

A baseline is only comparable when the query set, `k` and `frozen_now` match; the harness prints a note when they differ.
