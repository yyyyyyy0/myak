"""scripts/eval_search.py: metric functions, input handling, baseline gate, end-to-end runs."""

import hashlib
import importlib.util
import json
import re
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from myak import config, query, vec
from tests._vec_skip import require_sqlite_vec
from tests.corpus_helpers import SESSION_A, SESSION_B, build_corpus
from tests.fakes import FakeEmbedder

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "eval_search.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("eval_search", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["eval_search"] = module  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(module)
    return module


ev = _load_script()
FROZEN = ev.parse_instant("2026-10-15T00:00:00Z")


# ------------------------------------------------------------------- helpers


def res(*pairs):
    """Result dicts as the search path returns them: (segment_id, 8-char session prefix)."""
    return [{"segment_id": s, "session_id": p, "source": "fts"} for s, p in pairs]


def outcome(qtype="lexical", seg=None, sess=None, n=0, ms=1.0, vec_assisted=False, qid="q"):
    return ev.Outcome(qid, qtype, seg, sess, n, ms, vec_assisted)


def query_row(qid, seg, session, qtype, text, **extra):
    return {"qid": qid, "segment_id": seg, "session_id": session, "type": qtype,
            "query": text, **extra}


QUERY_ROWS = [
    query_row("q1", 1, SESSION_A, "lexical", "garage automobile repair"),
    query_row("q2", 4, SESSION_B, "lexical", "postgres migration checklist"),
    query_row("q3", 1, SESSION_A, "semantic", "vehicle problem today"),
]


