// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ScanConfigPreview, ScanPreviewEventProperties } from '@/types'
import { ScanCreatePage } from './ScanConfigForm'
import { EventPropertiesPreview } from './ScanSetupPresetFields'
import {
  eventPropertiesFor,
  guessEventColumn,
  guessPropertiesColumn,
  jsonColumnNames,
  scalarColumnNames,
} from './scanSetupPreset'
import {
  PRESET_INCOMPLETE_TITLE,
  type ScanFormState,
  hasEventTarget,
  scanFormBlocker,
  toBackendPayload,
  toDryRunRequest,
} from './useScanForm'

vi.mock('@uiw/react-codemirror', () => ({
  default: ({
    value,
    onChange,
    placeholder,
  }: {
    value: string
    onChange: (v: string) => void
    placeholder?: string
  }) => (
    <textarea value={value} onChange={e => onChange(e.target.value)} placeholder={placeholder} />
  ),
}))

vi.mock('@/hooks/useBranch', () => ({
  useActiveBranchId: () => null,
}))

const preview: ScanConfigPreview = {
  columns: [
    { name: 'event', type_name: 'String', is_nullable: false },
    { name: 'properties', type_name: 'JSON', is_nullable: true },
    { name: 'context', type_name: 'JSON', is_nullable: true },
    { name: 'user_id', type_name: 'String', is_nullable: false },
    { name: 'ts', type_name: 'DateTime', is_nullable: false },
  ],
  rows: [],
  json_columns: [
    { column: 'properties', paths: [] },
    { column: 'context', paths: [] },
  ],
}

const summary: ScanPreviewEventProperties = {
  event_name_column: 'event',
  properties_column: 'properties',
  sample_rows: 3,
  events: [
    {
      name: 'page_view',
      sample_rows: 2,
      properties: [
        { path: 'url', presence: 1, type: 'string', sample_values: ['/a', '/b'] },
        { path: 'ms', presence: 0.5, type: 'number', sample_values: ['12'] },
      ],
    },
    { name: 'signup', sample_rows: 1, properties: [] },
  ],
  error: null,
}

function formState(overrides: Partial<ScanFormState> = {}): ScanFormState {
  return {
    mode: 'catalog',
    setupPreset: 'event_properties',
    eventNameColumn: 'event',
    propertiesColumn: 'properties',
    jsonStringColumns: [],
    dataSourceId: 'ds-1',
    name: 'Events',
    baseQuery: 'SELECT * FROM analytics.events',
    eventTypeId: '',
    eventTypeColumn: 'category',
    timeColumn: 'ts',
    appVersionColumn: '',
    appVersionPrereleasePattern: '',
    appVersionActiveShareMin: '',
    platformColumn: '',
    eventNameFormat: '{action}',
    jsonValuePaths: ['properties.plan'],
    eventGroupRules: [],
    metricBreakdownColumns: [],
    metricBreakdownValuesLimit: '',
    distributionDriftFields: [],
    cardinalityThreshold: '100',
    interval: '',
    chunkInterval: '',
    scanLookbackHours: '24',
    scanRowLimit: '',
    metricsRowLimit: '',
    ...overrides,
  }
}

describe('scanSetupPreset helpers', () => {
  it('splits the columns into JSON ones and the rest', () => {
    expect(jsonColumnNames(preview)).toEqual(['properties', 'context'])
    expect(scalarColumnNames(preview)).toEqual(['event', 'user_id', 'ts'])
    expect(jsonColumnNames(null)).toEqual([])
  })

  it('guesses the conventional column names', () => {
    expect(guessEventColumn(preview)).toBe('event')
    expect(guessPropertiesColumn(preview)).toBe('properties')
  })

  it('takes the only JSON column when none is named like properties', () => {
    const single: ScanConfigPreview = {
      ...preview,
      columns: [
        { name: 'kind', type_name: 'String', is_nullable: false },
        { name: 'blob', type_name: 'Map(String, String)', is_nullable: false },
      ],
      json_columns: [{ column: 'blob', paths: [] }],
    }
    expect(guessEventColumn(single)).toBe('')
    expect(guessPropertiesColumn(single)).toBe('blob')
  })

  it('shows a summary only for the columns it was computed for', () => {
    const withSummary = { ...preview, event_properties: summary }
    expect(eventPropertiesFor(withSummary, 'event', 'properties')).toBe(summary)
    expect(eventPropertiesFor(withSummary, 'event', 'context')).toBeNull()
    expect(eventPropertiesFor(preview, 'event', 'properties')).toBeNull()
  })
})

