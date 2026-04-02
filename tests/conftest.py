"""Shared pytest fixtures."""

import sqlite3

import pytest


@pytest.fixture
def tmp_db(tmp_path):
    """Return (db_path, conn_factory) for tests needing an isolated SQLite DB."""
    db_path = tmp_path / "memory.db"

    def conn_factory():
        return sqlite3.connect(str(db_path))

    return db_path, conn_factory
