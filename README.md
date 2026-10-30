# agent-memory

One markdown memory store shared by every coding agent on your machine.

Notes live as plain markdown on disk — one file per memory, in Claude Code's note format and
indexed by a `MEMORY.md` table of contents ([`docs/research.md`](docs/research.md) §1) — so the
data stays readable by hand, by `cat`, and by `grep`.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

The `mem` package holds every rule: the store, the index, consolidation, the MCP server. The CLI
and the MCP server are two front doors to the same code. Any agent with an MCP client shares the
same notes, with no per-agent plugin or hook to install and no instruction file to maintain.

## How it works

**Retrieval *is* the index.** No keyword search, no model call, and nothing injected behind the
agent's back. `memory_index` returns the whole table of contents, `memory_read` loads one note,
and `memory_write` saves one — then consolidates the store before it returns, so there is no
queued state and no background pass.

A rewrite changes only the fields the caller passes: `name`, `description` and `type` keep their
current values when omitted, so fixing a typo in the body cannot move a note to another section
of the index. A new note defaults to the filename stem, an empty description and `project`.

The store lives at `~/.agents/memory` and is wired into any MCP client by registering the server
(below). It is installed once with `uv tool install`, so every agent calls the same `mem` on
`PATH` rather than a path inside this checkout.

Python 3.11+ and the standard library — no dependencies, on Linux, macOS or Windows.

## Install

Requires [uv](https://docs.astral.sh/uv/) (or any Python installer that can create a console
script) and Python 3.11 or newer.

```bash
uv tool install .                        # from this checkout: puts `mem` on PATH
uv tool install --reinstall .            # after changing this checkout
```

