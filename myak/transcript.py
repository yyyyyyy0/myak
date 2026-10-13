"""Claude Code transcript (JSONL) の純粋なパーサ。DB には触れない。

transcript の形式は内部仕様で不安定なため、未知の type / 壊れた行は無視する。
"""

import json
import re
from pathlib import Path

from myak.config import MAX_CONTENT_LENGTH, MIN_CONTENT_LENGTH

# <tag>...</tag> ブロックを除去し、周囲のテキストは残す
NOISE_TAGS = ("system-reminder", "task-notification", "side-sessions-event")
# これらを含むレコードは丸ごと捨てる
DROP_TAGS = (
    "command-name",
    "command-message",
    "command-args",
    "local-command-stdout",
    "local-command-stderr",
)

_NOISE_RE = re.compile(
    "|".join(f"<{t}>.*?</{t}>" for t in NOISE_TAGS),
    re.DOTALL,
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_BLANKS_RE = re.compile(r"\n{3,}")


def clean_text(text):
    """ノイズタグ除去・制御文字除去・空行圧縮を行う。"""
    text = _NOISE_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    text = _BLANKS_RE.sub("\n\n", text)
    return text.strip()


def extract_text_content(content):
    """message.content からテキストを抽出する。str or list[block] に対応。"""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = (block.get("text") or "").strip()
                if text:
                    texts.append(text)
        return "\n".join(texts)
    return ""


def iter_records(path, start_offset=0):
    """完結した行 (末尾 \\n あり) のみ (record, end_offset) として yield する。

    壊れた JSON 行はスキップするがオフセットは進める。
    末尾の改行なしの行は yield せず、オフセットも進めない。
    """
    with open(path, "rb") as f:
        f.seek(start_offset)
        offset = start_offset
        for raw in f:
            if not raw.endswith(b"\n"):
                return
            offset += len(raw)
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(rec, dict):
                yield rec, offset


def _has_tool_result_only(content):
    if not isinstance(content, list) or not content:
        return False
    return all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


def record_to_segment(rec):
    """1 レコードをセグメント dict に変換する。対象外なら None。"""
    if not isinstance(rec, dict):
        return None
    rec_type = rec.get("type", "")
    if rec_type not in ("user", "assistant") or rec.get("isMeta"):
        return None

    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None

    raw_content = msg.get("content", "")
    if _has_tool_result_only(raw_content):
        return None

    text = extract_text_content(raw_content)
    if any(f"<{tag}>" in text for tag in DROP_TAGS):
        return None

    text = clean_text(text)
    if len(text) < MIN_CONTENT_LENGTH:
        return None

    return {
        "uuid": rec.get("uuid"),
        "role": msg.get("role", rec_type),
        "content": text[:MAX_CONTENT_LENGTH],
        "timestamp": rec.get("timestamp", ""),
        "is_sidechain": bool(rec.get("isSidechain", False)),
        "agent_id": rec.get("agentId"),
        "cwd": rec.get("cwd"),
    }


def extract_segments(path, start_offset=0):
    """(segments, new_offset) を返す。new_offset は最後に消費した完結行の終端。"""
    segments = []
    offset = start_offset
    for rec, end_offset in iter_records(path, start_offset):
        offset = end_offset
        seg = record_to_segment(rec)
        if seg is not None:
            segments.append(seg)
    return segments, offset


def read_agent_meta(agent_jsonl_path):
    """agent-<id>.jsonl の隣の .meta.json を読む。無効なら agent_type=unknown。"""
    meta_path = Path(agent_jsonl_path).with_suffix(".meta.json")
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"agent_type": "unknown"}
    if not isinstance(data, dict):
        return {"agent_type": "unknown"}
    result = {k: v for k, v in data.items() if k != "agentType"}
    agent_type = data.get("agentType")
    result["agent_type"] = agent_type if isinstance(agent_type, str) and agent_type else "unknown"
    return result
