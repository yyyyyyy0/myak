"""Shell-level tests for install.sh / uninstall.sh using stubbed uv/claude/myak."""
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
INSTALL = REPO / "install.sh"
UNINSTALL = REPO / "uninstall.sh"

STUB = '#!/bin/sh\necho "{name} $*" >> "{log}"\n{extra}exit 0\n'


def _stub(path, name, log, extra=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STUB.format(name=name, log=log, extra=extra))
    path.chmod(0o755)


@pytest.fixture
def sandbox(tmp_path):
    home = tmp_path / "home"
    stubs = tmp_path / "stubs"
    log = tmp_path / "calls.log"
    log.write_text("")
    _stub(stubs / "uv", "uv", log)
    _stub(stubs / "claude", "claude", log)
    _stub(home / ".local" / "bin" / "myak", "myak", log)
    return home, stubs, log


def _run(script, home, stubs, extra_env=None):
    env = {
        "HOME": str(home),
        "PATH": f"{stubs}:/usr/bin:/bin",
        **(extra_env or {}),
    }
    return subprocess.run(
        ["env", "-i", *[f"{k}={v}" for k, v in env.items()], "bash", str(script)],
        capture_output=True, text=True, timeout=30,
    )


def _calls(log):
    return [line for line in log.read_text().splitlines() if line]


@pytest.mark.parametrize("script", [INSTALL, UNINSTALL], ids=lambda p: p.name)
def test_syntax(script):
    subprocess.run(["bash", "-n", str(script)], check=True)
    assert os.access(script, os.X_OK)


def test_install_call_sequence(sandbox):
    home, stubs, log = sandbox
    r = _run(INSTALL, home, stubs)
    assert r.returncode == 0, r.stderr
    calls = _calls(log)
    assert calls == [
        f"uv tool install {REPO} --python 3.12 --force",
        "myak install-hooks",
        f"claude plugin marketplace add {REPO}",
        "claude plugin install myak@myak",
        "myak backfill",
    ]
    assert "migrate-settings --mode remove" in r.stdout  # printed as a hint
    assert not any("migrate-settings" in c for c in calls)


def test_install_editable_opt_in(sandbox):
    home, stubs, log = sandbox
    r = _run(INSTALL, home, stubs, {"MYAK_EDITABLE": "1"})
    assert r.returncode == 0, r.stderr
    assert f"uv tool install --editable {REPO} --python 3.12 --force" in _calls(log)
    assert "must stay in place" in r.stdout


def test_install_warns_about_duplicate_injection(sandbox):
    home, stubs, log = sandbox
    r = _run(INSTALL, home, stubs)
    assert "TWICE" in r.stdout
    assert "myak migrate-settings --mode remove" in r.stdout


def test_install_tolerates_claude_and_backfill_failures(sandbox):
    home, stubs, log = sandbox
    _stub(stubs / "claude", "claude", log, extra="exit 1\n")
    _stub(home / ".local" / "bin" / "myak", "myak", log,
          extra='[ "$1" = "backfill" ] && exit 1\n')
    r = _run(INSTALL, home, stubs)
    assert r.returncode == 0, r.stderr
    assert "marketplace already added" in r.stdout
    assert "plugin already installed" in r.stdout
    assert "WARN: backfill failed" in r.stdout


def test_alias_step_is_opt_in(sandbox):
    home, stubs, log = sandbox
    zshrc = home / ".zshrc"
    zshrc.write_text("# rc\n")
    r = _run(INSTALL, home, stubs)
    assert r.returncode == 0, r.stderr
    assert "claude-reload" not in zshrc.read_text()
    r = _run(INSTALL, home, stubs, {"MYAK_SHELL_ALIAS": "1"})
    assert r.returncode == 0, r.stderr
    assert "claude-reload" in zshrc.read_text()


def test_install_removes_settings_hooks_when_opted_in(sandbox):
    home, stubs, log = sandbox
    r = _run(INSTALL, home, stubs, {"MYAK_REMOVE_SETTINGS_HOOKS": "1"})
    assert r.returncode == 0, r.stderr
    calls = _calls(log)
    assert "myak migrate-settings --mode remove" in calls
    assert calls.index("myak migrate-settings --mode remove") < calls.index("myak backfill")


def test_install_without_claude_prints_commands(sandbox):
    home, stubs, log = sandbox
    (stubs / "claude").unlink()
    r = _run(INSTALL, home, stubs)
    assert r.returncode == 0, r.stderr
    assert not any(c.startswith("claude") for c in _calls(log))
    assert "claude plugin install myak@myak" in r.stdout


def test_install_requires_uv(sandbox):
    home, stubs, log = sandbox
    (stubs / "uv").unlink()
    r = _run(INSTALL, home, stubs)
    assert r.returncode == 1
    assert "uv" in r.stderr
    assert _calls(log) == []


def test_uninstall_sequence(sandbox):
    home, stubs, log = sandbox
    r = _run(UNINSTALL, home, stubs)
    assert r.returncode == 0, r.stderr
    assert _calls(log) == [
        "myak migrate-settings --mode remove",
        "claude plugin uninstall myak@myak",
        "uv tool uninstall myak",
    ]
    assert ".claude/memory" in r.stdout
