"""myak CLI とフックランチャーのテスト。"""

import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

from myak import cli, config
from tests.idx_helpers import FIXTURES, count, rec, setup_env, text, write
from tests.test_db_migration import make_v0

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "templates" / "myak-hook.sh"

SUBCOMMANDS = [
    "hook", "index", "query", "backfill", "export", "mcp",
    "migrate", "reindex", "install-hooks",
]


@pytest.fixture
def env(db_env, tmp_path, monkeypatch):
    e = setup_env(db_env, tmp_path, monkeypatch)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    e.conn.close()
    return e


def test_help_lists_all_subcommands(capsys):
    with pytest.raises(SystemExit) as ei:
        cli.main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    for name in SUBCOMMANDS:
        assert name in out


def test_no_command_prints_help(capsys):
    assert cli.main([]) == 1
    assert "usage: myak" in capsys.readouterr().out


def test_pyproject_registers_scripts():
    text_ = (REPO / "pyproject.toml").read_text()
    assert 'myak = "myak.cli:main"' in text_
    for legacy in ("myak-index", "myak-query", "myak-backfill", "myak-export", "myak-mcp"):
        assert f"{legacy} =" in text_


def test_hook_subcommand_reads_stdin(env, monkeypatch, capsys):
    t = env.tmp / "projects" / "-Users-x-p" / "s.jsonl"
    write(t, [rec("u1", "user", text(1)), rec("a1", "assistant", text(2))])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(t)})))
    assert cli.main(["hook", "stop"]) == 0
    c = sqlite3.connect(str(env.db_path))
    assert count(c) == 2
    c.close()


def test_hook_subcommand_stdin_error_is_swallowed(env, monkeypatch):
    class Broken:
        def read(self):
            raise OSError("closed")

    monkeypatch.setattr("sys.stdin", Broken())
    assert cli.main(["hook", "prompt"]) == 0


def test_index_passthrough(env, capsys):
    t = env.tmp / "s.jsonl"
    write(t, [rec("u1", "user", text(1)), rec("a1", "assistant", text(2))])
    assert cli.main(["index", "--file", str(t), "--final"]) == 0
    assert "Indexed 2 segments" in capsys.readouterr().out
    assert cli.main(["index"]) == 1  # prints help, exit 1


def test_query_passthrough(env, capsys):
    assert cli.main(["query", "--limit", "2", "nothing matches zzz"]) == 0
    assert capsys.readouterr().out.strip()


