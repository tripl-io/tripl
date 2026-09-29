import { useEffect, useRef, useState } from 'react'
import { type UseMutationResult, useMutation } from '@tanstack/react-query'
import { scansApi } from '@/api/scans'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type {
  EventGroupRule,
  IntervalCode,
  ScanConfig,
  ScanConfigPreview,
  ScanDryRunRequest,
  ScanDryRunResponse,
  ScanSetupPreset,
} from '@/types'
import { type UiEventGroupRule, stripUiIds, withUiIds } from './scanFormTypes'
import { guessEventColumn, guessPropertiesColumn, jsonColumnNames } from './scanSetupPreset'
import { type ScanFormMode, formModeOf } from './scanMode'
import {
  eligibleChunkIntervals,
  parseOptionalPositiveInt,
  parseOptionalShare,
  positiveIntError,
  shareError,
  splitFullJsonPath,
} from './scanUtils'

// Shape of the create/update payload shared by both API calls. Built once from
// form state so the create page and the Configuration tab stay in sync.
export interface ScanFormPayload {
  name: string
  base_query: string
  event_type_id: string | null
  event_type_column: string | null
  time_column: string | null
  event_name_format: string | null
  json_value_paths: string[]
  event_group_rules: EventGroupRule[]
  setup_preset: ScanSetupPreset
  event_name_column: string | null
  properties_column: string | null
  json_string_columns: string[]
  metric_breakdown_columns: string[]
  metric_breakdown_values_limit: number | null
  distribution_drift_fields: string[]
  app_version_column: string | null
  app_version_prerelease_pattern: string | null
  app_version_active_share_min: number | null
  platform_column: string | null
  cardinality_threshold: number
  interval: string | null
  replay_chunk_interval: string | null
  scan_lookback_hours: number | null
  scan_row_limit: number | null
  metrics_row_limit: number | null
}

export interface ScanFormState {
  /**
   * Form state only — never sent to the backend, never stored. It decides which
   * fields the form asks for and whether the payload carries a schedule (see
   * scanMode.ts). It does NOT decide the time column: that is a query bound in
   * both modes and is the user's to set or clear.
   */
  mode: ScanFormMode
  /**
   * `event_properties` asks for two columns and derives the naming fields;
   * `custom` asks for them all. Switching keeps the other setup's answers in
   * state, so switching back restores them; only the payload drops them.
   */
  setupPreset: ScanSetupPreset
  /** The preset's event-name column and JSON properties column. */
  eventNameColumn: string
  propertiesColumn: string
  /**
   * Text columns every read of the source parses as JSON (F23.9), in both
   * setups: their keys become properties like a JSON column's.
   */
  jsonStringColumns: string[]
  dataSourceId: string
  name: string
  baseQuery: string
  eventTypeId: string
  eventTypeColumn: string
  timeColumn: string
  appVersionColumn: string
  appVersionPrereleasePattern: string
  appVersionActiveShareMin: string
  platformColumn: string
  eventNameFormat: string
  jsonValuePaths: string[]
  eventGroupRules: UiEventGroupRule[]
  metricBreakdownColumns: string[]
  metricBreakdownValuesLimit: string
  distributionDriftFields: string[]
  // A string like every other numeric input: `Number('')` made a cleared field
  // a 0 the backend rejects with a raw 422 (DATA-25).
  cardinalityThreshold: string
  interval: string
  chunkInterval: string
  scanLookbackHours: string
  scanRowLimit: string
  metricsRowLimit: string
}

