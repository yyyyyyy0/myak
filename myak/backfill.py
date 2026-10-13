"""既存セッションの一括バックフィル。"""

import argparse
import sys
import time

from myak import config
from myak.config import ensure_memory_dir, get_connection
from myak.db import require_current
from myak.indexer import index_transcript_tree, init_db


def backfill(project_filter=None, rebuild_fts=False):
    """全 transcript (サブエージェント含む) をインデックスする。終了コードを返す。"""
    ensure_memory_dir()
    conn = get_connection()
    try:
        init_db(conn)
        if not require_current(conn):
            print(
                "Database schema is not current; run `myak migrate` before backfill.",
                file=sys.stderr,
            )
            return 1

        total_sessions = 0
        total_segments = 0
        skipped = 0
        start = time.time()

        projects_dir = config.PROJECTS_DIR
        project_dirs = sorted(projects_dir.iterdir()) if projects_dir.is_dir() else []

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
                count = index_transcript_tree(conn, jsonl_path, project_slug, final=True)
                if count > 0:
                    total_sessions += 1
                    total_segments += count
                    print(f"  + {jsonl_path.name}: {count} segments")
                else:
                    skipped += 1

        if rebuild_fts:
            conn.execute("INSERT INTO segments_fts(segments_fts) VALUES('rebuild')")
            print("FTS index rebuilt")

        elapsed = time.time() - start
        print("\n--- Backfill complete ---")
        print(f"Sessions indexed: {total_sessions}")
        print(f"Segments created: {total_segments}")
        print(f"Skipped (already indexed or empty): {skipped}")
        print(f"Time: {elapsed:.1f}s")
        return 0
    finally:
        conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Backfill all session transcripts")
    parser.add_argument("project", nargs="?", help="Filter by project slug (partial match)")
    parser.add_argument("--project", dest="project_opt", help="Same as the positional filter")
    parser.add_argument(
        "--rebuild-fts", action="store_true", help="Rebuild the FTS index after indexing"
    )
    args = parser.parse_args(argv)

    project = args.project_opt or args.project
    sys.exit(backfill(project_filter=project, rebuild_fts=args.rebuild_fts))


if __name__ == "__main__":
    main()
