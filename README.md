# agent-memory

A memory store shared by several coding agents (DSH, opencode, Claude Code, Qoder), so
that what one of them learns is available to the others.

Plain markdown on disk, one file per memory, indexed by a `MEMORY.md` table of contents —
the same shape Claude Code and Qoder already use, so the data stays readable by hand, by
`grep`, and by those tools. The `mem` CLI is the only place logic lives; every agent
adapter is a thin caller of it.

Design and research behind it: [`docs/`](docs/) — `research.md` (how Claude Code and Qoder
actually work, and which claims are verified), `design.md` (the design, with the decisions
and the alternatives that were dropped), `roadmap.md` (the phase plan and its acceptance
criteria).

## Status

P0–P2 are implemented: the store, index, versioned writes, lexical recall, the CLI, the
four adapters (Claude Code hooks, the MCP stdio server, the opencode V2 plugin, the DSH
plugin), and one background pass — `mem dream` (session-counted consolidation, which the
SessionStart hook spawns once the counter reaches `dream.every_sessions`).

End-of-session extraction (`mem extract`) existed through P2 and was **removed**: neither
Claude Code nor Qoder runs such a pass, and it was not wanted here. Nothing writes memories
behind the agent's back now — the agent writes with `memory_write`, and `dream` only
consolidates what is already there.

The store is installed on this machine (`~/.agents/memory`) and wired into all four agents:
Claude Code (hooks + MCP), opencode (plugin + MCP), and DSH, whose plugin is installed into
the `web` profile as a linked package and a selected bundle.

## Usage

```bash
export MEM_ROOT=~/.agents/memory          # default; set it explicitly when testing

# create (version token comes back for the next write)
./bin/mem write --scope project --cwd "$PWD" --path pnpm-not-npm.md \
  --name pnpm-not-npm --description "the lockfile is pnpm; npm install breaks CI" \
  --if-version new --session "$SESSION_ID" <<'EOF'
The lockfile is pnpm. `npm install` rewrites it and breaks CI.
EOF

./bin/mem read   --scope project --cwd "$PWD" --path pnpm-not-npm.md
./bin/mem list   --scope project --cwd "$PWD"
./bin/mem index  --cwd "$PWD"                 # what gets injected into a prompt
./bin/mem recall "lockfile" --cwd "$PWD"      # only above select_threshold memories
./bin/mem forget --scope project --cwd "$PWD" --path pnpm-not-npm.md

# adapters
./bin/mem hook session-start < payload.json   # Claude Code hook (JSON in, JSON out)
./bin/mem mcp                                 # MCP server on stdio

# consolidation
./bin/mem dream --cwd "$PWD"                  # consolidate global + this project
./bin/mem dream --all                         # every project scope
./bin/mem dream --force                       # ignore the session counter
```

Exit codes: `0` ok, `1` invalid input, `2` version conflict (current content is returned so
the caller can merge), `3` content looked like a secret and was refused.

## Configuration

`$MEM_ROOT/config.toml` (all keys optional):

```toml
[context]
index_max_lines = 200            # cap on the injected index block

[recall]
select_threshold = 40            # below this many memories, recall never runs
top_k = 8

[dream]
every_sessions = 5               # session counter that triggers consolidation
```

`mem dream` merges notes of the same type whose descriptions are near-identical, lets the
newer of two notes with the same name *and* type replace the older, repairs `MEMORY.md`,
records one line in `dream.log` for every run that actually consolidates, and — when the
store is a git repository — commits before and after the pass. It is deterministic (no model
call) and idempotent: a second run changes nothing.

The commits need a git identity. If the repository has none (no `user.name`/`user.email` in
the repo or globally), `git commit` fails; the pass then says so on stderr instead of
reporting success while the history stays frozen and the tree stays staged. On a store you
care about, set one locally: `git -C $MEM_ROOT config user.name mem` and
`git -C $MEM_ROOT config user.email mem@localhost`.

## Layout

```
bin/mem              launcher (python3 -m memlib, no install needed)
memlib/store.py      files, frontmatter, version tokens, conflicts, secret refusal, recall
memlib/index.py      MEMORY.md generation and budget capping
memlib/config.py     root, config.toml subset, scope directories
memlib/cli.py        argparse surface
memlib/hooks.py      Claude Code hook adapter + the session counter (spawns dream)
memlib/mcp.py        MCP stdio server: memory_read / memory_write / memory_search
memlib/dream.py      consolidation: dedupe, replace, index repair, git commits
PROTOCOL.md          the agent-facing contract (copied into the store root)
docs/                research.md, design.md, roadmap.md — the background, kept in-repo
tests/               unittest (stdlib only), 149 tests
integrations/opencode/  opencode V2 plugin (compaction hook)
integrations/dsh/       cordis plugin (prompt section, pre-step, tools)
install/             merge fragments: claude-code.json, mcp.json, opencode.json, agents-md.md
```

## Store layout

```
$MEM_ROOT/
├── config.toml
├── PROTOCOL.md                              # copied from this repo
├── global/{MEMORY.md,*.md}                  # user-wide facts and preferences
├── projects/<sanitized-path>/{MEMORY.md,*.md}
├── .state/state.json                        # session counter for `mem dream`
├── .state/dream.lock                        # one consolidation at a time
├── dream.log                                # one line per consolidation
└── .git/                                    # optional; `mem dream` commits to it
```

`<sanitized-path>` is the absolute cwd with every unsafe character replaced by `-`
(`/home/u/proj` → `-home-u-proj`), matching what Claude Code and Qoder use.

## Install

There is no package to install: `bin/mem` runs straight from this checkout.

1. Create the store and its repository:
   `mkdir -p ~/.agents/memory && git -C ~/.agents/memory init`
2. Copy the agent contract into it: `cp PROTOCOL.md ~/.agents/memory/PROTOCOL.md`
3. Merge the relevant fragments from `install/` into each agent's configuration
   (`claude-code.json`, `mcp.json`, `opencode.json`), and append `install/agents-md.md` to
   `~/.agents/AGENTS.md` — Claude Code already imports that file.
4. Set `"autoMemoryEnabled": false` in `~/.claude/settings.json` so the native store does not
   run in parallel with this one.
5. DSH: link `integrations/dsh` into `~/.dsh/plugins/memory`, register it with
   `dsh plugin --profile <name> add link:$HOME/.dsh/plugins/memory`, and restart — the
   details are in `integrations/dsh/README.md`. opencode has its own steps in
   `integrations/opencode/README.md` (copy `mem.js` into its plugins directory, merge
   `install/opencode.json`).

## Tests

```bash
export MEM_ROOT=$(mktemp -d)          # tests refuse to touch the real store
python3 -m unittest discover -s tests -v
```

## Notes

- **No database.** The index is regenerated from the files by scanning the directory, which
  is cheap at this scale and makes drift impossible.
- **`if_version` is mandatory** on write unless `--force` is passed: a blind overwrite is
  exactly what the token exists to prevent.
- **Dream is deterministic.** It has no model: a "contradiction" is the same `name` and type,
  where the newer `modified` wins, and duplicates are notes of one type whose descriptions
  overlap by at least 80%. Rename-and-contradict is not detected; that is a deliberate
  trade-off, not an oversight.
- **Runtime state is not memory.** `.state/` and `dream.log` are ignored by the store's
  repository; only notes and `MEMORY.md` are committed.
- **Nothing to install.** The standard library only, plus `git` as an optional companion for
  dream's commits; without it the pass still consolidates.
- When testing, set `MEM_ROOT` to a scratch directory. The default root is the real store.