function initialState(scanConfig: ScanConfig | null): ScanFormState {
  return {
    mode: formModeOf(scanConfig),
    setupPreset: scanConfig?.setup_preset ?? 'custom',
    eventNameColumn: scanConfig?.event_name_column ?? '',
    propertiesColumn: scanConfig?.properties_column ?? '',
    jsonStringColumns: scanConfig?.json_string_columns ?? [],
    dataSourceId: scanConfig?.data_source_id ?? '',
    name: scanConfig?.name ?? '',
    baseQuery: scanConfig?.base_query ?? '',
    eventTypeId: scanConfig?.event_type_id ?? '',
    eventTypeColumn: scanConfig?.event_type_column ?? '',
    timeColumn: scanConfig?.time_column ?? '',
    appVersionColumn: scanConfig?.app_version_column ?? '',
    appVersionPrereleasePattern: scanConfig?.app_version_prerelease_pattern ?? '',
    appVersionActiveShareMin: scanConfig?.app_version_active_share_min != null
      ? String(scanConfig.app_version_active_share_min)
      : '',
    platformColumn: scanConfig?.platform_column ?? '',
    eventNameFormat: scanConfig?.event_name_format ?? '',
    jsonValuePaths: scanConfig?.json_value_paths ?? [],
    eventGroupRules: withUiIds(scanConfig?.event_group_rules ?? []),
    metricBreakdownColumns: scanConfig?.metric_breakdown_columns ?? [],
    metricBreakdownValuesLimit: scanConfig?.metric_breakdown_values_limit
      ? String(scanConfig.metric_breakdown_values_limit)
      : '',
    distributionDriftFields: scanConfig?.distribution_drift_fields ?? [],
    cardinalityThreshold: String(scanConfig?.cardinality_threshold ?? 100),
    interval: scanConfig?.interval ?? '',
    chunkInterval: scanConfig?.replay_chunk_interval ?? '',
    // New scans default to a 24h lookback (matches the create-page mockup); when
    // editing an existing config an explicit null stays empty (backend default).
    scanLookbackHours: scanConfig
      ? (scanConfig.scan_lookback_hours == null ? '' : String(scanConfig.scan_lookback_hours))
      : '24',
    scanRowLimit: scanConfig?.scan_row_limit == null ? '' : String(scanConfig.scan_row_limit),
    metricsRowLimit: scanConfig?.metrics_row_limit == null ? '' : String(scanConfig.metrics_row_limit),
  }
}

/**
 * Assembles the create/update payload from form state.
 *
 * Catalog only is the absence of a SCHEDULE, and nothing else. The dispatcher
 * selects on `time_column IS NOT NULL AND interval IS NOT NULL`
 * (worker/tasks/metrics/schedule.py:349-350), so a null interval alone already
 * makes a config catalog-only; nulling the time column with it bought nothing
 * and cost the per-run bound, because `resolve_lookback_window` returns None
 * without a time column and the run then reads the whole base query. That is why
 * only `interval` and `replay_chunk_interval` are mode-gated here: a saved time
 * column must survive an edit made in Catalog only, and a catalog scan bounded
 * to the last 24h is a config a user is entitled to keep.
 *
 * Monitoring additionally REQUIRES the time column — `canSubmitScanForm` refuses
 * to submit without it, so the silently-never-monitoring config this whole change
 * exists to kill cannot be created.
 */
export function toBackendPayload(state: ScanFormState): ScanFormPayload {
  const monitoring = state.mode === 'monitoring'
  // The preset's naming fields are the backend's to derive from the two
  // columns; sending the custom setup's answers would be refused as a conflict.
  const preset = state.setupPreset === 'event_properties'
  return {
    name: state.name,
    base_query: state.baseQuery,
    // Empty in the preset means "the preset's own event type", which the
    // backend finds or creates on save.
    event_type_id: state.eventTypeId || null,
    event_type_column: preset ? null : state.eventTypeColumn || null,
    time_column: state.timeColumn || null,
    event_name_format: preset ? null : state.eventNameFormat || null,
    json_value_paths: preset ? [] : state.jsonValuePaths,
    event_group_rules: preset ? [] : stripUiIds(state.eventGroupRules),
    setup_preset: state.setupPreset,
    event_name_column: preset ? state.eventNameColumn || null : null,
    properties_column: preset ? state.propertiesColumn || null : null,
    json_string_columns: state.jsonStringColumns,
    metric_breakdown_columns: state.metricBreakdownColumns,
    metric_breakdown_values_limit: parseOptionalPositiveInt(state.metricBreakdownValuesLimit),
    distribution_drift_fields: state.distributionDriftFields,
    app_version_column: state.appVersionColumn || null,
    app_version_prerelease_pattern: state.appVersionColumn
      ? state.appVersionPrereleasePattern.trim() || null
      : null,
    app_version_active_share_min: state.appVersionColumn
      ? parseOptionalShare(state.appVersionActiveShareMin)
      : null,
    platform_column: state.platformColumn || null,
    // `scanFormBlocker` refuses anything but a whole number >= 1 before a save,
    // so the fallback only ever reaches a dry run of a half-typed draft.
    cardinality_threshold: parseOptionalPositiveInt(state.cardinalityThreshold) ?? 100,
    interval: monitoring ? state.interval || null : null,
    replay_chunk_interval: monitoring ? state.chunkInterval || null : null,
    scan_lookback_hours: parseOptionalPositiveInt(state.scanLookbackHours),
    scan_row_limit: parseOptionalPositiveInt(state.scanRowLimit),
    metrics_row_limit: parseOptionalPositiveInt(state.metricsRowLimit),
  }
}

