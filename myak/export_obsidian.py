"""SQLite メモリを Obsidian vault にエクスポートする。"""

import argparse
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from myak.config import DB_PATH, OBSIDIAN_VAULT, get_connection, home_parts

MAX_TITLE_LENGTH = 50


def slug_from_project(project_path):
    """プロジェクトスラグを短い名前に変換する。環境非依存。"""
    parts = project_path.strip("-").split("-")
    if len(parts) <= 2:
        return "home"
    # ホームディレクトリのパーツ + 汎用ディレクトリ名を除外
    exclude = home_parts() | {"github", "repos"}
    significant = [p for p in parts if p and p not in exclude]
    return significant[-1] if significant else parts[-1]


def extract_title(segments):
    """最初の user メッセージからタイトルを生成する。"""
    for seg in segments:
        if seg["role"] == "user":
            text = re.sub(r'<[^>]+>', '', seg["content"])
            for line in text.split("\n"):
                line = re.sub(r'^#+\s*', '', line.strip())
                if len(line) < 5:
                    continue
                if line.startswith(("```", "import ", "Caveat", "command", "local-command")):
                    continue
                if re.match(r'^(task-notification|system-reminder|command-)', line):
                    continue
                if len(line) > MAX_TITLE_LENGTH:
                    return line[:MAX_TITLE_LENGTH] + "…"
                return line
    return "untitled"


def sanitize_filename(name):
    return re.sub(r'[/\\:*?"<>|]', '', name).strip()


def format_session_note(session, segments, project_name):
    title = extract_title(segments)
    date = session["started_at"][:10]

    lines = [
        "---",
        f"date: {date}",
        f"project: {project_name}",
        f"session_id: {session['session_id']}",
        f"segments: {len(segments)}",
        "type: session",
        "---",
        "",
        f"# {title}",
        "",
        f"**Project**: [[{project_name}]] | **Date**: {date} | **Segments**: {len(segments)}",
        "",
        "---",
        "",
    ]

    for seg in segments:
        role_label = "User" if seg["role"] == "user" else "Assistant"
        ts = seg["timestamp"][:16].replace("T", " ") if seg["timestamp"] else ""
        lines.append(f"### {role_label} {ts}")
        lines.append("")
        lines.append(seg["content"][:2000])
        lines.append("")

    return "\n".join(lines), title


def format_index_note(sessions_by_project):
    total = sum(len(v) for v in sessions_by_project.values())
    lines = [
        "---",
        "type: index",
        f"updated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}",
        "---",
        "",
        "# Session Index",
        "",
        f"Total: {total} sessions",
        "",
    ]

    for project_name in sorted(sessions_by_project.keys()):
        entries = sessions_by_project[project_name]
        lines.append(f"## {project_name} ({len(entries)} sessions)")
        lines.append("")
        for entry in sorted(entries, key=lambda e: e["date"], reverse=True):
            lines.append(f"- {entry['date']} [[{entry['link']}|{entry['title']}]]")
        lines.append("")

    return "\n".join(lines)


def export(vault_dir, since_days=None, project_filter=None):
    if not DB_PATH.exists():
        print("memory.db not found", file=sys.stderr)
        sys.exit(1)

    sessions_dir = vault_dir / "sessions"
    conn = get_connection()
    conn.row_factory = sqlite3.Row

    query = "SELECT * FROM sessions WHERE segment_count > 0"
    params = []

    if since_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        query += " AND started_at >= ?"
        params.append(cutoff)

    if project_filter:
        query += " AND project_path LIKE ?"
        params.append(f"%{project_filter}%")

    query += " ORDER BY started_at DESC"
    sessions = conn.execute(query, params).fetchall()

    if not sessions:
        print("No sessions to export")
        return

    sessions_by_project = {}
    exported = 0

    for session in sessions:
        session_id = session["session_id"]
        project_name = slug_from_project(session["project_path"])

        segments = [
            dict(r) for r in conn.execute(
                "SELECT role, content, timestamp FROM segments WHERE session_id = ? ORDER BY timestamp",
                (session_id,),
            ).fetchall()
        ]

        if not segments:
            continue

        note_content, title = format_session_note(dict(session), segments, project_name)
        date = session["started_at"][:10]
        safe_title = sanitize_filename(title)
        suffix = session_id[:8]
        filename = f"{date}-{safe_title}-{suffix}.md"

        project_dir = sessions_dir / project_name
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / filename).write_text(note_content, encoding="utf-8")

        if project_name not in sessions_by_project:
            sessions_by_project[project_name] = []
        sessions_by_project[project_name].append({
            "date": date,
            "title": safe_title[:40],
            "link": f"sessions/{project_name}/{filename[:-3]}",
        })
        exported += 1

    index_content = format_index_note(sessions_by_project)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / "_index.md").write_text(index_content, encoding="utf-8")

    conn.close()
    print(f"Exported {exported} sessions to {sessions_dir}/")
    print(f"Projects: {', '.join(sorted(sessions_by_project.keys()))}")


def main():
    parser = argparse.ArgumentParser(description="Export myak memory to Obsidian vault")
    parser.add_argument("--vault", help="Path to Obsidian vault directory")
    parser.add_argument("--since", type=int, help="Export sessions from last N days")
    parser.add_argument("--project", help="Filter by project name (partial match)")
    args = parser.parse_args()

    vault = args.vault or OBSIDIAN_VAULT
    if not vault:
        print("Vault path required: --vault <path> or set MYAK_OBSIDIAN_VAULT", file=sys.stderr)
        sys.exit(1)

    export(Path(vault), since_days=args.since, project_filter=args.project)


if __name__ == "__main__":
    main()
