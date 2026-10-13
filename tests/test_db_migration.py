"""myak.db のスキーマ作成・マイグレーションのテスト。"""

import sqlite3

import pytest

from myak import db

V0_SCHEMA = """
    CREATE TABLE IF NOT EXISTS sessions (
        session_id TEXT PRIMARY KEY,
        project_path TEXT,
        started_at TEXT,
        ended_at TEXT,
        segment_count INTEGER DEFAULT 0,
        indexed_at TEXT
    );

    CREATE TABLE IF NOT EXISTS segments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp TEXT,
        FOREIGN KEY (session_id) REFERENCES sessions(session_id)
    );

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


def make_v0(path):
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(V0_SCHEMA)
    sessions = [
        ("s1", "proj-a"),
        ("s2", "proj-b"),
        ("desktop-20260101-000000", "claude-desktop"),
    ]
    for sid, proj in sessions:
        conn.execute(
            "INSERT INTO sessions(session_id, project_path, started_at, segment_count) "
            "VALUES (?,?,?,?)",
            (sid, proj, "2026-01-01T00:00:00Z", 2),
        )
    rows = [
        ("s1", "user", "alpha unique-token-zebra discussion about retries", "t1"),
        ("s1", "assistant", "beta response about exponential backoff handling", "t2"),
        ("s2", "user", "gamma question regarding database migrations", "t3"),
        ("s2", "assistant", "delta answer on rollback strategy details", "t4"),
        ("desktop-20260101-000000", "assistant", "saved note about desktop memory", "t5"),
    ]
    for r in rows:
        conn.execute(
            "INSERT INTO segments(session_id, role, content, timestamp) VALUES (?,?,?,?)", r
        )
    return conn


@pytest.fixture
def v0(tmp_path):
    conn = make_v0(tmp_path / "memory.db")
    yield conn
    conn.close()


def counts(conn):
    return (
        conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0],
    )


def test_fresh_db_created_at_v1(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "n.db"), isolation_level=None)
    db.create_schema(conn)
    assert db.schema_version(conn) == 2
    cols = {r[1] for r in conn.execute("PRAGMA table_info(segments)")}
    assert {"uuid", "agent_id", "agent_type"} <= cols
    assert "cwd" in {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert db.require_current(conn)
    # 再実行は no-op
    db.create_schema(conn)
    assert db.schema_version(conn) == 2


def test_create_schema_leaves_v0_untouched(v0):
    db.create_schema(v0)
    assert db.schema_version(v0) == 0
    assert not db.require_current(v0)


def test_migrate_v0_to_v1(v0, tmp_path):
    before = counts(v0)
    result = db.migrate(v0)
    assert result["status"] == "migrated"
    assert db.schema_version(v0) == 2
    assert counts(v0) == before == (3, 5)
    assert (result["sessions"], result["segments"]) == before
    hit = v0.execute(
        "SELECT rowid FROM segments_fts WHERE segments_fts MATCH '\"unique-token-zebra\"'"
    ).fetchall()
    assert len(hit) == 1
    idx = v0.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='seg_uuid'"
    ).fetchone()
    assert idx and "WHERE uuid IS NOT NULL" in idx[0]
    assert v0.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    row = v0.execute("SELECT uuid, agent_id, agent_type FROM segments LIMIT 1").fetchone()
    assert row == (None, "", None)

    backup = result["backup"]
    assert backup and "memory.db.bak-v0-" in backup
    b = sqlite3.connect(backup)
    assert counts(b) == before
    assert b.execute("PRAGMA user_version").fetchone()[0] == 0
    b.close()


def test_migrate_custom_backup_dir(v0, tmp_path):
    result = db.migrate(v0, backup_dir=tmp_path / "bk")
    assert (tmp_path / "bk").is_dir()
    assert result["backup"].startswith(str(tmp_path / "bk"))


def test_migrate_default_backup_in_state_dir_0600_and_pruned(tmp_path, monkeypatch):
    from myak import config

    state = tmp_path / "st"
    monkeypatch.setattr(config, "STATE_DIR", state)
    for i in range(5):
        path = tmp_path / f"d{i}" / "memory.db"
        path.parent.mkdir()
        conn = make_v0(path)
        conn.isolation_level = None
        result = db.migrate(conn)
        conn.close()
        assert result["backup"].startswith(str(state / "backups"))
        assert oct(__import__("os").stat(result["backup"]).st_mode & 0o777) == "0o600"
        __import__("time").sleep(0.01)
    assert len(list((state / "backups").glob("memory.db.bak-v0-*"))) == db.KEEP_BACKUPS


def test_migrate_idempotent(v0):
    db.migrate(v0)
    again = db.migrate(v0)
    assert again == {"status": "already-current", "version": 2}


def test_migrate_empty_db_creates(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "e.db"), isolation_level=None)
    assert db.migrate(conn)["status"] == "created"
    assert db.schema_version(conn) == 2


def test_schema_too_new(v0):
    v0.execute("PRAGMA user_version=99")
    with pytest.raises(db.SchemaTooNew):
        db.migrate(v0)
    assert not db.require_current(v0)


def test_fault_injection_rolls_back(v0, monkeypatch):
    before = counts(v0)
    real = db._exec
    calls = []

    def flaky(conn, sql):
        calls.append(sql)
        if len(calls) == 2:
            raise RuntimeError("boom")
        real(conn, sql)

    monkeypatch.setattr(db, "_exec", flaky)
    with pytest.raises(RuntimeError):
        db.migrate(v0)
    assert len(calls) == 2  # first ALTER ran, second failed
    assert db.schema_version(v0) == 0
    assert counts(v0) == before
    assert "uuid" not in {r[1] for r in v0.execute("PRAGMA table_info(segments)")}
    assert not v0.in_transaction


def test_migrate_with_default_isolation_conn(tmp_path):
    make_v0(tmp_path / "m.db").close()
    conn = sqlite3.connect(str(tmp_path / "m.db"))  # default isolation level
    assert db.migrate(conn)["status"] == "migrated"
    conn.close()


def test_integrity_failure_blocks_migration(v0, monkeypatch):
    def bad(conn):
        raise db.MigrationError("corrupt")

    monkeypatch.setattr(db, "_check_integrity", bad)
    with pytest.raises(db.MigrationError):
        db.migrate(v0)
    assert db.schema_version(v0) == 0


def test_count_mismatch_detected(v0, monkeypatch):
    seq = iter([{"sessions": 1, "segments": 1}, {"sessions": 2, "segments": 2}])
    monkeypatch.setattr(db, "_counts", lambda c: next(seq))
    with pytest.raises(db.MigrationError):
        db.migrate(v0)


def test_get_connection_settings(db_env):
    from myak import config

    db_env.memory_dir.mkdir(parents=True)
    conn = config.get_connection()
    try:
        assert conn.isolation_level is None
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()


def test_create_schema_is_atomic(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "a.db"), isolation_level=None)
    real = db._statements

    def broken(script):
        stmts = real(script)
        return stmts[:-1] + ["CREATE TABLE ("]  # 最後の文が壊れている

    monkeypatch.setattr(db, "_statements", broken)
    with pytest.raises(sqlite3.OperationalError):
        db.create_schema(conn)
    assert not conn.in_transaction
    assert db.schema_version(conn) == 0
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0


def test_create_schema_default_isolation_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "d.db"))
    db.create_schema(conn)
    assert db.schema_version(conn) == 2
    assert not conn.in_transaction


def test_post_commit_failure_message_includes_backup_path(v0, tmp_path, monkeypatch):
    calls = []
    real = db._check_integrity

    def second_fails(conn):
        calls.append(1)
        if len(calls) == 2:
            raise db.MigrationError("integrity_check failed: boom")
        real(conn)

    monkeypatch.setattr(db, "_check_integrity", second_fails)
    with pytest.raises(db.MigrationError) as exc:
        db.migrate(v0, backup_dir=tmp_path / "bk")
    msg = str(exc.value)
    assert "boom" in msg and "backup:" in msg
    backup = msg.split("backup: ")[1].rstrip(")")
    assert (tmp_path / "bk") in __import__("pathlib").Path(backup).parents


def test_count_mismatch_message_includes_backup_path(v0, tmp_path, monkeypatch):
    seq = iter([{"sessions": 1, "segments": 1}, {"sessions": 2, "segments": 2}])
    monkeypatch.setattr(db, "_counts", lambda c: next(seq))
    with pytest.raises(db.MigrationError, match=r"backup: .*\.bak-v0-"):
        db.migrate(v0, backup_dir=tmp_path / "bk")


def _idx(conn):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='sessions_cwd_ended'"
    ).fetchone()


def make_v1(path):
    conn = make_v0(path)
    for sql in db._V1_STEPS:
        conn.execute(sql)
    conn.execute("PRAGMA user_version=1")
    return conn


def test_fresh_db_has_cwd_index(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "n.db"), isolation_level=None)
    db.create_schema(conn)
    assert _idx(conn)


def test_migrate_v1_to_v2_preserves_counts(tmp_path):
    conn = make_v1(tmp_path / "memory.db")
    before = counts(conn)
    assert not _idx(conn)
    result = db.migrate(conn, backup_dir=tmp_path / "bk")
    assert result["status"] == "migrated" and result["from"] == 1
    assert db.schema_version(conn) == 2
    assert counts(conn) == before == (3, 5)
    assert _idx(conn)
    assert list((tmp_path / "bk").glob("memory.db.bak-v1-*"))
    conn.close()


def test_migrate_v0_gets_cwd_index(v0):
    db.migrate(v0)
    assert _idx(v0)