/**
 * The draft a dry run is computed from, derived from the SAME payload the save
 * button would send.
 *
 * Building it out of `toBackendPayload` is the point: "what would this scan
 * create?" has to be answered for the config that would actually be saved, not
 * for a second reading of form state that could drift from it. The window the
 * dry run reads is therefore the window the saved scan would read — including
 * the case with no time column, where both read everything the base query
 * returns and the panel says so.
 */
export function toDryRunRequest(state: ScanFormState): ScanDryRunRequest {
  const payload = toBackendPayload(state)
  return {
    data_source_id: state.dataSourceId,
    base_query: payload.base_query,
    event_type_id: payload.event_type_id,
    event_type_column: payload.event_type_column,
    time_column: payload.time_column,
    event_name_format: payload.event_name_format,
    event_group_rules: payload.event_group_rules,
    json_value_paths: payload.json_value_paths,
    setup_preset: payload.setup_preset,
    event_name_column: payload.event_name_column,
    properties_column: payload.properties_column,
    json_string_columns: payload.json_string_columns,
    cardinality_threshold: payload.cardinality_threshold,
    app_version_column: payload.app_version_column,
    platform_column: payload.platform_column,
    scan_lookback_hours: payload.scan_lookback_hours,
  }
}

/** Hover text on the disabled save/create button in Catalog + monitoring. */
export const MONITORING_INCOMPLETE_TITLE =
  'Catalog + monitoring needs a time column and a schedule.'

/** Hover text when the scan has no answer to "where do event names come from?". */
export const EVENT_NAMING_INCOMPLETE_TITLE =
  'Pick an Event type, or the Event type column your event names are in.'

/** Hover text when the Event + properties setup is missing one of its columns. */
export const PRESET_INCOMPLETE_TITLE =
  'Pick the event column and the properties column.'

/** Hover text before the three fields every scan needs are filled in. */
export const ESSENTIALS_INCOMPLETE_TITLE =
  'A scan needs a name, a data source and a base query.'

/** The numeric fields the form validates, with the label each one is shown under. */
export const SCAN_NUMERIC_FIELD_LABEL = {
  cardinalityThreshold: 'Cardinality threshold',
  metricBreakdownValuesLimit: 'Value limit',
  appVersionActiveShareMin: 'Traffic share that counts as released',
  scanLookbackHours: 'Lookback (hours)',
  scanRowLimit: 'Row cap per run',
  metricsRowLimit: 'Row cap per metrics run',
} as const

export type ScanNumericField = keyof typeof SCAN_NUMERIC_FIELD_LABEL

/**
 * Field-level messages for every numeric input holding a value the backend
 * would refuse (`ge=1`, or a share in (0, 1)). The form renders each under its
 * input, and {@link scanFormBlocker} refuses the save, so a bad value is neither
 * coerced into a different one nor answered by a raw 422 (DATA-25).
 */
