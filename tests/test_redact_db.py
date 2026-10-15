"""myak redact-secrets (redact_db.py): 既存 DB への遡及的な伏せ字化。"""

import sqlite3

import pytest

from myak import cli, config, redact_db
from myak.db import create_schema
from tests.test_redact import B64URL, Fake, discord_token, rand

LONG = "this is a perfectly ordinary message that mentions tokens and passwords in prose"
BOT = discord_token()
ANT = Fake("sk-ant-api03-" + rand(80, B64URL))


def seed(conn, sid, contents):
    conn.execute(
        "INSERT OR IGNORE INTO sessions(session_id, project_path, segment_count) VALUES (?,?,?)",
        (sid, "proj", len(contents)),
    )
    for i, content in enumerate(contents):
        conn.execute(
            "INSERT INTO segments(session_id, role, content, timestamp, uuid) VALUES (?,?,?,?,?)",
            (sid, "user", content, "t", f"{sid}-{i}"),
        )


def fts(conn, term):
    return conn.execute(
        "SELECT COUNT(*) FROM segments_fts WHERE segments_fts MATCH ?", (f'"{term}"',)
    ).fetchone()[0]


def contents(conn):
    return [r[0] for r in conn.execute("SELECT content FROM segments ORDER BY id")]


@pytest.fixture
def db(db_env):
    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    conn = config.get_connection()
    create_schema(conn)
    seed(conn, "s1", [LONG, f"bot token is {BOT} keep-marker", LONG + " two"])
    seed(conn, "desktop-1", [f"export ANTHROPIC_API_KEY={ANT}\nand DISCORD={BOT}"])
    seed(conn, "s2", [LONG + " three"])
    yield db_env, conn
    conn.close()


