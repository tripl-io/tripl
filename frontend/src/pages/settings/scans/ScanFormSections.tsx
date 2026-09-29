import { useEffect, useRef, useState, type ReactNode } from 'react'
import { ChevronDown, ChevronRight, Play } from 'lucide-react'
import type { DataSource, EventType, IntervalCode } from '@/types'
import { useDataSourceSchema } from '@/hooks/useDataSourceSchema'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ErrorState } from '@/components/error-state'
import { AppVersionFields } from './AppVersionFields'
import { CreateMissingFieldsButton } from './CreateMissingFieldsButton'
import { DistributionDriftPicker } from './DistributionDriftPicker'
import { EventGroupRulesEditor } from './EventGroupRulesEditor'
import { JsonValuePathsPicker } from './JsonValuePathsPicker'
import { MetricBreakdownPicker } from './MetricBreakdownPicker'
import { ScanCausalNote } from './ScanCausalNote'
import { ScanPreviewPanel } from './ScanPreviewPanel'
import { PresetColumnFields, SetupPresetChoice } from './ScanSetupPresetFields'
import { LazySqlEditor } from '@/components/sql-editor-lazy'
import { Field, NativeSelect, SCard } from '@/components/settings/kit'
import { FieldError } from '@/components/forms/FieldError'
import { sqlPlaceholder } from '@/components/forms/placeholders'
import { invalidAria } from '@/components/forms/validation'
import type { ScanFormMode } from './scanMode'
import { rowCapHint, useRowLimitDefaults } from './rowCapHints'
import type { NamingFixTarget } from './scanDryRunWarnings'
import { LIMITS_SECTION_ID } from './scanErrorNextStep'
import { CHUNK_LABELS, eligibleChunkIntervals } from './scanUtils'
import {
  MONITORING_INCOMPLETE_TITLE,
  type UseScanFormResult,
  hasEventTarget,
  scanFormBlocker,
} from './useScanForm'

// The manual/"no schedule" option is gone: a schedule is only ever asked for in
// Catalog + monitoring, where leaving it empty is the defect this form exists to
// prevent. Catalog only does not render the field at all.
const INTERVAL_OPTIONS: { value: IntervalCode; label: string }[] = [
  { value: '15m', label: 'Every 15 min' },
  { value: '1h', label: 'Every hour' },
  { value: '6h', label: 'Every 6 hours' },
  { value: '1d', label: 'Every day' },
  { value: '1w', label: 'Every week' },
]

const MODE_OPTIONS: {
  value: ScanFormMode
  label: string
  description: string
}[] = [
  {
    value: 'monitoring',
    label: 'Catalog + monitoring',
    description:
      'Adds events and fields to your tracking plan and records metric points, so anomalies and alerts can fire. Needs a time column and a schedule.',
  },
  {
    value: 'catalog',
    label: 'Catalog only',
    description:
      'Adds events and fields to your tracking plan when you run it. No schedule, so no metric points, no anomalies and no alerts.',
  },
]

const PREVIEW_GATE_TEXT =
  "Load preview first — tripl needs your query's columns to offer choices here."

const NO_LOOKBACK_WITHOUT_TIME_COLUMN =
  'Each run reads everything the base query returns. Pick a Time column to bound runs to a window.'

interface SectionProps {
  form: UseScanFormResult
  slug: string
  branchId: string | null
  dataSources: DataSource[]
  eventTypes: EventType[]
  // Configuration tab locks the data source (a scan can't change source); the
  // create page lets the user pick one.
  sourceLocked: boolean
  // Configuration tab for someone who may not edit it: the SQL is shown, not
  // editable, and the editor-only schema lookup behind autocomplete is skipped.
  readOnly?: boolean
}

/**
 * An advanced section, collapsed until asked for.
 *
 * The header carries one line of "what this is for and what happens if you leave
 * it alone" — the form asks 22 questions and eight of them need knowledge of
 * tripl's detection internals that exists nowhere else in the product.
 *
 * `defaultOpen` is computed by each caller from the form state, so editing a
 * config that already uses a section opens it rather than hiding the user's own
 * settings behind a chevron.
 */
