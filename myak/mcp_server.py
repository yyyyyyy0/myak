"""myak MCP server — expose memory search and save to Claude Desktop."""

import hashlib
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from myak import __version__
from myak.config import ensure_memory_dir, get_connection
from myak.db import require_current
from myak.indexer import init_db
from myak.query import filter_hybrid, format_plain, search

DESKTOP_SESSION_PREFIX = "desktop-"
MAX_CONTENT_CHARS = 4000
SESSION_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,64}")

logger = logging.getLogger("myak.mcp")

mcp = MCPServer("myak", version=__version__)


def save_memory(content, role="assistant", session_id=None):
    """Save a memory segment to the database.

    Deduplicated per session by content hash; `role` is not part of the dedupe key, so
    saving the same text again with a different role is reported as a duplicate.
    """
    if not isinstance(content, str):
        return {"saved": False, "reason": "Content must be a string"}
    if len(content.strip()) < 10:
        return {"saved": False, "reason": "Content too short (min 10 chars)"}

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
        stored = content[:MAX_CONTENT_CHARS]  # safety net; the tool rejects longer input
        uuid = "save:" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO sessions "
                "(session_id, project_path, started_at, segment_count, indexed_at) "
                "VALUES (?, ?, ?, 0, ?) ON CONFLICT(session_id) DO NOTHING",
                (session_id, "claude-desktop", timestamp, timestamp),
            )
            cur = conn.execute(
                "INSERT INTO segments "
                "(session_id, role, content, timestamp, uuid, agent_id) "
                "VALUES (?, ?, ?, ?, ?, '') "
                "ON CONFLICT(session_id, agent_id, uuid) WHERE uuid IS NOT NULL DO NOTHING",
                (session_id, role, stored, timestamp, uuid),
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


@mcp.tool(
    name="myak_search",
    description=(
        "Search past conversation memories across Claude Code and Claude Desktop sessions. "
        "Returns relevant snippets ranked by relevance and recency. "
        "Use this to recall past decisions, discussions, code changes, and context."
    ),
)
def myak_search(query: str, limit: int = 5) -> str:
    """query: search query (min 3 chars, Japanese or English). limit: 1-20, default 5."""
    query = query.strip()
    if len(query) < 3:
        raise ToolError("Query too short (min 3 chars)")
    limit = max(1, min(limit, 20))
    try:
        try:
            results = search(query, max_results=limit, use_vec=True)
        except Exception:
            logger.warning("hybrid search failed; falling back to FTS", exc_info=True)
            results = search(query, max_results=limit, use_vec=False)
    except Exception as e:
        logger.exception("myak_search failed")
        raise ToolError(f"Search failed: {type(e).__name__}") from None
    return format_plain(filter_hybrid(results, query), query)


@mcp.tool(
    name="myak_save",
    description=(
        "Save an important piece of information to long-term memory. "
        "Use this to persist key decisions, facts, user preferences, or conversation summaries "
        "so they can be recalled in future sessions (both Claude Desktop and Claude Code). "
        "Keep content concise and self-contained."
    ),
)
def myak_save(
    content: str,
    role: Literal["user", "assistant"] = "assistant",
    session_id: str | None = None,
) -> str:
    """content: info to save (10-4000 chars). role: who produced it. session_id: optional."""
    if isinstance(content, str) and len(content) > MAX_CONTENT_CHARS:
        raise ToolError(f"Content too long ({len(content)} chars; max {MAX_CONTENT_CHARS})")
    if session_id is not None and not SESSION_ID_RE.fullmatch(session_id):
        raise ToolError("Invalid session_id (max 64 chars of A-Z a-z 0-9 _ . : -)")
    result = save_memory(content, role=role, session_id=session_id)
    if result["saved"] and result.get("duplicate"):
        return f"Already saved (duplicate, session: {result['session_id']})"
    if result["saved"]:
        return f"Saved to memory (session: {result['session_id']})"
    raise ToolError(f"Failed to save: {result['reason']}")


def main():
    """Run the MCP server over stdio (stdout is protocol-only; logs go to stderr)."""
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
