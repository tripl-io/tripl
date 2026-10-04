/// <reference types="node" />
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

/**
 * Keeps raw Tailwind palette shades out of the UI.
 *
 * A status chip written as `bg-amber-500/15 text-amber-700` has two problems
 * `src/theme-contrast.test.ts` cannot see, because that test only reads
 * `index.css`:
 *
 *  1. A light shade with no `dark:` twin. Measured on the tinted fill over the
 *     dark surface scale, `text-*-700` lands at 2.7–2.9:1 — below AA. Both
 *     halves have to be hand-maintained forever, and the dark half kept getting
 *     forgotten.
 *  2. The shade is picked by eye, so it drifts from the token scale silently.
 *
 * The tone tokens fix both: `--<tone>` is pinned as the AA-safe ink for its own
 * `--<tone>-soft` fill in *both* themes, so `bg-warning-soft text-warning`
 * needs no `dark:` variant and is covered by measurement. This test is what
 * makes that the only option on the table.
 */

const SELF = fileURLToPath(import.meta.url)
const SRC = dirname(SELF)

const PALETTE = [
  'slate', 'gray', 'zinc', 'neutral', 'stone', 'red', 'orange', 'amber',
  'yellow', 'lime', 'green', 'emerald', 'teal', 'cyan', 'sky', 'blue',
  'indigo', 'violet', 'purple', 'fuchsia', 'pink', 'rose',
].join('|')

// `text-amber-700`, `dark:bg-rose-500/15`, `hover:border-emerald-400` …
//
// The variant-prefix group is optional on purpose, and that is what keeps
// bracketed variants covered: in `data-[state=open]:bg-amber-500` the prefix
// matches zero times and `\b` still holds right after the `:`, so the match
// simply starts at `bg-amber-500`. Verified rather than argued — a probe file
// carrying `data-[state=open]:bg-amber-500`, `[&_svg]:text-amber-700` and
// `peer-data-[state=checked]:border-rose-400` is flagged on all three.
const PALETTE_CLASS = new RegExp(
  String.raw`\b(?:[a-z-]+:)*(?:text|bg|border|ring|fill|stroke|from|via|to|divide|outline|shadow|accent|caret|placeholder|decoration)-(?:${PALETTE})-\d{2,3}\b`,
  'g',
)

/**
 * Files that legitimately paint outside the token system. Each entry carries a
 * reason, not just a path — an allowlist without one is how the rule dies.
 */
const ALLOWED = new Map<string, string>([
  [
    'components/event-photos-section.tsx',
    'One decorative multi-hue gradient standing in for the Figma mark. It carries '
      + 'no text and no status meaning.',
  ],
])

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) return sourceFiles(full)
    return /\.(tsx?|css)$/.test(entry.name) ? [full] : []
  })
}

function rawShadesIn(source: string): string[] {
  return [...new Set(source.match(PALETTE_CLASS) ?? [])]
}

describe('raw Tailwind palette shades', () => {
  it('appear only in the files that are allowed to opt out', () => {
    const offenders = new Map<string, string[]>()
    for (const file of sourceFiles(SRC)) {
      // This file quotes the banned shades to explain why they are banned.
      if (file === SELF) continue
      const rel = relative(SRC, file)
      if (ALLOWED.has(rel)) continue
      const found = rawShadesIn(readFileSync(file, 'utf8'))
      if (found.length > 0) offenders.set(rel, found)
    }
    expect(
      Object.fromEntries(offenders),
      'Use the tone tokens instead: bg-<tone>-soft + text-<tone>, or <Chip tone> / '
        + '<Badge variant>. Those are AA-verified in both themes by '
        + 'theme-contrast.test.ts and need no dark: variant.',
    ).toEqual({})
  })

  it('leave no allowlisted file that has since been cleaned up', () => {
    for (const [rel, reason] of ALLOWED) {
      const found = rawShadesIn(readFileSync(resolve(SRC, rel), 'utf8'))
      expect(
        found.length,
        `${rel} no longer uses raw palette shades — drop it from ALLOWED (${reason})`,
      ).toBeGreaterThan(0)
    }
  })
})
