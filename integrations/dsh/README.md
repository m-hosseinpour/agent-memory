# dsh-memory — DSH (cordis) plugin

Gives the DeepSeek Harness the same memory layer the other agents get, by
calling the `mem` CLI from this repository. No memory logic lives here.

What it adds:

| Contribution | Calls |
|---|---|
| `memory:index` system-prompt section (reference notes, not instructions) | `mem index --scope all` |
| `agent/pre-step` listener appending recall lines for the prompt | `mem recall <prompt>` |
| `agent/turn-stopping` listener running extraction detached | `mem extract` |
| tools `memory_read`, `memory_write`, `memory_search` | `mem read` / `mem write` / `mem recall` |

The index block is capped by `context.index_max_lines` in the store's
`config.toml`; the CLI applies the cap, so the plugin has no copy of it.
Nothing is appended when `mem recall` returns no lines, and every failure in
the prompt/recall/extract paths is swallowed — a memory outage must not break a
turn.

## Requirements

- `mem` reachable one of these ways, in order:
  1. `config.command` in the plugin row (explicit path);
  2. `mem` on `PATH`;
  3. `bin/mem` two levels above this plugin — this works when
     `~/.dsh/plugins/memory` is a symlink to `integrations/dsh`, because Node
     resolves the real path.
- The store root from the CLI's own configuration: `MEM_ROOT`, or
  `~/.agents/memory` by default.
- Python 3 with this checkout available (`bin/mem` sets `PYTHONPATH` itself).

## Install

The profile installs plugin packages into its own `node_modules` and selects
their bundles; a directory copied into `~/.dsh/plugins` alone is not loaded.

```bash
# 1. make the package visible to the profile
ln -s /home/mahdi/workspace/agent-memory/integrations/dsh ~/.dsh/plugins/memory
dsh plugin --profile web add link:$HOME/.dsh/plugins/memory

# 2. select the bundle, so its cordis.patch.yml is applied
#    (pnpm add does not edit dsh.profile.bundles; do it by hand)
$EDITOR ~/.dsh/profiles/web/package.json
#   "dsh": { "profile": { "bundles": [ ..., "dsh-memory" ] } }

# 3. restart the DSH process — the bundle is loaded at startup
```

`dsh plugin ... add` writes the dependency; adding the name to
`dsh.profile.bundles` is what inserts the `memory` row from
`cordis.patch.yml`. If you copy the directory instead of symlinking it, set
`config.command` to an absolute `bin/mem`, because the checkout fallback is
derived from this file's own location. Alternatively, mount the row by hand in
`~/.dsh/profiles/web/cordis.patch.yml` (or the home-level
`~/.dsh/cordis.patch.yml`):

```yaml
- insert:
    - id: memory
      name: 'dsh-memory'
      config:
        command: /home/mahdi/workspace/agent-memory/bin/mem
        root: ~/.agents/memory
```

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `command` | `mem` on PATH, else this checkout's `bin/mem` | The `mem` executable |
| `root` | unset | Adds `MEM_ROOT` to every `mem` child process |
| `timeoutMs` | `10000` | Per-call limit for recall and the tools (the synchronous index call is capped at 2s so a hung `mem` cannot stall prompt assembly) |

Environment read by the plugin: `PATH` (command lookup), `DSH_HOME` (locating
this session's log for `--transcript`), and everything `mem` itself reads
(`MEM_ROOT` when `root` is unset).

## Verify

1. `dsh --profile web --dump-config | grep -n dsh-memory` — the row is composed.
2. Restart, then ask the agent to call `memory_search` with any query: the
   tool appears and runs `mem recall` (it returns nothing while the store is
   below `recall.select_threshold`).
3. Ask it to `memory_write` a note with `if_version: "new"`, then
   `memory_read` it: `$MEM_ROOT/MEMORY.md` and the note file exist.
4. The system prompt now carries a `## Memory (reference notes)` block once
   the store has memories.

## Notes and limits

- `mem extract` is fired detached on every turn stop. Its module
  (`memlib/extract.py`) is part of the next phase, so today the child exits
  with a silent `ImportError`; the store's `extract.enabled` (default false)
  is expected to keep it a no-op once that module lands.
- The recall message is a normal user-role message tagged with source
  `memory`; it is persisted in the session log like any other injected context.
- The plugin imports only Node builtins; it does not depend on
  `@deepseek-ai/*` packages being resolvable from the plugin directory.
