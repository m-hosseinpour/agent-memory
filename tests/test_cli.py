"""Tests for the CLI contract every adapter depends on.

Adapters (hooks, MCP, editor plugins) do not import `memlib`; they parse the
JSON these commands print and branch on the exit code. So the shapes asserted
here are a compatibility surface: changing one silently breaks an adapter.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from memlib import cli, config, store

WRITE_KEYS = {"ok", "path", "version", "modified"}


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mem-cli-"))
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

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(args))
        return code, stdout.getvalue()

    def json_cli(self, *args):
        """Run a command that must print exactly one JSON object."""
        code, text = self.run_cli(*args)
        self.assertEqual(len(text.strip().splitlines()), 1, "stdout: {!r}".format(text))
        return code, json.loads(text)

    def write_args(self, path="note.md", scope="project", **extra):
        options = {
            "scope": scope,
            "cwd": str(self.cwd),
            "path": path,
            "name": path[:-3],
            "description": "a description",
        }
        options.update(extra)
        args = ["write"]
        for key, value in options.items():
            args.extend(["--" + key.replace("_", "-"), str(value)])
        return args

    def create(self, path="note.md", body="body", scope="project", **extra):
        code, data = self.json_cli(
            *self.write_args(path, scope, content=body, if_version="new", **extra)
        )
        self.assertEqual(code, 0)
        return data

    def project_dir(self):
        return config.scope_dir("project", str(self.cwd))

    def write_config(self, text):
        (self.root / "config.toml").write_text(text, encoding="utf-8")


class WriteContractTests(CliTestCase):
    def test_create_prints_the_documented_keys(self):
        code, data = self.json_cli(
            *self.write_args("pnpm.md", content="use pnpm", if_version="new")
        )
        self.assertEqual(code, 0)
        self.assertEqual(set(data), WRITE_KEYS)
        self.assertTrue(data["ok"])
        self.assertEqual(data["path"], str(self.project_dir() / "pnpm.md"))
        self.assertEqual(len(data["version"]), 12)
        self.assertTrue((self.project_dir() / "MEMORY.md").is_file())

    def test_read_returns_body_version_and_raw(self):
        created = self.create("pnpm.md", "use pnpm")
        code, data = self.json_cli(
            "read", "--scope", "project", "--cwd", str(self.cwd), "--path", "pnpm.md"
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            set(data), {"path", "name", "description", "type", "modified", "version", "content", "raw"}
        )
        self.assertEqual(data["content"], "use pnpm")
        self.assertEqual(data["version"], created["version"])
        self.assertIn("use pnpm", data["raw"])

    def test_conflict_returns_2_and_the_current_content(self):
        first = self.create("pnpm.md", "one")
        current = store.write(
            self.project_dir(), "pnpm.md", "two", if_version=first["version"]
        )
        code, data = self.json_cli(
            *self.write_args("pnpm.md", content="three", if_version=first["version"])
        )
        self.assertEqual(code, 2)
        self.assertEqual(set(data), {"ok", "error", "message", "current"})
        self.assertEqual(data["error"], "conflict")
        self.assertFalse(data["ok"])
        self.assertEqual(data["current"]["version"], current.version)
        self.assertEqual(data["current"]["content"], "two")
        self.assertIn("two", data["current"]["raw"])

    def test_new_on_an_existing_file_is_a_conflict(self):
        self.create("pnpm.md")
        code, data = self.json_cli(*self.write_args("pnpm.md", content="body", if_version="new"))
        self.assertEqual(code, 2)
        self.assertEqual(data["current"]["content"], "body")

    def test_missing_if_version_is_exit_1(self):
        code, data = self.json_cli(*self.write_args("pnpm.md", content="body"))
        self.assertEqual(code, 1)
        self.assertEqual((data["ok"], data["error"]), (False, "invalid"))

    def test_secret_is_exit_3(self):
        code, data = self.json_cli(
            *self.write_args("leak.md", content="api_key: sk-" + "a" * 24, if_version="new")
        )
        self.assertEqual(code, 3)
        self.assertEqual((data["ok"], data["error"]), (False, "secret"))
        self.assertFalse((self.project_dir() / "leak.md").exists())

    def test_unknown_type_is_a_usage_error(self):
        # argparse owns the choice list, so this never reaches the store check.
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli(
                *self.write_args("odd.md", content="body", if_version="new", type="gotcha")
            )
        self.assertEqual(ctx.exception.code, 2)

    def test_bad_filename_is_exit_1(self):
        code, data = self.json_cli(
            *self.write_args("bad.txt", content="body", if_version="new")
        )
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], "invalid")

    def test_content_file(self):
        body = self.root / "body.txt"
        body.write_text("from a file", encoding="utf-8")
        code, data = self.json_cli(
            *self.write_args("file.md", content_file=body, if_version="new")
        )
        self.assertEqual(code, 0)
        self.assertEqual(store.load(self.project_dir() / "file.md").body, "from a file")


class ReadListForgetContractTests(CliTestCase):
    def test_read_of_a_missing_memory_is_exit_1(self):
        code, data = self.json_cli(
            "read", "--scope", "project", "--cwd", str(self.cwd), "--path", "nope.md"
        )
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], "invalid")

    def test_list_shape(self):
        self.create("a.md", "body", description="first")
        code, data = self.json_cli("list", "--scope", "project", "--cwd", str(self.cwd))
        self.assertEqual(code, 0)
        self.assertEqual(set(data), {"directory", "count", "memories"})
        self.assertEqual(data["count"], 1)
        self.assertEqual(
            set(data["memories"][0]),
            {"file", "name", "description", "type", "modified", "version"},
        )
        self.assertEqual(data["memories"][0]["file"], "a.md")

    def test_forget_shape_and_index_update(self):
        self.create("a.md")
        code, data = self.json_cli(
            "forget", "--scope", "project", "--cwd", str(self.cwd), "--path", "a.md"
        )
        self.assertEqual(code, 0)
        self.assertEqual(set(data), {"ok", "removed", "path"})
        self.assertTrue(data["removed"])
        self.assertNotIn("a.md", (self.project_dir() / "MEMORY.md").read_text())

    def test_forget_of_a_missing_file_is_not_an_error(self):
        code, data = self.json_cli(
            "forget", "--scope", "project", "--cwd", str(self.cwd), "--path", "nope.md"
        )
        self.assertEqual(code, 0)
        self.assertFalse(data["removed"])


class IndexAndRecallTests(CliTestCase):
    def test_index_prints_prompt_text_not_json(self):
        self.create("pnpm.md", description="the lockfile is pnpm")
        self.create("pref.md", description="short answers", scope="global")
        code, text = self.run_cli("index", "--cwd", str(self.cwd))
        self.assertEqual(code, 0)
        self.assertIn("# global memory", text)
        self.assertIn("# project memory", text)
        self.assertIn("pnpm.md", text)
        with self.assertRaises(ValueError):
            json.loads(text)

    def test_empty_index_prints_nothing(self):
        code, text = self.run_cli("index", "--cwd", str(self.cwd))
        self.assertEqual((code, text), (0, ""))

    def test_recall_is_silent_below_the_threshold(self):
        self.create("pnpm.md", description="the lockfile is pnpm")
        code, text = self.run_cli("recall", "lockfile", "--cwd", str(self.cwd))
        self.assertEqual((code, text), (0, ""))

    def test_recall_prints_reference_lines_above_the_threshold(self):
        self.write_config("[recall]\nselect_threshold = 1\ntop_k = 3\n")
        self.create("pnpm.md", description="the lockfile is pnpm")
        self.create("kde.md", description="chromium poisons the fontconfig cache")
        code, text = self.run_cli("recall", "pnpm lockfile", "--cwd", str(self.cwd))
        self.assertEqual(code, 0)
        self.assertEqual(len(text.strip().splitlines()), 1)
        self.assertIn("[Pnpm](", text)
        self.assertIn(str(self.project_dir() / "pnpm.md"), text)


class RootOverrideTests(CliTestCase):
    def test_root_flag_moves_everything(self):
        other = self.root / "elsewhere"
        code, text = self.run_cli(
            "--root",
            str(other),
            "write",
            "--scope",
            "project",
            "--cwd",
            str(self.cwd),
            "--path",
            "x.md",
            "--content",
            "body",
            "--if-version",
            "new",
        )
        self.assertEqual(code, 0)
        path = json.loads(text)["path"]
        self.assertTrue(path.startswith(str(other)), path)
        self.assertTrue(Path(path).is_file())


class AdapterEntryPointTests(CliTestCase):
    def test_extract_is_quiet_when_disabled_even_without_a_transcript(self):
        code, data = self.json_cli("extract")
        self.assertEqual(code, 0)
        self.assertEqual(data["reason"], "disabled")

    def test_dream_below_the_threshold_is_a_no_op_json(self):
        code, data = self.json_cli("dream", "--cwd", str(self.cwd))
        self.assertEqual(code, 0)
        self.assertFalse(data["ran"])
        self.assertEqual(data["reason"], "below threshold")

    def test_dream_with_force_runs(self):
        code, data = self.json_cli("dream", "--cwd", str(self.cwd), "--force")
        self.assertEqual(code, 0)
        self.assertTrue(data["ran"])


if __name__ == "__main__":
    unittest.main()
