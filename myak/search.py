"""ハイブリッド検索: FTS5 + ベクトルを RRF で融合する。

時間減衰と subagent 重みは、融合前に各候補リストの並び (bm25 / コサイン類似度) に掛ける。
RRF の融合スコアは順位由来で幅が狭い (k=60 なら 1 位と 15 位で約 1.2 倍) ため、融合後に
減衰 (半年で約 1/64) を掛けると並びがほぼ新しさだけで決まってしまう。
"""

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


def _prior(row):
    """並び順用の事前重み (時間減衰 x subagent 重み)。関連度の閾値判定には使わない。"""
    timestamp, agent_type = row[2], row[5]
    weight = _query.time_decay_score(timestamp, 1.0)
    if agent_type is not None:
        weight *= SUBAGENT_WEIGHT
    return weight


def _ranked(scores):
    """スコア降順の id リスト (同点は元の候補順)。"""
    return sorted(scores, key=scores.get, reverse=True)


def _result(row, score, source="fts", bm25=None, segment_id=None):
    """検索結果 dict。

    `score` は並び順用 (事前重み込み)。`bm25` は減衰前の FTS 関連度で閾値判定用
    (ベクトルのみのヒットは None)。`segment_id` は内部用 (評価ハーネス向け)。
    公開フォーマッタはどれも参照しない。
    """
    role, content, timestamp, session_id, project, agent_type = row
    return {
        "role": role,
        "content": content[:MAX_SNIPPET_CHARS],
        "timestamp": timestamp[:10] if timestamp else "",
        "session_id": session_id[:8],
        "project": project or "",
        "score": score,
        "agent_type": agent_type,
        "source": source,
        "bm25": bm25,
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


def _vector_hits(conn, query, k, embedder):
    """ベクトル候補 [(id, 距離)] (VEC_MAX_DISTANCE 以内)。使えない/失敗時は None (FTS に縮退)。"""
    try:
        embedder = embedder or _default_embedder()
        hits = vec.vector_candidates(conn, embedder, query, k)
        return [(sid, dist) for sid, dist in hits if dist <= config.VEC_MAX_DISTANCE]
    except vec.VecUnavailable as e:
        logger.info("vector search skipped: %s", e)
    except Exception as e:  # noqa: BLE001 - ベクトル側の不具合で検索全体を落とさない
        logger.warning("vector search failed: %s", e)
    return None


def hybrid_search(query, max_results, *, use_vec=True, embedder=None):
    """FTS (+ ベクトル) で検索し、時間減衰と subagent 重みを加味して並べる。"""
    if not config.DB_PATH.exists():
        return []
    conn = get_connection()
    try:
        if schema_version(conn) < CURRENT_VERSION:
            print("myak: schema outdated, run `myak migrate`", file=sys.stderr)
            return []
        k = max_results * 3
        fts = fts_candidates(conn, query, k)
        vec_hits = _vector_hits(conn, query, k, embedder) if use_vec else None
        ids = {sid for sid, _ in fts} | {sid for sid, _ in vec_hits or ()}
        rows = _fetch_rows(conn, list(ids))
    except sqlite3.OperationalError:  # スキーマ不整合など
        return []
    finally:
        conn.close()
    bm25 = {sid: abs(rank) for sid, rank in fts if sid in rows}
    fts_scores = {sid: rel * _prior(rows[sid]) for sid, rel in bm25.items()}
    if vec_hits is None:
        scores = fts_scores
        sources = dict.fromkeys(scores, "fts")
    else:
        # コサイン類似度 (VEC_MAX_DISTANCE < 1 なら正) に事前重みを掛けて並べ、順位で融合する
        vec_scores = {
            sid: max(0.0, 1.0 - dist) * _prior(rows[sid]) for sid, dist in vec_hits if sid in rows
        }
        fused = rrf_fuse(_ranked(fts_scores), _ranked(vec_scores))
        scores = {sid: s * (RRF_K + 1) for sid, s in fused.items()}
        sources = {
            sid: "both" if sid in fts_scores and sid in vec_scores
            else "fts" if sid in fts_scores else "vec"
            for sid in scores
        }
    results = [
        _result(rows[sid], score, sources[sid], bm25.get(sid), segment_id=sid)
        for sid, score in scores.items()
    ]
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:max_results]
