"""ベクトル索引 (任意機能 `myak[vec]`)。sqlite-vec + fastembed を遅延 import する。

ベクトルは別ファイル memory.vec.db (派生キャッシュ) に置き、ATTACH して使う。
依存/拡張/モデルが無ければ VecUnavailable を送出し、呼び出し側は FTS のみに縮退する。
"""

import fcntl
import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Protocol

from myak import config

logger = logging.getLogger("myak.vec")

KNOWN_DIMS = {
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2": 384,
    "intfloat/multilingual-e5-large": 1024,
}
TEXT_CHARS = 1000


class VecUnavailable(RuntimeError):
    """ベクトル検索を使えない (依存欠如・拡張ロード不可・無効化・モデル未取得)。"""


class Embedder(Protocol):
    model_id: str

    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def ready_marker(model_id, cache_dir=None):
    """`<MODELS_DIR>/<model_id の / を -- に置換>.ready`。

    fastembed は HF リポジトリ id (例 models--Qdrant--...-onnx-Q) でキャッシュするため、
    sentence-transformers 側の id ではディレクトリ名から判定できない。ロード成功の印を自前で持つ。
    """
    return (cache_dir or config.MODELS_DIR) / f"{model_id.replace('/', '--')}.ready"


def write_ready_marker(model_id, dim, cache_dir=None):
    config.atomic_write(ready_marker(model_id, cache_dir), f"{int(dim)}\n", 0o600)


def models_cached(model_id, cache_dir=None):
    """モデルを取得・ロード済みか (マーカー有無のみ。ダウンロードはしない)。"""
    try:
        return ready_marker(model_id, cache_dir).is_file()
    except OSError:
        return False


class FastEmbedEmbedder:
    """fastembed の薄いラッパー。import / モデルロードは初回 embed まで遅延。"""

    def __init__(self, model_id=None, cache_dir=None, local_files_only=False):
        self.model_id = model_id or config.VEC_MODEL
        self.cache_dir = cache_dir or config.MODELS_DIR
        self.local_files_only = local_files_only
        self._model = None

    def _load(self):
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                raise VecUnavailable("fastembed is not installed (pip install 'myak[vec]')") from e
            kwargs = {"model_name": self.model_id, "cache_dir": str(self.cache_dir)}
            if self.local_files_only:
                kwargs["local_files_only"] = True
            try:
                self._model = TextEmbedding(**kwargs)
            except Exception as e:  # noqa: BLE001 - モデル未取得/破損
                raise VecUnavailable(f"embedding model unavailable: {e}") from e
        return self._model

    @property
    def dim(self):
        if self.model_id in KNOWN_DIMS:
            return KNOWN_DIMS[self.model_id]
        return len(self.embed(["dim"])[0])

    def embed(self, texts):
        model = self._load()
        return [[float(x) for x in v] for v in model.embed(texts)]


def indexed_model():
    """vec DB に記録済みのモデル id。無い/読めなければ None (拡張ロード不要で読む)。"""
    path = config.VEC_DB_PATH
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute("SELECT model_id FROM embedding_meta").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def default_embedder(*, local_files_only=True):
    """フック/検索経路用: 拡張・モデルが無ければ VecUnavailable。ダウンロードしない。

    索引が既にあればそのモデルを使う (`--model` の選択が検索側でも維持される)。
    """
    if config.vec_disabled():
        raise VecUnavailable("disabled by MYAK_DISABLE_VEC")
    model_id = indexed_model() or config.VEC_MODEL
    if local_files_only and not models_cached(model_id):
        raise VecUnavailable("embedding model not downloaded (run `myak embed`)")
    return FastEmbedEmbedder(
        model_id=model_id, cache_dir=config.MODELS_DIR, local_files_only=local_files_only
    )


def _load_extension(conn):
    try:
        import sqlite_vec
    except ImportError as e:
        raise VecUnavailable("sqlite-vec is not installed (pip install 'myak[vec]')") from e
    if not hasattr(conn, "enable_load_extension"):
        raise VecUnavailable("this Python's sqlite3 cannot load extensions")
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
    except (sqlite3.Error, AttributeError, OSError) as e:
        raise VecUnavailable(f"cannot load sqlite-vec: {e}") from e


def _attached(conn):
    return any(row[1] == "vec" for row in conn.execute("PRAGMA database_list"))


