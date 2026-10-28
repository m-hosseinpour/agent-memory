"""End-of-session memory extraction (`mem extract`).

The Stop hook fires on every turn, so a run only reads the part of the
transcript that has not been sent to the model yet. The byte offset (or, for
SQLite transcripts, the last message `seq`) of that point lives in
`.state/extract.json`. Keeping the offset unchanged when the new tail is tiny
is deliberate: a few short turns should accumulate into one useful pass instead
of producing several useless ones.

The model call is one configurable command reading the prompt on stdin and
printing JSON on stdout. There is no SDK and no vendor client, so the same
module works with `claude -p`, `dsh -p`, or any script that follows the shape.
Nothing is written unless the model proposes a valid, non-duplicate memory.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import config, index, store

MAX_MEMORIES = 5
MIN_NEW_CHARS = 200
MAX_MESSAGE_CHARS = 4_000
MAX_TRANSCRIPT_CHARS = 60_000
MAX_INDEX_CHARS = 8_000
MAX_NAME_CHARS = 60
MAX_DESCRIPTION_CHARS = 200
MAX_ERROR_CHARS = 300
COMMAND_TIMEOUT = 20

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_SQLITE_MAGIC = b"SQLite format 3\x00"

PROMPT_HEADER = """You extract durable memories from a coding-agent session transcript.

Save:
- user preferences and corrections
- decisions and the reasons behind them
- environment facts that cannot be inferred from the code or git

Do not save:
- anything derivable from the code or git history
- the current temporary task
- anything AGENTS.md or CLAUDE.md already says

An empty {"memories": []} is the normal, common answer: most sessions contain
nothing durable. Do not invent memories to fill the list."""

PROMPT_JSON = """Reply with strict JSON only: no prose, no markdown fences, exactly this shape:

{"memories": [{"name": "<kebab-case-slug>", "description": "<one line, <= 200 chars>", "type": "user|feedback|project|reference", "content": "<markdown body: what, why, how to apply>"}]}"""


class ExtractError(Exception):
    """A failure that is reported as JSON, never as a traceback."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# entry point


def run(
    transcript: str = "",
    agent: str = "unknown",
    session: str = "",
    cwd: str = ".",
) -> int:
    """Extract memories once; prints exactly one JSON object and returns 0/1."""
    settings = config.load()
    if config.get("extract", "enabled", settings) is not True:
        _emit(_noop("disabled"))
        return 0

    command = str(config.get("extract", "command", settings) or "").strip()
    if not command:
        _emit(
            {
                "ok": False,
                "error": "config",
                "message": "extract.enabled is true but extract.command is empty",
            }
        )
        return 1

    try:
        return _run_enabled(command, transcript, agent, session, cwd)
    except ExtractError as exc:
        _emit_error(exc.code, exc.message)
        return 1
    except Exception as exc:  # a hook calls this; a traceback helps nobody
        _emit_error("internal", "{}: {}".format(exc.__class__.__name__, exc))
        return 1


def _run_enabled(
    command: str, transcript: str, agent: str, session: str, cwd: str
) -> int:
    path = _resolve_transcript(transcript, agent, session)
    project_cwd = os.path.abspath(os.path.expanduser(cwd or "."))
    fmt = _format_of(path)
    if fmt == "sqlite" and not session:
        raise ExtractError(
            "transcript", "sqlite transcripts need --session to select a conversation"
        )

    key = _state_key(path, fmt, session)
    previous = _stored_pos(key)

    if fmt == "sqlite":
        turns, new_pos = _sqlite_turns(path, session, previous)
    else:
        chunk, start = _text_tail(path, previous, fmt)
        # A writer may be mid-line. Leave that line for the next run instead of
        # losing it to a JSON parse error and advancing past it.
        usable, length = _complete_lines(chunk)
        turns = _jsonl_turns(usable.decode("utf-8", "replace"))
        new_pos = start + length

    rendered = _render(turns)
    if len(rendered) < MIN_NEW_CHARS:
        # Deliberately do not advance: the next turn's tail joins this one.
        _emit(_noop("nothing new"))
        return 0

    prompt = _build_prompt(project_cwd, rendered)
    proposals = _parse_output(_run_command(command, prompt, project_cwd))

    directory = config.scope_dir("project", project_cwd)
    written, skipped, refused = _write_memories(proposals, directory, session)
    if written:
        index.rebuild(directory)
    if new_pos != previous:
        _save_pos(key, new_pos, session)

    _emit(
        {
            "ok": True,
            "written": written,
            "skipped": skipped,
            "refused": refused,
            "reason": "ok",
        }
    )
    return 0