function CollapsibleSection({
  title,
  explanation,
  defaultOpen,
  toggleId,
  sectionId,
  revealOnMount = false,
  readOnly = false,
  children,
}: {
  title: string
  explanation: string
  defaultOpen: boolean
  /** Lets another control open this section (the dry run's flood warning). */
  toggleId?: string
  /** An anchor a link can name (`#scan-limits`). */
  sectionId?: string
  /** Scroll to the section and focus its toggle once, on arrival by link. */
  revealOnMount?: boolean
  /**
   * Lock the fields, not the disclosure: someone who may not edit the scan
   * still opens a section to read it, and a link to `#scan-limits` still
   * lands focus on its toggle.
   */
  readOnly?: boolean
  children: ReactNode
}) {
  const [open, setOpen] = useState(defaultOpen)
  const sectionRef = useRef<HTMLElement>(null)
  const toggleRef = useRef<HTMLButtonElement>(null)
  // Read once: the link is followed on arrival, not on every re-render.
  const [reveal] = useState(revealOnMount)
  useEffect(() => {
    if (!reveal) return
    sectionRef.current?.scrollIntoView?.({ block: 'start' })
    toggleRef.current?.focus({ preventScroll: true })
  }, [reveal])
  const Chevron = open ? ChevronDown : ChevronRight
  return (
    <section
      ref={sectionRef}
      id={sectionId}
      className="mb-5 overflow-hidden rounded-card border border-border bg-surface"
    >
      <button
        ref={toggleRef}
        type="button"
        id={toggleId}
        aria-expanded={open}
        onClick={() => setOpen(current => !current)}
        className="flex w-full items-start gap-3 px-4 py-4 text-left transition-colors hover:bg-[var(--surface-hover)]"
      >
        <div className="min-w-0 flex-1">
          <h3 className="m-0 text-body-sm font-semibold text-fg">
            {title}
          </h3>
          <p className="mt-1 text-body-sm leading-relaxed text-fg-tertiary">
            {explanation}
          </p>
        </div>
        <Chevron className="mt-0.5 size-4 shrink-0" style={{ color: 'var(--fg-subtle)' }} aria-hidden="true" />
      </button>
      {open && (
        <div className="border-t border-border-subtle">
          {/* `disabled` on a fieldset reaches every native control inside it;
              `contents` keeps it out of the layout. */}
          <fieldset disabled={readOnly} className="contents">
            {children}
          </fieldset>
        </div>
      )}
    </section>
  )
}

/**
 * Why the numeric input above cannot be saved: the shared inline message
 * (AU-4). The input points at it with `aria-describedby` via `invalidAria`, so
 * it is read with the field rather than only seen.
 */

const NAMING_TOGGLE_ID = 'scan-naming-toggle'

/**
 * Open "Event names and grouping" and put the reader on the control the dry
 * run's flood warning named: the fix sat in a collapsed section below, and
 * nothing pointed at it (#247 DA-1).
 */
function openNamingControl(target: NamingFixTarget) {
  const toggle = document.getElementById(NAMING_TOGGLE_ID)
  if (toggle?.getAttribute('aria-expanded') === 'false') toggle.click()
  requestAnimationFrame(() => {
    const control = target === 'format'
      ? document.getElementById('scan-event-name-format')
      : document.getElementById('scan-event-groups')
    control?.scrollIntoView({ block: 'center' })
    control?.focus()
  })
}

function PreviewGate() {
  return (
    <p className="text-body-sm text-fg-tertiary">
      {PREVIEW_GATE_TEXT}
    </p>
  )
}

/**
 * Everything a scan cannot be created without, always visible: what the scan
 * does, where it reads from, the time column that bounds every run, and — in
 * Catalog + monitoring only — the schedule that, together with that column,
 * decides whether the dispatcher ever picks the config up.
 */
