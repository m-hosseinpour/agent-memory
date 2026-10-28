"""Command line interface.

Everything an agent does goes through the MCP server, which is this module's
`store`/`index`/`dream` logic in-process. This CLI exists for two callers:

* a human (or a hookless agent) running the same operations by hand;
* `PROTOCOL.md`, the written contract for an agent that has no MCP client.

Output convention: mutating and query commands print JSON; `index` prints text
meant to be read, not parsed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, dream, index, migrate, store


def _read_content(args) -> str:
    if args.content_file:
        return Path(args.content_file).read_text(encoding="utf-8")
    if args.content is not None and args.content != "-":
        return args.content
    if sys.stdin.isatty():
        raise store.StoreError("no content: pass --content or pipe the body on stdin")
    return sys.stdin.read()


def _emit(payload: dict, code: int = 0) -> int:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return code


def cmd_write(args) -> int:
    directory = config.scope_dir(args.scope, args.cwd)
    memory = store.write(
        directory,
        args.path,
        _read_content(args),
        name=args.name,
        description=args.description,
        memory_type=args.type,
        if_version=args.if_version,
        force=args.force,
    )
    # The change is consolidated before the command returns: no queued state
    # to be picked up by a later pass. `keep` stops the pass from deleting the
    # note that was just written when timestamps tie.
    dream.after_change(directory, keep=memory.filename)
    # The pass may have merged another note's body into this one, so report the
    # version that is actually on disk, not the one from before the pass.
    current = store.load(memory.path)
    return _emit(
        {
            "ok": True,
            "path": str(current.path),
            "version": current.version,
            "modified": current.modified,
        }
    )


def cmd_read(args) -> int:
    directory = config.scope_dir(args.scope, args.cwd)
    filename = store.check_filename(args.path)
    path = directory / filename
    if not path.is_file():
        raise store.StoreError(f"no such memory: {args.path}")
    memory = store.load(path)
    return _emit(
        {
            "path": str(memory.path),
            "name": memory.name,
            "description": memory.description,
            "type": memory.type,
            "modified": memory.modified,
            "version": memory.version,
            "content": memory.body,
            "raw": path.read_text(encoding="utf-8", errors="replace"),
        }
    )


def cmd_list(args) -> int:
    directory = config.scope_dir(args.scope, args.cwd)
    memories = store.list_memories(directory)
    return _emit(
        {
            "directory": str(directory),
            "count": len(memories),
            "memories": [
                {
                    "file": m.filename,
                    "name": m.name,
                    "description": m.description,
                    "type": m.type,
                    "modified": m.modified,
                    "version": m.version,
                }
                for m in memories
            ],
        }
    )


def cmd_forget(args) -> int:
    directory = config.scope_dir(args.scope, args.cwd)
    removed = store.delete(directory, args.path)
    if removed:
        dream.after_change(directory)
    return _emit({"ok": True, "removed": removed, "path": args.path})


def cmd_index(args) -> int:
    scopes = ["global", "project"] if args.scope == "all" else [args.scope]
    blocks = []
    for scope in scopes:
        directory = config.scope_dir(scope, args.cwd)
        text = index.render(store.list_memories(directory))
        if text:
            blocks.append(f"# {scope} memory\n{text}")
    if blocks:
        sys.stdout.write("\n\n".join(blocks) + "\n")
    return 0


def cmd_mcp(args) -> int:
    from . import mcp

    return mcp.serve()


def cmd_dream(args) -> int:
    return dream.run(cwd=args.cwd, all_scopes=args.all)


def cmd_migrate(args) -> int:
    types = {part.strip() for part in args.global_types.split(",") if part.strip()}
    report = migrate.run(
        source=Path(args.source).expanduser() if args.source else None,
        cwd=args.cwd,
        global_types=types,
        dry_run=args.dry_run,
    )
    # A bulk import can succeed file by file; the exit code still says whether
    # something was left behind, with 3 (a secret) winning over 1 (a note that
    # could not be imported or disagreed with what is already stored).
    if report["refused"]:
        return _emit(report, 3)
    if report["invalid"] or report["collision"]:
        return _emit(report, 1)
    return _emit(report)


class _Parser(argparse.ArgumentParser):
    """Usage errors are invalid input, like every other bad argument.

    argparse exits 2 on a usage error, but the JSON contract reserves 2 for a
    version conflict. A caller that branches on the exit code would report a
    bogus conflict (or read an empty stdout) for a typo, so usage errors exit 1
    with the usual error object.
    """

    def error(self, message: str) -> None:
        json.dump({"ok": False, "error": "invalid", "message": message}, sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()
        raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="mem", description="Agent-agnostic memory store")
    parser.add_argument("--root", help="override the memory root (default ~/.agents/memory)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_scope(p):
        p.add_argument("--scope", choices=["global", "project"], default="project")
        p.add_argument("--cwd", default=".", help="project directory (default: current)")

    write = sub.add_parser("write", help="create or replace a memory")
    add_scope(write)
    write.add_argument("--path", required=True, help="file name, e.g. pnpm-not-npm.md")
    write.add_argument("--name", help="stable slug for the memory (default: keep the current one)")
    write.add_argument(
        "--description",
        default=None,
        help="one line, used in the index (default: keep the current one; \"\" clears it)",
    )
    write.add_argument(
        "--type",
        default=None,
        choices=store.TYPES,
        help="memory type (default: keep the current one; project for a new note)",
    )
    write.add_argument("--content", help="body text, or - for stdin")
    write.add_argument("--content-file", help="read the body from a file")
    write.add_argument("--if-version", dest="if_version", help="version from read, or 'new'")
    write.add_argument("--force", action="store_true", help="skip the version check")
    write.set_defaults(func=cmd_write)

    read = sub.add_parser("read", help="read one memory")
    add_scope(read)
    read.add_argument("--path", required=True)
    read.set_defaults(func=cmd_read)

    listing = sub.add_parser("list", help="list memories in a scope")
    add_scope(listing)
    listing.set_defaults(func=cmd_list)

    forget = sub.add_parser("forget", help="delete a memory")
    add_scope(forget)
    forget.add_argument("--path", required=True)
    forget.set_defaults(func=cmd_forget)

    idx = sub.add_parser("index", help="print the MEMORY.md table of contents")
    idx.add_argument("--scope", choices=["global", "project", "all"], default="all")
    idx.add_argument("--cwd", default=".")
    idx.set_defaults(func=cmd_index)

    sub.add_parser("mcp", help="serve MCP over stdio").set_defaults(func=cmd_mcp)

    consolidate = sub.add_parser("dream", help="consolidate memories now")
    consolidate.add_argument("--cwd", default=".")
    consolidate.add_argument("--all", action="store_true", help="every project, not just this one")
    consolidate.set_defaults(func=cmd_dream)

    migration = sub.add_parser("migrate", help="import Claude Code's memories")
    migration.add_argument(
        "--from",
        dest="source",
        help="Claude Code projects root, or with --cwd one memory directory "
        "(default: $CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects)",
    )
    migration.add_argument(
        "--cwd",
        help="only this project, mapped to its own project scope",
    )
    migration.add_argument(
        "--global",
        dest="global_types",
        default="",
        metavar="TYPES",
        help="comma-separated types to put in the global scope, e.g. user,feedback "
        "(default: every note stays in its project)",
    )
    migration.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be copied and write nothing",
    )
    migration.set_defaults(func=cmd_migrate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "root", None):
        import os

        os.environ["MEM_ROOT"] = args.root
    try:
        return args.func(args)
    except store.ConflictError as exc:
        payload = {"ok": False, "error": "conflict", "message": str(exc)}
        if exc.current is not None:
            payload["current"] = {
                "version": exc.current.version,
                "content": exc.current.body,
                "raw": exc.current.path.read_text(encoding="utf-8", errors="replace"),
            }
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 2
    except store.SecretError as exc:
        _emit({"ok": False, "error": "secret", "message": str(exc)})
        return 3
    except store.StoreError as exc:
        _emit({"ok": False, "error": "invalid", "message": str(exc)})
        return 1
    except UnicodeDecodeError as exc:
        # A content file or stdin that is not UTF-8: invalid input, not a crash.
        _emit({"ok": False, "error": "invalid", "message": f"input is not valid UTF-8: {exc}"})
        return 1
    except OSError as exc:
        # A missing --content-file, an unreadable directory, a path that turned
        # out to be a directory: still the JSON contract, never a traceback.
        _emit({"ok": False, "error": "io", "message": str(exc)})
        return 1
