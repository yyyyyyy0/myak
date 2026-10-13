"""settings_migrate: myak エントリだけを書き換え/削除する。"""

import copy
import json
import stat

import pytest

from myak import cli, config, settings_migrate
from myak.settings_migrate import is_myak_hook, migrate_settings

LAUNCHER = "/home/u/.claude/memory/bin/myak-hook"

BROKEN_QUERY = (
    "query=$(cat | python3 -c \"import sys,json; d=json.load(sys.stdin); "
    "print(d.get('message',''))\"); [ -n \"$query\" ] && myak-query --hook \"$query\""
)

OTHER_PRE = {
    "matcher": "Bash",
    "hooks": [{"type": "command", "command": "/usr/local/bin/guard.sh", "timeout": 5}],
}
OTHER_POST = {
    "matcher": "Edit|Write",
    "hooks": [{"type": "command", "command": "ruff format ."}],
}
OTHER_STOP = {"hooks": [{"type": "command", "command": "notify-send done"}]}
MYAK_STOP = {"matcher": "*", "hooks": [{"type": "command", "command": "myak-index", "async": True}]}
OTHER_PROMPT = {"hooks": [{"type": "command", "command": "~/bin/prompt-logger"}]}
MYAK_PROMPT = {"matcher": "*", "hooks": [{"type": "command", "command": BROKEN_QUERY}]}


def owner_settings():
    return {
        "model": "opus",
        "permissions": {"allow": ["Bash(ls:*)"], "deny": []},
        "env": {"FOO": "bar"},
        "hooks": {
            "PreToolUse": [copy.deepcopy(OTHER_PRE)],
            "PostToolUse": [copy.deepcopy(OTHER_POST)],
            "Stop": [copy.deepcopy(OTHER_STOP), copy.deepcopy(MYAK_STOP)],
            "UserPromptSubmit": [copy.deepcopy(MYAK_PROMPT), copy.deepcopy(OTHER_PROMPT)],
        },
        "日本語": "そのまま",
    }


@pytest.fixture
def settings(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(owner_settings(), indent=2, ensure_ascii=False))
    p.chmod(0o640)
    return p


def load(p):
    return json.loads(p.read_text())


def myak_commands(data):
    return [
        h["command"]
        for groups in data.get("hooks", {}).values()
        for g in groups
        for h in g.get("hooks", [])
        if is_myak_hook(h)
    ]


def test_is_myak_hook():
    for cmd in ("myak-index", "x/myak-query --hook", "/a/myak-hook stop", "myak hook prompt"):
        assert is_myak_hook({"command": cmd})
    for h in ({"command": "echo hi"}, {"command": 5}, {}, "myak-index", None):
        assert not is_myak_hook(h)


def test_remove_drops_only_myak(settings):
    r = migrate_settings(settings, mode="remove")
    assert r["changed"] and r["status"] == "migrated" and r["changes"] == 2
    got = load(settings)
    want = owner_settings()
    want["hooks"]["Stop"] = [OTHER_STOP]
    want["hooks"]["UserPromptSubmit"] = [OTHER_PROMPT]
    assert got == want
    assert myak_commands(got) == []


def test_remove_drops_empty_group_and_event(tmp_path):
    p = tmp_path / "s.json"
    data = {"hooks": {"Stop": [MYAK_STOP], "PreToolUse": [OTHER_PRE]}, "x": 1}
    p.write_text(json.dumps(data))
    migrate_settings(p, mode="remove")
    assert load(p) == {"hooks": {"PreToolUse": [OTHER_PRE]}, "x": 1}


def test_remove_keeps_group_with_other_hooks(tmp_path):
    p = tmp_path / "s.json"
    group = {"matcher": "*", "hooks": [{"command": "myak-index"}, {"command": "keep-me"}]}
    p.write_text(json.dumps({"hooks": {"Stop": [group]}}))
    migrate_settings(p, mode="remove")
    assert load(p) == {"hooks": {"Stop": [{"matcher": "*", "hooks": [{"command": "keep-me"}]}]}}


def test_remove_leaves_preexisting_empty_things(tmp_path):
    p = tmp_path / "s.json"
    data = {"hooks": {"Stop": [{"hooks": []}], "Other": []}}
    p.write_text(json.dumps(data))
    r = migrate_settings(p, mode="remove")
    assert not r["changed"]
    assert load(p) == data


def test_rewrite_replaces_and_adds_session_end(settings):
    r = migrate_settings(settings, mode="rewrite", launcher=LAUNCHER)
    assert r["changed"]
    got = load(settings)
    want = owner_settings()
    want["hooks"]["Stop"] = [
        OTHER_STOP,
        {"matcher": "*", "hooks": [{
            "type": "command", "command": f"{LAUNCHER} stop", "async": True,
            "timeout": 60, "statusMessage": "myak: indexing",
        }]},
    ]
    want["hooks"]["UserPromptSubmit"] = [
        {"matcher": "*", "hooks": [{
            "type": "command", "command": f"{LAUNCHER} prompt", "timeout": 10,
        }]},
        OTHER_PROMPT,
    ]
    want["hooks"]["SessionEnd"] = [
        {"hooks": [{"type": "command", "command": f"{LAUNCHER} session-end", "timeout": 10}]}
    ]
    assert got == want
    # 無関係エントリは完全一致
    for k in ("PreToolUse", "PostToolUse"):
        assert got["hooks"][k] == owner_settings()["hooks"][k]
    assert len(myak_commands(got)) == 3


def test_rewrite_requires_launcher(settings):
    with pytest.raises(ValueError, match="launcher"):
        migrate_settings(settings, mode="rewrite")


def test_unknown_mode(settings):
    with pytest.raises(ValueError, match="mode"):
        migrate_settings(settings, mode="nuke")


