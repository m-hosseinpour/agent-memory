# Handoff

Current state of the memory store and what a fresh session needs to know.

## What exists

`mem` (this repository, installed with `uv tool install .` as `mem` on PATH) is a shared
markdown memory store: one note per file, a `MEMORY.md` index per scope, a `global` scope and
one scope per working directory. Agents reach it through **one** MCP server, `mem mcp`; the
CLI is the same code for a human or a script.

Tools: `memory_index` (the table of contents), `memory_read` (one note), `memory_write` (save
one). There is no search, no cap on the index, no hooks, no background pass.

Wiring on this machine: `~/.claude.json` (user-scope MCP server `mem`), `~/.config/opencode/opencode.json`,
and `~/.dsh/cordis.patch.yml` (an `@deepseek-ai/dsh-mcp-client` row). The store lives at
`~/.agents/memory`.

## Decisions that were made in review (2026-10-28) and must not be undone without a reason

- **No keyword recall, no `select_threshold`.** The index is complete and is the only retrieval
  path; the agent reads a note body when a line applies.
- **No line budget on the index.** Truncating it would hide notes.
- **Consolidation runs after every change**, not on a session counter. No `.state/`, no lock
  file, no `dream.log`. Deletion is guarded by re-checking the file's version; concurrent
  writers leave both files for the next pass. `write`/`delete` do take a short `flock` on the
  scope directory — an inode lock with no file behind it, released when the process exits.
- **No git history** in the store. A wrong consolidation is a manual rewrite, accepted as the
  price of a store with no runtime state.
- **MCP only.** No hooks, no per-agent plugin. `instructions` in the MCP handshake is the
  onboarding text; `AGENTS.md` must not carry a memory section.
- **`PROTOCOL.md` is not part of onboarding.** It exists for connecting an agent that has no
  MCP client, by hand.
- **No config file.** `MEM_ROOT` is the only input.

Removed earlier and not to be re-added: end-of-session extraction (P2), per-prompt injection,
the symlink bridge to Claude Code's native memory (its backend refuses symlinked paths).

## Gotchas

- `MEM_ROOT` must be exported when testing; otherwise writes land in the real `~/.agents/memory`.
- `mem` on PATH is an installed copy: after changing this checkout, run
  `uv tool install --reinstall .` or the agents keep running the old code.
- The MCP server resolves `project` from its own cwd (or `MEMORY_PROJECT_CWD`), unless a caller
  passes `cwd`. DSH runs one server for several projects, so its model should pass `cwd`.
- Consolidation merges notes whose *descriptions* are near-identical. Two notes about the same
  thing with different wording stay separate on purpose; a rename plus a contradiction is not
  detected.
- `MEMORY.md` is rewritten atomically; a half-written index is never served.

## How to verify

```bash
export MEM_ROOT=$(mktemp -d)
mem write --scope project --cwd "$PWD" --path a.md --name shared --description "a note" \
  --if-version new --content "body"
mem write --scope project --cwd "$PWD" --path b.md --name shared --description "a note" \
  --if-version new --content "body"
ls "$MEM_ROOT"/projects/*/          # a.md is gone: consolidation ran on write
mem index --cwd "$PWD"

printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}' \
  | mem mcp | python3 -m json.tool | grep -A3 instructions
```

For the agents: `claude mcp list` should show `mem` connected, opencode lists it under `mcp`,
and DSH has the `mcp-mem` row in `~/.dsh/cordis.patch.yml`.
