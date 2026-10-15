"""単一 CLI エントリポイント `myak`。"""

import argparse
import json
import sys
from importlib.resources import files
from pathlib import Path

from myak import config

TEMPLATES_DIR = Path(str(files("myak") / "templates"))

# 既存モジュールの main(argv) にそのまま委譲するサブコマンド
PASSTHROUGH = {
    "index": ("myak.indexer", "run_from_cli", "Index a session transcript"),
    "query": ("myak.query", "main", "Search memory (supports --hook/--codex/--limit)"),
    "backfill": ("myak.backfill", "main", "Backfill all session transcripts"),
    "export": ("myak.export_obsidian", "main", "Export memory to an Obsidian vault"),
    "mcp": ("myak.mcp_server", "main", "Run the MCP server over stdio"),
}


def build_parser():
    parser = argparse.ArgumentParser(prog="myak", description="Persistent memory for Claude Code")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    hook = sub.add_parser("hook", help="Run a Claude Code hook handler (reads JSON on stdin)")
    hook.add_argument("event", choices=["stop", "session-end", "session-start", "prompt"])

    for name, (_mod, _fn, helptext) in PASSTHROUGH.items():
        p = sub.add_parser(name, help=helptext, add_help=False)
        p.add_argument("args", nargs=argparse.REMAINDER)

    mig = sub.add_parser("migrate", help="Migrate the database schema (explicit only)")
    mig.add_argument("--dry-run", action="store_true", help="Print version and counts only")

    cl = sub.add_parser(
        "clean-legacy", help="Apply noise filters retroactively to legacy (uuid-less) rows"
    )
    cl.add_argument("--dry-run", action="store_true", help="Print counts and examples only")

    rs = sub.add_parser(
        "redact-secrets", help="Redact API keys/tokens already stored in the database"
    )
    rs.add_argument("--dry-run", action="store_true", help="Print counts and segment ids only")

    re = sub.add_parser("reindex", help="Reset cursors and re-run backfill")
    re.add_argument("--full", action="store_true", help="Required: confirm a full reindex")

    em = sub.add_parser("embed", help="Embed pending segments for hybrid search (needs myak[vec])")
    em.add_argument("--limit", type=int, default=None, help="Max segments to embed")
    em.add_argument("--model", default=None, help="Embedding model id (fastembed)")
    em.add_argument(
        "--rebuild", action="store_true",
        help="Discard the existing vector index (required to switch --model)",
    )

    sub.add_parser("install-hooks", help="Install the hook launcher and print settings snippet")

    ms = sub.add_parser(
        "migrate-settings", help="Rewrite or remove myak hook entries in settings.json"
    )
    ms.add_argument("--mode", choices=["rewrite", "remove"], required=True)
    ms.add_argument("--dry-run", action="store_true", help="Print a diff, change nothing")
    ms.add_argument("--path", help="settings.json path (default: ~/.claude/settings.json)")
    ms.add_argument(
        "--launcher", help="Launcher path for rewrite (default: STATE_DIR/bin/myak-hook)"
    )

    return parser


def _delegate(name, argv):
    import importlib

    mod_name, fn_name, _ = PASSTHROUGH[name]
    fn = getattr(importlib.import_module(mod_name), fn_name)
    try:
        if name == "mcp":
            result = fn()
        else:
            result = fn(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return result if isinstance(result, int) else 0


def _counts(conn):
    try:
        return {
            "sessions": conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
            "segments": conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0],
        }
    except Exception:  # noqa: BLE001 - table may not exist yet
        return None


def cmd_migrate(dry_run):
    from myak import db

    if not config.DB_PATH.exists():
        if dry_run:
            print(
                f"No database at {config.DB_PATH}; "
                f"migrate would create a v{db.CURRENT_VERSION} one."
            )
            return 0
    config.ensure_memory_dir()
    conn = config.get_connection()
    try:
        version = db.schema_version(conn)
        before = _counts(conn)
        print(f"Database: {config.DB_PATH}")
        print(f"Schema version: {version} (code expects {db.CURRENT_VERSION})")
        print(f"Counts before: {before}")
        if dry_run:
            return 0
        try:
            result = db.migrate(conn)
        except db.SchemaTooNew as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        except db.MigrationError as e:
            print(f"Migration failed: {e}", file=sys.stderr)
            return 1
        print(f"Status: {result['status']}")
        print(f"Counts after: {_counts(conn)}")
        print(f"Backup: {result.get('backup') or '(none)'}")
        return 0
    finally:
        conn.close()


def cmd_reindex(full):
    from myak import backfill, state

    if not full:
        print("Refusing without --full (resets all indexing cursors).", file=sys.stderr)
        return 2
    state.reset_all()
    print("Cursors reset.")
    return backfill.backfill()


def hook_launcher_path():
    return config.STATE_DIR / "bin" / "myak-hook"


def cmd_install_hooks():
    launcher_src = TEMPLATES_DIR / "myak-hook.sh"
    hooks_src = TEMPLATES_DIR / "hooks.json"
    if not launcher_src.is_file() or not hooks_src.is_file():
        print(f"Templates not found under {TEMPLATES_DIR}", file=sys.stderr)
        return 1
    config.ensure_state_dir()
    dest = hook_launcher_path()
    config.atomic_write(dest, launcher_src.read_text(encoding="utf-8"), 0o755)

    venv_file = config.STATE_DIR / "venv-python"
    config.atomic_write(venv_file, sys.executable + "\n", 0o600)

    from myak import settings_migrate

    hooks = settings_migrate.render_template(dest)
    print(f"Launcher: {dest}")
    print(f"Recorded interpreter: {sys.executable} -> {venv_file}")
    print("Add this to ~/.claude/settings.json (or run `myak migrate-settings`):")
    print(json.dumps({"hooks": hooks}, indent=2, ensure_ascii=False))
    return 0


def cmd_migrate_settings(args):
    from myak import settings_migrate

    launcher = args.launcher or str(hook_launcher_path())
    try:
        result = settings_migrate.migrate_settings(
            args.path, mode=args.mode, launcher=launcher, dry_run=args.dry_run
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print(f"{result['path']}: {result['status']}")
    if result["diff"]:
        print(result["diff"], end="")
    if result["backup"]:
        print(f"Backup: {result['backup']}")
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in PASSTHROUGH:
        return _delegate(argv[0], argv[1:])

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 1
    if args.command == "hook":
        from myak.hooks import dispatch

        stdin_text = ""
        try:
            stdin_text = sys.stdin.read()
        except Exception:  # noqa: BLE001
            pass
        return dispatch(args.event, stdin_text)
    if args.command == "migrate":
        return cmd_migrate(args.dry_run)
    if args.command == "clean-legacy":
        from myak import cleanup

        return cleanup.run(args.dry_run)
    if args.command == "redact-secrets":
        from myak import redact_db

        return redact_db.run(args.dry_run)
    if args.command == "reindex":
        return cmd_reindex(args.full)
    if args.command == "embed":
        from myak import vec

        return vec.run_embed(limit=args.limit, model=args.model, rebuild=args.rebuild)
    if args.command == "install-hooks":
        return cmd_install_hooks()
    if args.command == "migrate-settings":
        return cmd_migrate_settings(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
