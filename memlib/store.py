"""Filesystem store for agent memories.

One memory = one markdown file with a YAML-ish frontmatter block holding
`name`, `description` and `metadata.{node_type,type,originSessionId,modified}`.
That shape is deliberately identical to Claude Code's memory files, so the
same directory stays readable by hand, by `grep`, and by other agents.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TYPES = ("user", "feedback", "project", "reference")
DEFAULT_TYPE = "project"
MAX_BODY_BYTES = 100 * 1024

_FM_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.S)


class StoreError(Exception):
    """Base class for store failures that the CLI reports verbatim."""


class ConflictError(StoreError):
    def __init__(self, message: str, current: "Memory | None" = None) -> None:
        super().__init__(message)
        self.current = current


class SecretError(StoreError):
    pass


# Keys are matched as assignments, not as bare words, so that prose like
# "the token budget is small" does not trip the check.
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:sk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|secret|passwd|password|access[_-]?token|auth[_-]?token)"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+]{16,}"
    ),
)


def find_secret(text: str) -> str | None:
    """Return the name of the first matching secret pattern, if any."""
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return pattern.pattern
    return None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def version_of(data: bytes) -> str:
    """Content-derived version token; no state to keep in sync."""
    return hashlib.sha256(data).hexdigest()[:12]


def slug_for(cwd: str | os.PathLike[str]) -> str:
    """Map a working directory to a stable directory name.

    `/home/u/proj` becomes `-home-u-proj`, the same convention Claude Code and
    Qoder use, so directories are recognisable across tools.
    """
    absolute = os.path.abspath(os.fspath(cwd))
    return re.sub(r"[^A-Za-z0-9._-]", "-", absolute)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        inner = value[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\") if value[0] == '"' else inner
    return value


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def parse(text: str) -> tuple[dict, str]:
    """Split a memory file into (frontmatter, body).

    Only the YAML subset this project writes is understood: scalars and one
    level of nesting. That is enough to round-trip our own files and to read
    files written by Claude Code.
    """
    match = _FM_RE.match(text)
    if not match:
        return {}, text

    meta: dict = {}
    nested_key: str | None = None
    for raw in match.group(1).splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[:1] in (" ", "\t")
        key, sep, value = line.strip().partition(":")
        if not sep:
            continue
        key = key.strip()
        if indented and nested_key:
            if not isinstance(meta.get(nested_key), dict):
                meta[nested_key] = {}
            meta[nested_key][key] = _unquote(value)
        elif value.strip():
            meta[key] = _unquote(value)
            nested_key = None
        else:
            meta[key] = {}
            nested_key = key
    return meta, text[match.end():]


def render(meta: dict, body: str) -> str:
    """Serialise frontmatter + body, keeping a stable key order."""
    lines = ["---"]
    for key in ("name", "description"):
        if key in meta:
            lines.append(f"{key}: {_quote(str(meta[key]))}")
    metadata = meta.get("metadata") or {}
    if metadata:
        lines.append("metadata:")
        for key in ("node_type", "type", "originSessionId", "modified"):
            if key in metadata:
                lines.append(f"  {key}: {_quote(str(metadata[key]))}")
        for key, value in metadata.items():
            if key not in ("node_type", "type", "originSessionId", "modified"):
                lines.append(f"  {key}: {_quote(str(value))}")
    for key, value in meta.items():
        if key in ("name", "description", "metadata"):
            continue
        lines.append(f"{key}: {_quote(str(value))}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.lstrip("\n")


@dataclass
class Memory:
    path: Path
    name: str
    description: str
    type: str
    body: str
    version: str
    modified: str
    meta: dict = field(default_factory=dict)
    size: int = 0

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def title(self) -> str:
        explicit = self.meta.get("title")
        if explicit:
            return str(explicit)
        words = re.split(r"[-_]+", self.name)
        text = " ".join(w for w in words if w)
        return text[:1].upper() + text[1:] if text else self.filename


def _mtime_iso(path: Path) -> str:
    return (
        datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def load(path: Path) -> Memory:
    data = path.read_bytes()
    meta, body = parse(data.decode("utf-8", "replace"))
    metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    memory_type = str(metadata.get("type") or meta.get("type") or DEFAULT_TYPE)
    if memory_type not in TYPES:
        memory_type = DEFAULT_TYPE
    modified = str(metadata.get("modified") or _mtime_iso(path))
    return Memory(
        path=path,
        name=str(meta.get("name") or path.stem),
        description=str(meta.get("description") or ""),
        type=memory_type,
        body=body.strip(),
        version=version_of(data),
        modified=modified,
        meta=meta,
        size=len(data),
    )


def list_memories(directory: Path) -> list[Memory]:
    """Every memory file in a store directory, newest first."""
    if not directory.is_dir():
        return []
    memories = []
    for path in directory.glob("*.md"):
        if path.name == "MEMORY.md":
            continue
        try:
            memories.append(load(path))
        except OSError:
            continue
    memories.sort(key=lambda m: m.modified, reverse=True)
    return memories


def write(
    directory: Path,
    filename: str,
    body: str,
    *,
    name: str | None = None,
    description: str = "",
    memory_type: str = DEFAULT_TYPE,
    if_version: str | None = None,
    session_id: str | None = None,
    force: bool = False,
) -> Memory:
    """Create or replace one memory, enforcing optimistic concurrency.

    `if_version` is the token returned by the last read, or the literal `new`
    for a file that must not exist yet. Omitting it is refused unless `force`
    is set, because a blind overwrite is exactly what this guard exists to
    prevent.
    """
    if memory_type not in TYPES:
        raise StoreError(f"unknown type {memory_type!r}; expected one of {', '.join(TYPES)}")
    if "/" in filename or filename in ("", ".", "..") or not filename.endswith(".md"):
        raise StoreError("filename must be a flat name ending in .md")
    if filename == "MEMORY.md":
        raise StoreError("MEMORY.md is generated; choose another name")

    encoded = body.encode("utf-8")
    if len(encoded) > MAX_BODY_BYTES:
        raise StoreError(f"body is {len(encoded)} bytes, over the {MAX_BODY_BYTES} cap")
    if not body.strip():
        raise StoreError("body is empty")

    secret = find_secret(body) or find_secret(description)
    if secret:
        raise SecretError(f"content looks like a secret (matched /{secret}/); refusing to store it")

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    existing: Memory | None = None
    if path.exists():
        existing = load(path)
        if if_version == "new":
            raise ConflictError(
                f"{filename} already exists; read it first and pass its version", existing
            )
        if if_version not in (None, existing.version) and not force:
            raise ConflictError(
                f"{filename} changed since it was read; re-read and merge", existing
            )
    elif if_version not in (None, "new") and not force:
        raise StoreError(f"{filename} does not exist; pass if_version=new to create it")
    if if_version is None and not force:
        raise StoreError("if_version is required (pass the version from read, or 'new')")

    metadata = dict((existing.meta.get("metadata") if existing else {}) or {})
    metadata.update(
        {
            "node_type": "memory",
            "type": memory_type,
            "originSessionId": session_id or metadata.get("originSessionId", ""),
            "modified": now_iso(),
        }
    )
    meta = {"name": name or path.stem, "description": description, "metadata": metadata}
    text = render(meta, body + "\n")

    tmp = directory / f".{filename}.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return load(path)


def delete(directory: Path, filename: str) -> bool:
    if "/" in filename or filename == "MEMORY.md":
        raise StoreError("refusing to delete that path")
    path = directory / filename
    if not path.exists():
        return False
    path.unlink()
    return True


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9\u0600-\u06FF]+", text.lower()) if len(t) > 2}


def recall(memories: list[Memory], query: str, top_k: int = 8) -> list[tuple[float, Memory]]:
    """Lexical relevance over name+description.

    Deterministic and free; the index is small enough that a model call would
    be wasted work. Returns (score, memory) sorted by score, zero-score dropped.
    """
    wanted = _tokens(query)
    if not wanted:
        return []
    scored = []
    for memory in memories:
        haystack = _tokens(f"{memory.name} {memory.description} {memory.type}")
        if not haystack:
            continue
        overlap = len(wanted & haystack)
        if not overlap:
            continue
        score = overlap / (len(wanted) ** 0.5) / (len(haystack) ** 0.25)
        scored.append((round(score, 3), memory))
    scored.sort(key=lambda pair: (-pair[0], pair[1].filename))
    return scored[:top_k]
