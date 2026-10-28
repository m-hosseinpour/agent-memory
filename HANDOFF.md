# Handoff

Started from the research + design, then P0, P1 and P2. A fresh session should be able to
start here with no other context.

P2 was committed as `0b83b0d`, independently reviewed, and the review's findings were fixed
in a follow-up commit (see "Review of P2 and what changed"). The store is installed and
wired into all four agents — Claude Code, opencode, and the DSH plugin (linked into the
`web` profile as a package and a selected bundle). The remaining work is a design decision
(which model drives extraction), not an install step.

The background documents now live in `docs/` in this repository; the old
`~/.dsh/memory-system/` directory was migrated there and deleted.

## Read these first

1. `docs/design.md` — the design being implemented (short: ~250 lines).
2. `docs/research.md` — how Claude Code and Qoder actually work, and which
   claims are verified vs unverified. Read this before assuming anything about their APIs.
3. `README.md` here — how to run the CLI, the config keys, and the install steps.
4. `memlib/store.py`, `memlib/index.py`, `memlib/config.py` and `memlib/cli.py` — the
   interface every adapter consumes.

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
- `memlib/cli.py`: every subcommand is now implemented — `write`, `read`, `list`, `forget`,
  `index`, `recall`, `hook`, `mcp`, `dream`, `extract`.
- `tests/test_store.py`: 20 tests, all passing.

## Done in P1 — the four adapters

All four were implemented, tested, and reviewed. `python3 -m unittest discover -s tests`
ran 96 tests, all passing at that point (168 after P2). Files: `memlib/hooks.py`,
`memlib/mcp.py`, `integrations/opencode/`, `integrations/dsh/`, and three
`install/*.json` fragments.

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

## Done in P2 — extraction and consolidation

`mem extract` and `mem dream` exist, so the adapters' detached calls now do something.
`python3 -m unittest discover -s tests` runs 168 tests, all passing.

- **`memlib/dream.py`** — `mem dream`: session-counter gate (`dream.every_sessions`;
  `--force` bypasses it), a `flock` on `.state/dream.lock` (released by the kernel, so a crash
  cannot wedge later passes), same-name+type replacement (newer `modified` wins, bodies are
  *not* merged),
  near-duplicate merging (same type, description word overlap ≥ 0.8, unique lines of the
  older body appended to the newest), `MEMORY.md` repair, one line per run in `dream.log`,
  and git commits before/after when the store root is its own repository (`.state/` and
  `dream.log` are gitignored, and untracked once if they were already committed). Fully
  deterministic, and idempotent: a second run makes no diff and no commit. The SessionStart
  hook now spawns it detached once the counter reaches the threshold.
- **`memlib/extract.py`** — `mem extract`: config gate (`enabled`, default false), then
  `extract.command` with the prompt on **stdin**, a 20 s timeout, and the project as cwd.
  Strict-JSON output, validation, secret refusal, and create-only writes through
  `store.write`. Incremental: `.state/extract.json` stores a byte offset per transcript (max
  `seq` for SQLite), so the per-turn Stop hook never re-sends the whole session; a tail under
  200 rendered characters waits for the next turn and does not advance. Readers: Claude Code
  `.jsonl`, DSH `session*.jsonl[.zstd]` (`compression.zstd` on 3.14+, else the `zstd` CLI),
  and opencode 2.x SQLite (`session_message`). `--transcript` is now optional — DSH and
  Claude sessions resolve by `--session` when the plugin could not find a file.
- **`tests/test_cli.py`** — the CLI contract: exit codes 0/1/2/3, the conflict payload
  (`ok/error/message/current.{version,content,raw}`), one-JSON-line output, text output for
  `index`/`recall`, `--root`, and the disabled / below-threshold no-op shapes.
- **`PROTOCOL.md`** (repo root) and **`install/agents-md.md`** — the agent-facing contract and
  the lines to append to `~/.agents/AGENTS.md`. They are written; applying them is part of
  the install step below.

Note on P0/P1 docs above: the "expected ImportError" comment and the P1 test count describe
the tree as it was then. The suite is 175 tests now (the 7 added in `872d0bf` came after
P2 and the counts in this file were not swept then).

## Install state (2026-10-28)

Done, on this machine:

- `~/.agents/memory` exists with `git init`, `PROTOCOL.md`, `config.toml` (extraction off)
  and an empty `global/MEMORY.md`; bootstrap commit `0481121`.
- `~/.agents/AGENTS.md` carries the "Shared memory" block from `install/agents-md.md`
  (Claude Code imports that file; opencode lists it in `instructions`).
- `~/.claude/settings.json`: `autoMemoryEnabled: false`, plus `SessionStart`,
  `UserPromptSubmit`, `Stop` and `SessionEnd` hooks. Existing hooks were appended to, not
  replaced. Backups: `*.bak-20261028-123916`.
- Claude Code user-scope MCP server `mem` (`claude mcp add-json ... --scope user`), reported
  `✔ Connected`.
- `~/.config/opencode/opencode.json`: `instructions` gained
  `~/.agents/memory/global/MEMORY.md`; `mcp.servers.mem` added next to searxng/playwright.