def open_vec(conn, embedder, *, allow_rebuild=False, create=False):
    """vec DB を ATTACH し sqlite-vec を読み込む。

    既定 (検索/読み出し経路) では vec DB を一切変更しない: 索引が無い/モデルが違う場合は
    VecUnavailable。作り直し (DROP) は allow_rebuild=True (= `myak embed`) のときだけ。
    create=True は索引が未作成のときの新規作成のみ許可 (既存索引とのモデル不一致は不可)。
    """
    if config.vec_disabled():
        raise VecUnavailable("disabled by MYAK_DISABLE_VEC")
    writable = create or allow_rebuild
    path = config.VEC_DB_PATH
    if not writable and not path.exists():
        raise VecUnavailable("no vector index; run `myak embed`")
    _load_extension(conn)
    try:
        _open_attached(conn, embedder, path, writable, allow_rebuild)
    except sqlite3.DatabaseError as e:  # 破損ファイル / ロック等。検索側は FTS に縮退
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise VecUnavailable(f"vector index unusable: {e}") from e


def _open_attached(conn, embedder, path, writable, allow_rebuild):
    if writable:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _attached(conn):
        conn.execute("ATTACH DATABASE ? AS vec", (str(path),))
        if writable:
            config.secure_file(path)
    dim = embedder.dim
    if not writable:
        try:
            meta = conn.execute("SELECT model_id, dim FROM vec.embedding_meta").fetchone()
        except sqlite3.OperationalError as e:
            raise VecUnavailable("no vector index; run `myak embed`") from e
        if meta is None or tuple(meta) != (embedder.model_id, dim):
            raise VecUnavailable("model mismatch; run myak embed")
        return
    own_tx = not conn.in_transaction
    if own_tx:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS vec.embedding_meta(model_id TEXT, dim INTEGER)")
        meta = conn.execute("SELECT model_id, dim FROM vec.embedding_meta").fetchone()
        if meta is not None and tuple(meta) != (embedder.model_id, dim):
            if not allow_rebuild:
                raise VecUnavailable("model mismatch; run myak embed")
            logger.warning("embedding model changed %s -> %s; rebuilding", meta, embedder.model_id)
            conn.execute("DROP TABLE IF EXISTS vec.segments_vec")
            conn.execute("DELETE FROM vec.embedding_meta")
            meta = None
        if meta is None:
            conn.execute("DELETE FROM vec.embedding_meta")
            conn.execute("INSERT INTO vec.embedding_meta VALUES (?, ?)", (embedder.model_id, dim))
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS vec.segments_vec USING vec0("
            f"segment_id INTEGER PRIMARY KEY, embedding float[{int(dim)}] "
            "distance_metric=cosine)"
        )
        if own_tx:
            conn.execute("COMMIT")
    except BaseException:
        if own_tx and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _serialize(vector):
    import sqlite_vec

    return sqlite_vec.serialize_float32(vector)


def segment_text(role, content):
    return f"{role}: {content[:TEXT_CHARS]}"


def embed_pending(conn, embedder, batch=64, limit=None, *, allow_rebuild=False):
    """まだ埋め込んでいない segment を埋め込む。埋め込んだ件数を返す (インクリメンタル)。

    ページ単位でストリーミングする (全 segment をメモリに載せない)。
    """
    open_vec(conn, embedder, allow_rebuild=allow_rebuild, create=True)
    count = 0
    while limit is None or count < limit:
        size = batch if limit is None else min(batch, limit - count)
        chunk = conn.execute(
            "SELECT id, role, content FROM segments "
            "WHERE id NOT IN (SELECT segment_id FROM vec.segments_vec) ORDER BY id LIMIT ?",
            (size,),
        ).fetchall()
        if not chunk:
            break
        vectors = embedder.embed([segment_text(r[1], r[2]) for r in chunk])
        conn.execute("BEGIN IMMEDIATE")
        try:
            # vec0 は INSERT OR REPLACE 不可: 競合しても重複しないよう先に消してから入れる
            conn.executemany(
                "DELETE FROM vec.segments_vec WHERE segment_id = ?", [(r[0],) for r in chunk]
            )
            conn.executemany(
                "INSERT INTO vec.segments_vec(segment_id, embedding) VALUES (?, ?)",
                [(r[0], _serialize(v)) for r, v in zip(chunk, vectors, strict=True)],
            )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        count += len(chunk)
    return count


def vector_candidates(conn, embedder, query, k=20):
    """クエリに近い segment を [(segment_id, cosine distance)] で返す。"""
    open_vec(conn, embedder)
    vector = embedder.embed([query])[0]
    try:
        rows = conn.execute(
            "SELECT segment_id, distance FROM vec.segments_vec "
            "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (_serialize(vector), int(k)),
        ).fetchall()
    except sqlite3.DatabaseError as e:
        raise VecUnavailable(f"vector index unusable: {e}") from e
    return [(r[0], r[1]) for r in rows]


