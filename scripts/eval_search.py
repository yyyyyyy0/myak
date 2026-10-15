"""Search-quality evaluation harness for myak (developer tool; not part of the package).

Replays a private query set against the real memory DB through the three production search
paths and reports ranking quality and latency. It never writes to the databases.

    uv run --extra vec python scripts/eval_search.py [--eval-dir DIR] [--k 5]
        [--pipelines hook,fts,hybrid] [--baseline PATH] [--save-baseline] [--threshold 0.03]

Pipelines (each calls the production functions; ranking is not re-implemented here):
    hook    UserPromptSubmit auto-injection: FTS + hook filters, capped at HOOK_MAX_RESULTS
    fts     `myak query --no-hybrid`: FTS + filter_hybrid
    hybrid  MCP `myak_search` / `myak query --hybrid`: FTS + vector RRF + filter_hybrid

Exit status:
    0  ok (including a first run with no baseline: nothing was gated)
    1  regression against the baseline beyond --threshold
    2  input error (missing/invalid queries, DB, baseline file; nothing evaluable)
    3  baseline incomparable or incomplete: nothing was gated (the message says why and
       how to re-save)
    4  unexpected error (the exception type is printed, never query text)
See docs/EVAL.md.
"""

import argparse
import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import traceback
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from myak import config, db, query, vec
from myak import search as search_mod

REPO_ROOT = Path(__file__).resolve().parent.parent

REPORT_SCHEMA = 2  # 2: usable-set digest, clock block, clamped decay, per-pipeline sources
DEFAULT_FROZEN_NOW = "2026-10-15T00:00:00Z"
DEFAULT_K = query.MAX_RESULTS  # production default depth
DEFAULT_THRESHOLD = 0.03
PIPELINE_NAMES = ("hook", "fts", "hybrid")
QUERY_TYPES = ("lexical", "semantic")
MIN_QUERY_CHARS = 3  # same floor as the CLI / MCP entry points
SESSION_PREFIX_LEN = 8  # search results only carry session_id[:8]
SESSION_HIT_CUTOFF = 5
SQL_CHUNK = 500
MIN_GATE_N = 30  # a per-type slice is gated only when it has at least this many queries
CLOCK_DRIFT_DAYS = 1.0  # warn when the newest segment is this much later than frozen_now

GATE_METRICS = ("session_hit_at_5", "mrr_at_k")
COMPARE_METRICS = ("hit_at_1", "hit_at_5", "mrr_at_k", "session_hit_at_5", "empty_rate")

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_INPUT_ERROR = 2
EXIT_INCOMPARABLE = 3
EXIT_UNEXPECTED = 4


class EvalInputError(Exception):
    """Bad eval inputs or environment. Messages never echo query text."""


@dataclass(frozen=True)
class EvalQuery:
    qid: str | int
    segment_id: int
    session_id: str
    type: str
    text: str


@dataclass(frozen=True)
class Outcome:
    qid: str | int
    type: str
    segment_rank: int | None
    session_rank: int | None
    n_results: int
    latency_ms: float
    vec_assisted: bool = False


# --------------------------------------------------------------------------- input


def _require(cond, where, what):
    if not cond:
        raise EvalInputError(f"{where}: {what}")


def _parse_query_line(raw, where):
    """One queries.jsonl line -> EvalQuery, or None for lines marked "skip": true."""
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        raise EvalInputError(f"{where}: not valid JSON") from None
    _require(isinstance(obj, dict), where, "expected a JSON object")
    if obj.get("skip") is True:
        return None
    qid, seg = obj.get("qid"), obj.get("segment_id")
    sid, qtype, text = obj.get("session_id"), obj.get("type"), obj.get("query")
    _require(
        isinstance(qid, (str, int)) and not isinstance(qid, bool) and qid != "",
        where, "qid must be a non-empty string or integer",
    )
    _require(
        isinstance(seg, int) and not isinstance(seg, bool),
        where, "segment_id must be an integer",
    )
    _require(isinstance(sid, str) and sid != "", where, "session_id must be a non-empty string")
    _require(qtype in QUERY_TYPES, where, f"type must be one of {', '.join(QUERY_TYPES)}")
    _require(
        isinstance(text, str) and len(text.strip()) >= MIN_QUERY_CHARS,
        where, f"query must be a string of at least {MIN_QUERY_CHARS} characters",
    )
    return EvalQuery(qid=qid, segment_id=seg, session_id=sid, type=qtype, text=text.strip())


