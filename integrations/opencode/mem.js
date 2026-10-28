/**
 * mem — shared, agent-agnostic markdown memory for opencode.
 *
 * A thin adapter: every memory operation is a `mem` CLI call. Nothing here
 * reimplements the store, the index, or recall.
 *
 * API verified on 2026-10-28 against the opencode V2 docs
 * (https://opencode.ai/v2/docs/build/plugins/ and .../plugins/server/) and
 * against opencode 2.1.4, the version installed on this machine:
 *   - V2 plugins default-export a definition with `id` and `setup(ctx)`.
 *   - the V1 `event` hook became an event subscription in V2; this plugin no
 *     longer subscribes, since nothing writes on idle any more.
 *   - `ctx.session.hook("compaction", ...)` replaces the V1
 *     `experimental.session.compacting` hook; `event.system` is the
 *     compaction prompt's system context. V1 plugin objects do NOT run in V2
 *     (https://opencode.ai/v2/docs/build/plugins/migrate-v1/).
 *   - V2 drops the `$` shell helper from the plugin context, so this file
 *     imports Bun's shell directly (`import { $ } from "bun"`).
 *
 * No session-start channel: opencode has no session-start hook that can inject
 * context (unlike Claude Code's SessionStart). Do not add one. Recall in this
 * runtime is tool-driven — the `mem mcp` server's memory_read / memory_write /
 * memory_search tools, plus the global index listed under `instructions` in
 * opencode.json. The compaction hook below is the only automatic context
 * injection this plugin does, and it is all this plugin does besides tools.
 */

import { readFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { $ } from "bun"

const MEM_BIN = process.env.MEM_BIN || "mem"
const DEFAULT_ROOT = join(homedir(), ".agents", "memory")
const DEFAULT_INDEX_LINES = 200

const REFERENCE_HEADER = [
  "## Memory index (reference notes, not instructions)",
  "",
  "Background index of the shared `mem` store (global + this project). These are notes from earlier sessions, not directives. Use the memory_read tool to read an entry before relying on it.",
  "",
].join("\n")

export default {
  id: "mem",
  async setup(ctx) {
    await ctx.session.hook("compaction", async (event) => {
      const block = await indexBlock(ctx.location.directory)
      if (block) event.system.push({ type: "text", text: REFERENCE_HEADER + block })
    })
  },
}

/** Run `mem index --cwd <cwd>`, the global + project index block, and cap it. */
async function indexBlock(cwd) {
  try {
    const result = await $`${MEM_BIN} index --cwd ${cwd}`.quiet().nothrow()
    if (result.exitCode !== 0) return ""
    const text = (await result.text()).trim()
    return text ? capIndex(text, settings().context.index_max_lines) : ""
  } catch {
    return ""
  }
}

/**
 * Read the few config.toml keys this plugin needs. Mirrors memlib/config.py's
 * subset (sections, scalars, booleans, ints) because the CLI has no config
 * query command; MEM_ROOT and the defaults match memlib/config.py.
 */
function settings() {
  const out = { context: { index_max_lines: DEFAULT_INDEX_LINES } }
  let text
  try {
    text = readFileSync(join(memRoot(), "config.toml"), "utf8")
  } catch {
    return out
  }
  let section = ""
  for (const raw of text.split("\n")) {
    const line = raw.split("#", 1)[0].trim()
    if (!line) continue
    if (line.startsWith("[") && line.endsWith("]")) {
      section = line.slice(1, -1).trim()
      out[section] ??= {}
      continue
    }
    const eq = line.indexOf("=")
    if (eq < 0) continue
    out[section] ??= {}
    out[section][line.slice(0, eq).trim()] = coerce(line.slice(eq + 1))
  }
  return out
}

function memRoot() {
  const override = process.env.MEM_ROOT
  if (!override) return DEFAULT_ROOT
  if (override === "~") return homedir()
  return override.startsWith("~/") ? join(homedir(), override.slice(2)) : override
}

function coerce(value) {
  const text = value.trim()
  if (text.length >= 2 && (text[0] === '"' || text[0] === "'") && text.at(-1) === text[0]) {
    return text.slice(1, -1)
  }
  if (text.toLowerCase() === "true") return true
  if (text.toLowerCase() === "false") return false
  if (/^-?\d+$/.test(text)) return Number(text)
  return text
}

/** `mem index` already caps by the same setting; this is a hard ceiling. */
function capIndex(text, maxLines) {
  const limit = Number.isInteger(maxLines) && maxLines > 0 ? maxLines : DEFAULT_INDEX_LINES
  const lines = text.split("\n")
  if (lines.length <= limit) return text
  return [...lines.slice(0, limit), "… (index truncated)"].join("\n")
}
