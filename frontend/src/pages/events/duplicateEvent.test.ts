import { describe, expect, it } from 'vitest'
import type { Event as TEvent, EventType, MetaFieldDefinition } from '@/types'
import { duplicateHref, duplicateSeed, withoutDuplicateFrom } from './duplicateEvent'

const field = (id: string, name: string, field_type = 'string') => ({
  id,
  event_type_id: 'et-main',
  name,
  display_name: name.charAt(0).toUpperCase() + name.slice(1),
  field_type,
  is_required: false,
  enum_options: null,
  description: '',
  order: 0,
  sensitivity: 'none',
})

const MAIN_TYPE = {
  id: 'et-main',
  name: 'checkout',
  display_name: 'Checkout',
  field_definitions: [field('f-screen', 'screen'), field('f-count', 'count', 'number')],
} as unknown as EventType

/** The branch copy of MAIN_TYPE: same names, new ids. */
const BRANCH_TYPE = {
  id: 'et-branch',
  name: 'checkout',
  display_name: 'Checkout',
  field_definitions: [field('fb-screen', 'screen'), field('fb-count', 'count', 'number')],
} as unknown as EventType

const RULE_TYPE = { ...MAIN_TYPE, event_name_format: '{screen}' } as EventType

const META_MAIN = [
  { id: 'm-jira', name: 'jira', display_name: 'Jira', allow_multiple: true },
] as unknown as MetaFieldDefinition[]
const META_BRANCH = [
  { id: 'mb-jira', name: 'jira', display_name: 'Jira', allow_multiple: true },
] as unknown as MetaFieldDefinition[]

const SOURCE = {
  id: 'ev-1',
  event_type_id: 'et-main',
  event_type: { id: 'et-main', name: 'checkout', display_name: 'Checkout', color: '' },
  name: 'checkout_started',
  source_name: null,
  title: 'Checkout started',
  description: 'Fires when checkout opens',
  status: 'live',
  sunset_at: '2027-01-01T00:00:00Z',
  superseded_by_event_id: 'ev-9',
  owner_id: 'u-1',
  metric_breakdown_columns: ['platform'],
  required_presence_threshold: 0.9,
  tags: [{ id: 't-1', name: 'checkout' }],
  field_values: [
    { id: 'fv-1', field_definition_id: 'f-screen', value: '${screen}', is_authored: false },
    { id: 'fv-2', field_definition_id: 'f-count', value: '3' },
  ],
  meta_values: [
    { id: 'mv-1', meta_field_definition_id: 'm-jira', value: 'PROJ-1' },
    { id: 'mv-2', meta_field_definition_id: 'm-jira', value: 'PROJ-2' },
  ],
} as unknown as TEvent

describe('duplicateSeed', () => {
  it('copies the plan content and leaves the lifecycle behind', () => {
    const result = duplicateSeed(SOURCE, { eventTypes: [MAIN_TYPE], metaFields: META_MAIN })
    expect(result).not.toBeNull()
    const { seed, source, dropped } = result!
    expect(seed).toEqual({
      eventTypeId: 'et-main',
      name: 'checkout_started',
      title: 'Checkout started',
      description: 'Fires when checkout opens',
      ownerId: 'u-1',
      metricBreakdownColumns: ['platform'],
      tags: ['checkout'],
      // The token is copied byte for byte.
      fieldValues: { 'f-screen': '${screen}', 'f-count': '3' },
      // Every value of a multi-value meta field.
      metaValues: { 'm-jira': ['PROJ-1', 'PROJ-2'] },
      requiredPresenceThreshold: 0.9,
    })
    expect(seed).not.toHaveProperty('status')
    expect(seed).not.toHaveProperty('sunsetAt')
    expect(seed).not.toHaveProperty('supersededBy')
    expect(source).toEqual({
      id: 'ev-1',
      name: 'checkout_started',
      identity: 'checkout_started',
      eventTypeId: 'et-main',
    })
    expect(dropped).toEqual([])
  })

  it('leaves the name to the rule on a type a scan names', () => {
    const result = duplicateSeed(
      { ...SOURCE, source_name: 'checkout_started_identity' } as TEvent,
      { eventTypes: [RULE_TYPE], metaFields: META_MAIN },
    )
    expect(result!.seed.name).toBe('')
    expect(result!.source.identity).toBe('checkout_started_identity')
  })

  it('follows a branch copy of the type, its fields and meta fields by name', () => {
    const result = duplicateSeed(SOURCE, {
      eventTypes: [BRANCH_TYPE],
      metaFields: META_BRANCH,
      sourceEventTypes: [MAIN_TYPE],
      sourceMetaFields: META_MAIN,
    })
    expect(result!.seed.eventTypeId).toBe('et-branch')
    expect(result!.source.eventTypeId).toBe('et-branch')
    expect(result!.seed.fieldValues).toEqual({ 'fb-screen': '${screen}', 'fb-count': '3' })
    expect(result!.seed.metaValues).toEqual({ 'mb-jira': ['PROJ-1', 'PROJ-2'] })
    expect(result!.dropped).toEqual([])
  })

  it('names what it cannot carry instead of losing it silently', () => {
    const narrowType = {
      ...BRANCH_TYPE,
      field_definitions: [field('fb-count', 'count', 'number')],
    } as unknown as EventType
    const result = duplicateSeed(
      {
        ...SOURCE,
        // A scan stored text in a number field: the new form would refuse it.
        field_values: [
          { id: 'fv-1', field_definition_id: 'f-screen', value: 'cart' },
          { id: 'fv-2', field_definition_id: 'f-count', value: 'N/A' },
        ],
      } as TEvent,
      {
        eventTypes: [narrowType],
        metaFields: [],
        sourceEventTypes: [MAIN_TYPE],
        sourceMetaFields: META_MAIN,
      },
    )
    expect(result!.seed.fieldValues).toEqual({})
    expect(result!.seed.metaValues).toEqual({})
    expect(result!.dropped).toEqual(['Screen', 'Count', 'Jira'])
  })

  it('has no seed when the plan has no such type', () => {
    const other = { ...BRANCH_TYPE, id: 'et-x', name: 'search' } as EventType
    expect(duplicateSeed(SOURCE, { eventTypes: [other], metaFields: [] })).toBeNull()
  })
})

describe('duplicateHref', () => {
  it('names the source and keeps the branch and the other parameters', () => {
    expect(duplicateHref('/p/demo/events/all/new', '?branch=b-1&tag=checkout', 'ev-1')).toBe(
      '/p/demo/events/all/new?branch=b-1&tag=checkout&from=ev-1',
    )
  })

  it('replaces an earlier source', () => {
    expect(duplicateHref('/p/demo/events/all/new', '?from=ev-0', 'ev-1')).toBe(
      '/p/demo/events/all/new?from=ev-1',
    )
  })

  it('drops only `from` for the list and a blank form', () => {
    expect(withoutDuplicateFrom('?branch=b-1&from=ev-1')).toBe('?branch=b-1')
    expect(withoutDuplicateFrom('?from=ev-1')).toBe('')
  })
})
