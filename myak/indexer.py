"""セッション transcript を SQLite FTS5 にインデックスする。

Stop hook から stdin 経由、または CLI 引数で呼ばれる。
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from myak.config import (
    PROJECTS_DIR,
    ensure_memory_dir,
    get_connection,
)
from myak.db import DatabaseLocked, create_schema, is_locked_error, require_current
from myak.state import clear_cursor, fingerprint, get_cursor, set_cursor
from myak.transcript import extract_segments as _extract_segments
from myak.transcript import (
    extract_text_content,  # noqa: F401  (backward compat)
    read_agent_meta,
)


def init_db(conn):
    """新規 DB は v1 で作成する。既存 DB は変更しない (migrate は明示的にのみ)。"""
    create_schema(conn)


def extract_segments(jsonl_path):
    """JSONL ファイルからセグメントを抽出する（transcript に委譲）。"""
    return _extract_segments(jsonl_path)[0]


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


def _needs_full_rescan(path, cursor, size):
    """D2: カーソルが信頼できなければ True。"""
    if cursor is None:
        return True
    if cursor["offset"] > size or size < cursor["size"]:
        return True
    _, sha = fingerprint(path, limit=cursor["size"])
    if sha != cursor["sha1"]:
        return True
    if cursor["offset"] > 0:
        with open(path, "rb") as f:
            f.seek(cursor["offset"] - 1)
            if f.read(1) != b"\n":
                return True
    return False


def _is_legacy_session(conn, session_id):
    """desktop- 以外で、本体 (agent_id='') の行が存在しすべて uuid が NULL のセッションか。"""
    if session_id.startswith("desktop-"):
        return False
    row = conn.execute(
        "SELECT COUNT(*), COUNT(uuid) FROM segments WHERE session_id = ? AND agent_id = ''",
        (session_id,),
    ).fetchone()
    return row[0] > 0 and row[1] == 0


_INSERT_SEGMENT = (
    "INSERT INTO segments(session_id, role, content, timestamp, uuid, agent_id, agent_type) "
    "VALUES (?, ?, ?, ?, ?, ?, ?) "
    "ON CONFLICT(session_id, agent_id, uuid) WHERE uuid IS NOT NULL DO NOTHING"
)

_UPSERT_SESSION = (
    "INSERT INTO sessions "
    "(session_id, project_path, started_at, ended_at, segment_count, indexed_at, cwd) "
    "VALUES (?, ?, ?, ?, 0, ?, ?) "
    "ON CONFLICT(session_id) DO UPDATE SET "
    "ended_at = MAX(COALESCE(sessions.ended_at, ''), COALESCE(excluded.ended_at, '')), "
    "segment_count = (SELECT COUNT(*) FROM segments WHERE session_id = excluded.session_id), "
    "indexed_at = excluded.indexed_at, "
    "cwd = COALESCE(sessions.cwd, excluded.cwd)"
)


def index_session(
    conn, jsonl_path, project_slug=None, *, final=False, agent_id="", agent_type=None,
    session_id=None,
):
    """1 transcript ファイルをインクリメンタルにインデックスする。挿入行数を返す。

    session_id 省略時はファイル名 stem。サブエージェントでは親セッション ID を渡す。
    書き込みロックが取れない場合は DatabaseLocked を送出する (カーソルは進めない)。

    既知の制限 (D2): カーソルの妥当性検査は「サイズ非縮小・先頭 4KB の sha1 一致・
    カーソル直前のバイトが改行」のみ。先頭 4KB とカーソル位置直前の 1 バイトを保ったまま
    それより後ろを書き換えるファイルは検知できず、書き換え部分は再索引されない。
    その場合は `myak reindex --full` を使う。
    """
    jsonl_path = Path(jsonl_path)
    if session_id is None:
        session_id = jsonl_path.stem
    if project_slug is None:
        project_slug = jsonl_path.parent.name

    try:
        size = jsonl_path.stat().st_size
    except OSError:
        return 0

    cursor = get_cursor(jsonl_path)
    try:
        full = _needs_full_rescan(jsonl_path, cursor, size)
    except OSError:
        return 0
    start_offset = 0 if full else cursor["offset"]

    # ロック取得前にパースする
    try:
        segments, new_offset = _extract_segments(jsonl_path, start_offset, final)
        new_size, new_sha = fingerprint(jsonl_path)
    except OSError:
        return 0

    if full and cursor is not None:
        clear_cursor(jsonl_path)

    if not segments:
        set_cursor(jsonl_path, new_offset, new_size, new_sha)
        return 0

    if conn.isolation_level is not None:
        conn.commit()
        conn.isolation_level = None

    now = datetime.now(timezone.utc).isoformat()
    basename = jsonl_path.name
    cwd = next((s["cwd"] for s in segments if s.get("cwd")), None)

    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as e:
        if is_locked_error(e):
            raise DatabaseLocked(f"database is locked, skipping {basename}: {e}") from e
        raise

    inserted = 0
    try:
        if start_offset == 0 and agent_id == "" and _is_legacy_session(conn, session_id):
            conn.execute(
                "DELETE FROM segments WHERE session_id = ? AND agent_id = ''", (session_id,)
            )
        conn.execute(
            _UPSERT_SESSION,
            (
                session_id, project_slug, segments[0]["timestamp"],
                segments[-1]["timestamp"], now, cwd,
            ),
        )
        for seg in segments:
            uuid = seg["uuid"] or f"f:{basename}:{seg['start_offset']}"
            cur = conn.execute(
                _INSERT_SEGMENT,
                (
                    session_id, seg["role"], seg["content"], seg["timestamp"],
                    uuid, agent_id, agent_type,
                ),
            )
            inserted += max(cur.rowcount, 0)
        conn.execute(
            "UPDATE sessions SET segment_count = "
            "(SELECT COUNT(*) FROM segments WHERE session_id = ?) WHERE session_id = ?",
            (session_id, session_id),
        )
        conn.execute("COMMIT")
    except sqlite3.OperationalError as e:
        _rollback(conn)
        if is_locked_error(e):
            raise DatabaseLocked(f"database is locked, skipping {basename}: {e}") from e
        raise
    except BaseException:
        _rollback(conn)
        raise

    set_cursor(jsonl_path, new_offset, new_size, new_sha)
    return inserted


def _rollback(conn):
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def discover_transcripts(session_jsonl):
    """[(path, agent_id, agent_type)]: 本体 ('' , None) の後にサブエージェントを列挙する。"""
    session_jsonl = Path(session_jsonl)
    found = [(session_jsonl, "", None)]
    sub_dir = session_jsonl.with_suffix("") / "subagents"
    if sub_dir.is_dir():
        for agent_path in sorted(sub_dir.glob("agent-*.jsonl")):
            agent_id = agent_path.stem[len("agent-"):]
            meta = read_agent_meta(agent_path)
            found.append((agent_path, agent_id, meta["agent_type"]))
    return found


def index_transcript_tree(conn, session_jsonl, project_slug=None, final=False):
    """本体とサブエージェントの transcript をすべてインデックスする。挿入行数の合計を返す。"""
    session_jsonl = Path(session_jsonl)
    if project_slug is None:
        project_slug = session_jsonl.parent.name
    total = 0
    for path, agent_id, agent_type in discover_transcripts(session_jsonl):
        total += index_session(
            conn, path, project_slug, final=final, agent_id=agent_id,
            agent_type=agent_type, session_id=session_jsonl.stem,
        )
    return total


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
        if not require_current(conn):
            print(
                "myak: database schema is not current; run `myak migrate` "
                "(skipping index)", file=sys.stderr,
            )
            return
        try:
            count = index_transcript_tree(conn, jsonl_path, project_slug)
        except DatabaseLocked as e:
            print(f"myak: {e}", file=sys.stderr)
            return
        if count > 0:
            print(f"Indexed {count} segments from {jsonl_path.name}", file=sys.stderr)
    finally:
        conn.close()


def run_from_cli(argv=None):
    """CLI 引数でインデックスする。"""
    parser = argparse.ArgumentParser(description="Index a session transcript")
    parser.add_argument("--session", help="Session ID")
    parser.add_argument("--project", help="Project slug")
    parser.add_argument("--file", help="Direct path to JSONL file")
    parser.add_argument(
        "--final", action="store_true",
        help="Accept an unterminated tail line (default behaviour of this command)",
    )
    args = parser.parse_args(argv)

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
        if not require_current(conn):
            print("Database schema is not current; run `myak migrate` first.", file=sys.stderr)
            sys.exit(1)
        try:
            count = index_transcript_tree(conn, jsonl_path, args.project, final=True)
        except DatabaseLocked as e:
            print(f"myak: database locked: {e}", file=sys.stderr)
            sys.exit(3)
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
