// @vitest-environment jsdom
import { act, fireEvent, render, renderHook, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ScanConfigPreview } from '@/types'
import { ScanCreatePage } from './ScanConfigForm'
import { JsonStringColumnsPicker } from './JsonStringColumnsPicker'
import { PresetColumnFields } from './ScanSetupPresetFields'
import {
  guessPropertiesColumn,
  isTextColumnType,
  jsonColumnNames,
  scalarColumnNames,
  textColumnNames,
} from './scanSetupPreset'
import { type ScanFormState, toBackendPayload, toDryRunRequest, useScanForm } from './useScanForm'

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

// `payload` holds JSON text: a String column the user can ask to parse.
const preview: ScanConfigPreview = {
  columns: [
    { name: 'event', type_name: 'String', is_nullable: false },
    { name: 'payload', type_name: 'Nullable(String)', is_nullable: true },
    { name: 'context', type_name: 'JSON', is_nullable: true },
    { name: 'n', type_name: 'Int64', is_nullable: false },
    { name: 'ts', type_name: 'DateTime', is_nullable: false },
  ],
  rows: [],
  json_columns: [{ column: 'context', paths: [] }],
}

// The same source previewed with `payload` parsed: the backend reports it as JSON.
const parsedPreview: ScanConfigPreview = {
  ...preview,
  columns: preview.columns.map(column =>
    column.name === 'payload' ? { ...column, type_name: 'JSON' } : column,
  ),
  json_columns: [
    { column: 'payload', paths: [] },
    { column: 'context', paths: [] },
  ],
}

describe('text column helpers', () => {
  it('recognises the text types every engine that can parse them reports', () => {
    for (const type of ['String', 'Nullable(String)', 'LowCardinality(Nullable(String))', 'STRING', 'FixedString(8)']) {
      expect(isTextColumnType(type)).toBe(true)
    }
    for (const type of ['JSON', 'Int64', 'Array(String)', 'Map(String, String)', 'text']) {
      expect(isTextColumnType(type)).toBe(false)
    }
  })

  it('offers the text columns, and keeps a ticked one the preview now calls JSON', () => {
    expect(textColumnNames(preview)).toEqual(['event', 'payload'])
    expect(textColumnNames(parsedPreview, ['payload'])).toEqual(['payload', 'event'])
    expect(textColumnNames(null, ['payload'])).toEqual(['payload'])
  })

  it('counts a ticked column as JSON before the preview is reloaded', () => {
    expect(jsonColumnNames(preview, ['payload'])).toEqual(['context', 'payload'])
    expect(scalarColumnNames(preview, ['payload'])).toEqual(['event', 'n', 'ts'])
    // A column the query does not return is never offered.
    expect(jsonColumnNames(preview, ['gone'])).toEqual(['context'])
    expect(guessPropertiesColumn(preview, ['payload'])).toBe('payload')
  })
})

