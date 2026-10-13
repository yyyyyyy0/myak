"""mcp_server.save_memory (D5)."""

import sqlite3

import pytest

from myak import mcp_server


@pytest.fixture
def mem(db_env):
    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    return db_env


def rows(mem):
    c = sqlite3.connect(str(mem.db_path))
    try:
        return (
            c.execute("SELECT session_id, uuid, agent_id FROM segments").fetchall(),
            c.execute("SELECT session_id, segment_count FROM sessions").fetchall(),
        )
    finally:
        c.close()


def test_save_creates_session_and_dedupes_by_content(mem):
    text = "remember that the deploy key rotates monthly"
    r1 = mcp_server.save_memory(text, session_id="desktop-fixed")
    r2 = mcp_server.save_memory(text, session_id="desktop-fixed")
    assert r1["saved"] and r2["saved"]
    segs, sessions = rows(mem)
    assert len(segs) == 1
    assert segs[0][1].startswith("save:") and len(segs[0][1]) == len("save:") + 16
    assert segs[0][2] == ""
    assert sessions == [("desktop-fixed", 1)]


def test_save_second_distinct_memory_same_session(mem):
    mcp_server.save_memory("first memory text long enough", session_id="desktop-x")
    mcp_server.save_memory("second memory text long enough", session_id="desktop-x")
    segs, sessions = rows(mem)
    assert len(segs) == 2
    assert sessions == [("desktop-x", 2)]


def test_save_validates_and_refuses_v0(mem):
    assert not mcp_server.save_memory(5)["saved"]
    assert not mcp_server.save_memory("short")["saved"]
    from tests.test_db_migration import make_v0

    mem.db_path.unlink(missing_ok=True)
    make_v0(mem.db_path).close()
    r = mcp_server.save_memory("long enough memory text here")
    assert not r["saved"] and "migrate" in r["reason"]
