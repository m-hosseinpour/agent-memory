"""Tests for the CLI contract.

The CLI is the scripting surface: every command prints one JSON object (or text
for `index`), and the exit code says what happened. The MCP server shares the
same store/index/dream code, so a change here is a change there too.
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
from unittest import mock

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

    def test_usage_error_is_exit_1_with_a_json_error(self):
        # argparse owns the choice list, so this never reaches the store check.
        # Exit 2 is reserved for a version conflict; a typo must not look like
        # one (and must not print an empty stdout).
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), self.assertRaises(SystemExit) as ctx:
            cli.main(
                [
                    "write", "--scope", "project", "--cwd", str(self.cwd),
                    "--path", "odd.md", "--content", "body",
                    "--if-version", "new", "--type", "gotcha",
                ]
            )
        self.assertEqual(ctx.exception.code, 1)
        data = json.loads(stdout.getvalue())
        self.assertEqual((data["ok"], data["error"]), (False, "invalid"))

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

    def test_rewrite_with_only_content_keeps_the_note(self):
        self.create(
            "u.md", "x", name="prefers-vim", description="prefers vim", type="user"
        )
        version = store.load(self.project_dir() / "u.md").version
        code, data = self.json_cli(
            "write", "--scope", "project", "--cwd", str(self.cwd), "--path", "u.md",
            "--content", "x, fixed typo", "--if-version", version,
        )
        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        memory = store.load(self.project_dir() / "u.md")
        self.assertEqual(
            (memory.name, memory.description, memory.type),
            ("prefers-vim", "prefers vim", "user"),
        )
        _, text = self.run_cli("index", "--cwd", str(self.cwd))
        self.assertIn("## user (1)", text)
        self.assertIn("- [Prefers vim](u.md) — prefers vim", text)

    def test_force_rewrite_with_only_content_keeps_the_note(self):
        self.create(
            "u.md", "x", name="prefers-vim", description="prefers vim", type="user"
        )
        code, data = self.json_cli(
            "write", "--scope", "project", "--cwd", str(self.cwd),
            "--path", "u.md", "--content", "forced", "--force",
        )
        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        memory = store.load(self.project_dir() / "u.md")
        self.assertEqual(
            (memory.name, memory.description, memory.type),
            ("prefers-vim", "prefers vim", "user"),
        )

    def test_explicit_flags_still_change_the_note(self):
        self.create(
            "u.md", "x", name="prefers-vim", description="prefers vim", type="user"
        )
        version = store.load(self.project_dir() / "u.md").version
        code, _ = self.json_cli(
            "write", "--scope", "project", "--cwd", str(self.cwd), "--path", "u.md",
            "--content", "x", "--if-version", version,
            "--name", "edits-in-vim", "--description", "edits in vim", "--type", "feedback",
        )
        self.assertEqual(code, 0)
        memory = store.load(self.project_dir() / "u.md")
        self.assertEqual(
            (memory.name, memory.description, memory.type),
            ("edits-in-vim", "edits in vim", "feedback"),
        )


class ReadListForgetContractTests(CliTestCase):
    def test_read_of_a_missing_memory_is_exit_1(self):
        code, data = self.json_cli(
            "read", "--scope", "project", "--cwd", str(self.cwd), "--path", "nope.md"
        )
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], "invalid")

    def test_read_cannot_escape_the_scope(self):
        self.create("n.md")
        secret = self.root / "secret.txt"
        secret.write_text("SECRET-DATA", encoding="utf-8")
        for path in ("../../secret.txt", str(secret)):
            code, data = self.json_cli(
                "read", "--scope", "project", "--cwd", str(self.cwd), "--path", path
            )
            self.assertEqual(code, 1, path)
            self.assertEqual(data["error"], "invalid", path)
            self.assertNotIn("SECRET-DATA", json.dumps(data), path)

    def test_read_refuses_the_generated_index(self):
        self.create("n.md")
        self.assertTrue((self.project_dir() / "MEMORY.md").is_file())
        code, data = self.json_cli(
            "read", "--scope", "project", "--cwd", str(self.cwd), "--path", "MEMORY.md"
        )
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], "invalid")

    def test_read_survives_non_utf8_bytes(self):
        self.create("n.md")
        path = self.project_dir() / "n.md"
        path.write_bytes(b'---\nname: "n"\ndescription: "d"\n---\n\nplain \xff body\n')
        code, data = self.json_cli(
            "read", "--scope", "project", "--cwd", str(self.cwd), "--path", "n.md"
        )
        self.assertEqual(code, 0, data)
        self.assertIn("plain", data["content"])
        self.assertIn("\ufffd", data["content"])
        self.assertIn("\ufffd", data["raw"])

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


class IndexTests(CliTestCase):
    def test_index_prints_text_not_json(self):
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

    def test_index_lists_every_note(self):
        # The index is the only retrieval path, so it is never truncated.
        # Distinct words: descriptions that share every token would be merged
        # by the consolidation that runs after each write.
        words = (
            "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo "
            "lima mike november oscar papa quebec romeo sierra tango uniform "
            "victor whiskey xray yankee zulu"
        ).split()
        for word in words:
            self.create(f"note-{word}.md", description=f"{word} preference")
        _, text = self.run_cli("index", "--cwd", str(self.cwd))
        for word in words:
            self.assertIn(f"note-{word}.md", text)
        self.assertNotIn("truncated", text)


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


class ConsolidationOnChangeTests(CliTestCase):
    """A change is consolidated before the command returns."""

    def test_same_name_notes_are_reduced_to_the_newest(self):
        self.create("a.md", "first body", name="shared")
        self.create("b.md", "second body", name="shared")
        self.assertFalse((self.project_dir() / "a.md").exists())
        self.assertTrue((self.project_dir() / "b.md").exists())
        self.assertNotIn("a.md", (self.project_dir() / "MEMORY.md").read_text())

    def test_duplicate_descriptions_are_merged(self):
        description = "the lockfile is pnpm and npm install breaks CI"
        self.create("a.md", "first detail", description=description)
        self.create("b.md", "second detail", description=description)
        self.assertFalse((self.project_dir() / "a.md").exists())
        body = store.load(self.project_dir() / "b.md").body
        self.assertIn("second detail", body)
        self.assertIn("first detail", body)

    def test_forget_consolidates(self):
        self.create("a.md", name="shared")
        self.create("b.md", name="shared")
        code, data = self.json_cli(
            "forget", "--scope", "project", "--cwd", str(self.cwd), "--path", "b.md"
        )
        self.assertEqual(code, 0)
        self.assertEqual(store.list_memories(self.project_dir()), [])

    def test_dream_is_a_manual_full_pass(self):
        self.create("a.md", name="shared")
        self.create("b.md", name="shared")
        code, data = self.json_cli("dream", "--cwd", str(self.cwd))
        self.assertEqual(code, 0)
        self.assertTrue(data["ran"])
        self.assertEqual(data["scopes"], 2)
        self.assertEqual(set(data), {"ok", "ran", "scopes", "merged", "replaced", "index_lines"})

    def test_dream_repairs_an_index_line_left_behind(self):
        self.create("a.md")
        (self.project_dir() / "a.md").unlink()
        code, data = self.json_cli("dream", "--cwd", str(self.cwd))
        self.assertEqual(code, 0)
        self.assertEqual(data["index_lines"], 1)
        self.assertNotIn("a.md", (self.project_dir() / "MEMORY.md").read_text())

    def test_the_version_returned_is_the_one_on_disk(self):
        description = "the lockfile is pnpm and npm install breaks CI"
        self.create("a.md", "first detail", description=description)
        written = self.create("b.md", "second detail", description=description)
        on_disk = store.load(self.project_dir() / "b.md")
        # The pass merged a.md's body into b.md, so the version from before the
        # pass would no longer be accepted by the next write.
        self.assertEqual(written["version"], on_disk.version)
        self.assertIn("first detail", on_disk.body)

    def test_a_tied_timestamp_keeps_the_note_just_written(self):
        # Same name and the same millisecond: the tiebreak sorts by file name,
        # and "a.md" < "z.md" would delete the file that was just written.
        with mock.patch("memlib.store.now_iso", return_value="2026-10-02T00:00:00.000Z"):
            self.create("z.md", "old body", name="shared")
            written = self.create("a.md", "new body", name="shared")
        self.assertTrue((self.project_dir() / "a.md").exists())
        self.assertFalse((self.project_dir() / "z.md").exists())
        self.assertEqual(written["version"], store.load(self.project_dir() / "a.md").version)


class CliErrorShapeTests(CliTestCase):
    """Ordinary failures are one JSON object with an exit code, not a traceback."""

    def test_a_missing_content_file_is_an_io_error(self):
        code, data = self.json_cli(
            *self.write_args("x.md", content_file="/nonexistent/body.txt", if_version="new")
        )
        self.assertEqual(code, 1)
        self.assertEqual((data["ok"], data["error"]), (False, "io"))

    def test_a_non_utf8_content_file_is_invalid(self):
        body = self.root / "body.bin"
        body.write_bytes(b"plain \xff body")
        code, data = self.json_cli(
            *self.write_args("x.md", content_file=body, if_version="new")
        )
        self.assertEqual(code, 1)
        self.assertEqual((data["ok"], data["error"]), (False, "invalid"))
        self.assertFalse((self.project_dir() / "x.md").exists())

    def test_a_dotdot_path_is_refused_as_invalid(self):
        code, data = self.json_cli(
            "forget", "--scope", "project", "--cwd", str(self.cwd), "--path", ".."
        )
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], "invalid")

    def test_a_missing_memory_on_read_is_invalid(self):
        code, data = self.json_cli(
            "read", "--scope", "project", "--cwd", str(self.cwd), "--path", "nope.md"
        )
        self.assertEqual(code, 1)
        self.assertEqual(data["error"], "invalid")


if __name__ == "__main__":
    unittest.main()