Then register the MCP server in the agents you use — [MCP](#mcp) is the main way the store is
consumed. The CLI below is the same code by hand.

The store itself is just a directory. The first write creates it; to also place the agent
protocol next to the notes:

```bash
mkdir -p ~/.agents/memory
cp PROTOCOL.md ~/.agents/memory/
```

`MEM_ROOT` points `mem` at a different root (used by the tests).

For Claude Code, also set `"autoMemoryEnabled": false` in `~/.claude/settings.json`: otherwise
its native memory keeps writing beside this store and the two drift apart (see
[`docs/design.md`](docs/design.md) §9–§10).

For development, no install is needed — run the checkout directly:

```bash
python -m memlib --help      # every platform, from the checkout root
./bin/mem --help             # Linux/macOS launcher
```

```powershell
bin\mem.cmd --help           # Windows launcher
```

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

The server's usage contract — the two scopes, the four types, what not to save, `if_version` —
travels in the MCP `instructions` field of `initialize` / `server/discover`, which is what that
field is for. It opens with the session bootstrap: call `memory_index` with `cwd` before the
first action, and again after a resume or compaction when the index is no longer in context.
MCP has no channel that pushes the index into a client's context, so this contract is the
portable way to get it in front of the model without an adapter per client. No `AGENTS.md`
section is needed.

`cwd` exists for clients that run one server for several projects (DSH does); a per-project
client can omit it and the server uses its own working directory.

## Manual usage

The CLI is the same store by hand. `--scope` defaults to `project` and `--cwd` to the current
directory, so a command run inside a project needs neither:

```bash
mem write --path pnpm-not-npm.md --name pnpm-not-npm \
  --description "the lockfile is pnpm; npm install breaks CI" --if-version new <<'EOF'
The lockfile is pnpm. `npm install` rewrites it and breaks CI.
EOF

mem read   --path pnpm-not-npm.md
mem list
mem index                     # the table of contents of both scopes
mem forget --path pnpm-not-npm.md
mem dream                     # a manual consolidation pass
mem dream --all               # every project scope
mem migrate --dry-run         # Claude Code notes: preview, write nothing
mem mcp                       # the MCP server on stdio
mem --version
```

Pass `--cwd /path/to/project` to act on another project, and `--scope global` for the global
store.

Exit codes: `0` ok, `1` invalid input, `2` version conflict (the current content is returned so
the caller can merge), `3` content looked like a secret and was refused. `migrate` uses the same
codes for a batch: `3` if any note was refused, `1` if any note was invalid or collided with a
different existing note, `0` otherwise.

## Migrating from Claude Code

Claude Code keeps auto memory per project under
`${CLAUDE_CONFIG_DIR:-~/.claude}/projects/<slug>/memory/`, in the note format this store reads.
`mem migrate` copies the notes in, skips the generated `MEMORY.md`, and lets this store rebuild
the index:

```bash
mem migrate --dry-run                  # the plan, no writes
mem migrate                            # every project, into projects/<slug>
mem migrate --global user,feedback     # promote those types to global/
mem migrate --cwd /path/to/project     # one project (e.g. a custom autoMemoryDirectory)
```

Claude Code's directory name cannot be decoded back to a path — it turns every non-alphanumeric
into `-`, so a dot is lost — so the importer reads the real cwd from the project's session logs
and builds the scope with the store's own slug. A project whose cwd cannot be recovered is
reported and skipped rather than written where no `mem` command would read it; `--cwd` resolves
a project from the cwd you already know.

Notes stay in their own project scope by default: one may say `type: feedback` and still be
about a single repository, and Claude Code kept it under one project. Each note goes through the
store's write path, so the filename rules, the size cap, the empty-body check and the secret
refusal apply, and `--dry-run` runs the same checks without writing. An existing file is never
overwritten: an identical note is `exists`, a different one is a `collision` (exit 1), and a
note the store already has under another filename — same type and name, or the same description
by the consolidation rule — is `exists` too, so a rerun cannot overturn what the first pass
kept. Consolidation then runs once per touched scope; `replaced` and `merged` say what the pass
deleted, and a migrated note it removed is reported as `removed`.

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
└── projects/<slug>/{MEMORY.md,*.md}
```

`<slug>` is the absolute cwd with every unsafe character replaced by `-` and `.`/`_` kept:
`/home/u/proj` becomes `-home-u-proj`. On Windows the path is case-folded first, so
`C:/Users/u/proj` becomes `c--users-u-proj` and the same project cannot get two scopes from two
spellings. Each project gets one directory and no path escapes `$MEM_ROOT`.

There is no config file, no runtime state, and no git repository: the store is the notes and the
index. On Windows each scope also holds a `.mem.lock` file; the OS releases the lock when the
process exits, the file is invisible to the index, and it can be deleted while `mem` is not
running.

## Windows

Everything above works unchanged; the differences are the shell and a few file-system rules.

- Install with the same `uv tool install .` in PowerShell. `mem.exe` lands on `PATH`; open a new
  terminal so the PATH update is visible (`Get-Command mem` confirms it).
- There is no bash requirement. Use the installed `mem`, `python -m memlib`, or `bin\mem.cmd`
  as the no-install launcher.
- README shell examples use bash heredocs. In PowerShell, pass the body inline or from a file:

  ```powershell
  mem write --path pnpm-not-npm.md --name pnpm-not-npm `
    --description "the lockfile is pnpm; npm install breaks CI" `
    --if-version new --content-file body.md
  ```

- Notes written by Notepad or another CRLF editor are read correctly; the store writes LF, so
  one note has the same bytes on every OS. `memory.md` is reserved in any casing, because on a
  case-insensitive volume it *is* the generated `MEMORY.md`.
- Claude Code's user config is `%USERPROFILE%\.claude.json`; `claude mcp add mem mem mcp --scope
  user` works in PowerShell as written. opencode and DSH use their own config files under the
  home directory, with the same JSON/YAML shown above.
- The CLI prints UTF-8 JSON regardless of the console code page. Windows PowerShell 5.1 can
  still garble non-ASCII output when it reads a native command; use PowerShell 7+, or run
  `[Console]::OutputEncoding = [Text.Encoding]::UTF8` once per session.

## Development

```
bin/mem              POSIX launcher for development (python3 -m memlib, no install needed)
bin/mem.cmd          Windows launcher for development
memlib/store.py      files, frontmatter, version tokens, conflicts, secret refusal, locking
memlib/index.py      MEMORY.md generation (atomic, no line budget)
memlib/dream.py      consolidation, called after every change and by `mem dream`
memlib/mcp.py        MCP stdio server: memory_index / memory_read / memory_write
memlib/migrate.py    import Claude Code's per-project notes and rebuild the index
memlib/cli.py        argparse surface
PROTOCOL.md          the agent-facing contract, copied into the store root
docs/                research.md, design.md, roadmap.md, operations.md
tests/               unittest (stdlib only)
```

Tests use the standard library only:

```bash
export MEM_ROOT=$(mktemp -d)          # tests refuse to touch the real store
python3 -m unittest discover -s tests -v
```

On Windows:

```powershell
$env:MEM_ROOT = Join-Path $env:TEMP "mem-test"
python -m unittest discover -s tests -v
```

Background and rationale: [`docs/`](docs/) — `research.md` (how Claude Code and Qoder actually
work, and which claims are verified), `design.md` (the design and the alternatives dropped),
`roadmap.md` (history), and `operations.md` (setup, env vars, and how to verify this machine).
README and `PROTOCOL.md` are English; the documents under `docs/` are Persian.

## Design notes

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
  take an exclusive lock on the scope for the read-check-replace, because a token check without a
  lock is check-then-act: two writers holding the same token would both pass. On POSIX the lock
  is the directory's own inode — no file, no state, gone with the process. Windows cannot lock a
  directory handle, so there the lock is one byte of a `.mem.lock` file in the scope; it is still
  kernel state rather than bookkeeping, and the OS drops it when the process dies.
- **`mem read` takes a flat `.md` name.** `..`, absolute paths and `MEMORY.md` (in any case) are
  refused; the generated index is `mem index`. A hand-written file with invalid UTF-8 is decoded
  with replacement characters instead of failing the command.
- **The store is local to one machine.** There is no remote, no sync and no backup inside the
  tool: the version token and the short-lived lock only protect writers on the same machine, and
  a shared or network-backed store is not safe to write from two agents. Copy
  `~/.agents/memory` elsewhere on a schedule if you want a backup; the tool will not do it.
- **`PROTOCOL.md` is not part of the agent's onboarding.** The MCP `instructions` field is. The
  protocol file is only for connecting an agent that has no MCP client, and the server never
  exposes it as a tool or a resource.
- When testing, set `MEM_ROOT` to a scratch directory. The default root is the real store.

## License

MIT — see [`LICENSE`](LICENSE).
