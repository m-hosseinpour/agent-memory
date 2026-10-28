"""Tests for the consolidation pass (`mem dream`).

Every test gets its own MEM_ROOT. Timestamps are written by hand instead of
using `store.write`, because "newer wins" is exactly what most of these cases
turn on and the wall clock is not a usable input for that.
"""

from __future__ import annotations

import fcntl
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from memlib import cli, config, dream, hooks, index, store

GIT = shutil.which("git")
OLD = "2026-10-01T00:00:00.000Z"
NEW = "2026-10-02T00:00:00.000Z"


class DreamTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mem-dream-"))
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

    def put(self, scope, filename, name, description, body="body", modified=OLD, kind="project"):
        """Write a memory file with an explicit `modified`, bypassing the clock."""
        directory = config.scope_dir(scope, str(self.cwd))
        directory.mkdir(parents=True, exist_ok=True)
        meta = {
            "name": name,
            "description": description,
            "metadata": {
                "node_type": "memory",
                "type": kind,
                "modified": modified,
            },
        }
        path = directory / filename
        path.write_text(store.render(meta, body + "\n"), encoding="utf-8")
        return path

    def directory(self, scope="project"):
        return config.scope_dir(scope, str(self.cwd))

    def run_dream(self, **kwargs):
        kwargs.setdefault("cwd", str(self.cwd))
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = dream.run(**kwargs)
        text = stdout.getvalue().strip()
        self.assertEqual(len(text.splitlines()), 1, "dream must print one JSON line: %r" % text)
        return code, json.loads(text)

    def bump(self, times):
        for _ in range(times):
            hooks.bump_session("session-x")

    def files(self, scope="project"):
        return sorted(path.name for path in self.directory(scope).glob("*.md"))


class GateTests(DreamTestCase):
    def test_below_threshold_does_not_run(self):
        self.bump(4)
        code, out = self.run_dream()
        self.assertEqual(code, 0)
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "below threshold")
        self.assertFalse((self.directory() / "MEMORY.md").exists())

    def test_runs_at_the_threshold_and_resets_the_counter(self):
        self.bump(5)
        code, out = self.run_dream()
        self.assertEqual(code, 0)
        self.assertTrue(out["ran"])
        self.assertEqual(out["sessions"], 5)
        self.assertEqual(hooks.session_count(), 0)

    def test_force_runs_below_the_threshold(self):
        self.bump(1)
        code, out = self.run_dream(force=True)
        self.assertTrue(out["ran"])
        self.assertEqual(out["sessions"], 1)
        self.assertEqual(hooks.session_count(), 0)

    def test_threshold_comes_from_config(self):
        self.write_config("[dream]\nevery_sessions = 2\n")
        self.bump(2)
        _, out = self.run_dream()
        self.assertTrue(out["ran"])

    def test_empty_root_runs_cleanly(self):
        code, out = self.run_dream(force=True)
        self.assertEqual(code, 0)
        self.assertTrue(out["ran"])
        self.assertEqual(out["merged"], 0)
        self.assertEqual(out["replaced"], 0)
        self.assertFalse(self.directory().exists())


class LockTests(DreamTestCase):
    def lock(self, contents=None):
        path = self.root / ".state" / dream.LOCK_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        if contents is not None:
            path.write_text(contents, encoding="utf-8")
        return path

    def hold_lock(self):
        """Hold the flock the way a running pass does."""
        handle = os.open(self.lock(), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, handle)

    def lock_is_free(self):
        handle = os.open(self.lock(), os.O_RDWR)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(handle)

    def test_a_held_lock_blocks_the_run(self):
        self.bump(3)
        self.hold_lock()
        code, out = self.run_dream(force=True)
        self.assertEqual(code, 0)
        self.assertFalse(out["ran"])
        self.assertEqual(out["reason"], "locked")
        self.assertEqual(hooks.session_count(), 3)

    def test_a_leftover_lock_file_does_not_block(self):
        # A crash leaves the file behind; only the flock means "running".
        self.lock(str(os.getpid()))
        code, out = self.run_dream(force=True)
        self.assertEqual(code, 0)
        self.assertTrue(out["ran"])

    def test_lock_is_free_after_a_run(self):
        self.run_dream(force=True)
        self.lock_is_free()

    def test_lock_is_released_when_the_pass_fails(self):
        with mock.patch("memlib.dream._consolidate", side_effect=RuntimeError("boom")):
            code, out = self.run_dream(force=True)
        self.assertEqual(code, 1)
        self.assertEqual(out["error"], "internal")
        self.assertIn("boom", out["message"])
        self.lock_is_free()


