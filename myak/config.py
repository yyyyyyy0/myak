"""環境非依存のパス解決・設定管理。"""

import os
import sqlite3
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_DIR", Path.home() / ".claude"))
MEMORY_DIR = Path(os.environ.get("MYAK_DIR", CLAUDE_DIR / "memory"))
DB_PATH = MEMORY_DIR / "memory.db"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# 機械ローカルの状態 (インクリメンタル索引のカーソル)。同期対象外。
STATE_DIR = Path(os.environ.get("MYAK_STATE_DIR", CLAUDE_DIR / "myak-state"))
STATE_DB_PATH = STATE_DIR / "state.db"
# ベクトル索引 (派生キャッシュ。機械ローカル・同期対象外・再構築可能)
VEC_DB_PATH = STATE_DIR / "memory.vec.db"
MODELS_DIR = STATE_DIR / "models"
# ベクトル検索の距離上限 (vec0 は distance_metric=cosine; 0=同一, 2=逆向き)。超える近傍は捨てる
VEC_MAX_DISTANCE = 0.6
# SessionEnd の自動埋め込み 1 回あたりの上限件数 (残りは次回以降 / `myak embed`)
HOOK_EMBED_LIMIT = 500
# fastembed 非対応の multilingual-e5-small の代わりに、対応済みの多言語 384 次元小型モデルを使う
VEC_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"

# Obsidian vault は環境変数 or CLI 引数で指定
OBSIDIAN_VAULT = os.environ.get("MYAK_OBSIDIAN_VAULT", "")

MIN_CONTENT_LENGTH = 50
MAX_CONTENT_LENGTH = 4000

# 時間減衰: 半減期30日
HALF_LIFE_DAYS = 30
MAX_RESULTS = 5
# subagent 由来セグメントの順位重み
SUBAGENT_WEIGHT = 0.5
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


def vec_disabled():
    """MYAK_DISABLE_VEC でベクトル検索を無効化するキルスイッチ。

    1 / true / yes / on (大文字小文字不問) のときだけ有効。それ以外 (空・0・false 等) は無効。
    """
    return os.environ.get("MYAK_DISABLE_VEC", "").strip().lower() in ("1", "true", "yes", "on")


def home_parts():
    """ホームディレクトリのパーツを返す（slug 変換時の除外用）。"""
    return set(Path.home().parts[1:])  # '/' を除く


def ensure_memory_dir():
    """memory ディレクトリが存在しなければ作成する。"""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def ensure_state_dir():
    """機械ローカルの STATE_DIR を 0700 で作成する (同期対象外・実行物/ログ/派生物用)。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass
    return STATE_DIR


def secure_file(path, mode=0o600):
    """作成直後のファイルのモードを絞る (失敗は無視)。"""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def atomic_write(dest, data, mode):
    """tmp + chmod + os.replace で原子的に書く。dest がシンボリックリンクなら先に外す。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink():
        dest.unlink()
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, dest)
    except BaseException:
        if tmp.exists():
            tmp.unlink()
        raise


def get_connection():
    """同期セーフな SQLite 接続を返す。

    DELETE journal で WAL ファイル不要。isolation_level=None (autocommit) なので
    トランザクションは呼び出し側が明示的に BEGIN/COMMIT する。
    """
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=DELETE")
    return conn
