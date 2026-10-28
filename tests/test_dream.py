"""Tests for consolidation: `mem dream` and the pass after every change.

Every test gets its own MEM_ROOT. Timestamps are written by hand instead of
using `store.write`, because "newer wins" is exactly what most of these cases
turn on and the wall clock is not a usable input for that.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from memlib import config, dream, index, store

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

    def put(
        self,
        scope,
        filename,
        name,
        description,
        body="body",
        modified=OLD,
        kind="project",
        cwd=None,
    ):
        """Write a memory file with an explicit `modified`, bypassing the clock."""
        directory = config.scope_dir(scope, str(cwd or self.cwd))
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

    def files(self, scope="project"):
        return sorted(path.name for path in self.directory(scope).glob("*.md"))

    def run_dream(self, **kwargs):
        kwargs.setdefault("cwd", str(self.cwd))
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = dream.run(**kwargs)
        text = stdout.getvalue().strip()
        self.assertEqual(len(text.splitlines()), 1, "dream must print one JSON line: %r" % text)
        return code, json.loads(text)


class ReplaceTests(DreamTestCase):
    """Same type + same name = the same note; the newest `modified` wins."""

    def test_the_older_note_is_removed(self):
        self.put("project", "old.md", "shared", "first", modified=OLD)
        self.put("project", "new.md", "shared", "second", modified=NEW)
        code, out = self.run_dream()
        self.assertEqual(code, 0)
        self.assertEqual(out["replaced"], 1)
        self.assertEqual(self.files(), ["MEMORY.md", "new.md"])

    def test_the_newer_body_is_the_one_kept(self):
        self.put("project", "old.md", "shared", "first", body="old body", modified=OLD)
        self.put("project", "new.md", "shared", "second", body="new body", modified=NEW)
        self.run_dream()
        self.assertEqual(store.load(self.directory() / "new.md").body, "new body")

    def test_a_different_type_is_a_different_note(self):
        self.put("project", "a.md", "shared", "d", kind="project", modified=OLD)
        self.put("project", "b.md", "shared", "d", kind="user", modified=NEW)
        _, out = self.run_dream()
        self.assertEqual(out["replaced"], 0)
        self.assertEqual(self.files(), ["MEMORY.md", "a.md", "b.md"])

    def test_a_file_edited_since_it_was_read_is_kept(self):
        path = self.put("project", "old.md", "shared", "first", modified=OLD)
        stale = store.load(path)
        store.write(
            self.directory(), "old.md", "edited by someone else", name="shared",
            description="first", if_version=stale.version,
        )
        self.assertFalse(dream._delete_unchanged(self.directory(), stale))
        self.assertTrue(path.exists())


class MergeTests(DreamTestCase):
    """Same type + exactly the same description words = duplicates."""

    DESCRIPTION = "the lockfile is pnpm and npm install breaks CI"

    def test_the_same_description_merges(self):
        self.put("project", "a.md", "one", self.DESCRIPTION, body="first detail", modified=OLD)
        self.put("project", "b.md", "two", self.DESCRIPTION, body="second detail", modified=NEW)
        _, out = self.run_dream()
        self.assertEqual(out["merged"], 1)
        self.assertEqual(self.files(), ["MEMORY.md", "b.md"])

    def test_unique_lines_of_the_older_are_appended(self):
        self.put("project", "a.md", "one", self.DESCRIPTION, body="first detail", modified=OLD)
        self.put("project", "b.md", "two", self.DESCRIPTION, body="second detail", modified=NEW)
        self.run_dream()
        body = store.load(self.directory() / "b.md").body
        self.assertIn("second detail", body)
        self.assertIn("first detail", body)

    def test_appended_lines_keep_their_paragraph_breaks(self):
        # Dropping the blank lines between the older note's paragraphs glued
        # unrelated statements into one block.
        self.put(
            "project", "a.md", "one", self.DESCRIPTION,
            body="older first\n\nolder second", modified=OLD,
        )
        self.put("project", "b.md", "two", self.DESCRIPTION, body="keeper", modified=NEW)
        self.run_dream()
        body = store.load(self.directory() / "b.md").body
        self.assertEqual(body, "keeper\n\nolder first\n\nolder second")

    def test_a_line_the_duplicate_repeats_is_copied_as_written(self):
        # Filtering is against the keeper, not against the duplicate itself:
        # a fenced block or a rule repeated line-for-line must survive intact.
        self.put(
            "project", "a.md", "one", self.DESCRIPTION,
            body="```\nrun\nrun\n```", modified=OLD,
        )
        self.put("project", "b.md", "two", self.DESCRIPTION, body="keeper", modified=NEW)
        self.run_dream()
        body = store.load(self.directory() / "b.md").body
        self.assertEqual(body, "keeper\n\n```\nrun\nrun\n```")

    def test_a_narrowing_qualifier_is_not_merged(self):
        # Each pair of descriptions shares nearly every word. At the old 0.8
        # cut-off the older note was deleted and its title lost without a
        # trace, so these pairs pin the rule down.
        pairs = [
            ("deploy with make release and tag", "deploy with make release and tag blue"),
            ("run unit tests before every commit", "run unit tests before every commit only"),
            (
                "deploy to production with make release and tag",
                "deploy to staging with make release and tag",
            ),
            ("pnpm, not npm", "use pnpm not npm"),
        ]
        for index, (first, second) in enumerate(pairs):
            with self.subTest(descriptions=(first, second)):
                cwd = self.root / f"pair-{index}"
                cwd.mkdir()
                self.put("project", "a.md", "one", first, modified=OLD, cwd=cwd)
                self.put("project", "b.md", "two", second, modified=NEW, cwd=cwd)
                directory = config.scope_dir("project", str(cwd))
                stats = dream.after_change(directory)
                self.assertEqual(stats.merged, 0)
                self.assertEqual(
                    sorted(path.name for path in directory.glob("*.md")),
                    ["MEMORY.md", "a.md", "b.md"],
                )

    def test_the_same_words_in_another_order_still_merge(self):
        self.put("project", "a.md", "one", "deploy with make release and tag", modified=OLD)
        self.put("project", "b.md", "two", "tag and release: deploy with make", modified=NEW)
        _, out = self.run_dream()
        self.assertEqual(out["merged"], 1)
        self.assertEqual(self.files(), ["MEMORY.md", "b.md"])

    def test_similarity_requires_the_same_word_set(self):
        self.assertFalse(dream.similar_descriptions("release tag", "release tag blue"))
        self.assertFalse(dream.similar_descriptions("release tag v2", "release tag v3"))
        self.assertTrue(dream.similar_descriptions("release tag", "tag, release"))
        self.assertFalse(dream.similar_descriptions("", "release tag"))
        self.assertFalse(dream.similar_descriptions("", ""))

    def test_the_threshold_is_only_safe_up_to_its_word_ceiling(self):
        # 0.95 is exactly the point where one added word to a 19-word
        # description reaches the threshold, so 19 is the largest size the
        # number can cover; the ceiling and the number have to move together.
        ceiling = dream.DUPLICATE_WORDS
        self.assertLess((ceiling - 1) / ceiling, dream.DUPLICATE_THRESHOLD)
        self.assertLessEqual(ceiling / (ceiling + 1), dream.DUPLICATE_THRESHOLD)

    def test_a_description_past_the_ceiling_is_never_merged(self):
        # Above the ceiling the same number would accept a qualifier, so the
        # pass declines instead of guessing: two identical long descriptions
        # stay apart rather than risk deleting an independent note.
        long = " ".join(f"word{number}" for number in range(dream.DUPLICATE_WORDS + 1))
        self.assertFalse(dream.similar_descriptions(long, long))
        self.put("project", "a.md", "one", long, modified=OLD)
        self.put("project", "b.md", "two", long, modified=NEW)
        _, out = self.run_dream()
        self.assertEqual(out["merged"], 0)
        self.assertEqual(self.files(), ["MEMORY.md", "a.md", "b.md"])

    def test_dissimilar_descriptions_are_kept_apart(self):
        self.put("project", "a.md", "one", "the lockfile is pnpm", modified=OLD)
        self.put("project", "b.md", "two", "chromium poisons fontconfig", modified=NEW)
        _, out = self.run_dream()
        self.assertEqual(out["merged"], 0)
        self.assertEqual(self.files(), ["MEMORY.md", "a.md", "b.md"])

    def test_a_different_type_is_never_merged(self):
        self.put("project", "a.md", "one", self.DESCRIPTION, kind="project", modified=OLD)
        self.put("project", "b.md", "two", self.DESCRIPTION, kind="user", modified=NEW)
        _, out = self.run_dream()
        self.assertEqual(out["merged"], 0)
        self.assertEqual(self.files(), ["MEMORY.md", "a.md", "b.md"])

    def test_a_conflict_on_the_keeper_keeps_both_files(self):
        self.put("project", "a.md", "one", self.DESCRIPTION, body="first", modified=OLD)
        self.put("project", "b.md", "two", self.DESCRIPTION, body="second", modified=NEW)
        memories = store.list_memories(self.directory())
        stats = dream.Stats()
        with mock.patch.object(
            store, "write", side_effect=store.ConflictError("changed under us")
        ):
            dream._merge_duplicates(self.directory(), memories, stats)
        self.assertEqual(stats.merged, 0)
        self.assertTrue((self.directory() / "a.md").exists())
        self.assertTrue((self.directory() / "b.md").exists())


class IndexRepairTests(DreamTestCase):
    def test_one_pass_rebuilds_the_index_once(self):
        self.put("project", "a.md", "a", "d")
        with mock.patch.object(index, "rebuild", wraps=index.rebuild) as rebuild:
            dream.after_change(self.directory())
        self.assertEqual(rebuild.call_count, 1)

    def test_a_pass_that_raises_still_rebuilds_the_index(self):
        self.put("project", "a.md", "a", "d")
        stderr = io.StringIO()
        with mock.patch.object(
            dream, "_merge_duplicates", side_effect=RuntimeError("boom")
        ):
            with redirect_stderr(stderr):
                dream.after_change(self.directory())
        self.assertIn("consolidation skipped", stderr.getvalue())
        self.assertIn("a.md", (self.directory() / "MEMORY.md").read_text())

    def test_a_line_for_a_gone_file_is_removed_and_counted(self):
        path = self.put("project", "a.md", "a", "d")
        index.rebuild(self.directory())
        path.unlink()
        _, out = self.run_dream()
        self.assertEqual(out["index_lines"], 1)
        self.assertNotIn("a.md", (self.directory() / "MEMORY.md").read_text())

    def test_a_second_run_changes_nothing(self):
        self.put("project", "a.md", "one", MergeTests.DESCRIPTION, body="first", modified=OLD)
        self.put("project", "b.md", "two", MergeTests.DESCRIPTION, body="second", modified=NEW)
        self.run_dream()
        before = {p.name: p.read_bytes() for p in self.directory().iterdir()}
        _, out = self.run_dream()
        after = {p.name: p.read_bytes() for p in self.directory().iterdir()}
        self.assertEqual(out["merged"], 0)
        self.assertEqual(out["replaced"], 0)
        self.assertEqual(out["index_lines"], 0)
        self.assertEqual(before, after)


class ScopeTests(DreamTestCase):
    def test_run_covers_global_and_the_current_project(self):
        self.put("global", "g.md", "g", "d")
        self.put("project", "p.md", "p", "d")
        _, out = self.run_dream()
        self.assertEqual(out["scopes"], 2)
        self.assertIn("g.md", (config.scope_dir("global", str(self.cwd)) / "MEMORY.md").read_text())
        self.assertIn("p.md", (self.directory() / "MEMORY.md").read_text())

    def test_all_covers_every_project(self):
        other = self.root / "other"
        other.mkdir()
        self.put("project", "p.md", "p", "d")
        directory = config.scope_dir("project", str(other))
        directory.mkdir(parents=True)
        (directory / "q.md").write_text(
            store.render({"name": "q", "description": "d", "metadata": {"type": "project"}}, "body\n"),
            encoding="utf-8",
        )
        _, out = self.run_dream(all_scopes=True)
        self.assertEqual(out["scopes"], 3)
        self.assertIn("q.md", (directory / "MEMORY.md").read_text())

    def test_an_empty_store_is_a_clean_run(self):
        code, out = self.run_dream()
        self.assertEqual(code, 0)
        self.assertEqual(out["merged"], 0)
        self.assertEqual(out["replaced"], 0)
        self.assertFalse(self.directory().exists())


class AfterChangeTests(DreamTestCase):
    def test_consolidates_the_touched_scope(self):
        self.put("project", "a.md", "shared", "d", modified=OLD)
        self.put("project", "b.md", "shared", "d", modified=NEW)
        stats = dream.after_change(self.directory())
        self.assertEqual(stats.scopes, 1)
        self.assertEqual(stats.replaced, 1)
        self.assertEqual(self.files(), ["MEMORY.md", "b.md"])

    def test_rebuilds_the_index_even_when_the_pass_fails(self):
        self.put("project", "a.md", "a", "d")
        stderr = io.StringIO()
        with mock.patch.object(dream, "_consolidate", side_effect=RuntimeError("boom")):
            with redirect_stderr(stderr):
                stats = dream.after_change(self.directory())
        self.assertEqual(stats.scopes, 1)
        self.assertIn("a.md", (self.directory() / "MEMORY.md").read_text())
        self.assertIn("boom", stderr.getvalue())

    def test_never_raises(self):
        self.put("project", "a.md", "a", "d")
        with mock.patch.object(dream, "_consolidate", side_effect=RuntimeError("boom")):
            with redirect_stderr(io.StringIO()):
                dream.after_change(self.directory())  # must not raise


if __name__ == "__main__":
    unittest.main()