export function ScanEssentialsSection({
  form,
  slug,
  branchId,
  dataSources,
  eventTypes,
  sourceLocked,
  readOnly = false,
}: SectionProps) {
  const {
    state, set, preview, dryRun, dryRunStale,
    setBaseQuery, setDataSourceId, setEventTypeColumn, setTimeColumn, setInterval, setSetupPreset,
    previewMut, dryRunMut, loadPreview, runDryRun,
  } = form
  const monitoring = state.mode === 'monitoring'
  const preset = state.setupPreset === 'event_properties'
  /**
   * Whether the monitoring pair is the FIRST thing standing between this draft
   * and a saved scan — the gate on both warnings below.
   *
   * A new scan opens on Catalog + monitoring with neither field set, so both
   * used to render on first paint: two red blocks on a form nobody had typed
   * into yet, the first of them pointing at a select the form had not enabled
   * (its options are the preview's columns). People stop reading a warning that
   * fires on the ordinary path, and these are the two that later mean "this scan
   * will never collect anything".
   *
   * `scanFormBlocker` already orders the requirements the way the form asks for
   * them, so reusing it costs nothing and cannot drift from the disabled Create
   * button's own reason. It also keeps the case that matters loud: a saved
   * never-dispatched config has its name, source, query and naming answered
   * already, so its edit form flags the missing time column immediately.
   */
  const monitoringPairIsNext = scanFormBlocker(state) === MONITORING_INCOMPLETE_TITLE
  // Kept on screen when it already holds a value even under an explicit event
  // type, so a saved config's column is never both invisible and unremovable.
  const namesEventsFromColumn = !state.eventTypeId || Boolean(state.eventTypeColumn)
  const selectedSource = dataSources.find(ds => ds.id === state.dataSourceId)
  const sourceName = selectedSource?.name ?? ''
  const { data: schemaData } = useDataSourceSchema(readOnly ? undefined : state.dataSourceId || undefined)

  // A saved config opens its edit form before any preview has been loaded, so
  // the column list is empty and a <select> whose value matches no option shows
  // its placeholder instead. That made an existing time column read as "not
  // set" on the one screen a user checks — so the saved value is always among
  // the choices, whether or not a preview has named it.
  const previewColumns = preview?.columns.map(column => column.name) ?? []
  const withSaved = (saved: string) =>
    saved && !previewColumns.includes(saved) ? [saved, ...previewColumns] : previewColumns
  const timeColumnChoices = withSaved(state.timeColumn)
  const eventTypeColumnChoices = withSaved(state.eventTypeColumn)

  return (
    // A real title: the card used to open with an empty one, and the mode
    // legend sat in the fieldset's border slot, flush with the card's top edge.
    // Floated, the legend lays out inside the padding like any label (#247 DA-12).
    <SCard title="Source and schedule">
      <fieldset
        data-testid="scan-mode"
        className="border-b px-4 py-4 border-border-subtle"
      >
        <legend className="float-left mb-2 w-full text-body font-medium text-fg">
          What this scan does
        </legend>
        <div className="clear-both flex flex-col gap-2">
          {MODE_OPTIONS.map(option => (
            <div
              key={option.value}
              className="flex items-start gap-2.5 rounded-lg border p-3"
              style={{
                borderColor: state.mode === option.value ? 'var(--accent)' : 'var(--border-subtle)',
              }}
            >
              <input
                type="radio"
                id={`scan-mode-${option.value}`}
                name="scan-mode"
                className="mt-0.5"
                value={option.value}
                checked={state.mode === option.value}
                aria-describedby={`scan-mode-${option.value}-description`}
                onChange={() => set('mode', option.value)}
              />
              <div className="min-w-0">
                <label
                  htmlFor={`scan-mode-${option.value}`}
                  className="block text-body font-medium text-fg"
                >
                  {option.label}
                </label>
                <p
                  id={`scan-mode-${option.value}-description`}
                  className="mt-0.5 text-body-sm leading-snug text-fg-tertiary"
                >
                  {option.description}
                </p>
              </div>
            </div>
          ))}
        </div>
      </fieldset>

      {/* Next sibling of the mode radio: only what its description lacks,
          so Catalog only has none (tripl-3y7z.2, #247 DA-13). */}
      {monitoring && (
        <div className="border-b px-4 pb-4 border-border-subtle">
          <ScanCausalNote variant="form" mode={state.mode} />
        </div>
      )}

      <Field label="Name" htmlFor="scan-name">
        <Input
          id="scan-name"
          value={state.name}
          onChange={e => set('name', e.target.value)}
          placeholder="e.g. Main events scan"
        />
      </Field>
      <Field label="Data source" htmlFor="scan-data-source">
        {sourceLocked ? (
          <Input id="scan-data-source" value={sourceName} disabled className="max-w-[280px]" />
        ) : (
          <NativeSelect
            id="scan-data-source"
            value={state.dataSourceId}
            onChange={setDataSourceId}
            options={[
              { value: '', label: 'Select…' },
              ...dataSources.map(ds => ({ value: ds.id, label: ds.name })),
            ]}
          />
        )}
      </Field>
      {/* id={false}: SqlEditor is a CodeMirror contenteditable, not a labelable
          element — it names itself with ariaLabel below. */}
      <Field label="Base query" htmlFor={false} hint="Used as a subquery. tripl wraps it to scan windows.">
        <LazySqlEditor
          ariaLabel="SQL base query"
          value={state.baseQuery}
          onChange={setBaseQuery}
          placeholder={sqlPlaceholder('The rows to scan, e.g.', 'SELECT * FROM analytics.events')}
          dialect={selectedSource?.db_type}
          tables={schemaData?.tables}
          readOnly={readOnly}
        />
      </Field>
      {/* The button loads the sample rows; the ANSWER it also computes is
          rendered at the foot of this card, after the fields that feed it. */}
      <Field
        label="Preview"
        // No control to point a label at — a button and, on failure, an error.
        htmlFor={false}
        hint="Loads sample rows so the pickers below can offer real columns. What this scan would create is answered underneath them."
      >
        <div className="flex flex-col gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="self-start"
            onClick={loadPreview}
            disabled={previewMut.isPending || !state.dataSourceId || !state.baseQuery.trim()}
          >
            <Play className="size-3" />
            {previewMut.isPending ? 'Loading…' : preview ? 'Reload preview' : 'Load preview'}
          </Button>
          {previewMut.isError && <ErrorState compact title="Preview failed" error={previewMut.error} />}
        </div>
      </Field>

      {/* How the table lays events out, before the naming questions: the
          Event + properties preset asks two columns instead of all of them. */}
      <SetupPresetChoice value={state.setupPreset} onChange={setSetupPreset} />
      {preset && (
        <PresetColumnFields
          preview={preview}
          eventTypes={eventTypes}
          eventNameColumn={state.eventNameColumn}
          propertiesColumn={state.propertiesColumn}
          eventTypeId={state.eventTypeId}
          onEventNameColumnChange={value => set('eventNameColumn', value)}
          onPropertiesColumnChange={value => set('propertiesColumn', value)}
          onEventTypeIdChange={value => set('eventTypeId', value)}
        />
      )}

      {/* "Where does the event name come from?" is ONE question, so it is asked
          in one place. It used to be split: an "Auto-detect" default here, and
          the column auto-detect actually reads hidden in a collapsed section
          whose header said to leave it alone. Nothing detected anything — with
          neither set, `run_scan` and the dry-run planner both abort, so every
          scan created on the defaults failed its first run. "Auto-detect" is
          gone with it; the empty option now names the answer it stands for. */}
      {!preset && (
        <Field
          label="Event type"
          htmlFor="scan-event-type"
          hint="Give every row the same event type, or read each event's name from a column."
        >
          <NativeSelect
            id="scan-event-type"
            value={state.eventTypeId}
            onChange={value => set('eventTypeId', value)}
            options={[
              { value: '', label: 'Name events from a column' },
              ...eventTypes.map(et => ({ value: et.id, label: et.display_name })),
            ]}
          />
        </Field>
      )}
      {!preset && namesEventsFromColumn && (
        <Field
          label="Event type column"
          htmlFor="scan-event-type-column"
          hint={
            state.eventTypeId
              ? 'The Event type above names these events, so this column names nothing — and it never becomes an event field either. Clear it if you did not mean to set it.'
              : "The column each row's event name is in. Every distinct value becomes its own event type."
          }
        >
          {/* The empty option is selectable only when an Event type carries
              the naming instead — otherwise clearing it would leave the scan
              unable to name a thing, which is the state this field exists
              to prevent. */}
          <NativeSelect
            id="scan-event-type-column"
            value={state.eventTypeColumn}
            onChange={setEventTypeColumn}
            disabled={!preview}
            {...invalidAria(
              'scan-event-type-column',
              preview && !state.eventTypeColumn && !state.eventTypeId,
            )}
            options={[
              {
                value: '',
                label: preview ? (state.eventTypeId ? 'None' : 'Choose a column') : 'Load preview first',
                disabled: !state.eventTypeId,
              },
              ...eventTypeColumnChoices,
            ]}
          />
          {/* Only once the column list exists: before that the select is
              disabled and the user is being flagged for not doing something the
              form has not let them do yet. The disabled Create button carries
              the gate in the meantime. */}
          {/* Red: it blocks Create (AU-5). */}
          <FieldError
            inputId="scan-event-type-column"
            announce
            message={
              preview && !state.eventTypeColumn && !state.eventTypeId
                ? 'Pick the column your event names are in — without it this scan cannot name a single event, and every run fails.'
                : null
            }
          />
        </Field>
      )}
      {/* The time column is asked for in BOTH modes, because it does two jobs and
          only one of them is monitoring: it buckets metric points, and it bounds
          every run to Limits → Lookback (hours). Catalog only drops the SCHEDULE
          — that alone is what makes a config catalog-only, since the dispatcher
          selects on `time_column IS NOT NULL AND interval IS NOT NULL`. Hiding
          this field in Catalog only is what let an unrelated edit null a saved
          column and hand the next run an unbounded full-table read. In Catalog
          only it is optional and says what leaving it empty costs; there is no
          warning, because empty is a legitimate answer there. */}
      <Field
        label="Time column"
        htmlFor="scan-time-column"
        hint={
          monitoring
            ? 'The timestamp tripl buckets metric points by. Required for monitoring.'
            : 'Optional. Bounds each run to the lookback window under Limits — without one, every run reads everything the base query returns.'
        }
        last={!monitoring && !preview}
      >
        {/* Monitoring cannot proceed on the empty option, so it stays a
            disabled placeholder there; Catalog only needs it selectable, or a
            time column could be set and never removed again. */}
        <NativeSelect
          id="scan-time-column"
          value={state.timeColumn}
          onChange={setTimeColumn}
          disabled={!preview}
          {...invalidAria('scan-time-column', monitoringPairIsNext && !state.timeColumn)}
          options={[
            {
              value: '',
              label: preview
                ? monitoring
                  ? 'Choose a time column'
                  : 'No time column — read the whole query'
                : 'Load preview first',
              disabled: monitoring,
            },
            ...timeColumnChoices,
          ]}
        />
        {monitoringPairIsNext && !state.timeColumn && (
          <p
            id="scan-time-column-error"
            role="alert"
            data-slot="field-error"
            className="mt-1.5 text-body-sm leading-[1.45] text-danger"
          >
            {/* The select above is `disabled={!preview}` and reads "Load preview
                first", so "Pick a time column" pointed at a control the reader
                cannot use. An explicit Event type satisfies the blocker without
                a preview, and a saved never-dispatched config opens with none —
                both reach this branch. Name the step that IS available instead
                of dropping the warning, which would let a saved broken config
                open quiet. */}
            {preview
              ? 'Pick a time column — monitoring buckets metric points by it.'
              : 'Load a preview to choose a time column — monitoring needs one.'}
          </p>
        )}
      </Field>
      {monitoring && (
        <Field label="Schedule" htmlFor="scan-interval" hint="How often this scan runs." last={!preview}>
          <NativeSelect
            id="scan-interval"
            value={state.interval}
            onChange={setInterval}
            {...invalidAria('scan-interval', monitoringPairIsNext && !state.interval)}
            options={[{ value: '', label: 'Choose a schedule', disabled: true }, ...INTERVAL_OPTIONS]}
          />
          <FieldError
            inputId="scan-interval"
            announce
            message={
              monitoringPairIsNext && !state.interval
                ? 'Pick a schedule — monitoring needs one to record metric points.'
                : null
            }
          />
        </Field>
      )}

      {/* Last in the card, because it is the only thing here that is an ANSWER
          rather than a question — and because a dry run describes one specific
          draft. `toDryRunRequest` carries the time column (it is what the scan
          window is computed from), so with this panel above the Time column
          field, the sequence the form itself demands was: check → scroll down →
          pick the time column the form is asking for in red → and the answer
          just given turns to "The form changed since this check ran". Every
          monitoring scan, every time, until the user learns to ignore the one
          banner that also catches a genuinely broken event name format.
          Everything the request is built from that is still below this point
          lives in a collapsed section, where an edit is deliberate and staling
          the answer is the banner doing its job. */}
      {preview && (
        <div data-testid="scan-preview-panel" className="space-y-3 px-4 py-4">
          <ScanPreviewPanel
            preview={preview}
            dryRun={dryRun}
            dryRunStale={dryRunStale}
            dryRunPending={dryRunMut.isPending}
            dryRunError={dryRunMut.isError ? dryRunMut.error : null}
            eventTargetMissing={!hasEventTarget(state)}
            onRecheck={runDryRun}
            onFixNaming={openNamingControl}
            slug={slug}
          />
          {/* Directly under the answer it acts on, and driven by the same
              `unmapped_columns` the panel just listed — it used to sit hundreds
              of pixels higher, computing its own reserved set, so the two could
              and did disagree about the same columns on the same screen. Hidden
              while the answer is stale: those column names belong to the draft
              the dry run ran on, which is no longer this one. */}
          {/* The preset creates its own two fields when it runs. */}
          {dryRun && !dryRunStale && state.eventTypeId && !preset && (
            <CreateMissingFieldsButton
              slug={slug}
              eventType={eventTypes.find(et => et.id === state.eventTypeId)}
              preview={preview}
              unmappedColumns={dryRun.unmapped_columns}
              branchId={branchId}
              // Re-ask so the list names only what is still unmapped (DATA-27).
              onCreated={runDryRun}
            />
          )}
        </div>
      )}
    </SCard>
  )
}

