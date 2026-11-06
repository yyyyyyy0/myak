"""segments.source (v3): マイグレーション・保存・導出・読み出し・CLI。"""

import asyncio
import io
import json
import sqlite3

import pytest
from mcp.client import Client
from mcp.types import Implementation

from myak import cli, db, mcp_server, query, source
from myak.save import save_memory
from tests.corpus_helpers import build_corpus
from tests.test_db_migration import counts, make_v0


@pytest.fixture
def mem(db_env, tmp_path, monkeypatch):
    from myak import config

    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.delenv("MYAK_SOURCE", raising=False)
    return db_env


def make_v2(path):
    conn = make_v0(path)
    for sql in db._V1_STEPS + db._V2_STEPS:
        conn.execute(sql)
    conn.execute("PRAGMA user_version=2")
    return conn


def seg_cols(conn):
    return {r[1] for r in conn.execute("PRAGMA table_info(segments)")}


# --- schema / migration -------------------------------------------------------------


def test_fresh_schema_is_v3_with_source(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "n.db"), isolation_level=None)
    db.create_schema(conn)
    assert db.CURRENT_VERSION == 3 and db.schema_version(conn) == 3
    assert "source" in seg_cols(conn)


def test_migrate_v2_to_v3_only_adds_column(tmp_path, monkeypatch):
    conn = make_v2(tmp_path / "memory.db")
    conn.isolation_level = None
    before = counts(conn)
    ran = []
    real = db._exec
    monkeypatch.setattr(db, "_exec", lambda c, sql: ran.append(sql) or real(c, sql))
    result = db.migrate(conn, backup_dir=tmp_path / "bk")
    assert ran == list(db._V3_STEPS)  # V2 の手順は再実行しない / UPDATE も無い
    assert result["status"] == "migrated" and result["from"] == 2 and result["version"] == 3
    assert db.schema_version(conn) == 3
    assert "source" in seg_cols(conn)
    assert counts(conn) == before
    assert conn.execute("SELECT COUNT(*) FROM segments WHERE source IS NOT NULL").fetchone()[0] == 0
    assert conn.execute(
        "SELECT rowid FROM segments_fts WHERE segments_fts MATCH '\"unique-token-zebra\"'"
    ).fetchall()
    assert list((tmp_path / "bk").glob("memory.db.bak-v2-*"))
    conn.close()


@pytest.mark.parametrize("maker", [make_v0, None])
def test_migrate_v0_and_v1_reach_v3(tmp_path, maker):
    if maker is None:
        conn = make_v0(tmp_path / "memory.db")
        for sql in db._V1_STEPS:
            conn.execute(sql)
        conn.execute("PRAGMA user_version=1")
    else:
        conn = maker(tmp_path / "memory.db")
    conn.isolation_level = None
    db.migrate(conn, backup_dir=tmp_path / "bk")
    assert db.schema_version(conn) == 3 and "source" in seg_cols(conn)
    conn.close()


# --- source rules ---------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "A b", "-x", "x" * 33, "claude-code", "Claude-Code", 5])
def test_validate_source_rejects(bad):
    with pytest.raises(source.InvalidSource):
        source.validate_source(bad)


def test_validate_source_lowercases():
    assert source.validate_source(" Muse ") == "muse"


@pytest.mark.parametrize(
    "name,expected",
    [
        ("claude-ai", "claude-desktop"),
        ("Claude Code", "claude-code-mcp"),
        ("claude-code", "claude-code-mcp"),
        ("My Client!", "my-client-"),
        ("", "mcp"),
        (None, "mcp"),
        ("-", "mcp"),
        ("x" * 80, "x" * 32),
    ],
)
def test_source_from_client_name(name, expected):
    assert source.source_from_client_name(name) == expected


def test_derive_legacy_source():
    assert source.derive_legacy_source("muse", "save:1") == "muse"
    assert source.derive_legacy_source(None, None) == "claude-code"
    assert source.derive_legacy_source(None, "abc") == "claude-code"
    assert source.derive_legacy_source(None, "save:abc") is None


# --- shared save ----------------------------------------------------------------------


