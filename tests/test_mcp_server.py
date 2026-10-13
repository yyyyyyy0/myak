"""mcp_server JSON-RPC handler tests."""

import io
import json
import sys

import pytest

from myak import mcp_server
from myak.mcp_server import handle_request


@pytest.fixture
def mem(db_env):
    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    return db_env


def call(name, arguments, rid=1):
    return handle_request(
        {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
         "params": {"name": name, "arguments": arguments}}
    )


def test_initialize_list_ping_and_notifications():
    init = handle_request({"id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "myak"
    assert handle_request({"method": "notifications/initialized"}) is None
    tools = handle_request({"id": 2, "method": "tools/list"})["result"]["tools"]
    assert {t["name"] for t in tools} == {"myak_search", "myak_save"}
    assert handle_request({"id": 3, "method": "ping"})["result"] == {}


def test_unknown_method_and_tool():
    assert handle_request({"id": 4, "method": "nope"})["error"]["code"] == -32601
    assert handle_request({"method": "nope"}) is None
    assert call("zzz", {})["error"]["code"] == -32601


def test_search_short_query_is_error():
    assert call("myak_search", {"query": "ab"})["result"]["isError"] is True


def test_save_then_search_and_duplicate(mem):
    text = "remember the unique platypus deployment checklist"
    r = call("myak_save", {"content": text, "session_id": "desktop-t"})["result"]
    assert r["isError"] is False and "Saved to memory" in r["content"][0]["text"]
    r = call("myak_save", {"content": text, "session_id": "desktop-t"})["result"]
    assert r["isError"] is False and "Already saved" in r["content"][0]["text"]
    out = call("myak_search", {"query": "platypus deployment", "limit": "x"})["result"]
    assert "platypus" in out["content"][0]["text"] or "No memories" in out["content"][0]["text"]


def test_save_failure_is_error(mem):
    r = call("myak_save", {"content": "short"})["result"]
    assert r["isError"] is True and "Failed to save" in r["content"][0]["text"]


def test_main_loop(monkeypatch, capsys):
    lines = "\n".join(
        ["", "not json", json.dumps({"id": 1, "method": "ping"}),
         json.dumps({"method": "notifications/initialized"})]
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(lines))
    mcp_server.main()
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert out[0]["error"]["code"] == -32700
    assert out[1] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert len(out) == 2
