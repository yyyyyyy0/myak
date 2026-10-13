"""Shared pytest fixtures."""

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def tmp_db(tmp_path):
    """Return (db_path, conn_factory) for tests needing an isolated SQLite DB."""
    db_path = tmp_path / "memory.db"

    def conn_factory():
        return sqlite3.connect(str(db_path))

    return db_path, conn_factory


@pytest.fixture
def fixtures_dir():
    """Directory containing hand-written transcript JSONL fixtures."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def db_env(tmp_path, monkeypatch):
    """Redirect memory dir / DB path (including by-value imports) into tmp_path."""
    import myak.config
    import myak.export_obsidian
    import myak.query

    memory_dir = tmp_path / "memory"
    db_path = memory_dir / "memory.db"
    monkeypatch.setattr(myak.config, "MEMORY_DIR", memory_dir)
    monkeypatch.setattr(myak.config, "DB_PATH", db_path)
    monkeypatch.setattr(myak.query, "DB_PATH", db_path)
    monkeypatch.setattr(myak.export_obsidian, "DB_PATH", db_path)
    return SimpleNamespace(memory_dir=memory_dir, db_path=db_path)


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path_factory, monkeypatch):
    """実機の ~/.claude/myak-state を絶対に触らない。"""
    import myak.config

    state_dir = tmp_path_factory.mktemp("autostate") / "myak-state"
    monkeypatch.setattr(myak.config, "STATE_DIR", state_dir)
    monkeypatch.setattr(myak.config, "STATE_DB_PATH", state_dir / "state.db")
    monkeypatch.setattr(myak.config, "VEC_DB_PATH", state_dir / "memory.vec.db")
    monkeypatch.setattr(myak.config, "MODELS_DIR", state_dir / "models")
    monkeypatch.delenv("MYAK_DISABLE_VEC", raising=False)