export function scanFieldErrors(state: ScanFormState): Partial<Record<ScanNumericField, string>> {
  const errors: Partial<Record<ScanNumericField, string>> = {}
  const check = (field: ScanNumericField, error: string | null) => {
    if (error) errors[field] = error
  }
  check('cardinalityThreshold', positiveIntError(state.cardinalityThreshold, { required: true }))
  // The metrics fields are only on screen in Catalog + monitoring; refusing a
  // save over an input the reader cannot see would leave no way to fix it.
  const monitoring = state.mode === 'monitoring'
  if (monitoring) check('metricBreakdownValuesLimit', positiveIntError(state.metricBreakdownValuesLimit))
  // Not sent without a version column, so not a reason to refuse the save.
  if (state.appVersionColumn) check('appVersionActiveShareMin', shareError(state.appVersionActiveShareMin))
  check('scanLookbackHours', positiveIntError(state.scanLookbackHours))
  check('scanRowLimit', positiveIntError(state.scanRowLimit))
  if (monitoring) check('metricsRowLimit', positiveIntError(state.metricsRowLimit))
  return errors
}

/**
 * Whether the config says how its events are named — an event type for every
 * row, or the column each row's event name is read from.
 *
 * Neither is not a configuration: `run_scan` and the dry-run planner both abort
 * on it, in both modes, so a scan that answers this question with nothing cannot
 * ingest a single event. That makes it a save gate rather than a warning, and it
 * is why nothing asks the warehouse anything until one of the two is set.
 */
export function hasEventTarget(state: ScanFormState): boolean {
  // The preset names events from its event column and files them under the
  // chosen event type or its own, so its two columns are the whole answer.
  if (state.setupPreset === 'event_properties') {
    return Boolean(state.eventNameColumn && state.propertiesColumn)
  }
  return Boolean(state.eventTypeId || state.eventTypeColumn)
}

/**
 * The first unmet requirement, in the order the form asks for them, or null when
 * the config can be saved. Doubles as the disabled button's hover text, so the
 * reason is never "the button is off and I do not know why".
 */
export function scanFormBlocker(state: ScanFormState): string | null {
  if (!state.dataSourceId || !state.name.trim() || !state.baseQuery.trim()) {
    return ESSENTIALS_INCOMPLETE_TITLE
  }
  if (!hasEventTarget(state)) {
    return state.setupPreset === 'event_properties'
      ? PRESET_INCOMPLETE_TITLE
      : EVENT_NAMING_INCOMPLETE_TITLE
  }
  // In Catalog only, an empty time column and an empty schedule are deliberate
  // answers; in Catalog + monitoring a config missing either one is never
  // dispatched and collects nothing, forever.
  if (state.mode === 'monitoring' && !(state.timeColumn && state.interval)) {
    return MONITORING_INCOMPLETE_TITLE
  }
  // Named by its label, so the hover text says which input to look at.
  const invalid = Object.keys(scanFieldErrors(state))[0] as ScanNumericField | undefined
  if (invalid) return `Fix ${SCAN_NUMERIC_FIELD_LABEL[invalid]}.`
  return null
}

/**
 * What a preview's columns depend on: the source and the query.
 *
 * Each loaded preview is stamped with the draft it was requested for, the way a
 * dry run is, so an answer that arrives after the user moved on can never land
 * on the newer draft (DATA-2). The time column and lookback are left out on
 * purpose: they bound which rows come back, not which columns, and they are
 * picked FROM a loaded preview — keying on them would throw away a reload the
 * moment the user chose a time column while it was in flight.
 */
export function previewDraftKey(state: Pick<ScanFormState, 'dataSourceId' | 'baseQuery'>): string {
  return JSON.stringify({ dataSourceId: state.dataSourceId, baseQuery: state.baseQuery })
}

type PreviewRequest = Parameters<typeof scansApi.preview>[1]

/** A preview or JSON-discovery request, and the draft it was asked for. */
export interface PreviewVariables {
  key: string
  request: PreviewRequest
  signal: AbortSignal
}

/** Whether two column lists name the same columns, in any order. */
function sameColumns(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every(column => b.includes(column))
}

/** Save gate shared by create and edit. */
export function canSubmitScanForm(state: ScanFormState): boolean {
  return scanFormBlocker(state) === null
}

