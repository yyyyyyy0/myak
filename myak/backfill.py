"""既存セッションの一括バックフィル。"""

import argparse
import sys
import time

from myak.config import PROJECTS_DIR, ensure_memory_dir, get_connection
from myak.indexer import index_session, init_db


def backfill(project_filter=None):
    ensure_memory_dir()
    conn = get_connection()
    init_db(conn)

    total_sessions = 0
    total_segments = 0
    skipped = 0
    start = time.time()

    project_dirs = sorted(PROJECTS_DIR.iterdir()) if PROJECTS_DIR.is_dir() else []

    for project_dir in project_dirs:
        if not project_dir.is_dir():
            continue
        if project_filter and project_filter not in project_dir.name:
            continue

        jsonl_files = sorted(project_dir.glob("*.jsonl"))
        if not jsonl_files:
            continue

        project_slug = project_dir.name
        print(f"\n[{project_slug}] {len(jsonl_files)} files")

        for jsonl_path in jsonl_files:
            count = index_session(conn, jsonl_path, project_slug)
            if count > 0:
                total_sessions += 1
                total_segments += count
                print(f"  + {jsonl_path.name}: {count} segments")
            else:
                skipped += 1

    elapsed = time.time() - start
    print(f"\n--- Backfill complete ---")
    print(f"Sessions indexed: {total_sessions}")
    print(f"Segments created: {total_segments}")
    print(f"Skipped (already indexed or empty): {skipped}")
    print(f"Time: {elapsed:.1f}s")

    conn.close()


def main():
    parser = argparse.ArgumentParser(description="Backfill all session transcripts")
    parser.add_argument("project", nargs="?", help="Filter by project slug (partial match)")
    args = parser.parse_args()

    backfill(project_filter=args.project)


if __name__ == "__main__":
    main()
