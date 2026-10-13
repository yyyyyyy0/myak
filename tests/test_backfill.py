"""backfill over a fixture tree."""

import shutil

import pytest

from myak import backfill, config
from tests.idx_helpers import FIXTURES, count, setup_env


@pytest.fixture
def env(db_env, tmp_path, monkeypatch):
    e = setup_env(db_env, tmp_path, monkeypatch)
    projects = tmp_path / "projects"
    monkeypatch.setattr(config, "PROJECTS_DIR", projects)
    for slug in ("-Users-x-alpha", "-Users-x-beta"):
        d = projects / slug
        d.mkdir(parents=True)
        shutil.copy(FIXTURES / "session_basic.jsonl", d / f"sess-{slug.rsplit('-', 1)[1]}.jsonl")
    sub = projects / "-Users-x-alpha" / "sess-alpha" / "subagents"
    sub.mkdir(parents=True)
    shutil.copy(FIXTURES / "subagents" / "agent-a1.jsonl", sub / "agent-a1.jsonl")
    shutil.copy(FIXTURES / "subagents" / "agent-a1.meta.json", sub / "agent-a1.meta.json")
    e.conn.close()
    return e


def snapshot(env):
    import sqlite3

    c = sqlite3.connect(str(env.db_path))
    try:
        return (
            c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM segments").fetchone()[0],
        )
    finally:
        c.close()


def test_backfill_twice_is_identical(env, capsys):
    assert backfill.backfill() == 0
    first = snapshot(env)
    assert first[0] == 2 and first[1] > 4
    assert backfill.backfill() == 0
    assert snapshot(env) == first
    # even after losing the local cursors the DB does not grow
    env.state.reset_all()
    assert backfill.backfill() == 0
    assert snapshot(env) == first
    assert "Backfill complete" in capsys.readouterr().out


def test_subagent_rows_have_agent_type(env):
    import sqlite3

    backfill.backfill()
    c = sqlite3.connect(str(env.db_path))
    rows = c.execute(
        "SELECT session_id, agent_id, agent_type FROM segments WHERE agent_id != ''"
    ).fetchall()
    c.close()
    assert rows == [("sess-alpha", "a1", "general-purpose")] * 3


def test_project_filter(env):
    import sqlite3

    assert backfill.backfill(project_filter="beta") == 0
    c = sqlite3.connect(str(env.db_path))
    assert count(c, "sessions") == 1
    assert count(c, where="agent_id != ''") == 0
    c.close()


def test_rebuild_fts(env, capsys):
    import sqlite3

    backfill.backfill(rebuild_fts=True)
    assert "FTS index rebuilt" in capsys.readouterr().out
    c = sqlite3.connect(str(env.db_path))
    assert c.execute(
        "SELECT COUNT(*) FROM segments_fts WHERE segments_fts MATCH '\"deprecated\"'"
    ).fetchone()[0] >= 1
    c.close()


def test_refuses_on_v0(db_env, tmp_path, monkeypatch, capsys):
    from tests.test_db_migration import make_v0

    monkeypatch.setattr(config, "STATE_DB_PATH", tmp_path / "st" / "s.db")
    db_env.memory_dir.mkdir(parents=True)
    make_v0(db_env.db_path).close()
    assert backfill.backfill() == 1
    assert "migrate" in capsys.readouterr().err


def test_main_cli(env, monkeypatch):
    monkeypatch.setattr("sys.argv", ["myak-backfill", "--project", "alpha", "--rebuild-fts"])
    with pytest.raises(SystemExit) as e:
        backfill.main()
    assert e.value.code == 0
    assert snapshot(env)[0] == 1
    monkeypatch.setattr("sys.argv", ["myak-backfill", "beta"])
    with pytest.raises(SystemExit):
        backfill.main()
    assert snapshot(env)[0] == 2


def test_missing_projects_dir(db_env, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB_PATH", tmp_path / "st" / "s.db")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "none")
    assert backfill.backfill() == 0


def test_backfill_counts_locked_files_and_exits_nonzero(env, monkeypatch, capsys):
    from myak.db import DatabaseLocked

    real = backfill.index_transcript_tree

    def flaky(conn, path, *a, **k):
        if "alpha" in str(path):
            raise DatabaseLocked("database is locked")
        return real(conn, path, *a, **k)

    monkeypatch.setattr(backfill, "index_transcript_tree", flaky)
    assert backfill.backfill() == 3
    cap = capsys.readouterr()
    assert "Locked (not indexed" in cap.err and "sess-alpha.jsonl" in cap.err
    assert "Sessions indexed: 1" in cap.out
    monkeypatch.setattr(backfill, "index_transcript_tree", real)
    assert backfill.backfill() == 0
