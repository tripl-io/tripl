import { describe, expect, it } from 'vitest'
import { bulkExtraColumns, bulkUnsupportedReason, parseBulkDraft } from './bulkEventDraft'
import { at } from '@/test/at'
import type { FieldDefinition } from '@/types'

describe('parseBulkDraft', () => {
  it('names each line by the scan rule, in the order the format reads its columns', () => {
    const rows = parseBulkDraft('settings\tunit_change\twind_speed\nspot\topen\tmodels', {
      columns: ['category', 'action', 'label'],
      nameFormat: '{category}:{action}:{label}',
    })

    expect(rows.map(row => row.name)).toEqual([
      'settings:unit_change:wind_speed',
      'spot:open:models',
    ])
    expect(rows.every(row => row.status === 'ready')).toBe(true)
  })

  it('takes a comma when there is no tab, and the whole line when one column is enough', () => {
    const commas = parseBulkDraft('settings, unit_change, wind_speed', {
      columns: ['category', 'action', 'label'],
      nameFormat: '{category}:{action}:{label}',
    })
    expect(at(commas, 0).name).toBe('settings:unit_change:wind_speed')

    // A single-column format must NOT split: `{page}` names events after paths
    // that carry commas, and splitting would tear them apart.
    const single = parseBulkDraft('/buoy/2758a8b1,Tregde+A', {
      columns: ['page'],
      nameFormat: '{page}',
    })
    expect(at(single, 0).name).toBe('/buoy/2758a8b1,Tregde+A')
  })

  it('reports a line that leaves a naming column empty rather than naming it half', () => {
    const rows = parseBulkDraft('settings\tunit_change', {
      columns: ['category', 'action', 'label'],
      nameFormat: '{category}:{action}:{label}',
    })

    expect(at(rows, 0).status).toBe('incomplete')
    expect(at(rows, 0).missing).toEqual(['label'])
  })

  it('counts blank lines so the reported line number is the one on screen', () => {
    const rows = parseBulkDraft('\n\nsign_up\n\nsign_out', {
      columns: ['action'],
      nameFormat: '{action}',
    })

    expect(rows.map(row => row.line)).toEqual([3, 5])
  })

  it('marks a repeat within the paste, and a name the catalog already holds', () => {
    const rows = parseBulkDraft('sign_up\nsign_up\nsign_out', {
      columns: ['action'],
      nameFormat: '{action}',
      taken: new Set(['sign_out']),
    })

    expect(rows.map(row => row.status)).toEqual(['ready', 'duplicate', 'exists'])
  })

  it('takes one name per line where no rule governs the type', () => {
    const rows = parseBulkDraft('checkout:started\ncheckout:completed', {
      columns: [],
      nameFormat: null,
    })

    expect(rows.map(row => row.name)).toEqual(['checkout:started', 'checkout:completed'])
    expect(rows.every(row => row.status === 'ready')).toBe(true)
  })

  it('takes what follows the identity columns as the title, its own commas included', () => {
    const rows = parseBulkDraft(
      'weather_alert,show,widget,Weather alert widget shown\nspot,open,models,Opened, then closed',
      { columns: ['category', 'action', 'label'], nameFormat: '{category}:{action}:{label}' },
    )

    // The label never leaks into the identity, and the values stay the three
    // the name was built from.
    expect(at(rows, 0).name).toBe('weather_alert:show:widget')
    expect(at(rows, 0).values).toEqual(['weather_alert', 'show', 'widget'])
    expect(at(rows, 0).title).toBe('Weather alert widget shown')
    expect(at(rows, 1).title).toBe('Opened, then closed')
  })

  it('separates a title from a single identity column on a tab only', () => {
    const ruled = parseBulkDraft('sign_up\tUser signs up\n/buoy/2758a8b1,Tregde+A', {
      columns: ['page'],
      nameFormat: '{page}',
    })
    // A comma may be part of a one-column identity, so it cannot also start a title.
    expect(ruled.map(row => [row.name, row.title])).toEqual([
      ['sign_up', 'User signs up'],
      ['/buoy/2758a8b1,Tregde+A', ''],
    ])

    const free = parseBulkDraft('checkout:started\tCheckout started', {
      columns: [],
      nameFormat: null,
    })
    expect(free[0]).toMatchObject({ name: 'checkout:started', title: 'Checkout started' })
  })

  it('leaves the title empty where a line gives none', () => {
    const rows = parseBulkDraft('settings\tunit_change\twind_speed\nsettings\tunit_change', {
      columns: ['category', 'action', 'label'],
      nameFormat: '{category}:{action}:{label}',
    })

    expect(rows.map(row => row.title)).toEqual(['', ''])
    expect(at(rows, 1).status).toBe('incomplete')
  })
})

