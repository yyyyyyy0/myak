"""~/.claude/settings.json の myak hook エントリを書き換え/削除する (D8)。

myak のエントリ以外は一切変更しない。書き込みは atomic (tmp + os.replace)、
変更前に `<path>.bak-<ts>` を作成する。冪等。
"""

import copy
import difflib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from myak import config

MARKERS = ("myak-index", "myak-query", "myak-hook", "myak hook")
HOOK_PLACEHOLDER = "__MYAK_HOOK__"
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "hooks.json"
EVENT_ORDER = ("Stop", "SessionEnd", "UserPromptSubmit")


def default_settings_path():
    return config.CLAUDE_DIR / "settings.json"


def is_myak_hook(hook):
    if not isinstance(hook, dict):
        return False
    command = hook.get("command")
    return isinstance(command, str) and any(m in command for m in MARKERS)


def _template_entries(launcher):
    raw = TEMPLATE_PATH.read_text(encoding="utf-8").replace(HOOK_PLACEHOLDER, str(launcher))
    return {event: groups[0]["hooks"][0] for event, groups in json.loads(raw).items()}


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

    path = Path(path) if path is not None else default_settings_path()
    result = {
        "path": str(path), "mode": mode, "changed": False, "changes": 0,
        "diff": "", "backup": None,
    }
    if not path.is_file():
        result["status"] = "no-file"
        return result

    original_text = path.read_text(encoding="utf-8")
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
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    result["backup"] = str(backup)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_pretty(new))
        shutil.copymode(path, tmp_name)
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
    result["status"] = "migrated"
    return result
