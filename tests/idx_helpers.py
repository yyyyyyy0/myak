"""Shared helpers for indexer/backfill tests (not a conftest; imported explicitly)."""

import json
from pathlib import Path
from types import SimpleNamespace

FIXTURES = Path(__file__).parent / "fixtures"


def text(n, tag="x"):
    return f"message number {n} {tag}: " + "padding words for length " * 4


def rec(uuid, role, content, ts="2026-10-01T12:00:00.000Z", **extra):
    msg_content = content if role == "user" else [{"type": "text", "text": content}]
    d = {
        "type": role,
        "message": {"role": role, "content": msg_content},
        "timestamp": ts,
        "cwd": "/Users/x/proj",
    }
    if uuid is not None:
        d["uuid"] = uuid
    d.update(extra)
    return d


def dumps(records, eol="\n"):
    return "".join(json.dumps(r, ensure_ascii=False) + eol for r in records)


def write(path, records, eol="\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dumps(records, eol).encode("utf-8"))
    return path


def append(path, records, eol="\n"):
    with open(path, "ab") as f:
        f.write(dumps(records, eol).encode("utf-8"))


def setup_env(db_env, tmp_path, monkeypatch):
    """db_env に加え、カーソルストアを tmp に向けて接続を開く。"""
    import myak.config
    import myak.state
    from myak.config import get_connection
    from myak.indexer import init_db

    monkeypatch.setattr(myak.config, "STATE_DB_PATH", tmp_path / "state" / "state.db")
    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    init_db(conn)
    return SimpleNamespace(
        conn=conn, tmp=tmp_path, state=myak.state, db_path=db_env.db_path,
        memory_dir=db_env.memory_dir,
    )


def count(conn, table="segments", where="1=1", params=()):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0]
