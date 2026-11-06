"""myak MCP server — expose memory search and save to Claude Desktop."""

import logging
import sys
from typing import Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from myak import __version__
from myak.query import fetch_limit, filter_by_source, filter_hybrid, format_plain, search
from myak.save import (  # noqa: F401 - 後方互換の再エクスポート
    DESKTOP_SESSION_PREFIX,
    MAX_CONTENT_CHARS,
    SESSION_ID_RE,
    save_memory,
)
from myak.source import (
    SOURCE_MCP_FALLBACK,
    InvalidSource,
    source_from_client_name,
    validate_source,
)

logger = logging.getLogger("myak.mcp")

mcp = MCPServer("myak", version=__version__)


def _client_source(ctx):
    """MCP クライアントが initialize で名乗った clientInfo.name から source を導出する。

    自己申告なので認証ではない。取得に失敗しても例外は投げず 'mcp' に倒す。
    """
    try:
        name = ctx.request_context.session.client_params.client_info.name
    except Exception:  # noqa: BLE001 - clientInfo が無い/形が違うときも保存は止めない
        return SOURCE_MCP_FALLBACK
    return source_from_client_name(name)


@mcp.tool(
    name="myak_search",
    description=(
        "Search past conversation memories across Claude Code and Claude Desktop sessions. "
        "Returns relevant snippets ranked by relevance and recency. "
        "Use this to recall past decisions, discussions, code changes, and context."
    ),
)
def myak_search(query: str, limit: int = 5, source: str | None = None) -> str:
    """query: search query (min 3 chars, Japanese or English). limit: 1-20, default 5.

    source: optional filter by the agent that wrote the memory (e.g. claude-code, muse).
    """
    query = query.strip()
    if len(query) < 3:
        raise ToolError("Query too short (min 3 chars)")
    limit = max(1, min(limit, 20))
    sources = None
    if source is not None and source.strip():
        sources = [source.strip().lower()]
    fetch = fetch_limit(limit, bool(sources))
    try:
        try:
            results = search(query, max_results=fetch, use_vec=True)
        except Exception:
            logger.warning("hybrid search failed; falling back to FTS", exc_info=True)
            results = search(query, max_results=fetch, use_vec=False)
    except Exception as e:
        logger.exception("myak_search failed")
        raise ToolError(f"Search failed: {type(e).__name__}") from None
    results = filter_hybrid(results, query)
    if sources:
        results = filter_by_source(results, sources)[:limit]
    return format_plain(results, query)


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
    source: str | None = None,
    ctx: Context | None = None,
) -> str:
    """content: info to save (10-4000 chars). role: who produced it. session_id: optional.

    source: optional name of the writing agent (a-z 0-9 . _ -); defaults to the MCP client name.
    """
    if source is not None:
        try:
            source = validate_source(source)
        except InvalidSource as e:
            raise ToolError(str(e)) from None
    else:
        source = _client_source(ctx)
    result = save_memory(content, role=role, session_id=session_id, source=source)
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