def test_dry_run_reports_counts_and_changes_nothing(db, capsys):
    env, conn = db
    before = contents(conn)
    r = redact_db.redact_secrets(conn, dry_run=True)
    assert (r["segments"], r["sessions"]) == (2, 2)
    assert r["by_type"] == {"anthropic_api_key": 1, "discord_bot_token": 2}
    assert r["ids"] == [2, 4] and r["backup"] is None
    assert contents(conn) == before

    assert cli.main(["redact-secrets", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would redact: 2 segments in 2 sessions" in out
    assert "discord_bot_token: 2" in out and "segment ids: 2, 4" in out
    assert (BOT in out or ANT in out) is False
    assert not (config.STATE_DIR / "backups").exists()
    assert contents(conn) == before


def test_apply_redacts_rebuilds_fts_and_purges_file(db, tmp_path):
    env, conn = db
    r = redact_db.redact_secrets(conn, backup_dir=tmp_path / "bk")
    assert (r["segments"], r["sessions"]) == (2, 2)
    assert r["vacuumed"] and r["vacuum_error"] is None
    rows = contents(conn)
    assert not any(BOT in c or ANT in c for c in rows)
    assert rows[1] == "bot token is [REDACTED:discord_bot_token] keep-marker"
    assert rows[0] == LONG and rows[2] == LONG + " two"  # 無関係な行は変えない
    assert fts(conn, BOT[:20]) == 0 and fts(conn, ANT[:20]) == 0
    assert fts(conn, "keep-marker") == 1 and fts(conn, "REDACTED:discord") == 2
    conn.execute("INSERT INTO segments_fts(segments_fts) VALUES('integrity-check')")

    # 物理ファイル上にも平文が残っていない (旧ページ・FTS セグメント含む)
    raw = env.db_path.read_bytes()
    assert (BOT.encode() in raw) is False and (ANT.encode() in raw) is False
    # バックアップには元の平文が残る (だから利用者に削除を促す)
    backup = r["backup"]
    assert backup and "bak-redact-" in backup
    with open(backup, "rb") as f:
        assert (BOT.encode() in f.read()) is True

    again = redact_db.redact_secrets(conn, backup_dir=tmp_path / "bk")
    assert again["segments"] == 0 and again["backup"] is None
    assert len(list((tmp_path / "bk").iterdir())) == 1  # 何もない時はバックアップも取らない


def test_refuses_non_current_schema(db):
    env, conn = db
    conn.execute("PRAGMA user_version=1")
    with pytest.raises(ValueError):
        redact_db.redact_secrets(conn)


def test_rollback_on_failure(db, tmp_path, monkeypatch):
    env, conn = db
    before = contents(conn)
    real_scan = redact_db._scan
    calls = []

    def scan_then_break(c, *a):
        calls.append(1)
        updates, totals, sessions, max_id = real_scan(c, *a)
        if len(calls) == 2:  # ロック下の取り直しで壊す
            return updates + [(None, 1)], totals, sessions, max_id  # content NOT NULL 違反
        return updates, totals, sessions, max_id

    monkeypatch.setattr(redact_db, "_scan", scan_then_break)
    with pytest.raises(sqlite3.IntegrityError):
        redact_db.redact_secrets(conn, backup_dir=tmp_path / "bk")
    assert contents(conn) == before
    assert not conn.in_transaction


def test_isolation_level_restored(db, tmp_path):
    env, conn = db
    conn.isolation_level = ""
    redact_db.redact_secrets(conn, dry_run=True)
    assert conn.isolation_level == ""
    redact_db.redact_secrets(conn, backup_dir=tmp_path / "bk")
    assert conn.isolation_level == ""
    conn.isolation_level = None


def test_vacuum_failure_is_reported_not_fatal(db, tmp_path, monkeypatch):
    env, conn = db

    class Wrap:
        def __init__(self, c):
            self.c = c

        def __getattr__(self, n):
            return getattr(self.c, n)

        def execute(self, sql, *a):
            if sql == "VACUUM":
                raise sqlite3.OperationalError("database is locked")
            return self.c.execute(sql, *a)

    r = redact_db.redact_secrets(Wrap(conn), backup_dir=tmp_path / "bk")
    assert r["segments"] == 2 and not r["vacuumed"]
    assert "locked" in r["vacuum_error"]
    assert not any(BOT in c for c in contents(conn))


def test_stale_vectors_dropped_and_reembedded(db, tmp_path):
    from tests._vec_skip import require_sqlite_vec

    require_sqlite_vec()
    from myak import vec
    from tests.fakes import FakeEmbedder

    env, conn = db
    assert vec.embed_pending(conn, FakeEmbedder()) == 5
    r = redact_db.redact_secrets(conn, backup_dir=tmp_path / "bk")
    assert r["vectors_dropped"] == 2
    left = {row[0] for row in conn.execute("SELECT segment_id FROM vec.segments_vec")}
    assert left == {1, 3, 5}
    assert vec.embed_pending(conn, FakeEmbedder()) == 2


def test_cli_apply(db, capsys, monkeypatch):
    from myak import vec

    calls = []

    def fake_embed(limit=None, **kw):
        calls.append(limit)
        return 2, None

    monkeypatch.setattr(vec, "try_embed_pending", fake_embed)
    monkeypatch.setattr(vec, "forget_segments", lambda c, ids: len(ids))
    assert cli.main(["redact-secrets"]) == 0
    out = capsys.readouterr().out
    assert "did redact: 2 segments in 2 sessions" in out
    assert "delete it once verified" in out and "vacuumed" in out
    assert "Backups that still contain secrets" in out and "bak-redact-" in out
    assert "Not touched:" in out
    assert "Vectors dropped: 2" in out and "Re-embedded: 2" in out
    assert calls == [config.HOOK_EMBED_LIMIT]
    assert (BOT in out or ANT in out) is False
    assert list((config.STATE_DIR / "backups").glob("memory.db.bak-redact-*"))

    assert cli.main(["redact-secrets"]) == 0
    out = capsys.readouterr().out
    assert "did redact: 0 segments" in out and "Backup (still holds" not in out
    assert "bak-redact-" in out  # 平文入りのバックアップは消すまで一覧に出続ける


def test_cli_reports_skipped_reembed_and_vacuum_failure(db, capsys, monkeypatch):
    from myak import vec

    real = redact_db.redact_secrets

    def with_failed_vacuum(conn, **kw):
        r = real(conn, **kw)
        return {**r, "vacuumed": False, "vacuum_error": "database is locked",
                "vectors_dropped": 1}

    monkeypatch.setattr(redact_db, "redact_secrets", with_failed_vacuum)
    monkeypatch.setattr(vec, "try_embed_pending", lambda limit=None: (0, "no model"))
    assert cli.main(["redact-secrets"]) == redact_db.EXIT_VACUUM_FAILED
    out = capsys.readouterr().out
    assert "VACUUM failed (database is locked)" in out
    assert f"Retry with: sqlite3 {config.DB_PATH} VACUUM" in out
    assert "Re-embedding skipped (no model); run `myak embed`." in out


def test_cli_no_db_old_schema_and_busy(db_env, capsys, monkeypatch):
    assert cli.main(["redact-secrets", "--dry-run"]) == 1
    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    c = config.get_connection()
    c.execute("CREATE TABLE segments(x)")
    c.close()
    assert cli.main(["redact-secrets"]) == 1
    assert "myak migrate" in capsys.readouterr().err

    def busy(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(redact_db, "redact_secrets", busy)
    assert cli.main(["redact-secrets"]) == 3
    assert "database busy, retry" in capsys.readouterr().err


def test_cli_integrity_failure_exits_1(db, capsys, monkeypatch):
    from myak import db as dbmod

    def bad(conn):
        raise dbmod.MigrationError("integrity_check failed")

    monkeypatch.setattr(dbmod, "_check_integrity", bad)
    assert cli.main(["redact-secrets"]) == 1
    err = capsys.readouterr().err
    assert "integrity_check failed" in err and "backup:" in err


def test_redact_backups_are_pruned_separately(db, tmp_path):
    from myak.db import KEEP_BACKUPS, make_backup

    env, conn = db
    bk = tmp_path / "bk"
    migrate_bak = make_backup(conn, "v1", bk)
    for _ in range(KEEP_BACKUPS + 2):
        make_backup(conn, "redact", bk)
    assert migrate_bak.exists()
    assert len(list(bk.glob("memory.db.bak-redact-*"))) == KEEP_BACKUPS


def test_rows_added_after_first_pass_are_redacted_under_lock(db, tmp_path, monkeypatch):
    from myak import db as dbmod

    env, conn = db
    real_backup = dbmod.make_backup
    late = discord_token("1234567890123456789", 9)

    def backup_then_hook_writes(c, tag, backup_dir=None):
        path = real_backup(c, tag, backup_dir)
        seed(c, "s9", [f"late row {late} from a concurrent hook"])  # 1 回目の走査の後に追加
        return path

    monkeypatch.setattr(dbmod, "make_backup", backup_then_hook_writes)
    r = redact_db.redact_secrets(conn, backup_dir=tmp_path / "bk")
    assert r["ids"] == [2, 4, 6] and r["sessions"] == 3
    assert not any(late in c for c in contents(conn))


def test_dry_run_lists_backups_that_still_hold_secrets(db, capsys):
    from myak.db import make_backup

    env, conn = db
    old = make_backup(conn, "v1")
    (old.parent / "memory.db.bak-v0-garbage").write_bytes(b"not a database" * 100)
    assert cli.main(["redact-secrets", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert f"  {old} (2 segments)" in out
    assert "garbage" not in out
    assert (BOT in out or ANT in out) is False


def test_cli_refuses_while_embed_runs(db, capsys):
    from myak import vec

    env, conn = db
    before = contents(conn)
    with vec.embed_lock() as acquired:
        assert acquired
        assert cli.main(["redact-secrets"]) == 3
    assert "another embed is running" in capsys.readouterr().err
    assert contents(conn) == before
