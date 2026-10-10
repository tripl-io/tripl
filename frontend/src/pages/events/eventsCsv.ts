import type {
  EventListItem,
  EventTypeBrief,
  FieldDefinition,
  MetaFieldDefinition,
} from '@/types'
import { EVENT_ATTRIBUTE_LABEL } from '@/lib/eventAttributes'
import { EVENT_STATUS_LABELS, type EventStatus } from '@/lib/eventStatus'

import { resolveFieldValue, resolveMetaValue } from './useEventsFiltering'

export type EventsCsvColumn = {
  header: string
  value: (ev: EventListItem) => string
}

// A cell that opens with one of these is executed as a formula by Excel,
// Sheets and LibreOffice. Event names come from ingested scan data, so they are
// untrusted input; prefixing neutralises the cell without dropping characters.
const FORMULA_LEAD = /^[=+\-@\t\r]/

function escapeCsvCell(raw: string): string {
  const guarded = FORMULA_LEAD.test(raw) ? `'${raw}` : raw
  if (!/[",\r\n]/.test(guarded)) return guarded
  return `"${guarded.replace(/"/g, '""')}"`
}

/**
 * RFC 4180 CSV, CRLF-delimited and BOM-prefixed so Excel opens it as UTF-8
 * instead of mangling non-ASCII event names.
 */
export function toCsv(columns: EventsCsvColumn[], rows: EventListItem[]): string {
  const bom = '\ufeff'
  const lines = [columns.map(col => escapeCsvCell(col.header)).join(',')]
  for (const row of rows) {
    lines.push(columns.map(col => escapeCsvCell(col.value(row))).join(','))
  }
  return `${bom}${lines.join('\r\n')}\r\n`
}

export type EventsCsvColumnOptions = {
  activeTypeName: string | null
  eventTypesById: Map<string, EventTypeBrief>
  usersById: Map<string, { name: string | null; email: string }>
  fieldDefsById: Map<string, FieldDefinition>
  fieldColumns: FieldDefinition[]
  metaFields: MetaFieldDefinition[]
  hideStatus: boolean
  hideReviewed: boolean
  hideTags: boolean
  hideLastSeen: boolean
  hideOwner: boolean
}

/**
 * The exported columns, mirroring the table's current column picker.
 *
 * Signal, Δ · 24h and 48h are deliberately absent: those cells are filled from
 * a metrics request issued only for the rows on screen (useEventRowMetrics), so
 * they simply do not exist for the rest of the filtered set. Exporting them as
 * blanks would read as "no volume" rather than "not fetched".
 */
export function buildEventsCsvColumns(options: EventsCsvColumnOptions): EventsCsvColumn[] {
  const columns: EventsCsvColumn[] = [
    { header: 'Event', value: ev => ev.name },
    // The free-text label beside the identity. It has no column of its own in
    // the table (it renders inside the Event cell), so it is exported
    // unconditionally right after the identity it belongs to.
    { header: 'Title', value: ev => ev.title },
  ]

  if (!options.activeTypeName) {
    columns.push({
      header: 'Type',
      value: (ev) => {
        const type = options.eventTypesById.get(ev.event_type_id)
        return type?.display_name ?? type?.name ?? ''
      },
    })
  }
  if (!options.hideStatus) {
    columns.push({
      header: 'Status',
      value: ev => EVENT_STATUS_LABELS[(ev.status as EventStatus) ?? 'draft'] ?? ev.status,
    })
  }
  if (!options.hideReviewed) {
    columns.push({ header: EVENT_ATTRIBUTE_LABEL.reviewed, value: ev => (ev.reviewed ? 'yes' : 'no') })
  }
  if (!options.hideTags) {
    columns.push({ header: 'Tags', value: ev => ev.tags.map(tag => tag.name).join(' ') })
  }
  if (!options.hideLastSeen) {
    columns.push({ header: 'Last seen', value: ev => ev.last_seen_at ?? '' })
  }
  if (!options.hideOwner) {
    columns.push({
      header: 'Owner',
      value: (ev) => {
        const owner = ev.owner_id ? options.usersById.get(ev.owner_id) : undefined
        return owner?.name ?? owner?.email ?? ''
      },
    })
  }
  for (const field of options.fieldColumns) {
    columns.push({
      header: field.display_name,
      value: ev => resolveFieldValue(ev, field, options.fieldDefsById),
    })
  }
  for (const meta of options.metaFields) {
    columns.push({ header: meta.display_name, value: ev => resolveMetaValue(ev, meta) })
  }
  return columns
}

/** `tripl-events-<slug>-<tab>-2026-08-17.csv` */
export function eventsCsvFilename(slug: string, tab: string, now = new Date()): string {
  return `tripl-events-${slug}-${tab}-${now.toISOString().slice(0, 10)}.csv`
}

export function downloadCsv(filename: string, csv: string): void {
  const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  // Deferred: some browsers (Safari, older Firefox) start the download after
  // `click()` returns, and a URL revoked in the same tick fails a large export
  // with a network error.
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
