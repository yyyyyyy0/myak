"""Claude Code hook ハンドラ。

契約: 例外を外に出さない。常に exit 0。失敗は STATE_DIR/hook.log に 1 行ずつ記録する。
"""

import json
import logging
import os
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from myak import config
from myak.config import get_connection
from myak.db import DatabaseLocked, require_current
from myak.indexer import cwd_to_project_slug, index_transcript_tree, init_db

LOG_MAX_BYTES = 256 * 1024
LOG_BACKUP_COUNT = 1
TAIL_BYTES = 64 * 1024
NEEDLE_CHARS = 80
RETRY_SLEEP = 0.5
LOG_MESSAGE_CHARS = 200
SCHEMA_MESSAGE = "database schema is not current; run `myak migrate` (hooks do not index)"

EVENTS = ("stop", "session-end", "session-start", "prompt")


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """ログファイルを作成直後に 0600 にする。"""

    def _open(self):
        stream = super()._open()
        config.secure_file(self.baseFilename)
        return stream


def log(message):
    """hook.log に 1 行追記する (ローテーション付き)。ログ失敗は握りつぶす。"""
    try:
        config.ensure_state_dir()
        handler = _PrivateRotatingFileHandler(
            str(config.STATE_DIR / "hook.log"),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger = logging.getLogger(f"myak.hook.{id(handler)}")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            logger.info(" ".join(str(message).split())[:LOG_MESSAGE_CHARS])
        finally:
            logger.removeHandler(handler)
            handler.close()
    except Exception:  # noqa: BLE001 - logging must never break a hook
        pass


def _schema_warned_marker():
    return config.STATE_DIR / "schema-warned"


def _log_schema_once():
    marker = _schema_warned_marker()
    try:
        if marker.exists():
            return
        config.ensure_state_dir()
        marker.write_text("1")
        config.secure_file(marker)
    except OSError:
        pass
    log(SCHEMA_MESSAGE)


def _clear_schema_warning():
    try:
        _schema_warned_marker().unlink()
    except OSError:
        pass


def _open_current():
    """スキーマが現行なら接続を返す。そうでなければ (once) ログして None。"""
    config.ensure_memory_dir()
    conn = get_connection()
    try:
        init_db(conn)
        if require_current(conn):
            _clear_schema_warning()
            return conn
        _log_schema_once()
    except BaseException:
        conn.close()
        raise
    conn.close()
    return None


def _transcript(payload, event):
    tp = payload.get("transcript_path")
    if not isinstance(tp, str) or not tp:
        log(f"{event}: no transcript_path in payload")
        return None
    raw = Path(tp).expanduser()
    try:
        is_link = raw.is_symlink()
        path = raw.resolve(strict=True)
    except (OSError, RuntimeError):
        log(f"{event}: transcript not found: {raw}")
        return None
    if is_link or not path.is_file():
        log(f"{event}: transcript rejected (symlink or not a file): {raw}")
        return None
    root = config.PROJECTS_DIR.resolve()
    if path.suffix != ".jsonl" or os.path.commonpath([str(path), str(root)]) != str(root):
        log(f"{event}: transcript rejected (must be .jsonl under {root}): {raw}")
        return None
    return path


def _project_slug(payload):
    cwd = payload.get("cwd")
    return cwd_to_project_slug(cwd) if isinstance(cwd, str) and cwd else None


def _tail_has(path, message):
    needle = message[:NEEDLE_CHARS]
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        f.seek(max(0, size - TAIL_BYTES))
        tail = f.read().decode("utf-8", errors="replace")
    # transcript は JSON エスケープされているので両方の形で探す
    variants = {
        needle,
        json.dumps(needle, ensure_ascii=False)[1:-1],
        json.dumps(needle, ensure_ascii=True)[1:-1],
    }
    return any(v in tail for v in variants)


def _wait_for_tail(path, message):
    """D5: transcript が遅延していたら 1 回だけ待って再確認する。"""
    if not isinstance(message, str) or not message.strip():
        return
    if _tail_has(path, message):
        return
    time.sleep(RETRY_SLEEP)


def handle_stop(payload):
    path = _transcript(payload, "stop")
    if path is None:
        return
    _wait_for_tail(path, payload.get("last_assistant_message"))
    conn = _open_current()
    if conn is None:
        return
    try:
        index_transcript_tree(conn, path, _project_slug(payload), final=False)
    except DatabaseLocked as e:
        log(f"stop: {e}")
    finally:
        conn.close()


def handle_session_end(payload):
    path = _transcript(payload, "session-end")
    if path is None:
        return
    conn = _open_current()
    if conn is None:
        return
    conn.close()
    # -I: PYTHON* env/user site/cwd を無視。--file=: '-' 始まりのパスを option 扱いさせない
    cmd = [
        sys.executable, "-I", "-m", "myak.cli", "index", f"--file={path}", "--final", "--embed",
    ]
    slug = _project_slug(payload)
    if slug:
        cmd += ["--project", slug]
    config.ensure_state_dir()
    subprocess.Popen(  # noqa: S603 - fixed argv, detached on purpose
        cmd,
        cwd=str(config.STATE_DIR),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def handle_prompt(payload):
    from myak import query

    if not config.DB_PATH.exists():
        return ""
    conn = get_connection()
    try:
        current = require_current(conn)
    finally:
        conn.close()
    if not current:
        _log_schema_once()
        return ""
    return query.run_hook(json.dumps(payload, ensure_ascii=False))


def handle_session_start(payload):
    """直近セッションを additionalContext として返す。DDL・埋め込みは行わない。"""
    from myak import recall

    if payload.get("source") not in recall.START_SOURCES:
        return ""
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd or not config.DB_PATH.exists():
        return ""
    conn = get_connection()
    try:
        if not require_current(conn):
            _log_schema_once()
            return ""
        rows = recall.recent_sessions(conn, cwd, exclude_session=payload.get("session_id"))
        text = recall.format_session_start(rows)
    finally:
        conn.close()
    if not text:
        return ""
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
        ensure_ascii=False,
    )


def dispatch(event, stdin_text):
    """hook イベントを処理する。常に 0 を返し、例外を投げない。"""
    try:
        if event not in EVENTS:
            log(f"unknown hook event: {event!r}")
            return 0
        try:
            payload = json.loads(stdin_text) if stdin_text and stdin_text.strip() else None
        except ValueError:
            log(f"{event}: invalid JSON on stdin")
            return 0
        if not isinstance(payload, dict):
            log(f"{event}: empty or non-object stdin")
            return 0
        if event == "stop":
            handle_stop(payload)
        elif event == "session-end":
            handle_session_end(payload)
        elif event == "session-start":
            out = handle_session_start(payload)
            if out:
                print(out)
        else:
            out = handle_prompt(payload)
            if out:
                print(out)
    except Exception as e:  # noqa: BLE001 - hooks must never fail the session
        log(f"{event}: {type(e).__name__}: {e}")
    return 0
