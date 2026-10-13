"""Claude Code transcript (JSONL) の純粋なパーサ。DB には触れない。

transcript の形式は内部仕様で不安定なため、未知の type / 壊れた行は無視する。
"""

import json
import re
from pathlib import Path

from myak.config import MAX_CONTENT_LENGTH, MIN_CONTENT_LENGTH

# <tag>...</tag> ブロックを除去し、周囲のテキストは残す
NOISE_TAGS = ("system-reminder", "task-notification", "side-sessions-event")
# user ロールで、本文が先頭からこれらで始まるレコードは丸ごと捨てる (slash command の残骸)
DROP_TAGS = (
    "command-name",
    "command-message",
    "local-command-stdout",
    "local-command-stderr",
)

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_BLANKS_RE = re.compile(r"\n{3,}")


def _strip_tag(text, tag, role):
    """<tag>...</tag> を線形時間で除去する (正規表現のバックトラックを避ける)。

    閉じタグが無い場合: user は開きタグから末尾までを捨て、それ以外はそのまま残す。
    """
    open_tag = f"<{tag}>"
    close_tag = f"</{tag}>"
    parts = []
    pos = 0
    while True:
        start = text.find(open_tag, pos)
        if start < 0:
            break
        end = text.find(close_tag, start + len(open_tag))
        if end < 0:
            if role == "user":
                parts.append(text[pos:start])
                return "".join(parts)
            break
        parts.append(text[pos:start])
        pos = end + len(close_tag)
    parts.append(text[pos:])
    return "".join(parts)


def clean_text(text, role=None):
    """ノイズタグ除去・制御文字除去・空行圧縮を行う。"""
    for tag in NOISE_TAGS:
        text = _strip_tag(text, tag, role)
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
                raw = block.get("text")
                text = raw.strip() if isinstance(raw, str) else ""
                if text:
                    texts.append(text)
        return "\n".join(texts)
    return ""


def iter_records_with_start(path, start_offset=0, final=False):
    """(record, start_offset, end_offset) を yield する。

    完結行 (末尾 \\n あり) のみ対象。壊れた JSON 行はスキップするがオフセットは進める。
    末尾の改行なしの行は通常 yield せずオフセットも進めない。
    final=True の場合のみ、JSON として解釈できれば受理してオフセットを EOF まで進める。
    """
    with open(path, "rb") as f:
        f.seek(start_offset)
        offset = start_offset
        for raw in f:
            terminated = raw.endswith(b"\n")
            if not terminated and not final:
                return
            start = offset
            line = raw.strip()
            rec = None
            if line:
                try:
                    rec = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, ValueError, RecursionError):
                    rec = None
            if not terminated and not isinstance(rec, dict):
                # 未完結かつ解釈不能な末尾は消費しない (後続の追記で完結しうる)
                return
            offset += len(raw)
            if isinstance(rec, dict):
                yield rec, start, offset


def iter_records(path, start_offset=0, final=False):
    """完結した行のみ (record, end_offset) として yield する。"""
    for rec, _start, end in iter_records_with_start(path, start_offset, final):
        yield rec, end


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
    role = msg.get("role", rec_type)
    if role == "user" and text.lstrip().startswith(tuple(f"<{t}>" for t in DROP_TAGS)):
        return None

    text = clean_text(text, role)
    if len(text) < MIN_CONTENT_LENGTH:
        return None

    return {
        "uuid": rec.get("uuid"),
        "role": role,
        "content": text[:MAX_CONTENT_LENGTH],
        "timestamp": rec.get("timestamp", ""),
        "is_sidechain": bool(rec.get("isSidechain", False)),
        "agent_id": rec.get("agentId"),
        "cwd": rec.get("cwd"),
    }


def extract_segments(path, start_offset=0, final=False):
    """(segments, new_offset) を返す。new_offset は最後に消費した行の終端。

    各 segment には、そのレコードの開始バイトオフセット `start_offset` が付く。
    """
    segments = []
    offset = start_offset
    for rec, start, end in iter_records_with_start(path, start_offset, final):
        offset = end
        seg = record_to_segment(rec)
        if seg is not None:
            seg["start_offset"] = start
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
