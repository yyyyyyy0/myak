"""myak MCP server — expose memory search and save to Claude Desktop."""

import json
import sys
from datetime import datetime, timezone

from myak.config import ensure_memory_dir, get_connection
from myak.indexer import init_db
from myak.query import format_plain, search

DESKTOP_SESSION_PREFIX = "desktop-"


def save_memory(content, role="assistant", session_id=None):
    """Save a memory segment to the database."""
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

        existing = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO sessions "
                "(session_id, project_path, started_at, segment_count, indexed_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (session_id, "claude-desktop", now.isoformat(), now.isoformat()),
            )

        timestamp = now.isoformat()
        conn.execute(
            "INSERT INTO segments (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            (session_id, role, content[:4000], timestamp),
        )
        conn.execute(
            "UPDATE sessions SET segment_count = segment_count + 1, "
            "ended_at = ? WHERE session_id = ?",
            (timestamp, session_id),
        )
        conn.commit()
        return {"saved": True, "session_id": session_id}
    except Exception as e:
        return {"saved": False, "reason": str(e)}
    finally:
        conn.close()


TOOLS = [
    {
        "name": "myak_search",
        "description": (
            "Search past conversation memories across Claude Code and Claude Desktop sessions. "
            "Returns relevant snippets ranked by relevance and recency. "
            "Use this to recall past decisions, discussions, code changes, and context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query (min 3 chars). Can be Japanese or English.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results to return (default 5, max 20).",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "myak_save",
        "description": (
            "Save an important piece of information to long-term memory. "
            "Use this to persist key decisions, facts, user preferences, or conversation summaries "
            "so they can be recalled in future sessions (both Claude Desktop and Claude Code). "
            "Keep content concise and self-contained."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The information to save (min 10 chars, max 4000 chars).",
                },
                "role": {
                    "type": "string",
                    "description": (
                        "Who produced this info: "
                        "'user' or 'assistant' (default: 'assistant')."
                    ),
                    "enum": ["user", "assistant"],
                    "default": "assistant",
                },
                "session_id": {
                    "type": "string",
                    "description": (
                        "Optional session ID to group related saves. "
                        "Omit to auto-generate. Reuse to append to the same session."
                    ),
                },
            },
            "required": ["content"],
        },
    },
]


def handle_request(request):
    """Handle a single JSON-RPC request."""
    method = request.get("method", "")
    req_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "myak", "version": "0.2.0"},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name == "myak_search":
            query = arguments.get("query", "")
            raw_limit = arguments.get("limit", 5)
            limit = max(1, min(int(raw_limit) if isinstance(raw_limit, (int, float)) else 5, 20))

            if len(query) < 3:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": "Query too short (min 3 chars)"}],
                        "isError": True,
                    },
                }

            results = search(query, max_results=limit)
            text = format_plain(results, query)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": text}]},
            }

        if tool_name == "myak_save":
            content = arguments.get("content", "")
            role = arguments.get("role", "assistant")
            session_id = arguments.get("session_id")

            result = save_memory(content, role=role, session_id=session_id)

            if result["saved"]:
                text = f"Saved to memory (session: {result['session_id']})"
            else:
                text = f"Failed to save: {result['reason']}"

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": not result["saved"],
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    return None


def main():
    """Run MCP server over stdio."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            error_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
            sys.stdout.write(json.dumps(error_resp) + "\n")
            sys.stdout.flush()
            continue

        response = handle_request(request)
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
