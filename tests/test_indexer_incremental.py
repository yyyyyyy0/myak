"""Incremental index_session (D2/D5) tests."""

import shutil
import sqlite3

import pytest

from myak import indexer
from tests.idx_helpers import FIXTURES, append, count, dumps, rec, setup_env, text, write


@pytest.fixture
def env(db_env, tmp_path, monkeypatch):
    e = setup_env(db_env, tmp_path, monkeypatch)
    yield e
    e.conn.close()


def base_records(n=2):
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(rec(f"u{i}", role, text(i), ts=f"2026-10-01T12:0{i}:00.000Z"))
    return out


def test_first_index_inserts_with_uuid(env):
    p = write(env.tmp / "s1.jsonl", base_records(2))
    assert indexer.index_session(env.conn, p, "proj") == 2
    rows = env.conn.execute("SELECT uuid, agent_id, agent_type FROM segments").fetchall()
    assert sorted(r[0] for r in rows) == ["u0", "u1"]
    assert all(r[1] == "" and r[2] is None for r in rows)
    s = env.conn.execute(
        "SELECT project_path, segment_count, cwd, started_at, ended_at FROM sessions"
    ).fetchone()
    assert s[0] == "proj" and s[1] == 2 and s[2] == "/Users/x/proj"
    assert s[3] == "2026-10-01T12:00:00.000Z" and s[4] == "2026-10-01T12:01:00.000Z"


def test_append_grows_and_rerun_is_zero(env):
    p = write(env.tmp / "s1.jsonl", base_records(2))
    assert indexer.index_session(env.conn, p) == 2
    assert indexer.index_session(env.conn, p) == 0
    append(p, [rec("u2", "user", text(2)), rec("u3", "assistant", text(3))])
    assert indexer.index_session(env.conn, p) == 2
    assert indexer.index_session(env.conn, p) == 0
    assert count(env.conn) == 4
    assert env.conn.execute("SELECT segment_count FROM sessions").fetchone()[0] == 4


def test_incremental_does_not_reread_old_records(env, monkeypatch):
    p = write(env.tmp / "s1.jsonl", base_records(2))
    indexer.index_session(env.conn, p)
    append(p, [rec("u2", "user", text(2))])
    seen = []
    real = indexer._extract_segments

    def spy(path, start_offset=0, final=False):
        seen.append(start_offset)
        return real(path, start_offset, final)

    monkeypatch.setattr(indexer, "_extract_segments", spy)
    indexer.index_session(env.conn, p)
    assert seen and seen[0] > 0


def test_rewritten_same_size_triggers_rescan_without_dupes(env):
    recs = base_records(2)
    p = write(env.tmp / "s1.jsonl", recs)
    assert indexer.index_session(env.conn, p) == 2
    size = p.stat().st_size
    # same uuids/length, different first bytes (timestamp digits) -> fingerprint mismatch
    recs2 = [dict(r, timestamp="2027-11-02T13:00:00.000Z") for r in recs]
    p.write_bytes(dumps(recs2).encode())
    assert p.stat().st_size == size
    assert indexer.index_session(env.conn, p) == 0  # uuids dedupe
    assert count(env.conn) == 2
    # cursor was reset and re-established: next run uses incremental path
    cur = env.state.get_cursor(p)
    assert cur["offset"] == size


def test_rewritten_shorter_file_rescans(env):
    p = write(env.tmp / "s1.jsonl", base_records(3))
    indexer.index_session(env.conn, p)
    write(p, base_records(2))
    assert indexer.index_session(env.conn, p) == 0
    assert count(env.conn) == 3
    assert env.state.get_cursor(p)["offset"] == p.stat().st_size


def test_cursor_not_at_newline_triggers_rescan(env):
    p = write(env.tmp / "s1.jsonl", base_records(2))
    indexer.index_session(env.conn, p)
    cur = env.state.get_cursor(p)
    # simulate corrupt cursor pointing mid-line (fingerprint still matches)
    env.state.clear_cursor(p)
    env.state.set_cursor(p, cur["offset"] - 5, cur["size"], cur["sha1"])
    assert indexer.index_session(env.conn, p) == 0
    assert count(env.conn) == 2
    assert env.state.get_cursor(p)["offset"] == p.stat().st_size


