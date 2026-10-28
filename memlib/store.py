"""Filesystem store for agent memories.

One memory = one markdown file with a YAML-ish frontmatter block holding
`name`, `description` and `metadata.{node_type,type,modified}`. The shape is
Claude Code's minus its `originSessionId` (design §11: this server has no
reliable session identity, so an empty provenance field would only lie). The
files stay readable by hand, by `grep`, and by other agents.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

TYPES = ("user", "feedback", "project", "reference")
DEFAULT_TYPE = "project"
MAX_BODY_BYTES = 100 * 1024

_FM_RE = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.S)


@contextmanager
def _locked(directory: Path):
    """Serialize the read-check-write (and read-check-unlink) in one scope.

    The lock is the directory's own inode, so it leaves no file and no state
    behind and the kernel drops it when the process exits. Without it the
    `if_version` check is a check-then-act: two writers can both read the same
    version, both pass, and one silently overwrites the other.
    """
    handle = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


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
        for key in ("node_type", "type", "modified"):
            if key in metadata:
                lines.append(f"  {key}: {_quote(str(metadata[key]))}")
        for key, value in metadata.items():
            if key not in ("node_type", "type", "modified"):
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
    # The filename tiebreaker keeps the order stable when two notes share a
    # timestamp; otherwise the index could reorder between runs and dream's
    # "second run changes nothing" guarantee would not hold.
    memories.sort(key=lambda m: (m.modified, m.filename), reverse=True)
    return memories


# Every character `str.splitlines()` treats as a line boundary, which is what
# `parse` uses. A line break of any kind ends the scalar early and leaves the
# rest of the value inside the frontmatter block.
_LINE_BREAKS = frozenset("\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029")


def _single_line(value: str, field: str) -> None:
    """Frontmatter is parsed line by line, so a quoted scalar must stay on one."""
    if any(ch in _LINE_BREAKS for ch in value):
        raise StoreError(f"{field} must be a single line (it contains a line break)")


def check_filename(filename: str) -> str:
    if (
        "/" in filename
        or "\\" in filename
        or filename in ("", ".", "..")
        or not filename.endswith(".md")
    ):
        raise StoreError("filename must be a flat name ending in .md")
    if filename == "MEMORY.md":
        raise StoreError("MEMORY.md is the generated index, not a memory")
    if any(ch in filename for ch in "\r\n\0"):
        raise StoreError("filename must not contain control characters")
    return filename


def write(
    directory: Path,
    filename: str,
    body: str,
    *,
    name: str | None = None,
    description: str | None = None,
    memory_type: str | None = None,
    if_version: str | None = None,
    force: bool = False,
) -> Memory:
    """Create or replace one memory, enforcing optimistic concurrency.

    `if_version` is the token returned by the last read, or the literal `new`
    for a file that must not exist yet. Omitting it is refused unless `force`
    is set, because a blind overwrite is exactly what this guard exists to
    prevent.

    `name`, `description` and `memory_type` default to `None`, meaning "keep
    what the file already has"; a rewritten note keeps its identity unless the
    caller passes a different value. For a new file they fall back to the
    filename stem, an empty description and `project`. An explicit empty
    description clears it; a blank name falls back instead of blanking the file.
    `name` and `description` must be one line: the frontmatter is parsed line
    by line, so a line break is refused before anything is written.
    """
    check_filename(filename)
    # Refuse before touching the directory: a rejected write must leave no file
    # and no newly created scope behind.
    if name is not None:
        _single_line(name, "name")
    if description is not None:
        _single_line(description, "description")
    if if_version is None and not force:
        raise StoreError("if_version is required (pass the version from read, or 'new')")

    encoded = body.encode("utf-8")
    if len(encoded) > MAX_BODY_BYTES:
        raise StoreError(f"body is {len(encoded)} bytes, over the {MAX_BODY_BYTES} cap")
    if not body.strip():
        raise StoreError("body is empty")

    secret = find_secret(body) or (find_secret(description) if description else None)
    if secret:
        raise SecretError(f"content looks like a secret (matched /{secret}/); refusing to store it")

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    # The lock spans read-check-replace: without it the guard is a
    # check-then-act, and two writers holding the same token both pass.
    with _locked(directory):
        existing = load(path) if path.exists() else None
        if existing is not None:
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

        # Fields the caller did not pass keep what the file already holds: a
        # rewrite that carries only a body must not change the note's identity.
        # Frontmatter keys beyond these three are dropped on purpose: the store
        # owns the file format (design §11).
        final_name = name or (existing.name if existing else path.stem)
        final_description = (
            description
            if description is not None
            else (existing.description if existing else "")
        )
        final_type = (
            memory_type
            if memory_type is not None
            else (existing.type if existing else DEFAULT_TYPE)
        )
        if final_type not in TYPES:
            raise StoreError(
                f"unknown type {final_type!r}; expected one of {', '.join(TYPES)}"
            )

        metadata = dict((existing.meta.get("metadata") if existing else {}) or {})
        # A file written by another tool may still carry provenance this store
        # no longer records; writing keeps only the managed keys' values, so the
        # field goes rather than surviving as half-kept state.
        metadata.pop("originSessionId", None)
        metadata.update(
            {
                "node_type": "memory",
                "type": final_type,
                "modified": now_iso(),
            }
        )
        meta = {
            "name": final_name,
            "description": final_description,
            "metadata": metadata,
        }
        text = render(meta, body + "\n")

        # Unique per process: two agents writing the same note must not interleave
        # inside one temp file before either replaces it.
        tmp = directory / f".{filename}.{os.getpid()}.tmp"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return load(path)


def delete(directory: Path, filename: str, if_version: str | None = None) -> bool:
    """Remove one memory; with `if_version`, only while the bytes still match.

    The check and the unlink happen under the scope lock. A separate
    load-then-delete could not promise that: a write landing between the two
    steps would be deleted by a guard that had already passed.
    """
    check_filename(filename)
    if not directory.is_dir():
        return False
    path = directory / filename
    with _locked(directory):
        if not path.exists():
            return False
        if if_version is not None and version_of(path.read_bytes()) != if_version:
            return False
        path.unlink()
        return True


def tokens(text: str) -> set[str]:
    """Words used to compare descriptions; shared with dream's duplicate rule.

    Every word counts, however short. The rule compares the sets for identity,
    and a short qualifier ("v2" versus "v3") is exactly what has to keep two
    notes apart.
    """
    return {t for t in re.split(r"[^a-z0-9\u0600-\u06FF]+", text.lower()) if t}
