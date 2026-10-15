"""ハイブリッド検索: FTS5 + ベクトル (RRF 融合) → 時間減衰 / subagent 重み。"""

import functools
import logging
import sqlite3
import sys

from myak import config, vec
from myak import query as _query
from myak.config import MAX_SNIPPET_CHARS, SUBAGENT_WEIGHT, get_connection
from myak.db import CURRENT_VERSION, schema_version

logger = logging.getLogger("myak.search")

RRF_K = 60


def fts_candidates(conn, query, k):
    """FTS5 候補を [(segment_id, rank)] (rank 昇順=良い順) で返す。構文エラーは空。"""
    try:
        rows = conn.execute(
            "SELECT rowid, rank FROM segments_fts WHERE segments_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (_query.tokenize_query(query), k),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r[0], r[1]) for r in rows]


def rrf_fuse(fts_ids, vec_ids, k=RRF_K):
    """Reciprocal Rank Fusion。{segment_id: score} (id で重複排除)。"""
    scores = {}
    for ids in (fts_ids, vec_ids):
        seen = set()
        for rank, sid in enumerate(ids, start=1):
            if sid in seen:
                continue
            seen.add(sid)
            scores[sid] = scores.get(sid, 0.0) + 1.0 / (k + rank)
    return scores


def _fetch_rows(conn, ids):
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT s.id, s.role, s.content, s.timestamp, s.session_id, se.project_path, "
        "s.agent_type FROM segments s JOIN sessions se ON se.session_id = s.session_id "
        f"WHERE s.id IN ({marks})",
        list(ids),
    ).fetchall()
    return {r[0]: r[1:] for r in rows}


def _result(row, base_score, source="fts", segment_id=None):
    """検索結果 dict。`segment_id` は内部用 (評価ハーネス向け)。公開フォーマッタは参照しない。"""
    role, content, timestamp, session_id, project, agent_type = row
    score = _query.time_decay_score(timestamp, base_score)
    if agent_type is not None:
        score *= SUBAGENT_WEIGHT
    return {
        "role": role,
        "content": content[:MAX_SNIPPET_CHARS],
        "timestamp": timestamp[:10] if timestamp else "",
        "session_id": session_id[:8],
        "project": project or "",
        "score": score,
        "agent_type": agent_type,
        "source": source,
        "segment_id": segment_id,
    }


@functools.lru_cache(maxsize=2)
def _cached_fastembed(model_id):
    """プロセス内で 1 つだけ保持 (MCP サーバーが毎回モデルをロードしないように)。"""
    return vec.FastEmbedEmbedder(
        model_id=model_id, cache_dir=config.MODELS_DIR, local_files_only=True
    )


def _default_embedder():
    probe = vec.default_embedder()  # 無効化/モデル未取得の判定 (軽量・ロードしない)
    if isinstance(probe, vec.FastEmbedEmbedder):
        return _cached_fastembed(probe.model_id)
    return probe


def _vector_ids(conn, query, k, embedder):
    """ベクトル候補 id (距離が VEC_MAX_DISTANCE 以内)。使えない/失敗時は None (FTS に縮退)。"""
    try:
        embedder = embedder or _default_embedder()
        hits = vec.vector_candidates(conn, embedder, query, k)
        return [sid for sid, dist in hits if dist <= config.VEC_MAX_DISTANCE]
    except vec.VecUnavailable as e:
        logger.info("vector search skipped: %s", e)
    except Exception as e:  # noqa: BLE001 - ベクトル側の不具合で検索全体を落とさない
        logger.warning("vector search failed: %s", e)
    return None


def hybrid_search(query, max_results, *, use_vec=True, embedder=None):
    """FTS (+ ベクトル) で検索し、時間減衰と subagent 重みで並べ替える。"""
    if not config.DB_PATH.exists():
        return []
    conn = get_connection()
    try:
        if schema_version(conn) < CURRENT_VERSION:
            print("myak: schema outdated, run `myak migrate`", file=sys.stderr)
            return []
        k = max_results * 3
        fts = fts_candidates(conn, query, k)
        vec_ids = _vector_ids(conn, query, k, embedder) if use_vec else None
        fts_set = {sid for sid, _ in fts}
        if vec_ids is None:
            base = {sid: abs(rank) for sid, rank in fts}
            sources = dict.fromkeys(base, "fts")
        else:
            fused = rrf_fuse([sid for sid, _ in fts], vec_ids)
            base = {sid: s * (RRF_K + 1) for sid, s in fused.items()}
            vec_set = set(vec_ids)
            sources = {
                sid: "both" if sid in fts_set and sid in vec_set
                else "fts" if sid in fts_set else "vec"
                for sid in base
            }
        rows = _fetch_rows(conn, list(base))
    except sqlite3.OperationalError:  # スキーマ不整合など
        return []
    finally:
        conn.close()
    results = [
        _result(rows[sid], score, sources[sid], segment_id=sid)
        for sid, score in base.items()
        if sid in rows
    ]
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:max_results]
