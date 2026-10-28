# agent-memory

A memory store shared by coding agents through one MCP server. Plain markdown on disk, one
note per memory, in Claude Code's note format and indexed by a `MEMORY.md` table of contents
([`docs/research.md`](docs/research.md) §1), so the data stays readable by hand and by `grep`.

The `mem` package holds every rule: the store, the index, consolidation, the MCP server. The
CLI and the MCP server are two front doors to the same code. Any agent with an MCP client
shares the same notes, with no per-agent plugin or hook to install and no instruction file to
maintain.

Background and rationale: [`docs/`](docs/) — `research.md` (how Claude Code and Qoder actually
work, and which claims are verified), `design.md` (the design and the alternatives dropped),
`roadmap.md` (history), and `operations.md` (setup, env vars, and how to verify this machine).

## Status

0.2.0. Retrieval *is* the index: no keyword search, no model call, and nothing injected behind
the agent's back. `memory_index` returns the whole table of contents, `memory_read` loads one
note, and `memory_write` saves one — then consolidates the store before it returns, so there is
no queued state and no background pass.

A rewrite changes only the fields the caller passes: `name`, `description` and `type` keep their
current values when omitted, so fixing a typo in the body cannot move a note to another section
of the index. A new note defaults to the filename stem, an empty description and `project`.

The store lives at `~/.agents/memory` and is wired into Claude Code, opencode and DSH by
registering the MCP server (below). It is installed once with `uv tool install`, so every agent
calls the same `mem` on `PATH` rather than a path inside this checkout.

## Usage

```bash
mem write --scope project --cwd "$PWD" --path pnpm-not-npm.md \
  --name pnpm-not-npm --description "the lockfile is pnpm; npm install breaks CI" \
  --if-version new <<'EOF'
The lockfile is pnpm. `npm install` rewrites it and breaks CI.
EOF

mem read   --scope project --cwd "$PWD" --path pnpm-not-npm.md
mem list   --scope project --cwd "$PWD"
mem index  --cwd "$PWD"                 # the table of contents, both scopes
mem forget --scope project --cwd "$PWD" --path pnpm-not-npm.md
mem dream  --cwd "$PWD"                 # a manual consolidation pass
mem dream  --all                        # every project scope
mem mcp                                 # the MCP server on stdio
```

Exit codes: `0` ok, `1` invalid input, `2` version conflict (the current content is returned so
the caller can merge), `3` content looked like a secret and was refused.

## MCP

Register the server once per agent; there is nothing else to install per agent.

```json
// Claude Code: ~/.claude.json (user scope), or `claude mcp add mem mem mcp --scope user`
{ "mcpServers": { "mem": { "command": "mem", "args": ["mcp"] } } }
```

```json
// opencode: ~/.config/opencode/opencode.json
{ "mcp": { "servers": { "mem": { "type": "local", "command": ["mem", "mcp"], "enabled": true } } } }
```

```yaml
# DSH: ~/.dsh/cordis.patch.yml (rewrite the file live, no restart needed)
- insert:
    - id: mcp-mem
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: mem
        transport: stdio
        command: mem
        args: ['mcp']
```

Tools:

| Tool | Arguments | Returns |
|---|---|---|
| `memory_index` | `cwd?` | the table of contents of the global and the project store |
| `memory_read` | `store`, `path`, `cwd?` | body, version, modified, type, description |
| `memory_write` | `store`, `path`, `content`, `if_version`, `name?`, `description?`, `type?`, `cwd?` | the new version; consolidates the store first |

The server's usage contract — the four types, what not to save, `if_version` — travels in the
MCP `instructions` field of `initialize` / `server/discover`, which is what that field is for.
No `AGENTS.md` section is needed.

`cwd` exists for clients that run one server for several projects (DSH does); a per-project
client can omit it and the server uses its own working directory.

## Install

```bash
uv tool install .                        # puts `mem` on PATH, isolated from the system Python
uv tool install --reinstall .            # after changing this checkout
```

For development, `./bin/mem` runs the checkout directly without installing anything.

The store itself is just a directory: `mkdir -p ~/.agents/memory`, copy `PROTOCOL.md` into it,
and register the MCP server. `MEM_ROOT` points `mem` at a different root (used by the tests).

For Claude Code, also set `"autoMemoryEnabled": false` in `~/.claude/settings.json`: otherwise
its native memory keeps writing beside this store and the two drift apart (see
[`docs/design.md`](docs/design.md) §9–§10).

## Consolidation

Every `mem write`/`mem forget` and every `memory_write` consolidates the scope it touched before
returning. The rules are deterministic — no model call:

