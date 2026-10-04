import { describe, expect, it } from 'vitest'

import {
  DEFAULT_CADENCE,
  cadenceToCron,
  cronToCadence,
  describeCron,
  formatInProjectZone,
  resolveScheduleTimezone,
  validateCadence,
  type CadenceDraft,
} from './deliverySchedule'

const draft = (over: Partial<CadenceDraft>): CadenceDraft => ({ ...DEFAULT_CADENCE, ...over })

describe('cadenceToCron', () => {
  it('means "immediate" with a null expression, which is the wire default', () => {
    expect(cadenceToCron(draft({ mode: 'immediate' }))).toBeNull()
  })

  it('builds the expression each preset promises', () => {
    expect(cadenceToCron(draft({ mode: 'hourly' }))).toBe('0 * * * *')
    expect(cadenceToCron(draft({ mode: 'daily', time: '09:00' }))).toBe('0 9 * * *')
    expect(cadenceToCron(draft({ mode: 'daily', time: '18:30' }))).toBe('30 18 * * *')
    expect(cadenceToCron(draft({ mode: 'weekly', time: '07:15', weekday: 1 }))).toBe('15 7 * * 1')
    expect(cadenceToCron(draft({ mode: 'times_of_day', times: '09:00, 18:00' }))).toBe(
      '0 9,18 * * *',
    )
  })

  it('passes a custom expression through untouched apart from whitespace', () => {
    expect(cadenceToCron(draft({ mode: 'custom', cron: '  */5   *  * * 1-5 ' }))).toBe(
      '*/5 * * * 1-5',
    )
  })
})

describe('cronToCadence', () => {
  it('round-trips every preset', () => {
    for (const value of [
      draft({ mode: 'hourly' }),
      draft({ mode: 'daily', time: '09:00' }),
      draft({ mode: 'daily', time: '18:30' }),
      draft({ mode: 'weekly', time: '07:15', weekday: 3 }),
      draft({ mode: 'times_of_day', times: '09:00, 18:00' }),
    ]) {
      const cron = cadenceToCron(value)
      const back = cronToCadence(cron)
      expect(cadenceToCron(back)).toBe(cron)
      expect(back.mode).toBe(value.mode)
    }
  })

  it('reads an empty or missing expression as immediate', () => {
    expect(cronToCadence(null).mode).toBe('immediate')
    expect(cronToCadence(undefined).mode).toBe('immediate')
    expect(cronToCadence('   ').mode).toBe('immediate')
  })

  it('keeps an expression the presets cannot express in custom mode, verbatim', () => {
    // A hand-written cron must never be silently rewritten into a preset that
    // means something slightly different.
    const exotic = '*/5 9-17 * * 1-5'
    const back = cronToCadence(exotic)
    expect(back.mode).toBe('custom')
    expect(cadenceToCron(back)).toBe(exotic)
  })

  it('treats Sunday written as 7 the way cron does', () => {
    expect(cronToCadence('0 9 * * 7').weekday).toBe(0)
  })
})

describe('describeCron', () => {
  it('says plainly what happens, including the unchanged default', () => {
    expect(describeCron(null)).toBe('Immediately, after every collection')
    expect(describeCron('0 9 * * *')).toBe('Daily at 09:00')
    expect(describeCron('0 * * * *')).toBe('Hourly, on the hour')
    expect(describeCron('0 9,18 * * *')).toBe('Every day at 09:00, 18:00')
    expect(describeCron('15 7 * * 1')).toBe('Every Monday at 07:15')
    expect(describeCron('*/5 9-17 * * 1-5')).toContain('Custom')
    // The weekday preset is named on every surface, not only the card.
    expect(describeCron('0 9 * * 1-5')).toBe('Weekdays at 09:00')
  })
})

describe('validateCadence', () => {
  it('accepts what the presets produce', () => {
    expect(validateCadence(draft({ mode: 'immediate' }))).toBeNull()
    expect(validateCadence(draft({ mode: 'daily', time: '09:00' }))).toBeNull()
    expect(validateCadence(draft({ mode: 'times_of_day', times: '09:00, 18:00' }))).toBeNull()
    expect(validateCadence(draft({ mode: 'custom', cron: '0 9 * * *' }))).toBeNull()
  })

  it('catches the common typos before a round trip', () => {
    expect(validateCadence(draft({ mode: 'daily', time: '9am' }))).toContain('HH:MM')
    expect(validateCadence(draft({ mode: 'daily', time: '25:00' }))).toContain('HH:MM')
    expect(validateCadence(draft({ mode: 'times_of_day', times: '' }))).toContain('at least one')
    expect(validateCadence(draft({ mode: 'custom', cron: '@daily' }))).toContain('5 fields')
    expect(validateCadence(draft({ mode: 'custom', cron: '' }))).toContain('cron expression')
  })
})

