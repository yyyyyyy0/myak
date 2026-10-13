"""myak.state (機械ローカルのカーソルストア) のテスト。"""

import hashlib

import pytest

from myak import config, state


@pytest.fixture
def state_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_DB_PATH", tmp_path / "st" / "state.db")
    return tmp_path


def test_get_missing_returns_none(state_env):
    assert state.get_cursor("/x.jsonl") is None


def test_set_and_get(state_env):
    state.set_cursor("/x.jsonl", 10, 20, "abc")
    assert state.get_cursor("/x.jsonl") == {"offset": 10, "size": 20, "sha1": "abc"}
    assert (state_env / "st" / "state.db").exists()


def test_monotonic_update(state_env):
    state.set_cursor("/x", 100, 200, "new")
    state.set_cursor("/x", 50, 300, "stale")
    assert state.get_cursor("/x") == {"offset": 100, "size": 200, "sha1": "new"}
    state.set_cursor("/x", 150, 400, "newer")
    assert state.get_cursor("/x") == {"offset": 150, "size": 400, "sha1": "newer"}


def test_clear_and_reset_all(state_env):
    state.set_cursor("/a", 1, 1, "s")
    state.set_cursor("/b", 2, 2, "s")
    state.clear_cursor("/a")
    assert state.get_cursor("/a") is None
    assert state.get_cursor("/b") is not None
    state.reset_all()
    assert state.get_cursor("/b") is None


def test_fingerprint(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"hello")
    assert state.fingerprint(p) == (5, hashlib.sha1(b"hello").hexdigest())
    assert state.fingerprint(p, limit=2) == (5, hashlib.sha1(b"he").hexdigest())
    big = b"x" * 5000
    p.write_bytes(big)
    size, sha = state.fingerprint(p)
    assert size == 5000
    assert sha == hashlib.sha1(big[:4096]).hexdigest()


def test_state_dir_default_in_config():
    assert config.STATE_DIR.name  # defined
    assert config.STATE_DB_PATH.parent == config.STATE_DIR


def test_state_db_mode_0600_and_dir_0700(state_env):
    state.set_cursor("/x.jsonl", 1, 1, "a")
    db = state_env / "st" / "state.db"
    assert oct(db.stat().st_mode & 0o777) == "0o600"
    assert oct(db.parent.stat().st_mode & 0o777) == "0o700"
