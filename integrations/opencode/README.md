# opencode integration

opencode V2 plugin + config fragment. Every memory operation shells out to the `mem` CLI;
there is no memory logic in this directory. V1 plugin objects do not run in V2.

Install:

1. Put the repo's `bin/` on PATH (or set `MEM_BIN=/abs/path/to/bin/mem`), so `mem` is callable.
2. Copy `mem.js` to `~/.config/opencode/plugins/mem.js` (global) or `.opencode/plugins/mem.js` (project).
3. Merge `install/opencode.json` into `~/.config/opencode/opencode.json`: it adds the global index to
   `instructions` and the `mem mcp` stdio server under `mcp.servers`. Set `MEM_ROOT` in opencode's
   environment if the store is not at `~/.agents/memory`; the plugin reads the same variable.
4. Restart opencode. The `memory_read` / `memory_write` / `memory_search` tools appear, and
   the plugin injects the index into the compaction context (capped by
   `context.index_max_lines`).

**There is no session-start hook.** opencode has no session-start channel that can inject context, so
the index is not loaded when a session begins; recall is tool-driven through the MCP tools. Do not add
such a hook. The plugin does no background work: besides the tools it only adds the compaction-time
index, because automatic extraction was removed on 2026-10-28.
