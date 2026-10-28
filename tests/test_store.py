"""Tests for the store: frontmatter, versions, conflicts, secret refusal."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memlib import index, store


class FrontmatterTests(unittest.TestCase):
    def test_roundtrip(self):
        meta = {
            "name": "pnpm-not-npm",
            "description": 'the lockfile is pnpm; npm "install" breaks CI',
            "metadata": {
                "node_type": "memory",
                "type": "project",
                "originSessionId": "abc",
                "modified": "2026-10-02T11:20:00.000Z",
            },
        }
        text = store.render(meta, "body line\n")
        parsed, body = store.parse(text)
        self.assertEqual(parsed["name"], "pnpm-not-npm")
        self.assertEqual(parsed["description"], meta["description"])
        self.assertEqual(parsed["metadata"]["type"], "project")
        self.assertEqual(body.strip(), "body line")

    def test_no_frontmatter_is_all_body(self):
        meta, body = store.parse("just text\n")
        self.assertEqual(meta, {})
        self.assertEqual(body, "just text\n")


class WriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_then_read(self):
        memory = store.write(
            self.dir, "a.md", "hello", name="a", description="first", if_version="new"
        )
        self.assertEqual(memory.name, "a")
        self.assertEqual(store.load(self.dir / "a.md").body, "hello")
        self.assertEqual(len(memory.version), 12)

    def test_version_changes_with_content(self):
        first = store.write(self.dir, "a.md", "one", if_version="new")
        second = store.write(self.dir, "a.md", "two", if_version=first.version)
        self.assertNotEqual(first.version, second.version)

    def test_stale_version_is_rejected_and_returns_current(self):
        first = store.write(self.dir, "a.md", "one", if_version="new")
        store.write(self.dir, "a.md", "two", if_version=first.version)
        with self.assertRaises(store.ConflictError) as ctx:
            store.write(self.dir, "a.md", "three", if_version=first.version)
        self.assertIsNotNone(ctx.exception.current)
        self.assertEqual(ctx.exception.current.body, "two")

    def test_new_on_existing_is_rejected(self):
        store.write(self.dir, "a.md", "one", if_version="new")
        with self.assertRaises(store.ConflictError):
            store.write(self.dir, "a.md", "two", if_version="new")

    def test_if_version_is_required(self):
        with self.assertRaises(store.StoreError):
            store.write(self.dir, "a.md", "one")

    def test_force_skips_the_check(self):
        store.write(self.dir, "a.md", "one", if_version="new")
        memory = store.write(self.dir, "a.md", "two", force=True)
        self.assertEqual(memory.body, "two")

    def test_secret_is_refused(self):
        with self.assertRaises(store.SecretError):
            store.write(self.dir, "a.md", "key is sk-" + "a" * 24, if_version="new")

    def test_prose_about_tokens_is_allowed(self):
        memory = store.write(
            self.dir, "a.md", "the token budget is small", if_version="new"
        )
        self.assertEqual(memory.title, "A")

    def test_empty_body_is_refused(self):
        with self.assertRaises(store.StoreError):
            store.write(self.dir, "a.md", "   \n", if_version="new")

    def test_bad_filename_is_refused(self):
        for name in ("../x.md", "x.txt", "MEMORY.md"):
            with self.assertRaises(store.StoreError):
                store.write(self.dir, name, "body", if_version="new")

    def test_unknown_type_is_refused(self):
        with self.assertRaises(store.StoreError):
            store.write(self.dir, "a.md", "body", if_version="new", memory_type="gotcha")

    def test_delete(self):
        store.write(self.dir, "a.md", "one", if_version="new")
        self.assertTrue(store.delete(self.dir, "a.md"))
        self.assertFalse(store.delete(self.dir, "a.md"))

    def test_delete_with_a_stale_version_keeps_the_file(self):
        # `if_version` on delete is what lets consolidation remove a file only
        # while it still holds the bytes the pass read.
        written = store.write(self.dir, "a.md", "one", if_version="new")
        self.assertFalse(store.delete(self.dir, "a.md", if_version="deadbeef"))
        self.assertTrue((self.dir / "a.md").exists())
        self.assertTrue(store.delete(self.dir, "a.md", if_version=written.version))
        self.assertFalse((self.dir / "a.md").exists())


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rebuild_groups_by_type(self):
        store.write(
            self.dir, "u.md", "b", name="pref", description="d", memory_type="user",
            if_version="new",
        )
        store.write(
            self.dir, "p.md", "b", name="proj", description="d", memory_type="project",
            if_version="new",
        )
        index.rebuild(self.dir)
        text = (self.dir / "MEMORY.md").read_text()
        self.assertIn("## user (1)", text)
        self.assertIn("## project (1)", text)
        self.assertIn("(u.md)", text)

    def test_rename_leaves_no_dangling_line(self):
        store.write(self.dir, "old.md", "b", name="x", description="d", if_version="new")
        index.rebuild(self.dir)
        store.delete(self.dir, "old.md")
        store.write(self.dir, "new.md", "b", name="x", description="d", if_version="new")
        index.rebuild(self.dir)
        text = (self.dir / "MEMORY.md").read_text()
        self.assertNotIn("old.md", text)
        self.assertIn("new.md", text)

    def test_empty_store(self):
        index.rebuild(self.dir)
        self.assertIn("_empty_", (self.dir / "MEMORY.md").read_text())


if __name__ == "__main__":
    unittest.main()