export function EventNamingSection({ form, readOnly }: SectionProps) {
  const {
    state, set, preview, fieldErrors,
    toggleJsonValuePath, discoverJsonMut, discoverJsonPaths,
  } = form
  // Event + properties derives every field here from its two columns.
  if (state.setupPreset === 'event_properties') return null
  // "Event type column" is no longer here — it is half of the essentials'
  // "where does the event name come from?" question, and burying the field a
  // scan cannot run without behind a header saying to leave it alone is what
  // made every default scan fail. What is left is genuinely optional, so the
  // header can promise that leaving it alone works and be telling the truth.
  const defaultOpen = Boolean(
    state.eventNameFormat
    || state.cardinalityThreshold !== '100'
    || state.eventGroupRules.length
    || state.jsonValuePaths.length,
  )

  return (
    <CollapsibleSection
      readOnly={readOnly}
      title="Event names and grouping"
      toggleId={NAMING_TOGGLE_ID}
      explanation="Reshape the names tripl derives above — rewrite them from a template, collapse high-cardinality values, or merge several into one. Leave this alone and each name is used as it is."
      defaultOpen={defaultOpen}
    >
      <Field label="Event name format" htmlFor="scan-event-name-format" hint="Template, e.g. {action}:{category}.">
        <Input
          id="scan-event-name-format"
          value={state.eventNameFormat}
          onChange={e => set('eventNameFormat', e.target.value)}
          className="font-mono max-w-[280px]"
          placeholder="e.g. {action}"
        />
      </Field>
      <Field
        label="Cardinality threshold"
        htmlFor="cardinality-threshold"
        hint="Columns with more distinct values than this are collapsed into a template like {country} instead of one event per value."
        last
      >
        <Input
          id="cardinality-threshold"
          type="number"
          min={1}
          value={state.cardinalityThreshold}
          onChange={e => set('cardinalityThreshold', e.target.value)}
          className="font-mono max-w-[280px]"
          {...invalidAria('cardinality-threshold', fieldErrors.cardinalityThreshold)}
        />
        <FieldError id="cardinality-threshold-error" message={fieldErrors.cardinalityThreshold} />
      </Field>
      <div className="space-y-4 border-t px-4 py-4 border-border-subtle">
        <EventGroupRulesEditor
          rules={state.eventGroupRules}
          columns={preview?.columns}
          onChange={rules => set('eventGroupRules', rules)}
        />
        {preview ? (
          <JsonValuePathsPicker
            preview={preview}
            selectedJsonValuePaths={state.jsonValuePaths}
            onToggleJsonValuePath={toggleJsonValuePath}
            onDiscoverJsonPaths={discoverJsonPaths}
            isDiscoveringJsonPaths={discoverJsonMut.isPending}
            jsonPathsError={discoverJsonMut.error}
            jsonPathsDiscovered={discoverJsonMut.isSuccess}
          />
        ) : (
          <PreviewGate />
        )}
      </div>
    </CollapsibleSection>
  )
}

