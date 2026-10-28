"""Consolidation pass over the store ("dream").

`mem dream` is the equivalent of Claude Code's autoDream: a periodic pass that
merges duplicates, drops a note that a newer version of the same note replaced,
and repairs the index. It is deterministic — no model call — because at this
size the cheap rules catch nearly all drift, and a pass that costs tokens would
never be allowed to run unattended.

Trigger: the session counter in `.state/state.json`, which the SessionStart hook
increments. The pass runs when the counter reaches `dream.every_sessions`, or
whenever `--force` is given. Without `--all` it touches the global scope and the
current project's scope; `--all` walks every project directory.

Concurrency: one lockfile holding a pid, `.state/dream.lock`. A second pass that
finds a live owner exits without doing anything; a lock left behind by a dead
process is taken over.

History: when the store is a git repository (and its root is the repository
top-level, not a directory inside another repo), the pass commits the tree
before it touches anything and again afterwards, so each consolidation is one
reviewable diff. `.state/` and `dream.log` are runtime bookkeeping, not
memories; they are ignored by the repository, which is also what lets a second
run leave no diff at all.

"Contradiction" here means identity, not semantics: two files of the same type
and the same `name` are the same note, and the newer `modified` wins. A note
whose *stance* changed under a different name is not detected — guessing that
from text without a model is exactly the kind of heuristic that quietly deletes
the wrong half of a pair.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config, hooks, index, store

DEFAULT_EVERY = 5
LOCK_NAME = "dream.lock"
LOG_NAME = "dream.log"
GIT_TIMEOUT = 15

# Patterns for the store's own repository. Temp files are written next to the
# memory they replace (`.foo.md.tmp`) and only survive a crash.
GITIGNORE_LINES = (".state/", LOG_NAME, ".*.tmp")

# Two descriptions this close describe the same thing. Jaccard over words of
# three or more characters, so "pnpm, not npm" and "use pnpm not npm" match.
DUPLICATE_THRESHOLD = 0.8


@dataclass
class Stats:
    scopes: int = 0
    merged: int = 0
    replaced: int = 0
    index_lines: int = 0
    commits: int = 0
    sessions: int = 0

    def as_dict(self) -> dict:
        return {
            "scopes": self.scopes,
            "merged": self.merged,
            "replaced": self.replaced,
            "index_lines": self.index_lines,
            "commits": self.commits,
            "sessions": self.sessions,
        }


def run(force: bool = False, cwd: str = ".", all_scopes: bool = False) -> int:
    settings = config.load()
    every = _as_int(config.get("dream", "every_sessions", settings), DEFAULT_EVERY)
    sessions = hooks.session_count()
    if not force and sessions < every:
        _emit(
            {
                "ok": True,
                "ran": False,
                "reason": "below threshold",
                "sessions": sessions,
                "every": every,
            }
        )
        return 0

    root = config.root()
    try:
        lock = _acquire(root)
    except OSError as exc:
        _emit({"ok": False, "error": "io", "message": str(exc)})
        return 1
    if lock is None:
        _emit({"ok": True, "ran": False, "reason": "locked"})
        return 0

    stats = Stats()
    errors: list[str] = []
    try:
        directories = _scopes(cwd, all_scopes)
        stats.scopes = len(directories)
        # Ignore rules first: the lockfile is already on disk, and the snapshot
        # below must not pick up runtime bookkeeping.
        _ensure_gitignore(root)
        # Snapshot first, then let the pass write: the diff between the two
        # commits is exactly what consolidation changed.
        if _git_commit(root, "dream: snapshot before consolidation"):
            stats.commits += 1
        for directory in directories:
            try:
                _consolidate(directory, stats)
            except OSError as exc:
                errors.append("{}: {}".format(directory.name, exc))
        if _git_commit(root, _commit_message(stats)):
            stats.commits += 1
        if sessions:
            hooks.reset_sessions()
            stats.sessions = sessions
        _append_log(root, stats)
    finally:
        _release(lock)

    payload = {"ok": True, "ran": True, **stats.as_dict()}
    if errors:
        payload.update({"ok": False, "error": "partial", "message": "; ".join(errors)})
        _emit(payload)
        return 1
    _emit(payload)
    return 0


# --------------------------------------------------------------------------
# one scope


def _scopes(cwd: str, all_scopes: bool) -> list[Path]:
    directories = [config.scope_dir("global", cwd), config.scope_dir("project", cwd)]
    if all_scopes:
        projects = config.root() / "projects"
        try:
            directories.extend(entry for entry in sorted(projects.iterdir()) if entry.is_dir())
        except OSError:
            pass
    unique: list[Path] = []
    seen: set[str] = set()
    for directory in directories:
        key = str(directory)
        if key not in seen:
            seen.add(key)
            unique.append(directory)
    return unique


def _consolidate(directory: Path, stats: Stats) -> None:
    if not directory.is_dir():
        return
    memories = store.list_memories(directory)
    replaced = _replace_same_name(directory, memories, stats)
    if replaced:
        memories = [m for m in memories if m.filename not in replaced]
    _merge_duplicates(directory, memories, stats)
    stats.index_lines += _repair_index(directory)


def _replace_same_name(directory: Path, memories: list[store.Memory], stats: Stats) -> set[str]:
    """Same type + same name = the same note; the newest `modified` wins."""
    groups: dict[tuple[str, str], list[store.Memory]] = {}
    for memory in memories:
        groups.setdefault((memory.type, memory.name), []).append(memory)
    removed: set[str] = set()
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda m: (m.modified, m.filename))
        for older in group[:-1]:
            if store.delete(directory, older.filename):
                removed.add(older.filename)
                stats.replaced += 1
    return removed


def _merge_duplicates(directory: Path, memories: list[store.Memory], stats: Stats) -> None:
    """Fold notes with a near-identical description into the newest one.

    Bodies are merged line-wise: only lines the keeper does not already have are
    appended, so a near-duplicate's extra detail survives. The keeper is written
    before any duplicate is deleted; if that write fails, both files stay.
    """
    ordered = sorted(memories, key=lambda m: (m.modified, m.filename), reverse=True)
    clusters: list[tuple[store.Memory, list[store.Memory]]] = []
    bodies: dict[str, str] = {}
    for memory in ordered:
        keeper = next(
            (
                entry
                for entry in clusters
                if entry[0].type == memory.type
                and _similar(entry[0].description, memory.description)
            ),
            None,
        )
        if keeper is None:
            clusters.append((memory, []))
            continue
        keeper[1].append(memory)
        current = bodies.get(keeper[0].filename, keeper[0].body)
        merged = _merge_body(current, memory.body)
        if merged != current:
            bodies[keeper[0].filename] = merged

    for keeper, duplicates in clusters:
        if not duplicates:
            continue
        body = bodies.get(keeper.filename)
        if body is not None and body != keeper.body:
            try:
                store.write(
                    directory,
                    keeper.filename,
                    body,
                    name=keeper.name,
                    description=keeper.description,
                    memory_type=keeper.type,
                    force=True,
                )
            except store.StoreError:
                continue
        for duplicate in duplicates:
            if store.delete(directory, duplicate.filename):
                stats.merged += 1


def _repair_index(directory: Path) -> int:
    """Rebuild MEMORY.md and count the lines that pointed at gone files."""
    path = directory / "MEMORY.md"
    try:
        before = path.read_text(encoding="utf-8")
    except OSError:
        before = ""
    index.rebuild(directory)
    after = path.read_text(encoding="utf-8")
    if before == after:
        return 0
    # Only entry lines count; a changed "(N)" heading follows from them.
    old = {line.strip() for line in before.splitlines() if line.strip().startswith("- [")}
    new = {line.strip() for line in after.splitlines() if line.strip().startswith("- [")}
    return len(old - new)


def _similar(first: str, second: str) -> bool:
    left, right = store.tokens(first), store.tokens(second)
    if not left or not right:
        return False
    return len(left & right) / len(left | right) >= DUPLICATE_THRESHOLD


def _merge_body(keeper: str, other: str) -> str:
    lines = keeper.splitlines()
    known = {line.strip() for line in lines if line.strip()}
    extra = [line for line in other.splitlines() if line.strip() and line.strip() not in known]
    if not extra:
        return keeper
    return keeper.rstrip() + "\n\n" + "\n".join(extra)


# --------------------------------------------------------------------------
# lock


def _acquire(root: Path) -> Path | None:
    path = root / ".state" / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if _alive(_read_pid(path)):
                return None
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        try:
            os.write(handle, str(os.getpid()).encode("ascii"))
        finally:
            os.close(handle)
        return path
    return None


def _release(path: Path) -> None:
    try:
        if _read_pid(path) == os.getpid():
            path.unlink()
    except OSError:
        pass


def _read_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------
# git


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT,
    )


def _is_repo(root: Path) -> bool:
    if not (root / ".git").exists():
        return False
    try:
        finished = _git(root, ["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.SubprocessError):
        return False
    if finished.returncode != 0:
        return False
    try:
        return Path(finished.stdout.strip()).resolve() == root.resolve()
    except OSError:
        return False


def _ensure_gitignore(root: Path) -> None:
    """Keep runtime bookkeeping out of the repository.

    A store committed by hand before this rule existed may already track these
    paths; untracking them once is what makes later runs leave no diff.
    """
    if not _is_repo(root):
        return
    path = root / ".gitignore"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    missing = [line for line in GITIGNORE_LINES if line not in lines]
    if missing:
        try:
            path.write_text("\n".join(lines + missing) + "\n", encoding="utf-8")
        except OSError:
            return
    try:
        tracked = _git(root, ["ls-files", "--", ".state", LOG_NAME])
    except (OSError, subprocess.SubprocessError):
        return
    if tracked.returncode == 0 and tracked.stdout.strip():
        try:
            _git(root, ["rm", "-r", "--cached", "-q", "--ignore-unmatch", "--", ".state", LOG_NAME])
        except (OSError, subprocess.SubprocessError):
            pass


def _git_commit(root: Path, message: str) -> bool:
    """Commit the whole tree when it differs from HEAD; return whether it did."""
    if not _is_repo(root):
        return False
    try:
        if _git(root, ["add", "-A"]).returncode != 0:
            return False
        if _git(root, ["diff", "--cached", "--quiet"]).returncode == 0:
            return False
        finished = _git(
            root,
            ["-c", "commit.gpgsign=false", "commit", "--no-verify", "-m", message],
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return finished.returncode == 0


def _commit_message(stats: Stats) -> str:
    return "dream: merged {0}, replaced {1}, index lines removed {2}".format(
        stats.merged, stats.replaced, stats.index_lines
    )


# --------------------------------------------------------------------------
# output


def _append_log(root: Path, stats: Stats) -> None:
    line = (
        "{time} dream scopes={scopes} merged={merged} replaced={replaced} "
        "index={index_lines} sessions={sessions} commits={commits}\n"
    ).format(time=store.now_iso(), **stats.as_dict())
    try:
        with open(root / LOG_NAME, "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