# --------------------------------------------------------------------------
# transcript resolution


def _resolve_transcript(transcript: str, agent: str, session: str) -> Path:
    if transcript:
        path = Path(os.path.expanduser(transcript))
        if path.is_file():
            return path
        raise ExtractError("transcript", "transcript not found: {}".format(transcript))

    agent_name = (agent or "").strip().lower()
    safe_session = _safe_session(session)
    if agent_name == "dsh" and safe_session:
        home = os.environ.get("DSH_HOME") or "~/.dsh"
        base = Path(os.path.expanduser(home)) / "sessions"
        if base.is_dir():
            matches = []
            for dirname in ("session-{}".format(safe_session), safe_session):
                matches.extend(base.glob("*/{}/session*.jsonl*".format(dirname)))
            newest = _newest(matches)
            if newest:
                return newest
    elif agent_name in ("claude-code", "claude") and safe_session:
        base = Path("~/.claude/projects").expanduser()
        if base.is_dir():
            newest = _newest(list(base.glob("*/{}.jsonl".format(safe_session))))
            if newest:
                return newest

    raise ExtractError(
        "transcript",
        "no transcript for agent {!r} and session {!r}".format(agent, session),
    )


def _newest(paths: list[Path]) -> Path | None:
    best: Path | None = None
    best_mtime = float("-inf")
    for path in paths:
        try:
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def _safe_session(value: str) -> str:
    """Session ids become path components, so allow only filename-safe ones."""
    text = str(value or "").strip()
    return text if re.fullmatch(r"[A-Za-z0-9._-]+", text) else ""


# --------------------------------------------------------------------------
# incremental state


def _state_key(path: Path, fmt: str, session: str) -> str:
    resolved = str(path.resolve())
    # A database holds many conversations; the session is part of its identity.
    return "{}#{}".format(resolved, session) if fmt == "sqlite" else resolved


def _stored_pos(key: str) -> int:
    entry = _read_state().get(key)
    if not isinstance(entry, dict):
        return 0
    try:
        return max(0, int(entry.get("pos")))
    except (TypeError, ValueError):
        return 0


