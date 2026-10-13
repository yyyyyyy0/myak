"""Subagent discovery and tree indexing."""

import json
import shutil

import pytest

from myak import indexer
from tests.idx_helpers import append, count, rec, setup_env, text


@pytest.fixture
def env(db_env, tmp_path, monkeypatch):
    e = setup_env(db_env, tmp_path, monkeypatch)
    yield e
    e.conn.close()


@pytest.fixture
def tree(tmp_path, fixtures_dir):
    proj = tmp_path / "projects" / "-Users-x-proj"
    proj.mkdir(parents=True)
    main = proj / "sess-basic.jsonl"
    shutil.copy(fixtures_dir / "session_basic.jsonl", main)
    sub = proj / "sess-basic" / "subagents"
    sub.mkdir(parents=True)
    shutil.copy(fixtures_dir / "subagents" / "agent-a1.jsonl", sub / "agent-a1.jsonl")
    shutil.copy(fixtures_dir / "subagents" / "agent-a1.meta.json", sub / "agent-a1.meta.json")
    return main


def test_discover_main_then_subagents(tree):
    found = indexer.discover_transcripts(tree)
    assert found[0] == (tree, "", None)
    assert len(found) == 2
    path, agent_id, agent_type = found[1]
    assert path.name == "agent-a1.jsonl"
    assert (agent_id, agent_type) == ("a1", "general-purpose")


def test_discover_without_subagents(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("")
    assert indexer.discover_transcripts(p) == [(p, "", None)]


def test_discover_missing_meta_is_unknown(tree):
    (tree.parent / "sess-basic" / "subagents" / "agent-a1.meta.json").unlink()
    assert indexer.discover_transcripts(tree)[1][2] == "unknown"


def test_tree_indexes_main_and_subagents_under_parent_session(env, tree):
    total = indexer.index_transcript_tree(env.conn, tree, "slug")
    assert total == count(env.conn)
    sessions = env.conn.execute("SELECT session_id, project_path FROM sessions").fetchall()
    assert sessions == [("sess-basic", "slug")]
    sub = env.conn.execute(
        "SELECT agent_id, agent_type, session_id FROM segments WHERE agent_id != ''"
    ).fetchall()
    assert len(sub) == 3
    assert set(sub) == {("a1", "general-purpose", "sess-basic")}
    main_rows = count(env.conn, where="agent_id = ''")
    assert main_rows >= 2
    assert env.conn.execute("SELECT segment_count FROM sessions").fetchone()[0] == total
    assert indexer.index_transcript_tree(env.conn, tree, "slug") == 0


def test_tree_new_subagent_appears_later(env, tree):
    indexer.index_transcript_tree(env.conn, tree, "slug")
    sub = tree.parent / "sess-basic" / "subagents" / "agent-b2.jsonl"
    sub.write_text(json.dumps(rec("b1", "user", text(1))) + "\n")
    assert indexer.index_transcript_tree(env.conn, tree, "slug") == 1
    assert count(env.conn, where="agent_id = 'b2' AND agent_type = 'unknown'") == 1


def test_tree_slug_defaults_to_parent_dir(env, tree):
    indexer.index_transcript_tree(env.conn, tree)
    assert env.conn.execute("SELECT project_path FROM sessions").fetchone()[0] == "-Users-x-proj"


def test_hook_indexes_tree(env, tree, monkeypatch, capsys):
    import io

    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"transcript_path": str(tree), "cwd": "/Users/x/proj"}))
    )
    indexer.run_from_hook()
    assert "Indexed" in capsys.readouterr().err
    assert count(env.conn, where="agent_id = 'a1'") == 3


def test_hook_ignores_bad_json(monkeypatch):
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    indexer.run_from_hook()


def test_cli_index_file_final(env, tree, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["myak-index", "--file", str(tree), "--project", "slug"])
    indexer.main()
    assert "Indexed" in capsys.readouterr().out
    assert count(env.conn, where="agent_id = 'a1'") == 3
    clean = env.tmp / "clean.jsonl"
    clean.write_text(json.dumps(rec("c1", "user", text(1))) + "\n")
    monkeypatch.setattr("sys.argv", ["myak-index", "--file", str(clean)])
    indexer.main()
    append(clean, [rec("zz", "user", text(5))])
    indexer.main()
    assert count(env.conn, where="uuid = 'zz'") == 1


def test_cli_session_lookup_and_missing(env, tree, monkeypatch, capsys):
    import myak.indexer as ix

    monkeypatch.setattr(ix, "PROJECTS_DIR", tree.parent.parent)
    monkeypatch.setattr("sys.argv", ["myak-index", "--session", "sess-basic"])
    ix.main()
    assert count(env.conn) > 0
    monkeypatch.setattr("sys.argv", ["myak-index", "--session", "nope"])
    with pytest.raises(SystemExit):
        ix.main()
    monkeypatch.setattr("sys.argv", ["myak-index", "--project", "x"])
    with pytest.raises(SystemExit):
        ix.main()


def test_cli_refuses_v0(db_env, tmp_path, monkeypatch, tree, capsys):
    import myak.config
    from tests.test_db_migration import make_v0

    monkeypatch.setattr(myak.config, "STATE_DB_PATH", tmp_path / "st" / "s.db")
    db_env.memory_dir.mkdir(parents=True)
    make_v0(db_env.db_path).close()
    monkeypatch.setattr("sys.argv", ["myak-index", "--file", str(tree)])
    with pytest.raises(SystemExit) as e:
        indexer.main()
    assert e.value.code == 1
