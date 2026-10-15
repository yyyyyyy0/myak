# Search quality evaluation

`scripts/eval_search.py` replays a fixed set of queries against your real memory database and reports how well each search path finds the segment you meant. It is a developer tool: it is not installed with the package and never runs in CI.

## What it measures

Most queries name one target segment (and its session); `negative` queries name none (see below). The harness runs each query through the three production paths, calling the same functions they use:

| Pipeline | Path | Notes |
| --- | --- | --- |
| `hook` | `UserPromptSubmit` auto-injection | FTS only, hook filters, one result per session, capped at `HOOK_MAX_RESULTS`. Hit@5 here is effectively Hit@3: that is all the hook injects. |
| `fts` | `myak query --no-hybrid` | FTS plus the standard threshold filters. |
| `hybrid` | MCP `myak_search`, `myak query --hybrid` | FTS plus vector RRF. Falls back to FTS when vectors are unavailable, exactly like production; the report says so. |

Per pipeline, overall and per targeted query type (`lexical` / `semantic` / `recency`):

- segment Hit@1, Hit@5 and MRR@k (k is `--k`, default `MAX_RESULTS`, the production default, currently 5)
- session Hit@5: any result from the target session counts. Results only carry an 8-character session prefix, so prefixes are compared.
- empty-result rate and mean number of results (`mean`)
- latency p50/p95 per query, measured warm. The first query is run once beforehand; that cold time (model load, caches) is reported separately.
- vector coverage (embedded rows vs segments) when `hybrid` runs
- recency pairs (`recency` queries with `supersedes`): `newer_first_rate`, the share of pairs where the newer target is returned above the segment it supersedes (or that one is not returned), and `older_first_rate`, the reverse. Both absent counts for neither.

`all` covers the targeted queries only. `negative` queries are reported in their own row:

- `mean`: the mean number of results. For `hook` this is what gets injected into a prompt that needs no past memory; for `hybrid` it is what `myak_search` returns.
- `fill_rate` in the report: `mean` divided by the pipeline's depth (3 for `hook`, `--k` otherwise), so pipelines compare on a 0 to 1 scale. Lower is better.
- `empty`: the share of negative queries that got nothing. Higher is better here.
- `results_hist` in the report: how many negative queries got 0, 1, 2, ... results.

Time decay uses a frozen clock (`frozen_now`, default `2026-10-15T00:00:00Z`), so the passage of real time does not change the numbers. Production code is not changed for this; the harness patches the clock for the run only. Production's `time_decay_score` gives a timestamp later than "now" a multiplier above 1 that grows exponentially (30 days later: 2.0, 60 days: 4.0). Without a guard, every segment indexed after `frozen_now` would be boosted more and more. During the eval the harness therefore wraps `time_decay_score` so a timestamp later than `frozen_now` is clamped to `frozen_now` (decay 1.0) before the original function is called. The databases are opened read-only (`mode=ro`, `query_only`), including the attached vector index.

The clock does not freeze the corpus. **Results do drift as the corpus grows**: new sessions compete with your target segments in the ranking, `myak embed` changes vector coverage, and cleanup or re-indexing changes the rows. The harness records the newest segment timestamp in the report (`clock`) and prints a warning when it is more than 1 day after `frozen_now`. The warning means the corpus has moved since the eval set was frozen; it is not an error.

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
- Aim for 50+ queries, split across both types. With fewer, the metrics are noisy. A query type is gated on its own only with 30+ queries of that type.

Known-item queries alone favour weak time decay when their targets are old: nothing in them is hurt by ranking an old segment first. Two more types cover that:

`recency`: the target is a recent segment (for example from the last 30 days before `frozen_now`), phrased like a `lexical` query. When the same topic also has an older segment that the target replaces (a later decision, a fixed version of the same procedure), name it in `supersedes`; the query then also counts as a pair:

```json
{"qid": "r001", "segment_id": 5678, "session_id": "<full session uuid>", "type": "recency", "query": "<what you would type>", "supersedes": {"segment_id": 1234, "session_id": "<full session uuid>"}}
```

`supersedes` is only allowed on `recency` queries and must name a different, older segment: a pair whose target is not newer than the superseded segment (by segment timestamp) is an input error (exit `2`), because it would count the wrong way. When the superseded segment is gone or moved, the whole query counts as `stale`.

`negative`: a prompt that needs no past memory (for example "run the tests" or "fix the lint errors"), written the way you would type it in a session. It has no target, so `segment_id`, `session_id` and `supersedes` must be absent:

```json
{"qid": "n001", "type": "negative", "query": "<a generic prompt>"}
```

Do not write prompts starting with `/`: the hook ignores them, so they measure nothing. Ranking metrics do not apply to negative queries; they are measured by how much is returned (see above).

Optional `meta.json`: `{"frozen_now": "2026-10-15T00:00:00Z"}`. Change it only when you start a new eval set: a different `frozen_now` makes every older baseline incomparable.

