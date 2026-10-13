"""myak.vec: sqlite-vec / 埋め込み (FakeEmbedder)。"""

import sqlite3
import sys
import types
from datetime import datetime, timezone

import pytest

from myak import config, db, vec
from tests.fakes import FakeEmbedder

pytest.importorskip("sqlite_vec")

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


def test_model_mismatch_recreates_table(conn):
    vec.embed_pending(conn, FakeEmbedder("model-a"))
    assert vec.embed_pending(conn, FakeEmbedder("model-b")) == 3
    meta = conn.execute("SELECT model_id FROM vec.embedding_meta").fetchall()
    assert meta == [("model-b",)]


def test_vec_db_is_separate_file(conn):
    vec.embed_pending(conn, FakeEmbedder())
    assert config.VEC_DB_PATH.exists()
    assert config.VEC_DB_PATH != config.DB_PATH


def test_disabled_env(conn, monkeypatch):
    monkeypatch.setenv("MYAK_DISABLE_VEC", "1")
    with pytest.raises(vec.VecUnavailable):
        vec.open_vec(conn, FakeEmbedder())
    with pytest.raises(vec.VecUnavailable):
        vec.default_embedder()


def test_missing_sqlite_vec_raises(conn, monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    with pytest.raises(vec.VecUnavailable):
        vec.open_vec(conn, FakeEmbedder())


def test_no_load_extension_support(monkeypatch):
    class Bare:
        pass

    with pytest.raises(vec.VecUnavailable):
        vec._load_extension(Bare())


def test_extension_load_error(monkeypatch):
    class Boom:
        def enable_load_extension(self, flag):
            raise sqlite3.OperationalError("not authorized")

    with pytest.raises(vec.VecUnavailable):
        vec._load_extension(Boom())


def test_models_cached_and_default_embedder(monkeypatch):
    assert not vec.models_cached("a/b")
    with pytest.raises(vec.VecUnavailable):
        vec.default_embedder()
    d = config.MODELS_DIR / "models--sentence-transformers--paraphrase-multilingual-MiniLM-L12-v2"
    d.mkdir(parents=True)
    assert vec.models_cached(config.VEC_MODEL)
    assert isinstance(vec.default_embedder(), vec.FastEmbedEmbedder)


def _fake_fastembed(monkeypatch, fail=False):
    calls = {}

    class TextEmbedding:
        def __init__(self, **kwargs):
            if fail:
                raise RuntimeError("no model")
            calls.update(kwargs)

        def embed(self, texts):
            return ([1, 2, 3] for _ in texts)

    mod = types.ModuleType("fastembed")
    mod.TextEmbedding = TextEmbedding
    monkeypatch.setitem(sys.modules, "fastembed", mod)
    return calls


def test_fastembed_embedder_with_mock(monkeypatch):
    calls = _fake_fastembed(monkeypatch)
    e = vec.FastEmbedEmbedder(local_files_only=True)
    assert e.dim == 384
    assert e.embed(["a", "b"]) == [[1.0, 2.0, 3.0]] * 2
    assert calls["local_files_only"] is True
    assert vec.FastEmbedEmbedder(model_id="other/model").dim == 3


def test_fastembed_failures(monkeypatch):
    _fake_fastembed(monkeypatch, fail=True)
    with pytest.raises(vec.VecUnavailable):
        vec.FastEmbedEmbedder().embed(["x"])
    monkeypatch.setitem(sys.modules, "fastembed", None)
    with pytest.raises(vec.VecUnavailable):
        vec.FastEmbedEmbedder().embed(["x"])


def test_try_embed_pending(db_env, conn, monkeypatch):
    n, reason = vec.try_embed_pending()
    assert n == 0 and "not downloaded" in reason
    monkeypatch.setattr(vec, "default_embedder", lambda **kw: FakeEmbedder())
    assert vec.try_embed_pending() == (3, None)
    assert vec.try_embed_pending() == (0, None)


def test_try_embed_pending_no_db(db_env):
    assert vec.try_embed_pending() == (0, "no database")


def test_try_embed_pending_generic_failure(conn, monkeypatch):
    def boom(**kw):
        raise ValueError("bad")

    monkeypatch.setattr(vec, "default_embedder", boom)
    n, reason = vec.try_embed_pending()
    assert n == 0 and "embed failed" in reason


def test_run_embed_without_extra_exits_2(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    assert vec.run_embed() == 2
    assert "myak[vec]" in capsys.readouterr().err


def test_run_embed_disabled(monkeypatch):
    monkeypatch.setenv("MYAK_DISABLE_VEC", "1")
    assert vec.run_embed() == 2


def test_run_embed_success_and_cli(conn, monkeypatch, capsys):
    from myak import cli

    _fake_fastembed(monkeypatch)
    monkeypatch.setattr(vec, "FastEmbedEmbedder", lambda model_id=None: FakeEmbedder(model_id))
    monkeypatch.setattr(config, "VEC_MODEL", "fake-model")
    assert cli.main(["embed", "--limit", "2", "--model", "fake-model"]) == 0
    assert "Embedded 2 segments" in capsys.readouterr().out


def test_run_embed_no_db(db_env, monkeypatch):
    _fake_fastembed(monkeypatch)
    assert vec.run_embed() == 1