def _read_state() -> dict:
    path = config.state_file().with_name("extract.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_pos(key: str, pos: int, session: str) -> None:
    state = _read_state()
    state[key] = {"pos": int(pos), "session": session, "updated": store.now_iso()}
    path = config.state_file().with_name("extract.json")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(".{}.{}.tmp".format(path.name, os.getpid()))
        try:
            tmp.write_text(
                json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError as exc:
        # The memories are already on disk; a failed offset write only means
        # the next run repeats this tail.
        _log("could not save extract state: {}".format(exc))


# --------------------------------------------------------------------------
# reading


def _format_of(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError as exc:
        raise ExtractError("transcript", "cannot read transcript {}: {}".format(path, exc))
    if path.suffix.lower() == ".zstd" or head.startswith(_ZSTD_MAGIC):
        return "zstd"
    if head.startswith(_SQLITE_MAGIC):
        return "sqlite"
    return "jsonl"


def _text_tail(path: Path, pos: int, fmt: str) -> tuple[bytes, int]:
    """Return (bytes from the offset, the offset actually used)."""
    if fmt == "zstd":
        data = _zstd_decode(path)
        start = pos if 0 <= pos <= len(data) else 0
        return data[start:], start
    try:
        size = path.stat().st_size
        start = pos if 0 <= pos <= size else 0
        with path.open("rb") as handle:
            handle.seek(start)
            return handle.read(), start
    except OSError as exc:
        raise ExtractError("transcript", "cannot read transcript {}: {}".format(path, exc))


def _complete_lines(chunk: bytes) -> tuple[bytes, int]:
    if not chunk:
        return b"", 0
    if chunk.endswith(b"\n"):
        return chunk, len(chunk)
    cut = chunk.rfind(b"\n")
    if cut == -1:
        return b"", 0
    return chunk[: cut + 1], cut + 1


def _zstd_decode(path: Path) -> bytes:
    try:
        from compression import zstd
    except ImportError:
        zstd = None
    if zstd is not None:
        try:
            return zstd.decompress(path.read_bytes())
        except Exception as exc:
            raise ExtractError(
                "transcript", "cannot decompress {}: {}".format(path.name, exc)
            )
    try:
        proc = subprocess.run(
            ["zstd", "-dc", str(path)],
            capture_output=True,
            timeout=COMMAND_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExtractError(
            "transcript", "no zstd decoder for {}: {}".format(path.name, exc)
        )
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", "replace")
        raise ExtractError(
            "transcript", "zstd failed for {}: {}".format(path.name, _one_line(detail))
        )
    return proc.stdout


def _jsonl_turns(text: str) -> list[tuple[str, str]]:
    """Read both JSONL dialects (Claude Code and DSH) by their event shape.

    The container format is detected from the file, but the line shape decides
    which reader applies. That keeps a transcript readable even when the agent
    name passed by the hook is missing or wrong.
    """
    turns: list[tuple[str, str]] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")

        if kind in ("user", "assistant"):
            if event.get("isSidechain"):  # subagent side conversations
                continue
            message = event.get("message")
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or kind)
            if role not in ("user", "assistant"):
                continue
            body = _block_text(message.get("content"))
            if body:
                turns.append((role, body))

        elif kind in ("user/message", "assistant/message"):
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            source = data.get("source")
            if isinstance(source, dict) and source.get("kind") == "memory":
                continue  # this tool's own injected recall, not the user
            if kind == "user/message":
                if str(data.get("role") or "user") != "user":
                    continue
                body = _block_text(data.get("content"))
                role = "user"
            else:
                message = data.get("message")
                if not isinstance(message, dict):
                    continue
                body = _block_text(message.get("content"))
                role = "assistant"
            if body:
                turns.append((role, body))
    return turns


def _block_text(content) -> str:
    """Concatenate text blocks; drop tool calls, results, reasoning, and files."""
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            value = block.get("text")
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return "\n".join(parts).strip()


def _sqlite_turns(path: Path, session: str, after: int) -> tuple[list[tuple[str, str]], int]:
    """Read rows newer than `after` for one session; soft-fail on a bad schema."""
    try:
        connection = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
    except sqlite3.Error as exc:
        _log("cannot open sqlite transcript: {}".format(exc))
        return [], after
    try:
        rows = connection.execute(
            "SELECT seq, type, data FROM session_message "
            "WHERE session_id = ? AND seq > ? AND type IN ('user', 'assistant') "
            "ORDER BY seq",
            (session, after),
        ).fetchall()
    except sqlite3.Error as exc:
        _log("sqlite transcript is not readable: {}".format(exc))
        return [], after
    finally:
        connection.close()

    turns: list[tuple[str, str]] = []
    newest = after
    for seq, kind, raw in rows:
        try:
            newest = max(newest, int(seq))
        except (TypeError, ValueError):
            pass
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        if kind == "user":
            value = data.get("text")
            body = value.strip() if isinstance(value, str) else ""
        else:
            body = _block_text(data.get("content"))
        if body:
            turns.append(("user" if kind == "user" else "assistant", body))
    return turns, newest


# --------------------------------------------------------------------------
# prompt


def _build_prompt(cwd: str, transcript: str) -> str:
    existing = _existing_index(cwd)
    return "\n\n".join(
        [
            PROMPT_HEADER,
            "Existing memories (do not propose them again):\n" + (existing or "(none)"),
            "Transcript (most recent turns):\n" + transcript,
            PROMPT_JSON,
        ]
    ) + "\n"


def _existing_index(cwd: str) -> str:
    blocks = []
    for scope in ("project", "global"):
        memories = store.list_memories(config.scope_dir(scope, cwd))
        if not memories:
            continue
        lines = ["## {}".format(scope)]
        for memory in memories:
            description = _one_line(memory.description, MAX_DESCRIPTION_CHARS)
            suffix = " — {}".format(description) if description else ""
            lines.append("- {}{}".format(memory.name, suffix))
        blocks.append("\n".join(lines))
    text = "\n\n".join(blocks)
    if len(text) > MAX_INDEX_CHARS:
        text = text[:MAX_INDEX_CHARS].rsplit("\n", 1)[0] + "\n… (index truncated)"
    return text


def _render(turns: list[tuple[str, str]]) -> str:
    paragraphs = []
    for role, text in turns:
        text = text.strip()
        if not text:
            continue
        if len(text) > MAX_MESSAGE_CHARS:
            text = text[:MAX_MESSAGE_CHARS].rstrip()
        paragraphs.append(
            "{}: {}".format("User" if role == "user" else "Assistant", text)
        )
    return _tail("\n\n".join(paragraphs), MAX_TRANSCRIPT_CHARS)


def _tail(text: str, limit: int) -> str:
    """Keep the end of the transcript, cut on a line boundary."""
    if len(text) <= limit:
        return text
    marker = "… (earlier turns omitted)\n"
    tail = text[-max(0, limit - len(marker)) :]
    cut = tail.find("\n")
    if cut != -1:
        tail = tail[cut + 1 :]
    return marker + tail


# --------------------------------------------------------------------------
# the model call


def _run_command(command: str, prompt: str, cwd: str) -> str:
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ExtractError("command", "cannot parse extract.command: {}".format(exc))
    if not argv:
        raise ExtractError("command", "extract.command is empty")

    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired:
        raise ExtractError(
            "command", "extract command timed out after {}s".format(COMMAND_TIMEOUT)
        )
    except OSError as exc:
        raise ExtractError("command", "could not run extract command: {}".format(exc))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ExtractError(
            "command",
            "extract command exited {}: {}".format(
                proc.returncode, _one_line(detail) or "no output"
            ),
        )
    return proc.stdout or ""


def _parse_output(raw: str) -> list:
    text = raw.strip()
    if not text:
        raise ExtractError("parse", "extract command printed nothing")

    text = _strip_fences(text)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ExtractError("parse", "output is not JSON: {}".format(_one_line(text)))
        try:
            data = json.loads(text[start : end + 1])
        except (ValueError, RecursionError):
            raise ExtractError(
                "parse", "output is not valid JSON: {}".format(_one_line(text))
            )
    if not isinstance(data, dict) or not isinstance(data.get("memories"), list):
        raise ExtractError("parse", 'output must be {"memories": [...]}')
    return data["memories"]


def _strip_fences(text: str) -> str:
    if not text.startswith("```"):
        return text
    newline = text.find("\n")
    if newline == -1:
        return text
    text = text[newline + 1 :].rstrip()
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


# --------------------------------------------------------------------------
# validation and writing


def _write_memories(
    proposals: list, directory: Path, session: str
) -> tuple[list[str], int, int]:
    written: list[str] = []
    skipped = max(0, len(proposals) - MAX_MEMORIES)
    refused = 0

    for item in proposals[:MAX_MEMORIES]:
        if not isinstance(item, dict):
            skipped += 1
            continue
        name = _clean_name(item.get("name"))
        if not name:
            skipped += 1
            continue
        description = _one_line(item.get("description"), MAX_DESCRIPTION_CHARS)
        memory_type = str(item.get("type") or "").strip().lower()
        if memory_type not in store.TYPES:
            memory_type = store.DEFAULT_TYPE
        content = item.get("content")
        content = content.strip() if isinstance(content, str) else ""
        if not content:
            skipped += 1
            continue

        filename = "{}.md".format(name)
        # Extraction is create-only: the model has not read existing bodies, so
        # a rewrite would blindly discard whatever a human or dream added.
        if (directory / filename).exists():
            skipped += 1
            continue

        try:
            store.write(
                directory,
                filename,
                content,
                name=name,
                description=description,
                memory_type=memory_type,
                if_version="new",
                session_id=session or None,
            )
        except store.SecretError:
            refused += 1
        except (store.ConflictError, store.StoreError):
            skipped += 1
        else:
            written.append(filename)

    return written, skipped, refused


def _clean_name(value) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    text = re.sub(r"[^a-z0-9-]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    text = text[:MAX_NAME_CHARS].strip("-")
    return text if _NAME_RE.match(text) else ""


def _one_line(value, limit: int = MAX_ERROR_CHARS) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit].strip()


# --------------------------------------------------------------------------
# output


def _noop(reason: str) -> dict:
    return {"ok": True, "written": [], "skipped": 0, "refused": 0, "reason": reason}


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")


def _emit_error(code: str, message: str) -> None:
    text = _one_line(message) or code
    _log("{}: {}".format(code, text))
    _emit({"ok": False, "error": code, "message": text})


def _log(message: str) -> None:
    try:
        print("mem extract: " + message, file=sys.stderr)
    except Exception:
        pass
