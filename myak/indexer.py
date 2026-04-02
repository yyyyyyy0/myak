"""セッション transcript を SQLite FTS5 にインデックスする。

Stop hook から stdin 経由、または CLI 引数で呼ばれる。
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from myak.config import (
    MAX_CONTENT_LENGTH,
    MIN_CONTENT_LENGTH,
    PROJECTS_DIR,
    ensure_memory_dir,
    get_connection,
)


def init_db(conn):
    conn.executescript("""
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
    """)
    conn.commit()


def extract_text_content(content):
    """message.content からテキストを抽出する。str or list[block] に対応。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "").strip()
                if text:
                    texts.append(text)
        return "\n".join(texts)
    return ""


def extract_segments(jsonl_path):
    """JSONL ファイルからセグメントを抽出する。"""
    segments = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            rec_type = rec.get("type", "")
            if rec_type not in ("user", "assistant"):
                continue

            msg = rec.get("message")
            if not msg:
                continue

            role = msg.get("role", rec_type)
            content = extract_text_content(msg.get("content", ""))

            if len(content) < MIN_CONTENT_LENGTH:
                continue

            segments.append({
                "role": role,
                "content": content[:MAX_CONTENT_LENGTH],
                "timestamp": rec.get("timestamp", ""),
            })

    return segments


def find_jsonl_for_session(session_id, project_slug=None):
    """session_id に対応する JSONL ファイルを探す。"""
    if project_slug:
        path = PROJECTS_DIR / project_slug / f"{session_id}.jsonl"
        if path.exists():
            return path

    if PROJECTS_DIR.is_dir():
        for project_dir in PROJECTS_DIR.iterdir():
            if not project_dir.is_dir():
                continue
            path = project_dir / f"{session_id}.jsonl"
            if path.exists():
                return path

    return None


def find_latest_jsonl(project_slug):
    """プロジェクト配下の最新の JSONL を返す。"""
    project_dir = PROJECTS_DIR / project_slug
    if not project_dir.is_dir():
        return None

    jsonl_files = sorted(
        project_dir.glob("*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return jsonl_files[0] if jsonl_files else None


def index_session(conn, jsonl_path, project_slug=None):
    """1セッション分の JSONL をインデックスする。冪等。"""
    session_id = jsonl_path.stem
    if project_slug is None:
        project_slug = jsonl_path.parent.name

    existing = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if existing:
        return 0

    segments = extract_segments(jsonl_path)
    if not segments:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO sessions "
        "(session_id, project_path, started_at, ended_at, segment_count, indexed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            session_id,
            project_slug,
            segments[0]["timestamp"],
            segments[-1]["timestamp"],
            len(segments),
            now,
        ),
    )

    for seg in segments:
        conn.execute(
            "INSERT INTO segments (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, seg["role"], seg["content"], seg["timestamp"]),
        )

    conn.commit()
    return len(segments)


def cwd_to_project_slug(cwd):
    """cwd をプロジェクトスラグに変換する。"""
    return cwd.replace("/", "-").lstrip("-")


def run_from_hook():
    """Stop hook の stdin から JSON を読んでインデックスする。"""
    try:
        hook_input = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return

    transcript_path = hook_input.get("transcript_path", "")
    cwd = hook_input.get("cwd", "")
    project_slug = cwd_to_project_slug(cwd) if cwd else None

    if not transcript_path:
        return

    jsonl_path = Path(transcript_path).expanduser()
    if not jsonl_path.exists():
        return

    ensure_memory_dir()
    conn = get_connection()
    try:
        init_db(conn)
        count = index_session(conn, jsonl_path, project_slug)
        if count > 0:
            print(f"Indexed {count} segments from {jsonl_path.name}", file=sys.stderr)
    finally:
        conn.close()


def run_from_cli():
    """CLI 引数でインデックスする。"""
    parser = argparse.ArgumentParser(description="Index a session transcript")
    parser.add_argument("--session", help="Session ID")
    parser.add_argument("--project", help="Project slug")
    parser.add_argument("--file", help="Direct path to JSONL file")
    args = parser.parse_args()

    if args.file:
        jsonl_path = Path(args.file)
    elif args.session:
        jsonl_path = find_jsonl_for_session(args.session, args.project)
    else:
        parser.print_help()
        sys.exit(1)

    if not jsonl_path or not jsonl_path.exists():
        print("JSONL not found", file=sys.stderr)
        sys.exit(1)

    ensure_memory_dir()
    conn = get_connection()
    try:
        init_db(conn)
        count = index_session(conn, jsonl_path, args.project)
        print(f"Indexed {count} segments from {jsonl_path.name}")
    finally:
        conn.close()


def main():
    if len(sys.argv) > 1:
        run_from_cli()
    else:
        run_from_hook()


if __name__ == "__main__":
    main()