class ConsolidationTests(DreamTestCase):
    def test_same_name_newest_wins_without_merging(self):
        self.put("project", "old.md", "pnpm-not-npm", "use pnpm", "OLD-BODY", modified=OLD)
        self.put("project", "new.md", "pnpm-not-npm", "use pnpm", "NEW-BODY", modified=NEW)
        code, out = self.run_dream(force=True)
        self.assertEqual(code, 0)
        self.assertEqual(out["replaced"], 1)
        self.assertEqual(out["merged"], 0)
        self.assertEqual(self.files(), ["MEMORY.md", "new.md"])
        self.assertNotIn("OLD-BODY", (self.directory() / "new.md").read_text())

    def test_same_name_different_type_is_not_a_contradiction(self):
        self.put("project", "a.md", "pnpm", "use pnpm", "A", modified=OLD, kind="user")
        self.put("project", "b.md", "pnpm", "use pnpm", "B", modified=NEW, kind="project")
        _, out = self.run_dream(force=True)
        self.assertEqual(out["replaced"], 0)
        self.assertEqual(self.files(), ["MEMORY.md", "a.md", "b.md"])

    def test_near_duplicate_bodies_are_merged(self):
        self.put(
            "project",
            "old.md",
            "pnpm-notes",
            "the lockfile is pnpm; npm install breaks CI",
            "shared line\nOLD-ONLY-LINE",
            modified=OLD,
        )
        self.put(
            "project",
            "new.md",
            "pnpm-rule",
            "the lockfile is pnpm; npm install breaks the CI",
            "shared line",
            modified=NEW,
        )
        code, out = self.run_dream(force=True)
        self.assertEqual(code, 0)
        self.assertEqual(out["merged"], 1)
        self.assertEqual(self.files(), ["MEMORY.md", "new.md"])
        keeper = store.load(self.directory() / "new.md")
        self.assertIn("shared line", keeper.body)
        self.assertIn("OLD-ONLY-LINE", keeper.body)
        self.assertEqual(keeper.description, "the lockfile is pnpm; npm install breaks the CI")

    def test_a_concurrent_edit_to_the_keeper_is_neither_lost_nor_merged_over(self):
        self.put(
            "project",
            "old.md",
            "pnpm-notes",
            "the lockfile is pnpm; npm install breaks CI",
            "shared line\nOLD-ONLY-LINE",
            modified=OLD,
        )
        self.put(
            "project",
            "new.md",
            "pnpm-rule",
            "the lockfile is pnpm; npm install breaks the CI",
            "shared line",
            modified=NEW,
        )
        directory = self.directory()
        snapshot = store.list_memories(directory)
        keeper = store.load(directory / "new.md")
        # Another agent rewrites the keeper between the scan and the merge.
        store.write(
            directory,
            "new.md",
            "CONCURRENT-EDIT",
            name=keeper.name,
            description=keeper.description,
            if_version=keeper.version,
        )

        stats = dream.Stats()
        dream._merge_duplicates(directory, snapshot, stats)

        self.assertEqual(stats.merged, 0)
        self.assertIn("CONCURRENT-EDIT", (directory / "new.md").read_text())
        self.assertTrue((directory / "old.md").exists(), "nothing may be deleted on a conflict")

    def test_a_concurrent_edit_to_the_replaced_note_is_not_deleted(self):
        self.put("project", "old.md", "pnpm", "use pnpm", "OLD-BODY", modified=OLD)
        self.put("project", "new.md", "pnpm", "use pnpm", "NEW-BODY", modified=NEW)
        directory = self.directory()
        snapshot = store.list_memories(directory)
        older = store.load(directory / "old.md")
        store.write(
            directory,
            "old.md",
            "CONCURRENT-EDIT",
            name=older.name,
            description=older.description,
            if_version=older.version,
        )

        stats = dream.Stats()
        dream._replace_same_name(directory, snapshot, stats)

        self.assertEqual(stats.replaced, 0)
        self.assertIn("CONCURRENT-EDIT", (directory / "old.md").read_text())

    def test_unrelated_descriptions_are_kept(self):
        self.put("project", "a.md", "pnpm", "the lockfile is pnpm", "A", modified=OLD)
        self.put("project", "b.md", "fonts", "chromium poisons the fontconfig cache", "B", NEW)
        _, out = self.run_dream(force=True)
        self.assertEqual(out["merged"], 0)
        self.assertEqual(self.files(), ["MEMORY.md", "a.md", "b.md"])

    def test_duplicate_across_types_is_kept(self):
        self.put(
            "project", "a.md", "pnpm", "the lockfile is pnpm", "A", modified=OLD, kind="user"
        )
        self.put(
            "project", "b.md", "npm", "the lockfile is pnpm", "B", modified=NEW, kind="project"
        )
        _, out = self.run_dream(force=True)
        self.assertEqual(out["merged"], 0)
        self.assertEqual(self.files(), ["MEMORY.md", "a.md", "b.md"])

    def test_global_scope_is_consolidated_too(self):
        self.put("global", "old.md", "short", "answers should be short", "A", modified=OLD)
        self.put("global", "new.md", "short", "answers should be short", "B", modified=NEW)
        _, out = self.run_dream(force=True)
        self.assertEqual(out["replaced"], 1)
        self.assertEqual(self.files("global"), ["MEMORY.md", "new.md"])

    def test_all_scopes_covers_other_projects(self):
        other = config.scope_dir("project", str(self.root / "other"))
        other.mkdir(parents=True)
        meta = {"name": "x", "description": "d", "metadata": {"type": "project", "modified": OLD}}
        (other / "old.md").write_text(store.render(meta, "A\n"), encoding="utf-8")
        (other / "new.md").write_text(
            store.render({**meta, "metadata": {**meta["metadata"], "modified": NEW}}, "B\n"),
            encoding="utf-8",
        )
        _, out = self.run_dream(force=True)
        self.assertEqual(out["replaced"], 0, "without --all another project is untouched")

        _, out = self.run_dream(force=True, all_scopes=True)
        self.assertEqual(out["replaced"], 1)
        self.assertEqual(sorted(p.name for p in other.glob("*.md")), ["MEMORY.md", "new.md"])


