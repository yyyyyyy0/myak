"""myak.vec: sqlite-vec / 埋め込み (FakeEmbedder)。"""

import sqlite3
from datetime import datetime, timezone

import pytest

from myak import config, db, vec
from tests._vec_skip import require_sqlite_vec
from tests.fakes import FakeEmbedder, install_fake_fastembed

require_sqlite_vec()

SCHEMA = f"""
PRAGMA user_version={db.CURRENT_VERSION};
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, project_path TEXT, started_at TEXT,
    ended_at TEXT, segment_count INTEGER DEFAULT 0, indexed_at TEXT);
CREATE TABLE segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    role TEXT NOT NULL, content TEXT NOT NULL, timestamp TEXT,
    uuid TEXT, agent_id TEXT, agent_type TEXT);
CREATE VIRTUAL TABLE segments_fts USING fts5(
    content, content='segments', content_rowid='id', tokenize='trigram');
CREATE TRIGGER segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


def make_db(db_env, contents):
    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_env.db_path), isolation_level=None)
    conn.executescript(SCHEMA)
    ts = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO sessions(session_id, project_path) VALUES ('s1', 'proj')")
    for text in contents:
        conn.execute(
            "INSERT INTO segments(session_id, role, content, timestamp) VALUES ('s1','user',?,?)",
            (text, ts),
        )
    return conn


@pytest.fixture
def conn(db_env):
    c = make_db(db_env, ["automobile repair notes", "login flow", "unrelated gardening"])
    yield c
    c.close()


def test_embed_pending_is_incremental(conn):
    emb = FakeEmbedder()
    assert vec.embed_pending(conn, emb, batch=2) == 3
    assert vec.embed_pending(conn, emb) == 0
    conn.execute(
        "INSERT INTO segments(session_id, role, content, timestamp) "
        "VALUES ('s1','user','vehicle trouble','2026-01-01T00:00:00+00:00')"
    )
    assert vec.embed_pending(conn, emb) == 1


def test_embed_pending_limit(conn):
    assert vec.embed_pending(conn, FakeEmbedder(), limit=2) == 2
    assert vec.embed_pending(conn, FakeEmbedder()) == 1


def test_vector_candidates_finds_synonym(conn):
    emb = FakeEmbedder()
    vec.embed_pending(conn, emb)
    top = vec.vector_candidates(conn, emb, "vehicle", k=2)
    assert top[0][0] == 1
    assert len(top) == 2


def test_model_mismatch_recreates_table_only_with_flag(conn):
    vec.embed_pending(conn, FakeEmbedder("model-a"))
    with pytest.raises(vec.VecUnavailable, match="model mismatch"):
        vec.embed_pending(conn, FakeEmbedder("model-b"))
    assert conn.execute("SELECT model_id FROM vec.embedding_meta").fetchall() == [("model-a",)]
    assert conn.execute("SELECT COUNT(*) FROM vec.segments_vec").fetchone()[0] == 3
    assert vec.embed_pending(conn, FakeEmbedder("model-b"), allow_rebuild=True) == 3
    meta = conn.execute("SELECT model_id FROM vec.embedding_meta").fetchall()
    assert meta == [("model-b",)]


def test_search_path_never_mutates_vec_db(conn):
    vec.embed_pending(conn, FakeEmbedder("model-a"))
    before = config.VEC_DB_PATH.read_bytes()
    with pytest.raises(vec.VecUnavailable, match="model mismatch"):
        vec.vector_candidates(conn, FakeEmbedder("model-b"), "vehicle")
    assert conn.execute("SELECT COUNT(*) FROM vec.segments_vec").fetchone()[0] == 3
    assert vec.vector_candidates(conn, FakeEmbedder("model-a"), "vehicle")
    assert config.VEC_DB_PATH.read_bytes() == before


def test_extension_loading_is_switched_off_after_open(conn):
    vec.embed_pending(conn, FakeEmbedder())
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT load_extension('nonexistent')")


def test_cosine_distance_orders_and_is_bounded(conn):
    emb = FakeEmbedder()
    vec.embed_pending(conn, emb)
    top = vec.vector_candidates(conn, emb, "vehicle", k=3)
    assert top[0][0] == 1 and top[0][1] < top[1][1] < config.VEC_MAX_DISTANCE + 1
    assert all(0 <= d <= 2 for _, d in top)


def test_corrupt_vec_db_is_unavailable(conn):
    config.VEC_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.VEC_DB_PATH.write_bytes(bytes(range(256)) * 40)
    with pytest.raises(vec.VecUnavailable):
        vec.vector_candidates(conn, FakeEmbedder(), "vehicle")


def test_embed_pending_streams_pages_and_respects_limit(conn):
    emb = FakeEmbedder()
    assert vec.embed_pending(conn, emb, batch=1, limit=2) == 2
    assert conn.execute("SELECT COUNT(*) FROM vec.segments_vec").fetchone()[0] == 2
    assert vec.embed_pending(conn, emb, batch=1) == 1


def test_vec_db_is_separate_file(conn):
    vec.embed_pending(conn, FakeEmbedder())
    assert config.VEC_DB_PATH.exists()
    assert config.VEC_DB_PATH != config.DB_PATH








def test_try_embed_pending(db_env, conn, monkeypatch):
    n, reason = vec.try_embed_pending()
    assert n == 0 and "not downloaded" in reason
    monkeypatch.setattr(vec, "default_embedder", lambda **kw: FakeEmbedder())
    assert vec.try_embed_pending() == (3, None)
    assert vec.try_embed_pending() == (0, None)



def test_try_embed_pending_generic_failure(conn, monkeypatch):
    def boom(**kw):
        raise ValueError("bad")

    monkeypatch.setattr(vec, "default_embedder", boom)
    n, reason = vec.try_embed_pending()
    assert n == 0 and "embed failed" in reason




def _patch_fastembed(monkeypatch):
    install_fake_fastembed(monkeypatch)
    monkeypatch.setattr(
        vec, "FastEmbedEmbedder", lambda model_id=None, **kw: FakeEmbedder(model_id)
    )


def test_run_embed_success_cli_and_marker(conn, monkeypatch, capsys):
    from myak import cli

    _patch_fastembed(monkeypatch)
    model = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    monkeypatch.setitem(vec.KNOWN_DIMS, model, 8)
    assert not vec.models_cached(model)
    assert cli.main(["embed", "--limit", "2", "--model", model]) == 0
    assert "Embedded 2 segments" in capsys.readouterr().out
    assert vec.models_cached(model)
    assert (config.MODELS_DIR / f"{model.replace('/', '--')}.ready").read_text().strip() == "8"


def test_run_embed_rejects_unknown_model(conn, monkeypatch, capsys):
    _patch_fastembed(monkeypatch)
    assert vec.run_embed(model="evil/model") == 2
    assert "Unknown model" in capsys.readouterr().err


def test_run_embed_model_switch_requires_rebuild(conn, monkeypatch, capsys):
    _patch_fastembed(monkeypatch)
    a, b = sorted(vec.KNOWN_DIMS)
    monkeypatch.setitem(vec.KNOWN_DIMS, a, 8)
    monkeypatch.setitem(vec.KNOWN_DIMS, b, 8)
    assert vec.run_embed(model=a) == 0
    capsys.readouterr()
    assert vec.run_embed(model=b) == 2
    assert "--rebuild" in capsys.readouterr().err
    assert vec.indexed_model() == a
    # モデル未指定なら永続化されたモデルを使い続ける
    assert vec.run_embed() == 0
    assert vec.indexed_model() == a
    assert vec.run_embed(model=b, rebuild=True) == 0
    assert vec.indexed_model() == b


def test_run_embed_rebuild_recovers_corrupt_index(conn, monkeypatch):
    _patch_fastembed(monkeypatch)
    config.VEC_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.VEC_DB_PATH.write_bytes(bytes(range(256)) * 40)
    assert vec.run_embed() == 2
    assert vec.run_embed(rebuild=True) == 0


def test_run_embed_skips_when_locked(conn, monkeypatch, capsys):
    _patch_fastembed(monkeypatch)
    with vec.embed_lock():
        assert vec.run_embed() == 1
    assert "another embed is running" in capsys.readouterr().err


def test_run_embed_no_db(db_env, monkeypatch):
    install_fake_fastembed(monkeypatch)
    assert vec.run_embed() == 1
