"""Plugin layout checks (D11): manifest, hooks, MCP wiring, skill, launcher."""
import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
RUN_SH = REPO / "hooks" / "run.sh"
JSON_FILES = [
    REPO / ".claude-plugin" / "plugin.json",
    REPO / ".claude-plugin" / "marketplace.json",
    REPO / "hooks" / "hooks.json",
    REPO / ".mcp.json",
]


@pytest.mark.parametrize("path", JSON_FILES, ids=lambda p: p.name)
def test_json_parses(path):
    assert isinstance(json.loads(path.read_text()), dict)


def test_hook_events_exact():
    hooks = json.loads((REPO / "hooks" / "hooks.json").read_text())["hooks"]
    assert set(hooks) == {"Stop", "SessionEnd", "UserPromptSubmit", "SessionStart"}
    assert hooks["SessionStart"][0]["matcher"] == "startup|clear|compact"
    for ev in ("Stop", "SessionEnd", "UserPromptSubmit"):
        assert "matcher" not in hooks[ev][0]


def test_hook_commands_quoted_and_use_plugin_root():
    hooks = json.loads((REPO / "hooks" / "hooks.json").read_text())["hooks"]
    for groups in hooks.values():
        for group in groups:
            for h in group["hooks"]:
                cmd = h["command"]
                assert '"${CLAUDE_PLUGIN_ROOT}/hooks/run.sh"' in cmd
                assert h["type"] == "command"


def test_stop_is_async():
    hooks = json.loads((REPO / "hooks" / "hooks.json").read_text())["hooks"]
    assert hooks["Stop"][0]["hooks"][0]["async"] is True


def test_mcp_json_references_run_sh():
    cfg = json.loads((REPO / ".mcp.json").read_text())["mcpServers"]["myak"]
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" in cfg["command"] + " ".join(cfg.get("args", []))
    assert "mcp" in cfg["args"]


def test_run_sh_executable_and_syntax():
    assert os.access(RUN_SH, os.X_OK)
    subprocess.run(["sh", "-n", str(RUN_SH)], check=True)


def test_manifest_version_matches_pyproject():
    manifest = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text())
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert manifest["version"] == pyproject["project"]["version"]
    assert manifest["name"] == "myak"


def test_marketplace_lists_plugin():
    mp = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    assert [p["name"] for p in mp["plugins"]] == ["myak"]
    assert mp["plugins"][0]["source"] == "./"


def test_skill_has_frontmatter():
    text = (REPO / "skills" / "myak-recall" / "SKILL.md").read_text()
    assert text.startswith("---\n")
    front = text.split("---\n")[1]
    assert "name: myak-recall" in front
    assert "description:" in front
    assert "myak_search" in text and "myak_save" in text and "<past-memory>" in text


# ---- run.sh behaviour -------------------------------------------------------

def _run(home, args, extra_env=None):
    env = {"HOME": str(home), **(extra_env or {})}
    return subprocess.run(
        ["env", "-i", *[f"{k}={v}" for k, v in env.items()], "sh", str(RUN_SH), *args],
        input="{}", capture_output=True, text=True, timeout=10,
    )


def _fake_myak(path, marker):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\n')
    path.chmod(0o755)


def test_run_sh_hook_event(tmp_path):
    marker = tmp_path / "argv"
    _fake_myak(tmp_path / ".local" / "bin" / "myak", marker)
    assert _run(tmp_path, ["prompt"]).returncode == 0
    assert marker.read_text().strip() == "hook prompt"


def test_run_sh_mcp_passes_through(tmp_path):
    marker = tmp_path / "argv"
    _fake_myak(tmp_path / ".local" / "bin" / "myak", marker)
    assert _run(tmp_path, ["mcp"]).returncode == 0
    assert marker.read_text().strip() == "mcp"


def test_run_sh_myak_bin_wins(tmp_path):
    marker = tmp_path / "argv"
    override = tmp_path / "override"
    _fake_myak(override, marker)
    _fake_myak(tmp_path / ".local" / "bin" / "myak", tmp_path / "other")
    _run(tmp_path, ["stop"], {"MYAK_BIN": override})
    assert marker.exists() and not (tmp_path / "other").exists()


def test_run_sh_nothing_installed_exits_zero(tmp_path):
    r = _run(tmp_path, ["prompt"])
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""


def test_run_sh_without_home_or_event_exits_zero(tmp_path):
    r = subprocess.run(["env", "-i", "sh", str(RUN_SH), "prompt"], capture_output=True, text=True)
    assert r.returncode == 0
    assert _run(tmp_path, []).returncode == 0
