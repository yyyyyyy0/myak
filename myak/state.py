"""機械ローカルのインクリメンタル索引カーソル (同期対象外の別 SQLite)。"""

import hashlib
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from myak import config

FINGERPRINT_BYTES = 4096


def _connect():
    path = config.STATE_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        os.close(os.open(str(path), os.O_WRONLY | os.O_CREAT, 0o600))
    config.secure_file(path)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=5)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cursors ("
        "path TEXT PRIMARY KEY, offset INTEGER, size INTEGER, "
        "first4k_sha1 TEXT, updated_at TEXT)"
    )
    return conn


def fingerprint(path, limit=None):
    """(size, 先頭 min(4096, limit) バイトの sha1) を返す。"""
    n = FINGERPRINT_BYTES if limit is None else min(FINGERPRINT_BYTES, limit)
    with open(path, "rb") as f:
        head = f.read(n)
        size = f.seek(0, 2)
    return size, hashlib.sha1(head).hexdigest()


def get_cursor(path):
    """{"offset", "size", "sha1"} or None。"""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT offset, size, first4k_sha1 FROM cursors WHERE path = ?",
            (str(path),),
        ).fetchone()
    if row is None:
        return None
    return {"offset": row[0], "size": row[1], "sha1": row[2]}


def set_cursor(path, offset, size, sha1):
    """単調更新: offset が既存以上の場合のみ全列を更新する。"""
    now = datetime.now(timezone.utc).isoformat()
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO cursors(path, offset, size, first4k_sha1, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET "
            "size = CASE WHEN excluded.offset >= offset THEN excluded.size ELSE size END, "
            "first4k_sha1 = CASE WHEN excluded.offset >= offset "
            "THEN excluded.first4k_sha1 ELSE first4k_sha1 END, "
            "updated_at = excluded.updated_at, "
            "offset = MAX(offset, excluded.offset)",
            (str(path), offset, size, sha1, now),
        )


def clear_cursor(path):
    """1 ファイル分のカーソルを削除する (フルリスキャン前に使う)。"""
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM cursors WHERE path = ?", (str(path),))


def reset_all():
    """全カーソルを削除する (`myak reindex --full`)。"""
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM cursors")
