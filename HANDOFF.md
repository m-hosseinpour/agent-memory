# Handoff

Written at the end of a session that did the research + design + P0. A fresh session should
be able to start here with no other context.

## Read these first

1. `~/.dsh/memory-system/design.md` — the design being implemented (short: ~250 lines).
2. `~/.dsh/memory-system/research.md` — how Claude Code and Qoder actually work, and which
   claims are verified vs unverified. Read this before assuming anything about their APIs.
3. `README.md` here — how to run the CLI.
4. `memlib/store.py` and `memlib/cli.py` — the interface every adapter consumes.

## What is done (P0)

- `memlib/store.py`: markdown memory files with Claude-Code-shaped frontmatter
  (`name`, `description`, `metadata.{node_type,type,originSessionId,modified}`), content-hash
  version tokens (12 hex), optimistic concurrency (`if_version`, `new`, `ConflictError`
  carrying current content), atomic writes, secret refusal, 100KB cap, lexical recall.
- `memlib/index.py`: `MEMORY.md` regenerated from files (grouped by type), budget capping
  (200 lines / 25KB).
- `memlib/config.py`: root (`MEM_ROOT` or `~/.agents/memory`), `config.toml` subset
  (`context.index_max_lines`, `recall.select_threshold`, `recall.top_k`, `extract.*`,
  `dream.every_sessions`), scope directories.
- `memlib/cli.py`: `write`, `read`, `list`, `forget`, `index`, `recall` implemented;
  `hook`, `mcp`, `dream`, `extract` are wired to modules that do not exist yet (they raise
  ImportError until written — that is expected).
- `tests/test_store.py`: 20 tests, all passing.

## Done in P1 — the four adapters

All four were implemented, tested, and reviewed. `python3 -m unittest discover -s tests`
runs 96 tests, all passing. Files: `memlib/hooks.py`, `memlib/mcp.py`,
`integrations/opencode/`, `integrations/dsh/`, and three `install/*.json` fragments.

Two deviations from the briefs below, both verified against the real host, **do not undo**:

- **opencode is V2.** The `{ id, server }` + `event` + `experimental.session.compacting`
  shape in brief C is the V1 API, and V1 plugins do not run in V2. opencode 2.1.4 is what is
  installed here; its plugins are `{ id, setup(ctx) }`, `ctx.event.subscribe()`, and
  `ctx.session.hook("compaction", …)` appending to `event.system`. The migration guide says
  so explicitly.
- **The `cordis-plugin-development` skill named in brief D is not installed.** Conventions
  came from the working plugins in `~/.dsh/plugins/` and the runtime `.d.ts` files instead.

Briefs kept for reference. Each one said: implement against `memlib/store.py` +
`memlib/index.py`, keep stdlib only, add `unittest` tests next to the code, and do not
modify the four P0 modules without saying so.

### A. Hooks adapter — `memlib/hooks.py`, `tests/test_hooks.py`, `install/claude-code.json`

- `run(event)` for `session-start`, `user-prompt`, `stop`, `session-end`.
- Input: hook JSON on stdin (`session_id`, `cwd`, `hook_event_name`, `transcript_path`).
- Output: one JSON object on stdout:
  `{"hookSpecificOutput": {"hookEventName": "...", "additionalContext": "..."}}`.
- `session-start`: `mem index`-equivalent block (global + project), capped; also bump the
  session counter in `.state/state.json`.
