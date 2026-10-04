/// <reference types="node" />
import { readFileSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * Keeps the published scan docs saying what the Scans surfaces say.
 *
 * The docs are read by a user who is stuck, which is exactly when a sentence
 * that disagrees with the product costs the most: the FAQ used to answer "Do I
 * need to run scans on a schedule to get metrics?" with **No**, while the same
 * file's "No metrics appear after a scan" section, concepts.md, quick-start.md
 * and feature-reference.md all say a schedule is precisely what separates a
 * monitoring scan from a catalog-only one. A user with an empty chart believed
 * the FAQ and went looking for a dead beat container.
 *
 * Nothing but prose ties those surfaces together, so this file ties them to the
 * product itself: the tile names come out of ScansTab.tsx, and the FAQ answer is
 * held to the rule `check_metrics_due` actually applies (it selects only scan
 * configs where BOTH `interval` and `time_column` are set —
 * backend/src/tripl/worker/tasks/metrics/schedule.py).
 */

const SELF = fileURLToPath(import.meta.url)
const SRC = dirname(SELF)
const DOCS = join(resolve(SRC, '../..'), 'website', 'docs')

const readDoc = (relative: string): string => readFileSync(join(DOCS, relative), 'utf8')

/** The four docs a reader treats as one document about scans. */
const SCAN_DOCS = [
  'quick-start.md',
  'use/concepts.md',
  'use/feature-reference.md',
  'use/troubleshooting.md',
]

/**
 * The wire's noun for a scan, in both spellings a writer reaches for.
 *
 * This started life as `/\bscan configs?\b/i` in two places, and the abbreviated
 * spelling is not the one that survives a sweep: `\b` after "config" is not a
 * boundary before the "u" of "configuration", so the Monitoring distribution
 * empty state kept saying "on the scan configuration" through two passes of this
 * very guard. One exported constant, because two copies of a rule is how the
 * rule ends up enforcing two different things.
 *
 * The space between the words is load-bearing and stays literal: `scan_config`,
 * `scan-config-id` and `ScanConfigurationTab` are wire identifiers and React
 * component names, and this guard never reads identifiers — only prose.
 */
const SCAN_CONFIG_NOUN = /\bscan config(?:uration)?s?\b/i

describe('the scan docs describe the product this branch ships', () => {
  it('feature-reference names the four figures the Scans page renders', () => {
    const page = readFileSync(join(SRC, 'pages', 'settings', 'ScansTab.tsx'), 'utf8')
    const labels = [...page.matchAll(/<MiniStat\s[\s\S]*?label="([^"]+)"/g)].map((m) => m[1])
    // Scans · Monitoring · Failing · Warehouse rows 24h (#247 DA-11, DA-4).
    expect(labels, 'ScansTab.tsx should still render exactly four KPI figures').toHaveLength(4)

    const doc = readDoc('use/feature-reference.md')
    const paragraph = doc
      .split('\n\n')
      .find((block) => block.includes('The scan list heads four figures'))
    expect(
      paragraph,
      'feature-reference.md no longer has the paragraph naming the Scans page tiles, so nothing '
        + 'documents them. Restore it, or update this guard to the sentence that replaced it.',
    ).toBeDefined()

    for (const label of labels) {
      expect(
        paragraph,
        `feature-reference.md names a tile the Scans page does not have: it must say **${label}**, `
          + 'the label ScansTab.tsx renders. A reader who goes looking for the tile the doc names '
          + 'finds a differently-named one.',
      ).toContain(`**${label}**`)
    }
  })

  it('the troubleshooting FAQ does not tell a user a schedule is optional for metric points', () => {
    const doc = readDoc('use/troubleshooting.md')
    const question = '**Do I need to run scans on a schedule to get metrics?**'
    const at = doc.indexOf(question)
    expect(
      at,
      `troubleshooting.md should still carry the FAQ entry ${question} — it is where a user with `
        + 'an empty chart lands.',
    ).toBeGreaterThan(-1)

    const answer = (doc.slice(at + question.length).split('\n\n')[0] ?? '').trim()

    expect(
      answer,
      'The FAQ answers "Do I need to run scans on a schedule to get metrics?" with No, but '
        + 'check_metrics_due only ever dispatches collect_metrics for scans that set BOTH a '
        + 'schedule and a time column, and this same file\'s "No metrics appear after a scan" '
        + 'section, concepts.md, quick-start.md and feature-reference.md all say so. A user reading '
        + 'No concludes the schedule is not the problem and goes hunting for a dead beat container.',
    ).not.toMatch(/^\**No\b/i)

    for (const requirement of [/schedule/i, /time column/i]) {
      expect(
        answer,
        `The FAQ answer must name what the dispatcher requires (${requirement.source}); without `
          + 'both halves it cannot send a Catalog only scan back to the form, which is where the '
          + 'fix is.',
      ).toMatch(requirement)
    }
  })

  /**
   * The correction that took metric points off "every run" is right — `run_scan`
   * writes no `EventMetric` row — but it is one word away from a second false
   * claim, and quick-start.md shipped it: "only the schedule records metric
   * points". The scan page ships a manual metrics path next to that
   * sentence. **Replay a period…** in its header (#247 DA-8 moved it out of
   * the Configuration tab's Danger zone) posts to `/metrics/replay` →
   * `scan_service.trigger_metrics_replay`, which dispatches `collect_metrics`
   * for an explicit window — the same task beat dispatches, and the one that
   * UPSERTs `event_metrics` rows. troubleshooting.md sends a user with an empty
   * chart to press exactly that button, so the absolute costs them an interval
   * of waiting with the fix on screen.
   */
  it('never makes the schedule the only path to a metric point', () => {
    const page = readFileSync(join(SRC, 'pages', 'settings', 'ScanConfigDetailView.tsx'), 'utf8')
    const CONTROL = 'Replay a period…'

    expect(
      page,
      `The scan page header no longer offers "${CONTROL}". If the manual metrics path is gone, `
        + 'the docs may say the schedule is the only one — retire this guard with it.',
    ).toContain(CONTROL)
    expect(
      page,
      'The replay control is no longer gated on time_column && interval, the pair '
        + 'trigger_metrics_replay 400s without. The docs describe it as unlocking with both.',
    ).toMatch(/canReplay = Boolean\(sc\.time_column && sc\.interval\)/)

    // Only the exclusive shape is banned. "records metric points on its
    // schedule" is the true claim and must stay sayable.
    const EXCLUSIVE = [
      /only the schedule\b[^.\n]*metric points?/i,
      /metric points?[^.\n]* only (?:on|from) (?:its|the) schedule/i,
    ]
    for (const relative of SCAN_DOCS) {
      const offenders = readDoc(relative)
        .split('\n')
        .map((line, index) => ({ line, number: index + 1 }))
        .filter(({ line }) => EXCLUSIVE.some((pattern) => pattern.test(line)))
        .map(({ line, number }) => `${relative}:${number}: ${line.trim()}`)

      expect(
        offenders,
        `${relative} says the schedule is the only thing that records metric points, but `
          + `"${CONTROL}" in the scan page header replays a past window through `
          + 'collect_metrics. Say Run now records none — not that nothing else can.',
      ).toEqual([])
    }

    // And wherever quick-start denies Run now the points, it must name the
    // control that does produce them; a bare denial is what sat the user out.
    const denials = readDoc('quick-start.md')
      .split(/\n(?=- \*\*)/)
      .filter((bullet) => /Run now/.test(bullet) && /metric point/.test(bullet))

    expect(
      denials.length,
      'quick-start.md no longer has the bullet telling the reader what Run now does about metric '
        + 'points. Restore it, or update this guard to the passage that replaced it.',
    ).toBeGreaterThan(0)

    for (const bullet of denials) {
      expect(
        bullet.replace(/\s+/g, ' '),
        `quick-start.md tells the reader Run now records no metric point without naming `
          + `"${CONTROL}", the control that does. That reader waits out a whole interval with the `
          + 'button on screen — the FAQ and the "No metrics appear after a scan" fix both send '
          + 'them to it.',
      ).toContain(CONTROL)
    }
  })

  it('no scan-facing doc calls a scan a "scan config"', () => {
    // Settled vocabulary: the web UI (and the docs describing it) say "scan".
    // "job" and "scan_config" stay on the wire, so the API/CLI references are
    // deliberately not in scope, and identifiers like `scan-config-id` or
    // `scan_config_not_dispatchable` are not matched here.
    //
    // Checked when SCAN_CONFIG_NOUN grew the "configuration" spelling — every
    // doc outside SCAN_DOCS that the wider pattern now hits describes the wire,
    // and each is out of scope on purpose, not by accident:
    //   run/cli.md               — the CLI reference; `tripl scans list` prints
    //                              "scan configs" and doctor's own summaries are
    //                              quoted verbatim.
    //   integrate/agent-api-guide.md — the API guide, written in wire nouns.
    //   run/security.md          — enumerates API scopes (`preview-jobs`,
    //                              `dry-run-jobs`) against permissions.
    //   develop/architecture.md  — the `ScanJob` row in the data-model table.
    for (const relative of SCAN_DOCS) {
      const offenders = readDoc(relative)
        .split('\n')
        .map((line, index) => ({ line, number: index + 1 }))
        .filter(({ line }) => SCAN_CONFIG_NOUN.test(line))
        .map(({ line, number }) => `${relative}:${number}: ${line.trim()}`)

      expect(
        offenders,
        `${relative} calls a scan a "scan config" — the noun the Scans page dropped. Say "scan".`,
      ).toEqual([])
    }
  })
})

/**
 * feature-reference.md promises the web UI's half of the split vocabulary:
 *
 *   "The API and the CLI call the same record a `job` … but every screen in the
 *    web UI says *run*."
 *
 * Prose cannot keep that promise on its own — it was false when written, on the
 * first screen after login: /projects headed a tile "Failed jobs" and reassured
 * with "Latest scan jobs are healthy", while Overview said "scan config" three
 * times and the alert-template editor offered `${scan_name}` as "Scan config
 * name". So this reads the shipped UI copy and holds it to the sentence.
 *
 * Scope is the sentence's own: `src/api` and `src/types` are the wire — the
 * generated OpenAPI types and the client that speaks `scan_config_id` and
 * `/jobs` — and are exempt by the same rule that exempts the API and the CLI.
 * Identifiers are never read, only text a user can see.
 */
const UI_TEXT_ROOTS_EXEMPT = new Set(['api', 'types'])

/** Comment-free copy of a TS/TSX source, so prose in comments is not UI copy. */
function stripComments(source: string): string {
  const out: string[] = []
  let mode: 'code' | 'line' | 'block' | "'" | '"' | '`' = 'code'
  for (let i = 0; i < source.length; i += 1) {
    const c = source.charAt(i)
    const next = source[i + 1] ?? ''
    if (mode === 'code') {
      if (c === '/' && next === '/') { mode = 'line'; i += 1; out.push(' '); continue }
      if (c === '/' && next === '*') { mode = 'block'; i += 1; out.push(' '); continue }
      if (c === "'" || c === '"' || c === '`') mode = c
      out.push(c)
      continue
    }
    if (mode === 'line') {
      if (c === '\n') { mode = 'code'; out.push(c) } else out.push(' ')
      continue
    }
    if (mode === 'block') {
      if (c === '*' && next === '/') { mode = 'code'; i += 1; out.push('  '); continue }
      out.push(c === '\n' ? c : ' ')
      continue
    }
    if (c === '\\') { out.push(c, next); i += 1; continue }
    if (c === mode) mode = 'code'
    out.push(c)
  }
  return out.join('')
}

/**
 * Text a user can read: string literals and JSX text nodes. `${…}` holes are
 * blanked (the identifier inside one is never rendered), and a JSX candidate
 * carrying code punctuation is dropped rather than guessed at — `>` also closes
 * an arrow and a comparison, and no UI sentence this guard protects needs
 * brackets.
 *
 * Runs of whitespace collapse to one space, because that is what the browser
 * does to a JSX text node and this guard reads what the user reads. Skipping
 * that step is the second reason "on the scan\n            configuration"
 * survived: the source holds a newline and twelve spaces where the rendered
 * sentence holds one, so a two-word phrase split across the line wrap matched
 * nothing, however the pattern was spelled.
 */
function userFacingText(source: string): { text: string, line: number }[] {
  const found: { text: string, line: number }[] = []
  const at = (index: number) => source.slice(0, index).split('\n').length
  const asRendered = (text: string) => text.replace(/\s+/g, ' ')
  for (const m of source.matchAll(/(['"`])((?:\\.|(?!\1)[^\\])*)\1/gs)) {
    found.push({ text: asRendered((m[2] ?? '').replace(/\$\{[^{}]*\}/g, ' ')), line: at(m.index) })
  }
  for (const m of source.matchAll(/>([^<>{}]+)</gs)) {
    const text = m[1] ?? ''
    if (/[;=()[\]]/.test(text)) continue
    found.push({ text: asRendered(text), line: at(m.index) })
  }
  return found.filter(({ text }) => / /.test(text) && /[a-z]/.test(text))
}

function uiSources(dir: string, root = dir): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      return dir === root && UI_TEXT_ROOTS_EXEMPT.has(entry.name) ? [] : uiSources(full, root)
    }
    if (!/\.tsx?$/.test(entry.name) || entry.name.includes('.test.')) return []
    return [full]
  })
}

