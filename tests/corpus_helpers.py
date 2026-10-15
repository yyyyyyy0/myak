"""Tiny synthetic corpus on the real schema (fixed ids / sessions) for search tests.

Not a conftest; imported explicitly. Segment ids are 1..N in the order below.
"""

from myak.config import get_connection
from myak.indexer import init_db

SESSION_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
SESSION_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
SESSION_C = "cccccccc-3333-4333-8333-cccccccccccc"

SESSIONS = (
    (SESSION_A, "-Users-x-alpha"),
    (SESSION_B, "-Users-x-beta"),
    (SESSION_C, "-Users-x-gamma"),
)

# (session_id, role, content)
SEGMENTS = (
    (SESSION_A, "user", "automobile repair notes for the garage"),           # 1
    (SESSION_A, "assistant", "login flow redesign discussion about the signin page"),  # 2
    (SESSION_B, "user", "gardening tips about tomatoes and compost"),         # 3
    (SESSION_B, "assistant", "database migration checklist for the postgres cluster"),  # 4
    (SESSION_C, "user", "kubernetes deployment rollout strategy notes"),      # 5
    (SESSION_C, "user", "garage door opener installation manual"),            # 6
)


def build_corpus(db_env, day="2026-10-14"):
    """Create the DB at db_env.db_path and return an open read-write connection."""
    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    init_db(conn)
    stamp = f"{day}T12:00:00.000Z"
    for session_id, project in SESSIONS:
        conn.execute(
            "INSERT INTO sessions(session_id, project_path, started_at, segment_count, indexed_at)"
            " VALUES (?, ?, ?, 0, ?)",
            (session_id, project, stamp, stamp),
        )
    for session_id, role, content in SEGMENTS:
        conn.execute(
            "INSERT INTO segments(session_id, role, content, timestamp, uuid, agent_id)"
            " VALUES (?, ?, ?, ?, NULL, '')",
            (session_id, role, content, stamp),
        )
    return conn
