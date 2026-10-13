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
