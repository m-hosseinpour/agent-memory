"""Command line interface.

Every integration (hooks, MCP, editor plugins) is a thin caller of these
commands, so the behaviour lives in one place and can be tested without any
agent present.

Output convention: mutating and query commands print JSON; `index` and
`recall` print text meant to be injected into a prompt.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config, index, store


def _read_content(args) -> str:
    if args.content_file:
        return Path(args.content_file).read_text(encoding="utf-8")
    if args.content is not None and args.content != "-":
        return args.content
    if sys.stdin.isatty():
        raise store.StoreError("no content: pass --content or pipe the body on stdin")
    return sys.stdin.read()


def _rel_prefix(directory: Path) -> str:
    """Path prefix that makes links resolvable inside a combined block."""
    try:
        return str(directory.relative_to(config.root())).rstrip("/") + "/"
    except ValueError:
        return ""


def _emit(payload: dict) -> int:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


def cmd_write(args) -> int:
    directory = config.scope_dir(args.scope, args.cwd)
    memory = store.write(
        directory,
        args.path,
        _read_content(args),
        name=args.name,
        description=args.description or "",
        memory_type=args.type,
        if_version=args.if_version,
        session_id=args.session,
        force=args.force,
    )
    index.rebuild(directory)
    return _emit(
        {
            "ok": True,
            "path": str(memory.path),
            "version": memory.version,
            "modified": memory.modified,
        }
    )


def cmd_read(args) -> int:
    directory = config.scope_dir(args.scope, args.cwd)
    path = directory / args.path
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
            "raw": path.read_text(encoding="utf-8"),
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
        index.rebuild(directory)
    return _emit({"ok": True, "removed": removed, "path": args.path})


def cmd_index(args) -> int:
    settings = config.load()
    max_lines = int(config.get("context", "index_max_lines", settings) or index.MAX_INDEX_LINES)
    scopes = ["global", "project"] if args.scope == "all" else [args.scope]
    blocks = []
    for scope in scopes:
        directory = config.scope_dir(scope, args.cwd)
        if args.rebuild:
            index.rebuild(directory)
        text = index.render(store.list_memories(directory), prefix=_rel_prefix(directory))
        if not text:
            continue
        blocks.append(f"# {scope} memory\n{text}")
    if not blocks:
        return 0
    sys.stdout.write(index.cap("\n\n".join(blocks), max_lines=max_lines) + "\n")
    return 0


def cmd_recall(args) -> int:
    settings = config.load()
    threshold = int(config.get("recall", "select_threshold", settings) or 40)
    top_k = args.top_k or int(config.get("recall", "top_k", settings) or 8)

    candidates: list[tuple[str, store.Memory]] = []
    for scope in ("project", "global"):
        directory = config.scope_dir(scope, args.cwd)
        for memory in store.list_memories(directory):
            candidates.append((scope, memory))

    # A small store is already fully present in the injected index; scoring it
    # again would only add noise. This mirrors how the reference agents behave
    # at small sizes.
    if len(candidates) < threshold:
        return 0

    hits = store.recall([m for _, m in candidates], args.query, top_k=top_k)
    lines = [
        f"- [{m.title}]({m.path}) — {m.description}"
        if m.description
        else f"- [{m.title}]({m.path})"
        for _, m in hits
    ]
    if lines:
        sys.stdout.write("\n".join(lines) + "\n")
    return 0


def cmd_hook(args) -> int:
    from . import hooks

    return hooks.run(args.event)


def cmd_mcp(args) -> int:
    from . import mcp

    return mcp.serve()


def cmd_dream(args) -> int:
    from . import dream

    return dream.run(force=args.force, cwd=args.cwd, all_scopes=args.all)


def cmd_extract(args) -> int:
    from . import extract

    return extract.run(
        transcript=args.transcript,
        agent=args.agent,
        session=args.session,
        cwd=args.cwd,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mem", description="Agent-agnostic memory store")
    parser.add_argument("--root", help="override the memory root (default ~/.agents/memory)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_scope(p):
        p.add_argument("--scope", choices=["global", "project"], default="project")
        p.add_argument("--cwd", default=".", help="project directory (default: current)")

    write = sub.add_parser("write", help="create or replace a memory")
    add_scope(write)
    write.add_argument("--path", required=True, help="file name, e.g. pnpm-not-npm.md")
    write.add_argument("--name", help="stable slug for the memory")
    write.add_argument("--description", default="", help="one line, used for retrieval")
    write.add_argument("--type", default=store.DEFAULT_TYPE, choices=store.TYPES)
    write.add_argument("--content", help="body text, or - for stdin")
    write.add_argument("--content-file", help="read the body from a file")
    write.add_argument("--if-version", dest="if_version", help="version from read, or 'new'")
    write.add_argument("--session", help="origin session id, for provenance")
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

    idx = sub.add_parser("index", help="print the MEMORY.md index block")
    idx.add_argument("--scope", choices=["global", "project", "all"], default="all")
    idx.add_argument("--cwd", default=".")
    idx.add_argument("--rebuild", action="store_true", help="regenerate MEMORY.md from files")
    idx.set_defaults(func=cmd_index)

    recall = sub.add_parser("recall", help="print memories relevant to a query")
    recall.add_argument("query")
    recall.add_argument("--cwd", default=".")
    recall.add_argument("--top-k", type=int, default=0)
    recall.set_defaults(func=cmd_recall)

    hook = sub.add_parser("hook", help="run as an agent hook (JSON on stdin)")
    hook.add_argument("event", choices=["session-start", "user-prompt", "stop", "session-end"])
    hook.set_defaults(func=cmd_hook)

    sub.add_parser("mcp", help="serve MCP over stdio").set_defaults(func=cmd_mcp)

    dream = sub.add_parser("dream", help="consolidate memories")
    dream.add_argument("--cwd", default=".")
    dream.add_argument("--all", action="store_true", help="every project, not just this one")
    dream.add_argument("--force", action="store_true", help="ignore the session counter")
    dream.set_defaults(func=cmd_dream)

    extract = sub.add_parser("extract", help="extract memories from a finished session")
    extract.add_argument("--transcript", required=True)
    extract.add_argument("--agent", default="unknown")
    extract.add_argument("--session", default="")
    extract.add_argument("--cwd", default=".")
    extract.set_defaults(func=cmd_extract)

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
                "raw": exc.current.path.read_text(encoding="utf-8"),
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