describe('parseBulkDraft extra columns', () => {
  const SCREEN = [{ name: 'screen_name' }]

  it('reads a required field after the identity columns and the title after it', () => {
    const rows = parseBulkDraft('settings,open,home,Home settings opened', {
      columns: ['category', 'action'],
      nameFormat: '{category}:{action}',
      extraColumns: SCREEN,
    })

    expect(at(rows, 0)).toMatchObject({
      name: 'settings:open',
      values: ['settings', 'open'],
      extras: ['home'],
      title: 'Home settings opened',
      status: 'ready',
    })
  })

  it('splits a free name from its extra columns on a tab only', () => {
    const rows = parseBulkDraft('Home Screen View\thome\tHome\n/buoy/1,Tregde', {
      columns: [],
      nameFormat: null,
      extraColumns: SCREEN,
    })

    expect(at(rows, 0)).toMatchObject({ name: 'Home Screen View', extras: ['home'], title: 'Home', status: 'ready' })
    // No tab: the comma stays in the name, and the line names what it lacks.
    expect(at(rows, 1)).toMatchObject({ name: '/buoy/1,Tregde', status: 'incomplete', missing: ['screen_name'] })
  })

  it('names an empty extra column alongside an empty naming column', () => {
    const rows = parseBulkDraft('settings', {
      columns: ['category', 'action'],
      nameFormat: '{category}:{action}',
      extraColumns: SCREEN,
    })

    expect(at(rows, 0).status).toBe('incomplete')
    expect(at(rows, 0).missing).toEqual(['action', 'screen_name'])
  })

  it('refuses an enum value the field does not allow, before the server does', () => {
    const rows = parseBulkDraft('sign_up\tweb\nsign_in\tfax', {
      columns: [],
      nameFormat: null,
      extraColumns: [{ name: 'platform', enumOptions: ['web', 'ios'] }],
    })

    expect(rows.map(row => row.status)).toEqual(['ready', 'invalid'])
    expect(at(rows, 1).problems).toEqual(['platform must be one of web, ios'])
  })
})

describe('bulkExtraColumns', () => {
  const field = (
    name: string,
    extra: Partial<Pick<FieldDefinition, 'field_type' | 'is_required' | 'enum_options' | 'order'>> = {},
  ): Pick<FieldDefinition, 'name' | 'field_type' | 'is_required' | 'enum_options' | 'order'> => ({
    name,
    field_type: 'string',
    is_required: true,
    enum_options: null,
    order: 0,
    ...extra,
  })

  it('takes the required fields the name is not built from, in field order', () => {
    expect(
      bulkExtraColumns(
        [
          field('title_text', { order: 2 }),
          field('action', { order: 0 }),
          field('screen_name', { order: 1 }),
          field('note', { is_required: false }),
          field('payload', { field_type: 'json' }),
        ],
        ['action'],
      ).map(column => column.name),
    ).toEqual(['screen_name', 'title_text'])
  })

  it("carries an enum field's options, and no others'", () => {
    expect(
      bulkExtraColumns(
        [field('platform', { field_type: 'enum', enum_options: ['web', 'ios'] }), field('screen', { order: 1 })],
        [],
      ),
    ).toEqual([
      { name: 'platform', enumOptions: ['web', 'ios'] },
      { name: 'screen', enumOptions: null },
    ])
  })
})

describe('bulkUnsupportedReason', () => {
  it('refuses a format that reads inside a JSON field', () => {
    const reason = bulkUnsupportedReason({
      nameFormat: 'pv:{page_data.variant}',
      namingColumns: ['page_data'],
      requiredJsonFields: [],
    })
    expect(reason).toMatch(/inside a JSON field/)
  })

  it('refuses a type with a required JSON field the paste cannot fill', () => {
    const reason = bulkUnsupportedReason({
      nameFormat: '{action}',
      namingColumns: ['action'],
      requiredJsonFields: ['payload'],
    })
    expect(reason).toMatch(/needs payload, a JSON value/)
  })

  it('allows a type whose other required fields the paste carries as columns', () => {
    // A required string field is an extra column now, not a refusal.
    expect(
      bulkUnsupportedReason({
        nameFormat: '{action}',
        namingColumns: ['action'],
        requiredJsonFields: [],
      }),
    ).toBeNull()
  })
})