def write_eval_dir(path, rows=QUERY_ROWS, meta=None):
    path.mkdir(parents=True, exist_ok=True)
    (path / "queries.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    if meta is not None:
        (path / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture
def corpus(db_env):
    conn = build_corpus(db_env)
    conn.close()
    return db_env


@pytest.fixture
def vec_corpus(db_env, monkeypatch):
    """Corpus with every segment embedded by the fake embedder; the search path uses it too."""
    require_sqlite_vec()
    conn = build_corpus(db_env)
    vec.embed_pending(conn, FakeEmbedder())
    conn.close()
    monkeypatch.setattr(vec, "default_embedder", lambda **kw: FakeEmbedder())
    return db_env


# ------------------------------------------------------------ ranking metrics


def test_segment_rank_positions_and_cutoff():
    results = res((5, "aaaaaaaa"), (9, "bbbbbbbb"), (7, "cccccccc"))
    assert ev.segment_rank(results, 5, 10) == 1
    assert ev.segment_rank(results, 7, 10) == 3
    assert ev.segment_rank(results, 7, 2) is None  # beyond the cutoff
    assert ev.segment_rank(results, 42, 10) is None
    assert ev.segment_rank([], 5, 10) is None


def test_segment_rank_duplicate_uses_first_occurrence():
    results = res((3, "aaaaaaaa"), (8, "aaaaaaaa"), (3, "bbbbbbbb"))
    assert ev.segment_rank(results, 3, 10) == 1


def test_segment_rank_ignores_results_without_segment_id():
    assert ev.segment_rank([{"session_id": "aaaaaaaa"}], 1, 10) is None


def test_session_rank_compares_eight_char_prefix():
    results = res((1, "aaaaaaaa"), (2, "bbbbbbbb"), (3, "bbbbbbbb"))
    assert ev.session_rank(results, "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb", 10) == 2
    assert ev.session_rank(results, "bbbbbbbb", 10) == 2  # short ids (e.g. desktop-) still work
    assert ev.session_rank(results, "bbbbbbbb-x", 1) is None
    assert ev.session_rank(results, "dddddddd-x", 10) is None
    assert ev.session_rank([], "aaaaaaaa-x", 10) is None


def test_score_query_combines_ranks_and_vector_flag():
    q = ev.EvalQuery("q1", 7, "cccccccc-3333", "semantic", "text")
    results = res((1, "aaaaaaaa"), (7, "cccccccc"))
    results[1]["source"] = "both"
    scored = ev.score_query(q, results, 10, 4.5)
    assert (scored.segment_rank, scored.session_rank, scored.n_results) == (2, 2, 2)
    assert scored.vec_assisted is True and scored.latency_ms == 4.5
    assert (scored.qid, scored.type) == ("q1", "semantic")


def test_score_query_with_empty_results_is_a_miss():
    q = ev.EvalQuery("q1", 7, "cccccccc-3333", "lexical", "text")
    scored = ev.score_query(q, [], 10, 1.0)
    assert (scored.segment_rank, scored.session_rank, scored.n_results) == (None, None, 0)
    assert scored.vec_assisted is False


# ------------------------------------------------------------------ aggregates


def test_percentile_hand_computed():
    values = [40, 10, 30, 20]
    assert ev.percentile(values, 0) == 10
    assert ev.percentile(values, 50) == 25  # (20 + 30) / 2
    assert ev.percentile(values, 95) == pytest.approx(38.5)  # 30 + 0.85 * 10
    assert ev.percentile(values, 100) == 40


def test_percentile_ties_single_and_empty():
    assert ev.percentile([5, 5, 5, 5], 50) == 5
    assert ev.percentile([5, 5, 5, 5], 95) == 5
    assert ev.percentile([7], 95) == 7
    assert ev.percentile([], 50) is None


def test_summarize_hand_computed():
    outcomes = [
        outcome(seg=1, sess=1, n=3, ms=10),
        outcome(seg=3, sess=2, n=5, ms=20),
        outcome(seg=None, sess=4, n=0, ms=30),
        outcome(seg=6, sess=None, n=2, ms=40),
    ]
    m = ev.summarize(outcomes)
    assert m["n"] == 4
    assert m["hit_at_1"] == 0.25  # only the first
    assert m["hit_at_5"] == 0.5  # ranks 1 and 3
    assert m["mrr_at_k"] == pytest.approx((1 + 1 / 3 + 0 + 1 / 6) / 4, abs=1e-6)  # 0.375
    assert m["session_hit_at_5"] == 0.75  # ranks 1, 2, 4
    assert m["empty_rate"] == 0.25
    assert m["latency_p50_ms"] == 25
    assert m["latency_p95_ms"] == pytest.approx(38.5)


def test_summarize_ties_in_rank_count_each_query():
    m = ev.summarize([outcome(seg=2, sess=2, n=2), outcome(seg=2, sess=2, n=2)])
    assert m["hit_at_1"] == 0.0 and m["hit_at_5"] == 1.0 and m["mrr_at_k"] == 0.5


def test_summarize_session_cutoff_is_five():
    m = ev.summarize([outcome(seg=None, sess=5, n=5), outcome(seg=None, sess=6, n=6)])
    assert m["session_hit_at_5"] == 0.5


def test_summarize_all_empty_results():
    m = ev.summarize([outcome(n=0), outcome(n=0)])
    assert (m["hit_at_1"], m["hit_at_5"], m["mrr_at_k"], m["session_hit_at_5"]) == (0, 0, 0, 0)
    assert m["empty_rate"] == 1.0


def test_summarize_no_outcomes_is_all_none():
    m = ev.summarize([])
    assert m["n"] == 0
    assert all(v is None for key, v in m.items() if key != "n")


def test_summarize_by_type_only_present_types():
    outcomes = [outcome("lexical", seg=1, n=1), outcome("lexical", n=0)]
    by_type = ev.summarize_by_type(outcomes)
    assert list(by_type) == ["lexical"]
    assert by_type["lexical"]["n"] == 2 and by_type["lexical"]["hit_at_1"] == 0.5
    both = ev.summarize_by_type(outcomes + [outcome("semantic", seg=2, n=2)])
    assert list(both) == ["lexical", "semantic"]


# ----------------------------------------------------------------- input files


def test_load_queries_skips_blank_and_skip_lines(tmp_path):
    rows = [
        query_row(1, 10, "s" * 36, "lexical", "  padded query  "),
        query_row("skipped", 11, "s" * 36, "lexical", "x", skip=True),
        query_row("q2", 12, "t" * 36, "semantic", "another one"),
    ]
    path = write_eval_dir(tmp_path, rows) / "queries.jsonl"
    path.write_text(path.read_text() + "\n   \n", encoding="utf-8")
    loaded = ev.load_queries(path)
    assert [q.qid for q in loaded] == [1, "q2"]
    assert loaded[0].text == "padded query" and loaded[0].segment_id == 10


@pytest.mark.parametrize(
    "mutation",
    [
        {"qid": ""}, {"qid": None}, {"qid": True},
        {"segment_id": "7"}, {"segment_id": True}, {"segment_id": None},
        {"session_id": ""}, {"session_id": 5},
        {"type": "other"}, {"type": None},
        {"query": "ab"}, {"query": "   "}, {"query": 7},
    ],
)
def test_load_queries_rejects_bad_fields(tmp_path, mutation):
    row = {**query_row("q1", 1, "s" * 36, "lexical", "SECRET-QUERY-TEXT"), **mutation}
    path = write_eval_dir(tmp_path, [row]) / "queries.jsonl"
    with pytest.raises(ev.EvalInputError, match=r"queries\.jsonl:1") as exc:
        ev.load_queries(path)
    assert "SECRET" not in str(exc.value)


def test_load_queries_rejects_non_json_and_non_object(tmp_path):
    path = tmp_path / "queries.jsonl"
    path.write_text("not json SECRET-QUERY-TEXT\n", encoding="utf-8")
    with pytest.raises(ev.EvalInputError, match="not valid JSON") as exc:
        ev.load_queries(path)
    assert "SECRET" not in str(exc.value)
    path.write_text("[1, 2]\n", encoding="utf-8")
    with pytest.raises(ev.EvalInputError, match="JSON object"):
        ev.load_queries(path)


def test_load_queries_rejects_duplicate_qid(tmp_path):
    rows = [query_row("dup", 1, "s" * 36, "lexical", "first"),
            query_row("dup", 2, "s" * 36, "lexical", "second")]
    path = write_eval_dir(tmp_path, rows) / "queries.jsonl"
    with pytest.raises(ev.EvalInputError, match=r"queries\.jsonl:2: duplicate qid"):
        ev.load_queries(path)


def test_parse_instant_variants():
    assert ev.parse_instant("2026-10-15T00:00:00Z").isoformat() == "2026-10-15T00:00:00+00:00"
    assert ev.parse_instant("2026-10-15T09:00:00+09:00").isoformat() == "2026-10-15T00:00:00+00:00"
    assert ev.parse_instant("2026-10-15T00:00:00").isoformat() == "2026-10-15T00:00:00+00:00"
    with pytest.raises(ev.EvalInputError):
        ev.parse_instant("yesterday")
    with pytest.raises(ev.EvalInputError):
        ev.parse_instant(20261015)


def test_load_frozen_now_default_meta_and_errors(tmp_path):
    assert ev.load_frozen_now(tmp_path) == ev.parse_instant(ev.DEFAULT_FROZEN_NOW)
    (tmp_path / "meta.json").write_text('{"frozen_now": "2026-01-02T03:04:05Z"}')
    assert ev.load_frozen_now(tmp_path).isoformat() == "2026-01-02T03:04:05+00:00"
    (tmp_path / "meta.json").write_text("{}")
    assert ev.load_frozen_now(tmp_path) == ev.parse_instant(ev.DEFAULT_FROZEN_NOW)
    (tmp_path / "meta.json").write_text("{broken")
    with pytest.raises(ev.EvalInputError, match="meta.json"):
        ev.load_frozen_now(tmp_path)
    (tmp_path / "meta.json").write_text("[]")
    with pytest.raises(ev.EvalInputError, match="JSON object"):
        ev.load_frozen_now(tmp_path)


def test_queries_digest_is_order_independent_and_content_sensitive():
    a = ev.EvalQuery("a", 1, "s1", "lexical", "alpha")
    b = ev.EvalQuery("b", 2, "s2", "semantic", "beta")
    assert ev.queries_digest([a, b]) == ev.queries_digest([b, a])
    changed = ev.EvalQuery("b", 2, "s2", "semantic", "beta!")
    assert ev.queries_digest([a, b]) != ev.queries_digest([a, changed])


# ----------------------------------------------------------------- environment


def test_frozen_time_pins_decay_and_restores_clock():
    original = query.datetime
    stamp = "2026-09-15T00:00:00Z"  # 30 days before FROZEN = one half-life
    with ev.frozen_time(FROZEN):
        assert query.time_decay_score(stamp, 1.0) == pytest.approx(0.5, rel=1e-3)
        assert query.datetime.now() == FROZEN
        assert query.datetime.now(ev.timezone.utc) == FROZEN
    assert query.datetime is original
    assert query.time_decay_score(stamp, 1.0) < 0.5  # real clock is much later than FROZEN


def test_open_readonly_rejects_writes_and_leaves_file_untouched(corpus):
    before = sha256(corpus.db_path)
    conn = ev.open_readonly(corpus.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 6
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM segments")
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE evil(x)")
    finally:
        conn.close()
    assert sha256(corpus.db_path) == before


def test_attached_vec_db_is_read_only_too(vec_corpus):
    before = sha256(config.VEC_DB_PATH)
    conn = ev.open_readonly(vec_corpus.db_path)
    try:
        vec.open_vec(conn, FakeEmbedder())
        assert conn.execute("SELECT COUNT(*) FROM vec.embedding_meta").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM vec.embedding_meta")
    finally:
        conn.close()
    assert sha256(config.VEC_DB_PATH) == before


def test_validate_queries_flags_stale_and_ambiguous(corpus):
    rw = sqlite3.connect(str(corpus.db_path), isolation_level=None)
    rw.execute(  # a second session sharing SESSION_A's 8-char prefix
        "INSERT INTO sessions(session_id, project_path) VALUES (?, 'p')",
        (SESSION_A[:8] + "-other",),
    )
    rw.close()
    queries = [
        ev.EvalQuery("ok-b", 3, SESSION_B, "lexical", "tomatoes"),
        ev.EvalQuery("ambiguous", 1, SESSION_A, "lexical", "garage"),
        ev.EvalQuery("missing", 99, SESSION_A, "lexical", "gone"),
        ev.EvalQuery("moved", 1, SESSION_B, "lexical", "wrong session"),
    ]
    conn = ev.open_readonly(corpus.db_path)
    try:
        usable, stale, ambiguous = ev.validate_queries(conn, queries)
    finally:
        conn.close()
    assert [q.qid for q in usable] == ["ok-b", "ambiguous"]
    assert [q.qid for q in stale] == ["missing", "moved"]
    assert ambiguous == 1


def test_preflight_db_errors(db_env):
    with pytest.raises(ev.EvalInputError, match="not found"):
        ev.preflight_db()
    db_env.memory_dir.mkdir(parents=True)
    old = sqlite3.connect(str(db_env.db_path))
    old.execute("CREATE TABLE segments(id)")
    old.close()
    with pytest.raises(ev.EvalInputError, match="myak migrate"):
        ev.preflight_db()


def test_vector_status_reports_coverage(vec_corpus):
    status = ev.vector_status()
    assert status["available"] is True and status["model"] == "fake-model"
    assert (status["embedded"], status["segments"], status["coverage"]) == (6, 6, 1.0)


def test_vector_status_partial_coverage(corpus, monkeypatch):
    require_sqlite_vec()
    conn = sqlite3.connect(str(corpus.db_path), isolation_level=None)
    vec.embed_pending(conn, FakeEmbedder(), limit=4)
    conn.close()
    monkeypatch.setattr(vec, "default_embedder", lambda **kw: FakeEmbedder())
    status = ev.vector_status()
    assert (status["embedded"], status["segments"]) == (4, 6)
    assert status["coverage"] == pytest.approx(4 / 6, abs=1e-6)


def test_vector_status_unavailable_variants(corpus, monkeypatch):
    status = ev.vector_status()  # no model marker in the isolated state dir
    assert status["available"] is False and "not downloaded" in status["reason"]
    assert status["segments"] == 6 and status["embedded"] is None and status["coverage"] is None
    monkeypatch.setenv("MYAK_DISABLE_VEC", "1")
    assert "MYAK_DISABLE_VEC" in ev.vector_status()["reason"]


def test_vector_status_without_index(corpus, monkeypatch):
    require_sqlite_vec()
    monkeypatch.setattr(vec, "default_embedder", lambda **kw: FakeEmbedder())
    status = ev.vector_status()  # embedder fine, but no memory.vec.db yet
    assert status["available"] is False and "no vector index" in status["reason"]


# ------------------------------------------------------------------- pipelines


def test_evaluate_pipeline_cold_pass_then_timed_pass(monkeypatch):
    calls = []

    def fake_runner(text, k):
        calls.append(text)
        return res((1, "aaaaaaaa")) if text == "one" else []

    monkeypatch.setitem(ev.PIPELINES, "fake", fake_runner)
    queries = [
        ev.EvalQuery("a", 1, "aaaaaaaa-x", "lexical", "one"),
        ev.EvalQuery("b", 2, "bbbbbbbb-x", "semantic", "two"),
    ]
    entry = ev.evaluate_pipeline("fake", queries, 7)
    assert calls == ["one", "one", "two"]  # first query: cold run, then measured with the rest
    assert entry["cold_ms"] is not None and entry["depth"] == 7
    assert entry["overall"]["n"] == 2 and entry["overall"]["hit_at_1"] == 0.5
    assert set(entry["by_type"]) == {"lexical", "semantic"}
    assert [p["qid"] for p in entry["per_query"]] == ["a", "b"]
    assert "vec_assisted_rate" not in entry
    assert ev.evaluate_pipeline("fake", [], 7)["cold_ms"] is None


def test_hook_depth_is_the_production_injection_cap(monkeypatch):
    assert ev.pipeline_depth("hook", 10) == query.HOOK_MAX_RESULTS
    assert ev.pipeline_depth("fts", 10) == 10 and ev.pipeline_depth("hybrid", 4) == 4


def test_hook_pipeline_matches_production_hook_output(corpus):
    with ev.eval_environment(FROZEN):
        for text in ("garage automobile repair", "garage", "postgres migration checklist",
                     "vehicle problem today", "no such words anywhere"):
            expected = query._hook_output(text, query.MAX_RESULTS)
            assert query.format_hook(ev.hook_pipeline(text, 10), text) == expected


def test_hook_pipeline_honours_hook_prompt_rules_and_cap(corpus, monkeypatch):
    with ev.eval_environment(FROZEN):
        assert ev.hook_pipeline("/garage automobile repair", 10) == []  # slash commands skipped
        tokens = query.tokenize_query_terms("garage")
        raw = query.search("garage", query.MAX_RESULTS)
        assert len(query.filter_results(raw, tokens, hook_mode=True)) >= 2
        monkeypatch.setattr(query, "HOOK_MAX_RESULTS", 1)
        capped = ev.hook_pipeline("garage", 10)
        assert len(capped) == 1
        assert query.format_hook(capped, "garage") == query._hook_output("garage", 5)


def test_fts_pipeline_matches_cli_no_hybrid(corpus, capsys):
    with ev.eval_environment(FROZEN):
        for text in ("garage automobile repair", "postgres migration checklist",
                     "vehicle problem today"):
            query.main([text, "--no-hybrid", "--limit", "10"])
            cli = capsys.readouterr().out
            assert query.format_plain(ev.fts_pipeline(text, 10), text) + "\n" == cli


def test_hybrid_pipeline_matches_cli_hybrid(vec_corpus, capsys):
    with ev.eval_environment(FROZEN):
        for text in ("garage automobile repair", "postgres migration checklist",
                     "vehicle problem today"):
            query.main([text, "--hybrid", "--limit", "10"])
            cli = capsys.readouterr().out
            assert query.format_plain(ev.hybrid_pipeline(text, 10), text) + "\n" == cli
        assert ev.hybrid_pipeline("vehicle problem today", 10)[0]["segment_id"] == 1


def test_hybrid_pipeline_falls_back_to_fts_like_mcp(monkeypatch):
    seen = []

    def fake_search(text, k, use_vec=False):
        seen.append(use_vec)
        if use_vec:
            raise RuntimeError("vec broke")
        return res((1, "aaaaaaaa"))

    monkeypatch.setattr(query, "search", fake_search)
    assert ev.hybrid_pipeline("some query", 5) == res((1, "aaaaaaaa"))
    assert seen == [True, False]


# ---------------------------------------------------------------- baseline gate


def make_report(sess, mrr, pipeline="fts", **over):
    overall = {"hit_at_1": 0.5, "hit_at_5": 0.6, "mrr_at_k": mrr, "session_hit_at_5": sess,
               "empty_rate": 0.1}
    report = {"k": 10, "frozen_now": "2026-10-15T00:00:00Z", "queries": {"sha256": "abc"},
              "pipelines": {pipeline: {"overall": overall}}}
    report.update(over)
    return report


def test_compare_reports_threshold_is_inclusive_of_equal_drop():
    base = make_report(0.80, 0.50)
    exactly = ev.compare_reports(make_report(0.77, 0.47), base, 0.03)
    assert exactly["regressions"] == []  # a drop of exactly 0.03 is allowed
    assert exactly["deltas"]["fts"]["session_hit_at_5"] == pytest.approx(-0.03)


def test_compare_reports_flags_each_gate_metric():
    base = make_report(0.80, 0.50)
    sess = ev.compare_reports(make_report(0.769, 0.50), base, 0.03)
    assert [(p, m) for p, m, _ in sess["regressions"]] == [("fts", "session_hit_at_5")]
    mrr = ev.compare_reports(make_report(0.80, 0.469), base, 0.03)
    assert [(p, m) for p, m, _ in mrr["regressions"]] == [("fts", "mrr_at_k")]
    both = ev.compare_reports(make_report(0.5, 0.1), base, 0.03)
    assert len(both["regressions"]) == 2


def test_compare_reports_improvement_and_non_gate_drops_pass():
    base = make_report(0.60, 0.40)
    better = ev.compare_reports(make_report(0.90, 0.80), base, 0.03)
    assert better["regressions"] == []
    worse_empty = make_report(0.60, 0.40)
    worse_empty["pipelines"]["fts"]["overall"]["hit_at_1"] = 0.0  # not a gate metric
    assert ev.compare_reports(worse_empty, base, 0.03)["regressions"] == []


def test_compare_reports_notes_for_incomparable_runs():
    base = make_report(0.8, 0.5, pipeline="fts")
    cur = make_report(0.8, 0.5, pipeline="hybrid", k=5, frozen_now="2026-01-01T00:00:00Z")
    cur["queries"]["sha256"] = "different"
    out = ev.compare_reports(cur, base, 0.03)
    text = " ".join(out["notes"])
    assert "k differs" in text and "frozen_now differs" in text
    assert "query set differs" in text and "hybrid: not in baseline" in text
    assert out["deltas"] == {} and out["regressions"] == []


def test_compare_reports_skips_none_metrics():
    base = make_report(0.8, 0.5)
    base["pipelines"]["fts"]["overall"]["session_hit_at_5"] = None
    out = ev.compare_reports(make_report(0.0, 0.5), base, 0.03)
    assert "session_hit_at_5" not in out["deltas"]["fts"] and out["regressions"] == []


def test_load_baseline_resolution_and_errors(tmp_path):
    assert ev.load_baseline(None, tmp_path) is None
    with pytest.raises(ev.EvalInputError, match="baseline not found"):
        ev.load_baseline(tmp_path / "nope.json", tmp_path)
    (tmp_path / "baseline.json").write_text(json.dumps(make_report(0.5, 0.5)))
    assert ev.load_baseline(None, tmp_path)["k"] == 10
    (tmp_path / "other.json").write_text("{broken")
    with pytest.raises(ev.EvalInputError, match="other.json"):
        ev.load_baseline(tmp_path / "other.json", tmp_path)
    (tmp_path / "other.json").write_text('{"k": 1}')
    with pytest.raises(ev.EvalInputError, match="not an eval report"):
        ev.load_baseline(tmp_path / "other.json", tmp_path)


# -------------------------------------------------------------- report writing


def test_write_run_report_permissions_name_and_collision(tmp_path):
    report = {"git": {"sha": "abc1234"}, "k": 10}
    first = ev.write_run_report(tmp_path, report)
    second = ev.write_run_report(tmp_path, report)
    assert first != second and second.name.endswith("-abc1234-2.json")
    assert re.fullmatch(r"\d{8}T\d{6}Z-abc1234\.json", first.name)
    assert stat.S_IMODE(first.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "runs").stat().st_mode) == 0o700
    assert json.loads(first.read_text()) == report


def test_git_info_shape_and_fallback(monkeypatch):
    info = ev.git_info()
    assert re.fullmatch(r"[0-9a-f]{4,40}|nogit", info["sha"])

    def boom(*a, **k):
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(ev.subprocess, "run", boom)
    assert ev.git_info() == {"sha": "nogit", "dirty": None}
    monkeypatch.setattr(
        ev.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="not-a-sha!\n", stderr=""),
    )
    assert ev.git_info()["sha"] == "nogit"


# -------------------------------------------------------------------- formatting


def test_format_helpers_render_expected_cells():
    outcomes = [outcome("lexical", seg=1, sess=1, n=1, ms=2.0),
                outcome("semantic", seg=None, sess=None, n=0, ms=4.0)]
    entry = {"depth": 10, "cold_ms": 12.34, "overall": ev.summarize(outcomes),
             "by_type": ev.summarize_by_type(outcomes), "vec_assisted_rate": 0.5}
    report = {
        "k": 10, "frozen_now": "2026-10-15T00:00:00Z", "git": {"sha": "abc1234", "dirty": True},
        "queries": {"evaluated": 2, "total": 3, "stale": 1, "ambiguous_session_prefix": 0},
        "vector": {"available": True, "embedded": 5, "segments": 6, "coverage": 0.8333,
                   "reason": None},
        "pipelines": {"hybrid": entry},
    }
    table = ev.format_table(report)
    assert "mrr@10" in table and "lexical" in table and "semantic" in table
    assert re.search(r"hybrid\s+all\s+2\s", table)
    assert "cold first query (ms): hybrid 12.3" in table
    summary = ev.format_summary(report)
    assert "(dirty)" in summary and "stale 1" in summary and "coverage=0.833" in summary
    comparison = {"deltas": {"fts": {"mrr_at_k": -0.1, "hit_at_1": 0.0}},
                  "regressions": [("fts", "mrr_at_k", -0.1)], "notes": ["k differs"]}
    rendered = ev.format_comparison(comparison, 0.03)
    assert "-0.100" in rendered and "FAIL" in rendered and "note: k differs" in rendered


def test_hybrid_warnings_branches():
    def report(vector, rate=None):
        return {"vector": vector, "pipelines": {"hybrid": {"vec_assisted_rate": rate}}}

    assert ev.hybrid_warnings({"vector": None, "pipelines": {}}) == []
    unavailable = {"available": False, "reason": "model missing", "embedded": None}
    assert "model missing" in ev.hybrid_warnings(report(unavailable))[0]
    silent = {"available": True, "reason": None, "embedded": 10}
    assert "fallen back" in ev.hybrid_warnings(report(silent, rate=0.0))[0]
    assert ev.hybrid_warnings(report(silent, rate=0.4)) == []


# ---------------------------------------------------------------------- CLI / e2e


def test_parse_args_defaults_and_validation(capsys):
    args = ev.parse_args([])
    assert (args.k, args.pipelines, args.threshold) == (10, ["hook", "fts", "hybrid"], 0.03)
    assert args.eval_dir is None and args.baseline is None and args.save_baseline is False
    assert ev.parse_args(["--pipelines", "fts, hook,fts"]).pipelines == ["fts", "hook"]
    for bad in (["--pipelines", "nope"], ["--pipelines", ","], ["--k", "0"],
                ["--threshold", "-1"]):
        with pytest.raises(SystemExit) as exc:
            ev.parse_args(bad)
        assert exc.value.code == 2


def test_main_reports_input_errors_with_exit_2(corpus, tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ev.main(["--eval-dir", str(empty)]) == 2
    assert "queries.jsonl not found" in capsys.readouterr().err

    write_eval_dir(empty, [])
    assert ev.main(["--eval-dir", str(empty)]) == 2
    assert "no queries" in capsys.readouterr().err

    stale_only = write_eval_dir(
        tmp_path / "stale", [query_row("x", 99, SESSION_A, "lexical", "abc")]
    )
    assert ev.main(["--eval-dir", str(stale_only), "--pipelines", "fts"]) == 2
    assert "no evaluable queries" in capsys.readouterr().err

    good = write_eval_dir(tmp_path / "good")
    assert ev.main(["--eval-dir", str(good), "--baseline", str(tmp_path / "missing.json")]) == 2
    assert "baseline not found" in capsys.readouterr().err


def test_main_without_database_exits_2(db_env, tmp_path, capsys):
    good = write_eval_dir(tmp_path / "good")
    assert ev.main(["--eval-dir", str(good)]) == 2
    assert "memory database not found" in capsys.readouterr().err


def test_main_fts_and_hook_end_to_end(corpus, tmp_path, capsys):
    eval_dir = write_eval_dir(tmp_path / "eval", QUERY_ROWS + [
        query_row("skipped", 1, SESSION_A, "lexical", "ignored", skip=True),
        query_row("gone", 99, SESSION_A, "lexical", "segment was deleted"),
    ])
    db_hash = sha256(corpus.db_path)

    assert ev.main(["--eval-dir", str(eval_dir), "--pipelines", "hook,fts"]) == 0
    out = capsys.readouterr().out
    assert "hook" in out and "fts" in out and "report:" in out

    assert sha256(corpus.db_path) == db_hash  # strictly read-only
    (report_path,) = list((eval_dir / "runs").glob("*.json"))
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    report = json.loads(report_path.read_text())
    assert report["queries"]["total"] == 4 and report["queries"]["evaluated"] == 3
    assert report["queries"]["stale"] == 1
    assert report["queries"]["by_type"] == {"lexical": 2, "semantic": 1}
    assert report["frozen_now"] == "2026-10-15T00:00:00Z" and report["vector"] is None
    assert list(report["pipelines"]) == ["hook", "fts"]
    for name in ("hook", "fts"):
        overall = report["pipelines"][name]["overall"]
        assert overall["n"] == 3
        assert overall["hit_at_1"] == pytest.approx(2 / 3, abs=1e-6)  # the semantic query misses
        assert overall["session_hit_at_5"] == pytest.approx(2 / 3, abs=1e-6)
        assert overall["empty_rate"] == pytest.approx(1 / 3, abs=1e-6)
        assert report["pipelines"][name]["by_type"]["semantic"]["hit_at_1"] == 0.0
        assert report["pipelines"][name]["by_type"]["lexical"]["hit_at_1"] == 1.0
    # no private text may reach the report
    raw = report_path.read_text()
    assert "garage" not in raw and "automobile" not in raw and "postgres" not in raw


def test_main_hybrid_end_to_end_is_read_only_and_reports_coverage(vec_corpus, tmp_path, capsys):
    eval_dir = write_eval_dir(tmp_path / "eval", meta={"frozen_now": "2026-10-15T00:00:00Z"})
    before = (sha256(vec_corpus.db_path), sha256(config.VEC_DB_PATH))

    assert ev.main(["--eval-dir", str(eval_dir)]) == 0
    captured = capsys.readouterr()
    assert "warning" not in captured.err

    assert (sha256(vec_corpus.db_path), sha256(config.VEC_DB_PATH)) == before
    (report_path,) = list((eval_dir / "runs").glob("*.json"))
    report = json.loads(report_path.read_text())
    assert report["vector"]["coverage"] == 1.0 and report["vector"]["embedded"] == 6
    hybrid = report["pipelines"]["hybrid"]
    assert hybrid["overall"]["hit_at_1"] == 1.0 and hybrid["overall"]["empty_rate"] == 0.0
    assert hybrid["by_type"]["semantic"]["session_hit_at_5"] == 1.0
    assert hybrid["vec_assisted_rate"] > 0
    assert report["pipelines"]["fts"]["overall"]["hit_at_1"] < hybrid["overall"]["hit_at_1"]
    assert "vector: available=True embedded=6/6" in captured.out


def test_main_hybrid_degrades_with_warning_when_vectors_unavailable(corpus, tmp_path, capsys):
    eval_dir = write_eval_dir(tmp_path / "eval")
    assert ev.main(["--eval-dir", str(eval_dir), "--pipelines", "hybrid"]) == 0
    captured = capsys.readouterr()
    assert "vector search unavailable" in captured.err
    (report_path,) = list((eval_dir / "runs").glob("*.json"))
    report = json.loads(report_path.read_text())
    assert report["vector"]["available"] is False
    assert report["pipelines"]["hybrid"]["overall"]["hit_at_1"] == pytest.approx(2 / 3, abs=1e-6)


def test_main_uses_default_eval_dir_under_state_dir(corpus, capsys):
    write_eval_dir(config.STATE_DIR / "eval")
    assert ev.main(["--pipelines", "fts"]) == 0
    assert list((config.STATE_DIR / "eval" / "runs").glob("*.json"))


def test_main_baseline_save_compare_and_regression_gate(corpus, tmp_path, capsys):
    eval_dir = write_eval_dir(tmp_path / "eval")
    argv = ["--eval-dir", str(eval_dir), "--pipelines", "hook,fts"]

    assert ev.main(argv + ["--save-baseline"]) == 0  # no baseline yet: nothing to compare
    baseline_path = eval_dir / "baseline.json"
    assert baseline_path.is_file() and stat.S_IMODE(baseline_path.stat().st_mode) == 0o600
    assert "vs baseline" not in capsys.readouterr().out

    assert ev.main(argv) == 0  # same code, same data: no deltas
    out = capsys.readouterr().out
    assert "vs baseline" in out and "+0.000" in out and "FAIL" not in out

    baseline = json.loads(baseline_path.read_text())
    baseline["pipelines"]["fts"]["overall"]["mrr_at_k"] += 0.2
    baseline_path.write_text(json.dumps(baseline))
    assert ev.main(argv) == 1
    assert "FAIL" in capsys.readouterr().out
    assert ev.main(argv + ["--threshold", "0.25"]) == 0  # a 0.2 drop is inside the threshold
    capsys.readouterr()

    other = tmp_path / "explicit.json"  # explicit --baseline wins over baseline.json
    other.write_text(json.dumps({**baseline, "pipelines": {
        "fts": {"overall": {**baseline["pipelines"]["fts"]["overall"], "mrr_at_k": 0.0}}}}))
    assert ev.main(argv + ["--baseline", str(other)]) == 0
    capsys.readouterr()

    assert ev.main(argv + ["--save-baseline"]) == 1  # gate fails, but the save is explicit
    assert json.loads(baseline_path.read_text())["pipelines"]["fts"]["overall"][
        "mrr_at_k"
    ] == pytest.approx(2 / 3, abs=1e-6)
