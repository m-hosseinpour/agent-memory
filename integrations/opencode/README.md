# opencode integration

opencode V2 plugin + config fragment. Every memory operation shells out to the `mem` CLI;
there is no memory logic in this directory. V1 plugin objects do not run in V2.

Install:

1. Put the repo's `bin/` on PATH (or set `MEM_BIN=/abs/path/to/bin/mem`), so `mem` is callable.
2. Copy `mem.js` to `~/.config/opencode/plugins/mem.js` (global) or `.opencode/plugins/mem.js` (project).
3. Merge `install/opencode.json` into `~/.config/opencode/opencode.json`: it adds the global index to
   `instructions` and the `mem mcp` stdio server under `mcp.servers`. Set `MEM_ROOT` in opencode's
   environment if the store is not at `~/.agents/memory`; the plugin reads the same variable.
4. Restart opencode. The `memory_read` / `memory_write` / `memory_search` tools appear; the plugin runs
   on `session.idle` (extraction, only when `[extract] enabled = true` in `$MEM_ROOT/config.toml`) and
   before compaction (index into the compaction context, capped by `context.index_max_lines`).

**There is no session-start hook.** opencode has no session-start channel that can inject context, so
the index is not loaded when a session begins; recall is tool-driven through the MCP tools. Do not add
such a hook. `mem extract` receives opencode's SQLite session database (`$XDG_DATA_HOME/opencode/opencode.db`)
plus `--session`, because opencode hands plugins no per-session transcript file; override with `MEM_OPENCODE_DB`.
