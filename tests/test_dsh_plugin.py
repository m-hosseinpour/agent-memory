"""Tests for the DSH plugin: manifest, patch, syntax, and a real module load.

There is no JS test runner in this project, so the load test boots the plugin
against a minimal fake Host context and a fake mem executable. That exercises
the real module: the system-prompt section, both event listeners, and the three
tools, including the exact argv each one shells out with.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN = REPO / "integrations" / "dsh"
INDEX_JS = PLUGIN / "index.js"
PACKAGE_JSON = PLUGIN / "package.json"
PATCH_YML = PLUGIN / "cordis.patch.yml"
README = PLUGIN / "README.md"
NODE = shutil.which("node")

IMPORT_RE = re.compile(r"^\s*import\s+(?:[^'\"\n]*?\s+from\s+)?['\"]([^'\"]+)['\"]", re.M)


def node_env(**overrides: str) -> dict[str, str]:
    env = {**os.environ}
    env.pop("MEM_ROOT", None)
    env.update(overrides)
    return env


def run_node(args, *, env=None, timeout=90):
    return subprocess.run(
        [NODE, *args],
        cwd=REPO,
        env=env or node_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def parse_insert_patch(text: str) -> list[dict[str, str]]:
    """Parse the small "- insert: [{id, name, ...}]" shape without PyYAML.

    Stdlib has no YAML parser and the project is stdlib-only, so this reads the
    exact subset cordis.patch.yml uses. Anything outside that subset is an
    error rather than silently ignored.
    """
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    in_insert = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped == "- insert:":
            in_insert = True
            continue
        if not in_insert:
            raise AssertionError(f"unexpected YAML before - insert: {line!r}")
        if stripped.startswith("- "):
            if current is not None:
                entries.append(current)
            current = {}
            stripped = stripped[2:].strip()
        if current is None:
            raise AssertionError(f"unexpected YAML outside an insert entry: {line!r}")
        key, sep, value = stripped.partition(":")
        if not sep or not key.strip():
            raise AssertionError(f"unexpected YAML line: {line!r}")
        current[key.strip()] = value.strip().strip("'\"")
    if current is not None:
        entries.append(current)
    return entries


@unittest.skipIf(NODE is None, "node is not installed; the DSH plugin cannot be checked")
class SyntaxAndManifestTests(unittest.TestCase):
    def test_index_js_parses(self):
        result = run_node(["--check", str(INDEX_JS)])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_package_json_is_a_dsh_bundle(self):
        manifest = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "dsh-memory")
        self.assertEqual(manifest["type"], "module")
        self.assertEqual(manifest["exports"], {".": "./index.js"})
        self.assertEqual(manifest["dsh"]["bundle"]["patch"], "./cordis.patch.yml")
        self.assertIn("index.js", manifest["files"])
        self.assertIn("cordis.patch.yml", manifest["files"])
        self.assertIn("README.md", manifest["files"])
        # A host-only bundle must not claim a Client half.
        self.assertNotIn("client", manifest["dsh"])

    def test_patch_inserts_the_package(self):
        manifest = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        entries = parse_insert_patch(PATCH_YML.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1, entries)
        self.assertEqual(entries[0]["id"], "memory")
        self.assertEqual(entries[0]["name"], manifest["name"])

    def test_plugin_imports_only_node_builtins(self):
        source = INDEX_JS.read_text(encoding="utf-8")
        specifiers = IMPORT_RE.findall(source)
        self.assertIn("node:child_process", specifiers)
        for specifier in specifiers:
            self.assertTrue(
                specifier.startswith("node:"),
                "plugin must not import "
                + repr(specifier)
                + ": plugins resolve from ~/.dsh/plugins, where @deepseek-ai/* is not installed",
            )
        self.assertNotIn("require(", source)

    def test_readme_documents_install(self):
        text = README.read_text(encoding="utf-8")
        for needle in ("dsh plugin --profile", "dsh.profile.bundles", "restart", "memory_write"):
            self.assertIn(needle, text)

    def test_readme_is_not_a_stub(self):
        self.assertGreater(len(README.read_text(encoding="utf-8")), 1500)


FAKE_MEM = r'''#!/usr/bin/env python3
import json
import os
import sys


def main():
    argv = sys.argv[1:]
    command = argv[0] if argv else ""
    stdin = ""
    if command == "write":
        try:
            stdin = sys.stdin.read()
        except Exception:
            stdin = ""
    record = {
        "argv": argv,
        "cwd": os.getcwd(),
        "stdin": stdin,
        "memRoot": os.environ.get("MEM_ROOT", ""),
    }
    log = os.environ.get("FAKE_MEM_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    if command == "extract":
        marker = os.environ.get("FAKE_EXTRACT_MARKER")
        if marker:
            with open(marker, "w", encoding="utf-8") as handle:
                json.dump(record, handle)
        return 0
    if command == "index":
        sys.stdout.write(os.environ.get("FAKE_INDEX", "") + "\n")
        return 0
    if command == "recall":
        sys.stdout.write(os.environ.get("FAKE_RECALL", "") + "\n")
        return 0
    if command == "read":
        if os.environ.get("FAKE_READ_FAIL"):
            sys.stdout.write(
                '{"ok": false, "error": "conflict", "message": "note.md changed since it was read"}'
            )
            return 2
        sys.stdout.write(os.environ.get("FAKE_READ_OUTPUT", '{"ok": true}') + "\n")
        return 0
    if command == "write":
        sys.stdout.write('{"ok": true, "version": "abc123456789"}\n')
        return 0
    return 0


sys.exit(main())
'''

HARNESS = r'''
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

const env = process.env;
const scenario = env.SCENARIO ?? 'main';
const workdir = env.FAKE_WORKDIR;
const serviceDir = env.FAKE_SERVICE_DIR;

const plugin = await import(pathToFileURL(env.PLUGIN_PATH).href);
assert.equal(typeof plugin.apply, 'function', 'apply must be a named export');
assert.ok(Array.isArray(plugin.inject), 'inject must be exported');
assert.ok(plugin.inject.includes('systemPrompt'), 'inject systemPrompt');
assert.ok(plugin.inject.includes('tools'), 'inject tools');

const events = new Map();
const sections = [];
const tools = new Map();
const labels = [];
const warnings = [];
const agent = { session: { id: 'session-test', header: { cwd: workdir } } };
const signal = new AbortController().signal;

function makeContext(withWorkingDirectory) {
  return {
    logger: {
      warn(message) {
        warnings.push(String(message));
      },
    },
    on(name, listener) {
      events.set(name, listener);
      return () => {};
    },
    effect(factory, label) {
      labels.push(label);
      return factory();
    },
    get(name) {
      if (name === 'workingDirectory' && withWorkingDirectory) {
        return {
          get(session) {
            assert.equal(session, agent.session, 'workingDirectory.get(agent.session)');
            return serviceDir;
          },
        };
      }
      return undefined;
    },
    systemPrompt: {
      section(definition) {
        sections.push(definition);
        return () => {};
      },
      getSectionOrder() {
        return 0;
      },
    },
    tools: {
      register(definition) {
        tools.set(definition.name, definition);
        return () => {};
      },
    },
  };
}

plugin.apply(makeContext(scenario !== 'fallback'), {
  command: env.FAKE_MEM,
  root: env.MEM_ROOT,
  timeoutMs: 5000,
});

assert.equal(labels.length, 4, 'one prompt section and three tools are registered');

// --- system prompt section -------------------------------------------------
assert.equal(sections.length, 1, 'exactly one prompt section');
const section = sections[0];
assert.equal(section.name, 'memory:index');
assert.equal(typeof section.order, 'number');
assert.equal(section.interpolate, false, 'index text must not be interpolated');
const sectionText = section.text({ agent });
assert.ok(
  sectionText.startsWith('## Memory (reference notes)'),
  'section must be marked as reference notes',
);
assert.ok(sectionText.includes(env.FAKE_INDEX.trim()), 'section carries mem index output');

// --- agent/pre-step --------------------------------------------------------
const preStep = events.get('agent/pre-step');
assert.equal(typeof preStep, 'function', 'agent/pre-step listener');
const claimed = scenario === 'empty'
  ? []
  : [{
      id: 'm1',
      role: 'user',
      content: [{ type: 'text', text: env.FAKE_PROMPT }],
      source: { kind: 'user' },
    }];
const decision = await preStep(
  { agent, messages: claimed, turn: 1, step: 1, signal },
  async () => ({ kind: 'enter', messages: claimed }),
);
assert.equal(decision.kind, 'enter');
const appended = decision.messages.length > claimed.length
  ? decision.messages[decision.messages.length - 1]
  : null;
if (appended !== null) {
  assert.equal(appended.role, 'user');
  assert.equal(appended.source.kind, 'memory');
  assert.equal(typeof appended.id, 'string');
  assert.ok(appended.content[0].text.includes(env.FAKE_RECALL.trim()));
}
const rejected = await preStep(
  { agent, messages: claimed, turn: 1, step: 1, signal },
  async () => ({ kind: 'reject' }),
);
assert.equal(rejected.kind, 'reject');
assert.equal(rejected.messages, undefined);

// --- tools -----------------------------------------------------------------
assert.deepEqual([...tools.keys()].sort(), ['memory_read', 'memory_search', 'memory_write']);
const read = tools.get('memory_read');
const write = tools.get('memory_write');
const search = tools.get('memory_search');
assert.equal(read.parameters.required[0], 'path');
assert.equal(write.parameters.required[0], 'path');
assert.equal(search.parameters.required[0], 'query');

let readResult;
try {
  readResult = await read.execute({ path: 'note.md' }, { agent, signal });
} catch (error) {
  if (scenario !== 'failure') throw error;
  readResult = String(error.message);
  assert.ok(readResult.includes('conflict'), 'conflict payload reaches the model: ' + readResult);
}
const writeResult = await write.execute(
  {
    path: 'note.md',
    content: 'body text',
    if_version: 'new',
    name: 'note',
    description: 'a note',
    type: 'project',
  },
  { agent, signal },
);
assert.ok(writeResult.includes('"ok": true'), 'write returns CLI JSON: ' + writeResult);
const searchResult = await search.execute({ query: 'lockfile', top_k: 3 }, { agent, signal });

const records = readFileSync(env.FAKE_MEM_LOG, 'utf8')
  .trim()
  .split('\n')
  .filter(Boolean)
  .map((line) => JSON.parse(line));
const byCommand = (name, second) =>
  records.filter((record) => record.argv[0] === name && (second === undefined || record.argv[1] === second));
const readRecord = byCommand('read').pop() ?? null;
const writeRecord = byCommand('write').pop() ?? null;
const searchRecord = byCommand('recall', 'lockfile').pop() ?? null;
const indexRecord = byCommand('index').pop() ?? null;

if (scenario === 'main') {
  assert.ok(readRecord.argv.includes('note.md'));
  assert.equal(writeRecord.stdin, 'body text', 'content goes over stdin');
  assert.ok(writeRecord.argv.includes('--if-version'));
  assert.ok(writeRecord.argv.includes('--session'));
  assert.ok(searchRecord.argv.includes('--top-k'));
}

// --- agent/turn-stopping ---------------------------------------------------
let extract = null;
if (scenario === 'main') {
  const turnStopping = events.get('agent/turn-stopping');
  assert.equal(typeof turnStopping, 'function', 'agent/turn-stopping listener');
  turnStopping({ agent, turn: 1, signal });
  const marker = env.FAKE_EXTRACT_MARKER;
  const deadline = Date.now() + 3000;
  while (!existsSync(marker) && Date.now() < deadline) {
    await new Promise((done) => setTimeout(done, 25));
  }
  assert.ok(existsSync(marker), 'detached mem extract ran');
  extract = JSON.parse(readFileSync(marker, 'utf8'));
}

console.log(
  JSON.stringify({
    section: { name: section.name, order: section.order, interpolate: section.interpolate },
    sectionText,
    tools: [...tools.keys()].sort(),
    typeEnum: write.parameters.properties.type.enum,
    preStep: { count: decision.messages.length, appended },
    rejected: { kind: rejected.kind, hasMessages: 'messages' in rejected },
    readResult,
    writeResult,
    searchResult,
    readRecord,
    writeRecord,
    searchRecord,
    indexRecord,
    extract,
    labels,
    warnings,
  }),
);
'''


@unittest.skipIf(NODE is None, "node is not installed; the DSH plugin cannot be checked")
class PluginLoadTests(unittest.TestCase):
    """Load the real module against a fake Host context and a fake mem CLI."""

    @classmethod
    def setUpClass(cls):
        cls.scratch = Path(tempfile.mkdtemp(prefix="dsh-plugin-test-"))
        cls.store = cls.scratch / "store"
        cls.store.mkdir()
        cls.workdir = cls.scratch / "project"
        cls.workdir.mkdir()
        cls.service_dir = cls.scratch / "project-moved"
        cls.service_dir.mkdir()
        cls.mem = cls.scratch / "fake_mem.py"
        cls.mem.write_text(FAKE_MEM, encoding="utf-8")
        cls.mem.chmod(0o755)
        cls.harness = cls.scratch / "harness.mjs"
        cls.harness.write_text(HARNESS, encoding="utf-8")
        cls.log = cls.scratch / "mem.log"
        cls.marker = cls.scratch / "extract.json"
        cls.dsh_home = cls.scratch / "dsh-home"
        cls.transcript = (
            cls.dsh_home
            / "sessions"
            / "--tmp-project--"
            / "session-test"
            / "session.v4.jsonl.zstd"
        )
        cls.transcript.parent.mkdir(parents=True)
        cls.transcript.write_text("", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.scratch, ignore_errors=True)

    def run_harness(self, scenario: str, **overrides: str) -> dict:
        # Each run observes only its own calls: a stale marker or log would let a
        # test pass without the plugin having done anything.
        self.log.unlink(missing_ok=True)
        self.marker.unlink(missing_ok=True)
        env = node_env(
            SCENARIO=scenario,
            PLUGIN_PATH=str(INDEX_JS),
            FAKE_MEM=str(self.mem),
            FAKE_WORKDIR=str(self.workdir),
            FAKE_SERVICE_DIR=str(self.service_dir),
            FAKE_MEM_LOG=str(self.log),
            FAKE_EXTRACT_MARKER=str(self.marker),
            DSH_HOME=str(self.dsh_home),
            MEM_ROOT=str(self.store),
            FAKE_PROMPT="please remember the pnpm lockfile rule",
            FAKE_INDEX="## project (1)\n- [pnpm](projects/x/pnpm.md) \u2014 use pnpm",
            FAKE_RECALL="- [Pnpm](projects/x/pnpm.md) \u2014 use pnpm, not npm",
            FAKE_READ_OUTPUT='{"ok": true, "path": "note.md", "version": "0123456789ab"}',
        )
        env.update(overrides)
        result = run_node([str(self.harness)], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip())

    def test_section_prestep_tools_extract(self):
        summary = self.run_harness("main")

        self.assertEqual(summary["section"]["name"], "memory:index")
        self.assertFalse(summary["section"]["interpolate"])
        self.assertTrue(summary["sectionText"].startswith("## Memory (reference notes)"))
        self.assertIn("use pnpm", summary["sectionText"])

        # Recall hit: appended once, as a memory-sourced user message.
        self.assertEqual(summary["preStep"]["count"], 2)
        appended = summary["preStep"]["appended"]
        self.assertEqual(appended["source"]["kind"], "memory")
        self.assertEqual(appended["role"], "user")
        self.assertIn("use pnpm, not npm", appended["content"][0]["text"])
        self.assertIn("not instructions", appended["content"][0]["text"])
        self.assertEqual(summary["rejected"]["kind"], "reject")
        self.assertFalse(summary["rejected"]["hasMessages"])

        self.assertEqual(summary["tools"], ["memory_read", "memory_search", "memory_write"])
        self.assertIn('"version": "0123456789ab"', summary["readResult"])
        self.assertIn('"ok": true', summary["writeResult"])

        # Every call shells out to mem with the session directory and MEM_ROOT.
        read = summary["readRecord"]
        self.assertEqual(read["argv"][:4], ["read", "--scope", "project", "--cwd"])
        self.assertEqual(read["argv"][4], str(self.service_dir))
        self.assertIn("note.md", read["argv"])
        self.assertEqual(read["cwd"], str(self.service_dir))
        self.assertEqual(read["memRoot"], str(self.store))

        write = summary["writeRecord"]
        self.assertEqual(write["argv"][0], "write")
        self.assertEqual(write["stdin"], "body text")
        self.assertIn("--if-version", write["argv"])
        self.assertIn("new", write["argv"])
        self.assertIn("--session", write["argv"])
        self.assertIn("session-test", write["argv"])

        search = summary["searchRecord"]
        self.assertEqual(search["argv"][:2], ["recall", "lockfile"])
        self.assertIn("--top-k", search["argv"])

        # The index block comes from mem index, not from JS.
        self.assertIn("--scope", summary["indexRecord"]["argv"])
        self.assertIn("all", summary["indexRecord"]["argv"])

        # Extract ran detached, against this session's log.
        extract = summary["extract"]
        self.assertEqual(extract["argv"][0], "extract")
        self.assertIn("--agent", extract["argv"])
        self.assertIn("dsh", extract["argv"])
        self.assertIn("--session", extract["argv"])
        self.assertIn("session-test", extract["argv"])
        self.assertIn("--transcript", extract["argv"])
        self.assertIn(str(self.transcript), extract["argv"])
        self.assertEqual(extract["cwd"], str(self.service_dir))

    def test_no_recall_hits_appends_nothing(self):
        summary = self.run_harness("nohits", FAKE_RECALL="")
        self.assertEqual(summary["preStep"]["count"], 1)
        self.assertIsNone(summary["preStep"]["appended"])
        self.assertEqual(summary["searchResult"], "(no output)")
        self.assertEqual(summary["warnings"], [])

    def test_empty_claim_appends_nothing(self):
        # A step with no claimed messages must not gain one: the loop would then
        # never close the turn.
        summary = self.run_harness("empty")
        self.assertEqual(summary["preStep"]["count"], 0)
        self.assertIsNone(summary["preStep"]["appended"])

    def test_tool_error_carries_the_cli_payload(self):
        summary = self.run_harness("failure", FAKE_READ_FAIL="1")
        self.assertIn("conflict", summary["readResult"])
        self.assertIn("changed since it was read", summary["readResult"])

    def test_falls_back_to_the_session_header_cwd(self):
        summary = self.run_harness("fallback")
        read = summary["readRecord"]
        self.assertEqual(read["argv"][4], str(self.workdir))
        self.assertEqual(read["cwd"], str(self.workdir))


REAL_HARNESS = r'''
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';

const plugin = await import(pathToFileURL(process.env.PLUGIN_PATH).href);
const events = new Map();
const sections = [];
const tools = new Map();
const warnings = [];
const project = process.env.PROJECT;
const agent = { session: { id: 'session-real', header: { cwd: project } } };
const signal = new AbortController().signal;
const ctx = {
  logger: { warn: (m) => warnings.push(String(m)) },
  on(name, fn) { events.set(name, fn); return () => {}; },
  effect(factory) { return factory(); },
  get() { return undefined; },
  systemPrompt: { section(d) { sections.push(d); return () => {}; }, getSectionOrder: () => 0 },
  tools: { register(d) { tools.set(d.name, d); return () => {}; } },
};
plugin.apply(ctx, {
  command: process.env.MEM_BIN,
  root: process.env.MEM_ROOT,
  timeoutMs: 10000,
});

const sectionText = sections[0].text({ agent });
assert.ok(sectionText.includes('pnpm'), 'index carries the real memory: ' + sectionText);

const claimed = [{
  id: 'm1',
  role: 'user',
  content: [{ type: 'text', text: 'what about the lockfile?' }],
  source: { kind: 'user' },
}];
const decision = await events.get('agent/pre-step')(
  { agent, messages: claimed, turn: 1, step: 1, signal },
  async () => ({ kind: 'enter', messages: claimed }),
);
assert.equal(decision.messages.length, 2, 'real mem recall appended a line');

const read = JSON.parse(await tools.get('memory_read').execute({ path: 'pnpm.md' }, { agent, signal }));
assert.equal(read.description, 'the lockfile is pnpm');
const written = await tools.get('memory_write').execute(
  {
    path: 'pnpm.md',
    content: 'The lockfile is pnpm. Never run npm install.',
    if_version: read.version,
    description: 'the lockfile is pnpm',
  },
  { agent, signal },
);
assert.ok(written.includes('"ok": true'), 'real write: ' + written);
const search = await tools.get('memory_search').execute({ query: 'lockfile', top_k: 5 }, { agent, signal });
assert.ok(search.includes('pnpm.md'), 'real search: ' + search);

// A stale version must surface the CLI's conflict payload, current content included.
await assert.rejects(
  () => tools.get('memory_write').execute(
    { path: 'pnpm.md', content: 'second', if_version: read.version },
    { agent, signal },
  ),
  (error) =>
    error.message.includes('conflict') &&
    error.message.includes('Never run npm install'),
);

// The detached extract path is covered by the fake-host test; this one stays
// synchronous so no stray mem process outlives the scratch store.

console.log(JSON.stringify({ sectionText, recallText: decision.messages[1].content[0].text, search, warnings }));
'''


@unittest.skipIf(NODE is None, "node is not installed; the DSH plugin cannot be checked")
class RealCliIntegrationTests(unittest.TestCase):
    """Drive the plugin against the frozen mem CLI, not a stub."""

    def test_roundtrip_through_the_frozen_cli(self):
        mem_bin = REPO / "bin" / "mem"
        if not mem_bin.exists() or not os.access(mem_bin, os.X_OK):
            self.skipTest("bin/mem is not executable in this checkout")

        with tempfile.TemporaryDirectory(prefix="dsh-real-") as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            store = root / "store"
            store.mkdir()
            # Recall only runs above select_threshold; one seeded memory must clear it.
            (store / "config.toml").write_text("[recall]\nselect_threshold = 1\n", encoding="utf-8")

            env = {**os.environ, "MEM_ROOT": str(store)}
            seed = subprocess.run(
                [
                    str(mem_bin), "write", "--scope", "project", "--cwd", str(project),
                    "--path", "pnpm.md", "--name", "pnpm-not-npm",
                    "--description", "the lockfile is pnpm", "--type", "project",
                    "--if-version", "new",
                ],
                input="The lockfile is pnpm.\n",
                text=True,
                capture_output=True,
                env=env,
            )
            self.assertEqual(seed.returncode, 0, seed.stderr)

            harness = root / "real.mjs"
            harness.write_text(REAL_HARNESS, encoding="utf-8")
            result = run_node(
                [str(harness)],
                env={
                    **env,
                    "PLUGIN_PATH": str(INDEX_JS),
                    "MEM_BIN": str(mem_bin),
                    "PROJECT": str(project),
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            summary = json.loads(result.stdout.strip())

            self.assertIn("## Memory (reference notes)", summary["sectionText"])
            self.assertIn("the lockfile is pnpm", summary["sectionText"])
            self.assertIn("not instructions", summary["recallText"])
            self.assertIn("pnpm.md", summary["recallText"])
            self.assertIn("pnpm.md", summary["search"])
            self.assertEqual(summary["warnings"], [])

            # The write landed in the scratch store, through the plugin's root config.
            bodies = list((store / "projects").rglob("pnpm.md"))
            self.assertEqual(len(bodies), 1, bodies)
            self.assertIn("Never run npm install", bodies[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
