"""Tests for the opencode adapter: install fragment shape and plugin wiring.

No JS test runner is added: the plugin is checked with `node --check` when node
is available, and its shell-outs are read from the source so the tests fail if
the `mem` subcommands or flags drift.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "integrations" / "opencode" / "mem.js"
INSTALL = REPO / "install" / "opencode.json"

# The shell-outs the plugin must make, as Bun template literals: $`...`
SHELLOUT = re.compile(r"\$`([^`]*)`")


class InstallFragmentTests(unittest.TestCase):
    def test_parses_and_has_expected_keys(self):
        data = json.loads(INSTALL.read_text(encoding="utf-8"))
        self.assertEqual(data["$schema"], "https://opencode.ai/config.json")
        self.assertIsInstance(data["instructions"], list)
        self.assertTrue(data["instructions"], "instructions must name the index file")

        server = data["mcp"]["servers"]["mem"]
        self.assertEqual(server["type"], "local")
        self.assertTrue(server["command"][0].endswith("bin/mem"), server["command"])
        self.assertEqual(server["command"][-1], "mcp")
        self.assertTrue(server["enabled"])

    def test_mcp_fragment_uses_the_same_servers_shape(self):
        """The generic MCP fragment drifted to `mcp.mem` once; pin both shapes."""
        data = json.loads((REPO / "install" / "mcp.json").read_text(encoding="utf-8"))
        opencode = data["opencode"]["mcp"]["servers"]["mem"]
        self.assertEqual(opencode["type"], "local")
        self.assertTrue(opencode["command"][0].endswith("bin/mem"))
        self.assertEqual(opencode["command"][-1], "mcp")

        claude = data["claudeCode"]["mcpServers"]["mem"]
        self.assertTrue(claude["command"].endswith("bin/mem"))
        self.assertEqual(claude["args"], ["mcp"])

    def test_instructions_point_at_the_global_index(self):
        data = json.loads(INSTALL.read_text(encoding="utf-8"))
        self.assertTrue(
            any(entry.endswith("MEMORY.md") for entry in data["instructions"]),
            data["instructions"],
        )


class PluginSourceTests(unittest.TestCase):
    def test_node_check_when_node_is_available(self):
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        proc = subprocess.run(
            [node, "--check", str(PLUGIN)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_shell_outs_use_expected_mem_subcommands(self):
        source = PLUGIN.read_text(encoding="utf-8")
        commands = SHELLOUT.findall(source)

        index = [c for c in commands if re.search(r"\bindex\b", c)]
        self.assertTrue(index, commands)
        self.assertIn("--cwd", index[0])

        extract = [c for c in commands if re.search(r"\bextract\b", c)]
        self.assertTrue(extract, commands)
        command = extract[0]
        for flag in ("--transcript", "--agent opencode", "--session", "--cwd"):
            self.assertIn(flag, command)

    def test_hooks_are_the_v2_equivalents(self):
        source = PLUGIN.read_text(encoding="utf-8")
        # V1 names would mean the plugin cannot load in opencode 2.x.
        self.assertNotIn('"experimental.session.compacting"', source)
        self.assertNotIn('"event":', source)
        self.assertIn('hook("compaction"', source)
        self.assertIn("ctx.event.subscribe", source)
        self.assertIn("session.idle", source)

    def test_no_invented_session_start_hook(self):
        source = PLUGIN.read_text(encoding="utf-8")
        # opencode has no session-start context channel; a hook key named for
        # one would be an invented API.
        self.assertNotRegex(source, r'["\']session[.-]start["\']\s*:')


if __name__ == "__main__":
    unittest.main()
