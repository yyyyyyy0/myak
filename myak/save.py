"""記憶の保存 (MCP SDK に依存しない共有実装)。CLI と MCP サーバーの両方が使う。"""

import hashlib
import re
from datetime import datetime, timezone

from myak.config import ensure_memory_dir, get_connection
from myak.db import require_current
from myak.indexer import init_db
from myak.redact import redact
from myak.source import InvalidSource, validate_source

DESKTOP_SESSION_PREFIX = "desktop-"
MIN_CONTENT_CHARS = 10
MAX_CONTENT_CHARS = 4000
SESSION_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")


def save_memory(content, role="assistant", session_id=None, source=None):
    """記憶を 1 件保存する。結果 dict ({"saved": bool, ...}) を返し、例外は投げない。

    セッション内で内容ハッシュにより重複排除する。`role` は dedupe キーに含まれない。
    `source` は呼び出し側が決めた書き込み元 (検証される)。None なら未記録 (NULL)。
    入力検証 (長さ・session_id・source) はここで行い、CLI と MCP で同じ保証にする。
    """
    if not isinstance(content, str):
        return {"saved": False, "reason": "Content must be a string"}
    if role not in ("user", "assistant"):
        return {"saved": False, "reason": "Invalid role (user or assistant)"}
    if len(content.strip()) < MIN_CONTENT_CHARS:
        return {"saved": False, "reason": f"Content too short (min {MIN_CONTENT_CHARS} chars)"}
    if len(content) > MAX_CONTENT_CHARS:
        return {
            "saved": False,
            "reason": f"Content too long ({len(content)} chars; max {MAX_CONTENT_CHARS})",
        }
    if session_id is not None and (
        not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id)
    ):
        return {
            "saved": False,
            "reason": "Invalid session_id (max 64 chars of A-Z a-z 0-9 _ . : -)",
        }
    if source is not None:
        try:
            source = validate_source(source)
        except InvalidSource as e:
            return {"saved": False, "reason": str(e)}

    now = datetime.now(timezone.utc)
    if session_id is None:
        session_id = f"{DESKTOP_SESSION_PREFIX}{now.strftime('%Y%m%d-%H%M%S')}"

    ensure_memory_dir()
    conn = get_connection()
    try:
        init_db(conn)
        if not require_current(conn):
            return {
                "saved": False,
                "reason": "Database schema is not current; run `myak migrate`",
            }

        timestamp = now.isoformat()
        stored = redact(content)  # secrets never reach the DB (nor the dedupe hash)
        uuid = "save:" + hashlib.sha1(stored.encode("utf-8")).hexdigest()[:16]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO sessions "
                "(session_id, project_path, started_at, segment_count, indexed_at) "
                "VALUES (?, ?, ?, 0, ?) ON CONFLICT(session_id) DO NOTHING",
                (session_id, source or "claude-desktop", timestamp, timestamp),
            )
            cur = conn.execute(
                "INSERT INTO segments "
                "(session_id, role, content, timestamp, uuid, agent_id, source) "
                "VALUES (?, ?, ?, ?, ?, '', ?) "
                "ON CONFLICT(session_id, agent_id, uuid) WHERE uuid IS NOT NULL DO NOTHING",
                (session_id, role, stored, timestamp, uuid, source),
            )
            duplicate = cur.rowcount == 0
            if not duplicate:
                conn.execute(
                    "UPDATE sessions SET segment_count = "
                    "(SELECT COUNT(*) FROM segments WHERE session_id = ?), ended_at = ? "
                    "WHERE session_id = ?",
                    (session_id, timestamp, session_id),
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        if duplicate:
            return {"saved": True, "duplicate": True, "session_id": session_id}
        return {"saved": True, "session_id": session_id}
    except Exception as e:
        return {"saved": False, "reason": str(e)}
    finally:
        conn.close()
