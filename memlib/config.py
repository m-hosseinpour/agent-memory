"""Paths and settings.

Root defaults to `~/.agents/memory` and can be overridden with `MEM_ROOT` or
`root` in `config.toml`. Only the handful of sections this tool needs are
parsed, so there is no dependency on a TOML library.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = "~/.agents/memory"

# Section -> key -> default. Unknown keys are ignored rather than fatal: a
# config written for a later version should not break an older binary.
DEFAULTS: dict[str, dict[str, object]] = {
    "context": {"index_max_lines": 200},
    "recall": {"select_threshold": 40, "top_k": 8},
    "dream": {"every_sessions": 5},
}


def root() -> Path:
    override = os.environ.get("MEM_ROOT")
    if override:
        return Path(os.path.expanduser(override))
    return Path(os.path.expanduser(DEFAULT_ROOT))


def _coerce(value: str) -> object:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    try:
        return int(text)
    except ValueError:
        return text


def load(path: Path | None = None) -> dict[str, dict[str, object]]:
    """Defaults merged with whatever config.toml declares."""
    settings = {section: dict(values) for section, values in DEFAULTS.items()}
    config_path = path or (root() / "config.toml")
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError:
        return settings

    section = ""
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            settings.setdefault(section, {})
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        settings.setdefault(section, {})[key.strip()] = _coerce(value)
    return settings


def get(section: str, key: str, settings: dict | None = None) -> object:
    data = settings if settings is not None else load()
    return data.get(section, {}).get(key, DEFAULTS.get(section, {}).get(key))


def every_sessions(settings: dict | None = None) -> int:
    """Sessions between consolidations.

    Both the hook that spawns `mem dream` and the pass itself ask here, so the
    trigger and the gate can never disagree about the threshold.
    """
    try:
        return int(get("dream", "every_sessions", settings))
    except (TypeError, ValueError):
        return int(DEFAULTS["dream"]["every_sessions"])


def scope_dir(scope: str, cwd: str) -> Path:
    """Directory for a scope: `global`, or `project` for the given directory."""
    from .store import slug_for

    base = root()
    if scope == "global":
        return base / "global"
    if scope == "project":
        return base / "projects" / slug_for(cwd)
    raise ValueError(f"unknown scope {scope!r}; expected 'global' or 'project'")


def state_file() -> Path:
    return root() / ".state" / "state.json"
