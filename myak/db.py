"""スキーマ作成・バージョン管理・明示的マイグレーション。

マイグレーションは `migrate()` からのみ実行する (hook は自動 migrate しない)。
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CURRENT_VERSION = 1

_V0_TABLES = """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        project_path TEXT,
        started_at TEXT,
        ended_at TEXT,
        segment_count INTEGER DEFAULT 0,
        indexed_at TEXT{sessions_extra}
    );

    CREATE TABLE IF NOT EXISTS segments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp TEXT{segments_extra},
        FOREIGN KEY (session_id) REFERENCES sessions(session_id)
    );
"""

_FTS_AND_TRIGGERS = """
    CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
        content,
        content='segments',
        content_rowid='id',
        tokenize='trigram'
    );

    CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
        INSERT INTO segments_fts(rowid, content)
        VALUES (new.id, new.content);
    END;

    CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
        INSERT INTO segments_fts(segments_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
    END;

    CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
        INSERT INTO segments_fts(segments_fts, rowid, content)
        VALUES ('delete', old.id, old.content);
        INSERT INTO segments_fts(rowid, content)
        VALUES (new.id, new.content);
    END;
"""

_UUID_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS seg_uuid "
    "ON segments(session_id, agent_id, uuid) WHERE uuid IS NOT NULL"
)

_V1_STEPS = (
    "ALTER TABLE segments ADD COLUMN uuid TEXT",
    "ALTER TABLE segments ADD COLUMN agent_id TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE segments ADD COLUMN agent_type TEXT",
    "ALTER TABLE sessions ADD COLUMN cwd TEXT",
    _UUID_INDEX,
)


class SchemaTooNew(RuntimeError):
    """DB の user_version がこのコードより新しい。"""


class MigrationError(RuntimeError):
    """マイグレーション前後の検証に失敗した。"""


def schema_version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def require_current(conn):
    """hook 用ゲート: スキーマが現行版なら True。False なら呼び出し側がログして exit 0。"""
    return schema_version(conn) == CURRENT_VERSION


def _has_table(conn, name):
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def create_schema(conn):
    """新規 DB を v1 で直接作成する。既存 DB (v0/v1) には何もしない。"""
    if _has_table(conn, "segments"):
        return
    conn.executescript(
        _V0_TABLES.format(
            sessions_extra=",\n        cwd TEXT",
            segments_extra=(
                ",\n        uuid TEXT"
                ",\n        agent_id TEXT NOT NULL DEFAULT ''"
                ",\n        agent_type TEXT"
            ),
        )
        + _FTS_AND_TRIGGERS
        + _UUID_INDEX
        + ";"
    )
    conn.execute(f"PRAGMA user_version={CURRENT_VERSION}")


def _exec(conn, sql):
    conn.execute(sql)


def _db_file(conn):
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return Path(file) if file else None
    return None


def _check_integrity(conn):
    result = conn.execute("PRAGMA integrity_check").fetchall()
    if result != [("ok",)]:
        raise MigrationError(f"integrity_check failed: {result[:5]}")
    try:
        conn.execute("INSERT INTO segments_fts(segments_fts) VALUES('integrity-check')")
    except sqlite3.DatabaseError as e:
        raise MigrationError(f"FTS integrity-check failed: {e}") from e


def _counts(conn):
    return {
        "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        "segments": conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0],
    }


def migrate(conn, *, backup_dir=None):
    """v0 → v1。明示的にのみ呼ぶ。結果 dict を返す。"""
    version = schema_version(conn)
    if version > CURRENT_VERSION:
        raise SchemaTooNew(
            f"database schema v{version} is newer than this code (v{CURRENT_VERSION})"
        )
    if version == CURRENT_VERSION:
        return {"status": "already-current", "version": version}

    if not _has_table(conn, "segments"):
        create_schema(conn)
        return {"status": "created", "version": CURRENT_VERSION}

    if conn.isolation_level is not None:
        conn.commit()
        conn.isolation_level = None

    _check_integrity(conn)
    before = _counts(conn)

    backup_path = None
    db_file = _db_file(conn)
    if db_file is not None:
        dest_dir = Path(backup_dir) if backup_dir is not None else db_file.parent
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup_path = dest_dir / f"{db_file.name}.bak-v{version}-{stamp}"
        dest = sqlite3.connect(str(backup_path))
        try:
            conn.backup(dest)
        finally:
            dest.close()

    conn.execute("BEGIN IMMEDIATE")
    try:
        for sql in _V1_STEPS:
            _exec(conn, sql)
        conn.execute(f"PRAGMA user_version={CURRENT_VERSION}")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

    _check_integrity(conn)
    after = _counts(conn)
    if after != before:
        raise MigrationError(f"row counts changed: {before} -> {after}")
    return {
        "status": "migrated",
        "from": version,
        "version": CURRENT_VERSION,
        "backup": str(backup_path) if backup_path else None,
        **after,
    }
