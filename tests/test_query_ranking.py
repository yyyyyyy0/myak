"""query.search のランキング（subagent 重み付け）テスト。"""

import sqlite3
from datetime import datetime, timezone

from myak.config import SUBAGENT_WEIGHT
from myak.query import search

SCHEMA = """
PRAGMA user_version=2;
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, project_path TEXT, started_at TEXT,
    ended_at TEXT, segment_count INTEGER DEFAULT 0, indexed_at TEXT);
CREATE TABLE segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
    role TEXT NOT NULL, content TEXT NOT NULL, timestamp TEXT,
    uuid TEXT, agent_id TEXT, agent_type TEXT);
CREATE VIRTUAL TABLE segments_fts USING fts5(
    content, content='segments', content_rowid='id', tokenize='trigram');
CREATE TRIGGER segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

CONTENT = "認証エラーの修正方法についてのメモ"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _build(db_env, rows):
    """rows: (session_id, agent_type)。同一 content / timestamp で投入する。"""
    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_env.db_path))
    conn.executescript(SCHEMA)
    ts = _now()
    for sid, agent_type in rows:
        conn.execute("INSERT OR IGNORE INTO sessions(session_id, project_path) "
                     "VALUES (?, 'proj')", (sid,))
        conn.execute(
            "INSERT INTO segments(session_id, role, content, timestamp, agent_type) "
            "VALUES (?, 'user', ?, ?, ?)", (sid, CONTENT, ts, agent_type))
    conn.commit()
    conn.close()


def test_main_session_outranks_subagent_at_equal_rank(db_env):
    # subagent を先に挿入しても main が上位に来る
    _build(db_env, [("subagent0001", "Explore"), ("mainsess0001", None)])
    results = search("認証エラー")
    by_agent = {r["agent_type"]: r for r in results}
    assert results[0]["agent_type"] is None
    ratio = by_agent["Explore"]["score"] / by_agent[None]["score"]
    assert abs(ratio - SUBAGENT_WEIGHT) < 0.01


def test_agent_type_in_results(db_env):
    _build(db_env, [("subagent0001", "unknown")])
    results = search("認証エラー")
    assert results[0]["agent_type"] == "unknown"


def test_null_agent_type_unweighted(db_env):
    _build(db_env, [("mainsess0001", None)])
    results = search("認証エラー")
    assert results[0]["agent_type"] is None
    assert results[0]["score"] > 0


def test_missing_agent_type_column_returns_empty(db_env):
    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_env.db_path))
    conn.executescript(SCHEMA.replace(", agent_type TEXT", ""))
    conn.execute("INSERT INTO sessions(session_id) VALUES ('s1')")
    conn.execute("INSERT INTO segments(session_id, role, content, timestamp) "
                 "VALUES ('s1','user',?,?)", (CONTENT, _now()))
    conn.commit()
    conn.close()
    assert search("認証エラー") == []