@pytest.mark.parametrize("mode", ["rewrite", "remove"])
def test_idempotent(settings, mode):
    migrate_settings(settings, mode=mode, launcher=LAUNCHER)
    after_first = settings.read_text()
    baks = sorted(settings.parent.glob("settings.json.bak-*"))
    r = migrate_settings(settings, mode=mode, launcher=LAUNCHER)
    assert not r["changed"] and r["status"] == "unchanged" and r["backup"] is None
    assert settings.read_text() == after_first
    assert sorted(settings.parent.glob("settings.json.bak-*")) == baks


def test_rewrite_dedupes_multiple_myak_entries(tmp_path):
    p = tmp_path / "s.json"
    data = {"hooks": {"Stop": [
        {"hooks": [{"command": "myak-index"}]}, {"hooks": [{"command": "myak-index"}]},
    ]}}
    p.write_text(json.dumps(data))
    migrate_settings(p, mode="rewrite", launcher=LAUNCHER)
    stop_cmds = [h["command"] for g in load(p)["hooks"]["Stop"] for h in g["hooks"]]
    assert stop_cmds == [f"{LAUNCHER} stop"]


def test_rewrite_without_hooks_key(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"model": "x"}))
    migrate_settings(p, mode="rewrite", launcher=LAUNCHER)
    got = load(p)
    assert got["model"] == "x"
    assert set(got["hooks"]) == {"Stop", "SessionEnd", "UserPromptSubmit"}


def test_backup_created_with_original_content(settings):
    original = settings.read_text()
    r = migrate_settings(settings, mode="remove")
    baks = list(settings.parent.glob("settings.json.bak-*"))
    assert len(baks) == 1
    assert r["backup"] == str(baks[0])
    assert baks[0].read_text() == original


def test_permissions_preserved_and_no_tmp_left(settings):
    migrate_settings(settings, mode="remove")
    assert stat.S_IMODE(settings.stat().st_mode) == 0o640
    assert not list(settings.parent.glob(".settings.json.*"))


def test_dry_run_writes_nothing_and_returns_diff(settings):
    before = settings.read_text()
    r = migrate_settings(settings, mode="rewrite", launcher=LAUNCHER, dry_run=True)
    assert r["status"] == "dry-run" and r["changed"] and r["backup"] is None
    assert settings.read_text() == before
    assert not list(settings.parent.glob("settings.json.bak-*"))
    lines = r["diff"].splitlines()
    assert any(ln.startswith("+") and f"{LAUNCHER} stop" in ln for ln in lines)
    assert any(ln.startswith("-") and "myak-index" in ln for ln in lines)


def test_atomic_failure_keeps_original(settings, monkeypatch):
    before = settings.read_text()

    def boom(src, dst):
        raise OSError("disk")

    monkeypatch.setattr(settings_migrate.os, "replace", boom)
    with pytest.raises(OSError):
        migrate_settings(settings, mode="remove")
    assert settings.read_text() == before
    assert not list(settings.parent.glob(".settings.json.*"))


def test_missing_file_and_no_hooks(tmp_path):
    assert migrate_settings(tmp_path / "none.json", mode="remove")["status"] == "no-file"
    p = tmp_path / "s.json"
    p.write_text("{}")
    assert migrate_settings(p, mode="remove")["status"] == "no-hooks"


def test_invalid_json_and_non_object(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{bad")
    with pytest.raises(ValueError, match="not valid JSON"):
        migrate_settings(p, mode="remove")
    p.write_text("[1]")
    with pytest.raises(ValueError, match="JSON object"):
        migrate_settings(p, mode="remove")


def test_tolerates_odd_shapes(tmp_path):
    p = tmp_path / "s.json"
    mine = {"hooks": [{"command": "myak-index"}]}
    data = {"hooks": {"Stop": "weird", "X": [5, {"hooks": "no"}, mine]}}
    p.write_text(json.dumps(data))
    migrate_settings(p, mode="remove")
    assert load(p) == {"hooks": {"Stop": "weird", "X": [5, {"hooks": "no"}]}}
    p.write_text(json.dumps(data))
    migrate_settings(p, mode="rewrite", launcher=LAUNCHER)
    got = load(p)
    assert got["hooks"]["Stop"] == "weird"
    assert got["hooks"]["X"][:2] == [5, {"hooks": "no"}]


def test_default_path_uses_claude_dir(tmp_path, monkeypatch):
    import myak.config

    monkeypatch.setattr(myak.config, "CLAUDE_DIR", tmp_path)
    assert settings_migrate.default_settings_path() == tmp_path / "settings.json"


# ---- CLI wiring ----------------------------------------------------------------------------

def test_cli_dry_run_then_real(settings, capsys):
    before = settings.read_text()
    assert cli.main([
        "migrate-settings", "--mode", "rewrite", "--launcher", LAUNCHER,
        "--dry-run", "--path", str(settings),
    ]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "@@" in out
    assert settings.read_text() == before

    assert cli.main([
        "migrate-settings", "--mode", "remove", "--path", str(settings),
    ]) == 0
    out = capsys.readouterr().out
    assert "Backup:" in out
    assert myak_commands(load(settings)) == []


def test_cli_rewrite_defaults_launcher(settings, db_env, capsys):
    assert cli.main(["migrate-settings", "--mode", "rewrite", "--path", str(settings)]) == 0
    assert str(config.STATE_DIR / "bin" / "myak-hook") in settings.read_text()


def test_cli_error_on_bad_json(tmp_path, capsys):
    p = tmp_path / "s.json"
    p.write_text("{")
    assert cli.main(["migrate-settings", "--mode", "remove", "--path", str(p)]) == 1
    assert "not valid JSON" in capsys.readouterr().err