def load_queries(path):
    """Read queries.jsonl (blank lines and "skip": true lines ignored). Duplicate qids fail."""
    queries, seen = [], set()
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            if not raw.strip():
                continue
            where = f"{Path(path).name}:{lineno}"
            parsed = _parse_query_line(raw, where)
            if parsed is None:
                continue
            _require(parsed.qid not in seen, where, "duplicate qid")
            seen.add(parsed.qid)
            queries.append(parsed)
    return queries


def parse_instant(value):
    """ISO 8601 -> aware UTC datetime (naive input is taken as UTC)."""
    if not isinstance(value, str):
        raise EvalInputError("frozen_now must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise EvalInputError("frozen_now is not a valid ISO 8601 timestamp") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def load_frozen_now(eval_dir):
    path = Path(eval_dir) / "meta.json"
    raw = DEFAULT_FROZEN_NOW
    if path.is_file():
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise EvalInputError("meta.json: unreadable or not valid JSON") from None
        _require(isinstance(meta, dict), "meta.json", "expected a JSON object")
        raw = meta.get("frozen_now", raw)
    return parse_instant(raw)


def queries_digest(queries):
    """Order-independent fingerprint of a query list.

    Reports store the digest of the usable (actually scored) queries, so any change to what was
    evaluated makes a baseline incomparable.
    """
    lines = sorted(
        f"{q.qid}\t{q.segment_id}\t{q.session_id}\t{q.type}\t{q.text}" for q in queries
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ environment


def _frozen_datetime_class(instant):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is None else instant.astimezone(tz)

    return FrozenDatetime


def _clamp_timestamp(timestamp_str, instant):
    """`timestamp_str`, or `instant` (ISO) when it is later than `instant`.

    Anything the production parser would reject (non-str, naive, malformed) is returned
    unchanged so the original function handles it exactly as in production.
    """
    if not isinstance(timestamp_str, str):
        return timestamp_str
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        later = ts.tzinfo is not None and ts > instant
    except ValueError:
        return timestamp_str
    return instant.isoformat() if later else timestamp_str


@contextlib.contextmanager
def frozen_time(instant):
    """Make myak.query's time decay see `instant` as "now" (production code is untouched).

    time_decay_score gives a timestamp later than "now" a multiplier above 1 that grows
    exponentially, so rows indexed after `instant` would be boosted more and more. The wrapper
    clamps such timestamps to `instant` (decay 1.0) before calling the original function.
    """
    original = query.time_decay_score

    def clamped(timestamp_str, base_score):
        return original(_clamp_timestamp(timestamp_str, instant), base_score)

    with (
        mock.patch.object(query, "datetime", _frozen_datetime_class(instant)),
        mock.patch.object(query, "time_decay_score", clamped),
    ):
        yield


def open_readonly(path):
    """Read-only connection (mode=ro + query_only). ATTACHed DBs inherit the read-only flag."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
    conn.execute("PRAGMA query_only=ON")
    return conn


@contextlib.contextmanager
def eval_environment(instant):
    """Frozen clock + read-only DB connections for everything the search paths open."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(frozen_time(instant))
        stack.enter_context(
            mock.patch.object(search_mod, "get_connection", lambda: open_readonly(config.DB_PATH))
        )
        yield


def preflight_db():
    db_path = Path(config.DB_PATH)
    if not db_path.is_file():
        raise EvalInputError("memory database not found")
    conn = open_readonly(db_path)
    try:
        if db.schema_version(conn) != db.CURRENT_VERSION:
            raise EvalInputError("database schema is not current; run `myak migrate`")
    finally:
        conn.close()
    return db_path


def validate_queries(conn, queries):
    """Split into (usable, stale, ambiguous_count).

    stale: the segment is gone or now belongs to another session (ids shifted after a
    re-index / cleanup), so the query can no longer be answered and is excluded.
    ambiguous: the target's 8-char session prefix is shared by several sessions, which can
    inflate session hits (e.g. every `desktop-` session).
    """
    ids = sorted({q.segment_id for q in queries})
    owner = {}
    for start in range(0, len(ids), SQL_CHUNK):
        chunk = ids[start:start + SQL_CHUNK]
        marks = ",".join("?" * len(chunk))
        owner.update(
            conn.execute(f"SELECT id, session_id FROM segments WHERE id IN ({marks})", chunk)
        )
    usable = [q for q in queries if owner.get(q.segment_id) == q.session_id]
    stale = [q for q in queries if owner.get(q.segment_id) != q.session_id]
    prefixes = Counter(
        row[0][:SESSION_PREFIX_LEN] for row in conn.execute("SELECT session_id FROM sessions")
    )
    ambiguous = sum(1 for q in usable if prefixes[q.session_id[:SESSION_PREFIX_LEN]] > 1)
    return usable, stale, ambiguous


def _parse_timestamp(value):
    """Segment timestamp -> aware UTC datetime, or None when it cannot be parsed."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def clock_info(conn, frozen_now):
    """How far the corpus has moved past the frozen clock (MAX(segments.timestamp))."""
    newest = conn.execute("SELECT MAX(timestamp) FROM segments").fetchone()[0]
    parsed = _parse_timestamp(newest)
    days = None if parsed is None else (parsed - frozen_now).total_seconds() / 86400
    return {
        "newest_segment_at": newest if isinstance(newest, str) else None,
        "days_after_frozen_now": None if days is None else round(days, 3),
        "drift_warning": days is not None and days > CLOCK_DRIFT_DAYS,
    }


def clock_warnings(report):
    clock = report.get("clock") or {}
    if not clock.get("drift_warning"):
        return []
    return [
        f"newest segment is {clock['days_after_frozen_now']:.1f} days after frozen_now "
        f"({report['frozen_now']}); newer rows are clamped to frozen_now, but results still "
        "drift as the corpus grows. Re-save the baseline on the old code against the current "
        "corpus before comparing a candidate (see docs/EVAL.md)"
    ]


def vector_status():
    """Whether the hybrid path can really use vectors, plus embedded-vs-segments coverage."""
    status = {
        "available": False, "reason": None, "model": None,
        "embedded": None, "segments": None, "coverage": None,
    }
    conn = open_readonly(config.DB_PATH)
    try:
        status["segments"] = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
        try:
            embedder = vec.default_embedder()
            vec.open_vec(conn, embedder)
            status["embedded"] = conn.execute("SELECT COUNT(*) FROM vec.segments_vec").fetchone()[0]
            status["model"] = embedder.model_id
            status["available"] = True
        except (vec.VecUnavailable, sqlite3.Error) as e:
            status["reason"] = str(e)
    finally:
        conn.close()
    if status["embedded"] is not None and status["segments"]:
        status["coverage"] = round(status["embedded"] / status["segments"], 6)
    return status


# -------------------------------------------------------------------- pipelines


def hook_pipeline(text, k):
    """UserPromptSubmit: same steps as query.run_hook -> _hook_output, minus formatting."""
    raw = json.dumps({"hook_event_name": query.HOOK_EVENT_NAME, "prompt": text}, ensure_ascii=False)
    hook_text = query.hook_query_from_stdin(raw)
    if not hook_text or len(hook_text) < MIN_QUERY_CHARS:
        return []
    results = query.search(hook_text, query.MAX_RESULTS)
    results = query.filter_results(results, query.tokenize_query_terms(hook_text), hook_mode=True)
    return results[: query.HOOK_MAX_RESULTS]  # what format_hook actually injects


def fts_pipeline(text, k):
    """`myak query --no-hybrid`."""
    return query.filter_hybrid(query.search(text, k, use_vec=False), text)


def hybrid_pipeline(text, k):
    """MCP myak_search (including its FTS fallback when the hybrid call raises)."""
    try:
        results = query.search(text, k, use_vec=True)
    except Exception:  # noqa: BLE001 - mirrors mcp_server.myak_search
        results = query.search(text, k, use_vec=False)
    return query.filter_hybrid(results, text)


PIPELINES = {"hook": hook_pipeline, "fts": fts_pipeline, "hybrid": hybrid_pipeline}


def pipeline_depth(name, k):
    return query.HOOK_MAX_RESULTS if name == "hook" else k


# ---------------------------------------------------------------------- metrics


def segment_rank(results, segment_id, k):
    """1-based position of the target segment within the top k, else None (first occurrence)."""
    for pos, result in enumerate(results[:k], start=1):
        if result.get("segment_id") == segment_id:
            return pos
    return None


def session_rank(results, session_id, k):
    """1-based position of the first result from the target session (8-char prefix compare)."""
    prefix = session_id[:SESSION_PREFIX_LEN]
    for pos, result in enumerate(results[:k], start=1):
        if result.get("session_id") == prefix:
            return pos
    return None


def score_query(q, results, k, latency_ms):
    return Outcome(
        qid=q.qid,
        type=q.type,
        segment_rank=segment_rank(results, q.segment_id, k),
        session_rank=session_rank(results, q.session_id, k),
        n_results=len(results),
        latency_ms=latency_ms,
        vec_assisted=any(r.get("source") in ("vec", "both") for r in results),
    )


def percentile(values, pct):
    """Linear-interpolated percentile (0-100); None for no data."""
    if not values:
        return None
    ordered = sorted(values)
    pos = (len(ordered) - 1) * pct / 100
    low, high = math.floor(pos), math.ceil(pos)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _rate(count, total):
    return round(count / total, 6)


def _hit(rank, cutoff):
    return rank is not None and rank <= cutoff


def summarize(outcomes):
    """Aggregate metrics for a list of Outcome. Empty input yields n=0 and None metrics."""
    n = len(outcomes)
    if n == 0:
        return {
            "n": 0, "hit_at_1": None, "hit_at_5": None, "mrr_at_k": None,
            "session_hit_at_5": None, "empty_rate": None,
            "latency_p50_ms": None, "latency_p95_ms": None,
        }
    latencies = [o.latency_ms for o in outcomes]
    return {
        "n": n,
        "hit_at_1": _rate(sum(_hit(o.segment_rank, 1) for o in outcomes), n),
        "hit_at_5": _rate(sum(_hit(o.segment_rank, 5) for o in outcomes), n),
        "mrr_at_k": round(sum(1 / o.segment_rank for o in outcomes if o.segment_rank) / n, 6),
        "session_hit_at_5": _rate(
            sum(_hit(o.session_rank, SESSION_HIT_CUTOFF) for o in outcomes), n
        ),
        "empty_rate": _rate(sum(o.n_results == 0 for o in outcomes), n),
        "latency_p50_ms": round(percentile(latencies, 50), 3),
        "latency_p95_ms": round(percentile(latencies, 95), 3),
    }


def summarize_by_type(outcomes):
    out = {}
    for qtype in QUERY_TYPES:
        subset = [o for o in outcomes if o.type == qtype]
        if subset:
            out[qtype] = summarize(subset)
    return out


def _elapsed_ms(started):
    return (time.perf_counter() - started) * 1000


def evaluate_pipeline(name, queries, k):
    """Run every query through one pipeline.

    The first query is run once up front and its time is reported as cold_ms (model load,
    page cache). The timed pass then covers all queries, so p50/p95 are warm numbers.
    """
    runner = PIPELINES[name]
    cold_ms = None
    if queries:
        started = time.perf_counter()
        runner(queries[0].text, k)
        cold_ms = round(_elapsed_ms(started), 3)
    outcomes = []
    for q in queries:
        started = time.perf_counter()
        results = runner(q.text, k)
        outcomes.append(score_query(q, results, k, _elapsed_ms(started)))
    entry = {
        "depth": pipeline_depth(name, k),
        "cold_ms": cold_ms,
        "overall": summarize(outcomes),
        "by_type": summarize_by_type(outcomes),
        "per_query": [
            {
                "qid": o.qid, "type": o.type, "segment_rank": o.segment_rank,
                "session_rank": o.session_rank, "n_results": o.n_results,
                "latency_ms": round(o.latency_ms, 3),
            }
            for o in outcomes
        ],
    }
    if name == "hybrid" and outcomes:
        entry["vec_assisted_rate"] = _rate(sum(o.vec_assisted for o in outcomes), len(outcomes))
    return entry


# ----------------------------------------------------------------------- report


def git_info():
    def run(*args):
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()

    try:
        sha = run("rev-parse", "--short", "HEAD")
        dirty = bool(run("status", "--porcelain", "--untracked-files=no"))
    except (OSError, subprocess.SubprocessError):
        return {"sha": "nogit", "dirty": None}
    if not re.fullmatch(r"[0-9a-f]{4,40}", sha):
        return {"sha": "nogit", "dirty": None}
    return {"sha": sha, "dirty": dirty}


def build_report(
    *, k, frozen_now, loaded, usable, stale, ambiguous, pipelines, results, vector, clock
):
    return {
        "schema": REPORT_SCHEMA,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git": git_info(),
        "k": k,
        "frozen_now": frozen_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "clock": clock,
        "queries": {
            "total": len(loaded),
            "evaluated": len(usable),
            "stale": len(stale),
            "ambiguous_session_prefix": ambiguous,
            "by_type": {t: sum(q.type == t for q in usable) for t in QUERY_TYPES},
            "usable_sha256": queries_digest(usable),
        },
        "vector": vector if "hybrid" in pipelines else None,
        "pipelines": {name: results[name] for name in pipelines},
    }


def ensure_private_dir(path):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def write_json(path, payload):
    config.atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", 0o600)
    return Path(path)


def write_run_report(eval_dir, report):
    runs = ensure_private_dir(Path(eval_dir) / "runs")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = f"{stamp}-{report['git']['sha']}"
    path, n = runs / f"{base}.json", 1
    while path.exists():
        n += 1
        path = runs / f"{base}-{n}.json"
    return write_json(path, report)


def baseline_path(eval_dir):
    return Path(eval_dir) / "baseline.json"


def load_baseline(explicit, eval_dir):
    path = Path(explicit) if explicit else baseline_path(eval_dir)
    if not path.is_file():
        if explicit:
            raise EvalInputError(f"baseline not found: {path}")
        return None
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise EvalInputError(f"{path.name}: unreadable or not valid JSON") from None
    _require(
        isinstance(baseline, dict) and isinstance(baseline.get("pipelines"), dict),
        path.name, "not an eval report",
    )
    return baseline


def _usable_entry(entry):
    """A pipeline entry the comparison can read (a dict with an `overall` dict)."""
    return isinstance(entry, dict) and isinstance(entry.get("overall"), dict)


def baseline_problems(current, baseline):
    """Why `baseline` cannot be compared with `current` at all (empty list: comparable).

    Run-wide checks only; which pipelines the baseline covers is checked by compare_reports.
    A differing schema is reported alone: the other fields may not mean the same thing.
    """
    if current.get("schema") != baseline.get("schema"):
        return [
            f"report schema differs (run {current.get('schema')}, baseline "
            f"{baseline.get('schema')}): the baseline was written by a different harness version"
        ]
    problems = []
    if current.get("k") != baseline.get("k"):
        problems.append(f"k differs (run {current.get('k')}, baseline {baseline.get('k')})")
    if current.get("frozen_now") != baseline.get("frozen_now"):
        problems.append(
            f"frozen_now differs (run {current.get('frozen_now')}, "
            f"baseline {baseline.get('frozen_now')})"
        )
    cur_q, base_q = current.get("queries") or {}, baseline.get("queries") or {}
    cur_digest, base_digest = cur_q.get("usable_sha256"), base_q.get("usable_sha256")
    if cur_digest is None or cur_digest != base_digest:
        problems.append(
            "evaluated query set differs "
            f"(run {cur_q.get('evaluated')} queries, baseline {base_q.get('evaluated')})"
        )
    return problems


def _gate_slices(cur_pipeline, base_pipeline):
    """(slice, current metrics, baseline metrics) for the overall numbers and each query type."""
    slices = [("all", cur_pipeline["overall"], base_pipeline["overall"])]
    cur_types = cur_pipeline.get("by_type") or {}
    base_types = base_pipeline.get("by_type") or {}
    for qtype in QUERY_TYPES:
        if isinstance(cur_types.get(qtype), dict) and isinstance(base_types.get(qtype), dict):
            slices.append((qtype, cur_types[qtype], base_types[qtype]))
    return slices


def compare_reports(current, baseline, threshold):
    """Compare a run with a baseline; a gate metric dropping by more than `threshold` fails.

    Fail closed: if the baseline is not comparable (different schema, k, frozen_now or evaluated
    query set) or does not cover every pipeline of the run, `comparable` is False and nothing is
    gated. Otherwise every pipeline is gated on its overall numbers, and on each query type whose
    slice has at least MIN_GATE_N queries in both runs.

    Returns {comparable, problems, deltas: {pipeline: {slice: {metric: delta}}},
             regressions: [(pipeline, slice, metric, delta)], ungated: [(pipeline, slice)], notes}.
    """
    result = {
        "comparable": False, "problems": baseline_problems(current, baseline),
        "deltas": {}, "regressions": [], "ungated": [], "notes": [],
    }
    base_pipelines = baseline.get("pipelines") or {}
    for name in current["pipelines"]:
        if not _usable_entry(base_pipelines.get(name)):
            covered = ", ".join(n for n in PIPELINE_NAMES if _usable_entry(base_pipelines.get(n)))
            result["problems"].append(
                f"{name} is not in the baseline (baseline covers: {covered or 'nothing'})"
            )
    if result["problems"]:
        return result
    result["comparable"] = True
    cur_stale = (current.get("queries") or {}).get("stale")
    base_stale = (baseline.get("queries") or {}).get("stale")
    if cur_stale != base_stale:
        result["notes"].append(
            f"stale query count differs (run {cur_stale}, baseline {base_stale}); "
            "the evaluated set is identical"
        )
    for name, pdata in current["pipelines"].items():
        result["deltas"][name] = {}
        for label, cur, base in _gate_slices(pdata, base_pipelines[name]):
            deltas = {
                m: round(cur[m] - base[m], 9)
                for m in COMPARE_METRICS
                if cur.get(m) is not None and base.get(m) is not None
            }
            result["deltas"][name][label] = deltas
            if label != "all" and min(cur.get("n") or 0, base.get("n") or 0) < MIN_GATE_N:
                result["ungated"].append((name, label))
                continue
            result["regressions"] += [
                (name, label, m, deltas[m])
                for m in GATE_METRICS
                if m in deltas and -deltas[m] > threshold
            ]
    return result


def _provenance(report):
    return {"created_at": report.get("created_at"), "git": report.get("git")}


def merge_baseline(existing, report):
    """The baseline to write for `report`, merged per pipeline into a compatible `existing`.

    A partial run must not silently shrink the baseline, so pipelines the run did not cover are
    carried over from `existing` (their `sources` entry keeps when and at which commit they were
    measured). If `existing` is not comparable with this run (different k, frozen_now, evaluated
    set or schema) it is replaced and its other pipelines are dropped.

    Returns (baseline, carried, dropped) with the carried / dropped pipeline names.
    """
    sources = {name: _provenance(report) for name in report["pipelines"]}
    fresh = {**report, "sources": sources}
    if existing is None:
        return fresh, [], []
    old = {n: e for n, e in existing["pipelines"].items() if _usable_entry(e)}
    others = [n for n in PIPELINE_NAMES if n in old and n not in report["pipelines"]]
    if baseline_problems(report, existing):
        return fresh, [], others
    old_sources = existing.get("sources") or {}
    for name in others:
        sources[name] = old_sources.get(name) or _provenance(existing)
    pipelines = {n: p for n, p in {**old, **report["pipelines"]}.items() if n in PIPELINE_NAMES}
    ordered = {n: pipelines[n] for n in PIPELINE_NAMES if n in pipelines}
    merged = {**report, "pipelines": ordered, "sources": {n: sources[n] for n in ordered}}
    if report["vector"] is None:
        merged["vector"] = existing.get("vector")
    return merged, others, []


def comparison_status(comparison):
    if not comparison["comparable"]:
        return EXIT_INCOMPARABLE
    return EXIT_REGRESSION if comparison["regressions"] else EXIT_OK


# ------------------------------------------------------------------- formatting


def _fmt(value, digits=3):
    return "-" if value is None else f"{value:.{digits}f}"


def _render(rows, left=2):
    """Fixed-width table; the first `left` columns are left-aligned, the rest right-aligned."""
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    lines = []
    for row in rows:
        cells = [
            cell.ljust(width) if i < left else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths, strict=True))
        ]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def format_table(report):
    k = report["k"]
    rows = [["pipeline", "slice", "n", "hit@1", "hit@5", f"mrr@{k}", "sess@5", "empty",
             "p50ms", "p95ms"]]
    for name, pdata in report["pipelines"].items():
        slices = [("all", pdata["overall"])] + list(pdata["by_type"].items())
        for i, (label, m) in enumerate(slices):
            rows.append([
                name if i == 0 else "", label, str(m["n"]),
                _fmt(m["hit_at_1"]), _fmt(m["hit_at_5"]), _fmt(m["mrr_at_k"]),
                _fmt(m["session_hit_at_5"]), _fmt(m["empty_rate"]),
                _fmt(m["latency_p50_ms"], 1), _fmt(m["latency_p95_ms"], 1),
            ])
    cold = ", ".join(f"{n} {_fmt(p['cold_ms'], 1)}" for n, p in report["pipelines"].items())
    return f"{_render(rows)}\ncold first query (ms): {cold}"


def format_summary(report):
    q = report["queries"]
    lines = [
        f"queries: {q['evaluated']} evaluated of {q['total']} "
        f"(stale {q['stale']}, ambiguous session prefix {q['ambiguous_session_prefix']}); "
        f"k={report['k']}, frozen_now={report['frozen_now']}, git={report['git']['sha']}"
        + (" (dirty)" if report["git"]["dirty"] else "")
    ]
    vector = report.get("vector")
    if vector:
        hybrid = report["pipelines"]["hybrid"]
        lines.append(
            f"vector: available={vector['available']} embedded={vector['embedded']}/"
            f"{vector['segments']} coverage={_fmt(vector['coverage'])} "
            f"vec-assisted queries={_fmt(hybrid.get('vec_assisted_rate'))}"
        )
    return "\n".join(lines)


RESAVE_HINT = (
    "to accept this run as the baseline, re-run with --save-baseline (all pipelines, or just "
    "the missing ones to merge into a compatible baseline)"
)


def format_comparison(comparison, threshold):
    if not comparison["comparable"]:
        lines = ["baseline not comparable; nothing was gated:"]
        lines += [f"  - {p}" for p in comparison["problems"]]
        lines.append(RESAVE_HINT)
        return "\n".join(lines)
    rows = [["pipeline", "slice", "d_hit@1", "d_hit@5", "d_mrr", "d_sess@5", "d_empty", "gate"]]
    failed = {(name, label) for name, label, _, _ in comparison["regressions"]}
    ungated = set(comparison["ungated"])
    for name, slices in comparison["deltas"].items():
        for i, (label, d) in enumerate(slices.items()):
            gate = "FAIL" if (name, label) in failed else (
                f"n<{MIN_GATE_N}" if (name, label) in ungated else "ok"
            )
            rows.append([
                name if i == 0 else "", label,
                *(f"{d[m]:+.3f}" if m in d else "-" for m in COMPARE_METRICS), gate,
            ])
    lines = [
        f"vs baseline (gate: session hit@5 / MRR drop > {threshold}; "
        f"query-type slices gated at n >= {MIN_GATE_N}):",
        _render(rows, left=2),
    ]
    lines += [f"note: {n}" for n in comparison["notes"]]
    return "\n".join(lines)


def hybrid_warnings(report):
    vector = report.get("vector")
    if not vector:
        return []
    if not vector["available"]:
        return [f"vector search unavailable: {vector['reason']}; hybrid fell back to FTS"]
    rate = report["pipelines"]["hybrid"].get("vec_assisted_rate")
    if rate == 0 and (vector["embedded"] or 0) > 0:
        return ["hybrid returned no vector-sourced results; it may have fallen back to FTS"]
    return []


# --------------------------------------------------------------------------- CLI


def _positive_int(value):
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def _non_negative_float(value):
    x = float(value)
    if x < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return x


def _pipeline_list(value):
    names = [n.strip() for n in value.split(",") if n.strip()]
    unknown = [n for n in names if n not in PIPELINES]
    if unknown or not names:
        raise argparse.ArgumentTypeError(f"choose from {','.join(PIPELINE_NAMES)}")
    return list(dict.fromkeys(names))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Evaluate myak search quality (read-only).")
    parser.add_argument("--eval-dir", type=Path, default=None,
                        help="dir with queries.jsonl [meta.json] (default: <state dir>/eval)")
    parser.add_argument("--k", type=_positive_int, default=DEFAULT_K,
                        help="result depth for fts/hybrid and the MRR cutoff "
                             f"(default: {DEFAULT_K}, the production default)")
    parser.add_argument("--pipelines", type=_pipeline_list, default=list(PIPELINE_NAMES),
                        help="comma list of hook,fts,hybrid (default: all)")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="baseline report to compare with (default: <eval-dir>/baseline.json)")
    parser.add_argument("--save-baseline", action="store_true",
                        help="also write this run to <eval-dir>/baseline.json (pipelines not in "
                             "this run are kept when the existing baseline is comparable)")
    parser.add_argument("--threshold", type=_non_negative_float, default=DEFAULT_THRESHOLD,
                        help="max allowed absolute drop in session hit@5 / MRR (default: 0.03)")
    return parser.parse_args(argv)


def run_evaluation(queries, k, pipelines, frozen_now):
    with eval_environment(frozen_now):
        return {name: evaluate_pipeline(name, queries, k) for name in pipelines}


def prepare_queries(eval_dir):
    queries_path = Path(eval_dir) / "queries.jsonl"
    if not queries_path.is_file():
        raise EvalInputError(f"queries.jsonl not found in {eval_dir}")
    loaded = load_queries(queries_path)
    _require(bool(loaded), queries_path.name, "no queries")
    return loaded


@dataclass(frozen=True)
class EvalRun:
    report: dict
    baseline: dict | None  # what the run is compared with
    existing: dict | None  # what --save-baseline merges into (<eval-dir>/baseline.json)
    eval_dir: Path


def execute(args):
    eval_dir = Path(args.eval_dir) if args.eval_dir else Path(config.STATE_DIR) / "eval"
    loaded = prepare_queries(eval_dir)
    frozen_now = load_frozen_now(eval_dir)
    baseline = load_baseline(args.baseline, eval_dir)
    existing = load_baseline(None, eval_dir) if args.save_baseline else None
    db_path = preflight_db()
    conn = open_readonly(db_path)
    try:
        usable, stale, ambiguous = validate_queries(conn, loaded)
        clock = clock_info(conn, frozen_now)
    finally:
        conn.close()
    if not usable:
        raise EvalInputError(f"no evaluable queries ({len(stale)} stale: segment ids moved)")
    vector = vector_status() if "hybrid" in args.pipelines else None
    results = run_evaluation(usable, args.k, args.pipelines, frozen_now)
    report = build_report(
        k=args.k, frozen_now=frozen_now, loaded=loaded, usable=usable, stale=stale,
        ambiguous=ambiguous, pipelines=args.pipelines, results=results, vector=vector,
        clock=clock,
    )
    return EvalRun(report, baseline, existing, eval_dir)


def save_baseline(run, status):
    """Write <eval-dir>/baseline.json (merged per pipeline) and describe what happened."""
    merged, carried, dropped = merge_baseline(run.existing, run.report)
    target = write_json(baseline_path(run.eval_dir), merged)
    lines = [f"baseline saved: {target} (pipelines: {', '.join(merged['pipelines'])})"]
    if carried:
        lines.append(
            f"merged: kept {', '.join(carried)} from the earlier baseline "
            "(`sources` records when each pipeline was measured)"
        )
    if dropped:
        lines.append(
            "warning: the earlier baseline was not comparable with this run and was replaced; "
            f"dropped pipelines: {', '.join(dropped)}"
        )
    missing = [n for n in PIPELINE_NAMES if n not in merged["pipelines"]]
    if missing:
        lines.append(
            f"note: the baseline does not cover {', '.join(missing)}; a run that includes them "
            "is incomparable (exit 3) until they are saved too"
        )
    if status == EXIT_REGRESSION:
        lines.append(
            "note: the gate failed above (regression, exit 1); the baseline was saved anyway"
        )
    elif status == EXIT_INCOMPARABLE:
        lines.append(
            "note: the old baseline was not comparable (nothing gated, exit 3); "
            "the baseline was saved anyway"
        )
    return "\n".join(lines)


def describe_exception(exc):
    """Exception type and raise site. Never the message: it may echo query text."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return type(exc).__name__
    last = frames[-1]
    return f"{type(exc).__name__} at {Path(last.filename).name}:{last.lineno} in {last.name}"


def run_cli(args):
    run = execute(args)
    report = run.report
    path = write_run_report(run.eval_dir, report)
    print(format_summary(report))
    print(format_table(report))
    for warning in hybrid_warnings(report) + clock_warnings(report):
        print(f"warning: {warning}", file=sys.stderr)
    print(f"report: {path}")
    status = EXIT_OK
    if run.baseline is None:
        print("no baseline to compare with; nothing was gated (--save-baseline records one)")
    else:
        comparison = compare_reports(report, run.baseline, args.threshold)
        print(format_comparison(comparison, args.threshold))
        status = comparison_status(comparison)
    if args.save_baseline:
        print(save_baseline(run, status))
    return status


def main(argv=None):
    args = parse_args(argv)
    try:
        return run_cli(args)
    except EvalInputError as e:
        print(f"eval_search: error: {e}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    except Exception as e:  # noqa: BLE001 - last resort; never echo the message (query text)
        print(f"eval_search: unexpected error: {describe_exception(e)}", file=sys.stderr)
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    sys.exit(main())
