"""The `MEMORY.md` index: a one-line-per-memory table of contents.

The index is regenerated from the files themselves rather than edited in
place. Regeneration is a directory scan of a few dozen files, so it is cheap
enough to run on every write, and it makes drift impossible: a renamed file
can never leave a dangling line behind.
"""

from __future__ import annotations

from pathlib import Path

from .store import TYPES, Memory, list_memories

MAX_INDEX_LINES = 200
MAX_INDEX_BYTES = 25 * 1024


def line_for(memory: Memory, prefix: str = "") -> str:
    target = f"{prefix}{memory.filename}"
    description = memory.description.replace("\n", " ").strip()
    return f"- [{memory.title}]({target}) — {description}" if description else f"- [{memory.title}]({target})"


def render(memories: list[Memory], prefix: str = "") -> str:
    """Grouped index text: one section per type, newest first inside a section."""
    if not memories:
        return ""
    sections = []
    for memory_type in TYPES:
        group = [m for m in memories if m.type == memory_type]
        if not group:
            continue
        body = "\n".join(line_for(m, prefix) for m in group)
        sections.append(f"## {memory_type} ({len(group)})\n{body}")
    return "\n\n".join(sections)


def rebuild(directory: Path, prefix: str = "") -> str:
    """Rewrite MEMORY.md from the files on disk and return the new text."""
    directory.mkdir(parents=True, exist_ok=True)
    text = render(list_memories(directory), prefix)
    header = "# Memory\n\n" if text else "# Memory\n\n_empty_\n"
    (directory / "MEMORY.md").write_text(header + (text + "\n" if text else ""), encoding="utf-8")
    return text


def cap(text: str, max_lines: int = MAX_INDEX_LINES, max_bytes: int = MAX_INDEX_BYTES) -> str:
    """Trim an index block to the budget, keeping whole lines."""
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + ["… (index truncated)"]
    out = "\n".join(lines)
    if len(out.encode("utf-8")) > max_bytes:
        out = out.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
        out = out.rsplit("\n", 1)[0] + "\n… (index truncated)"
    return out