def test_unterminated_tail_deferred_then_accepted_with_final(env):
    p = write(env.tmp / "s1.jsonl", base_records(1))
    tail = dumps([rec("u9", "user", text(9))]).rstrip("\n")
    with open(p, "ab") as f:
        f.write(tail.encode())
    assert indexer.index_session(env.conn, p) == 1
    assert count(env.conn) == 1
    assert indexer.index_session(env.conn, p, final=True) == 1
    assert count(env.conn) == 2
    # a later append (no newline separator) forces a clean rescan, no dupes
    append(p, [rec("u10", "assistant", text(10))])
    indexer.index_session(env.conn, p)
    assert count(env.conn, where="uuid = 'u9'") == 1


def test_unterminated_tail_completed_later_is_indexed_once(env):
    p = write(env.tmp / "s1.jsonl", base_records(1))
    tail = dumps([rec("u9", "user", text(9))])
    with open(p, "ab") as f:
        f.write(tail[:40].encode())
    assert indexer.index_session(env.conn, p) == 1
    with open(p, "ab") as f:
        f.write(tail[40:].encode())
    indexer.index_session(env.conn, p)
    assert count(env.conn, where="uuid = 'u9'") == 1
    assert count(env.conn) == 2


def test_crlf_file(env):
    p = write(env.tmp / "s1.jsonl", base_records(2), eol="\r\n")
    assert indexer.index_session(env.conn, p) == 2
    append(p, [rec("u2", "user", text(2))], eol="\r\n")
    assert indexer.index_session(env.conn, p) == 1
    assert count(env.conn) == 3


def test_no_uuid_records_use_offset_fallback(env):
    p = write(env.tmp / "s1.jsonl", [rec(None, "user", text(0)), rec(None, "assistant", text(1))])
    assert indexer.index_session(env.conn, p) == 2
    uuids = [r[0] for r in env.conn.execute("SELECT uuid FROM segments ORDER BY id")]
    assert uuids[0] == "f:s1.jsonl:0"
    assert uuids[1].startswith("f:s1.jsonl:") and uuids[1] != uuids[0]
    env.state.reset_all()
    assert indexer.index_session(env.conn, p) == 0
    assert count(env.conn) == 2


def test_stale_cursor_two_sequential_runs_no_dupes(env):
    p = write(env.tmp / "s1.jsonl", base_records(2))
    indexer.index_session(env.conn, p)
    append(p, [rec("u2", "user", text(2))])
    stale = env.state.get_cursor(p)
    assert indexer.index_session(env.conn, p) == 1
    # a second process that read the same stale cursor before the first committed
    env.state.clear_cursor(p)
    env.state.set_cursor(p, stale["offset"], stale["size"], stale["sha1"])
    assert indexer.index_session(env.conn, p) == 0
    assert count(env.conn) == 3
    assert env.state.get_cursor(p)["offset"] == p.stat().st_size


def test_no_segments_creates_no_session(env):
    p = write(env.tmp / "s1.jsonl", [{"type": "system", "uuid": "z"}])
    assert indexer.index_session(env.conn, p) == 0
    assert count(env.conn, "sessions") == 0


def test_missing_file_returns_zero(env):
    assert indexer.index_session(env.conn, env.tmp / "nope.jsonl") == 0


def test_null_role_raises_and_rolls_back(env):
    r = rec("u0", "user", text(0))
    r["message"]["role"] = None
    p = write(env.tmp / "s1.jsonl", [rec("u1", "user", text(1)), r])
    with pytest.raises(sqlite3.IntegrityError):
        indexer.index_session(env.conn, p)
    assert count(env.conn) == 0
    assert count(env.conn, "sessions") == 0
    assert env.state.get_cursor(p) is None
    assert not env.conn.in_transaction


