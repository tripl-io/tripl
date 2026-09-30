// @vitest-environment jsdom
import { type ReactNode, createElement } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { scansApi } from '@/api/scans'
import type { ScanConfig, ScanConfigPreview, ScanDryRunResponse } from '@/types'
import {
  type ScanFormState,
  canSubmitScanForm,
  hasEventTarget,
  scanFieldErrors,
  scanFormBlocker,
  toBackendPayload,
  toDryRunRequest,
  useScanForm,
} from './useScanForm'
import { at } from '@/test/at'

function formState(overrides: Partial<ScanFormState> = {}): ScanFormState {
  return {
    mode: 'monitoring',
    setupPreset: 'custom',
    eventNameColumn: '',
    propertiesColumn: '',
    jsonStringColumns: [],
    dataSourceId: 'ds-1',
    name: 'Main scan',
    baseQuery: 'SELECT * FROM analytics.events',
    eventTypeId: '',
    // A complete scan names its events. The fixture answers that question the
    // way the form now makes every scan answer it, so a test that cares about
    // the naming gate has to opt out of it explicitly.
    eventTypeColumn: 'event_name',
    timeColumn: 'received_at',
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
    interval: '1h',
    chunkInterval: '1d',
    scanLookbackHours: '24',
    scanRowLimit: '',
    metricsRowLimit: '',
    ...overrides,
  }
}

describe('toBackendPayload', () => {
  // Catalog only is the absence of a SCHEDULE. Switching to it leaves the
  // previous schedule in state so switching back restores it, but it must not
  // reach the wire: a config saved with an interval and no time column is never
  // dispatched and collects nothing.
  it('drops the schedule in catalog mode, whatever the hidden inputs still hold', () => {
    const payload = toBackendPayload(formState({
      mode: 'catalog',
      interval: '1h',
      chunkInterval: '1d',
    }))

    expect(payload.interval).toBeNull()
    expect(payload.replay_chunk_interval).toBeNull()
  })

  // The defect: the mode radio used to null `time_column` too. Since the payload
  // is a full body and update_scan_config merges by exclude_unset, an explicit
  // null CLEARS the saved column — so editing a scan's name in Catalog only
  // silently removed the lookback bound and the next run read the whole table.
  // Nulling the interval alone already makes a config catalog-only.
  it('keeps the time column in catalog mode — catalog-only is no schedule, not no window', () => {
    const payload = toBackendPayload(formState({
      mode: 'catalog',
      timeColumn: 'event_ts',
      scanLookbackHours: '24',
    }))

    expect(payload.time_column).toBe('event_ts')
    expect(payload.scan_lookback_hours).toBe(24)
    expect(payload.interval).toBeNull()
  })

  it('sends null only when the user actually cleared the time column', () => {
    expect(toBackendPayload(formState({ mode: 'catalog', timeColumn: '' })).time_column).toBeNull()
  })

  it('sends both columns in monitoring mode', () => {
    const payload = toBackendPayload(formState())

    expect(payload.time_column).toBe('received_at')
    expect(payload.interval).toBe('1h')
    expect(payload.replay_chunk_interval).toBe('1d')
  })
})

