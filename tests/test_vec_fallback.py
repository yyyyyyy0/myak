"""sqlite-vec / fastembed が無くても動くべき縮退・無効化・マーカーまわり (extra 不要)。"""

import sqlite3
import sys

import pytest

from myak import config, vec
from tests.fakes import FakeEmbedder, install_fake_fastembed

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


@pytest.fixture
def mem():
    c = sqlite3.connect(":memory:", isolation_level=None)
    yield c
    c.close()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " On "])
def test_disable_truthy_values(monkeypatch, value):
    monkeypatch.setenv("MYAK_DISABLE_VEC", value)
    assert config.vec_disabled()


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "2", "disabled"])
def test_disable_falsy_values(monkeypatch, value):
    monkeypatch.setenv("MYAK_DISABLE_VEC", value)
    assert not config.vec_disabled()


def test_disabled_env(mem, monkeypatch):
    monkeypatch.setenv("MYAK_DISABLE_VEC", "1")
    with pytest.raises(vec.VecUnavailable):
        vec.open_vec(mem, FakeEmbedder())
    with pytest.raises(vec.VecUnavailable):
        vec.default_embedder()


def test_search_path_without_index_raises_and_creates_nothing(mem):
    with pytest.raises(vec.VecUnavailable, match="myak embed"):
        vec.open_vec(mem, FakeEmbedder())
    assert not config.VEC_DB_PATH.exists()


def test_missing_sqlite_vec_raises(mem, monkeypatch):
    monkeypatch.setitem(sys.modules, "sqlite_vec", None)
    with pytest.raises(vec.VecUnavailable):
        vec.open_vec(mem, FakeEmbedder(), create=True)


def test_no_load_extension_support():
    class Bare:
        pass

    with pytest.raises(vec.VecUnavailable):
        vec._load_extension(Bare())


def test_extension_load_error_still_disables_loading(monkeypatch):
    calls = []

    class Boom:
        def enable_load_extension(self, flag):
            calls.append(flag)

    monkeypatch.setitem(
        sys.modules, "sqlite_vec", type(sys)("sqlite_vec")
    )  # load() が無い -> AttributeError
    with pytest.raises(vec.VecUnavailable):
        vec._load_extension(Boom())
    assert calls == [True, False]


def test_extension_load_error():
    class Boom:
        def enable_load_extension(self, flag):
            raise sqlite3.OperationalError("not authorized")

    with pytest.raises(vec.VecUnavailable):
        vec._load_extension(Boom())


def test_models_cached_is_marker_based(tmp_path):
    assert not vec.models_cached("a/b")
    # fastembed の HF キャッシュ名があっても、マーカーが無ければ未取得扱い
    hf = config.MODELS_DIR / "models--Qdrant--paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
    hf.mkdir(parents=True)
    assert not vec.models_cached(MODEL)
    vec.write_ready_marker(MODEL, 384)
    marker = config.MODELS_DIR / f"{MODEL.replace('/', '--')}.ready"
    assert marker.read_text().strip() == "384"
    assert marker.stat().st_mode & 0o777 == 0o600
    assert vec.models_cached(MODEL)
    assert not vec.models_cached("other/model")


def test_default_embedder_requires_marker_and_sets_cache_dir():
    with pytest.raises(vec.VecUnavailable):
        vec.default_embedder()
    vec.write_ready_marker(config.VEC_MODEL, 384)
    emb = vec.default_embedder()
    assert isinstance(emb, vec.FastEmbedEmbedder)
    assert emb.cache_dir == config.MODELS_DIR and emb.local_files_only


def test_default_embedder_prefers_indexed_model(monkeypatch):
    config.VEC_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(config.VEC_DB_PATH))
    c.execute("CREATE TABLE embedding_meta(model_id TEXT, dim INTEGER)")
    c.execute("INSERT INTO embedding_meta VALUES ('intfloat/multilingual-e5-large', 1024)")
    c.commit()
    c.close()
    with pytest.raises(vec.VecUnavailable):  # 索引のモデルのマーカーが必要
        vec.default_embedder()
    vec.write_ready_marker("intfloat/multilingual-e5-large", 1024)
    assert vec.default_embedder().model_id == "intfloat/multilingual-e5-large"


def test_indexed_model_ignores_garbage_file():
    config.VEC_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.VEC_DB_PATH.write_bytes(b"\x00garbage" * 100)
    assert vec.indexed_model() is None


def test_fastembed_embedder_with_mock(monkeypatch):
    calls = install_fake_fastembed(monkeypatch)
    e = vec.FastEmbedEmbedder(local_files_only=True)
    assert e.dim == 384
    assert e.embed(["a", "b"]) == [[1.0, 2.0, 3.0]] * 2
    assert calls["local_files_only"] is True
    assert vec.FastEmbedEmbedder(model_id="other/model").dim == 3


def test_fastembed_failures(monkeypatch):
    install_fake_fastembed(monkeypatch, fail=True)
    with pytest.raises(vec.VecUnavailable):
        vec.FastEmbedEmbedder().embed(["x"])
    monkeypatch.setitem(sys.modules, "fastembed", None)
    with pytest.raises(vec.VecUnavailable):
        vec.FastEmbedEmbedder().embed(["x"])


def test_run_embed_without_extra_exits_2(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "fastembed", None)
    assert vec.run_embed() == 2
    assert "myak[vec]" in capsys.readouterr().err


def test_run_embed_disabled(monkeypatch):
    monkeypatch.setenv("MYAK_DISABLE_VEC", "1")
    assert vec.run_embed() == 2


def test_try_embed_pending_no_db(db_env):
    assert vec.try_embed_pending() == (0, "no database")


def test_embed_lock_is_exclusive():
    with vec.embed_lock() as first:
        assert first is True
        with vec.embed_lock() as second:
            assert second is False
    with vec.embed_lock() as again:
        assert again is True
    lock = config.STATE_DIR / "embed.lock"
    assert lock.stat().st_mode & 0o777 == 0o600


def test_try_embed_pending_skips_when_locked(db_env):
    db_env.memory_dir.mkdir(parents=True)
    db_env.db_path.write_bytes(b"")
    with vec.embed_lock():
        assert vec.try_embed_pending() == (0, "another embed is running")