def test_locked_db_raises_database_locked(env):
    p = write(env.tmp / "s1.jsonl", base_records(2))
    env.conn.execute("PRAGMA busy_timeout=50")
    other = sqlite3.connect(str(env.db_path), isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(indexer.DatabaseLocked):
            indexer.index_session(env.conn, p)
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert env.state.get_cursor(p) is None
    assert indexer.index_session(env.conn, p) == 2


def test_default_isolation_conn_is_switched(env):
    conn = sqlite3.connect(str(env.db_path))
    p = write(env.tmp / "s1.jsonl", base_records(2))
    assert indexer.index_session(conn, p) == 2
    conn.close()


def _legacy_session(env, sid, rows, uuid=None):
    env.conn.execute(
        "INSERT INTO sessions(session_id, project_path, segment_count) VALUES (?, 'p', ?)",
        (sid, len(rows)),
    )
    for content in rows:
        env.conn.execute(
            "INSERT INTO segments(session_id, role, content, uuid) VALUES (?, 'user', ?, ?)",
            (sid, content, uuid),
        )


def test_legacy_noisy_rows_replaced_not_doubled(env):
    noisy = "<system-reminder>noise noise noise noise noise noise</system-reminder> OLDMARKER text"
    _legacy_session(env, "s1", [noisy, noisy + " 2", noisy + " 3"])
    p = write(env.tmp / "s1.jsonl", base_records(2))
    assert indexer.index_session(env.conn, p) == 2
    assert count(env.conn, where="session_id='s1'") == 2
    assert count(env.conn, where="content LIKE '%OLDMARKER%'") == 0
    assert count(env.conn, where="uuid IS NULL") == 0
    assert env.conn.execute("SELECT segment_count FROM sessions").fetchone()[0] == 2
    # FTS no longer finds deleted content
    hits = env.conn.execute(
        "SELECT rowid FROM segments_fts WHERE segments_fts MATCH '\"OLDMARKER\"'"
    ).fetchall()
    assert hits == []


def test_legacy_not_replaced_when_some_rows_have_uuid(env):
    _legacy_session(env, "s1", ["old row one with enough text"])
    env.conn.execute(
        "INSERT INTO segments(session_id, role, content, uuid) VALUES ('s1','user','x','keep')"
    )
    p = write(env.tmp / "s1.jsonl", base_records(2))
    indexer.index_session(env.conn, p)
    assert count(env.conn, where="content = 'old row one with enough text'") == 1


def test_desktop_session_rows_never_replaced(env):
    _legacy_session(env, "desktop-1", ["desktop memory row long enough"])
    p = write(env.tmp / "desktop-1.jsonl", base_records(2))
    indexer.index_session(env.conn, p)
    assert count(env.conn, where="content = 'desktop memory row long enough'") == 1


def test_legacy_session_without_transcript_untouched(env):
    _legacy_session(env, "gone", ["row whose transcript is gone"])
    p = write(env.tmp / "other.jsonl", base_records(2))
    indexer.index_session(env.conn, p)
    assert count(env.conn, where="session_id='gone'") == 1


def test_legacy_not_replaced_by_subagent_file(env):
    _legacy_session(env, "s1", ["legacy main row stays here"])
    agent = write(env.tmp / "s1" / "subagents" / "agent-zz.jsonl", base_records(2))
    n = indexer.index_session(env.conn, agent, "p", session_id="s1", agent_id="zz",
                              agent_type="Explore")
    assert n == 2
    assert count(env.conn, where="content = 'legacy main row stays here'") == 1
    rows = env.conn.execute(
        "SELECT agent_id, agent_type FROM segments WHERE agent_id != ''"
    ).fetchall()
    assert rows == [("zz", "Explore")] * 2
    assert env.conn.execute("SELECT segment_count FROM sessions").fetchone()[0] == 3


def test_subagent_and_main_same_uuid_both_kept(env):
    main = write(env.tmp / "s1.jsonl", [rec("same", "user", text(0))])
    sub = write(env.tmp / "s1" / "subagents" / "agent-a.jsonl", [rec("same", "user", text(0))])
    indexer.index_session(env.conn, main)
    indexer.index_session(env.conn, sub, session_id="s1", agent_id="a", agent_type="x")
    assert count(env.conn) == 2


def test_cwd_coalesce_keeps_first(env):
    p = write(env.tmp / "s1.jsonl", [rec("a", "user", text(0), cwd="/first")])
    indexer.index_session(env.conn, p)
    append(p, [rec("b", "user", text(1), cwd="/second")])
    indexer.index_session(env.conn, p)
    assert env.conn.execute("SELECT cwd FROM sessions").fetchone()[0] == "/first"


def test_v0_db_not_current(env, tmp_path):
    from myak import db
    from tests.test_db_migration import make_v0

    conn = make_v0(tmp_path / "old.db")
    assert not db.require_current(conn)
    conn.close()


def test_fixture_session_basic(env, fixtures_dir):
    p = env.tmp / "sess-basic.jsonl"
    shutil.copy(fixtures_dir / "session_basic.jsonl", p)
    n = indexer.index_session(env.conn, p, "proj")
    assert n >= 2
    assert indexer.index_session(env.conn, p, "proj", final=True) in (0, 1)
    total = count(env.conn)
    assert indexer.index_session(env.conn, p, "proj", final=True) == 0
    assert count(env.conn) == total


def test_fixtures_dir_exists():
    assert FIXTURES.is_dir()


def test_lock_during_insert_raises_and_rolls_back(env, monkeypatch):
    p = write(env.tmp / "s1.jsonl", base_records(2))

    class ConnProxy:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *a):
            if sql.startswith("INSERT INTO segments"):
                raise sqlite3.OperationalError("database is locked")
            return self._c.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._c, name)

    with pytest.raises(indexer.DatabaseLocked):
        indexer.index_session(ConnProxy(env.conn), p)
    assert count(env.conn) == 0
    assert env.state.get_cursor(p) is None
    assert not env.conn.in_transaction