def db_rows(mem):
    c = sqlite3.connect(str(mem.db_path))
    try:
        return c.execute(
            "SELECT s.session_id, g.source, s.project_path FROM sessions s "
            "JOIN segments g ON g.session_id = s.session_id"
        ).fetchall()
    finally:
        c.close()


def test_save_stores_source_and_project_path(mem):
    r = save_memory("remember the muse integration plan", source="Muse", session_id="desktop-a")
    assert r["saved"]
    assert db_rows(mem) == [("desktop-a", "muse", "muse")]


def test_save_existing_session_keeps_project_path(mem):
    save_memory("first note from the muse agent", source="muse", session_id="desktop-a")
    save_memory("second note from another agent", source="codex", session_id="desktop-a")
    rows = db_rows(mem)
    assert sorted(r[1] for r in rows) == ["codex", "muse"]
    assert {r[2] for r in rows} == {"muse"}


@pytest.mark.parametrize(
    "kwargs,needle",
    [
        ({"source": "claude-code"}, "reserved"),
        ({"source": "Bad Name"}, "Invalid source"),
        ({"content": "x" * 4001}, "too long"),
        ({"session_id": "has space"}, "Invalid session_id"),
        ({"role": "system"}, "Invalid role"),
        ({"session_id": 5}, "Invalid session_id"),
    ],
)
def test_save_rejects(mem, kwargs, needle):
    args = {"content": "a long enough memory text"} | kwargs
    r = save_memory(**args)
    assert not r["saved"] and needle in r["reason"]


def test_save_without_mcp_import():
    import subprocess
    import sys

    code = "import sys, myak.save; sys.exit(1 if 'mcp' in sys.modules else 0)"
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


# --- MCP -------------------------------------------------------------------------------


def _mcp_save(client_name, args):
    async def scenario():
        info = Implementation(name=client_name, version="1")
        async with Client(mcp_server.mcp, client_info=info) as client:
            return await client.call_tool("myak_save", args)

    return asyncio.run(scenario())


@pytest.mark.parametrize(
    "client,expected",
    [
        ("claude-ai", "claude-desktop"),
        ("claude-code", "claude-code-mcp"),
        ("Zed Editor", "zed-editor"),
    ],
)
def test_mcp_save_derives_source_from_client_info(mem, client, expected):
    args = {"content": "remember the platypus checklist", "session_id": "desktop-m"}
    res = _mcp_save(client, args)
    assert not res.is_error
    assert db_rows(mem)[0][1] == expected


def test_mcp_save_explicit_source_wins_and_reserved_rejected(mem):
    ok = _mcp_save("claude-ai", {"content": "remember the platypus checklist", "source": "Muse"})
    assert not ok.is_error and db_rows(mem)[0][1] == "muse"
    bad = _mcp_save(
        "claude-ai", {"content": "remember the other platypus note", "source": "claude-code"}
    )
    assert bad.is_error and "reserved" in bad.content[0].text


@pytest.mark.parametrize("ctx", [None, object()])
def test_client_source_never_raises(ctx):
    assert mcp_server._client_source(ctx) == "mcp"


def test_mcp_save_direct_call_falls_back_to_mcp(mem):
    mcp_server.myak_save("remember the platypus checklist", session_id="desktop-d")
    assert db_rows(mem)[0][1] == "mcp"


def test_mcp_search_source_filter(mem):
    save_memory("platypus checklist from muse agent", source="muse", session_id="desktop-a")
    save_memory("platypus checklist from codex agent", source="codex", session_id="desktop-b")
    out = mcp_server.myak_search("platypus checklist", source="muse")
    assert "muse agent" in out and "codex agent" not in out
    assert "| muse]" in out


# --- CLI save -----------------------------------------------------------------------------


def test_cli_save_args(mem, capsys):
    assert cli.main(["save", "--source", "muse", "remember", "the", "cli", "path"]) == 0
    assert "Saved to memory" in capsys.readouterr().out
    assert db_rows(mem)[0][1] == "muse"


def test_cli_save_stdin_dash_and_default_source(mem, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("note read from stdin text\n"))
    assert cli.main(["save", "-"]) == 0
    assert db_rows(mem)[0][1] == "cli"
    monkeypatch.setattr("sys.stdin", io.StringIO("another note from stdin without dash"))
    assert cli.main(["save", "--role", "user"]) == 0