describe('toDryRunRequest (tripl-3y7z.6)', () => {
  // "What would this scan create?" has to be answered for the config that would
  // actually be SAVED. Reading form state a second time is how the answer and
  // the saved scan drift apart, so the request is derived from the save payload
  // — including the window: a catalog scan with a time column is previewed over
  // the same lookback its runs will use, and one without a time column is
  // previewed over everything, exactly as its runs will read.
  it('asks about the config that would be saved, over the window that config would read', () => {
    const request = toDryRunRequest(formState({
      mode: 'catalog',
      timeColumn: 'received_at',
      interval: '1h',
    }))

    expect(request.time_column).toBe('received_at')
    expect(request.data_source_id).toBe('ds-1')
    expect(request.base_query).toBe('SELECT * FROM analytics.events')
  })

  it('reports an unbounded read as unbounded when there is no time column', () => {
    const request = toDryRunRequest(formState({ mode: 'catalog', timeColumn: '' }))

    expect(request.time_column).toBeNull()
  })

  it('carries the naming inputs the planner reads, so the names it reports are the names a run writes', () => {
    const request = toDryRunRequest(formState({
      eventTypeColumn: 'event_name',
      eventNameFormat: '{action}:{category}',
      cardinalityThreshold: '25',
      scanLookbackHours: '48',
    }))

    expect(request.event_type_column).toBe('event_name')
    expect(request.event_name_format).toBe('{action}:{category}')
    expect(request.cardinality_threshold).toBe(25)
    expect(request.scan_lookback_hours).toBe(48)
    expect(request.time_column).toBe('received_at')
  })
})

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return createElement(QueryClientProvider, { client }, children)
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('loadPreview — the first click on a brand-new scan (tripl-3y7z)', () => {
  const previewPayload: ScanConfigPreview = {
    columns: [{ name: 'event_name', type_name: 'String', is_nullable: false }],
    rows: [{ event_name: 'signup_started' }],
    json_columns: [],
  }

  // The P0: "Load preview" fired the dry run unconditionally, and a brand-new
  // scan opens with no event type and no event type column — the column is
  // picked FROM the rows this very click loads. The worker aborted on its own
  // precondition and the panel reported `Scan failed: Either event_type_id or
  // event_type_column must be specified`, deterministically, on the first click
  // of every scan created on the defaults.
  it('asks the warehouse nothing about events until the draft says how they are named', async () => {
    const preview = vi.spyOn(scansApi, 'preview').mockResolvedValue(previewPayload)
    const dryRun = vi
      .spyOn(scansApi, 'dryRun')
      .mockResolvedValue({ events: [] } as unknown as ScanDryRunResponse)

    const { result } = renderHook(() => useScanForm('demo', null), { wrapper })
    act(() => result.current.setDataSourceId('ds-1'))
    act(() => result.current.setBaseQuery('SELECT * FROM analytics.events'))
    act(() => result.current.loadPreview())

    await waitFor(() => expect(preview).toHaveBeenCalledTimes(1))
    expect(dryRun).not.toHaveBeenCalled()

    // Once the column that names events is chosen from those rows, the same
    // button answers the question it was always meant to answer.
    act(() => result.current.setEventTypeColumn('event_name'))
    act(() => result.current.loadPreview())

    await waitFor(() => expect(dryRun).toHaveBeenCalledTimes(1))
    expect(at(dryRun.mock.calls, 0)[1].event_type_column).toBe('event_name')
  })

  it('answers straight away when an explicit event type already names every row', async () => {
    const preview = vi.spyOn(scansApi, 'preview').mockResolvedValue(previewPayload)
    const dryRun = vi
      .spyOn(scansApi, 'dryRun')
      .mockResolvedValue({ events: [] } as unknown as ScanDryRunResponse)

    const { result } = renderHook(() => useScanForm('demo', null), { wrapper })
    act(() => result.current.setDataSourceId('ds-1'))
    act(() => result.current.setBaseQuery('SELECT * FROM analytics.events'))
    act(() => result.current.set('eventTypeId', 'et-1'))
    act(() => result.current.loadPreview())

    await waitFor(() => expect(preview).toHaveBeenCalledTimes(1))
    expect(dryRun).toHaveBeenCalledTimes(1)
  })
})

