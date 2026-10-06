import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { Event as TEvent, EventType, FieldDefinition } from '@/types'
import { EventFieldsTable } from './EventFieldsTable'

function field(id: string, name: string, order: number): FieldDefinition {
  return {
    id,
    name,
    display_name: name,
    field_type: 'string',
    is_required: false,
    order,
    sensitivity: 'none',
  } as unknown as FieldDefinition
}

const PAGE = field('fd-page', 'page', 0)
const ACTION = field('fd-action', 'action', 1)

const EVENT = {
  id: 'ev-1',
  field_values: [
    {
      id: 'fv-page',
      field_definition_id: PAGE.id,
      value: 'map/main',
      observed_values: {
        distinct_count: 2,
        total_count: 100,
        values: [
          { value: 'map/main', count: 62, share: 0.62 },
          { value: 'spot/main', count: 38, share: 0.38 },
        ],
        other_count: 0,
        observed_at: '2026-10-05T09:00:00Z',
        scan_config_id: null,
      },
    },
    { id: 'fv-action', field_definition_id: ACTION.id, value: 'tap', observed_values: null },
  ],
} as unknown as TEvent

describe('EventFieldsTable', () => {
  it('puts the observed values under the stored one, and only where they vary', () => {
    const eventType = { field_definitions: [PAGE, ACTION] } as unknown as EventType
    render(
      <EventFieldsTable
        eventType={eventType}
        event={EVENT}
        fieldDefMap={new Map([[PAGE.id, PAGE], [ACTION.id, ACTION]])}
      />,
    )
    const rows = screen.getAllByRole('row')
    const pageRow = rows.find(r => within(r).queryByText('page', { exact: true }))!
    expect(within(pageRow).getByTestId('field-observed-values')).toHaveTextContent(
      'Seen with 2 values: map/main 62% · spot/main 38%',
    )
    const actionRow = rows.find(r => within(r).queryByText('action', { exact: true }))!
    expect(within(actionRow).queryByTestId('field-observed-values')).toBeNull()
  })
})