describe('times-of-day cannot become a cron cross-product', () => {
  // Cron multiplies its minute and hour fields: `0,30 9,18 * * *` fires FOUR
  // times a day, not two. Serializing "09:00, 18:30" that way would schedule
  // digests nobody asked for, which is the one thing a delivery schedule must
  // never do.
  it('rejects a mixed-minute list instead of silently adding fire times', () => {
    const draft: CadenceDraft = { ...DEFAULT_CADENCE, mode: 'times_of_day', times: '09:00, 18:30' }

    expect(validateCadence(draft)).toContain('same minute')
    // ...and the serializer refuses outright rather than emitting the
    // cross-product OR a plausible-looking cron built from what survived.
    expect(cadenceToCron(draft)).toBeNull()
  })

  it('accepts a shared-minute list and emits exactly those times', () => {
    const draft: CadenceDraft = { ...DEFAULT_CADENCE, mode: 'times_of_day', times: '09:15, 18:15' }

    expect(validateCadence(draft)).toBeNull()
    expect(cadenceToCron(draft)).toBe('15 9,18 * * *')
    expect(describeCron('15 9,18 * * *')).toBe('Every day at 09:15, 18:15')
  })
})

describe('formatInProjectZone', () => {
  // The schedule is a wall-clock time in the PROJECT's zone. Rendering the next
  // fire in the viewer's zone would show an operator abroad "07:00" for a
  // schedule they set to 09:00.
  const nineMoscow = '2026-09-06T06:00:00Z'

  it('renders the instant in the project zone and names it', () => {
    const shown = formatInProjectZone(nineMoscow, 'Europe/Moscow')

    expect(shown).toContain('9:00')
    expect(shown).toContain('Europe/Moscow')
  })

  it('shows the same instant differently for a different project zone', () => {
    expect(formatInProjectZone(nineMoscow, 'UTC')).toContain('6:00')
  })

  it('degrades instead of blanking when the zone is unusable', () => {
    expect(formatInProjectZone(nineMoscow, 'Mars/Olympus_Mons')).toContain('local')
    expect(formatInProjectZone('not-a-date', 'UTC')).toBe('not-a-date')
  })
})

describe('resolveScheduleTimezone', () => {
  // The create path is the one that was wrong: `editingDestination` is null, so
  // a `destination-first` chain fell through to a hard-coded 'UTC' and the form
  // promised 09:00 UTC for a cron the server reads as 09:00 Moscow.
  it('uses the project zone when creating, where there is no destination yet', () => {
    expect(resolveScheduleTimezone({ timezone: 'Europe/Moscow' }, null)).toBe('Europe/Moscow')
  })

  it('uses the project zone when editing too — the project is the source of truth', () => {
    expect(
      resolveScheduleTimezone({ timezone: 'Europe/Moscow' }, { project_timezone: 'UTC' }),
    ).toBe('Europe/Moscow')
  })

  it('falls back to the destination while the project query is still in flight', () => {
    expect(resolveScheduleTimezone(undefined, { project_timezone: 'Asia/Tokyo' })).toBe(
      'Asia/Tokyo',
    )
  })

  it('only reaches UTC when nothing knows better', () => {
    expect(resolveScheduleTimezone(undefined, null)).toBe('UTC')
    // An older fixture or a pre-column response carries no timezone at all.
    expect(resolveScheduleTimezone({}, {})).toBe('UTC')
  })
})

describe('an invalid times-of-day draft never becomes a valid cron', () => {
  // `DeliveryScheduleField` seeds the Custom box from `cadenceToCron(draft)`
  // when the mode changes. A lenient serializer therefore turns a REJECTED
  // draft into a perfectly valid schedule the moment the operator switches to
  // Custom — which is precisely the "a typo must never change the schedule"
  // guarantee the component exists to make.
  it('refuses a mixed-minute list rather than moving a send', () => {
    const draft: CadenceDraft = { ...DEFAULT_CADENCE, mode: 'times_of_day', times: '09:00, 18:30' }

    // Would otherwise seed Custom with `0 9,18 * * *` — 18:00, not 18:30.
    expect(cadenceToCron(draft)).toBeNull()
    expect(validateCadence(draft)).toContain('same minute')
  })

  it('refuses a list with an unparseable entry rather than dropping it', () => {
    const draft: CadenceDraft = { ...DEFAULT_CADENCE, mode: 'times_of_day', times: '09:00, banana' }

    expect(cadenceToCron(draft)).toBeNull()
    expect(validateCadence(draft)).toContain('HH:MM')
  })

  it('still serializes a list that is actually valid', () => {
    const draft: CadenceDraft = { ...DEFAULT_CADENCE, mode: 'times_of_day', times: '09:00, 18:00' }

    expect(validateCadence(draft)).toBeNull()
    expect(cadenceToCron(draft)).toBe('0 9,18 * * *')
  })

  it('matches how daily and weekly already treat an unusable time', () => {
    expect(cadenceToCron({ ...DEFAULT_CADENCE, mode: 'daily', time: '9am' })).toBeNull()
    expect(cadenceToCron({ ...DEFAULT_CADENCE, mode: 'weekly', time: '25:00' })).toBeNull()
    expect(
      cadenceToCron({ ...DEFAULT_CADENCE, mode: 'times_of_day', times: 'nope' }),
    ).toBeNull()
  })
})
