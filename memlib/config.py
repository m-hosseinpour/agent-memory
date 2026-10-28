"""Paths: the store root and the two scope directories.

Root defaults to `~/.agents/memory` and can be overridden with `MEM_ROOT`, or
with `--root` on the CLI. There is no config file: the store has no tunables
left, so a file that could only restate the defaults was removed.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = "~/.agents/memory"


def root() -> Path:
    override = os.environ.get("MEM_ROOT")
    if override:
        return Path(os.path.expanduser(override))
    return Path(os.path.expanduser(DEFAULT_ROOT))


def scope_dir(scope: str, cwd: str) -> Path:
    """Directory for a scope: `global`, or `project` for the given directory."""
    from .store import slug_for

    base = root()
    if scope == "global":
        return base / "global"
    if scope == "project":
        return base / "projects" / slug_for(cwd)
    raise ValueError(f"unknown scope {scope!r}; expected 'global' or 'project'")
