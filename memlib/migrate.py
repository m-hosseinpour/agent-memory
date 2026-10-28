"""Import Claude Code's memory notes into this store.

Claude Code keeps auto memory per project under
`<claude-config>/projects/<slug>/memory/`: a generated `MEMORY.md` plus one
markdown note per memory, already in the frontmatter shape this store reads.
Migration is a copy of each note's body and managed fields, not a format
conversion, but a plain `cp` would still get five things wrong:

* the generated index would be copied too - this store rebuilds `MEMORY.md`
  from the files, so a copied one is stale by definition;
* the notes would stay inside Claude Code's tree instead of the store's
  `projects/<slug>` (or `global/`, when the caller promotes their type);
* the filename rules, the size cap, the empty-body check and the secret
  refusal would be skipped, because they live in `store.write`;
* `MEMORY.md` would not be rebuilt and duplicates would not be consolidated;
* the target scope would be guessed from Claude Code's directory name, which
  is lossy: it turns every non-alphanumeric into `-`, so `/home/u/.cache`
  becomes `-home-u--cache`, while this store keeps the dot. The real cwd is
  recorded in the project's session logs and is what this module uses; when it
  cannot be recovered the note is reported and skipped rather than written to
  a scope no `mem` command will ever read.

Per project is the default scope: a note may say `type: feedback` and still be
about one repository, and Claude Code kept it under one project. Promotion to
`global/` is opt-in with `--global`. An existing file is never overwritten: an
identical note is `exists`, a different one is a `collision`, and a note the
store already has under another filename (same type and name, or the
consolidation duplicate description) is `exists` too - without that, a rerun
would re-import what the pass deleted and delete the keeper instead.

Consolidation runs once per touched scope after every note is written, not
after each one: a migration is one batch, and the pass reads and rewrites the
whole scope each time.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import config, dream, store

# Claude Code writes `cwd` at session start; a transcript can be megabytes, so
# only the head of each file is read.
SESSION_HEAD_LINES = 100


@dataclass
class Project:
    """One Claude Code project directory and the cwd it belongs to."""

    memory: Path
    cwd: str | None

    @property
    def slug(self) -> str | None:
        return store.slug_for(self.cwd) if self.cwd else None


def default_source() -> Path:
    """Claude Code's projects root, honoring `CLAUDE_CONFIG_DIR`."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    base = Path(override).expanduser() if override else Path("~/.claude").expanduser()
    return base / "projects"


