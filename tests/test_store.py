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

    def test_a_crlf_note_parses_and_is_read_as_lf(self):
        # A note saved by a Windows editor must not turn into a body-only file
        # whose frontmatter (and so its type and description) is invisible.
        meta, body = store.parse(
            "---\r\n"
            "name: a\r\n"
            "description: d\r\n"
            "metadata:\r\n"
            "  type: user\r\n"
            "---\r\n"
            "\r\n"
            "one\r\n"
            "two\r\n"
        )
        self.assertEqual(meta["name"], "a")
        self.assertEqual(meta["metadata"]["type"], "user")
        self.assertEqual(body.strip(), "one\ntwo")
        self.assertNotIn("\r", body)


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

    def test_a_body_past_the_cap_is_refused(self):
        with self.assertRaises(store.StoreError):
            store.write(
                self.dir, "big.md", "x" * (store.MAX_BODY_BYTES + 1), if_version="new"
            )
        self.assertFalse((self.dir / "big.md").exists())

    def test_a_body_at_the_cap_is_accepted(self):
        memory = store.write(
            self.dir, "big.md", "x" * store.MAX_BODY_BYTES, if_version="new"
        )
        self.assertEqual(len(memory.body.encode("utf-8")), store.MAX_BODY_BYTES)

    def test_bad_filename_is_refused(self):
        for name in ("../x.md", "x.txt", "MEMORY.md"):
            with self.assertRaises(store.StoreError):
                store.write(self.dir, name, "body", if_version="new")

    def test_the_index_name_is_reserved_in_any_case(self):
        # On a case-insensitive volume `memory.md` *is* MEMORY.md, so accepting
        # it would let a note replace the generated index.
        for name in ("memory.md", "Memory.md"):
            with self.subTest(name=name):
                with self.assertRaises(store.StoreError):
                    store.write(self.dir, name, "body", if_version="new")

    def test_a_crlf_body_is_stored_as_lf(self):
        memory = store.write(self.dir, "a.md", "one\r\ntwo\r\n", if_version="new")
        self.assertEqual(memory.body, "one\ntwo")
        self.assertNotIn(b"\r", (self.dir / "a.md").read_bytes())

    def test_unknown_type_is_refused(self):
        with self.assertRaises(store.StoreError):
            store.write(self.dir, "a.md", "body", if_version="new", memory_type="gotcha")

    def test_a_line_break_in_description_or_name_is_refused(self):
        # The frontmatter is parsed line by line: the 0.2.0 bug wrote a value
        # cut at the break and left the rest of it inside the block.
        for field in ("description", "name"):
            for value in (
                "multi\nline",
                "multi\rline",
                "multi\r\nline",
                "multi\u2028line",
                "multi\x0bline",
                "trailing\n",
            ):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(store.StoreError):
                        store.write(
                            self.dir, "a.md", "body", if_version="new", **{field: value}
                        )
                    self.assertFalse((self.dir / "a.md").exists())

    def test_a_note_carries_no_session_field(self):
        # originSessionId was inherited from Claude Code but never filled in:
        # the MCP tool has no session argument, so the field stayed empty and
        # claimed a provenance the store could not have (design §11).
        store.write(self.dir, "a.md", "body", if_version="new")
        self.assertNotIn("originSessionId", (self.dir / "a.md").read_text())

    def test_a_rewrite_drops_a_session_field_from_another_tool(self):
        # A file another tool wrote stays readable with the field, but a rewrite
        # drops it (other metadata keys are left alone).
        path = self.dir / "a.md"
        path.write_text(
            store.render(
                {
                    "name": "a",
                    "description": "d",
                    "metadata": {
                        "node_type": "memory",
                        "type": "project",
                        "originSessionId": "507e6cf4",
                        "modified": "2026-10-01T00:00:00.000Z",
                    },
                },
                "body\n",
            ),
            encoding="utf-8",
        )
        current = store.load(path)
        self.assertEqual(current.meta["metadata"]["originSessionId"], "507e6cf4")
        store.write(self.dir, "a.md", "new body", if_version=current.version)
        self.assertNotIn("originSessionId", path.read_text())

    def test_a_refused_line_break_leaves_the_note_untouched(self):
        first = store.write(self.dir, "u.md", "x", description="before", if_version="new")
        raw = (self.dir / "u.md").read_bytes()
        with self.assertRaises(store.StoreError):
            store.write(
                self.dir, "u.md", "y", description="after\nline", if_version=first.version
            )
        self.assertEqual((self.dir / "u.md").read_bytes(), raw)

    def test_line_free_values_round_trip(self):
        values = (
            "",
            "plain",
            'with "quotes"',
            "back\\slash",
            "tab\there",
            "colon: dash - and ---",
            "  padded  ",
            "متن فارسی",
            "emoji 🙂",
        )
        for index, value in enumerate(values):
            with self.subTest(value=value):
                name = f"n{index}"
                filename = f"{name}.md"
                created = store.write(
                    self.dir,
                    filename,
                    "b",
                    name=name,
                    description=value,
                    if_version="new",
                )
                loaded = store.load(self.dir / filename)
                self.assertEqual((loaded.name, loaded.description), (name, value))
                again = store.write(self.dir, filename, "b2", if_version=created.version)
                self.assertEqual(again.description, value)

    def test_rewrite_keeps_fields_the_caller_did_not_pass(self):
        # The 0.2.0 bug: a body-only rewrite reset name, description and type.
        first = store.write(
            self.dir,
            "u.md",
            "x",
            name="prefers-vim",
            description="prefers vim",
            memory_type="user",
            if_version="new",
        )
        again = store.write(self.dir, "u.md", "x, fixed typo", if_version=first.version)
        self.assertEqual(again.name, "prefers-vim")
        self.assertEqual(again.description, "prefers vim")
        self.assertEqual(again.type, "user")
        self.assertEqual(again.body, "x, fixed typo")
        self.assertEqual(index.line_for(again), "- [Prefers vim](u.md) — prefers vim")

    def test_rewrite_changes_only_the_fields_it_passes(self):
        first = store.write(
            self.dir,
            "u.md",
            "x",
            name="prefers-vim",
            description="prefers vim",
            memory_type="user",
            if_version="new",
        )
        again = store.write(
            self.dir, "u.md", "x", description="edits in vim", if_version=first.version
        )
        self.assertEqual(again.description, "edits in vim")
        self.assertEqual((again.name, again.type), ("prefers-vim", "user"))

    def test_an_explicit_empty_description_clears_it(self):
        first = store.write(
            self.dir, "u.md", "x", description="prefers vim", if_version="new"
        )
        again = store.write(self.dir, "u.md", "x", description="", if_version=first.version)
        self.assertEqual(again.description, "")

    def test_force_rewrite_keeps_fields_the_caller_did_not_pass(self):
        store.write(
            self.dir,
            "u.md",
            "x",
            name="prefers-vim",
            description="prefers vim",
            memory_type="user",
            if_version="new",
        )
        forced = store.write(self.dir, "u.md", "forced", force=True)
        self.assertEqual(
            (forced.name, forced.description, forced.type),
            ("prefers-vim", "prefers vim", "user"),
        )

    def test_a_new_file_without_fields_gets_the_defaults(self):
        memory = store.write(self.dir, "fresh.md", "body", if_version="new")
        self.assertEqual((memory.name, memory.description, memory.type), ("fresh", "", "project"))

    def test_an_empty_name_falls_back_instead_of_blanking_the_file(self):
        first = store.write(self.dir, "u.md", "x", name="prefers-vim", if_version="new")
        memory = store.write(self.dir, "u.md", "y", name="", if_version=first.version)
        self.assertEqual(memory.name, "prefers-vim")

    def test_a_bad_type_on_rewrite_is_refused_and_leaves_the_file_alone(self):
        first = store.write(self.dir, "u.md", "x", if_version="new")
        with self.assertRaises(store.StoreError):
            store.write(self.dir, "u.md", "y", if_version=first.version, memory_type="gotcha")
        self.assertEqual(store.load(self.dir / "u.md").body, "x")

    def test_rewrite_drops_frontmatter_keys_the_store_does_not_own(self):
        # D2 rejected (design §11): only name, description and metadata survive a
        # rewrite; a hand-written `title` is dropped rather than carried unmanaged.
        path = self.dir / "vim.md"
        path.write_text(
            "---\n"
            "name: prefers-vim\n"
            'title: "Vim, not VS Code"\n'
            "description: edits in vim\n"
            "metadata:\n"
            "  type: user\n"
            "  modified: 2026-10-01T00:00:00.000Z\n"
            "---\n\nx\n",
            encoding="utf-8",
        )
        memory = store.write(
            self.dir,
            "vim.md",
            "x, fixed typo",
            if_version=store.version_of(path.read_bytes()),
        )
        self.assertNotIn("title", memory.meta)
        self.assertEqual(memory.title, "Prefers vim")
        self.assertEqual((memory.name, memory.type), ("prefers-vim", "user"))

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