- `user-prompt`: project+global recall for the prompt; print nothing when nothing matches.
- `stop`: spawn `mem extract` detached (subprocess, no wait), never block the turn.
- `session-end`: flush the counter; no output (the JSON is discarded by the host).
- Hard limits: `additionalContext` ≤ 10,000 chars; total runtime < 150ms; exit 0 on every
  error path (a hook that fails must not break the agent's turn).
- `install/claude-code.json`: the three hook entries to merge into `~/.claude/settings.json`.

### B. MCP server — `memlib/mcp.py`, `tests/test_mcp.py`, `install/mcp.json`

- stdio JSON-RPC 2.0: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`,
  and `ping`. Newline-delimited JSON. **Nothing but protocol JSON on stdout** (log to stderr).
- Tools: `memory_read(store, path)`, `memory_write(store, path, content, if_version, name?,
  description?, type?)`, `memory_search(query, top_k?)`.
- `store` is `global` or `project`; project resolves from `cwd` if the tool accepts it,
  otherwise from `MEMORY_PROJECT_CWD` set by the caller's config.
- Errors: return `isError: true` with a readable message; conflict must return the current
  content and version so the model can merge.
- `install/mcp.json`: the server entry for `~/.claude/settings.json`-style config and for
  opencode.

### C. opencode integration — `integrations/opencode/`, `install/opencode.json`

- Plugin in opencode's shape (`export default { id, server }`, hooks `event` and
  `experimental.session.compacting`), calling the `mem` CLI via `$`.
- `event` on `session.idle` → `mem extract` (when enabled); `experimental.session.compacting`
  → push the index block into `output.context` so it survives compaction.
- `install/opencode.json`: the `instructions[]` + `mcp` fragment to merge into
  `~/.config/opencode/opencode.json`.
- Note in a comment: opencode has **no** session-start context channel, so recall there is
  tool-driven. Do not invent one.

### D. DSH plugin — `integrations/dsh/`, plus a test that the module loads

- Load the `cordis-plugin-development` skill first; follow its conventions exactly.
- Plugin registers: a system-prompt section with the index block, an `agent/pre-step`
  listener that appends recall lines, an `agent/turn-stopping` listener that runs
  `mem extract` detached, and tools `memory_read`/`memory_write`/`memory_search`.
- Everything shells out to `mem`; no memory logic in the plugin.
- Include `cordis.patch.yml` and a short README with the install steps.

## What is next

The adapters shell out to `mem dream` and `mem extract`, which do not exist yet, so the
extraction hooks currently fail silently. Those two are the next work.

1. `memlib/dream.py` + tests: session counter from `.state/state.json`, threshold
   `dream.every_sessions`, pid lockfile, merge duplicates (same type + near-identical
   description), replace contradicted entries (newer wins), drop index lines whose file is
   gone, append to `dream.log`, `git commit` before/after when the store is a repo.
   Must be idempotent: a second run produces no diff.
2. `memlib/extract.py` + tests: read a transcript (Claude Code `.jsonl`, DSH session store),
   one LLM call through a **configurable command** (`extract.command`, e.g. `claude -p`),
   strict JSON output, validate + secret-scan, then write via `store.write`. Default off.
3. `tests/test_cli.py`: lock the CLI contract (exit codes, JSON shape, conflict payload).
4. `PROTOCOL.md` in the store root + the few lines to add to `~/.agents/AGENTS.md`.
5. ~~MCP protocol era~~ — done. `memlib/mcp.py` answers the handshake (`initialize`,
   2025-11-25) and the modern `server/discover` (2026-07-28) probe, and advertises both,
   so a modern-only client is no longer locked out.

The store counter that dream needs already exists in `memlib/hooks.py`
(`session_count`, `bump_session`, `reset_sessions`) — import it, do not re-implement it.

## Decisions already made (do not re-litigate without a reason)

- **No symlink bridge.** The store is not wired into Claude Code's memory directory. Its
  native memory is turned off with `"autoMemoryEnabled": false` (verified key: "When false,
  Claude will not read from or write to the auto-memory directory"). `CLAUDE.md` text alone
  cannot remove a tool — it only steers.
- **No candidate queue, no review queue, no SQLite, no scoring formula.** Writes are
  immediate; quality control is the extraction prompt plus dream.
- **Schema is Claude Code's schema.** Four types only (`user`, `feedback`, `project`,
  `reference`). No extra frontmatter fields.
- **Recall is lexical** (token overlap on name+description) and only runs when the store is
  bigger than `select_threshold`. An LLM selector is a later option, not v1.
- **Python 3, stdlib only.** Tests are `unittest`.

## Gotchas hit in P0

- `MEM_ROOT` must be exported when testing, or writes land in the real `~/.agents/memory`.
  Always `export MEM_ROOT=/tmp/...` in test shells.
- The on-disk `MEMORY.md` uses bare filenames (relative to itself); the injected block uses
  root-relative links (`projects/<slug>/x.md`); `recall` prints absolute paths. Three
  different renderings on purpose — do not "unify" them.
- `store.recall` sorts with a filename tiebreaker; comparing `Memory` objects raises.
- Frontmatter parsing is a YAML subset: scalars plus one nesting level. If an agent writes
  something richer, it will be ignored, not crash.
