"""hooks.dispatch の契約テスト。"""

import json
import sqlite3
import time

import pytest

from myak import config, hooks
from tests.idx_helpers import append, count, rec, setup_env, text, write
from tests.test_db_migration import make_v0


@pytest.fixture
def env(db_env, tmp_path, monkeypatch):
    e = setup_env(db_env, tmp_path, monkeypatch)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setenv("MYAK_DIR", str(db_env.memory_dir))
    monkeypatch.setenv("MYAK_STATE_DIR", str(tmp_path / "state"))
    e.conn.close()
    e.transcript = tmp_path / "projects" / "-Users-x-proj" / "sess-1.jsonl"
    write(e.transcript, [rec("u1", "user", text(1)), rec("a1", "assistant", text(2))])
    e.log = tmp_path / "state" / "hook.log"
    return e


def seg_count(env):
    c = sqlite3.connect(str(env.db_path))
    try:
        return count(c)
    finally:
        c.close()


def payload(env, **extra):
    d = {"session_id": "sess-1", "transcript_path": str(env.transcript), "cwd": "/Users/x/proj"}
    d.update(extra)
    return json.dumps(d)


def test_stop_indexes_then_only_new_rows(env, capsys):
    assert hooks.dispatch("stop", payload(env)) == 0
    assert seg_count(env) == 2
    append(env.transcript, [rec("u2", "user", text(3)), rec("a2", "assistant", text(4))])
    assert hooks.dispatch("stop", payload(env)) == 0
    assert seg_count(env) == 4
    assert hooks.dispatch("stop", payload(env)) == 0
    assert seg_count(env) == 4
    assert capsys.readouterr().out == ""


def test_stop_retries_once_when_tail_lacks_message(env, monkeypatch):
    sleeps = []
    monkeypatch.setattr(hooks.time, "sleep", sleeps.append)
    hooks.dispatch("stop", payload(env, last_assistant_message="not in the file at all " * 5))
    assert sleeps == [hooks.RETRY_SLEEP]
    assert seg_count(env) == 2


def test_stop_no_retry_when_tail_has_message(env, monkeypatch):
    sleeps = []
    monkeypatch.setattr(hooks.time, "sleep", sleeps.append)
    msg = text(2)  # assistant text already in the transcript
    hooks.dispatch("stop", payload(env, last_assistant_message=msg))
    assert sleeps == []


def test_stop_no_retry_on_escaped_message(env, monkeypatch):
    sleeps = []
    monkeypatch.setattr(hooks.time, "sleep", sleeps.append)
    msg = 'line one\nline "two" 日本語 ' + "x" * 60
    append(env.transcript, [rec("a9", "assistant", msg)])
    hooks.dispatch("stop", payload(env, last_assistant_message=msg))
    assert sleeps == []


def test_session_end_detaches_and_child_indexes(env):
    start = time.monotonic()
    assert hooks.dispatch("session-end", payload(env, reason="clear")) == 0
    assert time.monotonic() - start < 0.5
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and seg_count(env) < 2:
        time.sleep(0.05)
    assert seg_count(env) == 2


def test_session_end_popen_hardened(env, monkeypatch):
    calls = []
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    assert hooks.dispatch("session-end", payload(env)) == 0
    (cmd, kw), = calls
    assert cmd[:3] == [hooks.sys.executable, "-I", "-m"]
    assert f"--file={env.transcript.resolve()}" in cmd or f"--file={env.transcript}" in cmd
    assert "--file" not in cmd
    assert kw["cwd"] == str(config.STATE_DIR)
    assert kw["start_new_session"] is True


@pytest.mark.parametrize("event", ["stop", "session-end", "prompt", "session-start"])
@pytest.mark.parametrize("stdin", ["", "   ", "{not json", "[1,2]", "null"])
def test_bad_stdin_exits_zero_silently_and_logs(env, capsys, event, stdin):
    assert hooks.dispatch(event, stdin) == 0
    assert capsys.readouterr().out == ""
    assert env.log.read_text().strip()


@pytest.mark.parametrize("event", ["stop", "session-end"])
def test_missing_transcript_logs(env, capsys, event):
    p = json.dumps({"transcript_path": str(env.tmp / "nope.jsonl")})
    assert hooks.dispatch(event, p) == 0
    assert "transcript not found" in env.log.read_text()
    assert hooks.dispatch(event, "{}") == 0
    assert "no transcript_path" in env.log.read_text()
    assert capsys.readouterr().out == ""


def test_unknown_event(env, capsys):
    assert hooks.dispatch("bogus", "{}") == 0
    assert "unknown hook event" in env.log.read_text()
    assert capsys.readouterr().out == ""


def test_session_start_stub(env, capsys):
    assert hooks.dispatch("session-start", payload(env, source="startup")) == 0
    assert capsys.readouterr().out == ""


def test_v0_schema_logs_once_and_does_not_index(env, capsys):
    env.db_path.unlink()
    make_v0(env.db_path).close()
    for event in ("stop", "session-end", "prompt"):
        assert hooks.dispatch(event, payload(env, prompt="what about retries here")) == 0
    assert capsys.readouterr().out == ""
    log_text = env.log.read_text()
    assert log_text.count("myak migrate") == 1
    c = sqlite3.connect(str(env.db_path))
    try:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 5
    finally:
        c.close()


