"""export_obsidian.py の filename collision テスト。"""

import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from myak.export_obsidian import export
from myak.indexer import init_db


def _setup_db(db_path, sessions):
    """テスト用 DB を構築する。"""
    conn = sqlite3.connect(str(db_path))
    init_db(conn)

    for s in sessions:
        conn.execute(
            "INSERT INTO sessions (session_id, project_path, started_at, ended_at, segment_count, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (s["id"], s["project"], s["date"] + "T10:00:00Z", s["date"] + "T11:00:00Z", 1, datetime.now(timezone.utc).isoformat()),
        )
        conn.execute(
            "INSERT INTO segments (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (s["id"], "user", s["content"], s["date"] + "T10:00:00Z"),
        )
    conn.commit()
    conn.close()


class TestFilenameCollision:
    def test_same_date_same_title_different_sessions(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "memory.db"
            vault_dir = Path(tmp_dir) / "vault"

            _setup_db(db_path, [
                {"id": "aaaabbbb-1111-2222-3333-444455556666", "project": "test-project", "date": "2025-03-20", "content": "同じタイトルの内容。テストのために十分な長さが必要です。"},
                {"id": "ccccdddd-1111-2222-3333-444455556666", "project": "test-project", "date": "2025-03-20", "content": "同じタイトルの内容。テストのために十分な長さが必要です。"},
            ])

            with patch("myak.export_obsidian.DB_PATH", db_path):
                export(vault_dir)

            sessions_dir = vault_dir / "sessions"
            md_files = list(sessions_dir.rglob("*.md"))
            # _index.md + 2 session files
            session_files = [f for f in md_files if f.name != "_index.md"]
            assert len(session_files) == 2
            assert session_files[0].name != session_files[1].name