function formState(overrides: Partial<ScanFormState> = {}): ScanFormState {
  return {
    mode: 'catalog',
    setupPreset: 'custom',
    eventNameColumn: '',
    propertiesColumn: '',
    jsonStringColumns: ['payload'],
    dataSourceId: 'ds-1',
    name: 'Text events',
    baseQuery: 'SELECT * FROM events',
    eventTypeId: 'et-1',
    eventTypeColumn: '',
    timeColumn: 'ts',
    appVersionColumn: '',
    appVersionPrereleasePattern: '',
    appVersionActiveShareMin: '',
    platformColumn: '',
    eventNameFormat: '',
    jsonValuePaths: [],
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

describe('the payload', () => {
  it('sends the parsed columns in both setups, and the dry run asks with them', () => {
    expect(toBackendPayload(formState()).json_string_columns).toEqual(['payload'])
    const preset = formState({
      setupPreset: 'event_properties',
      eventNameColumn: 'event',
      propertiesColumn: 'payload',
    })
    expect(toBackendPayload(preset)).toMatchObject({
      json_string_columns: ['payload'],
      properties_column: 'payload',
    })
    expect(toDryRunRequest(preset).json_string_columns).toEqual(['payload'])
  })
})

describe('JsonStringColumnsPicker', () => {
  it('ticks and unticks text columns', () => {
    const onToggle = vi.fn()
    render(
      <JsonStringColumnsPicker preview={preview} selected={[]} dbType="clickhouse" stale={false} onToggle={onToggle} />,
    )
    const group = screen.getByRole('group', { name: 'Text columns to parse as JSON' })
    expect(within(group).getAllByRole('checkbox').map(box => box.getAttribute('aria-label'))).toEqual([
      'Parse event as JSON',
      'Parse payload as JSON',
    ])
    fireEvent.click(screen.getByRole('checkbox', { name: 'Parse payload as JSON' }))
    expect(onToggle).toHaveBeenCalledWith('payload')
  })

  it('asks for a reload when the preview was read with other columns parsed', () => {
    render(
      <JsonStringColumnsPicker preview={preview} selected={['payload']} dbType="bigquery" stale onToggle={() => {}} />,
    )
    expect(screen.getByRole('checkbox', { name: 'Parse payload as JSON' })).toBeChecked()
    expect(screen.getByTestId('json-string-columns-stale')).toHaveTextContent(/Reload the preview/)
  })

  it('is left out for an engine that cannot parse text, unless a column is ticked', () => {
    const { rerender } = render(
      <JsonStringColumnsPicker preview={preview} selected={[]} dbType="postgres" stale={false} onToggle={() => {}} />,
    )
    expect(screen.queryByTestId('json-string-columns')).toBeNull()
    rerender(
      <JsonStringColumnsPicker preview={preview} selected={['payload']} dbType="postgres" stale={false} onToggle={() => {}} />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent(/Only ClickHouse, BigQuery and Databricks/)
  })
})

describe('the preset properties picker', () => {
  it('offers a parsed text column and no longer offers it as the event column', () => {
    render(
      <PresetColumnFields
        preview={preview}
        eventTypes={[]}
        jsonStringColumns={['payload']}
        eventNameColumn="event"
        propertiesColumn="payload"
        eventTypeId=""
        onEventNameColumnChange={() => {}}
        onPropertiesColumnChange={() => {}}
        onEventTypeIdChange={() => {}}
      />,
    )
    const properties = screen.getByLabelText('Properties column')
    expect(properties).toHaveDisplayValue('payload')
    expect(within(properties).getAllByRole('option').map(option => option.textContent)).toEqual([
      'Choose a JSON column',
      'context',
      'payload',
    ])
    const events = within(screen.getByLabelText('Event column'))
      .getAllByRole('option')
      .map(option => option.textContent)
    expect(events).not.toContain('payload')
  })

  it('tells how to read a text column when the query has no JSON column', () => {
    render(
      <PresetColumnFields
        preview={{ ...preview, json_columns: [] }}
        eventTypes={[]}
        eventNameColumn="event"
        propertiesColumn=""
        eventTypeId=""
        onEventNameColumnChange={() => {}}
        onPropertiesColumnChange={() => {}}
        onEventTypeIdChange={() => {}}
      />,
    )
    expect(screen.getByText(/Tick its text column under Parse as JSON/)).toBeInTheDocument()
  })
})

describe('useScanForm toggling a parsed column', () => {
  it('drops it from the settings that read it as text, and unticking clears the properties column', () => {
    const queryClient = new QueryClient()
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    )
    const { result } = renderHook(() => useScanForm('demo', null), { wrapper })
    act(() => {
      result.current.set('eventNameColumn', 'payload')
      result.current.set('metricBreakdownColumns', ['payload', 'n'])
      result.current.set('distributionDriftFields', ['payload'])
    })
    act(() => result.current.toggleJsonStringColumn('payload'))
    expect(result.current.state).toMatchObject({
      jsonStringColumns: ['payload'],
      eventNameColumn: '',
      metricBreakdownColumns: ['n'],
      distributionDriftFields: [],
    })
    act(() => result.current.set('propertiesColumn', 'payload'))
    act(() => result.current.toggleJsonStringColumn('payload'))
    expect(result.current.state.jsonStringColumns).toEqual([])
    expect(result.current.state.propertiesColumn).toBe('')
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

describe('the New scan page with a text properties column', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('lets the preset pick a text column once it is parsed, and previews it parsed', async () => {
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
        const parsed = (body.json_string_columns as string[] | undefined)?.includes('payload')
        return mockJsonResponse({
          id: 'preview-job',
          status: 'completed',
          started_at: null,
          completed_at: null,
          result_summary: parsed ? parsedPreview : preview,
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
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Events' } })
    fireEvent.change(screen.getByLabelText('Data source'), { target: { value: 'ds-1' } })
    fireEvent.change(await screen.findByPlaceholderText(/SELECT \* FROM analytics\.events/), {
      target: { value: 'SELECT * FROM analytics.events' },
    })
    fireEvent.click(screen.getByRole('button', { name: /Load preview/ }))

    const payloadBox = await screen.findByRole('checkbox', { name: 'Parse payload as JSON' })
    const offered = () =>
      within(screen.getByLabelText('Properties column'))
        .getAllByRole('option')
        .map(option => option.textContent)
    expect(offered()).not.toContain('payload')

    fireEvent.click(payloadBox)
    expect(offered()).toContain('payload')
    expect(screen.getByTestId('json-string-columns-stale')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Reload preview/ }))
    await waitFor(() => expect(screen.queryByTestId('json-string-columns-stale')).toBeNull())
    expect(previewBodies.at(-1)).toMatchObject({ json_string_columns: ['payload'] })
    // Still tickable (and ticked) though the preview now reports it as JSON.
    expect(screen.getByRole('checkbox', { name: 'Parse payload as JSON' })).toBeChecked()
  })
})
