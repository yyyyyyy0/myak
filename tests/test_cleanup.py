"""myak clean-legacy (cleanup.py)。"""

import sqlite3

import pytest

from myak import cleanup, cli, config
from myak.db import create_schema, make_backup

LONG = "this is a perfectly fine legacy message with enough length to keep around"


def seed(conn, sid, rows, uuid_for=None):
    conn.execute(
        "INSERT OR IGNORE INTO sessions(session_id, project_path, segment_count) VALUES (?,?,?)",
        (sid, "proj", len(rows)),
    )
    for role, content, uuid in rows:
        conn.execute(
            "INSERT INTO segments(session_id, role, content, timestamp, uuid) "
            "VALUES (?,?,?,?,?)", (sid, role, content, "t", uuid))


def fts(conn, term):
    return conn.execute(
        "SELECT COUNT(*) FROM segments_fts WHERE segments_fts MATCH ?", (f'"{term}"',)
    ).fetchone()[0]


@pytest.fixture
def db(db_env):
    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    conn = config.get_connection()
    create_schema(conn)
    seed(conn, "s1", [
        ("user", "<command-name>/clear</command-name> zebra-dropped-marker " + "x" * 40, None),
        ("user", "<system-reminder>reminder-marker " + "y" * 80 + "</system-reminder>", None),
        ("user", "<task-notification>notif-marker</task-notification>", None),
        ("user", "short", None),
        ("assistant", LONG + " keep-marker", None),
        ("user", "<system-reminder>strip-me-marker</system-reminder>" + LONG + " tail-marker",
         None),
    ])
    seed(conn, "s2", [("user", "<local-command-stdout>" + "q" * 60, None)])  # all dropped
    seed(conn, "desktop-1", [("assistant", "<system-reminder>desk-marker</system-reminder>", None)])
    seed(conn, "s3", [("user", "<system-reminder>uuid-marker</system-reminder>", "u-1"),
                      ("assistant", LONG + " fine", "u-2")])
    yield db_env, conn
    conn.close()