- two files of the same type with the same `name` are one note; the newer `modified` wins and
  the older file is removed (its body is **not** merged);
- two files of the same type whose descriptions use the same words are duplicates: word Jaccard
  0.95 over descriptions of at most 19 distinct words, where only an identical word set can reach
  it. The older's unique lines are appended to the newest body, then the older file is removed; a
  description with more distinct words is never merged on similarity;
- `MEMORY.md` is rebuilt from the files, so a line for a deleted file cannot survive.

A pass deletes a file only while its bytes still match what it read, and writes a merged keeper
with the version it read, so a concurrent agent either wins cleanly or leaves both files for the
next pass. The pass is idempotent: running it twice changes nothing. `mem dream` runs it by hand
(`--all` for every project).

A description with more than 19 distinct words is not compared for similarity at all: at that
size the same 0.95 would begin to accept a qualifier, so two notes that long are left apart rather
than guessed at (the same-`name` rule still applies).

A restatement in other words is **not** merged, and rename-and-contradict is **not** detected —
the same fact under a different `name` or in a differently worded `description` stays as two
notes. That is deliberate: one added qualifier ("… tag blue", "… only") is what turns a
duplicate into a different note, and guessing that from text without a model is how the wrong
half of a pair gets deleted.

## Store layout

```
$MEM_ROOT/
├── PROTOCOL.md                # copied here by hand; no code creates it
├── global/{MEMORY.md,*.md}    # user-wide preferences and facts
└── projects/<sanitized-path>/{MEMORY.md,*.md}
```

`<sanitized-path>` is the absolute cwd with every unsafe character replaced by `-`
(`/home/u/proj` → `-home-u-proj`), so each project gets one directory and no path escapes
`$MEM_ROOT`. There is no config file, no runtime state, and no git repository: the store is the
notes and the index.

## Layout

```
bin/mem              launcher for development (python3 -m memlib, no install needed)
memlib/store.py      files, frontmatter, version tokens, conflicts, secret refusal
memlib/index.py      MEMORY.md generation (atomic, no line budget)
memlib/dream.py      consolidation, called after every change and by `mem dream`
memlib/mcp.py        MCP stdio server: memory_index / memory_read / memory_write
memlib/cli.py        argparse surface
PROTOCOL.md          the agent-facing contract, copied into the store root
docs/                research.md, design.md, roadmap.md, operations.md
tests/               unittest (stdlib only)
```

## Tests

```bash
export MEM_ROOT=$(mktemp -d)          # tests refuse to touch the real store
python3 -m unittest discover -s tests -v
```

## Notes

- **No database, no search index.** The index is regenerated from the files by scanning the
  directory, which is cheap at this scale and makes drift impossible.
- **The index has no line budget.** It is the whole retrieval path, so truncating it would hide
  notes rather than save context.
- **`if_version` is mandatory** on write unless `--force` is passed: a blind overwrite is
  exactly what the token exists to prevent.
- **A rewrite keeps what it is not given.** `name`, `description` and `type` are the only
  optional fields; omitting one leaves the file's current value in place, so a body-only fix
  cannot move a note to another section of the index. An empty `--description` clears the
  description; an empty `--name` keeps the current one. The store writes exactly three
  frontmatter keys (`name`, `description`, `metadata`) and drops any other hand-written key.
- **`name` and `description` are one line.** A line break in either is refused before anything
  is written: the frontmatter is parsed line by line, so a value with a break would be cut there.
  Multiple lines belong in the body.
- **Schema is Claude Code's, minus session provenance.** Four types (`user`, `feedback`,
  `project`, `reference`) and the same frontmatter shape, but no `originSessionId`: no client
  hands the tool a session id, so the field was always empty and claimed a provenance the store
  could not have. Notes another tool wrote with that field are still read; the next rewrite drops
  it, while other metadata keys another tool added are left alone.
- **Concurrency rests on the version token, held under a short-lived lock.** `write` and `delete`
  take an `flock` on the scope directory for the read-check-replace, because a token check
  without a lock is check-then-act: two writers holding the same token would both pass. The lock
  is a kernel lock on an inode — no file, no state, and it dies with the process.
- **`mem read` takes a flat `.md` name.** `..`, absolute paths and `MEMORY.md` are refused; the
  generated index is `mem index`. A hand-written file with invalid UTF-8 is decoded with
  replacement characters instead of failing the command.
- **`PROTOCOL.md` is not part of the agent's onboarding.** The MCP `instructions` field is. The
  protocol file is only for connecting an agent that has no MCP client.
- When testing, set `MEM_ROOT` to a scratch directory. The default root is the real store.