export function AppVersionSection({ form, readOnly }: SectionProps) {
  const { state, setAppVersionColumn, setPlatformColumn, set, preview, fieldErrors } = form
  const defaultOpen = Boolean(
    state.appVersionColumn
    || state.platformColumn
    || state.appVersionPrereleasePattern
    || state.appVersionActiveShareMin,
  )

  return (
    <CollapsibleSection
      readOnly={readOnly}
      title="App version"
      explanation="Attach an app release and platform to every event. Leave this alone if you do not ship versioned apps."
      defaultOpen={defaultOpen}
    >
      <AppVersionFields
        activeShareMinError={fieldErrors.appVersionActiveShareMin}
        columns={preview?.columns ?? null}
        appVersionColumn={state.appVersionColumn}
        prereleasePattern={state.appVersionPrereleasePattern}
        activeShareMin={state.appVersionActiveShareMin}
        platformColumn={state.platformColumn}
        onAppVersionColumnChange={setAppVersionColumn}
        onPrereleasePatternChange={value => set('appVersionPrereleasePattern', value)}
        onActiveShareMinChange={value => set('appVersionActiveShareMin', value)}
        onPlatformColumnChange={setPlatformColumn}
      />
    </CollapsibleSection>
  )
}

/** Rendered only in Catalog + monitoring — there are no metrics to break down otherwise. */
export function MetricsDriftSection({ form, readOnly }: SectionProps) {
  const {
    state, set, preview, fieldErrors,
    toggleMetricBreakdownColumn, toggleDistributionDriftField,
  } = form
  if (state.mode !== 'monitoring') return null

  const defaultOpen = Boolean(
    state.metricBreakdownColumns.length
    || state.distributionDriftFields.length
    || state.metricBreakdownValuesLimit,
  )

  return (
    <CollapsibleSection
      readOnly={readOnly}
      title="Metric breakdowns and drift"
      explanation="Extra columns to split metrics by, and columns whose value mix you want watched for drift. Leave this alone to collect one series per event."
      defaultOpen={defaultOpen}
    >
      {/* Field rows like the essentials card and App version, so the section
          reads down the same label column instead of opening two bordered
          cards with their own stacked headings (#247 DA-12). The pickers hold
          no single labelable control, so their rows name them as groups. */}
      {preview ? (
        <>
          <Field
            label="Metric breakdowns"
            htmlFor={false}
            hint="Each selected column gets its own series per value (e.g. one per platform), grouped in the warehouse."
          >
            <MetricBreakdownPicker
              columns={preview.columns}
              selectedColumns={state.metricBreakdownColumns}
              eventTypeColumn={state.eventTypeColumn}
              timeColumn={state.timeColumn}
              appVersionColumn={state.appVersionColumn}
              platformColumn={state.platformColumn}
              onToggleColumn={toggleMetricBreakdownColumn}
            />
          </Field>
          {/* The label matches SCAN_NUMERIC_FIELD_LABEL, which the blocked-save
              summary names this field by. */}
          <Field
            label="Value limit"
            htmlFor="breakdown-value-limit"
            hint="Keeps each breakdown's top values and aggregates the rest into Other. Empty means unlimited."
          >
            <Input
              id="breakdown-value-limit"
              type="number"
              min={1}
              value={state.metricBreakdownValuesLimit}
              onChange={e => set('metricBreakdownValuesLimit', e.target.value)}
              className="font-mono max-w-[280px]"
              placeholder="Unlimited"
              {...invalidAria('breakdown-value-limit', fieldErrors.metricBreakdownValuesLimit)}
            />
            <FieldError inputId="breakdown-value-limit" message={fieldErrors.metricBreakdownValuesLimit} />
            {state.metricBreakdownColumns.length > 0 && !state.metricBreakdownValuesLimit && (
              <p className="mt-2 rounded-md border border-warning/40 bg-warning-soft px-3 py-2 text-body-sm text-warning">
                Unlimited breakdowns can be expensive for high-cardinality columns. Set a limit to keep top values and aggregate the rest into Other.
              </p>
            )}
          </Field>
          {/* Plain words first; "PSI" and "scalar" explained nothing to the
              reader choosing columns (#247 DA-16). */}
          <Field
            label="Distribution drift"
            htmlFor={false}
            hint="tripl watches the mix of values in these columns (e.g. the share of iOS vs Android) and flags when it shifts from the usual pattern (population stability index)."
            last
          >
            <DistributionDriftPicker
              columns={preview.columns}
              selectedFields={state.distributionDriftFields}
              eventTypeColumn={state.eventTypeColumn}
              timeColumn={state.timeColumn}
              appVersionColumn={state.appVersionColumn}
              platformColumn={state.platformColumn}
              onToggleField={toggleDistributionDriftField}
            />
          </Field>
        </>
      ) : (
        <div className="px-4 py-4">
          <PreviewGate />
        </div>
      )}
    </CollapsibleSection>
  )
}