def test_dry_run_changes_nothing(db, capsys):
    env, conn = db
    before = conn.execute("SELECT id, content FROM segments ORDER BY id").fetchall()
    r = cleanup.clean_legacy(conn, dry_run=True)
    assert (r["deleted"], r["updated"]) == (5, 1)
    assert 0 < len(r["examples"]) <= 5
    assert conn.execute("SELECT id, content FROM segments ORDER BY id").fetchall() == before
    assert cli.main(["clean-legacy", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would delete: 5" in out and "would update: 1" in out
    assert not (config.STATE_DIR / "backups").exists()


def test_clean_applies_and_is_idempotent(db, tmp_path):
    env, conn = db
    r = cleanup.clean_legacy(conn, backup_dir=tmp_path / "bk")
    assert (r["deleted"], r["updated"]) == (5, 1)
    assert r["sessions_removed"] == 1  # s2
    assert r["backup"] and (tmp_path / "bk").exists()
    assert fts(conn, "zebra-dropped-marker") == 0
    assert fts(conn, "reminder-marker") == 0
    assert fts(conn, "strip-me-marker") == 0
    assert fts(conn, "tail-marker") == 1 and fts(conn, "keep-marker") == 1
    content = conn.execute(
        "SELECT content FROM segments WHERE content LIKE '%tail-marker%'").fetchone()[0]
    assert content.startswith(LONG[:20])
    sess = dict(conn.execute("SELECT session_id, segment_count FROM sessions").fetchall())
    assert sess == {"s1": 2, "desktop-1": 1, "s3": 2}
    # desktop / uuid rows untouched
    assert fts(conn, "desk-marker") == 1 and fts(conn, "uuid-marker") == 1
    again = cleanup.clean_legacy(conn, backup_dir=tmp_path / "bk")
    assert (again["deleted"], again["updated"], again["sessions_removed"]) == (0, 0, 0)


def test_refuses_non_current_schema(db):
    env, conn = db
    conn.execute("PRAGMA user_version=1")
    with pytest.raises(ValueError):
        cleanup.clean_legacy(conn)


def test_rollback_on_failure(db, tmp_path, monkeypatch):
    env, conn = db
    before = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]

    monkeypatch.setattr(cleanup, "_scan", lambda c: ([1], [], [], {"s1"}))
    orig = conn.executemany

    class Wrap:
        def __init__(self, c):
            self.c = c

        def __getattr__(self, n):
            return getattr(self.c, n)

        def executemany(self, *a):
            orig(*a)
            raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        cleanup.clean_legacy(Wrap(conn), backup_dir=tmp_path / "bk")
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == before
    assert not conn.in_transaction


def test_cli_run(db, capsys):
    assert cli.main(["clean-legacy"]) == 0
    out = capsys.readouterr().out
    assert "did delete: 5" in out and "Backup:" in out
    assert list((config.STATE_DIR / "backups").glob("memory.db.bak-*clean*"))
    assert cli.main(["clean-legacy"]) == 0
    assert "did delete: 0" in capsys.readouterr().out


def test_cli_no_db_and_old_schema(db_env, capsys):
    assert cli.main(["clean-legacy"]) == 1
    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    c = config.get_connection()
    c.execute("CREATE TABLE segments(x)")
    c.close()
    assert cli.main(["clean-legacy"]) == 1
    assert "myak migrate" in capsys.readouterr().err


def test_make_backup_in_memory_db_returns_none():
    c = sqlite3.connect(":memory:")
    assert make_backup(c, "v2") is None


CAVEAT = (
    "<local-command-caveat>Caveat: The messages below were generated by the user while running "
    "local commands. DO NOT respond to these messages or otherwise consider them in your "
    "response unless the user explicitly asks you to.</local-command-caveat>"
)


def test_caveat_row_is_deleted_by_cleanup_and_dropped_live(db_env):
    from myak.transcript import DROP_TAGS, record_to_segment

    assert "local-command-caveat" in DROP_TAGS
    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    conn = config.get_connection()
    create_schema(conn)
    seed(conn, "s1", [("user", CAVEAT + "z" * 200, None), ("assistant", LONG, None)])
    r = cleanup.clean_legacy(conn)
    assert (r["deleted"], r["updated"]) == (1, 0)
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 1
    conn.close()
    rec = {"type": "user", "uuid": "u", "timestamp": "t",
           "message": {"role": "user", "content": CAVEAT + "z" * 200}}
    assert record_to_segment(rec) is None


def test_migrate_backup_survives_repeated_clean_runs(db, tmp_path):
    env, conn = db
    bk = tmp_path / "bk"
    migrate_bak = make_backup(conn, "v1", bk)
    for _ in range(db_keep() + 2):
        cleanup.clean_legacy(conn, backup_dir=bk)
    assert migrate_bak.exists()
    cleans = list(bk.glob("memory.db.bak-clean-*"))
    assert len(cleans) == db_keep()
    assert list(bk.glob("memory.db.bak-v1-*")) == [migrate_bak]


def db_keep():
    from myak.db import KEEP_BACKUPS

    return KEEP_BACKUPS


def test_isolation_level_restored(db, tmp_path):
    env, conn = db
    conn.isolation_level = ""
    cleanup.clean_legacy(conn, backup_dir=tmp_path / "bk")
    assert conn.isolation_level == ""
    cleanup.clean_legacy(conn, dry_run=True)
    assert conn.isolation_level == ""
    conn.isolation_level = None


def test_busy_database_exits_3(db, capsys, monkeypatch):
    def busy(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(cleanup, "clean_legacy", busy)
    assert cli.main(["clean-legacy"]) == 3
    assert "database busy, retry" in capsys.readouterr().err


def test_stale_vectors_dropped_for_deleted_and_updated_rows(db, tmp_path):
    pytest.importorskip("sqlite_vec")
    from myak import vec
    from tests.fakes import FakeEmbedder

    env, conn = db
    vec.embed_pending(conn, FakeEmbedder())
    all_ids = {r[0] for r in conn.execute("SELECT id FROM segments")}
    r = cleanup.clean_legacy(conn, backup_dir=tmp_path / "bk")
    assert (r["deleted"], r["updated"]) == (5, 1)
    left = {r[0] for r in conn.execute("SELECT segment_id FROM vec.segments_vec")}
    live = {r[0] for r in conn.execute("SELECT id FROM segments")}
    assert not (left - live)  # 消えた行のベクトルは残らない
    assert len(left) == len(all_ids) - 5 - 1  # 更新行も消して再埋め込み対象にする
    assert vec.embed_pending(conn, FakeEmbedder()) == 1


def test_forget_segments_noop_without_vec_db(db):
    from myak import vec

    env, conn = db
    assert vec.forget_segments(conn, [1, 2]) == 0
    assert vec.forget_segments(conn, []) == 0


def test_forget_segments_survives_garbage_vec_db(db):
    from myak import vec

    env, conn = db
    config.VEC_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.VEC_DB_PATH.write_bytes(bytes(range(256)) * 40)
    assert vec.forget_segments(conn, [1]) == 0