def forget_segments(conn, ids, chunk=500):
    """削除/更新された segment のベクトルを消す (best effort)。消した件数を返す。

    vec DB が無い/使えないなら何もしない。残っていても害はなく、更新行は
    次回の `myak embed` で再計算される。
    """
    ids = list(ids)
    if not ids or not config.VEC_DB_PATH.exists():
        return 0
    attached_here = False
    try:
        _load_extension(conn)
        if not _attached(conn):
            conn.execute("ATTACH DATABASE ? AS vec", (str(config.VEC_DB_PATH),))
            attached_here = True
        n = 0
        for i in range(0, len(ids), chunk):
            part = ids[i:i + chunk]
            marks = ",".join("?" * len(part))
            n += conn.execute(
                f"DELETE FROM vec.segments_vec WHERE segment_id IN ({marks})", part
            ).rowcount
        return n
    except (VecUnavailable, sqlite3.DatabaseError) as e:
        logger.warning("could not drop stale vectors: %s", e)
        return 0
    finally:
        if attached_here:
            try:
                conn.execute("DETACH DATABASE vec")
            except sqlite3.DatabaseError:
                pass


@contextmanager
def embed_lock():
    """STATE_DIR/embed.lock の非ブロッキング排他。取れなければ False を yield する。"""
    config.ensure_state_dir()
    fd = os.open(str(config.STATE_DIR / "embed.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def try_embed_pending(*, allow_download=False, limit=None):
    """backfill / SessionEnd 用: 失敗は握りつぶして (count, 理由|None) を返す。"""
    from myak.db import require_current

    if not config.DB_PATH.exists():
        return 0, "no database"
    with embed_lock() as acquired:
        if not acquired:
            return 0, "another embed is running"
        conn = config.get_connection()
        try:
            if not require_current(conn):
                return 0, "schema not current"
            embedder = default_embedder(local_files_only=not allow_download)
            return embed_pending(conn, embedder, limit=limit), None
        except VecUnavailable as e:
            return 0, str(e)
        except Exception as e:  # noqa: BLE001
            return 0, f"embed failed: {e}"
        finally:
            conn.close()


def _discard_vec_db(conn):
    if _attached(conn):
        conn.execute("DETACH DATABASE vec")
    try:
        config.VEC_DB_PATH.unlink()
    except FileNotFoundError:
        pass


def run_embed(limit=None, model=None, rebuild=False):
    """`myak embed` 本体。終了コードを返す (依存欠如・不正なモデル指定は 2)。"""
    import sys

    if config.vec_disabled():
        print("Vector search is disabled by MYAK_DISABLE_VEC.", file=sys.stderr)
        return 2
    try:
        import fastembed  # noqa: F401
        import sqlite_vec  # noqa: F401
    except ImportError:
        print("myak[vec] is not installed. Run: pip install 'myak[vec]'", file=sys.stderr)
        return 2
    if model and model not in KNOWN_DIMS:
        print(
            f"Unknown model {model!r}. Choose one of: {', '.join(sorted(KNOWN_DIMS))}",
            file=sys.stderr,
        )
        return 2
    existing = indexed_model()
    if model and existing and model != existing and not rebuild:
        print(
            f"The index was built with {existing}; switching to {model} discards it. "
            "Re-run with --rebuild.",
            file=sys.stderr,
        )
        return 2
    model_id = model or existing or config.VEC_MODEL
    from myak.db import require_current

    if not config.DB_PATH.exists():
        print("No database; nothing to embed.", file=sys.stderr)
        return 1
    with embed_lock() as acquired:
        if not acquired:
            print("another embed is running", file=sys.stderr)
            return 1
        conn = config.get_connection()
        try:
            if not require_current(conn):
                print("Database schema is not current; run `myak migrate`.", file=sys.stderr)
                return 1
            embedder = FastEmbedEmbedder(model_id=model_id, cache_dir=config.MODELS_DIR)
            if rebuild:
                # 派生キャッシュなので破棄して作り直す (破損ファイルからの復旧も兼ねる)
                _discard_vec_db(conn)
            # 実際にロード/推論できたことを確認してからマーカーを書く (models_cached の根拠)
            probe_dim = len(embedder.embed(["ready"])[0])
            write_ready_marker(embedder.model_id, probe_dim)
            n = embed_pending(conn, embedder, limit=limit, allow_rebuild=True)
        except VecUnavailable as e:
            print(f"Vector search unavailable: {e}", file=sys.stderr)
            return 2
        finally:
            conn.close()
    print(f"Embedded {n} segments ({embedder.model_id})")
    return 0