export function LimitsSection({ form, readOnly }: SectionProps) {
  const { state, set, fieldErrors } = form
  // The instance's real caps for the hints, else the shipped ones (B15).
  const rowLimitDefaults = useRowLimitDefaults()
  const monitoring = state.mode === 'monitoring'
  // A create-page lookback of "24" is this form's own default, not a user choice,
  // so it must not spring the section open on every edit of a fresh config.
  const lookbackIsCustom = state.scanLookbackHours !== '' && state.scanLookbackHours !== '24'
  // Only count the two monitoring-only fields when they are on screen: a saved
  // chunk interval or metrics cap survives a switch to Catalog only (by design —
  // see toBackendPayload), and springing the section open to show neither of them
  // is a section that opens on nothing.
  const defaultOpen = Boolean(
    lookbackIsCustom
    || state.scanRowLimit
    || (monitoring && (state.chunkInterval || state.metricsRowLimit))
    // A value the save refuses must be on screen, or the blocker names a field
    // behind a chevron.
    || fieldErrors.scanLookbackHours
    || fieldErrors.scanRowLimit
    || fieldErrors.metricsRowLimit,
  )
  // A failed run's "Open Limits" link names this section (F15). The form also
  // renders outside a router (its tests, the edit dialog), so the hash comes
  // from the address bar rather than useLocation, which would throw there.
  const [linkedHere] = useState(
    () => typeof window !== 'undefined' && window.location.hash === `#${LIMITS_SECTION_ID}`,
  )

  return (
    <CollapsibleSection
      readOnly={readOnly}
      title="Limits"
      explanation="Caps on how much warehouse data each run reads. Leave these alone unless runs are slow or expensive."
      defaultOpen={defaultOpen || linkedHere}
      sectionId={LIMITS_SECTION_ID}
      revealOnMount={linkedHere}
    >
      {monitoring && state.interval && (
        <Field
          label="Replay chunk size"
          htmlFor="scan-chunk-interval"
          hint="Splits long replays into smaller warehouse queries. Must be at least as long as the schedule."
        >
          <NativeSelect
            id="scan-chunk-interval"
            value={state.chunkInterval}
            onChange={value => set('chunkInterval', value)}
            options={[
              { value: '', label: 'Whole window (no split)' },
              ...eligibleChunkIntervals(state.interval as IntervalCode).map(code => ({
                value: code,
                label: CHUNK_LABELS[code],
              })),
            ]}
          />
        </Field>
      )}
      {/* A lookback is the predicate `<time column> >= now() - N hours`, so with
          no time column there is no window to set: resolve_lookback_window
          returns None and the run reads everything the base query returns.
          Offering the input anyway would be a control that states a bound and
          applies none, so the field is replaced by the one sentence that says
          what is actually happening and where to change it. */}
      {state.timeColumn ? (
        <Field
          label="Lookback (hours)"
          htmlFor="scan-lookback-hours"
          hint={`How far back each run reads, counted on ${state.timeColumn}. Empty means the 24-hour default.`}
        >
          <Input
            id="scan-lookback-hours"
            type="number"
            min={1}
            value={state.scanLookbackHours}
            onChange={e => set('scanLookbackHours', e.target.value)}
            className="font-mono max-w-[280px]"
            placeholder="24"
            {...invalidAria('scan-lookback-hours', fieldErrors.scanLookbackHours)}
          />
          <FieldError id="scan-lookback-hours-error" message={fieldErrors.scanLookbackHours} />
        </Field>
      ) : (
        /* id={false}: this branch replaces the input with a sentence, so there is
           nothing here for a `<label htmlFor>` to point at (tripl-6h2b). */
        <Field label="Lookback (hours)" htmlFor={false}>
          <p className="text-body-sm text-fg-tertiary">
            {NO_LOOKBACK_WITHOUT_TIME_COLUMN}
          </p>
        </Field>
      )}
      <Field label="Row cap per run" htmlFor="scan-row-limit" hint={rowCapHint('catalog', rowLimitDefaults)} last={!monitoring}>
        <Input
          id="scan-row-limit"
          type="number"
          min={1}
          value={state.scanRowLimit}
          onChange={e => set('scanRowLimit', e.target.value)}
          className="font-mono max-w-[280px]"
          placeholder="Instance default"
          {...invalidAria('scan-row-limit', fieldErrors.scanRowLimit)}
        />
        <FieldError id="scan-row-limit-error" message={fieldErrors.scanRowLimit} />
      </Field>
      {/* A metrics run is `collect_metrics`, which the scheduler dispatches only
          for a config with both a schedule and a time column, so in Catalog only
          there are no metrics runs to cap — the same reason the breakdown section
          above is hidden outright rather than asked and ignored. Hidden, not
          cleared: `toBackendPayload` still sends `metrics_row_limit`, so a cap
          saved while monitoring survives a switch to Catalog only and comes back
          the moment monitoring does. */}
      {monitoring && (
        <Field label="Row cap per metrics run" htmlFor="scan-metrics-row-limit" hint={rowCapHint('metrics', rowLimitDefaults)} last>
          <Input
            id="scan-metrics-row-limit"
            type="number"
            min={1}
            value={state.metricsRowLimit}
            onChange={e => set('metricsRowLimit', e.target.value)}
            className="font-mono max-w-[280px]"
            placeholder="Instance default"
            {...invalidAria('scan-metrics-row-limit', fieldErrors.metricsRowLimit)}
          />
          <FieldError id="scan-metrics-row-limit-error" message={fieldErrors.metricsRowLimit} />
        </Field>
      )}
    </CollapsibleSection>
  )
}
