import { describe, expect, it } from 'vitest'

import {
  INBOX_MUTE_CHOICES,
  INDEFINITE_MUTE,
  MUTE_PRESETS,
  muteChoiceName,
  muteChoiceUntilIso,
  muteName,
  muteUntilIso,
  unmuteName,
} from './mutePresets'
import { at } from '@/test/at'

/**
 * The two lists are one module because the WORDING and the wire values must not
 * drift between surfaces — and they are two lists, not one, for a
 * reason the type system enforces and these tests pin behaviourally.
 */
describe('mute presets are durations, and only durations', () => {
  it('holds only finite positive durations, and never the open-ended option', () => {
    // All three mute surfaces map MUTE_PRESETS. Folding the open-ended choice
    // into it — the obvious "simplification" — grows a button on the two RULE
    // surfaces that posts a NULL `muted_until`, which `is_rule_muted()` reads
    // as NOT MUTED: a control labelled "until I unmute" that unmutes the rule.
    expect(MUTE_PRESETS.length).toBeGreaterThan(0)
    for (const preset of MUTE_PRESETS) {
      expect(typeof preset.ms).toBe('number')
      expect(Number.isFinite(preset.ms)).toBe(true)
      expect(preset.ms).toBeGreaterThan(0)
      expect(preset.label).not.toBe(INDEFINITE_MUTE.label)
    }
  })

  it('offers the inbox the timed presets plus exactly one open-ended choice', () => {
    // Order matters as much as membership: the timed presets stay first so the
    // riskiest choice is never the leftmost, default-looking button.
    expect(INBOX_MUTE_CHOICES).toHaveLength(MUTE_PRESETS.length + 1)
    expect(INBOX_MUTE_CHOICES.slice(0, MUTE_PRESETS.length)).toEqual([...MUTE_PRESETS])
    expect(INBOX_MUTE_CHOICES.filter(choice => choice.ms === null)).toEqual([INDEFINITE_MUTE])
  })

  it('words the open-ended option once, here', () => {
    // A second surface inventing "Forever" or "Indefinitely" is the
    // two-vocabularies-for-one-idea drift this module exists to stop.
    expect(INDEFINITE_MUTE.label).toBe('Until I unmute')
  })
})

describe('a choice resolves to what goes on the wire', () => {
  it('renders a duration against the injected clock', () => {
    // Injectable `now`, so this asserts a boundary rather than racing the wall
    // clock — and so a re-hand-rolled local resolver cannot quietly return a
    // different instant for the same preset.
    const now = Date.parse('2026-08-14T10:00:00Z')
    expect(muteChoiceUntilIso({ label: '1h', ms: 3_600_000 }, now)).toBe('2026-08-14T11:00:00.000Z')
    expect(muteChoiceUntilIso(at(MUTE_PRESETS, 0), now)).toBe(muteUntilIso(at(MUTE_PRESETS, 0).ms, now))
  })

  it('resolves the open-ended choice to null, strictly — never undefined', () => {
    // `undefined` would be OMITTED by the object spread that assembles the
    // request body, so the mute would post `{action: 'mute'}` with no
    // `muted_until` at all: a different request meaning "unspecified" rather
    // than "deliberately open-ended".
    const resolved = muteChoiceUntilIso(INDEFINITE_MUTE, Date.parse('2026-08-14T10:00:00Z'))
    expect(resolved).toBeNull()
    expect(resolved).not.toBeUndefined()
  })
})

/**
 * A choice resolves to WORDS as well as to a wire value, and this block is
 * where those words are pinned in English.
 *
 * THIS FILE IS THE ORACLE. Every expectation below is a literal string, and
 * deliberately so: the three surfaces now build their `aria-label`s by calling
 * these functions, so a test that ALSO derived its expectation from them would
 * move whenever the builder moved and assert nothing about the wording. Rename
 * a sentence inside `mutePresets.ts` and this file goes red with the old and
 * the new English side by side in the diff — that is the whole job.
 *
 * The three surface test files keep their own literals for the complementary
 * reason: they now compare a shared builder's output against a sentence written
 * independently per file, which is a cross-surface check none of them could
 * make while all three components hardcoded the same template.
 *
 * `TARGET` is adversarial on purpose. It contains no preset label as a
 * substring ('1h' / '24h' / '7d') and no regex metacharacter, so the
 * `toContain` assertions below cannot pass for the wrong reason and a name
 * built from it is safe to feed to `new RegExp`.
 */
