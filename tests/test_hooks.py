"""Tests for the Claude Code hook adapter (`mem hook <event>`).

Each test gets its own MEM_ROOT; config.root() reads the environment on every
call, so no cache has to be cleared between tests.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from memlib import cli, config, hooks, store

REPO = Path(__file__).resolve().parent.parent
HOOK_EVENTS = {
    "session-start": "SessionStart",
    "user-prompt": "UserPromptSubmit",
    "session-end": "SessionEnd",
}
CLI_EVENTS = {name: event for event, name in HOOK_EVENTS.items()}


class HookTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mem-hooks-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self._old_root = os.environ.get("MEM_ROOT")
        os.environ["MEM_ROOT"] = str(self.root)
        self.addCleanup(self._restore_root)
        self.cwd = self.root / "project"
        self.cwd.mkdir()

    def _restore_root(self):
        if self._old_root is None:
            os.environ.pop("MEM_ROOT", None)
        else:
            os.environ["MEM_ROOT"] = self._old_root

    # -- helpers ---------------------------------------------------------

    def write_config(self, text):
        (self.root / "config.toml").write_text(text, encoding="utf-8")

    def memory(self, scope, filename, name, description, body="body"):
        return store.write(
            config.scope_dir(scope, str(self.cwd)),
            filename,
            body,
            name=name,
            description=description,
            if_version="new",
        )

    def payload(self, event, **extra):
        data = {
            "session_id": "session-1",
            "cwd": str(self.cwd),
            "hook_event_name": HOOK_EVENTS[event],
            "transcript_path": str(self.root / "transcript.jsonl"),
        }
        data.update(extra)
        return data

    def run_hook(self, event, payload=None, raw=None):
        raw_text = raw if raw is not None else json.dumps(payload or {})
        stdout = io.StringIO()
        code = hooks.run(event, stdin=io.StringIO(raw_text), stdout=stdout)
        return code, stdout.getvalue()

    def output(self, event, payload=None, raw=None):
        """Run a hook that must emit exactly one JSON object; return its payload."""
        code, text = self.run_hook(event, payload, raw)
        self.assertEqual(code, 0)
        self.assertEqual(
            len(text.strip().splitlines()), 1, "stdout must be one JSON line: %r" % text
        )
        data = json.loads(text)
        self.assertEqual(set(data), {"hookSpecificOutput"})
        return data["hookSpecificOutput"]

    def state(self):
        path = self.root / ".state" / "state.json"
        return json.loads(path.read_text(encoding="utf-8"))


class SessionStartTests(HookTestCase):
    def test_injects_global_and_project_index(self):
        self.memory("global", "preference.md", "prefer-short", "user wants short answers")
        self.memory(
            "project",
            "pnpm.md",
            "pnpm-not-npm",
            "the lockfile is pnpm",
            body="BODY-MARKER not injected",
        )
        out = self.output("session-start", self.payload("session-start", source="startup"))
        context = out["additionalContext"]

        self.assertEqual(out["hookEventName"], "SessionStart")
        self.assertIn("# global memory", context)
        self.assertIn("# project memory", context)
        self.assertIn("Prefer short", context)
        self.assertIn("Pnpm not npm", context)
        self.assertNotIn("BODY-MARKER", context)
        self.assertEqual(context.count(hooks.DISCLAIMER), 1)

        global_dir = config.scope_dir("global", str(self.cwd))
        project_dir = config.scope_dir("project", str(self.cwd))
        self.assertIn("(%s)" % (cli._rel_prefix(global_dir) + "preference.md"), context)
        self.assertIn("(%s)" % (cli._rel_prefix(project_dir) + "pnpm.md"), context)

    def test_index_cap_from_config_is_enforced(self):
        self.write_config("[context]\nindex_max_lines = 3\n")
        for number in range(6):
            self.memory(
                "project", "m%d.md" % number, "note-%d" % number, "description %d" % number
            )
        context = self.output("session-start", self.payload("session-start"))[
            "additionalContext"
        ]
        body = context.split(hooks.DISCLAIMER + "\n\n", 1)[1]
        self.assertLessEqual(len(body.splitlines()), 4)  # 3 lines + truncation marker
        self.assertTrue(body.rstrip().endswith("(index truncated)"))

    def test_empty_store_prints_nothing_but_counts_the_session(self):
        code, text = self.run_hook("session-start", self.payload("session-start"))
        self.assertEqual((code, text), (0, ""))
        self.assertEqual(hooks.session_count(), 1)

    def test_missing_cwd_falls_back_without_crashing(self):
        code, text = self.run_hook(
            "session-start", {"session_id": "s", "hook_event_name": "SessionStart"}
        )
        self.assertEqual((code, text), (0, ""))
        self.assertEqual(hooks.session_count(), 1)

    def test_nonexistent_project_cwd_is_silent(self):
        code, text = self.run_hook(
            "session-start",
            self.payload("session-start", cwd=str(self.root / "does-not-exist")),
        )
        self.assertEqual((code, text), (0, ""))

    def test_oversize_content_is_capped(self):
        self.memory("project", "big.md", "big-note", "x" * 50_000)
        context = self.output("session-start", self.payload("session-start"))[
            "additionalContext"
        ]
        self.assertLessEqual(len(context), hooks.MAX_CONTEXT_CHARS)
        self.assertIn("truncated", context)
        self.assertNotIn("x" * 1_000, context)

    def test_many_memories_stay_under_the_char_cap(self):
        for number in range(120):
            self.memory(
                "project",
                "m%d.md" % number,
                "note-%d" % number,
                ("description %d " % number) + "detail " * 20,
            )
        context = self.output("session-start", self.payload("session-start"))[
            "additionalContext"
        ]
        self.assertLessEqual(len(context), hooks.MAX_CONTEXT_CHARS)

    def test_secret_in_a_handwritten_file_is_never_injected(self):
        self.memory("project", "safe.md", "safe-note", "nothing sensitive")
        directory = config.scope_dir("project", str(self.cwd))
        leaky = store.render(
            {
                "name": "leaky",
                "description": "api_key: sk-" + "a" * 24,
                "metadata": {"node_type": "memory", "type": "project"},
            },
            "body\n",
        )
        (directory / "leaky.md").write_text(leaky, encoding="utf-8")

        context = self.output("session-start", self.payload("session-start"))[
            "additionalContext"
        ]
        self.assertIn("Safe note", context)
        self.assertNotIn("leaky", context.lower())
        self.assertNotIn("sk-", context)


class UserPromptTests(HookTestCase):
    def test_silence_when_nothing_matches(self):
        self.write_config("[recall]\nselect_threshold = 1\ntop_k = 5\n")
        self.memory("project", "pnpm.md", "pnpm-not-npm", "the lockfile is pnpm")
        code, text = self.run_hook(
            "user-prompt", self.payload("user-prompt", prompt="kubernetes ingress")
        )
        self.assertEqual((code, text), (0, ""))

    def test_silence_below_select_threshold(self):
        self.memory("project", "pnpm.md", "pnpm-not-npm", "the lockfile is pnpm")
        code, text = self.run_hook(
            "user-prompt", self.payload("user-prompt", prompt="pnpm lockfile")
        )
        self.assertEqual((code, text), (0, ""))

    def test_recall_injects_matching_memories_once(self):
        self.write_config("[recall]\nselect_threshold = 1\ntop_k = 5\n")
        memory = self.memory(
            "global", "pnpm.md", "pnpm-not-npm", "the lockfile is pnpm; npm install breaks CI"
        )
        out = self.output(
            "user-prompt",
            self.payload("user-prompt", prompt="why does npm install break the lockfile?"),
        )
        context = out["additionalContext"]
        self.assertEqual(out["hookEventName"], "UserPromptSubmit")
        self.assertIn(memory.title, context)
        self.assertIn(str(memory.path), context)
        self.assertEqual(context.count(hooks.DISCLAIMER), 1)

    def test_same_note_in_both_scopes_is_injected_once(self):
        self.write_config("[recall]\nselect_threshold = 1\ntop_k = 8\n")
        self.memory("project", "same.md", "same-note", "shared description here")
        self.memory("global", "same.md", "same-note", "shared description here")
        context = self.output(
            "user-prompt",
            self.payload("user-prompt", prompt="shared description here"),
        )["additionalContext"]
        self.assertEqual(context.count("[Same note]"), 1)
        self.assertEqual(context.count(hooks.DISCLAIMER), 1)

    def test_oversize_recall_line_is_capped(self):
        self.write_config("[recall]\nselect_threshold = 1\ntop_k = 5\n")
        self.memory("project", "big.md", "big-note", "x" * 50_000)
        context = self.output(
            "user-prompt", self.payload("user-prompt", prompt="big note")
        )["additionalContext"]
        self.assertLessEqual(len(context), hooks.MAX_CONTEXT_CHARS)
        self.assertNotIn("x" * 1_000, context)

    def test_secret_in_recall_is_not_injected(self):
        self.write_config("[recall]\nselect_threshold = 1\ntop_k = 5\n")
        directory = config.scope_dir("project", str(self.cwd))
        directory.mkdir(parents=True, exist_ok=True)
        leaky = store.render(
            {
                "name": "leaky",
                "description": "api_key: sk-" + "a" * 24,
                "metadata": {"node_type": "memory", "type": "project"},
            },
            "body\n",
        )
        (directory / "leaky.md").write_text(leaky, encoding="utf-8")
        code, text = self.run_hook(
            "user-prompt", self.payload("user-prompt", prompt="api key leaky")
        )
        self.assertEqual((code, text), (0, ""))


class StateTests(HookTestCase):
    def test_counter_increments_across_runs(self):
        self.run_hook("session-start", self.payload("session-start", session_id="first"))
        self.run_hook("session-start", self.payload("session-start", session_id="second"))
        self.assertEqual(hooks.session_count(), 2)
        state = self.state()
        self.assertEqual(state["sessions"], 2)
        self.assertEqual(state["last_session_id"], "second")
        self.assertIn("updated", state)

    def test_reset_sessions_is_the_only_reset(self):
        self.run_hook("session-start", self.payload("session-start"))
        self.assertEqual(hooks.reset_sessions(), 0)
        self.assertEqual(hooks.session_count(), 0)
        self.run_hook("session-start", self.payload("session-start"))
        self.assertEqual(hooks.session_count(), 1)

    def test_corrupt_state_file_is_tolerated(self):
        state_dir = self.root / ".state"
        state_dir.mkdir()
        state_file = state_dir / "state.json"
        for garbage in ("{not json", "[1, 2, 3]", '"a string"', '{"sessions": "many"}'):
            with self.subTest(garbage=garbage):
                state_file.write_text(garbage, encoding="utf-8")
                self.assertEqual(hooks.session_count(), 0)
                code, text = self.run_hook("session-start", self.payload("session-start"))
                self.assertEqual((code, text), (0, ""))
                self.assertEqual(hooks.session_count(), 1)
                self.assertEqual(self.state()["sessions"], 1)

    def test_session_end_flushes_without_bumping(self):
        self.run_hook("session-start", self.payload("session-start"))
        code, text = self.run_hook(
            "session-end", self.payload("session-end", reason="logout")
        )
        self.assertEqual((code, text), (0, ""))
        state = self.state()
        self.assertEqual(state["sessions"], 1)
        self.assertEqual(state["last_end_reason"], "logout")

    def test_session_end_creates_state_when_missing(self):
        code, text = self.run_hook("session-end", self.payload("session-end", reason="other"))
        self.assertEqual((code, text), (0, ""))
        self.assertEqual(self.state()["sessions"], 0)


class MalformedInputTests(HookTestCase):
    def test_malformed_stdin_exits_zero_and_stays_silent(self):
        # Seven session starts reach the dream threshold, so the trigger fires
        # even here; the test is about input handling, not about launching it.
        with mock.patch("memlib.hooks.subprocess.Popen"):
            for raw in ("", "   ", "not json", "[1, 2]", "null", '"string"', '{"cwd": 5}'):
                for event in ("session-start", "user-prompt", "session-end"):
                    with self.subTest(raw=raw, event=event):
                        code, text = self.run_hook(event, raw=raw)
                        self.assertEqual(code, 0)
                        self.assertEqual(text, "")

    def test_unreadable_stdin_exits_zero(self):
        class BrokenStdin:
            def read(self):
                raise OSError("stdin closed")

        self.assertEqual(
            hooks.run("session-start", stdin=BrokenStdin(), stdout=io.StringIO()), 0
        )

    def test_broken_stdout_exits_zero(self):
        class BrokenStdout:
            def write(self, text):
                raise OSError("broken pipe")

        self.memory("project", "pnpm.md", "pnpm-not-npm", "the lockfile is pnpm")
        code = hooks.run(
            "session-start",
            stdin=io.StringIO(json.dumps(self.payload("session-start"))),
            stdout=BrokenStdout(),
        )
        self.assertEqual(code, 0)

    def test_unknown_event_exits_zero(self):
        code, text = self.run_hook("bogus", raw="{}")
        self.assertEqual((code, text), (0, ""))

    def test_truncate_never_exceeds_the_hard_limit(self):
        # One huge line with no newline: the only possible cut is mid-line,
        # but the result must still fit.
        self.assertEqual(len(hooks._truncate("x" * 50_000)), hooks.MAX_CONTEXT_CHARS)
        many_lines = hooks._truncate("a\n" * 6_000)
        self.assertLessEqual(len(many_lines), hooks.MAX_CONTEXT_CHARS)
        self.assertTrue(many_lines.endswith("(truncated)"))
        self.assertEqual(hooks._truncate("short"), "short")


class SubprocessTests(HookTestCase):
    """One real `bin/mem` run: stdout must be pure JSON, exit code 0."""

    def _run(self, event, stdin):
        env = dict(os.environ)
        env["MEM_ROOT"] = str(self.root)
        return subprocess.run(
            [str(REPO / "bin" / "mem"), "hook", event],
            input=stdin,
            text=True,
            capture_output=True,
            env=env,
            timeout=30,
        )

    def test_session_start_output_is_pure_json(self):
        self.memory("project", "pnpm.md", "pnpm-not-npm", "the lockfile is pnpm")
        proc = self._run("session-start", json.dumps(self.payload("session-start")))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(proc.stdout.strip().splitlines()), 1)
        self.assertTrue(proc.stdout.startswith("{"))
        data = json.loads(proc.stdout)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("pnpm", data["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.state()["sessions"], 1)  # the subprocess bumped it

    def test_malformed_stdin_is_silent_and_exits_zero(self):
        proc = self._run("session-start", "{not json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("Traceback", proc.stderr)


class InstallFragmentTests(unittest.TestCase):
    def test_fragment_is_valid_json_with_all_four_hooks(self):
        path = REPO / "install" / "claude-code.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertIs(data["autoMemoryEnabled"], False)
        self.assertEqual(set(data["hooks"]), set(HOOK_EVENTS.values()))
        for event in HOOK_EVENTS.values():
            groups = data["hooks"][event]
            self.assertEqual(len(groups), 1)
            hook = groups[0]["hooks"][0]
            self.assertEqual(hook["type"], "command")
            self.assertIn("hook %s" % CLI_EVENTS[event], hook["command"])


if __name__ == "__main__":
    unittest.main()
