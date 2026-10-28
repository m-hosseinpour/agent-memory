"""Tests for end-of-session extraction (`mem extract`).

The model is replaced by a small Python script written into each test's temp
directory. It records the prompt and its invocation count, then prints whatever
canned response the test put next to it. No network, no real LLM.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memlib import cli, config, extract, store

FAKE_LLM = '''\
import json
import os
import sys
import time
from pathlib import Path

here = Path(__file__).resolve().parent
prompt = sys.stdin.read()
(here / "prompt.txt").write_text(prompt, encoding="utf-8")
(here / "child_marker.txt").write_text(
    os.environ.get("MEM_EXTRACT_CHILD", ""), encoding="utf-8"
)
with (here / "calls.txt").open("a", encoding="utf-8") as handle:
    handle.write("call\\n")

mode_file = here / "mode.txt"
mode = mode_file.read_text(encoding="utf-8").strip() if mode_file.exists() else ""
if mode == "fail":
    sys.stderr.write("the model exploded\\n")
    sys.exit(3)
if mode == "sleep":
    time.sleep(60)
    sys.exit(0)
if mode == "garbage":
    sys.stdout.write("this is not JSON at all")
    sys.exit(0)

response = here / "response.txt"
if response.exists():
    sys.stdout.write(response.read_text(encoding="utf-8"))
else:
    sys.stdout.write('{"memories": []}')
'''


class ExtractTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mem-extract-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self._old_root = os.environ.get("MEM_ROOT")
        os.environ["MEM_ROOT"] = str(self.root)
        self.addCleanup(self._restore_root)
        self.cwd = self.root / "project"
        self.cwd.mkdir()
        self.script = self.root / "fake_llm.py"
        self.script.write_text(FAKE_LLM, encoding="utf-8")
        self.project_dir = config.scope_dir("project", str(self.cwd))

    def _restore_root(self):
        if self._old_root is None:
            os.environ.pop("MEM_ROOT", None)
        else:
            os.environ["MEM_ROOT"] = self._old_root

    # -- helpers ---------------------------------------------------------

    def write_config(self, enabled=True, command=None, timeout=None):
        if command is None:
            command = "{} {}".format(
                shlex.quote(sys.executable or "python3"), shlex.quote(str(self.script))
            )
        lines = ["[extract]", "enabled = {}".format("true" if enabled else "false")]
        if command:
            lines.append("command = {}".format(command))
        if timeout is not None:
            lines.append("timeout = {}".format(timeout))
        (self.root / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    def set_response(self, payload):
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (self.root / "response.txt").write_text(text, encoding="utf-8")

    def set_mode(self, mode):
        path = self.root / "mode.txt"
        if mode:
            path.write_text(mode, encoding="utf-8")
        else:
            path.unlink(missing_ok=True)

    def calls(self):
        path = self.root / "calls.txt"
        if not path.exists():
            return 0
        return len(path.read_text(encoding="utf-8").splitlines())

    def prompt(self):
        return (self.root / "prompt.txt").read_text(encoding="utf-8")

    def run_extract(self, *args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cli.main(["extract", *args])
        return code, stdout.getvalue()

    def extract(self, transcript, agent="unknown", session="", cwd=None):
        return self.run_extract(
            "--transcript",
            str(transcript),
            "--agent",
            agent,
            "--session",
            session,
            "--cwd",
            str(self.cwd if cwd is None else cwd),
        )

    def output(self, code, text):
        """Parse the single JSON line extraction is allowed to print."""
        self.assertEqual(len(text.strip().splitlines()), 1, "stdout: {!r}".format(text))
        data = json.loads(text)
        return code, data

    def write_jsonl(self, lines, name="transcript.jsonl"):
        path = self.root / name
        path.write_text(
            "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
        )
        return path

    def claude_transcript(self):
        filler = "durable detail " * 20
        return self.write_jsonl(
            [
                {
                    "type": "user",
                    "isSidechain": False,
                    "message": {
                        "role": "user",
                        "content": "CLAUDE-USER-MARKER " + filler,
                    },
                },
                {
                    "type": "assistant",
                    "isSidechain": False,
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "CLAUDE-THINKING-MARKER"},
                            {"type": "text", "text": "CLAUDE-ASSISTANT-MARKER " + filler},
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"description": "CLAUDE-TOOL-MARKER"},
                            },
                        ],
                    },
                },
                {
                    "type": "user",
                    "isSidechain": False,
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "content": "CLAUDE-TOOLRESULT-MARKER"}
                        ],
                    },
                },
                {
                    "type": "assistant",
                    "isSidechain": True,
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "CLAUDE-SIDECHAIN-MARKER"}],
                    },
                },
                {"type": "attachment", "content": "CLAUDE-ATTACHMENT-MARKER"},
            ]
        )

    def dsh_transcript(self):
        filler = "durable detail " * 20
        lines = [
            {"type": "session", "version": 4},
            {
                "type": "system/message",
                "seq": 0,
                "data": {"message": {"content": [{"type": "text", "text": "DSH-SYSTEM-MARKER"}]}},
            },
            {
                "type": "user/message",
                "seq": 1,
                "data": {
                    "role": "user",
                    "source": {"kind": "user"},
                    "content": [{"type": "text", "text": "DSH-USER-MARKER " + filler}],
                },
            },
            {
                "type": "user/message",
                "seq": 2,
                "data": {
                    "role": "user",
                    "source": {"kind": "memory"},
                    "content": [{"type": "text", "text": "DSH-MEMORY-MARKER " + filler}],
                },
            },
            {
                "type": "assistant/message",
                "seq": 3,
                "data": {
                    "message": {
                        "content": [
                            {"type": "reasoning", "text": "DSH-REASONING-MARKER"},
                            {"type": "tool-call", "name": "bash"},
                            {"type": "text", "text": "DSH-ASSISTANT-MARKER " + filler},
                        ]
                    }
                },
            },
            {"type": "tool/call", "seq": 4, "data": {"name": "bash"}},
            {"type": "tool/result", "seq": 5, "data": {"message": "DSH-TOOLRESULT-MARKER"}},
        ]
        decoded = ("\n".join(json.dumps(line) for line in lines) + "\n").encode("utf-8")
        path = self.root / "session.v4.jsonl.zstd"
        path.write_bytes(self.zstd_compress(decoded))
        return path

    def zstd_compress(self, data):
        try:
            from compression import zstd

            return zstd.compress(data)
        except ImportError:
            if shutil.which("zstd") is None:
                self.skipTest("no zstd decoder available")
            proc = subprocess.run(
                ["zstd", "-q", "-c"], input=data, capture_output=True, timeout=30
            )
            if proc.returncode != 0:
                self.skipTest("zstd CLI could not compress the fixture")
            return proc.stdout

    def sqlite_transcript(self):
        path = self.root / "opencode.db"
        connection = sqlite3.connect(str(path))
        try:
            connection.execute(
                "CREATE TABLE session_message ("
                "id TEXT, session_id TEXT, type TEXT, seq INTEGER, "
                "time_created INTEGER, time_updated INTEGER, data TEXT)"
            )
            rows = [
                ("m1", "s1", "user", 1, {"text": "SQLITE-USER-MARKER " + "durable detail " * 20}),
                (
                    "m2",
                    "s1",
                    "assistant",
                    2,
                    {
                        "content": [
                            {"type": "text", "text": "SQLITE-ASSISTANT-MARKER " + "durable detail " * 20},
                            {"type": "tool", "name": "bash"},
                        ]
                    },
                ),
                ("m3", "s1", "system", 3, {"text": "SQLITE-SYSTEM-MARKER"}),
                ("m4", "s1", "idle", 4, {}),
                ("m5", "s2", "user", 5, {"text": "SQLITE-OTHER-SESSION-MARKER " + "durable detail " * 20}),
            ]
            connection.executemany(
                "INSERT INTO session_message VALUES (?, ?, ?, ?, 0, 0, ?)",
                [(name, session, kind, seq, json.dumps(data)) for name, session, kind, seq, data in rows],
            )
            connection.commit()
        finally:
            connection.close()
        return path

    def state(self):
        path = self.root / ".state" / "extract.json"
        return json.loads(path.read_text(encoding="utf-8"))


class GatingTests(ExtractTestCase):
    def test_disabled_is_a_cheap_no_op(self):
        self.write_config(enabled=False)
        transcript = self.claude_transcript()
        code, data = self.output(*self.extract(transcript, agent="claude-code", session="s1"))
        self.assertEqual(code, 0)
        self.assertEqual(
            data,
            {"ok": True, "written": [], "skipped": 0, "refused": 0, "reason": "disabled"},
        )
        self.assertFalse((self.root / "prompt.txt").exists())
        self.assertEqual(self.calls(), 0)

    def test_enabled_without_command_is_a_config_error(self):
        (self.root / "config.toml").write_text("[extract]\nenabled = true\n", encoding="utf-8")
        code, data = self.output(*self.extract(self.root / "missing.jsonl"))
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], "config")
        self.assertEqual(data["message"], "extract.enabled is true but extract.command is empty")
        self.assertEqual(self.calls(), 0)


class ClaudeReaderTests(ExtractTestCase):
    def test_prompt_keeps_text_and_drops_tools_and_sidechains(self):
        self.write_config()
        self.set_response({"memories": []})
        transcript = self.claude_transcript()
        code, data = self.output(*self.extract(transcript, agent="claude-code", session="s1"))
        self.assertEqual((code, data["reason"]), (0, "ok"))

        prompt = self.prompt()
        self.assertIn("User: CLAUDE-USER-MARKER", prompt)
        self.assertIn("Assistant: CLAUDE-ASSISTANT-MARKER", prompt)
        for marker in (
            "CLAUDE-THINKING-MARKER",
            "CLAUDE-TOOL-MARKER",
            "CLAUDE-TOOLRESULT-MARKER",
            "CLAUDE-SIDECHAIN-MARKER",
            "CLAUDE-ATTACHMENT-MARKER",
        ):
            self.assertNotIn(marker, prompt)
        self.assertEqual(data["written"], [])


class DshReaderTests(ExtractTestCase):
    def test_prompt_keeps_user_and_assistant_and_drops_memory_injection(self):
        self.write_config()
        self.set_response({"memories": []})
        transcript = self.dsh_transcript()
        code, data = self.output(*self.extract(transcript, agent="dsh", session="s1"))
        self.assertEqual((code, data["reason"]), (0, "ok"))

        prompt = self.prompt()
        self.assertIn("User: DSH-USER-MARKER", prompt)
        self.assertIn("Assistant: DSH-ASSISTANT-MARKER", prompt)
        for marker in (
            "DSH-MEMORY-MARKER",
            "DSH-REASONING-MARKER",
            "DSH-SYSTEM-MARKER",
            "DSH-TOOLRESULT-MARKER",
        ):
            self.assertNotIn(marker, prompt)


class SqliteReaderTests(ExtractTestCase):
    def test_rows_are_filtered_by_session_and_type(self):
        self.write_config()
        self.set_response({"memories": []})
        transcript = self.sqlite_transcript()
        code, data = self.output(*self.extract(transcript, agent="opencode", session="s1"))
        self.assertEqual((code, data["reason"]), (0, "ok"))

        prompt = self.prompt()
        self.assertIn("User: SQLITE-USER-MARKER", prompt)
        self.assertIn("Assistant: SQLITE-ASSISTANT-MARKER", prompt)
        self.assertNotIn("SQLITE-SYSTEM-MARKER", prompt)
        self.assertNotIn("SQLITE-OTHER-SESSION-MARKER", prompt)

        state = self.state()
        key = "{}#s1".format(transcript.resolve())
        self.assertEqual(state[key]["pos"], 2)
        self.assertEqual(state[key]["session"], "s1")

    def test_missing_table_fails_soft(self):
        self.write_config()
        self.set_response({"memories": []})
        path = self.root / "broken.db"
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("CREATE TABLE something_else (id TEXT)")
            connection.commit()
        finally:
            connection.close()

        code, data = self.output(*self.extract(path, agent="opencode", session="s1"))
        self.assertEqual((code, data["reason"]), (0, "nothing new"))
        self.assertEqual(self.calls(), 0)

    def test_a_path_with_uri_characters_is_still_opened(self):
        # A hand-built file: URI would read the wrong file here; as_uri escapes.
        self.write_config()
        self.set_response({"memories": []})
        weird = self.sqlite_transcript().rename(self.root / "weird?name#x.db")
        code, data = self.output(*self.extract(weird, agent="opencode", session="s1"))
        self.assertEqual((code, data["reason"]), (0, "ok"))
        self.assertIn("SQLITE-USER-MARKER", self.prompt())


class ResolutionTests(ExtractTestCase):
    def test_dsh_accepts_both_session_directory_shapes(self):
        home = self.root / "dsh-home"
        older = home / "sessions" / "-proj" / "abc" / "session.jsonl"
        newer = home / "sessions" / "-proj" / "session-abc" / "session.v4.jsonl.zstd"
        older.parent.mkdir(parents=True)
        newer.parent.mkdir(parents=True)
        older.write_text("", encoding="utf-8")
        newer.write_bytes(b"not decoded by resolution")
        os.utime(older, (1_000, 1_000))
        os.utime(newer, (2_000, 2_000))

        with mock.patch.dict(os.environ, {"DSH_HOME": str(home)}):
            self.assertEqual(extract._resolve_transcript("", "dsh", "abc"), newer)

    def test_claude_resolves_the_session_file_in_projects(self):
        home = self.root / "claude-home"
        path = home / ".claude" / "projects" / "-proj" / "sess-1.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text("", encoding="utf-8")

        with mock.patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(
                extract._resolve_transcript("", "claude", "sess-1"), path
            )


class PromptTests(ExtractTestCase):
    def test_existing_memories_are_included_by_name_and_description(self):
        self.write_config()
        self.set_response({"memories": []})
        store.write(
            self.project_dir,
            "known-note.md",
            "body that should not be in the prompt",
            name="known-note",
            description="a distinctive known description",
            if_version="new",
        )
        transcript = self.claude_transcript()
        code, data = self.output(*self.extract(transcript))
        self.assertEqual((code, data["reason"]), (0, "ok"))

        prompt = self.prompt()
        self.assertIn("known-note", prompt)
        self.assertIn("a distinctive known description", prompt)
        self.assertNotIn("body that should not be in the prompt", prompt)
        self.assertIn('"memories": []', prompt)


class WritingTests(ExtractTestCase):
    def test_valid_proposal_is_written_with_provenance_and_index(self):
        self.write_config()
        self.set_response(
            {
                "memories": [
                    {
                        "name": "Use-Pnpm",
                        "description": "the lockfile is pnpm\nuse pnpm not npm",
                        "type": "feedback",
                        "content": "Use pnpm because the lockfile is pnpm.",
                    }
                ]
            }
        )
        transcript = self.claude_transcript()
        code, data = self.output(
            *self.extract(transcript, agent="claude-code", session="session-abc")
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["written"], ["use-pnpm.md"])
        self.assertEqual((data["skipped"], data["refused"]), (0, 0))

        memory = store.load(self.project_dir / "use-pnpm.md")
        self.assertEqual(memory.name, "use-pnpm")
        self.assertEqual(memory.description, "the lockfile is pnpm use pnpm not npm")
        self.assertEqual(memory.type, "feedback")
        self.assertEqual(memory.meta["metadata"]["originSessionId"], "session-abc")
        self.assertEqual(memory.body, "Use pnpm because the lockfile is pnpm.")
        self.assertIn("use-pnpm.md", (self.project_dir / "MEMORY.md").read_text(encoding="utf-8"))

    def test_existing_file_is_skipped_but_others_are_written(self):
        self.write_config()
        store.write(
            self.project_dir,
            "already-here.md",
            "old body",
            name="already-here",
            description="old",
            if_version="new",
        )
        self.set_response(
            {
                "memories": [
                    {
                        "name": "already-here",
                        "description": "new description",
                        "type": "project",
                        "content": "new body",
                    },
                    {
                        "name": "fresh-note",
                        "description": "new",
                        "type": "project",
                        "content": "fresh body",
                    },
                ]
            }
        )
        code, data = self.output(*self.extract(self.claude_transcript()))
        self.assertEqual(code, 0)
        self.assertEqual(data["written"], ["fresh-note.md"])
        self.assertEqual(data["skipped"], 1)
        self.assertEqual((self.project_dir / "already-here.md").read_text().count("old body"), 1)

    def test_a_name_that_exists_in_global_is_not_duplicated_in_the_project(self):
        self.write_config()
        store.write(
            config.scope_dir("global", str(self.cwd)),
            "reply-short.md",
            "the original body",
            name="reply-short",
            description="user wants short answers",
            if_version="new",
        )
        self.set_response(
            {
                "memories": [
                    {
                        "name": "reply-short",
                        "description": "user wants short answers",
                        "type": "user",
                        "content": "a second copy of a global note",
                    }
                ]
            }
        )
        code, data = self.output(*self.extract(self.claude_transcript()))
        self.assertEqual(code, 0)
        self.assertEqual(data["written"], [])
        self.assertEqual(data["skipped"], 1)
        self.assertFalse((self.project_dir / "reply-short.md").exists())

    def test_secret_is_refused_and_the_rest_still_write(self):
        self.write_config()
        self.set_response(
            {
                "memories": [
                    {
                        "name": "leaky-note",
                        "description": "a key",
                        "type": "reference",
                        "content": "api_key: sk-" + "a" * 24,
                    },
                    {
                        "name": "safe-note",
                        "description": "safe",
                        "type": "user",
                        "content": "a safe durable note",
                    },
                ]
            }
        )
        code, data = self.output(*self.extract(self.claude_transcript()))
        self.assertEqual(code, 0)
        self.assertEqual(data["written"], ["safe-note.md"])
        self.assertEqual(data["refused"], 1)
        self.assertFalse((self.project_dir / "leaky-note.md").exists())

    def test_at_most_five_memories_are_written(self):
        self.write_config()
        self.set_response(
            {
                "memories": [
                    {
                        "name": "note-{}".format(number),
                        "description": "d",
                        "type": "project",
                        "content": "body {}".format(number),
                    }
                    for number in range(7)
                ]
            }
        )
        code, data = self.output(*self.extract(self.claude_transcript()))
        self.assertEqual(code, 0)
        self.assertEqual(
            data["written"], ["note-{}.md".format(number) for number in range(5)]
        )
        self.assertEqual(data["skipped"], 2)

    def test_unknown_type_falls_back_to_the_default(self):
        self.write_config()
        self.set_response(
            {
                "memories": [
                    {
                        "name": "odd-type",
                        "description": "d",
                        "type": "gotcha",
                        "content": "body",
                    }
                ]
            }
        )
        code, data = self.output(*self.extract(self.claude_transcript()))
        self.assertEqual(code, 0)
        self.assertEqual(data["written"], ["odd-type.md"])
        self.assertEqual(store.load(self.project_dir / "odd-type.md").type, store.DEFAULT_TYPE)


class ExtractChildTests(ExtractTestCase):
    """The model command must be told it is a child, so its hooks stay inert.

    An agent used as the extractor (`claude -p`) loads this repo's hooks. Marking
    the child is what stops the Stop hook from starting a second extraction, whose
    own Stop would start a third — a chain of paid model calls.
    """

    def test_the_command_receives_the_child_marker(self):
        self.write_config()
        self.set_response({"memories": []})
        self.extract(self.claude_transcript(), agent="claude-code", session="session-abc")
        marker = (self.root / "child_marker.txt").read_text(encoding="utf-8")
        self.assertEqual(marker, "1")

    def test_the_marker_is_not_leaked_into_this_process(self):
        self.write_config()
        self.set_response({"memories": []})
        self.extract(self.claude_transcript(), agent="claude-code", session="session-abc")
        self.assertIsNone(os.environ.get(config.EXTRACT_CHILD_ENV))


class TimeoutTests(ExtractTestCase):
    """One model call may take a while: `claude -p` on a real session took 18.7 s.

    The 20 s default is right for a small API call and wrong for an agent CLI, so
    `extract.timeout` overrides it — and a value that is nonsense must not turn
    the hook's detached pass into a hang.
    """

    def test_the_configured_timeout_aborts_a_slow_command(self):
        self.write_config(timeout=1)
        self.set_mode("sleep")
        code, data = self.output(
            *self.extract(self.claude_transcript(), agent="claude-code", session="session-abc")
        )
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], "command")
        self.assertIn("timed out after 1s", data["message"])

    def test_unset_timeout_is_the_default(self):
        self.assertEqual(extract.command_timeout({}), extract.COMMAND_TIMEOUT)

    def test_invalid_values_fall_back_to_the_default(self):
        for value in (0, -5, "soon", None):
            with self.subTest(value=value):
                self.assertEqual(
                    extract.command_timeout({"extract": {"timeout": value}}),
                    extract.COMMAND_TIMEOUT,
                )

    def test_large_values_are_clamped(self):
        self.assertEqual(
            extract.command_timeout({"extract": {"timeout": 100_000}}),
            extract.MAX_COMMAND_TIMEOUT,
        )


class FailureTests(ExtractTestCase):
    def test_invalid_json_exits_one_with_a_parse_error(self):
        self.write_config()
        self.set_mode("garbage")
        code, data = self.output(*self.extract(self.claude_transcript()))
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], "parse")
        self.assertNotIn("Traceback", data["message"])
        # Nothing is advanced, so a retry sees the same tail.
        self.assertFalse((self.root / ".state" / "extract.json").exists())

    def test_nonzero_exit_is_a_command_error(self):
        self.write_config()
        self.set_mode("fail")
        code, data = self.output(*self.extract(self.claude_transcript()))
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], "command")
        self.assertIn("3", data["message"])

    def test_missing_transcript_path_is_a_transcript_error(self):
        self.write_config()
        code, data = self.output(*self.extract(self.root / "nope.jsonl"))
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], "transcript")

    def test_transcript_is_optional_when_resolution_finds_nothing(self):
        self.write_config()
        code, data = self.output(
            *self.run_extract("--agent", "unknown", "--cwd", str(self.cwd))
        )
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], "transcript")


class IncrementalTests(ExtractTestCase):
    def test_corrupt_state_starts_from_zero(self):
        self.write_config()
        self.set_response({"memories": []})
        (self.root / ".state").mkdir()
        (self.root / ".state" / "extract.json").write_text("[1, 2, 3]", encoding="utf-8")
        transcript = self.claude_transcript()

        code, data = self.output(*self.extract(transcript))
        self.assertEqual((code, data["reason"]), (0, "ok"))
        self.assertEqual(self.calls(), 1)
        self.assertGreater(self.state()[str(transcript.resolve())]["pos"], 0)

    def test_shrunk_transcript_starts_over_at_zero(self):
        self.write_config()
        self.set_response({"memories": []})
        transcript = self.claude_transcript()

        self.output(*self.extract(transcript))
        first_offset = self.state()[str(transcript.resolve())]["pos"]
        self.assertGreater(first_offset, 0)

        rewritten = json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "REWRITTEN-MARKER " + "durable detail " * 20,
                },
            }
        )
        transcript.write_text(rewritten + "\n", encoding="utf-8")

        code, data = self.output(*self.extract(transcript))
        self.assertEqual((code, data["reason"]), (0, "ok"))
        self.assertEqual(self.calls(), 2)
        self.assertIn("REWRITTEN-MARKER", self.prompt())
        self.assertEqual(
            self.state()[str(transcript.resolve())]["pos"],
            transcript.stat().st_size,
        )

    def test_unchanged_transcript_is_not_sent_again(self):
        self.write_config()
        self.set_response({"memories": []})
        transcript = self.claude_transcript()

        code, first = self.output(*self.extract(transcript))
        self.assertEqual((code, first["reason"]), (0, "ok"))
        self.assertEqual(self.calls(), 1)
        state = self.state()
        key = str(transcript.resolve())
        offset = state[key]["pos"]
        self.assertGreater(offset, 0)

        code, second = self.output(*self.extract(transcript))
        self.assertEqual((code, second["reason"]), (0, "nothing new"))
        self.assertEqual(second["written"], [])
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.state()[key]["pos"], offset)

    def test_short_tail_accumulates_without_advancing(self):
        self.write_config()
        self.set_response({"memories": []})
        transcript = self.claude_transcript()

        self.output(*self.extract(transcript))
        key = str(transcript.resolve())
        offset = self.state()[key]["pos"]
        self.assertEqual(self.calls(), 1)

        short_line = json.dumps(
            {"type": "user", "message": {"role": "user", "content": "thanks"}}
        )
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(short_line + "\n")

        code, data = self.output(*self.extract(transcript))
        self.assertEqual((code, data["reason"]), (0, "nothing new"))
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.state()[key]["pos"], offset)

        long_line = json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "more durable detail " * 20},
            }
        )
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(long_line + "\n")

        code, data = self.output(*self.extract(transcript))
        self.assertEqual((code, data["reason"]), (0, "ok"))
        self.assertEqual(self.calls(), 2)
        self.assertGreater(self.state()[key]["pos"], offset)


if __name__ == "__main__":
    unittest.main()
