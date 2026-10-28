# AGENTS.md

Only what a fresh session needs in order not to break something. Details live in `docs/`.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

- pytest is neither installed nor needed; new tests go in the four existing files under `tests/`
  and use `unittest`.
- Every `mem` command in a test or a check needs `MEM_ROOT=$(mktemp -d)`. The real store is
  `~/.agents/memory` and must not be touched.
- `./bin/mem` runs the checkout. `mem` on `PATH` is the installed copy and may be stale; to run the
  new code there you need `uv tool install --reinstall .`, but ask the user first.

## Documents

| File | Role |
|---|---|
| `README.md` | intro, install, agent wiring |
| `PROTOCOL.md` | hand-written connectivity guide for agents without an MCP client |
| `docs/design.md` | the design and rejected options; §11 is the closed decisions |
| `docs/operations.md` | this machine's state and setup |
| `docs/roadmap.md` | history of what shipped |
| `HANDOFF.md` | the open work order; delete or rewrite it when done |
| `docs/research.md` | historical research on Claude Code and Qoder |

- README and PROTOCOL are English; everything else is Persian, and so are the answers to the user.
- Update README and `docs/` in the same change that alters behaviour, not in a later commit.
- Do not reopen §11 of `design.md` without a new reason: no keyword search, no line cap on the
  index, no config file, MCP-only, consolidation after every change.

## Code

- Python ≥ 3.11, standard library only; do not add dependencies.
- `memlib/` is the only code package and `bin/mem` is just a launcher. CLI and MCP both go through
  `memlib.store`, so do not build the same logic twice.

## Git

- Push after every commit.
- Merge, rebase, and branch deletion only on an explicit request from the user.
