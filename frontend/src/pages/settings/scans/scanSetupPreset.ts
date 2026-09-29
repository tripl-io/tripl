import type { ScanConfigPreview, ScanPreviewEventProperties, ScanSetupPreset } from '@/types'

/**
 * The "event + properties" setup (F23.4c): one row per event, a column naming
 * it and a JSON column holding its properties. The backend maps the preset onto
 * the ordinary fields (name format `{event}`, no JSON value paths, no Event type
 * column, no group rules), so all the form has to collect is the two columns.
 */
export const SETUP_PRESET_OPTIONS: {
  value: ScanSetupPreset
  label: string
  description: string
}[] = [
  {
    value: 'event_properties',
    label: 'Event + properties',
    description:
      'One row per event: a column holds the event name and a JSON column its properties. Each name becomes an event, and every key of the JSON becomes one of its properties.',
  },
  {
    value: 'custom',
    label: 'Custom',
    description:
      'Choose the event type column, the name format, which JSON keys to keep as values, and grouping rules yourself.',
  },
]

/** What the event type select offers when the preset may create its own. */
export const PRESET_EVENT_TYPE_LABEL = 'Events (created if missing)'

// Lower-cased column names that usually hold an event's name or its properties,
// most specific first. Only a starting point: the user sees and can change both.
const EVENT_COLUMN_NAMES = ['event', 'event_name', 'eventname', 'event_type', 'action', 'name']
const PROPERTIES_COLUMN_NAMES = [
  'properties',
  'props',
  'event_properties',
  'event_params',
  'params',
  'payload',
  'attributes',
  'data',
]

/**
 * The columns a properties column can be picked from: the JSON-typed ones.
 *
 * Read from `json_columns`, the backend's own classification (JSON, Map,
 * struct, jsonb…), so the form and the scan cannot disagree about what counts.
 */
export function jsonColumnNames(preview: ScanConfigPreview | null): string[] {
  return preview?.json_columns.map(column => column.column) ?? []
}

/** The non-JSON columns, which are what an event name can be read from. */
export function scalarColumnNames(preview: ScanConfigPreview | null): string[] {
  if (!preview) return []
  const json = new Set(jsonColumnNames(preview))
  return preview.columns.map(column => column.name).filter(name => !json.has(name))
}

function byName(candidates: string[], names: string[]): string {
  const lower = new Map(candidates.map(name => [name.toLowerCase(), name]))
  for (const wanted of names) {
    const match = lower.get(wanted)
    if (match) return match
  }
  return ''
}

/** A first guess at the event column, or '' when nothing looks like one. */
export function guessEventColumn(preview: ScanConfigPreview | null): string {
  return byName(scalarColumnNames(preview), EVENT_COLUMN_NAMES)
}

/**
 * A first guess at the properties column: a conventional name, else the only
 * JSON column there is. '' when there is none, or more than one and none named
 * like properties.
 */
export function guessPropertiesColumn(preview: ScanConfigPreview | null): string {
  const json = jsonColumnNames(preview)
  return byName(json, PROPERTIES_COLUMN_NAMES) || (json.length === 1 ? (json[0] ?? '') : '')
}

/**
 * The preview's event + properties summary, when it was computed for the two
 * columns the form holds now. A summary for other columns says nothing about
 * these, so it is withheld rather than shown under the wrong heading.
 */
export function eventPropertiesFor(
  preview: ScanConfigPreview | null,
  eventNameColumn: string,
  propertiesColumn: string,
): ScanPreviewEventProperties | null {
  const summary = preview?.event_properties
  if (!summary) return null
  if (summary.event_name_column !== eventNameColumn) return null
  if (summary.properties_column !== propertiesColumn) return null
  return summary
}
