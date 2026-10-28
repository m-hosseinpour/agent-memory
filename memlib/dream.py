"""Consolidation of the store, run after every change.

`mem write` and `mem forget` call `after_change` on the scope they touched, and
the same pass is reachable by hand as `mem dream` (with `--all` to walk every
project). There is no schedule, no session counter and no background process:
a change is consolidated while the caller waits, so the store is never left in
a state that needs a later janitor.

What a pass does:

* two files of the same type with the same `name` are one note — the newer
  `modified` wins and the older file is removed (its body is *not* merged);
* two files of the same type whose descriptions use the same words are
  duplicates — the older's unique lines are appended to the newest and the older
  file is removed; a restatement in other words, or a description with more
  words than the threshold is tuned for, stays a separate note;
* `MEMORY.md` is rebuilt from the files, so a line for a deleted file cannot
  survive.

The pass is deterministic: no model call. At this size the cheap rules catch
nearly all drift, and a pass that costs tokens could not run on every write.
"Contradiction" therefore means identity, not semantics: a note whose *stance*
changed under a different name is not detected — guessing that from text
without a model is exactly the kind of heuristic that quietly deletes the wrong
half of a pair.

Concurrency: the pass itself takes no lock. It deletes a file only while its
bytes still match what the pass read, and writes a merged keeper with the
version it read, so a concurrent writer either wins cleanly or leaves both
files for the next pass. `store.write` and `store.delete` do hold a short
`flock` on the scope directory; this pass runs after that lock is released and
relies on the version re-checks. Rebuilding the index is atomic.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config, index, store

# Two notes merge only when their descriptions use the same words, and only
# under the word ceiling. A restatement with one added qualifier scored 0.86 at
# the old 0.8 cut-off ("deploy with make release and tag" vs "... tag blue") and
# the older, independent note was deleted. Word overlap cannot tell a filler
# word ("use pnpm not npm") from a qualifier, so the cut-off is put where only
# the same words can reach it: for n distinct words a pair that differs at all
# scores at most (n-1)/n, so 0.95 separates them up to 19 words (18/19 = 0.947,
# 19/20 = 0.95). Beyond that the same number would start accepting a qualifier,
# so a longer description is left alone instead of guessed at. Jaccard rather
# than string equality, so word order and punctuation do not matter.
DUPLICATE_THRESHOLD = 0.95
DUPLICATE_WORDS = 19


@dataclass
class Stats:
    scopes: int = 0
    merged: int = 0
    replaced: int = 0
    index_lines: int = 0

    def as_dict(self) -> dict:
        return {
            "scopes": self.scopes,
            "merged": self.merged,
            "replaced": self.replaced,
            "index_lines": self.index_lines,
        }


def after_change(directory: Path, keep: str | None = None) -> Stats:
    """Consolidate one scope after a memory was written or deleted.

    `keep` is the file the caller just wrote. The rules would otherwise be free
    to delete it — a same-name note whose `modified` ties to the millisecond,
    sorted by file name, can land before the new one — and the caller would
    report success for a note that is already gone.

    Best effort by design: the caller's change already landed, so a failure
    here is reported on stderr and left for the next pass instead of turning a
    successful write into an error. The index is rebuilt once, at the end of
    the pass; a pass that raises before reaching it falls back to a plain
    rebuild so the caller's change still shows up in `MEMORY.md`.
    """
    stats = Stats(scopes=1)
    try:
        _consolidate(directory, stats, keep)
    except Exception as exc:  # a janitor must never break the caller's write
        _warn(f"consolidation skipped: {type(exc).__name__}: {exc}")
        try:
            index.rebuild(directory)
        except OSError as rebuild_error:
            _warn(f"index not rebuilt: {rebuild_error}")
    return stats


def run(cwd: str = ".", all_scopes: bool = False) -> int:
    """Manual pass: consolidate the global scope and this project (or all)."""
    stats = Stats()
    try:
        directories = _scopes(cwd, all_scopes)
        stats.scopes = len(directories)
        errors: list[str] = []
        for directory in directories:
            try:
                _consolidate(directory, stats)
            except OSError as exc:
                errors.append(f"{directory.name}: {exc}")
    except Exception as exc:  # never a traceback instead of the JSON contract
        _emit({"ok": False, "error": "internal", "message": f"{type(exc).__name__}: {exc}"})
        return 1

    payload = {"ok": not errors, "ran": True, **stats.as_dict()}
    if errors:
        payload.update({"error": "partial", "message": "; ".join(errors)})
    _emit(payload)
    return 1 if errors else 0


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


def _consolidate(directory: Path, stats: Stats, keep: str | None = None) -> None:
    if not directory.is_dir():
        return
    memories = store.list_memories(directory)
    replaced = _replace_same_name(directory, memories, stats, keep)
    if replaced:
        memories = [m for m in memories if m.filename not in replaced]
    _merge_duplicates(directory, memories, stats, keep)
    stats.index_lines += _repair_index(directory)


def _replace_same_name(
    directory: Path, memories: list[store.Memory], stats: Stats, keep: str | None = None
) -> set[str]:
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
            if older.filename == keep:
                continue
            if _delete_unchanged(directory, older):
                removed.add(older.filename)
                stats.replaced += 1
    return removed


def _delete_unchanged(directory: Path, memory: store.Memory) -> bool:
    """Unlink only while the file still holds the bytes this pass looked at.

    The check and the unlink happen under the store's scope lock; doing them in
    two steps would let a write land in between and be deleted.
    """
    try:
        return store.delete(directory, memory.filename, if_version=memory.version)
    except store.StoreError:
        return False


def _merge_duplicates(
    directory: Path, memories: list[store.Memory], stats: Stats, keep: str | None = None
) -> None:
    """Fold notes with a near-identical description into the newest one.

    Bodies are merged line-wise: only lines the keeper does not already have are
    appended, so a near-duplicate's extra detail survives. The keeper is written
    before any duplicate is deleted, and the write carries the version this pass
    read; on a conflict both files stay for the next run.

    `keep` sorts first, so the note just written is the keeper even when its
    `modified` ties with an older duplicate — otherwise the tiebreak could make
    the new file the duplicate that gets deleted.
    """
    ordered = sorted(
        memories,
        key=lambda m: (m.filename == keep, m.modified, m.filename),
        reverse=True,
    )
    clusters: list[tuple[store.Memory, list[store.Memory]]] = []
    bodies: dict[str, str] = {}
    for memory in ordered:
        keeper = next(
            (
                entry
                for entry in clusters
                if entry[0].type == memory.type
                and similar_descriptions(entry[0].description, memory.description)
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
                    if_version=keeper.version,
                )
            except store.StoreError:
                continue
        for duplicate in duplicates:
            if duplicate.filename == keep:
                continue
            if _delete_unchanged(directory, duplicate):
                stats.merged += 1


def _repair_index(directory: Path) -> int:
    """Rebuild MEMORY.md and count the lines that pointed at gone files."""
    path = directory / "MEMORY.md"
    try:
        before = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        before = ""
    index.rebuild(directory)
    after = path.read_text(encoding="utf-8", errors="replace")
    if before == after:
        return 0
    # Only entry lines count; a changed "(N)" heading follows from them.
    old = {line.strip() for line in before.splitlines() if line.strip().startswith("- [")}
    new = {line.strip() for line in after.splitlines() if line.strip().startswith("- [")}
    return len(old - new)


def similar_descriptions(first: str, second: str) -> bool:
    """Whether two descriptions are the same note, judged only under the word ceiling."""
    left, right = store.tokens(first), store.tokens(second)
    if not left or not right:
        return False
    if len(left) > DUPLICATE_WORDS or len(right) > DUPLICATE_WORDS:
        return False
    return len(left & right) / len(left | right) >= DUPLICATE_THRESHOLD


def _merge_body(keeper: str, other: str) -> str:
    """Append the duplicate's own lines, keeping the blank lines between them.

    Line-wise, so a line the keeper already has is not repeated; block-wise, so
    the appended detail keeps the paragraph breaks `other` put it in instead of
    collapsing into one run of lines. A line the duplicate repeats itself — a
    `---` rule, a repeated line in a fenced block — is copied as written.
    """
    known = {line.strip() for line in keeper.splitlines() if line.strip()}
    blocks: list[str] = []
    for block in re.split(r"\n[ \t]*\n", other):
        kept = []
        for line in block.splitlines():
            text = line.strip()
            if text and text not in known:
                kept.append(line.rstrip())
        if kept:
            blocks.append("\n".join(kept))
    if not blocks:
        return keeper
    return keeper.rstrip() + "\n\n" + "\n\n".join(blocks)


def _warn(message: str) -> None:
    try:
        # No "dream" in the prefix: the same pass now runs after a write, where
        # naming a scheduler would only confuse whoever reads the log.
        sys.stderr.write(f"mem: {message}\n")
    except Exception:
        pass


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
