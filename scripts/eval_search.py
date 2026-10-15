"""Search-quality evaluation harness for myak (developer tool; not part of the package).

Replays a private query set against the real memory DB through the three production search
paths and reports ranking quality and latency. It never writes to the databases.

    uv run --extra vec python scripts/eval_search.py [--eval-dir DIR] [--k 10]
        [--pipelines hook,fts,hybrid] [--baseline PATH] [--save-baseline] [--threshold 0.03]

Pipelines (each calls the production functions; ranking is not re-implemented here):
    hook    UserPromptSubmit auto-injection: FTS + hook filters, capped at HOOK_MAX_RESULTS
    fts     `myak query --no-hybrid`: FTS + filter_hybrid
    hybrid  MCP `myak_search` / `myak query --hybrid`: FTS + vector RRF + filter_hybrid

Exit status: 0 ok, 1 regression against the baseline, 2 bad input or environment.
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
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from myak import config, db, query, vec
from myak import search as search_mod

REPO_ROOT = Path(__file__).resolve().parent.parent

REPORT_SCHEMA = 1
DEFAULT_FROZEN_NOW = "2026-10-15T00:00:00Z"
DEFAULT_K = 10
DEFAULT_THRESHOLD = 0.03
PIPELINE_NAMES = ("hook", "fts", "hybrid")
QUERY_TYPES = ("lexical", "semantic")
MIN_QUERY_CHARS = 3  # same floor as the CLI / MCP entry points
SESSION_PREFIX_LEN = 8  # search results only carry session_id[:8]
SESSION_HIT_CUTOFF = 5
SQL_CHUNK = 500

GATE_METRICS = ("session_hit_at_5", "mrr_at_k")
COMPARE_METRICS = ("hit_at_1", "hit_at_5", "mrr_at_k", "session_hit_at_5", "empty_rate")

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_ERROR = 2


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
    """Order-independent fingerprint of the eval set (to warn when a baseline is not comparable)."""
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


@contextlib.contextmanager
def frozen_time(instant):
    """Make myak.query's time decay see `instant` as "now" (production code is untouched)."""
    with mock.patch.object(query, "datetime", _frozen_datetime_class(instant)):
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


def build_report(*, k, frozen_now, loaded, usable, stale, ambiguous, pipelines, results, vector):
    return {
        "schema": REPORT_SCHEMA,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git": git_info(),
        "k": k,
        "frozen_now": frozen_now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "queries": {
            "total": len(loaded),
            "evaluated": len(usable),
            "stale": len(stale),
            "ambiguous_session_prefix": ambiguous,
            "by_type": {t: sum(q.type == t for q in usable) for t in QUERY_TYPES},
            "sha256": queries_digest(loaded),
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


def load_baseline(explicit, eval_dir):
    path = Path(explicit) if explicit else Path(eval_dir) / "baseline.json"
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


def compare_reports(current, baseline, threshold):
    """Deltas (current - baseline) per pipeline; a gate metric dropping by > threshold fails."""
    deltas, regressions, notes = {}, [], []
    for key in ("k", "frozen_now"):
        if current.get(key) != baseline.get(key):
            notes.append(f"{key} differs from baseline; numbers are not directly comparable")
    cur_sha = (current.get("queries") or {}).get("sha256")
    base_sha = (baseline.get("queries") or {}).get("sha256")
    if cur_sha != base_sha:
        notes.append("query set differs from baseline; numbers are not directly comparable")
    for name, pdata in current["pipelines"].items():
        base_pipeline = baseline["pipelines"].get(name)
        if base_pipeline is None:
            notes.append(f"{name}: not in baseline; skipped")
            continue
        cur, base = pdata["overall"], base_pipeline["overall"]
        deltas[name] = {
            m: round(cur[m] - base[m], 9)
            for m in COMPARE_METRICS
            if cur.get(m) is not None and base.get(m) is not None
        }
        regressions += [
            (name, m, deltas[name][m])
            for m in GATE_METRICS
            if m in deltas[name] and -deltas[name][m] > threshold
        ]
    return {"deltas": deltas, "regressions": regressions, "notes": notes}


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


def format_comparison(comparison, threshold):
    rows = [["pipeline", "d_hit@1", "d_hit@5", "d_mrr", "d_sess@5", "d_empty", "gate"]]
    failed = {name for name, _, _ in comparison["regressions"]}
    for name, d in comparison["deltas"].items():
        rows.append([
            name, *(f"{d[m]:+.3f}" if m in d else "-" for m in COMPARE_METRICS),
            "FAIL" if name in failed else "ok",
        ])
    lines = [
        f"vs baseline (gate: session hit@5 / MRR drop > {threshold}):",
        _render(rows, left=1),
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
                        help="result depth for fts/hybrid and the MRR cutoff (default: 10)")
    parser.add_argument("--pipelines", type=_pipeline_list, default=list(PIPELINE_NAMES),
                        help="comma list of hook,fts,hybrid (default: all)")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="baseline report to compare with (default: <eval-dir>/baseline.json)")
    parser.add_argument("--save-baseline", action="store_true",
                        help="also write this run as <eval-dir>/baseline.json")
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


def execute(args):
    eval_dir = Path(args.eval_dir) if args.eval_dir else Path(config.STATE_DIR) / "eval"
    loaded = prepare_queries(eval_dir)
    frozen_now = load_frozen_now(eval_dir)
    baseline = load_baseline(args.baseline, eval_dir)
    db_path = preflight_db()
    conn = open_readonly(db_path)
    try:
        usable, stale, ambiguous = validate_queries(conn, loaded)
    finally:
        conn.close()
    if not usable:
        raise EvalInputError(f"no evaluable queries ({len(stale)} stale: segment ids moved)")
    vector = vector_status() if "hybrid" in args.pipelines else None
    results = run_evaluation(usable, args.k, args.pipelines, frozen_now)
    report = build_report(
        k=args.k, frozen_now=frozen_now, loaded=loaded, usable=usable, stale=stale,
        ambiguous=ambiguous, pipelines=args.pipelines, results=results, vector=vector,
    )
    return report, baseline, eval_dir


def main(argv=None):
    args = parse_args(argv)
    try:
        report, baseline, eval_dir = execute(args)
        path = write_run_report(eval_dir, report)
    except EvalInputError as e:
        print(f"eval_search: error: {e}", file=sys.stderr)
        return EXIT_ERROR
    print(format_summary(report))
    print(format_table(report))
    for warning in hybrid_warnings(report):
        print(f"warning: {warning}", file=sys.stderr)
    print(f"report: {path}")
    status = EXIT_OK
    if baseline is not None:
        comparison = compare_reports(report, baseline, args.threshold)
        print(format_comparison(comparison, args.threshold))
        if comparison["regressions"]:
            status = EXIT_REGRESSION
    if args.save_baseline:
        print(f"baseline saved: {write_json(Path(eval_dir) / 'baseline.json', report)}")
    return status


if __name__ == "__main__":
    sys.exit(main())
