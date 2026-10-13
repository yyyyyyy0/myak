"""`myak clean-legacy`: Phase 1 のノイズ規則を uuid 無しの旧行へ遡って適用する。"""

import sqlite3
import sys

from myak import db
from myak.config import MAX_CONTENT_LENGTH, MIN_CONTENT_LENGTH
from myak.textutil import flat as _flat
from myak.transcript import DROP_TAGS, clean_text

EXAMPLE_COUNT = 5
EXAMPLE_CHARS = 80
_DROP_PREFIXES = tuple(f"<{t}>" for t in DROP_TAGS)
_SCAN_SQL = (
    "SELECT id, session_id, role, content FROM segments "
    "WHERE uuid IS NULL AND session_id NOT LIKE 'desktop-%'"
)


def classify(role, content):
    """('delete', None) / ('update', new_content) / (None, None)。"""
    if role == "user" and content.lstrip().startswith(_DROP_PREFIXES):
        return "delete", None
    cleaned = clean_text(content, role)
    if len(cleaned) < MIN_CONTENT_LENGTH:
        return "delete", None
    cleaned = cleaned[:MAX_CONTENT_LENGTH]
    if cleaned != content:
        return "update", cleaned
    return None, None


def _scan(conn):
    deletes, updates, examples = [], [], []
    sessions = set()
    for seg_id, sid, role, content in conn.execute(_SCAN_SQL):
        action, new = classify(role, content)
        if action is None:
            continue
        sessions.add(sid)
        if action == "delete":
            deletes.append(seg_id)
        else:
            updates.append((new, seg_id))
        if len(examples) < EXAMPLE_COUNT:
            examples.append((action, _flat(content)[:EXAMPLE_CHARS]))
    return deletes, updates, examples, sessions


def clean_legacy(conn, *, dry_run=False, backup_dir=None):
    """結果 dict を返す。スキーマが現行でなければ ValueError。"""
    if not db.require_current(conn):
        raise ValueError(
            f"database schema is not current (v{db.schema_version(conn)}); run `myak migrate`"
        )
    original_isolation = conn.isolation_level
    if original_isolation is not None:
        conn.commit()
        conn.isolation_level = None
    try:
        return _clean(conn, dry_run, backup_dir)
    finally:
        conn.isolation_level = original_isolation


def _clean(conn, dry_run, backup_dir):
    if dry_run:
        deletes, updates, examples, _ = _scan(conn)
        return {
            "deleted": len(deletes), "updated": len(updates), "examples": examples,
            "backup": None, "sessions_removed": 0, "dry_run": True,
        }

    backup = db.make_backup(conn, "clean", backup_dir)
    conn.execute("BEGIN IMMEDIATE")
    try:
        deletes, updates, examples, sessions = _scan(conn)
        conn.executemany("DELETE FROM segments WHERE id = ?", [(i,) for i in deletes])
        conn.executemany("UPDATE segments SET content = ? WHERE id = ?", updates)
        removed = 0
        for sid in sessions:
            n = conn.execute(
                "SELECT COUNT(*) FROM segments WHERE session_id = ?", (sid,)
            ).fetchone()[0]
            if n == 0:
                conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
                removed += 1
            else:
                conn.execute(
                    "UPDATE sessions SET segment_count = ? WHERE session_id = ?", (n, sid)
                )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    # 削除/更新した行のベクトルは古い: 消しておけば次回 `myak embed` で再計算される (best effort)
    from myak import vec

    vec.forget_segments(conn, deletes + [seg_id for _, seg_id in updates])
    return {
        "deleted": len(deletes), "updated": len(updates), "examples": examples,
        "backup": str(backup) if backup else None, "sessions_removed": removed,
        "dry_run": False,
    }


def run(dry_run=False):
    """CLI 本体。終了コードを返す。"""
    from myak import config

    if not config.DB_PATH.exists():
        print(f"No database at {config.DB_PATH}", file=sys.stderr)
        return 1
    conn = config.get_connection()
    try:
        try:
            result = clean_legacy(conn, dry_run=dry_run)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except sqlite3.OperationalError as e:
            print(f"Error: database busy, retry ({e})", file=sys.stderr)
            return 3
    finally:
        conn.close()
    verb = "would" if dry_run else "did"
    print(f"{verb} delete: {result['deleted']}")
    print(f"{verb} update: {result['updated']}")
    if not dry_run:
        print(f"Sessions removed (empty): {result['sessions_removed']}")
        print(f"Backup: {result['backup'] or '(none)'}")
    for action, snippet in result["examples"]:
        print(f"  [{action}] {snippet}")
    return 0
