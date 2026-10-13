"""~/.claude/settings.json の myak hook エントリを書き換え/削除する (D8)。

myak のエントリ以外は一切変更しない。書き込みは atomic (tmp + os.replace)、
変更前に `<path>.bak-<ts>` を作成する。冪等。
"""

import copy
import difflib
import json
import os
import re
import shlex
import shutil
import tempfile
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path

from myak import config

MARKER_BASENAMES = frozenset({"myak-index", "myak-query", "myak-hook"})
_SHELL_SEPARATORS = frozenset({"&&", "||", ";", "|"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
HOOK_PLACEHOLDER = "__MYAK_HOOK__"
TEMPLATE_PATH = Path(str(files("myak") / "templates")) / "hooks.json"
EVENT_ORDER = ("Stop", "SessionEnd", "UserPromptSubmit")


def default_settings_path():
    return config.CLAUDE_DIR / "settings.json"


class SettingsChangedError(ValueError):
    """読み込み後に settings.json が別プロセスに書き換えられた。"""


def validate_launcher(launcher):
    """ランチャーのパスは絶対パスで、制御文字 (改行含む) を含まないこと。"""
    text = str(launcher)
    if not text or not os.path.isabs(text):
        raise ValueError(f"launcher must be an absolute path: {text!r}")
    if _CONTROL.search(text):
        raise ValueError("launcher path contains control characters")
    return text


def _tokens(command):
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def is_myak_command(command):
    """`command` が myak の hook か。token の basename が正確に一致する場合のみ真。"""
    tokens = _tokens(command)
    for i, tok in enumerate(tokens):
        if os.path.basename(tok) in MARKER_BASENAMES:
            return True
        # `myak hook ...` はコマンド位置 (先頭 or 区切りの直後) に限る。`echo myak hook` は除外
        if (
            os.path.basename(tok) == "myak"
            and i + 1 < len(tokens)
            and tokens[i + 1] == "hook"
            and (i == 0 or tokens[i - 1] in _SHELL_SEPARATORS)
        ):
            return True
    return False


def is_myak_hook(hook):
    if not isinstance(hook, dict):
        return False
    command = hook.get("command")
    return isinstance(command, str) and is_myak_command(command)


def render_template(launcher):
    """hooks.json テンプレートを描画する。コマンドには shlex.quote 済みのパスを入れる。"""
    quoted = shlex.quote(validate_launcher(launcher))
    data = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    for groups in data.values():
        for group in groups:
            for hook in group["hooks"]:
                hook["command"] = hook["command"].replace(HOOK_PLACEHOLDER, quoted)
    return data


def _template_entries(launcher):
    return {event: groups[0]["hooks"][0] for event, groups in render_template(launcher).items()}


def _remove(hooks):
    """myak の hook を落とす。空になったグループ/イベントも (今回空にした分だけ) 落とす。"""
    removed = 0
    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        kept_groups = []
        event_removed = 0
        for group in groups:
            inner = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(inner, list):
                kept_groups.append(group)
                continue
            kept = [h for h in inner if not is_myak_hook(h)]
            event_removed += len(inner) - len(kept)
            if len(kept) == len(inner):
                kept_groups.append(group)
            elif kept:
                kept_groups.append({**group, "hooks": kept})
        removed += event_removed
        if event_removed and not kept_groups:
            del hooks[event]
        else:
            hooks[event] = kept_groups
    return removed


def _rewrite(hooks, launcher):
    entries = _template_entries(launcher)
    changed = 0
    for event in EVENT_ORDER:
        desired = entries[event]
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            continue
        placed = False
        for group in groups:
            inner = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(inner, list):
                continue
            new_inner = []
            for h in inner:
                if not is_myak_hook(h):
                    new_inner.append(h)
                elif not placed:
                    placed = True
                    new_inner.append(desired)
                    changed += 0 if h == desired else 1
                else:
                    changed += 1  # 重複する myak エントリは落とす
            group["hooks"] = new_inner
        if not placed:
            groups.append({"hooks": [copy.deepcopy(desired)]})
            changed += 1
    return changed


def _pretty(data):
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def migrate_settings(path=None, mode="rewrite", launcher=None, dry_run=False):
    """settings.json を移行する。結果 dict を返す。

    keys: path, mode, changed, changes, diff, backup
    """
    if mode not in ("rewrite", "remove"):
        raise ValueError(f"unknown mode: {mode!r}")
    if mode == "rewrite" and not launcher:
        raise ValueError("rewrite mode requires a launcher path (see `myak install-hooks`)")

    if mode == "rewrite":
        validate_launcher(launcher)
    path = Path(path) if path is not None else default_settings_path()
    real = Path(os.path.realpath(path))  # シンボリックリンクは実体を in-place で編集する
    result = {
        "path": str(path), "mode": mode, "changed": False, "changes": 0,
        "diff": "", "backup": None,
    }
    if not real.is_file():
        result["status"] = "no-file"
        return result

    st = real.stat()
    read_sig = (st.st_mtime_ns, st.st_size)
    original_text = real.read_text(encoding="utf-8")
    try:
        data = json.loads(original_text)
    except ValueError as e:
        raise ValueError(f"{path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")

    new = copy.deepcopy(data)
    hooks = new.get("hooks")
    if hooks is None and mode == "rewrite":
        hooks = new["hooks"] = {}
    if not isinstance(hooks, dict):
        result["status"] = "no-hooks"
        return result

    changes = _remove(hooks) if mode == "remove" else _rewrite(hooks, launcher)
    if new == data:
        result["status"] = "unchanged"
        return result

    result["changes"] = changes
    result["changed"] = True
    result["diff"] = "".join(
        difflib.unified_diff(
            _pretty(data).splitlines(keepends=True),
            _pretty(new).splitlines(keepends=True),
            fromfile=str(path), tofile=f"{path} (migrated)",
        )
    )
    if dry_run:
        result["status"] = "dry-run"
        return result

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = real.with_name(f"{real.name}.bak-{stamp}")
    shutil.copy2(real, backup)
    result["backup"] = str(backup)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{real.name}.", suffix=".tmp", dir=str(real.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_pretty(new))
        shutil.copymode(real, tmp_name)
        now = real.stat()
        if (now.st_mtime_ns, now.st_size) != read_sig:
            raise SettingsChangedError(
                f"{path} changed while migrating; aborted without writing (backup: {backup})"
            )
        os.replace(tmp_name, real)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    result["status"] = "migrated"
    return result