class IndexRepairTests(DreamTestCase):
    def test_dead_index_line_is_removed(self):
        self.put("project", "gone.md", "gone", "a note that is deleted", "body")
        self.put("project", "kept.md", "kept", "a note that stays", "body")
        index.rebuild(self.directory())
        (self.directory() / "gone.md").unlink()

        code, out = self.run_dream(force=True)
        self.assertEqual(code, 0)
        self.assertEqual(out["index_lines"], 1)
        text = (self.directory() / "MEMORY.md").read_text()
        self.assertNotIn("gone.md", text)
        self.assertIn("kept.md", text)

    def test_index_is_created_when_missing(self):
        self.put("project", "note.md", "note", "a note", "body")
        _, out = self.run_dream(force=True)
        self.assertEqual(out["index_lines"], 0)
        self.assertIn("note.md", (self.directory() / "MEMORY.md").read_text())

    def test_second_run_is_idempotent(self):
        self.put("project", "gone.md", "gone", "a deleted note", "body")
        self.put("project", "kept.md", "kept", "a kept note", "body")
        index.rebuild(self.directory())
        (self.directory() / "gone.md").unlink()

        self.run_dream(force=True)
        before = (self.directory() / "MEMORY.md").read_bytes()
        code, out = self.run_dream(force=True)
        self.assertEqual(code, 0)
        self.assertEqual(
            (out["merged"], out["replaced"], out["index_lines"]), (0, 0, 0)
        )
        self.assertEqual((self.directory() / "MEMORY.md").read_bytes(), before)


class LogTests(DreamTestCase):
    def test_one_line_is_appended_per_run(self):
        self.run_dream(force=True)
        self.run_dream(force=True)
        lines = (self.root / dream.LOG_NAME).read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("dream scopes=", lines[0])
        self.assertIn("merged=0", lines[0])


