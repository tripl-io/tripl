/**
 * The delivery cadence of an alert destination, as the form sees it.
 *
 * The wire format is a single 5-field cron string (or null for "immediate"),
 * because one encoding in the database means one parser in the worker. The
 * presets below are a UI affordance over that string: picking "Daily" and a
 * time writes `0 9 * * *`, and reading `0 9 * * *` back selects "Daily" at
 * 09:00. Anything the presets cannot express round-trips through the Custom
 * mode unchanged, so a hand-written cron is never silently rewritten.
 */

import { APP_LOCALE } from '@/lib/format'

export type CadenceMode = 'immediate' | 'hourly' | 'daily' | 'times_of_day' | 'weekly' | 'custom'

export interface CadenceDraft {
  mode: CadenceMode
  /** "HH:MM" for daily/weekly. */
  time: string
  /** Comma-separated "HH:MM" list for times_of_day. */
  times: string
  /** 0 = Sunday .. 6 = Saturday, for weekly. */
  weekday: number
  /** The raw expression, authoritative in custom mode. */
  cron: string
}

export const WEEKDAY_LABELS = [
  'Sunday',
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
] as const

export const CADENCE_OPTIONS: { value: CadenceMode; label: string; hint: string }[] = [
  {
    value: 'immediate',
    label: 'Immediately',
    hint: 'Send as soon as a collection finds something. This is the default.',
  },
  { value: 'hourly', label: 'Hourly', hint: 'One message per hour, on the hour.' },
  { value: 'daily', label: 'Daily', hint: 'One message a day, at the time you choose.' },
  {
    value: 'times_of_day',
    label: 'Several times a day',
    hint: 'One message at each time you list.',
  },
  { value: 'weekly', label: 'Weekly', hint: 'One message a week.' },
  { value: 'custom', label: 'Custom (cron)', hint: 'A 5-field cron expression.' },
]

export const DEFAULT_CADENCE: CadenceDraft = {
  mode: 'immediate',
  time: '09:00',
  times: '09:00, 18:00',
  weekday: 1,
  cron: '',
}

const TIME_PATTERN = /^([01]?\d|2[0-3]):([0-5]\d)$/

function parseTime(value: string): { hour: number; minute: number } | null {
  const match = TIME_PATTERN.exec(value.trim())
  if (!match) return null
  return { hour: Number(match[1]), minute: Number(match[2]) }
}

function formatTime(hour: number, minute: number): string {
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
}

/** The cron expression a draft means, or null for "immediate". */
export function cadenceToCron(draft: CadenceDraft): string | null {
  switch (draft.mode) {
    case 'immediate':
      return null
    case 'hourly':
      return '0 * * * *'
    case 'daily': {
      const at = parseTime(draft.time)
      return at ? `${at.minute} ${at.hour} * * *` : null
    }
    case 'times_of_day': {
      const parts = draft.times.split(',').map(part => part.trim()).filter(Boolean)
      const parsed = parts.map(part => parseTime(part))
      if (parsed.length === 0) return null
      // STRICT, like `daily` and `weekly` above: an entry that does not parse,
      // or a list whose times do not share a minute, yields null rather than a
      // cron built from whatever survived.
      //
      // Being lenient here is not a smaller sin than emitting a bad schedule —
      // it IS one. `DeliveryScheduleField` seeds the Custom box from
      // `cadenceToCron(draft)` when the mode changes, so a lenient reading
      // turned "09:00, 18:30" (rejected by validation, never published) into a
      // perfectly valid `0 9,18 * * *` the moment the operator switched to
      // Custom — silently moving the second send half an hour and defeating the
      // component's whole "a typo must never change the schedule" guarantee.
      if (parsed.some(at => at === null)) return null
      const minutes = new Set(parsed.map(at => at!.minute))
      if (minutes.size > 1) return null
      const hours = [...new Set(parsed.map(at => at!.hour))].sort((a, b) => a - b)
      return `${parsed[0]!.minute} ${hours.join(',')} * * *`
    }
    case 'weekly': {
      const at = parseTime(draft.time)
      return at ? `${at.minute} ${at.hour} * * ${draft.weekday}` : null
    }
    case 'custom': {
      const cleaned = draft.cron.trim().replace(/\s+/g, ' ')
      return cleaned === '' ? null : cleaned
    }
  }
}

/** The draft that best represents a stored expression. */
export function cronToCadence(cron: string | null | undefined): CadenceDraft {
  if (!cron || cron.trim() === '') return { ...DEFAULT_CADENCE }
  const cleaned = cron.trim().replace(/\s+/g, ' ')
  const fields = cleaned.split(' ')
  const base: CadenceDraft = { ...DEFAULT_CADENCE, mode: 'custom', cron: cleaned }
  const [minute, hour, dom, month, dow] = fields
  if (
    fields.length !== 5 ||
    minute === undefined ||
    hour === undefined ||
    dom === undefined ||
    month === undefined ||
    dow === undefined
  ) {
    return base
  }
  const everyDay = dom === '*' && month === '*'
  const isNumber = (value: string) => /^\d+$/.test(value)
  const isNumberList = (value: string) => /^\d+(,\d+)*$/.test(value)

  if (everyDay && dow === '*' && minute === '0' && hour === '*') {
    return { ...base, mode: 'hourly' }
  }
  if (everyDay && dow === '*' && isNumber(minute) && isNumber(hour)) {
    return { ...base, mode: 'daily', time: formatTime(Number(hour), Number(minute)) }
  }
  if (everyDay && dow === '*' && isNumber(minute) && isNumberList(hour) && hour.includes(',')) {
    const times = hour
      .split(',')
      .map(part => formatTime(Number(part), Number(minute)))
      .join(', ')
    return { ...base, mode: 'times_of_day', times }
  }
  if (everyDay && isNumber(dow) && isNumber(minute) && isNumber(hour)) {
    return {
      ...base,
      mode: 'weekly',
      time: formatTime(Number(hour), Number(minute)),
      weekday: Number(dow) % 7,
    }
  }
  return base
}

