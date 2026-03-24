"""FTS5 メモリ検索。CLI / hook / codex の3モード。"""

import argparse
import json
import math
import re
import sqlite3
import sys
from datetime import datetime, timezone

from myak.config import (
    DB_PATH,
    HALF_LIFE_DAYS,
    MAX_CODEX_CHARS,
    MAX_RESULTS,
    MAX_SNIPPET_CHARS,
)


def time_decay_score(timestamp_str, base_score):
    """新しいセグメントほどスコアを上げる時間減衰。"""
    try:
        ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        days_old = (now - ts).total_seconds() / 86400
        decay = math.exp(-0.693 * days_old / HALF_LIFE_DAYS)
        return base_score * decay
    except (ValueError, TypeError):
        return base_score * 0.5


def tokenize_query(query):
    """長いクエリを FTS5 trigram 向けに OR 結合のトークンに分割する。"""
    cleaned = re.sub(
        r'[、。！？\s,.\-:;!?()（）「」『』\[\]{}'
        r'のにをはがでとからまでについてください]',
        ' ', query,
    )
    tokens = [t.strip() for t in cleaned.split() if len(t.strip()) >= 3]
    if len(tokens) < 2 and len(query) >= 6:
        tokens = []
        clean_q = re.sub(r'[、。！？\s,.\-:;!?()（）「」『』\[\]{}]', '', query)
        for i in range(0, max(1, len(clean_q) - 2), 3):
            chunk = clean_q[i:i + 4]
            if len(chunk) >= 3:
                tokens.append(chunk)
    if not tokens:
        return query
    seen = set()
    unique = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return " OR ".join(f'"{t}"' for t in unique[:5])


def search(query, max_results=MAX_RESULTS):
    """FTS5 で検索し、時間減衰でリランキング。"""
    if not DB_PATH.exists():
        return []

    fts_query = tokenize_query(query)

    conn = sqlite3.connect(str(DB_PATH))
    try:
        rows = conn.execute(
            """
            SELECT s.role, s.content, s.timestamp, s.session_id, se.project_path,
                   rank
            FROM segments_fts f
            JOIN segments s ON s.id = f.rowid
            JOIN sessions se ON se.session_id = s.session_id
            WHERE segments_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, max_results * 3),
        ).fetchall()
    except Exception:
        return []
    finally:
        conn.close()

    results = []
    for row in rows:
        role, content, timestamp, session_id, project, fts_rank = row
        score = time_decay_score(timestamp, abs(fts_rank))
        results.append({
            "role": role,
            "content": content[:MAX_SNIPPET_CHARS],
            "timestamp": timestamp[:10] if timestamp else "",
            "session_id": session_id[:8],
            "project": project or "",
            "score": score,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:max_results]


def format_plain(results, query):
    if not results:
        return f"No memories found for: {query}"
    lines = [f"## Memory search: {query}", ""]
    for r in results:
        project = r["project"].split("-")[-1] if r["project"] else "?"
        lines.append(f"[{r['timestamp']} | {project} | {r['role']}]")
        lines.append(r["content"])
        lines.append("")
    return "\n".join(lines)


def format_hook(results, query):
    if not results:
        return ""
    lines = ["## 関連する過去の記憶", ""]
    for r in results:
        project = r["project"].split("-")[-1] if r["project"] else "?"
        lines.append(f"[{r['timestamp']} | {project}] {r['content'][:300]}")
        lines.append("")
    return json.dumps({"system_message": "\n".join(lines)}, ensure_ascii=False)


def format_codex(results, query):
    if not results:
        return ""
    lines = ["## 関連する過去の文脈", ""]
    total_len = 0
    for r in results:
        entry = f"- [{r['timestamp']}] {r['content'][:200]}"
        if total_len + len(entry) > MAX_CODEX_CHARS:
            break
        lines.append(entry)
        total_len += len(entry)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Search myak memory")
    parser.add_argument("query", nargs="*", help="Search query")
    parser.add_argument("--hook", action="store_true", help="Hook mode: JSON output")
    parser.add_argument("--codex", action="store_true", help="Codex mode: compact output")
    parser.add_argument("--limit", type=int, default=MAX_RESULTS, help="Max results")
    args = parser.parse_args()

    query = " ".join(args.query) if args.query else ""

    if not query and args.hook:
        try:
            data = json.loads(sys.stdin.read())
            message = data.get("message", "")
            if isinstance(message, str):
                query = message[:200].strip()
        except (json.JSONDecodeError, ValueError):
            pass

    if not query:
        if args.hook:
            sys.exit(0)
        print("Usage: myak-query <query>", file=sys.stderr)
        sys.exit(1)

    if len(query) < 3:
        if args.hook:
            sys.exit(0)
        print("Query too short (min 3 chars)", file=sys.stderr)
        sys.exit(1)

    results = search(query, args.limit)

    if args.hook:
        output = format_hook(results, query)
        if output:
            print(output)
    elif args.codex:
        output = format_codex(results, query)
        if output:
            print(output)
    else:
        print(format_plain(results, query))


if __name__ == "__main__":
    main()
