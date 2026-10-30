"""The `MEMORY.md` index: a one-line-per-memory table of contents.

The index is regenerated from the files themselves rather than edited in
place. Regeneration is a directory scan of a few dozen files, so it is cheap
enough to run on every change, and it makes drift impossible: a renamed file
can never leave a dangling line behind.

There is no line budget. The index is the whole of retrieval: an agent reads it
and then loads the one note that applies, so a truncated index would hide notes
rather than save context.
"""

from __future__ import annotations

from pathlib import Path

from . import store
from .store import TYPES, Memory, list_memories


def line_for(memory: Memory) -> str:
    target = memory.filename
    description = memory.description.strip()
    return f"- [{memory.title}]({target}) — {description}" if description else f"- [{memory.title}]({target})"


def render(memories: list[Memory]) -> str:
    """Grouped index text: one section per type, newest first inside a section."""
    if not memories:
        return ""
    sections = []
    for memory_type in TYPES:
        group = [m for m in memories if m.type == memory_type]
        if not group:
            continue
        body = "\n".join(line_for(m) for m in group)
        sections.append(f"## {memory_type} ({len(group)})\n{body}")
    return "\n\n".join(sections)


def rebuild(directory: Path) -> str:
    """Rewrite MEMORY.md from the files on disk and return the new text."""
    directory.mkdir(parents=True, exist_ok=True)
    text = render(list_memories(directory))
    header = "# Memory\n\n" if text else "# Memory\n\n_empty_\n"
    target = directory / "MEMORY.md"
    # Atomic (and LF-only): another process may be reading the index while this
    # one repairs it, and a half-written table of contents is worse than a stale
    # one. `store.replace_file` owns the temp name and the Windows rename retry.
    store.replace_file(target, header + (text + "\n" if text else ""))
    return text
