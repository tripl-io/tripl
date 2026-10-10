/**
 * Time zone names for the settings pages: the project's Timezone select and
 * the browser zone Profile shows.
 *
 * Chromium enumerates and resolves zones by their CLDR ids, which keep names
 * the IANA database has since renamed: `Asia/Calcutta` for Kolkata,
 * `Europe/Kiev` for Kyiv. Someone looking for "Kolkata" found nothing, so the
 * renamed ones are shown, and saved, under their current name. The backend
 * (`validate_timezone`, Python's zoneinfo) accepts the names on both sides.
 */
const CURRENT_ZONE_NAMES: Readonly<Record<string, string>> = {
  'Africa/Asmera': 'Africa/Asmara',
  'America/Buenos_Aires': 'America/Argentina/Buenos_Aires',
  'America/Catamarca': 'America/Argentina/Catamarca',
  'America/Cordoba': 'America/Argentina/Cordoba',
  'America/Godthab': 'America/Nuuk',
  'America/Indianapolis': 'America/Indiana/Indianapolis',
  'America/Jujuy': 'America/Argentina/Jujuy',
  'America/Louisville': 'America/Kentucky/Louisville',
  'America/Mendoza': 'America/Argentina/Mendoza',
  'Asia/Calcutta': 'Asia/Kolkata',
  'Asia/Katmandu': 'Asia/Kathmandu',
  'Asia/Rangoon': 'Asia/Yangon',
  'Asia/Saigon': 'Asia/Ho_Chi_Minh',
  'Atlantic/Faeroe': 'Atlantic/Faroe',
  'Europe/Kiev': 'Europe/Kyiv',
  'Pacific/Enderbury': 'Pacific/Kanton',
  'Pacific/Ponape': 'Pacific/Pohnpei',
  'Pacific/Truk': 'Pacific/Chuuk',
}

/**
 * Whether the browser knows `zone` as an IANA time zone. The server validates
 * too (backend `validate_timezone`); this catches a typo before the round trip.
 */
export function isKnownTimeZone(zone: string): boolean {
  if (!zone) return false
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: zone })
    return true
  } catch {
    return false
  }
}

/**
 * The current IANA name of `zone`: `Asia/Kolkata` for `Asia/Calcutta`. A name
 * that was not renamed, or whose new name this browser cannot resolve, comes
 * back as it is.
 */
export function currentZoneName(zone: string): string {
  const renamed = CURRENT_ZONE_NAMES[zone]
  return renamed && isKnownTimeZone(renamed) ? renamed : zone
}

/**
 * `zone`'s offset from UTC at `at`, as "UTC+05:30", or null when the browser
 * cannot say. It is the offset of that moment, so summer time moves it.
 */
export function zoneOffsetLabel(zone: string, at: Date = new Date()): string | null {
  try {
    const part = new Intl.DateTimeFormat('en-US', { timeZone: zone, timeZoneName: 'longOffset' })
      .formatToParts(at)
      .find((p) => p.type === 'timeZoneName')
    if (!part) return null
    // "GMT+05:30", or a bare "GMT" from an engine that drops a zero offset.
    const offset = part.value.replace(/^GMT/, '')
    return `UTC${offset || '+00:00'}`
  } catch {
    return null
  }
}

export type TimeZoneOption = { value: string; label: string }

/** "Asia/Kolkata (UTC+05:30)": the name first, see {@link timeZoneOptions}. */
function zoneLabel(zone: string, at: Date): string {
  const offset = zoneOffsetLabel(zone, at)
  return offset ? `${zone} (${offset})` : zone
}

// The browser's zones are labelled once a day: a formatter per zone is too
// much to build again each time the select changes.
let browserZones: { day: string; options: TimeZoneOption[] } | null = null

function browserZoneOptions(now: Date): TimeZoneOption[] {
  const day = now.toISOString().slice(0, 10)
  if (browserZones?.day === day) return browserZones.options
  let zones: string[] = []
  try {
    zones = Intl.supportedValuesOf('timeZone')
  } catch {
    /* an engine without supportedValuesOf still offers UTC and the current value */
  }
  const names = [...new Set(zones.map(currentZoneName))].filter((zone) => zone !== 'UTC').sort()
  const options = names.map((zone) => ({ value: zone, label: zoneLabel(zone, now) }))
  browserZones = { day, options }
  return options
}

/**
 * The zones the Timezone select offers: UTC first, then every IANA zone the
 * browser knows, under its current name and with its offset after the name.
 * The name leads so a native select's type-ahead, which matches the start of
 * a label, still jumps to "Europe/Moscow" as the field's hint says.
 *
 * The field used to be free text, so `Europe/Moskow` could reach the server —
 * and the zone drives alert digest schedules. A stored value the list lacks
 * (a zone saved under its old name, or one this browser does not know) stays
 * selectable, so opening the page never silently changes it.
 */
export function timeZoneOptions(current: string, now: Date = new Date()): TimeZoneOption[] {
  const options: TimeZoneOption[] = [{ value: 'UTC', label: 'UTC' }, ...browserZoneOptions(now)]
  if (current && !options.some((option) => option.value === current)) {
    options.unshift({
      value: current,
      label: isKnownTimeZone(current) ? zoneLabel(current, now) : `${current} (not recognised)`,
    })
  }
  return options
}