describe('the preset payload', () => {
  it('sends the two columns and leaves the derived fields to the backend', () => {
    const payload = toBackendPayload(formState())
    expect(payload).toMatchObject({
      setup_preset: 'event_properties',
      event_name_column: 'event',
      properties_column: 'properties',
      event_type_column: null,
      event_name_format: null,
      json_value_paths: [],
      event_group_rules: [],
      event_type_id: null,
    })
    expect(toDryRunRequest(formState())).toMatchObject({
      setup_preset: 'event_properties',
      event_name_column: 'event',
      properties_column: 'properties',
    })
  })

  it('keeps the custom answers when the form is switched back', () => {
    const payload = toBackendPayload(formState({ setupPreset: 'custom' }))
    expect(payload).toMatchObject({
      setup_preset: 'custom',
      event_name_column: null,
      properties_column: null,
      event_type_column: 'category',
      event_name_format: '{action}',
      json_value_paths: ['properties.plan'],
    })
  })

  it('needs both columns before it can be saved', () => {
    expect(hasEventTarget(formState())).toBe(true)
    const missing = formState({ propertiesColumn: '', eventTypeId: 'et-1' })
    expect(hasEventTarget(missing)).toBe(false)
    expect(scanFormBlocker(missing)).toBe(PRESET_INCOMPLETE_TITLE)
    expect(scanFormBlocker(formState())).toBeNull()
  })
})

describe('EventPropertiesPreview', () => {
  it('lists each event with its keys, type and presence', () => {
    render(<EventPropertiesPreview summary={summary} />)
    const panel = screen.getByTestId('event-properties-preview')
    expect(within(panel).getByText('page_view')).toBeInTheDocument()
    expect(within(panel).getByText('signup')).toBeInTheDocument()
    expect(within(panel).getByText('in 50%')).toBeInTheDocument()
    expect(within(panel).getByText('e.g. /a, /b')).toBeInTheDocument()
  })

  it('says why nothing was summarised', () => {
    render(<EventPropertiesPreview summary={{ ...summary, events: [], error: 'Not JSON.' }} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Not JSON.')
  })

  it('asks for a reload when the summary is for other columns', () => {
    render(<EventPropertiesPreview summary={null} />)
    expect(screen.getByTestId('event-properties-preview')).toHaveTextContent(/Reload the preview/)
  })
})

function mockJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const dataSource = {
  id: 'ds-1',
  name: 'Web Production',
  db_type: 'clickhouse',
  host: 'h',
  port: 8123,
  database_name: 'analytics',
  username: 'u',
  password_set: true,
  connection_settings: {},
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

describe('the New scan page with the Event + properties setup', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('asks for two columns, guesses them from the preview and hides the custom naming', async () => {
    const previewBodies: Record<string, unknown>[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.includes('/data-sources/') && url.includes('/schema')) {
        return mockJsonResponse({ tables: [] })
      }
      if (url.endsWith('/api/v1/data-sources')) return mockJsonResponse([dataSource])
      if (url.includes('/scans/preview')) {
        const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>
        previewBodies.push(body)
        return mockJsonResponse({
          id: 'preview-job',
          status: 'completed',
          started_at: null,
          completed_at: null,
          result_summary: body.properties_column ? { ...preview, event_properties: summary } : preview,
          error_message: null,
        })
      }
      if (url.includes('/scans/dry-run')) {
        return mockJsonResponse({
          id: 'dry-run-job',
          status: 'completed',
          started_at: null,
          completed_at: null,
          result_summary: null,
          error_message: null,
        })
      }
      if (url.includes('/event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <ScanCreatePage slug="demo" onBack={() => {}} onCreated={() => {}} />
      </QueryClientProvider>,
    )

    fireEvent.click(await screen.findByLabelText('Event + properties'))
    // The custom naming questions are gone, and so is their section.
    expect(screen.queryByLabelText('Event type column')).toBeNull()
    expect(screen.queryByRole('button', { name: /Event names and grouping/ })).toBeNull()
    expect(screen.getByLabelText('Event type')).toHaveDisplayValue('Events (created if missing)')

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Events' } })
    fireEvent.change(screen.getByLabelText('Data source'), { target: { value: 'ds-1' } })
    fireEvent.change(await screen.findByPlaceholderText(/SELECT \* FROM analytics\.events/), {
      target: { value: 'SELECT * FROM analytics.events' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Load preview/ }))

    const eventColumn = await screen.findByLabelText('Event column')
    await waitFor(() => expect(eventColumn).toHaveDisplayValue('event'))
    const propertiesColumn = screen.getByLabelText('Properties column')
    expect(propertiesColumn).toHaveDisplayValue('properties')
    // Only JSON columns can hold the properties.
    const offered = within(propertiesColumn)
      .getAllByRole('option')
      .map(option => option.textContent)
    expect(offered).toEqual(['Choose a JSON column', 'properties', 'context'])

    // The first preview could not know the columns; reloading asks with them
    // and shows what they yield.
    expect(screen.getByTestId('event-properties-preview')).toHaveTextContent(/Reload the preview/)
    fireEvent.click(screen.getByRole('button', { name: /Reload preview/ }))
    await waitFor(() =>
      expect(screen.getByTestId('event-properties-preview')).toHaveTextContent('page_view'),
    )
    expect(previewBodies.at(-1)).toMatchObject({
      event_name_column: 'event',
      properties_column: 'properties',
    })
  })
})
