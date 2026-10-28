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
import re
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

    def test_a_line_break_in_the_description_is_invalid(self):
        code, data = self.json_cli(
            *self.write_args(
                "note.md", content="body", if_version="new", description="one\ntwo"
            )
        )
        self.assertEqual(code, 1)
        self.assertEqual((data["ok"], data["error"]), (False, "invalid"))
        # Refused before the scope directory is created, not just before the file.
        self.assertFalse(self.project_dir().exists())

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


class MigrateTests(CliTestCase):
    """`mem migrate` imports Claude Code's per-project memory notes."""

    def claude_projects(self):
        return self.root / "claude" / "projects"

    @staticmethod
    def claude_slug(cwd):
        # Claude Code's directory name, not the store's: every non-alphanumeric
        # becomes "-", so a dot is lost and cannot be decoded back.
        return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))

    def memory_dir(self, cwd=None):
        cwd = Path(cwd) if cwd is not None else self.cwd
        project = self.claude_projects() / self.claude_slug(cwd)
        memory = project / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        (project / "session.jsonl").write_text(
            json.dumps({"type": "summary", "cwd": str(cwd)}) + "\n", encoding="utf-8"
        )
        return memory

    def note(self, memory, filename="note.md", *, name=None, description="a description",
             body="body", type_="project"):
        name = name or filename[:-3]
        text = (
            "---\n"
            f"name: {name}\n"
            f"description: {json.dumps(description)}\n"
            "metadata:\n"
            "  node_type: memory\n"
            f"  type: {type_}\n"
            "  originSessionId: 507e6cf4-4818-4f6e-83ad-66883efd3283\n"
            "---\n\n"
            f"{body}\n"
        )
        path = memory / filename
        path.write_text(text, encoding="utf-8")
        return path

    def migrate(self, *args):
        return self.json_cli("migrate", "--from", str(self.claude_projects()), *args)

    def test_notes_land_in_the_matching_project_scope(self):
        memory = self.memory_dir()
        self.note(memory, "pnpm-not-npm.md", body="The lockfile is pnpm.")
        # The source index is Claude Code's; the store generates its own.
        (memory / "MEMORY.md").write_text("- [Pnpm](pnpm-not-npm.md) — stale\n", encoding="utf-8")

        code, report = self.migrate()

        self.assertEqual(code, 0)
        self.assertEqual((report["projects"], report["written"]), (1, 1))
        loaded = store.load(self.project_dir() / "pnpm-not-npm.md")
        self.assertEqual(
            (loaded.name, loaded.description, loaded.type, loaded.body),
            ("pnpm-not-npm", "a description", "project", "The lockfile is pnpm."),
        )
        self.assertNotIn("originSessionId", loaded.meta.get("metadata", {}))
        index_text = (self.project_dir() / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("pnpm-not-npm.md", index_text)
        self.assertNotIn("stale", index_text)

    def test_global_promotes_only_the_listed_types(self):
        memory = self.memory_dir()
        self.note(memory, "style.md", name="style", type_="user")
        self.note(memory, "repo.md", name="repo", type_="project")

        code, report = self.migrate("--global", "user")

        self.assertEqual(code, 0)
        self.assertEqual(report["written"], 2)
        global_dir = config.scope_dir("global", str(self.cwd))
        self.assertTrue((global_dir / "style.md").is_file())
        self.assertFalse((self.project_dir() / "style.md").exists())
        self.assertTrue((self.project_dir() / "repo.md").is_file())

    def test_an_identical_note_is_not_written_again(self):
        memory = self.memory_dir()
        self.note(memory, "a.md", body="first body")
        self.migrate()

        code, report = self.migrate()

        self.assertEqual(code, 0)
        self.assertEqual((report["written"], report["exists"]), (0, 1))
        self.assertEqual(report["removed"], 0)

    def test_a_changed_source_note_is_a_collision(self):
        memory = self.memory_dir()
        self.note(memory, "a.md", body="first body")
        self.migrate()
        self.note(memory, "a.md", body="changed in Claude Code")

        code, report = self.migrate()

        self.assertEqual((code, report["collision"]), (1, 1))
        self.assertEqual(store.load(self.project_dir() / "a.md").body, "first body")

    def test_dry_run_reports_without_writing(self):
        self.note(self.memory_dir(), "a.md")

        code, report = self.migrate("--dry-run")

        self.assertEqual(code, 0)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["would_write"], 1)
        self.assertFalse(self.project_dir().exists())

    def test_a_secret_is_refused_per_note(self):
        memory = self.memory_dir()
        self.note(memory, "fine.md", body="nothing sensitive")
        self.note(memory, "bad.md", body="token: sk-" + "a" * 24)

        code, report = self.migrate()

        self.assertEqual((code, report["written"], report["refused"]), (3, 1, 1))
        self.assertFalse((self.project_dir() / "bad.md").exists())

    def test_an_unknown_type_is_reported_not_misplaced(self):
        self.note(self.memory_dir(), "odd.md", type_="made-up")

        code, report = self.migrate()

        self.assertEqual((code, report["invalid"]), (1, 1))
        self.assertFalse(self.project_dir().exists())

    def test_a_memory_directory_without_cwd_is_an_error(self):
        memory = self.memory_dir()
        self.note(memory, "a.md")

        code, report = self.json_cli("migrate", "--from", str(memory))

        self.assertEqual((code, report["error"]), (1, "invalid"))

    def test_from_may_be_one_memory_directory_with_cwd(self):
        memory = self.memory_dir()
        self.note(memory, "a.md")
        code, report = self.json_cli(
            "migrate", "--from", str(memory), "--cwd", str(self.cwd)
        )
        self.assertEqual((code, report["written"]), (0, 1))
        self.assertTrue((self.project_dir() / "a.md").is_file())

    def test_default_source_honors_claude_config_dir(self):
        self.note(self.memory_dir(), "a.md")
        with mock.patch.dict(
            os.environ, {"CLAUDE_CONFIG_DIR": str(self.root / "claude")}
        ):
            code, report = self.json_cli("migrate")
        self.assertEqual((code, report["written"]), (0, 1))

    def test_an_unknown_global_type_is_invalid(self):
        code, report = self.migrate("--global", "made-up")
        self.assertEqual((code, report["error"]), (1, "invalid"))

    def test_a_second_note_with_the_same_name_is_not_written(self):
        memory = self.memory_dir()
        self.note(memory, "a.md", name="shared")
        self.note(memory, "z.md", name="shared")

        code, report = self.migrate()

        self.assertEqual(code, 0)
        self.assertEqual(
            (report["written"], report["exists"], report["replaced"]), (1, 1, 0)
        )
        self.assertTrue((self.project_dir() / "a.md").is_file())
        self.assertFalse((self.project_dir() / "z.md").exists())

    def test_a_second_note_with_a_duplicate_description_is_not_written(self):
        memory = self.memory_dir()
        self.note(memory, "a.md", name="a-note", description="the lockfile is pnpm")
        self.note(memory, "z.md", name="z-note", description="the lockfile is pnpm")

        code, report = self.migrate()

        self.assertEqual(code, 0)
        self.assertEqual(
            (report["written"], report["exists"], report["merged"]), (1, 1, 0)
        )
        self.assertFalse((self.project_dir() / "z.md").exists())

    def test_a_rerun_does_not_reimport_a_note_the_first_run_skipped(self):
        memory = self.memory_dir()
        self.note(memory, "a.md", name="shared", body="a body")
        self.note(memory, "z.md", name="shared", body="z body")

        first_code, first = self.migrate()
        before = {p.name for p in self.project_dir().glob("*.md")}
        second_code, second = self.migrate()
        after = {p.name for p in self.project_dir().glob("*.md")}

        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(first["written"], 1)
        self.assertEqual((second["written"], second["exists"]), (0, 2))
        self.assertEqual(before, after)
        self.assertEqual(store.load(self.project_dir() / "a.md").body, "a body")

    def test_a_dotted_cwd_uses_the_store_slug_not_claudes(self):
        dotted = self.root / "my.project"
        dotted.mkdir()
        memory = self.memory_dir(dotted)
        self.note(memory, "a.md")

        code, report = self.migrate()

        self.assertEqual(code, 0)
        self.assertTrue((config.scope_dir("project", str(dotted)) / "a.md").is_file())
        # Claude Code's own directory name would be "...-my-project"; a note
        # there would never be read by `mem --cwd /.../my.project`.
        self.assertFalse(
            (config.root() / "projects" / self.claude_slug(dotted) / "a.md").exists()
        )

    def test_cwd_finds_the_project_through_its_recorded_cwd(self):
        dotted = self.root / "my.project"
        dotted.mkdir()
        memory = self.memory_dir(dotted)
        self.note(memory, "a.md")

        code, report = self.json_cli(
            "migrate",
            "--from",
            str(self.claude_projects()),
            "--cwd",
            str(dotted),
        )

        self.assertEqual(code, 0)
        self.assertEqual(report["written"], 1)
        self.assertTrue((config.scope_dir("project", str(dotted)) / "a.md").is_file())

    def test_a_project_without_a_recorded_cwd_is_invalid(self):
        memory = self.memory_dir()
        self.note(memory, "a.md")
        (memory.parent / "session.jsonl").unlink()

        code, report = self.migrate()

        self.assertEqual((code, report["invalid"]), (1, 1))
        self.assertEqual(report["written"], 0)
        self.assertFalse(self.project_dir().exists())

    def test_cwd_falls_back_to_claudes_directory_name(self):
        dotted = self.root / "my.project"
        dotted.mkdir()
        memory = self.memory_dir(dotted)
        self.note(memory, "a.md")
        (memory.parent / "session.jsonl").unlink()

        code, report = self.json_cli(
            "migrate",
            "--from",
            str(self.claude_projects()),
            "--cwd",
            str(dotted),
        )

        self.assertEqual((code, report["written"]), (0, 1))
        self.assertTrue((config.scope_dir("project", str(dotted)) / "a.md").is_file())

    def test_dry_run_and_the_real_run_agree_on_an_oversized_note(self):
        memory = self.memory_dir()
        self.note(memory, "big.md", body="x" * (store.MAX_BODY_BYTES + 1))

        code, report = self.migrate("--dry-run")
        self.assertEqual((code, report["invalid"], report["would_write"]), (1, 1, 0))
        code, report = self.migrate()
        self.assertEqual((code, report["invalid"]), (1, 1))


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
