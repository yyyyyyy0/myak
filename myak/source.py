"""記憶を書き込んだエージェント (source) の名前規則と導出。

source は自己申告であり認証ではない (DB に書ける任意のプロセスが名乗れる)。
"""

import os
import re

SOURCE_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
SOURCE_CLAUDE_CODE = "claude-code"  # 予約: トランスクリプトの indexer だけが書く
SOURCE_MCP_FALLBACK = "mcp"
SOURCE_CLI_DEFAULT = "cli"
SOURCE_ENV = "MYAK_SOURCE"
SOURCE_MAX_LEN = 32

# MCP クライアント名 -> 保存する source (Claude Desktop は "claude-ai" で名乗る)
_CLIENT_ALIASES = {"claude-ai": "claude-desktop"}


class InvalidSource(ValueError):
    """source 名が規則に合わない、または予約名。"""


def validate_source(value):
    """明示指定された source を検証して返す (小文字化する)。不正・予約名は InvalidSource。"""
    if not isinstance(value, str):
        raise InvalidSource("source must be a string")
    name = value.strip().lower()
    if not SOURCE_RE.fullmatch(name):
        raise InvalidSource(
            "Invalid source (1-32 chars of a-z 0-9 . _ -, starting with a letter or digit)"
        )
    if name == SOURCE_CLAUDE_CODE:
        raise InvalidSource(f"source '{SOURCE_CLAUDE_CODE}' is reserved for transcript indexing")
    return name


def cli_default_source():
    """CLI の既定 source: 環境変数 MYAK_SOURCE (検証あり)、無ければ 'cli'。"""
    env = os.environ.get(SOURCE_ENV)
    if env is None or not env.strip():
        return SOURCE_CLI_DEFAULT
    return validate_source(env)


def source_from_client_name(name):
    """MCP clientInfo.name から source を導出する。決して例外を投げず、不正なら 'mcp'。"""
    try:
        if not isinstance(name, str):
            return SOURCE_MCP_FALLBACK
        norm = re.sub(r"[^a-z0-9._-]", "-", name.strip().lower())
        norm = _CLIENT_ALIASES.get(norm, norm)[:SOURCE_MAX_LEN]
        if not SOURCE_RE.fullmatch(norm):
            return SOURCE_MCP_FALLBACK
        if norm == SOURCE_CLAUDE_CODE:
            norm = f"{SOURCE_CLAUDE_CODE}-mcp"  # transcript 由来の行と区別する
        return norm
    except Exception:  # noqa: BLE001
        return SOURCE_MCP_FALLBACK


def derive_legacy_source(stored, uuid):
    """読み出し用: 保存値があればそれ、無ければ uuid から推定する (旧データ)。"""
    if stored:
        return stored
    if uuid is None or not str(uuid).startswith("save:"):
        return SOURCE_CLAUDE_CODE
    return None
