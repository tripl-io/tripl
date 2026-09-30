/// <reference types="node" />
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import { collapsedDriftLabel, DRIFT_REVIVE_LABEL } from '@/lib/variableDrift'

/**
 * Keeps the drift-review docs saying what the drift panels say (tripl-lh61).
 *
 * The wave that gave a snooze its own review state renamed the collapse toggle
 * and the revive button, and left four sentences across three published pages
 * still promising the old wording. A reader hunting for **Show N resolved** on
 * a panel that reads **Show 1 snoozed** is the person who just snoozed a drift
 * and wants it back — the one case where the name differs, and the reader most
 * likely to be looking.
 *
 * Asserting against the labels THE CODE EMITS rather than a hardcoded list is
 * the whole point: renaming a button now fails here instead of quietly making a
 * doc false, which is what happened the first time.
 */

const SRC = dirname(fileURLToPath(import.meta.url))
const DOCS = join(resolve(SRC, '..', '..'), 'website', 'docs')
const readDoc = (relative: string): string => readFileSync(join(DOCS, relative), 'utf8')

/** Every noun `collapsedDriftLabel` can put after "Show N ". */
const COLLAPSED_NOUNS = [
  collapsedDriftLabel({ snoozed: 0, resolved: 1 }),
  collapsedDriftLabel({ snoozed: 1, resolved: 0 }),
  collapsedDriftLabel({ snoozed: 1, resolved: 1 }),
]

describe('property drift docs agree with the panels', () => {
  it('emits exactly the three collapse nouns the docs have to cover', () => {
    // Pins the shape the assertions below depend on: a fourth branch in
    // `collapsedDriftLabel` would otherwise slip past them unnoticed.
    expect(COLLAPSED_NOUNS).toEqual(['resolved', 'snoozed', 'snoozed or resolved'])
  })

  it('names every collapse noun in the canonical workflow doc', () => {
    const doc = readDoc('use/variables-and-templates.md')
    for (const noun of COLLAPSED_NOUNS) {
      expect(doc, `variables-and-templates.md never mentions "${noun}"`).toContain(noun)
    }
  })

  it('names both revive labels in the canonical workflow doc', () => {
    const doc = readDoc('use/variables-and-templates.md')
    for (const label of Object.values(DRIFT_REVIVE_LABEL)) {
      expect(doc, `variables-and-templates.md never mentions "${label}"`).toContain(label)
    }
  })

  it('does not leave the reference and troubleshooting pages promising only "resolved"', () => {
    // Both pages send a reader to the collapse toggle by name. Neither has to
    // enumerate all three nouns, but a page that says "Show N resolved" and
    // stops there is telling a snoozing reader to look for a control they will
    // not find, so the snoozed reading has to appear somewhere on the page.
    for (const relative of ['use/feature-reference.md', 'use/troubleshooting.md']) {
      const doc = readDoc(relative)
      if (!doc.includes('Show N ')) continue
      expect(doc, `${relative} points at the collapse toggle without naming the snoozed reading`)
        .toContain('snoozed')
    }
  })
})