def test_other_operational_error_propagates(env):
    p = write(env.tmp / "s1.jsonl", base_records(2))

    class ConnProxy:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *a):
            if sql.startswith("INSERT INTO segments"):
                raise sqlite3.OperationalError("disk I/O error")
            return self._c.execute(sql, *a)

        def __getattr__(self, name):
            return getattr(self._c, name)

    with pytest.raises(sqlite3.OperationalError):
        indexer.index_session(ConnProxy(env.conn), p)


def test_legacy_replaced_even_when_subagent_rows_exist(env):
    _legacy_session(env, "s1", ["legacy main row one long enough"])
    env.conn.execute(
        "INSERT INTO segments(session_id, role, content, uuid, agent_id, agent_type) "
        "VALUES ('s1','user','subagent row kept','sub-u1','a1','Explore')"
    )
    p = write(env.tmp / "s1.jsonl", base_records(2))
    assert indexer.index_session(env.conn, p) == 2
    assert count(env.conn, where="content LIKE 'legacy main row%'") == 0
    assert count(env.conn, where="agent_id='a1'") == 1
    assert count(env.conn, where="agent_id='' AND uuid IS NULL") == 0


def test_known_limitation_prefix_preserving_rewrite_not_detected(env):
    """D2 の既知の制限: 先頭 4KB と直前 1 バイトが同じ書き換えは検知されない。"""
    recs = [rec(f"u{i:03d}", "user", text(i)) for i in range(80)]
    p = write(env.tmp / "s1.jsonl", recs)
    assert p.stat().st_size > 8192
    assert indexer.index_session(env.conn, p) == 80
    rewritten = list(recs)
    rewritten[60] = rec("u060", "user", text(60, "y"))  # 同じ長さ・4KB より後ろ
    write(p, rewritten)
    append(p, [rec("u080", "user", text(80))])
    assert indexer.index_session(env.conn, p) == 1
    assert count(env.conn, where="content LIKE '%number 60 y:%'") == 0