Segment ids survive re-indexing only while the rows do. Queries whose segment is gone, or now belongs to another session, are excluded and counted as `stale`. Rebuild those queries if the number grows. Queries whose session prefix is shared by several sessions (for example every `desktop-` session) are counted as `ambiguous`; their session hits can be inflated.

## Privacy

The query set is derived from your private sessions. It must never be committed or shared. Keep it in the machine-local state dir (`~/.claude/myak-state/`, not synced) or another path outside the repository. Reports in `runs/` contain only query ids, ranks and timings, no query or segment text, but treat them as private too. Directories are created `0700`, files `0600`.

## Run

```bash
uv run --extra vec python scripts/eval_search.py                  # all pipelines, k=5
uv run --extra vec python scripts/eval_search.py --pipelines fts  # one pipeline
```

`hybrid` needs the vec extra and a downloaded model (`myak embed`). Without them the run still completes, with a warning, and `hybrid` equals `fts`.

Each run writes `<eval-dir>/runs/<UTC timestamp>-<git sha>.json` and prints a compact table.

## Exit status

| Code | Meaning |
| --- | --- |
| `0` | OK. Also a first run with no baseline: the output says nothing was gated. |
| `1` | Regression: a gate metric dropped by more than `--threshold` against the baseline. |
| `2` | Input error: missing or invalid `queries.jsonl` / `meta.json` / baseline file, no database, outdated schema, or no evaluable queries. |
| `3` | Baseline incomparable or incomplete. Nothing was gated; the output lists the reasons and how to re-save. |
| `4` | Unexpected error. Only the exception type and raise site are printed, never query text. |

## The baseline gate

The gate compares a run with `<eval-dir>/baseline.json` (or `--baseline PATH`). It fails closed: when the numbers cannot be compared, it exits `3` instead of passing silently.

What is gated: for every pipeline in the run, session Hit@5 and MRR@k must not drop by more than `--threshold` (absolute, default `0.03`; a drop of exactly the threshold is allowed). The same two metrics are gated per query type (`lexical`, `semantic`, `recency`) when that slice has at least 30 queries in both runs; smaller slices are shown as `n<30` and not gated. The `negative` slice is gated under the same rules on two metrics where the bad direction differs: its empty rate must not drop by more than `--threshold` (more generic prompts get something back), and its `fill_rate` must not rise by more than `--threshold` (more results per generic prompt). Note the signs in the comparison: for `negative`, a positive `d_empty` is good. The recency pair rate is shown as `d_newer` but never gated; pair sets are usually small.

A baseline is comparable only when all of these match, otherwise the result is exit `3` and nothing is gated:

- report schema (a baseline written by an older harness version is incomparable; re-save it),
- `k`,
- `frozen_now`,
- the evaluated query set: the digest covers the usable queries, i.e. those actually scored. Editing, adding or removing a query, or a query turning stale (segment gone or moved), changes it. A change in the stale count alone, with the same evaluated set, is only a note,
- every pipeline in the run is present in the baseline. A baseline without `fts` cannot gate a run that includes `fts`.

### Saving a baseline

`--save-baseline` writes `<eval-dir>/baseline.json` after the comparison, even when the gate failed or was incomparable (the output says so). It merges per pipeline: if the existing baseline is comparable with the run (same schema, `k`, `frozen_now`, evaluated set), pipelines the run did not cover are kept and only the ones it covered are replaced, so `--pipelines hook --save-baseline` no longer drops `fts` and `hybrid`. Each pipeline's `sources` entry records when and at which commit it was measured. If the existing baseline is not comparable, it is replaced and the output lists the pipelines that were dropped. A baseline that does not cover every pipeline makes later full runs exit `3` until the missing ones are saved.

### When to re-save the baseline

Because results drift with the corpus, a baseline is only a fair reference for the corpus it was measured on. Re-save it:

- on the old code (the last release or `main`) right before you check a release candidate, against the current database. The tidy sequence is: check out the old code, run `--save-baseline`, check out the candidate, run again, compare, all in one sitting;
- after the corpus changed materially since it was saved: many new sessions indexed, `myak embed` run, a cleanup or re-index, or when the clock warning above appears;
- after changing the eval set, `k` or `frozen_now` (the harness tells you with exit `3`);
- to accept an intended trade-off after a regression (exit `1`). Old numbers stay in `runs/`.

Do not re-save to silence a regression you have not understood.

## Before a release

1. On the last release (or `main`), record a baseline: `scripts/eval_search.py --save-baseline`. This writes `<eval-dir>/baseline.json`.
2. On the release candidate, run the harness again. It compares against `baseline.json` (or `--baseline PATH`) and prints the deltas.
3. Exit `1` means a gate metric dropped by more than `--threshold`. Investigate before releasing. Exit `3` means the comparison was not possible; fix the cause (see above) and re-save. Exit `2` is bad input, `4` an unexpected failure.
4. If a drop is an intended trade-off, run again with `--save-baseline` to accept it.
