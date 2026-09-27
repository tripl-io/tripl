import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { toast } from 'sonner'
import type { Event, EventType, MetaFieldDefinition } from '@/types'
import { buildExamplePayload, buildSpecMarkdown } from '@/lib/eventSpec'
import { EventSpecCard } from './EventSpecCard'
import { at } from '@/test/at'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const EVENT_TYPE = {
  id: 'et-se',
  project_id: 'p',
  name: 'se',
  display_name: 'Structured Event',
  description: '',
  color: '#333',
  order: 0,
  created_at: '',
  updated_at: '',
  event_name_format: '{category}:{action}:{label}',
  field_definitions: [
    { id: 'f-category', event_type_id: 'et-se', name: 'category', display_name: 'Category', description: '', field_type: 'string', is_required: false, order: 0, sensitivity: 'none' },
    { id: 'f-action', event_type_id: 'et-se', name: 'action', display_name: 'Action', description: '', field_type: 'string', is_required: false, order: 1, sensitivity: 'none' },
    { id: 'f-label', event_type_id: 'et-se', name: 'label', display_name: 'Label', description: '', field_type: 'string', is_required: false, order: 2, sensitivity: 'none' },
    { id: 'f-property', event_type_id: 'et-se', name: 'property', display_name: 'Property', description: 'Extra context', field_type: 'json', is_required: false, order: 3, sensitivity: 'none' },
  ],
} as unknown as EventType

const EVENT = {
  id: 'ev-1',
  project_id: 'p',
  event_type_id: 'et-se',
  event_type: { id: 'et-se', name: 'se', display_name: 'Structured Event', color: '#333' },
  name: 'spot:open:models_guide',
  source_name: 'spot:open:models_guide',
  title: 'Tap on a model card',
  description: 'Sent once per screen.',
  order: 0,
  status: 'ready_for_dev',
  sunset_at: null,
  last_seen_at: null,
  owner_id: null,
  reviewed: false,
  metric_breakdown_columns: [],
  drift_count: 0,
  tags: [{ id: 't1', name: 'onboarding' }],
  field_values: [
    { id: 'v1', field_definition_id: 'f-category', value: 'spot' },
    { id: 'v2', field_definition_id: 'f-action', value: 'open' },
    { id: 'v3', field_definition_id: 'f-label', value: 'models_guide' },
    {
      id: 'v4',
      field_definition_id: 'f-property',
      value: '{"how": "${property.how}"}',
      variable_values: [
        {
          id: 'c1',
          variable_id: 'var-how',
          variable_name: 'property.how',
          source_column: 'property',
          value_kind: 'low',
          observed_count: 3,
          values: ['tap', 'swipe'],
        },
      ],
    },
  ],
  meta_values: [{ id: 'm1', meta_field_definition_id: 'mf-jira', value: 'PROJ-4563' }],
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
} as unknown as Event

const META_FIELDS = new Map<string, MetaFieldDefinition>([
  [
    'mf-jira',
    {
      id: 'mf-jira',
      name: 'jira',
      display_name: 'Jira',
      field_type: 'string',
      link_template: 'https://tracker.example.com/browse/${value}',
    } as unknown as MetaFieldDefinition,
  ],
])

function renderCard() {
  return render(
    <MemoryRouter>
      <EventSpecCard slug="demo" event={EVENT} eventType={EVENT_TYPE} metaFieldMap={META_FIELDS} />
    </MemoryRouter>,
  )
}