export interface UseScanFormResult {
  state: ScanFormState
  set: <K extends keyof ScanFormState>(key: K, value: ScanFormState[K]) => void
  /** The loaded preview, or null when none was loaded for the draft as it stands. */
  preview: ScanConfigPreview | null
  /**
   * True when the "Parse as JSON" columns changed since the preview was loaded:
   * its column types and summary describe the source parsed the old way. The
   * pickers already count a ticked column as JSON; a reload fills its keys in.
   */
  previewParseStale: boolean
  /** "What this scan would create", or null before the first check. */
  dryRun: ScanDryRunResponse | null
  /**
   * True when the form has changed since the displayed answer was computed. A
   * dry run is a statement about a specific draft; once the draft moves, the
   * answer is stale, and a stale "would create 3 events: A, B, C" is worse than
   * no answer at all.
   */
  dryRunStale: boolean
  // Preview/discovery mutations (real warehouse-backed jobs).
  previewMut: UseMutationResult<ScanConfigPreview, unknown, PreviewVariables>
  discoverJsonMut: UseMutationResult<ScanConfigPreview, unknown, PreviewVariables>
  dryRunMut: UseMutationResult<ScanDryRunResponse, unknown, ScanDryRunRequest>
  /** Load the sample rows and the dry run together — one button, one answer. */
  loadPreview: () => void
  /** Ask the warehouse for the nested JSON keys of the loaded preview's columns. */
  discoverJsonPaths: () => void
  /** Field-level messages for numeric inputs the backend would refuse. */
  fieldErrors: Partial<Record<ScanNumericField, string>>
  /** Re-answer "what would this scan create?" for the draft as it stands now. */
  runDryRun: () => void
  // Field-aware handlers that drop now-invalid column references.
  setBaseQuery: (value: string) => void
  setDataSourceId: (value: string) => void
  setEventTypeColumn: (value: string) => void
  setTimeColumn: (value: string) => void
  setAppVersionColumn: (value: string) => void
  setPlatformColumn: (value: string) => void
  setSetupPreset: (value: ScanSetupPreset) => void
  setInterval: (value: string) => void
  toggleJsonValuePath: (path: string) => void
  toggleMetricBreakdownColumn: (column: string) => void
  toggleDistributionDriftField: (field: string) => void
  toggleJsonStringColumn: (column: string) => void
  toBackendPayload: () => ScanFormPayload
}

