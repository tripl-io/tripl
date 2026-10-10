import { APP_LOCALE, MINUS_SIGN } from '@/lib/format'

const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/

/**
 * Parse for display. A bare `YYYY-MM-DD` is a calendar day, not an instant:
 * `new Date('2026-09-24')` reads it as UTC midnight, which west of UTC is still
 * Sep 23 locally. So a date-only string becomes local midnight of that day;
 * anything with a time part keeps the platform's instant parsing.
 */
function parseForDisplay(value: string): Date {
  const dateOnly = DATE_ONLY.exec(value)
  if (dateOnly) {
    return new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
  }
  return new Date(value)
}

// Returns '' for an empty or unparseable input (never the literal "Invalid Date").
export function formatDate(value: string) {
  const date = parseForDisplay(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleDateString(APP_LOCALE, { month: 'short', day: 'numeric', year: 'numeric' })
}

const pad2 = (value: number): string => String(value).padStart(2, '0')

/**
 * `YYYY-MM-DD` of a Date's LOCAL calendar day: a day filter's wire format and
 * `DatePicker`'s value. Built from the local parts, so it is locale-proof and
 * never slips a day the way `toISOString().slice(0, 10)` does away from UTC.
 */
export function toDateKey(date: Date): string {
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`
}

/**
 * `YYYY-MM-DDTHH:mm`, local wall-clock time to the minute: the value
 * `DateTimePicker` reads and writes (the `datetime-local` wire format).
 */
export function toLocalDateTimeValue(date: Date): string {
  return `${toDateKey(date)}T${pad2(date.getHours())}:${pad2(date.getMinutes())}`
}

/**
 * The instant that bounds a local calendar day (`YYYY-MM-DD`, a day filter's
 * value): its first millisecond, or with `'end'` its last. A day filter means
 * the day as the reader sees it, in their own zone, and the server's bounds
 * are inclusive, so "To: Aug 12" has to reach 23:59:59.999 or it drops the
 * very day the reader asked for. Undefined for an empty or unreadable day.
 */
export function dayBoundaryIso(day: string, edge: 'start' | 'end'): string | undefined {
  if (!DATE_ONLY.test(day)) return undefined
  const at = new Date(`${day}T${edge === 'start' ? '00:00:00.000' : '23:59:59.999'}`)
  return Number.isNaN(at.getTime()) ? undefined : at.toISOString()
}

/**
 * The time of day of an instant, in the viewer's zone and the app's 12-hour
 * clock: "9:30 PM". The one clock every short time on a page prints in.
 * Returns '' for an unparseable input.
 */
export function formatTimeOfDay(value: string | Date): string {
  const date = value instanceof Date ? value : new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleTimeString(APP_LOCALE, { hour: 'numeric', minute: '2-digit' })
}

/** `HH:mm` (24-hour) in the app's 12-hour clock: "21:30" -> "9:30 PM". */
export function formatClockTime(time: string): string {
  const [hours = 0, minutes = 0] = time.split(':').map(Number)
  return formatTimeOfDay(new Date(2024, 0, 1, hours, minutes))
}

const TYPED_TIME = /^(\d{1,2})(?::?(\d{2}))?\s*(?:([ap])\.?\s*m?\.?)?$/i

/**
 * A typed time of day as `HH:mm`, in either clock: "9:30 PM", "9:30pm",
 * "9 pm", "21:30" and "0930" all read. Null while the text is not a time yet,
 * so a half-typed "9:3" means nothing.
 */
export function parseClockTime(text: string): string | null {
  const match = TYPED_TIME.exec(text.trim())
  if (!match) return null
  const minutes = match[2] === undefined ? 0 : Number(match[2])
  const meridiem = match[3]?.toLowerCase()
  let hours = Number(match[1])
  if (minutes > 59) return null
  if (meridiem) {
    if (hours < 1 || hours > 12) return null
    hours = (hours % 12) + (meridiem === 'p' ? 12 : 0)
  } else if (hours > 23) {
    return null
  }
  return `${pad2(hours)}:${pad2(minutes)}`
}

// Explicit, unambiguous calendar date as `YYYY-MM-DD`. The bare
// `toLocaleDateString()` default renders US `mm/dd/yyyy` on many hosts, which is
// ambiguous on a mixed-locale (e.g. Europe/Berlin + Russian) instance. Returns
// '' for an empty or unparseable input.
export function formatIsoDate(value: string): string {
  const date = parseForDisplay(value)
  if (Number.isNaN(date.getTime())) return ''
  return toDateKey(date)
}

/**
 * The viewer's UTC offset at `date`, e.g. "UTC+3", "UTC−5:30", "UTC": how the
 * app names the zone a local time is in, wherever local and UTC times meet (a
 * form that takes local time beside charts bucketed in UTC, a short time whose
 * zone is in its hover title). Taken at `date`, so a summer and a winter
 * instant each carry their own offset.
 */
export function formatUtcOffset(date: Date): string {
  const minutes = -date.getTimezoneOffset()
  if (minutes === 0) return 'UTC'
  const sign = minutes > 0 ? '+' : MINUS_SIGN
  const hours = Math.floor(Math.abs(minutes) / 60)
  const rest = Math.abs(minutes) % 60
  return `UTC${sign}${hours}${rest ? `:${pad2(rest)}` : ''}`
}

/**
 * The browser's IANA zone ("Europe/Berlin"), or '' where the runtime does not
 * report one: the zone a page names once for a whole list, where an offset
 * would be right for some rows and wrong for those across a DST change.
 */
export function viewerTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone ?? ''
  } catch {
    return ''
  }
}

/**
 * Date+time of an instant, in the viewer's LOCAL zone and the app locale.
 *
 * Time-zone policy: every instant the app prints — "first
 * seen", delivery times, signal buckets, and the 15-minute / hour / 6-hour
 * ticks and tooltips of the charts (components/ui/chart-format.ts) — reads in
 * the viewer's local zone, so a spike, its signal card and its annotation all
 * say the same time. The exceptions are calendar buckets the server cut in UTC
 * (a chart's day / week / month bucket) and grids built from them (the
 * seasonality heatmap), which are labelled in UTC and say so.
 *
 * Same output as `formatTimestamp` without seconds: the two used to be
 * near-identical copies. Returns '' for an empty or unparseable input
 * (never the literal "Invalid Date").
 */
export function formatDateTime(value: string) {
  return formatTimestamp(value)
}

// Date+time for raw timestamps (metric buckets, "first seen", delivery times)
// in the app locale. Passes explicit field options so it renders a full,
// unambiguous date+time instead of the bare `toLocaleString()` host default
// (`m/d/yyyy, h:mm:ss AM`). Pass `{ seconds: true }` where second-level
// precision matters (e.g. audit log). Pass `{ zone: true }` for a hover title
// that names the zone ("Oct 9, 2026, 1:35:12 PM UTC+2"): the visible time
// stays short, and the title is where a reader checks which zone it is in.
// The zone is named by `formatUtcOffset`, as every other surface names it.
// Returns '' for an empty or unparseable input (never the literal
// "Invalid Date").
export function formatTimestamp(value: string, options: { seconds?: boolean; zone?: boolean } = {}) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const text = date.toLocaleString(APP_LOCALE, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    ...(options.seconds ? { second: '2-digit' } : {}),
  })
  return options.zone ? `${text} ${formatUtcOffset(date)}` : text
}

/**
 * The two halves of a short absolute time, for a cell that sets them on two
 * lines: `{ date: 'Sep 24', time: '6:00 PM' }`. Local zone, the app locale and
 * the same 12-hour clock as {@link formatTimestamp}. The year joins the date
 * only when it is not `now`'s year: repeating "2026" down a newest-first list
 * is noise, but a row from last year must never read as one from this week.
 * Null for an unparseable input.
 */
export function shortTimestampParts(
  value: string,
  now: Date = new Date(),
): { date: string; time: string } | null {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return {
    date: date.toLocaleDateString(APP_LOCALE, {
      month: 'short',
      day: 'numeric',
      ...(date.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }),
    }),
    time: formatTimeOfDay(date),
  }
}

/**
 * A short absolute time for a tight column: "Sep 24, 6:00 PM", or
 * "Sep 24, 2025, 6:00 PM" outside the current year (see
 * {@link shortTimestampParts}). With `today`, an instant on the current day
 * reads "Today 6:00 PM". Returns '' for an unparseable input.
 */
export function formatShortTimestamp(
  value: string,
  options: { now?: Date; today?: boolean } = {},
): string {
  const now = options.now ?? new Date()
  const parts = shortTimestampParts(value, now)
  if (!parts) return ''
  if (options.today && new Date(value).toDateString() === now.toDateString()) return `Today ${parts.time}`
  return `${parts.date}, ${parts.time}`
}

export function formatRelativeTime(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return 'never'
  const ts = Date.parse(iso)
  if (Number.isNaN(ts)) return 'never'
  const diffSec = Math.max(0, Math.round((now - ts) / 1000))
  if (diffSec < 60) return 'just now'
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`
  const days = Math.floor(diffSec / 86400)
  if (days < 30) return `${days}d ago`
  if (days < 365) return `${Math.floor(days / 30)}mo ago`
  return `${Math.floor(days / 365)}y ago`
}
