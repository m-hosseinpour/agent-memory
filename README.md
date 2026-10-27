# agent-memory

A memory store shared by several coding agents (DSH, opencode, Claude Code, Qoder), so
that what one of them learns is available to the others.

Plain markdown on disk, one file per memory, indexed by a `MEMORY.md` table of contents —
the same shape Claude Code and Qoder already use, so the data stays readable by hand, by
`grep`, and by those tools. The `mem` CLI is the only place logic lives; every agent
adapter is a thin caller of it.

Design and research behind it: `~/.dsh/memory-system/` (`research.md`, `design.md`, `roadmap.md`).

## Status

Phase P0 of the roadmap is implemented: store, index, versioned writes, lexical recall,
and the CLI. Adapters (hooks, MCP, DSH plugin) are not written yet — see `HANDOFF.md`.

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
```

Exit codes: `0` ok, `1` invalid input, `2` version conflict (current content is returned so
the caller can merge), `3` content looked like a secret and was refused.

## Layout

```
bin/mem              launcher (python3 -m memlib, no install needed)
memlib/store.py      files, frontmatter, version tokens, conflicts, secret refusal, recall
memlib/index.py      MEMORY.md generation and budget capping
memlib/config.py     root, config.toml subset, scope directories
memlib/cli.py        argparse surface
tests/               unittest (stdlib only)
integrations/        per-agent adapters (empty so far)
install/             settings snippets to copy into each agent
```

## Store layout

```
$MEM_ROOT/
├── config.toml
├── global/{MEMORY.md,*.md}                  # user-wide facts and preferences
├── projects/<sanitized-path>/{MEMORY.md,*.md}
└── .state/state.json                        # session counter for `mem dream`
```

`<sanitized-path>` is the absolute cwd with every unsafe character replaced by `-`
(`/home/u/proj` → `-home-u-proj`), matching what Claude Code and Qoder use.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Notes

- **No database.** The index is regenerated from the files by scanning the directory, which
  is cheap at this scale and makes drift impossible.
- **`if_version` is mandatory** on write unless `--force` is passed: a blind overwrite is
  exactly what the token exists to prevent.
- **Only the standard library.** No dependency to install, no build step.
- When testing, set `MEM_ROOT` to a scratch directory. The default root is the real store.