@unittest.skipUnless(GIT, "git is not installed")
class GitTests(DreamTestCase):
    def setUp(self):
        super().setUp()
        self.env = dict(os.environ)
        self.env.update(
            {
                "GIT_AUTHOR_NAME": "Dream Test",
                "GIT_AUTHOR_EMAIL": "dream@example.com",
                "GIT_COMMITTER_NAME": "Dream Test",
                "GIT_COMMITTER_EMAIL": "dream@example.com",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_SYSTEM": os.devnull,
            }
        )
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )

    def commits(self):
        return int(self.git("rev-list", "--count", "HEAD").stdout.strip())

    def test_commit_after_a_changed_run_and_none_after_a_clean_one(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        self.put("project", "old.md", "pnpm", "use pnpm", "A", modified=OLD)
        self.put("project", "new.md", "pnpm", "use pnpm", "B", modified=NEW)
        self.git("add", "-A")
        self.git("commit", "-qm", "baseline")
        baseline = self.commits()

        code, out = self.run_dream(force=True)
        self.assertEqual(code, 0)
        # The first run also creates .gitignore, which the "before" snapshot
        # picks up; the consolidation itself is the second commit.
        self.assertEqual(out["commits"], 2)
        self.assertEqual(self.commits(), baseline + 2)
        self.assertIn("dream:", self.git("log", "-1", "--pretty=%s").stdout)
        self.assertEqual(self.git("ls-files", ".state").stdout.strip(), "")

        code, out = self.run_dream(force=True)
        self.assertEqual(out["commits"], 0)
        self.assertEqual(self.commits(), baseline + 2)
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_runtime_files_are_ignored(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True, capture_output=True)
        self.put("project", "note.md", "note", "a note", "body")
        self.git("add", "-A")
        self.git("commit", "-qm", "baseline")
        self.bump(2)

        self.run_dream(force=True)
        ignore = (self.root / ".gitignore").read_text()
        self.assertIn(".state/", ignore)
        self.assertIn("dream.log", ignore)
        self.assertEqual(self.git("ls-files", ".state").stdout.strip(), "")
        self.assertEqual(self.git("status", "--porcelain").stdout, "")

    def test_plain_directory_is_not_touched_by_git(self):
        self.put("project", "note.md", "note", "a note", "body")
        _, out = self.run_dream(force=True)
        self.assertEqual(out["commits"], 0)
        self.assertFalse((self.root / ".gitignore").exists())

    def test_a_failed_commit_is_reported_instead_of_swallowed(self):
        """A missing git identity is the usual cause; silence hides a dead history."""
        import subprocess as sp

        (self.root / ".git").mkdir(exist_ok=True)

        def fake_git(root, args):
            if args[:1] == ["rev-parse"]:
                return sp.CompletedProcess(args, 0, str(root), "")
            if "commit" in args:
                return sp.CompletedProcess(args, 1, "", "Author identity unknown")
            if args[:2] == ["diff", "--cached"]:
                return sp.CompletedProcess(args, 1, "", "")  # something is staged
            return sp.CompletedProcess(args, 0, "", "")

        stderr = io.StringIO()
        with mock.patch("memlib.dream._git", side_effect=fake_git), mock.patch(
            "sys.stderr", stderr
        ):
            committed = dream._git_commit(self.root, "dream: test")

        self.assertFalse(committed)
        self.assertIn("git commit failed", stderr.getvalue())
        self.assertIn("Author identity unknown", stderr.getvalue())


class HookTriggerTests(DreamTestCase):
    def payload(self, event="session-start"):
        return {
            "session_id": "session-1",
            "cwd": str(self.cwd),
            "hook_event_name": "SessionStart",
            "transcript_path": str(self.root / "t.jsonl"),
            "source": "startup",
        }

    def hook(self):
        stdout = io.StringIO()
        code = hooks.run(
            "session-start", stdin=io.StringIO(json.dumps(self.payload())), stdout=stdout
        )
        return code, stdout.getvalue()

    def test_dream_is_spawned_at_the_threshold(self):
        self.bump(4)
        with mock.patch("memlib.hooks.subprocess.Popen") as popen:
            code, _ = self.hook()
        self.assertEqual(code, 0)
        popen.assert_called_once()
        args = popen.call_args[0][0]
        self.assertIn("dream", args)
        self.assertEqual(args[args.index("--cwd") + 1], str(self.cwd))

    def test_dream_is_not_spawned_below_the_threshold(self):
        self.bump(1)
        with mock.patch("memlib.hooks.subprocess.Popen") as popen:
            self.hook()
        popen.assert_not_called()

    def test_the_spawned_argv_is_one_the_cli_accepts(self):
        self.bump(4)
        with mock.patch("memlib.hooks.subprocess.Popen") as popen:
            self.hook()
        argv = popen.call_args[0][0]
        parsed = cli.build_parser().parse_args(argv[1:])
        self.assertEqual(parsed.command, "dream")
        self.assertEqual(parsed.cwd, str(self.cwd))

    def test_a_spawn_failure_does_not_break_the_hook(self):
        self.bump(4)
        stderr = io.StringIO()
        with mock.patch("memlib.hooks.subprocess.Popen", side_effect=OSError("no exec")):
            with mock.patch("sys.stderr", stderr):
                code, _ = self.hook()
        self.assertEqual(code, 0)
        self.assertIn("dream not started", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
