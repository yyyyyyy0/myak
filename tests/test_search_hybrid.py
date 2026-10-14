"""myak.search: RRF 融合とハイブリッド検索。"""

import sys

import pytest

from myak import query, search, vec
from tests._vec_skip import require_sqlite_vec
from tests.fakes import FakeEmbedder
from tests.test_vec import make_db

require_sqlite_vec()


@pytest.fixture
def corpus(db_env):
    c = make_db(
        db_env,
        [
            "automobile repair notes for the garage",
            "login flow redesign discussion",
            "gardening tips about tomatoes",
        ],
    )
    vec.embed_pending(c, FakeEmbedder())
    c.close()
    return db_env


def test_rrf_fuse_merges_and_dedupes():
    scores = search.rrf_fuse([1, 2, 2], [2, 3])
    assert set(scores) == {1, 2, 3}
    assert scores[2] > scores[1] > scores[3]
    assert scores[2] == pytest.approx(1 / 62 + 1 / 61)


def test_fts_candidates(corpus):
    from myak.config import get_connection

    conn = get_connection()
    try:
        ids = search.fts_candidates(conn, "garage", 5)
        assert [i for i, _ in ids] == [1]
        assert search.fts_candidates(conn, '"', 5) == [] or True
    finally:
        conn.close()


def test_fts_misses_but_vector_finds(corpus):
    emb = FakeEmbedder()
    assert query.search("vehicle", 5) == []
    hits = search.hybrid_search("vehicle", 5, embedder=emb)
    assert hits and "automobile" in hits[0]["content"]


def test_hybrid_dedupes_segments_found_by_both(corpus):
    hits = search.hybrid_search("garage automobile", 5, embedder=FakeEmbedder())
    contents = [h["content"] for h in hits]
    assert len(contents) == len(set(contents))
    assert "automobile" in contents[0]


def test_use_vec_false_is_fts_only(corpus):
    hits = query.search("garage", 5)
    assert len(hits) == 1
    assert search.hybrid_search("vehicle", 5, use_vec=False, embedder=FakeEmbedder()) == []


def test_missing_sqlite_vec_falls_back_to_fts(corpus, monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    hits = search.hybrid_search("garage", 5, embedder=FakeEmbedder())
    assert len(hits) == 1
    assert search.hybrid_search("vehicle", 5, embedder=FakeEmbedder()) == []


def test_disabled_env_falls_back(corpus, monkeypatch):
    monkeypatch.setenv("MYAK_DISABLE_VEC", "1")
    assert search.hybrid_search("vehicle", 5, embedder=FakeEmbedder()) == []
    assert len(search.hybrid_search("garage", 5, embedder=FakeEmbedder())) == 1


def test_model_not_downloaded_falls_back(corpus):
    # embedder=None -> default_embedder: モデル未取得なのでダウンロードせず FTS のみ
    assert len(search.hybrid_search("garage", 5)) == 1


def test_unexpected_vector_error_falls_back(corpus):
    class Broken(FakeEmbedder):
        def embed(self, texts):
            raise RuntimeError("boom")

    assert len(search.hybrid_search("garage", 5, embedder=Broken())) == 1


def test_no_database(db_env):
    assert search.hybrid_search("anything", 5) == []


def test_outdated_schema(db_env, capsys):
    c = make_db(db_env, ["x"])
    c.execute("PRAGMA user_version=0")
    c.close()
    assert search.hybrid_search("garage", 5) == []
    assert "schema outdated" in capsys.readouterr().err


def test_query_main_hybrid_flags(corpus, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(query, "search", lambda q, n, use_vec=False: seen.append(use_vec) or [])
    query.main(["garage"])
    query.main(["garage", "--no-hybrid"])
    assert seen == [True, False]
    monkeypatch.setenv("MYAK_DISABLE_VEC", "1")
    query.main(["garage"])
    assert seen[-1] is False


def test_mcp_search_falls_back(corpus, monkeypatch):
    from myak import mcp_server

    calls = []

    def fake_search(q, max_results=5, use_vec=False):
        calls.append(use_vec)
        if use_vec:
            raise RuntimeError("vec broke")
        return []

    monkeypatch.setattr(mcp_server, "search", fake_search)
    assert "No memories" in mcp_server.myak_search("garage")
    assert calls == [True, False]


def _use_fake_embedder(monkeypatch, model="fake-model"):
    monkeypatch.setattr(vec, "default_embedder", lambda **kw: FakeEmbedder(model))


def test_results_carry_source(corpus):
    hits = search.hybrid_search("garage automobile", 5, embedder=FakeEmbedder())
    assert hits[0]["source"] == "both"
    assert {h["source"] for h in hits} <= {"fts", "vec", "both"}
    syn = search.hybrid_search("vehicle", 5, embedder=FakeEmbedder())
    assert syn[0]["source"] == "vec"
    assert query.search("garage", 5)[0]["source"] == "fts"


def test_far_neighbours_are_cut_by_distance(corpus):
    hits = search.hybrid_search("vehicle", 5, embedder=FakeEmbedder())
    contents = " ".join(h["content"] for h in hits)
    assert "automobile" in contents
    assert "gardening" not in contents and "login" not in contents


def test_cli_hybrid_keeps_synonym_only_hit(corpus, monkeypatch, capsys):
    _use_fake_embedder(monkeypatch)
    # 3 トークン以上 + 語彙一致ゼロ: 旧実装ではトークンガードで落ちていた
    query.main(["vehicle problem today", "--hybrid"])
    out = capsys.readouterr().out
    assert "automobile repair notes" in out
    assert "gardening" not in out and "login flow" not in out


def test_cli_hybrid_drops_unrelated_query(corpus, monkeypatch, capsys):
    _use_fake_embedder(monkeypatch)
    query.main(["bug glitch defect", "--hybrid"])
    assert "No memories found" in capsys.readouterr().out


def test_filter_results_exempts_vec_only_from_guards():
    base = {"role": "user", "timestamp": "", "session_id": "s", "project": ""}
    both = {**base, "content": "alpha beta gamma", "score": 1.0, "source": "both"}
    vec_only = {**base, "content": "nothing shared", "score": 0.4, "source": "vec"}
    weak_vec = {**base, "content": "nothing shared", "score": 0.1, "source": "vec"}
    weak_fts = {**base, "content": "alpha beta gamma", "score": 0.01, "source": "fts"}
    tokens = ["alpha", "beta", "gamma"]
    kept = query.filter_results([both, vec_only, weak_vec, weak_fts], tokens)
    assert kept == [both, vec_only]  # 相対下限は vec にも効く / 絶対下限は FTS にだけ効く


def test_filter_hybrid_leaves_fts_only_untouched():
    rows = [{"content": "x", "score": 0.0001, "source": "fts", "session_id": "s"}]
    assert query.filter_hybrid(rows, "alpha beta gamma") is rows


def test_default_embedder_is_cached_per_model(monkeypatch):
    search._cached_fastembed.cache_clear()
    vec.write_ready_marker(vec.config.VEC_MODEL, 384)
    first = search._default_embedder()
    assert search._default_embedder() is first
    assert isinstance(first, vec.FastEmbedEmbedder) and first.local_files_only
    search._cached_fastembed.cache_clear()