describe('useScanForm — editing a saved catalog scan (tripl-3y7z)', () => {

  // The whole defect, end to end: a saved manual scan bounded to the last 24h
  // (time column, no interval) is classified `catalog`, so its edit form opens on
  // "Catalog only". Opening the Configuration tab, changing the name and pressing
  // Save must not clear the column that bounds every run — a full payload with an
  // explicit "time_column": null is a DELETE, not an omission.
  it('preserves the saved time column through an unrelated edit', () => {
    const saved = {
      id: 'sc-1',
      data_source_id: 'ds-1',
      name: 'Nightly catalog',
      base_query: 'SELECT * FROM analytics.events',
      time_column: 'event_ts',
      interval: null,
      scan_lookback_hours: 24,
      cardinality_threshold: 100,
    } as unknown as ScanConfig

    const { result } = renderHook(() => useScanForm('demo', saved), { wrapper })

    // The form opened where formModeOf put it — Catalog only, because there is
    // no schedule.
    expect(result.current.state.mode).toBe('catalog')

    const payload = result.current.toBackendPayload()
    expect(payload.time_column).toBe('event_ts')
    expect(payload.scan_lookback_hours).toBe(24)
    // Still catalog-only: no schedule is what makes it one.
    expect(payload.interval).toBeNull()
  })
})

describe('canSubmitScanForm', () => {
  it('refuses a monitoring scan with no time column — it would report success and monitor nothing', () => {
    expect(canSubmitScanForm(formState({ timeColumn: '' }))).toBe(false)
  })

  it('refuses a monitoring scan with no schedule', () => {
    expect(canSubmitScanForm(formState({ interval: '' }))).toBe(false)
  })

  it('accepts a catalog-only scan with both empty: there, empty is the answer', () => {
    expect(canSubmitScanForm(formState({ mode: 'catalog', timeColumn: '', interval: '' }))).toBe(true)
  })

  it('still requires a source, a name and a query in either mode', () => {
    expect(canSubmitScanForm(formState({ mode: 'catalog', name: '   ' }))).toBe(false)
    expect(canSubmitScanForm(formState({ mode: 'catalog', dataSourceId: '' }))).toBe(false)
    expect(canSubmitScanForm(formState({ mode: 'catalog', baseQuery: '' }))).toBe(false)
  })

  // The defect: with no event type AND no event type column, `run_scan` aborts
  // on its own precondition in BOTH modes — so the scan the form happily created
  // could never ingest an event, and its first run landed in Recent runs as
  // failed. The gate did not check either field, so "Create scan" was enabled.
  it('refuses a scan that cannot name its events, in either mode', () => {
    const unnameable = { eventTypeId: '', eventTypeColumn: '' }

    expect(canSubmitScanForm(formState(unnameable))).toBe(false)
    expect(canSubmitScanForm(formState({ ...unnameable, mode: 'catalog' }))).toBe(false)
  })

  it('accepts either answer to "where does the event name come from?"', () => {
    expect(canSubmitScanForm(formState({ eventTypeId: 'et-1', eventTypeColumn: '' }))).toBe(true)
    expect(canSubmitScanForm(formState({ eventTypeId: '', eventTypeColumn: 'event_name' }))).toBe(true)
  })
})

describe('scanFormBlocker', () => {
  // A disabled button with no reason is how a user concludes the form is broken.
  it('names the missing answer, in the order the form asks for it', () => {
    expect(scanFormBlocker(formState({ name: '  ' }))).toBe(
      'A scan needs a name, a data source and a base query.',
    )
    expect(scanFormBlocker(formState({ eventTypeId: '', eventTypeColumn: '' }))).toBe(
      'Pick an Event type, or the Event type column your event names are in.',
    )
    expect(scanFormBlocker(formState({ timeColumn: '' }))).toBe(
      'Catalog + monitoring needs a time column and a schedule.',
    )
    expect(scanFormBlocker(formState())).toBeNull()
  })

  // No user-facing string may name a database field: the message this replaces
  // was `Either event_type_id or event_type_column must be specified`.
  it('names controls, never database columns', () => {
    const messages = [
      scanFormBlocker(formState({ name: '  ' })),
      scanFormBlocker(formState({ eventTypeId: '', eventTypeColumn: '' })),
      scanFormBlocker(formState({ timeColumn: '' })),
    ]

    for (const message of messages) {
      expect(message).not.toMatch(/event_type_id|event_type_column/)
    }
  })
})