- **DSH plugin installed** into the `web` profile: `~/.dsh/plugins/memory` is a symlink to
  `integrations/dsh`, `dsh plugin --profile web add link:...` wrote the dependency,
  and it appended `dsh-memory` to `dsh.profile.bundles` itself (the plugin manager does
  this when the package declares `dsh.bundle.patch` — the README used to say to do it by
  hand). `dsh --profile web --dump-config` shows the composed `memory` row. The running
  harness at `127.0.0.1:3080` was started before this, so a restart loads it there; HMR is
  enabled, so the plugin-manager operation may already have applied.
- Extraction is **off** (`enabled = false`), as decided.

Still to do:

1. **Choose the extraction model** — still the open design decision. `extract.command` is the
   whole switch (e.g. `claude -p`); keep it off until chosen, then sample real sessions for
   noise.
2. Optional, later: `mem eval` / `mem stats`, note-body search (recall is description-only).

## Review of P2 and what changed

An independent review of `0b83b0d` found, and the follow-up commit fixes:

- **Dream overwrote the keeper with `force=True`** and deleted from a stale snapshot, so a
  concurrent agent edit could be lost. Now the keeper write carries `if_version`, and every
  delete re-loads the file first (`_delete_unchanged`); on a conflict nothing is deleted.
  Regression tests: `test_a_concurrent_edit_to_the_keeper_*`, `..._replaced_note_*`.
- **The pid lockfile raced** (empty file window) and could wedge on pid reuse. It is now a
  `flock` on `.state/dream.lock`; the pid is diagnostic only, and the counter is re-checked
  after the lock is taken. The lock file is left in place — unlinking it is its own race.
- **No catch-all in `dream.run`**, so an OSError from `reset_sessions` printed a traceback
  instead of the JSON contract. Now `except Exception` → `{"ok": false, "error": "internal"}`,
  and the user-editable reads use `errors="replace"`.
- **Extraction deduped only in the project scope** while its prompt lists global notes, so a
  global name could reappear as a project duplicate. The exists-check now looks in both
  scopes. Still project-only writes, deliberately (see README).
- **SQLite transcript URI** was string-interpolated and broke on `?`/`#` in the path; now
  `Path.as_uri()`.
- **`every_sessions` had two implementations** (hook vs pass); both call
  `config.every_sessions()` now.
- README/PROTOCOL claims corrected (dream is spawned by the hook; `dream.log` only on runs
  that consolidate; same name *and* type; no-delete via MCP; runtime deps).

**Found by install verification, not by the tests:** the MCP server answered `tools/list`
without `resultType` and without the `ttlMs`/`cacheScope` cache hints, so a 2026-07-28 client
(Claude Code 2.1.301) rejected the tool list and the server showed "Connected · tools fetch
failed". Every result now carries `resultType: "complete"`, `tools/list` and
`server/discover` carry `ttlMs`/`cacheScope`, and `tests/test_mcp.py` pins the shape. This is
worth re-checking with `claude mcp list` after any MCP change.

## Decisions made in P2 (do not undo without a reason)

- **Extraction is create-only.** The model sees names and descriptions, never bodies, so a
  rewrite would be blind; an existing path is skipped. Updates go through `memory_write` or
  dream.
- **Dream's contradiction is identity, not semantics**: same type + same `name`, newer
  `modified` wins; duplicates are description overlap ≥ 0.8. No model call — consistent with
  the "no scoring formula" decision. Rename-and-contradict is not detected, on purpose.
- **The dream trigger lives in the SessionStart hook**, not a daemon: the counter is bumped
  there anyway, and the lock makes repeated spawns harmless.
- **A short extraction tail waits.** Under 200 rendered characters, no model call is made and
  the offset does not move, so several small turns accumulate into one pass.

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

## Gotchas hit in P2

- **DSH session files are zstd-compressed** (`session.v4.jsonl.zstd`) and live in
  `~/.dsh/sessions/<project-key>/<session-dir>/`, where the directory is either
  `session-<uuid>` or `<uuid>`. Read the first line to tell a DSH log from a Claude one: the
  header is `{"type":"session", ...}`; events are `{"type":"user/message",...}` with the body
  inside `data`. `zstd` decoding is `compression.zstd` (Python 3.14) or the `zstd` CLI — do
  not add a dependency for it.
- **opencode 2.x has no transcript file**: the plugin passes its SQLite database
  (`~/.local/share/opencode/opencode.db`) and the session id; messages are rows of
  `session_message(type, seq, data)` with JSON in `data`. Open it read-only.
- **The Stop hook fires every turn**, which is why extraction keeps offsets. Do not "simplify"
  it back to reading the whole transcript.
- **`dream.log` and `.state/` are not memory.** They are gitignored on the first consolidation
  and untracked once if a store already committed them. That is what makes "two runs, no
  diff" true.
- **A test that runs session-start more than `every_sessions` times spawns a real `mem dream`**
  (detached). Patch `memlib.hooks.subprocess.Popen` in those tests.
- `store._tokens` was renamed to the public `store.tokens`; `dream` uses the same
  normalisation for description overlap, so recall and consolidation agree on what a word is.
