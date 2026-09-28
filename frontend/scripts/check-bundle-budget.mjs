#!/usr/bin/env node
// Fails the build when the JavaScript a first page load has to fetch grows past
// its budget, or when a chunk that is meant to load on demand ends up on that
// critical path. Reads what `vite build` wrote: the entry script and every
// modulepreload link in dist/index.html are what the browser fetches before
// the app can render anything.
//
// The budgets are a ratchet, not a target. Raise one only in the change that
// explains why the first load has to carry more; lower it when a change makes
// the first load smaller, so the saving cannot quietly be spent again.

import { readFileSync, statSync } from 'node:fs'
import path from 'node:path'

const DIST = path.resolve(import.meta.dirname, '..', 'dist')

// Bytes on disk before compression, measured 2026-09-25 plus ~5% headroom —
// after #194 took recharts, the demo chrome, the command palette dialog and the
// settings surfaces off the first load (entry 132 732, critical path 694 876).
// Raised for #195: the shell now carries the session-expiry sign-in dialog
// (eager on purpose — it exists to keep an unsaved page alive, so it must not
// depend on fetching a chunk after a deploy), the URL-synced branch context and
// the collapsed rail's controls (entry 145 128; the Appearance panel moved to
// its own chunk to pay for part of it).
// Critical path raised for React 19.3: react-dom's client build alone grew by
// ~31 KB minified, all of it in react-vendor, which every page needs (critical
// path 742 953 with the entry unchanged). Both lowered for tripl-fj5g.15: the
// demo scenario model loads only for demo projects and the alerting tab's
// sections load per tab (entry 127 029, critical path 724 527).
// Entry raised for the design-system follow-up (#198 / #206, frontend review
// r4): no new component code joined the first load — the entry grew by the
// lazy-route dependency map (`__vite__mapDeps`), which now names the shared
// on-demand chunks the refactor created (IconButton, the anchored combobox
// list, the kit form row used by the event forms, lib/format) in every lazy
// route that imports them (entry 134 119).
// Raised for the design review batches 5-12 (#237-#244): the shell now draws
// what used to be a blank screen or a page-level gap on first load: route
// skeletons shaped like each page (the Suspense fallbacks must be synchronous),
// the account menu in the sidebar footer, and the branch strip under the top
// bar. The activity rail moved out of the entry to pay for part of it
// (entry 145 367).
// Raised by the owner's decision (2026-09-28, F20 organizations): the budgets
// had been set a few KB above the last measurement with no benchmark behind
// them, and recent changes spent effort trimming search keywords to fit a
// menu entry. The critical path is capped at 1 MB and the entry at 200 KB;
// both still fail a change that accidentally pulls a lazy chunk onto the first
// load, which is what the check exists to catch (entry 152 649, critical path
// 758 961 at the time of the change).
const ENTRY_BUDGET = 200_000
const CRITICAL_PATH_BUDGET = 1_000_000

// Chunks that are split out so that only the pages using them pay for them:
// the SQL editor, its formatter (fetched on the first Format click) and
// recharts (vite.config.ts `charts-vendor`, behind components/ui/chart-lazy).
// The docs catalog (F22) keeps react-markdown in `DocsPage` and CodeMirror's
// Markdown grammar in `DocEditor` (loaded on Edit); the event page's Notes
// card is a plain list and must not pull either onto the first load.
const LAZY_ONLY = ['sql-editor', 'sql-format', 'charts-vendor', 'DocsPage', 'DocEditor']

const html = readFileSync(path.join(DIST, 'index.html'), 'utf8')
const entries = [...html.matchAll(/<script[^>]+type="module"[^>]+src="\/assets\/([^"]+\.js)"/g)]
const preloads = [...html.matchAll(/<link[^>]+rel="modulepreload"[^>]+href="\/assets\/([^"]+\.js)"/g)]

if (entries.length !== 1) {
  console.error(`expected exactly one module entry in dist/index.html, found ${entries.length}`)
  process.exit(1)
}

const size = (file) => statSync(path.join(DIST, 'assets', file)).size
const entry = entries[0][1]
const critical = [entry, ...preloads.map((match) => match[1])]
const total = critical.reduce((sum, file) => sum + size(file), 0)

const failures = []
if (size(entry) > ENTRY_BUDGET) {
  failures.push(`entry ${entry} is ${size(entry)} bytes, budget ${ENTRY_BUDGET}`)
}
if (total > CRITICAL_PATH_BUDGET) {
  failures.push(`critical path is ${total} bytes, budget ${CRITICAL_PATH_BUDGET}`)
}
for (const name of LAZY_ONLY) {
  const found = critical.find((file) => file.startsWith(`${name}-`))
  if (found) failures.push(`${found} is on the critical path; it should load on demand`)
}

for (const file of critical) console.log(`${String(size(file)).padStart(9)}  ${file}`)
console.log(`${String(total).padStart(9)}  total (budget ${CRITICAL_PATH_BUDGET})`)

if (failures.length > 0) {
  for (const failure of failures) console.error(`bundle budget: ${failure}`)
  process.exit(1)
}
