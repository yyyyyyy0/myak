"""環境非依存のパス解決・設定管理。"""

import os
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", Path.home() / ".claude"))
MEMORY_DIR = Path(os.environ.get("MYAK_DIR", CLAUDE_DIR / "memory"))
DB_PATH = MEMORY_DIR / "memory.db"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Obsidian vault は環境変数 or CLI 引数で指定
OBSIDIAN_VAULT = os.environ.get("MYAK_OBSIDIAN_VAULT", "")

MIN_CONTENT_LENGTH = 50
MAX_CONTENT_LENGTH = 4000

# 時間減衰: 半減期30日
HALF_LIFE_DAYS = 30
MAX_RESULTS = 5
MAX_SNIPPET_CHARS = 500
MAX_CODEX_CHARS = 1000

# Hook 用: 注入量を抑える
HOOK_MAX_RESULTS = 3
HOOK_SNIPPET_CHARS = 220

# 検索品質: 閾値フィルタ
MIN_RELATIVE_SCORE = 0.35  # best score 比
MIN_ABSOLUTE_SCORE = 0.03
MIN_MATCHING_TOKENS = 2    # TOKEN_MATCH_GUARD 以上のトークン数で適用
TOKEN_MATCH_GUARD = 3


def home_parts():
    """ホームディレクトリのパーツを返す（slug 変換時の除外用）。"""
    return set(Path.home().parts[1:])  # '/' を除く


def ensure_memory_dir():
    """memory ディレクトリが存在しなければ作成する。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
