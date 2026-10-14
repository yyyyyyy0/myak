"""sqlite-vec を実際にロードできる環境かどうかの判定 (テスト用)。"""

import sqlite3

import pytest


def require_sqlite_vec():
    """sqlite_vec が import でき、かつ sqlite3 が拡張ロード可能でなければ skip。"""
    pytest.importorskip("sqlite_vec")
    conn = sqlite3.connect(":memory:")
    try:
        if not hasattr(conn, "enable_load_extension"):
            pytest.skip("this Python's sqlite3 cannot load extensions")
        try:
            conn.enable_load_extension(True)
            import sqlite_vec

            sqlite_vec.load(conn)
        except (AttributeError, sqlite3.OperationalError, sqlite3.NotSupportedError) as e:
            pytest.skip(f"sqlite-vec extension cannot be loaded here: {e}")
    finally:
        conn.close()