def test_cli_save_env_source(mem, monkeypatch):
    monkeypatch.setenv("MYAK_SOURCE", "Codex")
    assert cli.main(["save", "text from the codex agent"]) == 0
    assert db_rows(mem)[0][1] == "codex"


@pytest.mark.parametrize("env", ["claude-code", "bad name"])
def test_cli_save_env_source_validated(mem, monkeypatch, capsys, env):
    monkeypatch.setenv("MYAK_SOURCE", env)
    assert cli.main(["save", "text from some agent here"]) == 1
    assert "Error" in capsys.readouterr().err


def test_cli_save_failures_exit_nonzero(mem, capsys):
    assert cli.main(["save", "short"]) == 1
    assert "too short" in capsys.readouterr().err
    assert cli.main(["save", "--source", "claude-code", "long enough text here"]) == 1
    assert cli.main(["save", "x" * 4001]) == 1


def test_cli_save_duplicate(mem, capsys):
    cli.main(["save", "--session-id", "desktop-z", "same text twice over"])
    cli.main(["save", "--session-id", "desktop-z", "same text twice over"])
    assert "Already saved" in capsys.readouterr().out


# --- query flags / formats -------------------------------------------------------------------


@pytest.fixture
def mixed(mem):
    conn = build_corpus(mem)
    conn.close()
    save_memory("garage notes written by the muse agent", source="muse", session_id="desktop-a")
    return mem


def test_legacy_rows_derive_claude_code(mixed):
    hits = query.search("garage", 10)
    by = {h["content"][:12]: h["agent_source"] for h in hits}
    assert by["automobile r"] == "claude-code"
    assert by["garage notes"] == "muse"


def test_query_json_and_source_filter(mixed, capsys):
    query.main(["garage", "--no-hybrid", "--json", "--source", "muse"])
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1
    assert set(data[0]) == {"timestamp", "project", "role", "source", "session_id", "content"}
    assert data[0]["source"] == "muse" and data[0]["project"] == "muse"


def test_query_project_filter_case_insensitive(mixed, capsys):
    query.main(["garage", "--no-hybrid", "--json", "--project", "ALPHA"])
    data = json.loads(capsys.readouterr().out)
    assert data and all("alpha" in d["project"] for d in data)
    query.main(["garage", "--no-hybrid", "--json", "--project", "nomatch"])
    assert json.loads(capsys.readouterr().out) == []


def test_query_source_repeatable_and_plain_format(mixed, capsys):
    query.main(["garage", "--no-hybrid", "--source", "muse", "--source", "claude-code"])
    out = capsys.readouterr().out
    assert "| muse]" in out and "| claude-code]" in out


def test_unknown_source_shown_as_question_mark():
    r = {"project": "p", "timestamp": "2026-01-01", "role": "user", "content": "x",
         "agent_source": None}
    assert "[2026-01-01 | p | user | ?]" in query.format_plain([r], "q")


def test_hook_source_attribute_is_flattened_and_escaped():
    r = {"project": "p", "timestamp": "2026-01-01", "role": "user", "content": "x",
         "agent_source": 'a"b\n<c>'}
    out = json.loads(query.format_hook([r], "q"))["hookSpecificOutput"]["additionalContext"]
    line = next(ln for ln in out.splitlines() if ln.startswith("<past-memory"))
    attr = line.split('source="', 1)[1].split('">', 1)[0]
    assert '"' not in attr and "<" not in attr and "\n" not in attr


def test_indexer_writes_claude_code_source(tmp_path, db_env, monkeypatch):
    from tests.idx_helpers import count, rec, setup_env, text, write

    env = setup_env(db_env, tmp_path, monkeypatch)
    p = write(tmp_path / "s1.jsonl", [rec("u1", "user", text(1)), rec("a1", "assistant", text(2))])
    from myak import indexer

    assert indexer.index_session(env.conn, p, "proj") == 2
    assert count(env.conn) == 2
    assert env.conn.execute("SELECT DISTINCT source FROM segments").fetchall() == [
        ("claude-code",)
    ]
    env.conn.close()
