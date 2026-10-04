import { describe, expect, it } from 'vitest'
import type { EventListItem, EventTypeBrief } from '@/types'

import { buildEventsCsvColumns, eventsCsvFilename, toCsv } from './eventsCsv'

const EVENT_TYPE: EventTypeBrief = {
  id: 'et-1',
  name: 'pv',
  display_name: 'Page View',
  color: '#3355ff',
}

function makeEvent(overrides: Partial<EventListItem> = {}): EventListItem {
  return {
    id: 'evt-1',
    project_id: 'proj-1',
    event_type_id: 'et-1',
    name: 'checkout_completed',
    source_name: null,
    title: '',
    description: '',
    order: 0,
    status: 'live',
    sunset_at: null,
    last_seen_at: '2026-08-17T09:00:00Z',
    owner_id: null,
    reviewed: false,
    metric_breakdown_columns: [],
    drift_count: 0,
    monitored: false,
    tags: [],
    field_values: [],
    meta_values: [],
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    ...overrides,
  }
}

function columns(overrides: Partial<Parameters<typeof buildEventsCsvColumns>[0]> = {}) {
  return buildEventsCsvColumns({
    activeTypeName: null,
    eventTypesById: new Map([[EVENT_TYPE.id, EVENT_TYPE]]),
    usersById: new Map(),
    fieldDefsById: new Map(),
    fieldColumns: [],
    metaFields: [],
    hideStatus: false,
    hideReviewed: false,
    hideTags: false,
    hideLastSeen: false,
    hideOwner: false,
    ...overrides,
  })
}

describe('buildEventsCsvColumns', () => {
  it('mirrors the column picker — a hidden column is not exported', () => {
    const headers = columns({ hideOwner: true, hideTags: true }).map(col => col.header)

    expect(headers).toEqual(['Event', 'Title', 'Type', 'Status', 'Verified', 'Last seen'])
  })

  // the title has no column of its own in the table (it renders
  // inside the Event cell), so the picker cannot hide it and the export always
  // carries it, right after the identity it labels.
  it('exports the free-text title beside the identity', () => {
    const rows = toCsv(columns({ hideTags: true, hideLastSeen: true, hideOwner: true }), [
      makeEvent({ title: 'Purchase finished' }),
      makeEvent({ id: 'evt-2', name: 'cart_opened' }),
    ]).split('\r\n')

    expect(rows[1]).toBe('checkout_completed,Purchase finished,Page View,Live,no')
    expect(rows[2]).toBe('cart_opened,,Page View,Live,no')
  })

  it('drops the Type column on a type-scoped tab, like the table does', () => {
    expect(columns({ activeTypeName: 'pv' }).map(col => col.header)).not.toContain('Type')
  })

  it('names the event type by display name, not its internal key', () => {
    const csv = toCsv(columns(), [makeEvent()])

    expect(csv).toContain('Page View')
    expect(csv).not.toContain(',pv,')
  })
})

describe('toCsv', () => {
  it('quotes cells containing separators and doubles embedded quotes', () => {
    const csv = toCsv(columns({ hideTags: true, hideLastSeen: true, hideOwner: true }), [
      makeEvent({ name: 'checkout, "final" step' }),
    ])

    expect(csv.split('\r\n')[1]).toBe('"checkout, ""final"" step",,Page View,Live,no')
  })

  // Event names arrive from ingested scan data, so a name starting with "=" is
  // untrusted input that a spreadsheet would otherwise evaluate.
  it('neutralises cells a spreadsheet would run as a formula', () => {
    const csv = toCsv(columns({ hideTags: true, hideLastSeen: true, hideOwner: true }), [
      makeEvent({ name: '=HYPERLINK("http://evil","x")' }),
    ])

    expect(csv.split('\r\n')[1]).toContain(`"'=HYPERLINK(""http://evil"",""x"")"`)
  })

  it('starts with a BOM so Excel reads it as UTF-8', () => {
    expect(toCsv(columns(), [])).toMatch(/^\ufeffEvent,/)
  })
})

describe('eventsCsvFilename', () => {
  it('names the file after the project, tab and day', () => {
    expect(eventsCsvFilename('acme-ios', 'review', new Date('2026-08-17T10:00:00Z'))).toBe(
      'tripl-events-acme-ios-review-2026-08-17.csv',
    )
  })
})
