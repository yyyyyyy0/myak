"""indexer.py の hook 契約テスト。"""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from myak.indexer import (
    index_session,
    init_db,
    run_from_hook,
)


def _make_jsonl(tmp_dir, session_id="test-session-001", messages=None):
    """テスト用 JSONL を生成する。"""
    if messages is None:
        user_text = (
            "これはテストメッセージです。十分な長さが必要です。"
            "セグメントとして保存されるためには最低50文字以上の内容が含まれている必要があります。"
        )
        assistant_text = (
            "アシスタントの応答です。これも十分な長さが必要です。"
            "セグメントとして保存されるためには最低50文字以上の内容が含まれている必要があります。"
        )
        messages = [
            {
                "type": "user",
                "message": {"role": "user", "content": user_text},
                "timestamp": "2025-03-20T10:00:00Z",
            },
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": assistant_text},
                "timestamp": "2025-03-20T10:01:00Z",
            },
        ]
    jsonl_path = Path(tmp_dir) / f"{session_id}.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    return jsonl_path


class TestTranscriptPath:
    def test_hook_uses_transcript_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            jsonl_path = _make_jsonl(tmp_dir)
            db_path = Path(tmp_dir) / "memory.db"

            hook_input = json.dumps({
                "transcript_path": str(jsonl_path),
                "cwd": "/Users/nil/src/test-project",
                "session_id": "test-session-001",
            })

            def fake_conn():
                return sqlite3.connect(str(db_path))

            with patch("myak.indexer.get_connection", fake_conn), \
                 patch("myak.indexer.ensure_memory_dir"), \
                 patch("sys.stdin") as mock_stdin:
                mock_stdin.read.return_value = hook_input
                run_from_hook()

            conn = sqlite3.connect(str(db_path))
            count = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
            conn.close()
            assert count == 2

    def test_hook_without_transcript_path_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "memory.db"

            hook_input = json.dumps({
                "cwd": "/Users/nil/src/test-project",
                "session_id": "test-session-001",
            })

            with patch("myak.indexer.ensure_memory_dir"), \
                 patch("sys.stdin") as mock_stdin:
                mock_stdin.read.return_value = hook_input
                run_from_hook()

            assert not db_path.exists()

    def test_hook_with_nonexistent_path_does_nothing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "memory.db"

            hook_input = json.dumps({
                "transcript_path": "/nonexistent/path/session.jsonl",
                "cwd": "/Users/nil/src/test-project",
            })

            with patch("myak.indexer.ensure_memory_dir"), \
                 patch("sys.stdin") as mock_stdin:
                mock_stdin.read.return_value = hook_input
                run_from_hook()

            assert not db_path.exists()


class TestIdempotency:
    def test_duplicate_session_skipped(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            jsonl_path = _make_jsonl(tmp_dir)
            db_path = Path(tmp_dir) / "memory.db"

            conn = sqlite3.connect(str(db_path))
            init_db(conn)

            count1 = index_session(conn, jsonl_path, "test-project")
            count2 = index_session(conn, jsonl_path, "test-project")

            assert count1 == 2
            assert count2 == 0

            total = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
            conn.close()
            assert total == 2