def test_query_hook_passthrough(env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert cli.main(["query", "--hook"]) == 0
    assert capsys.readouterr().out == ""


def test_backfill_passthrough(env, monkeypatch, capsys):
    monkeypatch.setattr(config, "PROJECTS_DIR", env.tmp / "projects")
    d = env.tmp / "projects" / "-Users-x-alpha"
    d.mkdir(parents=True)
    shutil.copy(FIXTURES / "session_basic.jsonl", d / "sess-a.jsonl")
    assert cli.main(["backfill"]) == 0
    assert "Backfill complete" in capsys.readouterr().out


def test_export_passthrough_requires_vault(env, monkeypatch, capsys):
    monkeypatch.delenv("MYAK_OBSIDIAN_VAULT", raising=False)
    import myak.export_obsidian as ex

    monkeypatch.setattr(ex, "OBSIDIAN_VAULT", "")
    assert cli.main(["export"]) == 1
    assert "Vault path required" in capsys.readouterr().err


def test_mcp_passthrough(env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["mcp"]) == 0


def test_migrate_dry_run_and_real(env, capsys):
    env.db_path.unlink()
    make_v0(env.db_path).close()

    assert cli.main(["migrate", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Schema version: 0" in out
    assert "'segments': 5" in out
    c = sqlite3.connect(str(env.db_path))
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0
    c.close()
    assert not list(env.memory_dir.glob("*.bak-*"))

    assert cli.main(["migrate"]) == 0
    out = capsys.readouterr().out
    assert "Counts before: {'sessions': 3, 'segments': 5}" in out
    assert "Counts after: {'sessions': 3, 'segments': 5}" in out
    backups = list(env.memory_dir.glob("memory.db.bak-v0-*"))
    assert len(backups) == 1
    assert str(backups[0]) in out
    c = sqlite3.connect(str(env.db_path))
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    c.close()

    assert cli.main(["migrate"]) == 0
    assert "already-current" in capsys.readouterr().out


def test_migrate_dry_run_without_db(env, capsys):
    env.db_path.unlink()
    assert cli.main(["migrate", "--dry-run"]) == 0
    assert "No database" in capsys.readouterr().out
    assert not env.db_path.exists()


def test_migrate_creates_missing_db(env, capsys):
    env.db_path.unlink()
    assert cli.main(["migrate"]) == 0
    assert "created" in capsys.readouterr().out


def test_migrate_refuses_newer_schema(env, capsys):
    c = sqlite3.connect(str(env.db_path), isolation_level=None)
    c.execute("PRAGMA user_version=99")
    c.close()
    assert cli.main(["migrate"]) == 1
    assert "newer" in capsys.readouterr().err


def test_migrate_reports_migration_error(env, monkeypatch, capsys):
    from myak import db

    env.db_path.unlink()
    make_v0(env.db_path).close()

    def boom(conn):
        raise db.MigrationError("counts differ")

    monkeypatch.setattr(db, "migrate", boom)
    assert cli.main(["migrate"]) == 1
    assert "counts differ" in capsys.readouterr().err


def test_reindex_requires_full(env, capsys):
    assert cli.main(["reindex"]) == 2
    assert "--full" in capsys.readouterr().err


def test_reindex_full_resets_cursors(env, monkeypatch, capsys):
    monkeypatch.setattr(config, "PROJECTS_DIR", env.tmp / "projects")
    d = env.tmp / "projects" / "-Users-x-alpha"
    d.mkdir(parents=True)
    shutil.copy(FIXTURES / "session_basic.jsonl", d / "sess-a.jsonl")
    assert cli.main(["backfill"]) == 0
    path = d / "sess-a.jsonl"
    assert env.state.get_cursor(path) is not None
    c = sqlite3.connect(str(env.db_path))
    before = count(c)
    c.close()

    env.state.set_cursor(env.tmp / "other.jsonl", 5, 5, "x")
    assert cli.main(["reindex", "--full"]) == 0
    out = capsys.readouterr().out
    assert "Cursors reset" in out
    # cursors were cleared then re-created by the backfill; other.jsonl is gone
    assert env.state.get_cursor(env.tmp / "other.jsonl") is None
    assert env.state.get_cursor(path) is not None
    c = sqlite3.connect(str(env.db_path))
    assert count(c) == before  # uuid dedupe: no duplicates
    c.close()


def test_install_hooks(env, capsys):
    assert cli.main(["install-hooks"]) == 0
    out = capsys.readouterr().out
    dest = env.memory_dir / "bin" / "myak-hook"
    assert dest.is_file()
    assert dest.stat().st_mode & stat.S_IXUSR
    assert oct(dest.stat().st_mode & 0o777) == "0o755"
    assert dest.read_text() == LAUNCHER.read_text()
    assert (env.tmp / "state" / "venv-python").read_text().strip()
    snippet = json.loads(out[out.index("{"):])
    hooks = snippet["hooks"]
    assert set(hooks) == {"Stop", "SessionEnd", "UserPromptSubmit"}
    assert "__MYAK_HOOK__" not in out
    stop = hooks["Stop"][0]["hooks"][0]
    assert stop == {
        "type": "command", "command": f"{dest} stop", "async": True,
        "timeout": 60, "statusMessage": "myak: indexing",
    }
    assert hooks["SessionEnd"][0]["hooks"][0]["timeout"] == 10
    assert all("matcher" not in g for groups in hooks.values() for g in groups)


def test_install_hooks_missing_templates(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, "TEMPLATES_DIR", env.tmp / "nowhere")
    assert cli.main(["install-hooks"]) == 1


def test_hooks_template_shape():
    data = json.loads((REPO / "templates" / "hooks.json").read_text())
    assert set(data) == {"Stop", "SessionEnd", "UserPromptSubmit"}
    for groups in data.values():
        for g in groups:
            assert "matcher" not in g
            assert "__MYAK_HOOK__" in g["hooks"][0]["command"]


# ---- launcher (real subprocess, no PATH) -------------------------------------------------

def _run_launcher(script, home, event, stdin, extra_env=None):
    env = {"HOME": str(home)}
    env.update(extra_env or {})
    return subprocess.run(
        ["env", "-i", *[f"{k}={v}" for k, v in env.items()], "sh", str(script), event],
        input=stdin, capture_output=True, text=True, timeout=10,
    )


def _fake_bin(path, marker):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\necho "$@" > "{marker}"\ncat > "{marker}.stdin"\n')
    path.chmod(0o755)


@pytest.fixture
def isolated_launcher(tmp_path):
    """/opt/homebrew の実 uv を踏まないよう、そのパスだけ無効化したコピー。"""
    script = tmp_path / "launcher.sh"
    script.write_text(
        LAUNCHER.read_text().replace("/opt/homebrew/bin/uv", str(tmp_path / "no-such-uv"))
    )
    return script


def test_launcher_uses_local_bin_myak(tmp_path):
    home = tmp_path / "home"
    marker = tmp_path / "called"
    _fake_bin(home / ".local" / "bin" / "myak", marker)
    r = _run_launcher(LAUNCHER, home, "prompt", '{"prompt":"hello there"}')
    assert r.returncode == 0
    assert marker.read_text().strip() == "hook prompt"
    assert (tmp_path / "called.stdin").read_text() == '{"prompt":"hello there"}'


def test_launcher_myak_bin_override_wins(tmp_path):
    home = tmp_path / "home"
    _fake_bin(home / ".local" / "bin" / "myak", tmp_path / "local")
    override = tmp_path / "override"
    _fake_bin(override, tmp_path / "ov")
    r = _run_launcher(LAUNCHER, home, "stop", "{}", {"MYAK_BIN": override})
    assert r.returncode == 0
    assert (tmp_path / "ov").read_text().strip() == "hook stop"
    assert not (tmp_path / "local").exists()


def test_launcher_uv_fallback(tmp_path):
    home = tmp_path / "home"
    marker = tmp_path / "uvcalled"
    _fake_bin(home / ".local" / "bin" / "uv", marker)
    r = _run_launcher(LAUNCHER, home, "stop", "{}")
    assert r.returncode == 0
    assert marker.read_text().strip() == "tool run myak hook stop"


def test_launcher_recorded_venv_python(tmp_path, isolated_launcher):
    home = tmp_path / "home"
    fake_py = tmp_path / "fakepy"
    _fake_bin(fake_py, tmp_path / "pycalled")
    state = home / ".claude" / "myak-state"
    state.mkdir(parents=True)
    (state / "venv-python").write_text(f"{fake_py}\n")
    r = _run_launcher(isolated_launcher, home, "session-end", "{}")
    assert r.returncode == 0
    assert (tmp_path / "pycalled").read_text().strip() == "-m myak.cli hook session-end"


def test_launcher_path_fallback(tmp_path, isolated_launcher):
    home = tmp_path / "home"
    home.mkdir()
    bindir = tmp_path / "pathbin"
    _fake_bin(bindir / "myak", tmp_path / "pathcalled")
    r = _run_launcher(isolated_launcher, home, "prompt", "{}", {"PATH": f"{bindir}:/usr/bin:/bin"})
    assert r.returncode == 0
    assert (tmp_path / "pathcalled").read_text().strip() == "hook prompt"


def test_launcher_nothing_installed_exits_silently(tmp_path, isolated_launcher):
    home = tmp_path / "home"
    home.mkdir()
    r = _run_launcher(isolated_launcher, home, "prompt", "{}")
    assert r.returncode == 0
    assert r.stdout == "" and r.stderr == ""


def test_launcher_no_event_exits_zero(tmp_path):
    r = subprocess.run(
        ["env", "-i", f"HOME={tmp_path}", "sh", str(LAUNCHER)],
        capture_output=True, text=True, timeout=10,
    )
    assert r.returncode == 0
    assert r.stdout == ""


def test_launcher_is_executable_template():
    assert os.access(LAUNCHER, os.X_OK)
