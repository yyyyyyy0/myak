"""mcp_server tool functions and an in-process MCP protocol round trip."""

import asyncio

import pytest
from mcp.client import Client
from mcp.server.mcpserver.exceptions import ToolError

import myak
from myak import mcp_server
from myak.mcp_server import mcp, myak_save, myak_search


@pytest.fixture
def mem(db_env):
    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    return db_env


def test_version_exposed():
    assert myak.__version__ and myak.__version__ != "0.0.0"


def test_search_short_query_is_error():
    with pytest.raises(ToolError, match="Query too short"):
        myak_search("ab")


def test_save_then_search_and_duplicate(mem):
    text = "remember the unique platypus deployment checklist"
    assert "Saved to memory" in myak_save(text, session_id="desktop-t")
    assert "Already saved" in myak_save(text, session_id="desktop-t")
    out = myak_search("platypus deployment", limit=999)
    assert "platypus" in out


def test_search_limit_is_clamped(mem, monkeypatch):
    seen = []
    monkeypatch.setattr(
        mcp_server, "search", lambda q, max_results: seen.append(max_results) or []
    )
    myak_search("abc", limit=0)
    myak_search("abc", limit=500)
    assert seen == [1, 20]


def test_save_failure_is_error(mem):
    with pytest.raises(ToolError, match="Failed to save"):
        myak_save("short")


def test_main_runs_stdio(monkeypatch):
    calls = []
    monkeypatch.setattr(mcp, "run", lambda *a, **k: calls.append((a, k)))
    mcp_server.main()
    assert calls == [(("stdio",), {})]


def test_protocol_round_trip(mem):
    text = "remember the unique platypus deployment checklist"

    async def scenario():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            saved = await client.call_tool(
                "myak_save", {"content": text, "session_id": "desktop-p"}
            )
            found = await client.call_tool("myak_search", {"query": "platypus deployment"})
            short = await client.call_tool("myak_search", {"query": "ab"})
            bad = await client.call_tool("myak_save", {"content": "short"})
            return tools, saved, found, short, bad

    tools, saved, found, short, bad = asyncio.run(scenario())
    assert {t.name for t in tools.tools} == {"myak_search", "myak_save"}
    assert not saved.is_error and "Saved to memory" in saved.content[0].text
    assert not found.is_error and "platypus" in found.content[0].text
    assert short.is_error and "Query too short" in short.content[0].text
    assert bad.is_error and "Failed to save" in bad.content[0].text


def test_search_query_is_stripped_before_length_check():
    with pytest.raises(ToolError, match="Query too short"):
        myak_search("  a  ")


def test_search_failure_is_sanitized(mem, monkeypatch, caplog):
    def boom(q, max_results):
        raise RuntimeError("/secret/path/memory.db is locked")

    monkeypatch.setattr(mcp_server, "search", boom)
    with pytest.raises(ToolError) as ei:
        myak_search("platypus")
    assert str(ei.value) == "Search failed: RuntimeError"
    assert "secret" not in str(ei.value)
    assert "myak_search failed" in caplog.text


def test_save_rejects_overlong_content(mem):
    with pytest.raises(ToolError, match="Content too long"):
        myak_save("x" * 4001, session_id="desktop-t")
    assert "Saved to memory" in myak_save("y" * 4000, session_id="desktop-t")


@pytest.mark.parametrize("sid", ["a" * 65, "has space", "bad/slash", "", "新"])
def test_save_rejects_invalid_session_id(mem, sid):
    with pytest.raises(ToolError, match="Invalid session_id"):
        myak_save("remember the unique platypus checklist", session_id=sid)


def test_save_memory_still_truncates_as_safety_net(mem):
    assert mcp_server.save_memory("z" * 5000, session_id="desktop-s")["saved"]