def _claude_slug(cwd: str) -> str:
    """Claude Code's directory name for a cwd: non-alphanumerics become `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        # A session removed between glob and stat is simply not the newest.
        return 0.0


def _recorded_cwd(project: Path) -> str | None:
    """The cwd Claude Code recorded for a project, from its newest sessions."""
    sessions = sorted(project.glob("*.jsonl"), key=_mtime, reverse=True)
    for session in sessions:
        try:
            with session.open(encoding="utf-8", errors="replace") as handle:
                for _ in range(SESSION_HEAD_LINES):
                    line = handle.readline()
                    if not line:
                        break
                    if '"cwd"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    cwd = record.get("cwd")
                    if isinstance(cwd, str) and os.path.isabs(cwd):
                        return cwd
        except OSError:
            continue
    return None


def _is_memory_dir(source: Path) -> bool:
    """True when `source` is one `memory/` directory, not a projects root."""
    if not source.is_dir():
        return False
    has_notes = any(source.glob("*.md"))
    has_projects = any(
        child.is_dir() and (child / "memory").is_dir() for child in source.iterdir()
    )
    return has_notes and not has_projects


def _projects(source: Path) -> list[Project]:
    """Every project with a memory directory under a projects root."""
    if not source.is_dir():
        raise store.StoreError(f"no Claude Code projects directory at {source}")
    projects: list[Project] = []
    for entry in sorted(source.iterdir()):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        memory = entry / "memory"
        if memory.is_dir():
            projects.append(Project(memory=memory, cwd=_recorded_cwd(entry)))
    if not projects:
        if any(source.glob("*.md")):
            raise store.StoreError(
                f"{source} is one memory directory, not a projects root; "
                "pass --cwd <project> to give its notes a scope"
            )
        return projects

    # The canonical directory (the one named after the cwd) wins the file-name
    # race over an `ssh-<id>` directory that records the same cwd.
    def order(project: Project) -> tuple[bool, bool, str]:
        canonical = project.cwd is not None and (
            project.memory.parent.name == _claude_slug(project.cwd)
        )
        return (project.cwd is None, not canonical, str(project.memory))

    return sorted(projects, key=order)


def _one_project(source: Path | None, cwd: str) -> list[Project]:
    """The memory directories of one project, found from its real cwd."""
    wanted = os.path.abspath(cwd)
    if source is not None and _is_memory_dir(source):
        return [Project(memory=source, cwd=wanted)]

    root = source if source is not None else default_source()
    projects = _projects(root)
    matches = [
        project
        for project in projects
        if project.cwd is not None
        and os.path.realpath(project.cwd) == os.path.realpath(wanted)
    ]
    if not matches:
        name = _claude_slug(wanted)
        matches = [p for p in projects if p.memory.parent.name == name]
    if not matches:
        raise store.StoreError(f"no Claude Code memory directory for {wanted} under {root}")
    # The caller supplied the real path, so even a name-only match gets a
    # usable scope instead of the "cwd unknown" refusal.
    return [Project(memory=p.memory, cwd=wanted) for p in matches]


def _note_files(memory: Path):
    for path in sorted(memory.iterdir()):
        if path.name == "MEMORY.md" or path.suffix != ".md" or not path.is_file():
            continue
        yield path


def _note_names(directory: Path) -> set[str]:
    return {
        path.name
        for path in directory.iterdir()
        if path.suffix == ".md" and path.name != "MEMORY.md"
    }


def _blocked(
    filename: str, name: str | None, description: str, body: str
) -> tuple[str, str] | None:
    """The refusals `store.write` would raise, so a dry run tells the truth."""
    try:
        store.validate(filename, body, name=name, description=description)
    except store.SecretError as exc:
        return "refused", str(exc)
    except store.StoreError as exc:
        return "invalid", str(exc)
    return None


def _same_note(
    target: Path, name: str | None, description: str, memory_type: str, body: str
) -> bool:
    current = store.load(target)
    return (
        current.name == (name or target.stem)
        and current.description == description
        and current.type == memory_type
        and current.body == body.strip()
    )


def _equivalent_note(
    directory: Path,
    filename: str,
    name: str | None,
    description: str,
    memory_type: str,
) -> store.Memory | None:
    """A note the consolidation pass would fold this one into.

    Without this, a rerun undoes the previous run: a.md, deleted by the first
    consolidation because z.md had the same name, is re-imported with a fresh
    timestamp and now deletes z.md instead. The same applies to a duplicate
    description. Treating either as already stored makes the import idempotent.
    """
    final_name = name or Path(filename).stem
    for memory in store.list_memories(directory):
        if memory.filename == filename or memory.type != memory_type:
            continue
        if memory.name == final_name or dream.similar_descriptions(
            memory.description, description
        ):
            return memory
    return None


def _migrate_note(
    path: Path, project: Project, promote: set[str], dry_run: bool
) -> dict:
    record = {"from": str(path), "to": "", "status": "", "detail": ""}
    slug = project.slug
    if slug is None:
        record.update(
            status="invalid",
            detail="cannot determine the project cwd from Claude Code's session logs; "
            "pass --cwd <project> to migrate its notes",
        )
        return record

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        record.update(status="invalid", detail=f"read failed: {exc}")
        return record
    meta, body = store.parse(text)
    metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    name = str(meta.get("name") or "") or None
    description = str(meta.get("description") or "")
    memory_type = str(metadata.get("type") or meta.get("type") or store.DEFAULT_TYPE)

    if memory_type not in store.TYPES:
        record.update(status="invalid", detail=f"unknown type {memory_type!r}")
        return record

    target_dir = config.global_dir() if memory_type in promote else config.project_dir(slug)
    target = target_dir / path.name
    record["to"] = str(target)

    if target.exists():
        if _same_note(target, name, description, memory_type, body):
            record.update(status="exists", detail="the same note is already in the store")
        else:
            record.update(
                status="collision",
                detail="a different note already has this file; not overwritten",
            )
        return record

    if target_dir.is_dir():
        equivalent = _equivalent_note(
            target_dir, path.name, name, description, memory_type
        )
        if equivalent is not None:
            record.update(
                status="exists",
                detail=f"an equivalent note is already stored as {equivalent.filename}; "
                "not written",
            )
            return record

    blocked = _blocked(path.name, name, description, body)
    if blocked is not None:
        record.update(status=blocked[0], detail=blocked[1])
        return record

    if dry_run:
        record["status"] = "would-write"
        return record

    try:
        store.write(
            target_dir,
            path.name,
            body,
            name=name,
            description=description,
            memory_type=memory_type,
            if_version="new",
        )
    except store.ConflictError:
        record.update(status="exists", detail="the same note is already in the store")
    except store.SecretError as exc:
        record.update(status="refused", detail=str(exc))
    except store.StoreError as exc:
        record.update(status="invalid", detail=str(exc))
    except OSError as exc:
        record.update(status="invalid", detail=f"write failed: {exc}")
    else:
        record["status"] = "written"
    return record


def run(
    source: Path | None = None,
    cwd: str | None = None,
    global_types: set[str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Import notes and return a JSON-serialisable report."""
    promote = set(global_types or ())
    unknown = promote - set(store.TYPES)
    if unknown:
        raise store.StoreError(
            f"unknown type(s) {', '.join(sorted(unknown))}; "
            f"expected a comma-separated subset of {', '.join(store.TYPES)}"
        )

    if cwd is not None:
        projects = _one_project(source, cwd)
        origin = projects[0].memory
    else:
        origin = source if source is not None else default_source()
        projects = _projects(origin)

    records: list[dict] = []
    touched: set[Path] = set()
    for project in projects:
        for path in _note_files(project.memory):
            record = _migrate_note(path, project, promote, dry_run)
            records.append(record)
            if record["status"] == "written":
                touched.add(Path(record["to"]).parent)

    replaced = merged = 0
    if not dry_run:
        for directory in sorted(touched):
            before = _note_names(directory)
            stats = dream.after_change(directory)
            replaced += stats.replaced
            merged += stats.merged
            gone = before - _note_names(directory)
            for record in records:
                target = Path(record["to"])
                if (
                    record["status"] in ("written", "exists")
                    and target.parent == directory
                    and target.name in gone
                ):
                    record.update(
                        status="removed",
                        detail="consolidation deleted this note (same name or duplicate)",
                    )

    counts = {
        "written": 0,
        "would-write": 0,
        "exists": 0,
        "collision": 0,
        "refused": 0,
        "invalid": 0,
        "removed": 0,
    }
    for record in records:
        counts[record["status"]] += 1

    return {
        "ok": counts["refused"] == 0
        and counts["invalid"] == 0
        and counts["collision"] == 0,
        "dry_run": dry_run,
        "source": str(origin),
        "projects": len(projects),
        "written": counts["written"],
        "would_write": counts["would-write"],
        "exists": counts["exists"],
        "collision": counts["collision"],
        "refused": counts["refused"],
        "invalid": counts["invalid"],
        "removed": counts["removed"],
        "replaced": replaced,
        "merged": merged,
        "notes": records,
    }