describe('hasEventTarget — the gate on asking the warehouse anything', () => {
  it('is false only when neither answer is given', () => {
    expect(hasEventTarget(formState({ eventTypeId: '', eventTypeColumn: '' }))).toBe(false)
    expect(hasEventTarget(formState({ eventTypeId: 'et-1', eventTypeColumn: '' }))).toBe(true)
    expect(hasEventTarget(formState({ eventTypeId: '', eventTypeColumn: 'event_name' }))).toBe(true)
  })
})

describe('useScanForm — a query edit keeps the saved selections (DATA-1)', () => {
  const saved = {
    id: 'sc-1',
    data_source_id: 'ds-1',
    name: 'Main scan',
    base_query: 'SELECT * FROM analytics.events',
    event_type_column: 'event_name',
    time_column: 'event_ts',
    interval: '1h',
    json_value_paths: ['props.plan', 'context.locale'],
    distribution_drift_fields: ['country'],
    metric_breakdown_columns: [],
    event_group_rules: [],
    cardinality_threshold: 100,
  } as unknown as ScanConfig

  // One space typed into a saved scan's query, or its Format button, used to
  // clear every JSON value path and drift field behind a preview gate the user
  // could not see past — and the next Save sent the empty lists.
  it('edit query on a saved config keeps json_value_paths and drift fields', () => {
    const { result } = renderHook(() => useScanForm('demo', saved), { wrapper })

    act(() => result.current.setBaseQuery('SELECT *\nFROM analytics.events'))

    const payload = result.current.toBackendPayload()
    expect(payload.base_query).toBe('SELECT *\nFROM analytics.events')
    expect(payload.json_value_paths).toEqual(['props.plan', 'context.locale'])
    expect(payload.distribution_drift_fields).toEqual(['country'])
  })

  it('prunes only the selections the next preview no longer has a column for', async () => {
    vi.spyOn(scansApi, 'dryRun').mockResolvedValue({ events: [] } as unknown as ScanDryRunResponse)
    vi.spyOn(scansApi, 'preview').mockResolvedValue({
      columns: [
        { name: 'event_name', type_name: 'String', is_nullable: false },
        { name: 'event_ts', type_name: 'DateTime', is_nullable: false },
        { name: 'props', type_name: 'JSON', is_nullable: true },
      ],
      rows: [],
      json_columns: [],
    })
    const { result } = renderHook(() => useScanForm('demo', saved), { wrapper })

    act(() => result.current.setBaseQuery('SELECT event_name, event_ts, props FROM analytics.events'))
    act(() => result.current.loadPreview())

    await waitFor(() => expect(result.current.preview).not.toBeNull())
    expect(result.current.state.jsonValuePaths).toEqual(['props.plan'])
    expect(result.current.state.distributionDriftFields).toEqual([])
  })
})