// Centralizes the create/edit form behavior (state, preview gating, column
// invalidation, payload assembly) so the create page and Configuration tab share
// one implementation. `dataSourceIdForPreview` lets the edit flow lock the source.
export function useScanForm(
  slug: string,
  scanConfig: ScanConfig | null,
): UseScanFormResult {
  const [state, setState] = useState<ScanFormState>(() => initialState(scanConfig))
  // The preview AND the draft it was loaded for (see `previewDraftKey`). Shown
  // only while the draft still matches, so a query edit hides it without
  // anything having to remember to clear it.
  const [previewResult, setPreviewResult] = useState<
    { preview: ScanConfigPreview; requestKey: string; parsed: string[] } | null
  >(null)
  // The answer AND the draft it answers for, so staleness is a fact rather than
  // a guess. Serializing the request is enough: it is exactly the set of inputs
  // the backend planner reads.
  const [dryRunResult, setDryRunResult] = useState<
    { answer: ScanDryRunResponse; requestKey: string } | null
  >(null)
  // The draft the newest preview was asked for. An older request that answers
  // later is dropped rather than replacing a newer preview (DATA-2).
  const latestPreviewKeyRef = useRef<string | null>(null)
  // Aborted when the draft changes source or query, and on unmount, so a
  // warehouse job's poll loop stops once nobody is waiting for its answer. The
  // job itself keeps running on the worker; only the wait stops.
  const inFlightRef = useRef<AbortController | null>(null)
  const draftSignal = () => {
    inFlightRef.current ??= new AbortController()
    return inFlightRef.current.signal
  }
  const abortInFlight = () => {
    inFlightRef.current?.abort()
    inFlightRef.current = null
  }
  useEffect(() => {
    const inFlight = inFlightRef
    return () => inFlight.current?.abort()
  }, [])

  const preview =
    previewResult && previewResult.requestKey === previewDraftKey(state) ? previewResult.preview : null
  const previewParseStale =
    preview !== null && !sameColumns(previewResult?.parsed ?? [], state.jsonStringColumns)

  const set = <K extends keyof ScanFormState>(key: K, value: ScanFormState[K]) =>
    setState(current => ({ ...current, [key]: value }))

  const setMany = (patch: Partial<ScanFormState>) =>
    setState(current => ({ ...current, ...patch }))

  const presetColumns =
    state.setupPreset === 'event_properties' && state.eventNameColumn && state.propertiesColumn
      ? { event_name_column: state.eventNameColumn, properties_column: state.propertiesColumn }
      : {}
  const previewRequest = (): PreviewRequest => ({
    data_source_id: state.dataSourceId,
    base_query: state.baseQuery,
    time_column: state.timeColumn || null,
    scan_lookback_hours: parseOptionalPositiveInt(state.scanLookbackHours),
    // Read the source the way the saved scan will, so a ticked text column
    // comes back as JSON and its keys can be discovered.
    json_string_columns: state.jsonStringColumns,
    // With both, the preview also says which events and properties the sample
    // rows would yield (`event_properties`).
    ...presetColumns,
  })

  const previewMut = useMutation<ScanConfigPreview, unknown, PreviewVariables>({
    // Rendered inline as "Preview failed".
    meta: SILENT_ERROR_META,
    mutationFn: ({ request, signal }) => scansApi.preview(slug, request, signal),
    onSuccess: (data, { key, request }) => {
      if (key !== latestPreviewKeyRef.current) return
      setPreviewResult({ preview: data, requestKey: key, parsed: request.json_string_columns ?? [] })
      const has = (name: string) => data.columns.some(column => column.name === name)
      setState(current => {
        // Columns of a query the user has since edited say nothing about the
        // one on screen, so they prune nothing.
        if (previewDraftKey(current) !== key) return current
        const eventTypeColumn = has(current.eventTypeColumn) ? current.eventTypeColumn : ''
        const timeColumn = has(current.timeColumn) ? current.timeColumn : ''
        const appVersionColumn = has(current.appVersionColumn) ? current.appVersionColumn : ''
        const platformColumn = has(current.platformColumn) ? current.platformColumn : ''
        // A parsed column the query no longer returns is not parsed.
        const jsonStringColumns = current.jsonStringColumns.filter(has)
        // The preset's columns: kept while the query still returns them (the
        // properties column only while it is still JSON, or parsed as JSON),
        // else guessed from the column names so the common layout needs no
        // picking at all.
        const jsonColumns = jsonColumnNames(data, jsonStringColumns)
        const eventNameColumn = has(current.eventNameColumn) && !jsonColumns.includes(current.eventNameColumn)
          ? current.eventNameColumn
          : guessEventColumn(data, jsonStringColumns)
        const propertiesColumn = jsonColumns.includes(current.propertiesColumn)
          ? current.propertiesColumn
          : guessPropertiesColumn(data, jsonStringColumns)
        const reserved = new Set(
          [eventTypeColumn, timeColumn, appVersionColumn, platformColumn].filter(Boolean),
        )
        return {
          ...current,
          eventTypeColumn,
          jsonStringColumns,
          eventNameColumn: eventNameColumn === propertiesColumn ? '' : eventNameColumn,
          propertiesColumn,
          timeColumn,
          appVersionColumn,
          platformColumn,
          appVersionPrereleasePattern: appVersionColumn ? current.appVersionPrereleasePattern : '',
          appVersionActiveShareMin: appVersionColumn ? current.appVersionActiveShareMin : '',
          metricBreakdownColumns: current.metricBreakdownColumns.filter(
            column => has(column) && !reserved.has(column),
          ),
          distributionDriftFields: current.distributionDriftFields.filter(
            field => has(field) && !reserved.has(field),
          ),
          // A path lives under a column; it goes only once its column has.
          jsonValuePaths: current.jsonValuePaths.filter(path => {
            const parsed = splitFullJsonPath(path)
            return !parsed || has(parsed.column)
          }),
        }
      })
    },
  })

  // Keyed off the mutation's own variables, not off a closure: React Query keeps
  // the LATEST options object, so an onSuccess that read the current render's
  // request would stamp an in-flight answer with a draft it was not computed
  // from — and silently call a stale answer fresh.
  const dryRunMut = useMutation<ScanDryRunResponse, unknown, ScanDryRunRequest>({
    // Rendered inline by ScanPreviewPanel.
    meta: SILENT_ERROR_META,
    mutationFn: request => scansApi.dryRun(slug, request, draftSignal()),
    onSuccess: (answer, request) =>
      setDryRunResult({ answer, requestKey: JSON.stringify(request) }),
  })

  /**
   * A dry run is only asked for once the draft can answer "how are events
   * named?". Firing it without an event type or an event type column made the
   * worker abort on its own internal precondition, and the panel reported that
   * as `Scan failed: …` — on the first click of every scan created on the
   * defaults. The form says what is missing instead; nothing is asked of the
   * warehouse until it can be answered.
   */
  const runDryRun = () => {
    if (!hasEventTarget(state)) return
    dryRunMut.mutate(toDryRunRequest(state))
  }

  const discoverJsonMut = useMutation<ScanConfigPreview, unknown, PreviewVariables>({
    // Rendered inline by JsonValuePathsPicker.
    meta: SILENT_ERROR_META,
    mutationFn: ({ request, signal }) => scansApi.preview(slug, request, signal),
    onSuccess: (data, { key }) => {
      // Merge into the preview it was asked about; a preview loaded for another
      // draft since then keeps its own keys.
      setPreviewResult(current =>
        current && current.requestKey === key
          ? { ...current, preview: { ...current.preview, json_columns: data.json_columns } }
          : current,
      )
    },
  })

  const discoverJsonPaths = () =>
    discoverJsonMut.mutate({
      key: previewDraftKey(state),
      request: { ...previewRequest(), json_value_paths: state.jsonValuePaths, include_json_paths: true },
      signal: draftSignal(),
    })

  /**
   * A new source or a new query: every warehouse answer on screen now describes
   * a draft that no longer exists.
   *
   * Only the ANSWERS go. The user's own selections — JSON value paths, drift
   * fields — stay, and the next preview prunes the ones its columns no longer
   * carry. Clearing them here meant one space typed into a saved scan's query
   * (or its Format button) wiped every saved path and drift field behind a
   * preview gate the user could not see past, and the next Save sent the empty
   * lists (DATA-1).
   */
  const resetPreviewDerived = () => {
    abortInFlight()
    latestPreviewKeyRef.current = null
    previewMut.reset()
    discoverJsonMut.reset()
    // Not merely stale: keeping it on screen would attribute events to a query
    // that no longer exists.
    setDryRunResult(null)
    dryRunMut.reset()
  }

  /**
   * One button, both warehouse jobs. The sample rows populate the column pickers
   * and the dry run says what the config would create; splitting them into two
   * controls would make "what would this create?" an optional extra, which is
   * how the promise in the docs went unbuilt in the first place.
   *
   * The dry run is the half that can be unanswerable: on a brand-new scan the
   * column that names events is picked FROM these sample rows, so the first
   * click loads rows only and `runDryRun` no-ops until the draft names its
   * events.
   */
  const loadPreview = () => {
    discoverJsonMut.reset()
    const key = previewDraftKey(state)
    latestPreviewKeyRef.current = key
    previewMut.mutate({ key, request: { ...previewRequest(), limit: 10 }, signal: draftSignal() })
    runDryRun()
  }

  const setBaseQuery = (value: string) => {
    if (value === state.baseQuery) return
    setMany({ baseQuery: value })
    resetPreviewDerived()
  }

  const setDataSourceId = (value: string) => {
    if (value === state.dataSourceId) return
    setMany({ dataSourceId: value })
    resetPreviewDerived()
  }

  const setEventTypeColumn = (value: string) =>
    setState(current => ({
      ...current,
      eventTypeColumn: value,
      metricBreakdownColumns: current.metricBreakdownColumns.filter(column => column !== value),
      distributionDriftFields: current.distributionDriftFields.filter(field => field !== value),
    }))

  const setTimeColumn = (value: string) =>
    setState(current => ({
      ...current,
      timeColumn: value,
      metricBreakdownColumns: current.metricBreakdownColumns.filter(column => column !== value),
      distributionDriftFields: current.distributionDriftFields.filter(field => field !== value),
    }))

  const setAppVersionColumn = (value: string) =>
    setState(current => ({
      ...current,
      appVersionColumn: value,
      appVersionPrereleasePattern: value ? current.appVersionPrereleasePattern : '',
      appVersionActiveShareMin: value ? current.appVersionActiveShareMin : '',
      metricBreakdownColumns: current.metricBreakdownColumns.filter(column => column !== value),
      distributionDriftFields: current.distributionDriftFields.filter(field => field !== value),
    }))

  const setPlatformColumn = (value: string) =>
    setState(current => ({
      ...current,
      platformColumn: value,
      metricBreakdownColumns: current.metricBreakdownColumns.filter(column => column !== value),
      distributionDriftFields: current.distributionDriftFields.filter(field => field !== value),
    }))

  const setSetupPreset = (value: ScanSetupPreset) => {
    if (value === state.setupPreset) return
    set('setupPreset', value)
    // The answer on screen was computed for the other setup.
    setDryRunResult(null)
    dryRunMut.reset()
  }

  const setInterval = (value: string) =>
    setState(current => ({
      ...current,
      interval: value,
      chunkInterval:
        current.chunkInterval
        && !eligibleChunkIntervals(value).includes(current.chunkInterval as IntervalCode)
          ? ''
          : current.chunkInterval,
    }))

  const toggleJsonValuePath = (path: string) =>
    setState(current => ({
      ...current,
      jsonValuePaths: current.jsonValuePaths.includes(path)
        ? current.jsonValuePaths.filter(item => item !== path)
        : [...current.jsonValuePaths, path],
    }))

  const toggleMetricBreakdownColumn = (column: string) =>
    setState(current => ({
      ...current,
      metricBreakdownColumns: current.metricBreakdownColumns.includes(column)
        ? current.metricBreakdownColumns.filter(item => item !== column)
        : [...current.metricBreakdownColumns, column],
    }))

  const toggleDistributionDriftField = (field: string) =>
    setState(current => ({
      ...current,
      distributionDriftFields: current.distributionDriftFields.includes(field)
        ? current.distributionDriftFields.filter(item => item !== field)
        : [...current.distributionDriftFields, field],
    }))

  /**
   * Parse a text column as JSON, or stop. A parsed column is a JSON document
   * for the whole scan, so ticking it drops it from the settings that read it
   * as text (the event column, scalar breakdowns and drift fields — the
   * backend refuses those); unticking the preset's properties column clears
   * it, since it is no longer JSON.
   */
  const toggleJsonStringColumn = (column: string) =>
    setState(current => {
      if (current.jsonStringColumns.includes(column)) {
        return {
          ...current,
          jsonStringColumns: current.jsonStringColumns.filter(item => item !== column),
          propertiesColumn: current.propertiesColumn === column ? '' : current.propertiesColumn,
        }
      }
      return {
        ...current,
        jsonStringColumns: [...current.jsonStringColumns, column],
        eventNameColumn: current.eventNameColumn === column ? '' : current.eventNameColumn,
        metricBreakdownColumns: current.metricBreakdownColumns.filter(item => item !== column),
        distributionDriftFields: current.distributionDriftFields.filter(item => item !== column),
      }
    })

  return {
    state,
    set,
    preview,
    previewParseStale,
    dryRun: dryRunResult?.answer ?? null,
    dryRunStale: dryRunResult != null && dryRunResult.requestKey !== JSON.stringify(toDryRunRequest(state)),
    previewMut,
    discoverJsonMut,
    dryRunMut,
    loadPreview,
    discoverJsonPaths,
    fieldErrors: scanFieldErrors(state),
    runDryRun,
    setBaseQuery,
    setDataSourceId,
    setEventTypeColumn,
    setTimeColumn,
    setAppVersionColumn,
    setPlatformColumn,
    setSetupPreset,
    setInterval,
    toggleJsonValuePath,
    toggleMetricBreakdownColumn,
    toggleDistributionDriftField,
    toggleJsonStringColumn,
    toBackendPayload: () => toBackendPayload(state),
  }
}