describe('EventSpecCard (tripl-kjhi.8)', () => {
  it('leads with the identity, the title and the rule, and marks the naming columns', () => {
    renderCard()
    expect(screen.getByTestId('spec-identity').textContent).toBe('spot:open:models_guide')
    expect(screen.getByText('Tap on a model card')).toBeInTheDocument()
    expect(screen.getByText('{category}:{action}:{label}')).toBeInTheDocument()
    expect(screen.getAllByText('names the event')).toHaveLength(3)
    // The documented values sit beside the template value, linked to the variable.
    expect(screen.getByRole('link', { name: '${property.how}' })).toHaveAttribute('href', '/p/demo/variables/var-how')
    expect(screen.getByText(/= tap, swipe/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'PROJ-4563' })).toHaveAttribute(
      'href',
      'https://tracker.example.com/browse/PROJ-4563',
    )
  })

  it('renders an example payload with documented values filled in and copies it as JSON', async () => {
    const writeText = vi.fn((text: string) => Promise.resolve(text).then(() => undefined))
    Object.assign(navigator, { clipboard: { writeText } })
    renderCard()
    const payload = JSON.parse(screen.getByTestId('spec-payload').textContent ?? '{}')
    expect(payload).toEqual({
      category: 'spot',
      action: 'open',
      label: 'models_guide',
      property: { how: 'tap' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Copy as JSON' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1))
    expect(JSON.parse(at(writeText.mock.calls, 0)[0] as string)).toEqual(payload)
  })

  it('reports a failed copy when there is no clipboard (plain HTTP)', async () => {
    const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', { value: undefined, configurable: true })
    try {
      renderCard()
      fireEvent.click(screen.getByRole('button', { name: 'Copy as JSON' }))
      await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Could not copy json'))
    } finally {
      if (clipboard) Object.defineProperty(navigator, 'clipboard', clipboard)
      else Reflect.deleteProperty(navigator, 'clipboard')
    }
  })

  it('builds a Markdown spec a ticket can carry', () => {
    const rows = [
      {
        field: at(EVENT_TYPE.field_definitions, 0),
        value: 'spot',
        namesTheEvent: true,
        contexts: [],
      },
    ]
    const payload = buildExamplePayload(rows)
    const markdown = buildSpecMarkdown({
      identity: 'spot:open:models_guide',
      title: 'Tap on a model card',
      eventTypeName: 'Structured Event',
      description: 'Sent once.',
      rule: '{category}:{action}:{label}',
      rows,
      payload,
    })
    expect(markdown).toContain('## spot:open:models_guide')
    expect(markdown).toContain('| `category` (names the event) | string | yes | `spot` |  |')
    expect(markdown).toContain('"category": "spot"')
  })

  it('keeps an unresolved template token in the payload rather than inventing a value', () => {
    const payload = buildExamplePayload([
      {
        field: at(EVENT_TYPE.field_definitions, 3),
        value: '{"how": "${property.how}"}',
        namesTheEvent: false,
        contexts: [],
      },
    ])
    expect(payload).toEqual({ property: { how: '${property.how}' } })
  })
})

describe('EventSpecCard spec fields table (DS-5)', () => {
  it('is the design-system table, which scrolls sideways with the edge fade', () => {
    renderCard()

    const table = screen.getByRole('table', { name: 'Spec fields' })
    expect(table).toHaveAttribute('data-slot', 'table')
    expect(table.closest('[data-slot="table-container"]')).not.toBeNull()
    expect(
      screen.getAllByRole('columnheader').map(header => header.textContent),
    ).toEqual(['Field', 'Type', 'Value', 'Documented values'])
  })

  it('folds unset optional fields and lists only the variables a value names (EV-32)', () => {
    const eventType = {
      ...EVENT_TYPE,
      field_definitions: [
        ...EVENT_TYPE.field_definitions,
        { id: 'f-amount', event_type_id: 'et-se', name: 'amount', display_name: 'Amount', description: '', field_type: 'number', is_required: false, order: 4, sensitivity: 'none' },
      ],
    } as unknown as EventType
    const event = {
      ...EVENT,
      field_values: EVENT.field_values.map(fv =>
        fv.field_definition_id === 'f-label'
          ? {
              ...fv,
              // `models_guide` names no variable, so a context riding on it is noise.
              variable_values: [
                { id: 'ctx-user', variable_id: 'var-user', variable_name: 'user_id', source_column: 'user_id', value_kind: 'high', observed_count: 2, values: ['u_001', 'u_002'] },
              ],
            }
          : fv,
      ),
    } as unknown as Event
    render(
      <MemoryRouter>
        <EventSpecCard slug="demo" event={event} eventType={eventType} metaFieldMap={META_FIELDS} />
      </MemoryRouter>,
    )

    expect(screen.queryByRole('link', { name: '${user_id}' })).toBeNull()
    expect(screen.queryByText('amount')).toBeNull()
    const disclosure = screen.getByRole('button', { name: '+1 optional field not set' })
    fireEvent.click(disclosure)
    expect(screen.getByText('amount')).toBeInTheDocument()
    expect(JSON.parse(screen.getByTestId('spec-payload').textContent ?? '{}')).not.toHaveProperty('amount')
  })
})
