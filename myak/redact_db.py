"""`myak redact-secrets`: 既存 DB のセグメントへ伏せ字化 (redact.py) を遡って適用する。

明示的にのみ実行する (hook からは呼ばない)。適用時の手順:
バックアップ → 伏せ字化 (FTS はトリガで追従) と FTS の rebuild を 1 トランザクションで →
VACUUM で旧ページを物理的に消す → 該当ベクトルを削除 (再埋め込みは run() が行う)。
バックアップには元の平文が残るため、確認後に削除してもらう。
"""

import json
import shlex
import sqlite3
import sys
from collections import Counter

from myak import db
from myak.redact import redact_with_counts

ID_PREVIEW = 20
EXIT_VACUUM_FAILED = 3  # 伏せ字化は済んだが旧ページの消去が未完了 (再試行が必要)
_SCAN_ALL = "SELECT id, session_id, content FROM segments ORDER BY id"
# ロック下では 1 回目に見つけた行と、その後に追加された行だけを見直す
_SCAN_AGAIN = (
    "SELECT id, session_id, content FROM segments "
    "WHERE id > ? OR id IN (SELECT value FROM json_each(?)) ORDER BY id"
)
OUTSIDE_NOTE = (
    "Not touched: other machines' copies, Syncthing .stversions / sync-conflict files, "
    "Obsidian exports and the original transcripts under ~/.claude/projects."
)


def _scan(conn, ids=None, after_id=0):
    """([(new_content, id)], Counter[type], {session_id}, 見た最大 id) を返す。

    ids を渡すと、その id と after_id より新しい行だけを読む。本文は保持しない。
    """
    if ids is None:
        rows = conn.execute(_SCAN_ALL)
    else:
        rows = conn.execute(_SCAN_AGAIN, (after_id, json.dumps(ids)))
    updates, totals, sessions, max_id = [], Counter(), set(), after_id
    for seg_id, sid, content in rows.fetchall():
        max_id = max(max_id, seg_id)
        new, counts = redact_with_counts(content)
        if not counts:
            continue
        updates.append((new, seg_id))
        totals.update(counts)
        sessions.add(sid)
    return updates, totals, sessions, max_id


def _summary(updates, totals, sessions):
    return {
        "segments": len(updates),
        "sessions": len(sessions),
        "by_type": dict(sorted(totals.items())),
        "ids": [seg_id for _, seg_id in updates],
    }


def redact_secrets(conn, *, dry_run=False, backup_dir=None):
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
        return _redact(conn, dry_run, backup_dir)
    finally:
        conn.isolation_level = original_isolation


def _redact(conn, dry_run, backup_dir):
    updates, totals, sessions, max_id = _scan(conn)
    found = _summary(updates, totals, sessions)
    if dry_run or not found["ids"]:
        return {**found, "dry_run": dry_run, "backup": None, "vacuumed": False,
                "vacuum_error": None, "vectors_dropped": 0}

    backup = db.make_backup(conn, "redact", backup_dir)
    # 解放されたページ/セルをゼロで上書きさせる (VACUUM が失敗した場合の保険)
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        # ロック下で取り直す (全件の再走査はしない: フックの書き込みを長く止めないため)
        updates, totals, sessions, _ = _scan(conn, found["ids"], max_id)
        conn.executemany("UPDATE segments SET content = ? WHERE id = ?", updates)
        # 'delete' 済みの旧 trigram が FTS のセグメントに残らないよう索引を作り直す
        conn.execute("INSERT INTO segments_fts(segments_fts) VALUES('rebuild')")
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    result = _summary(updates, totals, sessions)

    vacuum_error = None
    try:
        conn.execute("VACUUM")
    except sqlite3.OperationalError as e:
        vacuum_error = str(e)
    try:
        db._check_integrity(conn)
    except db.MigrationError as e:
        raise db.MigrationError(f"{e} (backup: {backup})") from e

    # 伏せ字化前の本文から作ったベクトルを消す (best effort)。次の埋め込みで再計算される
    from myak import vec

    dropped = vec.forget_segments(conn, result["ids"])
    return {**result, "dry_run": False, "backup": str(backup) if backup else None,
            "vacuumed": vacuum_error is None, "vacuum_error": vacuum_error,
            "vectors_dropped": dropped}


def backups_with_secrets(backup_dir):
    """[(path, 該当セグメント数)]: バックアップを読み取り専用で走査する (本文は出さない)。"""
    found = []
    for path in sorted(backup_dir.glob("memory.db.bak-*")):
        try:
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            updates = _scan(conn)[0]
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        if updates:
            found.append((path, len(updates)))
    return found


def _print_found(result, verb):
    print(f"{verb} redact: {result['segments']} segments in {result['sessions']} sessions")
    for name, n in result["by_type"].items():
        print(f"  {name}: {n}")
    ids = result["ids"]
    if ids:
        more = f" ... (+{len(ids) - ID_PREVIEW})" if len(ids) > ID_PREVIEW else ""
        print(f"segment ids: {', '.join(map(str, ids[:ID_PREVIEW]))}{more}")


def _print_backups(backup_dir):
    stale = backups_with_secrets(backup_dir) if backup_dir.is_dir() else []
    if not stale:
        return
    print("Backups that still contain secrets (move them to the Trash when no longer needed):")
    for path, n in stale:
        print(f"  {path} ({n} segments)")


def _apply(config, vec):
    """embed_lock の下で伏せ字化する (並行する `myak embed` が旧本文のベクトルを戻さないよう)。"""
    with vec.embed_lock() as acquired:
        if not acquired:
            raise sqlite3.OperationalError("another embed is running")
        conn = config.get_connection()
        try:
            return redact_secrets(conn)
        finally:
            conn.close()


def _dry_run(config):
    conn = config.get_connection()
    try:
        return redact_secrets(conn, dry_run=True)
    finally:
        conn.close()


def run(dry_run=False):
    """CLI 本体。終了コードを返す。シークレット本体や本文は出力しない。"""
    from myak import config, vec

    if not config.DB_PATH.exists():
        print(f"No database at {config.DB_PATH}", file=sys.stderr)
        return 1
    try:
        result = _dry_run(config) if dry_run else _apply(config, vec)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except sqlite3.OperationalError as e:
        print(f"Error: database busy, retry ({e})", file=sys.stderr)
        return 3
    except db.MigrationError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(f"Database: {config.DB_PATH}")
    _print_found(result, "would" if dry_run else "did")
    code = 0
    if not dry_run and result["ids"]:
        print(f"Backup (still holds the original plaintext; delete it once verified): "
              f"{result['backup'] or '(none)'}")
        if result["vacuumed"]:
            print("FTS rebuilt and database vacuumed.")
        else:
            code = EXIT_VACUUM_FAILED
            print(f"FTS rebuilt, but VACUUM failed ({result['vacuum_error']}); old pages may "
                  f"still hold plaintext. Retry with: sqlite3 {shlex.quote(str(config.DB_PATH))} "
                  "VACUUM")
        print(f"Vectors dropped: {result['vectors_dropped']}")
        if result["vectors_dropped"]:
            n, reason = vec.try_embed_pending(limit=config.HOOK_EMBED_LIMIT)
            if reason is None:
                print(f"Re-embedded: {n}")
            else:
                hint = "" if "myak embed" in reason else "; run `myak embed`"
                print(f"Re-embedding skipped ({reason}){hint}.")
    _print_backups(config.STATE_DIR / "backups")
    if result["ids"]:
        print(OUTSIDE_NOTE)
    return code
