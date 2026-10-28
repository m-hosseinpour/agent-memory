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


def global_dir() -> Path:
    return root() / "global"


def project_dir(slug: str) -> Path:
    """Directory for a project scope, from a slug already built by `slug_for`.

    An importer must pass `slug_for(real_cwd)`, not a directory name copied
    from another tool: Claude Code's names are lossy and would not match the
    scope `mem --cwd` reads.
    """
    return root() / "projects" / slug


def scope_dir(scope: str, cwd: str) -> Path:
    """Directory for a scope: `global`, or `project` for the given directory."""
    from .store import slug_for

    if scope == "global":
        return global_dir()
    if scope == "project":
        return project_dir(slug_for(cwd))
    raise ValueError(f"unknown scope {scope!r}; expected 'global' or 'project'")
