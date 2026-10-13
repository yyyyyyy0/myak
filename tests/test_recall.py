"""recall.py と SessionStart hook のテスト。"""

import sqlite3

import pytest

from myak import recall
from myak.db import create_schema

CWD = "/Users/x/proj"
SLUG = "Users-x-proj"


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "r.db"), isolation_level=None)
    create_schema(c)
    yield c
    c.close()


def add(conn, sid, *, cwd=CWD, project=SLUG, ended="2026-10-01T10:00:00Z",
        title="first question about the retry logic", n=2, agent_type=None):
    conn.execute(
        "INSERT OR IGNORE INTO sessions(session_id, project_path, ended_at, cwd) "
        "VALUES (?,?,?,?)", (sid, project, ended, cwd))
    for i in range(n):
        conn.execute(
            "INSERT INTO segments(session_id, role, content, timestamp, agent_type) "
            "VALUES (?,?,?,?,?)",
            (sid, "user" if i == 0 else "assistant", title if i == 0 else f"reply {i}",
             ended, agent_type))


def test_matches_only_project(conn):
    add(conn, "a", title="mine")
    add(conn, "b", cwd="/Users/x/other", project="Users-x-other", title="theirs")
    rows = recall.recent_sessions(conn, CWD)
    assert [r[1] for r in rows] == ["mine"]


def test_legacy_null_cwd_matches_slug(conn):
    add(conn, "a", cwd=None, title="legacy")
    add(conn, "b", cwd=None, project="Users-x-other", title="other")
    assert [r[1] for r in recall.recent_sessions(conn, CWD)] == ["legacy"]


def test_order_limit_and_date(conn):
    for i in range(7):
        add(conn, f"s{i}", ended=f"2026-10-0{i + 1}T00:00:00Z", title=f"t{i}")
    rows = recall.recent_sessions(conn, CWD, limit=5)
    assert [r[1] for r in rows] == ["t6", "t5", "t4", "t3", "t2"]
    assert rows[0][0] == "2026-10-07"


def test_desktop_excluded(conn):
    add(conn, "desktop-2026", title="desk")
    add(conn, "a", title="real")
    assert [r[1] for r in recall.recent_sessions(conn, CWD)] == ["real"]


def test_subagent_rows_excluded(conn):
    add(conn, "a", title="main title", n=2)
    conn.execute(
        "INSERT INTO segments(session_id, role, content, timestamp, agent_type, agent_id) "
        "VALUES ('a','user','subagent prompt',?, 'Explore', 'x1')", ("2026-10-01",))
    rows = recall.recent_sessions(conn, CWD)
    assert rows == [("2026-10-01", "main title", 2)]


def test_only_subagent_user_segment_gives_placeholder(conn):
    add(conn, "a", title="sub", agent_type="Explore")
    assert recall.recent_sessions(conn, CWD)[0][1] == "(no title)"


def test_title_sanitised_and_truncated(conn):
    add(conn, "a", title="line1\nline2\x1b[31m\x00 <b>x</b> " + "z" * 200)
    title = recall.recent_sessions(conn, CWD)[0][1]
    assert "\n" not in title and "\x1b" not in title and "\x00" not in title
    assert "<" not in title
    assert len(title) <= recall.TITLE_CHARS


def test_invalid_cwd(conn):
    assert recall.recent_sessions(conn, "") == []
    assert recall.recent_sessions(conn, None) == []


def test_format_empty():
    assert recall.format_session_start([]) == ""


def test_format_layout():
    out = recall.format_session_start([("2026-10-01", "hello", 3)])
    assert out.splitlines() == [
        recall.RECALL_HEADER, "- [2026-10-01] hello (3 segments)", recall.RECALL_FOOTER
    ]


def test_format_cap_drops_whole_lines():
    rows = [("2026-10-01", "x" * 80, 9)] * 40
    out = recall.format_session_start(rows)
    assert len(out) <= 1500
    assert out.endswith(recall.RECALL_FOOTER)
    lines = out.splitlines()
    assert 2 < len(lines) < 42
    assert all(ln.endswith("(9 segments)") for ln in lines[1:-1])


def test_format_cap_too_small_returns_empty(monkeypatch):
    monkeypatch.setattr(recall, "MAX_CHARS", 10)
    assert recall.format_session_start([("d", "t", 1)]) == ""
