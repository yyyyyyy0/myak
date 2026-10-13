"""ベクトル索引 (任意機能 `myak[vec]`)。sqlite-vec + fastembed を遅延 import する。

ベクトルは別ファイル memory.vec.db (派生キャッシュ) に置き、ATTACH して使う。
依存/拡張/モデルが無ければ VecUnavailable を送出し、呼び出し側は FTS のみに縮退する。
"""

import logging
import sqlite3
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


def models_cached(model_id, cache_dir=None):
    """モデルが既にローカルにあるか (ダウンロードはしない)。"""
    cache_dir = cache_dir or config.MODELS_DIR
    needle = model_id.replace("/", "--")
    try:
        return any(needle in p.name for p in cache_dir.iterdir())
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


def default_embedder(*, local_files_only=True):
    """フック/検索経路用: 拡張・モデルが無ければ VecUnavailable。ダウンロードしない。"""
    if config.vec_disabled():
        raise VecUnavailable("disabled by MYAK_DISABLE_VEC")
    if local_files_only and not models_cached(config.VEC_MODEL):
        raise VecUnavailable("embedding model not downloaded (run `myak embed`)")
    return FastEmbedEmbedder(local_files_only=local_files_only)


def _load_extension(conn):
    try:
        import sqlite_vec
    except ImportError as e:
        raise VecUnavailable("sqlite-vec is not installed (pip install 'myak[vec]')") from e
    if not hasattr(conn, "enable_load_extension"):
        raise VecUnavailable("this Python's sqlite3 cannot load extensions")
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (sqlite3.Error, AttributeError, OSError) as e:
        raise VecUnavailable(f"cannot load sqlite-vec: {e}") from e


def _attached(conn):
    return any(row[1] == "vec" for row in conn.execute("PRAGMA database_list"))


def open_vec(conn, embedder):
    """vec DB を ATTACH し sqlite-vec を読み込む。テーブルが無い/モデル不一致なら作り直す。"""
    if config.vec_disabled():
        raise VecUnavailable("disabled by MYAK_DISABLE_VEC")
    _load_extension(conn)
    path = config.VEC_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not _attached(conn):
        conn.execute("ATTACH DATABASE ? AS vec", (str(path),))
        config.secure_file(path)
    dim = embedder.dim
    conn.execute("CREATE TABLE IF NOT EXISTS vec.embedding_meta(model_id TEXT, dim INTEGER)")
    meta = conn.execute("SELECT model_id, dim FROM vec.embedding_meta").fetchone()
    if meta is not None and tuple(meta) != (embedder.model_id, dim):
        logger.warning("embedding model changed %s -> %s; rebuilding", meta, embedder.model_id)
        conn.execute("DROP TABLE IF EXISTS vec.segments_vec")
        conn.execute("DELETE FROM vec.embedding_meta")
        meta = None
    if meta is None:
        conn.execute("DELETE FROM vec.embedding_meta")
        conn.execute("INSERT INTO vec.embedding_meta VALUES (?, ?)", (embedder.model_id, dim))
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS vec.segments_vec USING vec0("
        f"segment_id INTEGER PRIMARY KEY, embedding float[{int(dim)}])"
    )


def _serialize(vector):
    import sqlite_vec

    return sqlite_vec.serialize_float32(vector)


def segment_text(role, content):
    return f"{role}: {content[:TEXT_CHARS]}"


def embed_pending(conn, embedder, batch=64, limit=None):
    """まだ埋め込んでいない segment を埋め込む。埋め込んだ件数を返す (インクリメンタル)。"""
    open_vec(conn, embedder)
    done = {r[0] for r in conn.execute("SELECT segment_id FROM vec.segments_vec")}
    rows = [
        r for r in conn.execute("SELECT id, role, content FROM segments ORDER BY id")
        if r[0] not in done
    ]
    if limit is not None:
        rows = rows[:limit]
    count = 0
    for i in range(0, len(rows), batch):
        chunk = rows[i:i + batch]
        vectors = embedder.embed([segment_text(r[1], r[2]) for r in chunk])
        conn.execute("BEGIN")
        try:
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
    """クエリに近い segment を [(segment_id, distance)] で返す。"""
    open_vec(conn, embedder)
    vector = embedder.embed([query])[0]
    rows = conn.execute(
        "SELECT segment_id, distance FROM vec.segments_vec "
        "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
        (_serialize(vector), int(k)),
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def try_embed_pending(*, allow_download=False, limit=None):
    """backfill / SessionEnd 用: 失敗は握りつぶして (count, 理由|None) を返す。"""
    from myak.db import require_current

    if not config.DB_PATH.exists():
        return 0, "no database"
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


def run_embed(limit=None, model=None):
    """`myak embed` 本体。終了コードを返す (依存欠如は 2)。"""
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
    if model:
        config.VEC_MODEL = model
    from myak.db import require_current

    if not config.DB_PATH.exists():
        print("No database; nothing to embed.", file=sys.stderr)
        return 1
    conn = config.get_connection()
    try:
        if not require_current(conn):
            print("Database schema is not current; run `myak migrate`.", file=sys.stderr)
            return 1
        embedder = FastEmbedEmbedder(model_id=config.VEC_MODEL)
        n = embed_pending(conn, embedder, limit=limit)
    except VecUnavailable as e:
        print(f"Vector search unavailable: {e}", file=sys.stderr)
        return 2
    finally:
        conn.close()
    print(f"Embedded {n} segments ({embedder.model_id})")
    return 0
