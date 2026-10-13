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


def _ladder(path):
    text = path.read_text()
    return text.split("# --- resolve begin ---")[1].split("# --- resolve end ---")[0]


def test_resolve_ladder_identical_in_both_launchers():
    hook_sh = REPO / "myak" / "templates" / "myak-hook.sh"
    assert _ladder(RUN_SH).strip() and _ladder(RUN_SH).encode() == _ladder(hook_sh).encode()


def test_run_sh_hook_mode_exits_zero_when_binary_fails(tmp_path):
    bad = tmp_path / ".local" / "bin" / "myak"
    bad.parent.mkdir(parents=True)
    bad.write_text("#!/bin/sh\nexit 7\n")
    bad.chmod(0o755)
    assert _run(tmp_path, ["prompt"]).returncode == 0
    hook_sh = REPO / "myak" / "templates" / "myak-hook.sh"
    r = subprocess.run(["env", "-i", f"HOME={tmp_path}", "sh", str(hook_sh), "stop"],
                       capture_output=True, text=True, timeout=10)
    assert r.returncode == 0


def test_run_sh_mcp_without_interpreter_fails_loudly(tmp_path):
    r = _run(tmp_path, ["mcp"])
    assert r.returncode == 1
    assert r.stderr.strip() == "myak: no interpreter found; run install.sh"
    assert r.stdout == ""


def test_run_sh_mcp_uses_venv_python(tmp_path):
    marker = tmp_path / "argv"
    py = tmp_path / "fakepy"
    py.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\n')
    py.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    (state / "venv-python").write_text(f"{py}\n")
    assert _run(tmp_path, ["mcp"], {"MYAK_STATE_DIR": state}).returncode == 0
    assert marker.read_text().strip() == "-I -m myak.cli mcp"