describe('a choice resolves to the sentence a screen reader announces', () => {
  const TARGET = 'checkout'

  it('words all four shared forms, once, here', () => {
    expect(muteName(TARGET)).toBe('Mute checkout')
    expect(unmuteName(TARGET)).toBe('Unmute checkout')
    expect(muteChoiceName(TARGET, { label: '1h', ms: 3_600_000 })).toBe('Mute checkout for 1h')
    expect(muteChoiceName(TARGET, INDEFINITE_MUTE)).toBe('Mute checkout until unmuted')
  })

  it('names the target it is given, and nothing else', () => {
    // Guards the degenerate builder that ignores its argument and the one that
    // closes over a fixture name. Every surface reads its target from loaded
    // data, so a builder blind to it would still look right on one screen.
    expect(muteName('a')).not.toBe(muteName('b'))
    expect(unmuteName('a')).not.toBe(unmuteName('b'))
    expect(muteChoiceName('a', at(MUTE_PRESETS, 0))).not.toBe(muteChoiceName('b', at(MUTE_PRESETS, 0)))
  })

  it('phrases the open-ended choice from `ms`, never from the label', () => {
    // The asymmetry, asserted rather than described. Splicing the label into
    // the duration sentence yields "Mute checkout for Until I unmute", which is
    // not English — so the open-ended button's visible face
    // (INDEFINITE_MUTE.label) and its accessible name are different strings on
    // purpose. This used to be a comment beside the one call site that had the
    // branch; a fourth mute surface would have had to rediscover it.
    expect(muteChoiceName(TARGET, INDEFINITE_MUTE)).not.toContain('for Until I unmute')
    expect(muteChoiceName(TARGET, { label: 'anything at all', ms: null })).toBe(
      'Mute checkout until unmuted',
    )
    // …and the inverse, which looks absurd on purpose: the branch keys on `ms`,
    // not on wording that happens to read open-ended. That is what keeps
    // `MUTE_PRESETS`' `ms: number` a real type guard rather than a coincidence —
    // a builder taking a label string could be handed INDEFINITE_MUTE.label by
    // a rule surface, and `is_rule_muted()` reads the value that button writes
    // as NOT MUTED.
    expect(muteChoiceName(TARGET, { label: 'Until I unmute', ms: 3_600_000 })).toBe(
      'Mute checkout for Until I unmute',
    )
  })

  it('keeps every visible duration inside the name it is announced by (WCAG 2.5.3)', () => {
    // Label in Name: the face of each preset button is its bare duration, so
    // "click 1h" must keep working for speech input — while the name still says
    // WHOSE alerts stop, which is the gap tripl-in45 closed. Looping over
    // MUTE_PRESETS rather than over three literals is right here: a fourth
    // preset added next year is covered automatically, and the duration
    // literals themselves are pinned by the three surface test files.
    for (const preset of MUTE_PRESETS) {
      const name = muteChoiceName(TARGET, preset)
      expect(name).toContain(preset.label)
      expect(name).toContain(TARGET)
      // …and the bare duration is not the WHOLE name. Without this, the
      // degenerate builder `(target, preset) => preset.label` satisfies the
      // line above and reinstates the very defect tripl-in45 fixed.
      expect(name).not.toBe(preset.label)
    }
    expect(muteName(TARGET)).toContain('Mute')
    expect(unmuteName(TARGET)).toContain('Unmute')
  })

  it('records the one form that deliberately breaks Label in Name, so it stays the only one', () => {
    // Stated out loud rather than left to the loop above not covering it: the
    // open-ended button's visible face is "Until I unmute" and its accessible
    // name is "Mute <target> until unmuted", which does NOT contain that
    // phrase — a speech-input user saying "click Until I unmute" does not
    // activate it. That is the accepted cost of not writing "for Until I
    // unmute", and it is confined to exactly one choice on exactly one surface.
    // A fifth form must either satisfy the loop above or be added here,
    // visibly, with a reason.
    expect(muteChoiceName(TARGET, INDEFINITE_MUTE)).not.toContain(INDEFINITE_MUTE.label)
    expect(INBOX_MUTE_CHOICES.filter(choice => choice.ms === null)).toHaveLength(1)
  })

  it('gives every offered control a distinct name on one target', () => {
    // Operational, not cosmetic: every one of these strings is used as a
    // `getByRole({ name })` selector, and two forms collapsing into one turns
    // the surface suites into "found multiple elements" failures that read like
    // flakes. Note "Mute checkout" IS a prefix of "Mute checkout for 1h" and
    // that is fine — RTL matches a string `name` in full — but it is why a
    // surface must not reach for /^Mute /-style regexes while the duration
    // panel is open.
    const names = [
      muteName(TARGET),
      unmuteName(TARGET),
      ...INBOX_MUTE_CHOICES.map(choice => muteChoiceName(TARGET, choice)),
    ]
    expect(new Set(names).size).toBe(names.length)
  })
})