def test_prompt_returns_hook_json(env, capsys, monkeypatch):
    import myak.query

    # tiny corpus yields tiny bm25 scores; relax only the absolute floor
    monkeypatch.setattr(myak.query, "MIN_ABSOLUTE_SCORE", 0.0)
    hooks.dispatch("stop", payload(env))
    query = "message number 1 x padding words"
    p = payload(env, prompt=query, hook_event_name="UserPromptSubmit")
    assert hooks.dispatch("prompt", p) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "message number" in out["hookSpecificOutput"]["additionalContext"]


def test_prompt_without_db_is_silent(db_env, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    assert hooks.dispatch("prompt", json.dumps({"prompt": "something to search"})) == 0
    assert capsys.readouterr().out == ""


def test_exception_is_swallowed_and_logged(env, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(hooks, "index_transcript_tree", boom)
    assert hooks.dispatch("stop", payload(env)) == 0
    assert "RuntimeError: kaput" in env.log.read_text()
    assert capsys.readouterr().out == ""


def test_stop_database_locked_logged_exit_zero(env, monkeypatch, capsys):
    from myak.db import DatabaseLocked

    def locked(*a, **k):
        raise DatabaseLocked("database is locked, skipping sess-1.jsonl")

    monkeypatch.setattr(hooks, "index_transcript_tree", locked)
    assert hooks.dispatch("stop", payload(env)) == 0
    assert "database is locked" in env.log.read_text()
    assert "Traceback" not in env.log.read_text()
    assert capsys.readouterr().out == ""


def test_log_rotates(env, monkeypatch):
    monkeypatch.setattr(hooks, "LOG_MAX_BYTES", 300)
    for i in range(30):
        hooks.log(f"line {i} " + "x" * 50)
    assert (env.log.parent / "hook.log.1").exists()
    assert not (env.log.parent / "hook.log.2").exists()


def test_log_failure_is_swallowed(env, monkeypatch):
    monkeypatch.setattr(config, "STATE_DIR", env.tmp / "file")
    (env.tmp / "file").write_text("not a dir")
    hooks.log("anything")  # must not raise


def test_log_file_mode_dir_mode_and_truncation(env):
    hooks.log("x" * 1000)
    assert oct(env.log.stat().st_mode & 0o777) == "0o600"
    assert oct(env.log.parent.stat().st_mode & 0o777) == "0o700"
    line = env.log.read_text().strip()
    assert len(line.split(" ", 2)[2]) == hooks.LOG_MESSAGE_CHARS


def test_hook_log_not_in_memory_dir(env):
    hooks.log("hello")
    assert not (env.memory_dir / "hook.log").exists()


def test_schema_marker_in_state_dir_0600(env):
    env.db_path.unlink()
    make_v0(env.db_path).close()
    hooks.dispatch("stop", payload(env))
    marker = env.log.parent / "schema-warned"
    assert marker.exists() and oct(marker.stat().st_mode & 0o777) == "0o600"
    assert not (env.memory_dir / "schema-warned").exists()


@pytest.mark.parametrize("event", ["stop", "session-end"])
def test_transcript_outside_projects_dir_rejected(env, tmp_path, monkeypatch, event):
    outside = tmp_path / "elsewhere" / "sess.jsonl"
    write(outside, [rec("u1", "user", text(1))])
    popen = []
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda *a, **k: popen.append(a))
    p = json.dumps({"transcript_path": str(outside), "cwd": "/Users/x/proj"})
    assert hooks.dispatch(event, p) == 0
    assert "rejected" in env.log.read_text()
    assert seg_count(env) == 0 and not popen


def test_transcript_sibling_prefix_dir_rejected(env, tmp_path):
    evil = tmp_path / "projects-evil" / "s.jsonl"  # 文字列プレフィクスは一致するが配下ではない
    write(evil, [rec("u1", "user", text(1))])
    hooks.dispatch("stop", json.dumps({"transcript_path": str(evil)}))
    assert "rejected" in env.log.read_text() and seg_count(env) == 0


def test_transcript_wrong_suffix_rejected(env):
    bad = env.transcript.with_suffix(".txt")
    bad.write_text("x")
    hooks.dispatch("stop", json.dumps({"transcript_path": str(bad)}))
    assert "rejected" in env.log.read_text() and seg_count(env) == 0


def test_transcript_symlink_rejected_even_inside_projects(env, tmp_path):
    target = tmp_path / "secret.jsonl"
    write(target, [rec("u1", "user", text(1))])
    link = env.transcript.with_name("link.jsonl")
    link.symlink_to(target)
    hooks.dispatch("stop", json.dumps({"transcript_path": str(link)}))
    assert "rejected" in env.log.read_text() and seg_count(env) == 0


def test_transcript_dotdot_escape_rejected(env, tmp_path):
    outside = tmp_path / "out.jsonl"
    write(outside, [rec("u1", "user", text(1))])
    sneaky = tmp_path / "projects" / ".." / "out.jsonl"
    hooks.dispatch("stop", json.dumps({"transcript_path": str(sneaky)}))
    assert "rejected" in env.log.read_text() and seg_count(env) == 0


def test_transcript_dash_leading_relative_path_safe(env, tmp_path, monkeypatch):
    # '-' 始まりの相対パスは --file= 形式で option として解釈されない
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(hooks.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    write(tmp_path / "-evil.jsonl", [rec("u1", "user", text(1))])
    hooks.dispatch("session-end", json.dumps({"transcript_path": "-evil.jsonl"}))
    # 相対パスは resolve 済みの絶対パスで渡る
    assert calls and calls[0][calls[0].index("-m") + 2] == "index"
    assert any(a.startswith("--file=/") for a in calls[0])