/**
 * The zone a destination's schedule should be READ in.
 *
 * The project is the source of truth, not the destination being edited. On the
 * create path there is no destination yet, and defaulting to 'UTC' there told
 * an operator on a Europe/Moscow project that "daily at 09:00" meant 09:00 UTC
 * while the server would read the very same cron as 09:00 Moscow.
 *
 * A pure function rather than an inline `??` chain because the create path is
 * the one that was wrong and the one a component test cannot easily reach —
 * the cadence picker is a Radix Select, and the zone is only rendered once a
 * non-immediate cadence is chosen.
 */
export function resolveScheduleTimezone(
  project: { timezone?: string } | undefined | null,
  destination: { project_timezone?: string } | undefined | null,
): string {
  return project?.timezone || destination?.project_timezone || 'UTC'
}

/**
 * An absolute instant rendered in the PROJECT's timezone, with the zone named.
 *
 * Formatting without a `timeZone` renders in the viewer's zone, which is actively
 * misleading here: the schedule was written as a wall-clock time in the
 * project's zone, so an operator in another country would read "next digest
 * 07:00" for a schedule they set to 09:00 and reasonably conclude it was wrong.
 */
export function formatInProjectZone(iso: string, timeZone: string | undefined): string {
  const at = new Date(iso)
  if (Number.isNaN(at.getTime())) return iso
  const zone = timeZone || 'UTC'
  try {
    return `${at.toLocaleString(APP_LOCALE, { timeZone: zone, dateStyle: 'medium', timeStyle: 'short' })} (${zone})`
  } catch {
    // An unresolvable zone must not blank the tooltip; the backend degrades to
    // UTC for the same reason.
    return `${at.toLocaleString(APP_LOCALE, { dateStyle: 'medium', timeStyle: 'short' })} (local)`
  }
}

/** A one-line description of a cron expression, for the card and the form. */
// `M H * * 1-5`: the common custom cron the cadence editor has no mode for,
// named in words on every surface that describes a schedule.
const WEEKDAYS_CRON = /^(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+1-5$/

function describeWeekdaysCron(cron: string): string | null {
  const match = WEEKDAYS_CRON.exec(cron.trim())
  if (!match) return null
  const minute = Number(match[1])
  const hour = Number(match[2])
  if (minute >= 60 || hour >= 24) return null
  return `Weekdays at ${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`
}

export function describeCron(cron: string | null | undefined): string {
  if (!cron || cron.trim() === '') return 'Immediately, after every collection'
  const weekdays = describeWeekdaysCron(cron)
  if (weekdays) return weekdays
  const draft = cronToCadence(cron)
  switch (draft.mode) {
    case 'hourly':
      return 'Hourly, on the hour'
    case 'daily':
      return `Daily at ${draft.time}`
    case 'times_of_day':
      return `Every day at ${draft.times}`
    case 'weekly':
      return `Every ${WEEKDAY_LABELS[draft.weekday]} at ${draft.time}`
    default:
      return `Custom schedule (${cron.trim()})`
  }
}

/**
 * Why this draft cannot be saved, or null when it can.
 *
 * Client-side only, and deliberately narrow: the backend validates the cron
 * itself and owns the final word. This exists so the common typo is caught
 * before a round trip, not to reimplement the parser.
 */
export function validateCadence(draft: CadenceDraft): string | null {
  switch (draft.mode) {
    case 'daily':
    case 'weekly':
      return parseTime(draft.time) ? null : 'Enter a time as HH:MM, for example 09:00.'
    case 'times_of_day': {
      const parts = draft.times.split(',').map(part => part.trim()).filter(Boolean)
      if (parts.length === 0) return 'List at least one time, for example 09:00, 18:00.'
      const parsed = parts.map(part => parseTime(part))
      if (parsed.some(at => at === null)) {
        return 'Enter each time as HH:MM, separated by commas.'
      }
      // A cron expression multiplies its minute and hour fields together, so
      // "09:00, 18:30" would also fire at 09:30 and 18:00 — sends nobody asked
      // for. The preset is honest about what it can express instead.
      const minutes = new Set(parsed.map(at => at!.minute))
      return minutes.size === 1
        ? null
        : 'Every time must share the same minute (09:00, 18:00). For mixed minutes use Custom (cron).'
    }
    case 'custom': {
      const cleaned = draft.cron.trim().replace(/\s+/g, ' ')
      if (cleaned === '') return 'Enter a cron expression, or choose another cadence.'
      return cleaned.split(' ').length === 5
        ? null
        : 'A cron expression has 5 fields: minute hour day-of-month month day-of-week.'
    }
    default:
      return null
  }
}