describe('the web UI speaks the settled vocabulary feature-reference promises', () => {
  it('still makes the promise this guard enforces', () => {
    const doc = readDoc('use/feature-reference.md').replace(/\s+/g, ' ')
    expect(
      doc,
      'feature-reference.md dropped the sentence promising the web UI says *run* where the API '
        + 'says `job`. Restore it, or delete this describe block — an unenforced claim and an '
        + 'unclaimed enforcement are both worse than either alone.',
    ).toContain('but every screen in the web UI says *run*')
  })

  it('never shows a user the word "job" or the noun "scan config"', () => {
    /**
     * The other sense of "job", and the only one allowed: Plan → Observe →
     * Govern are jobs-to-be-done — the sidebar's information architecture, not
     * executions. feature-reference.md calls them "three job-based areas" in
     * the same file that bans `job` for a run, so both senses are deliberate.
     */
    const JOBS_TO_BE_DONE = /Three jobs turn a tracking plan/

    const banned: [RegExp, string][] = [
      [SCAN_CONFIG_NOUN, 'calls a scan a "scan config"; the web UI noun is "scan"'],
      [/\bjobs?\b/i, 'calls a run a "job"; the web UI noun is "run"'],
    ]

    const offenders: string[] = []
    for (const file of uiSources(SRC)) {
      for (const { text, line } of userFacingText(stripComments(readFileSync(file, 'utf8')))) {
        if (JOBS_TO_BE_DONE.test(text)) continue
        const hit = banned.find(([pattern]) => pattern.test(text))
        if (!hit) continue
        offenders.push(`${file.slice(SRC.length + 1)}:${line} ${hit[1]} — ${text.trim()}`)
      }
    }

    expect(
      offenders,
      'A web-UI surface shows the wire\'s vocabulary. feature-reference.md tells the reader that '
        + '`job` and `scan_config` live only on the API and the CLI and that every screen says '
        + '*run* and *scan*, so each line below makes that sentence false for the user standing '
        + 'in front of it.',
    ).toEqual([])
  })
})