describe('useScanForm — answers belong to the draft that asked (DATA-2)', () => {
  const oldColumns: ScanConfigPreview = {
    columns: [{ name: 'legacy_name', type_name: 'String', is_nullable: false }],
    rows: [],
    json_columns: [],
  }

  it('drops a preview that answers after the query was edited, and stops waiting for it', async () => {
    let resolveOld: (preview: ScanConfigPreview) => void = () => {}
    let oldSignal: AbortSignal | undefined
    vi.spyOn(scansApi, 'preview').mockImplementation((_slug, _request, signal) => {
      oldSignal = signal
      return new Promise(resolve => {
        resolveOld = resolve
      })
    })

    const { result } = renderHook(() => useScanForm('demo', null), { wrapper })
    act(() => result.current.setDataSourceId('ds-1'))
    act(() => result.current.setBaseQuery('SELECT legacy_name FROM old_events'))
    act(() => result.current.loadPreview())
    await waitFor(() => expect(oldSignal).toBeDefined())

    // The user moves on: a new query, and picks its naming column by hand.
    act(() => result.current.setBaseQuery('SELECT event_name FROM analytics.events'))
    act(() => result.current.set('eventTypeColumn', 'event_name'))
    expect(oldSignal?.aborted).toBe(true)

    // The old job answers anyway (its POST was already out).
    await act(async () => {
      resolveOld(oldColumns)
    })

    expect(result.current.preview).toBeNull()
    // The old columns lack `event_name`; pruning against them would have
    // cleared the column the user just chose for the new query.
    expect(result.current.state.eventTypeColumn).toBe('event_name')
  })

  it('merges discovered JSON keys only into the preview they were asked about', async () => {
    const loaded: ScanConfigPreview = {
      columns: [{ name: 'props', type_name: 'JSON', is_nullable: true }],
      rows: [],
      json_columns: [],
    }
    let resolveDiscovery: (preview: ScanConfigPreview) => void = () => {}
    vi.spyOn(scansApi, 'preview').mockImplementation((_slug, request) =>
      request.include_json_paths
        ? new Promise(resolve => {
          resolveDiscovery = resolve
        })
        : Promise.resolve(loaded),
    )

    const { result } = renderHook(() => useScanForm('demo', null), { wrapper })
    act(() => result.current.setDataSourceId('ds-1'))
    act(() => result.current.setBaseQuery('SELECT props FROM a'))
    act(() => result.current.loadPreview())
    await waitFor(() => expect(result.current.preview).not.toBeNull())

    act(() => result.current.discoverJsonPaths())
    act(() => result.current.setBaseQuery('SELECT props FROM b'))
    act(() => result.current.loadPreview())
    await waitFor(() => expect(result.current.preview).not.toBeNull())

    await act(async () => {
      resolveDiscovery({
        columns: [],
        rows: [],
        json_columns: [{ column: 'props', paths: [{ full_path: 'props.a', path: 'a', sample_values: [] }] }],
      })
    })

    expect(result.current.preview?.json_columns).toEqual([])
  })
})

describe('scanFieldErrors — numeric limits the backend would refuse (DATA-25)', () => {
  it('flags zero, negatives and fractions instead of truncating them', () => {
    const errors = scanFieldErrors(formState({ scanRowLimit: '0', scanLookbackHours: '-3', metricsRowLimit: '2.5' }))
    expect(Object.keys(errors).sort()).toEqual(['metricsRowLimit', 'scanLookbackHours', 'scanRowLimit'])
    expect(toBackendPayload(formState({ scanRowLimit: '0' })).scan_row_limit).toBeNull()
  })

  it('requires a cardinality threshold rather than sending a cleared field as 0', () => {
    expect(scanFieldErrors(formState({ cardinalityThreshold: '' })).cardinalityThreshold).toBeTruthy()
  })

  it('refuses an out-of-range traffic share instead of saving the default in its place', () => {
    const state = formState({ appVersionColumn: 'app_version', appVersionActiveShareMin: '5' })
    expect(scanFieldErrors(state).appVersionActiveShareMin).toMatch(/between 0 and 1/)
    expect(scanFormBlocker(state)).toBe('Fix Traffic share that counts as released.')
  })

  it('does not refuse a save over metrics limits that are not on screen outside monitoring', () => {
    const state = formState({ mode: 'catalog', metricsRowLimit: '0', metricBreakdownValuesLimit: '0' })
    expect(scanFieldErrors(state)).toEqual({})
    expect(scanFieldErrors({ ...state, mode: 'monitoring' })).toHaveProperty('metricsRowLimit')
  })

  it('leaves blank optional limits alone — blank is the backend default', () => {
    expect(scanFieldErrors(formState({ scanRowLimit: '', metricsRowLimit: '', scanLookbackHours: '' }))).toEqual({})
    expect(scanFormBlocker(formState())).toBeNull()
  })
})
