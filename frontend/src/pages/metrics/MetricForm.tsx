import { useMemo, useState } from 'react'
import { Link, Navigate, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  Activity,
  AlertTriangle,
  ChevronLeft,
  ChevronRight,
  Code2,
  Loader2,
  Plus,
  Save,
  Table2,
} from 'lucide-react'
import { dataSourcesApi } from '@/api/dataSources'
import { usersApi } from '@/api/users'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { ErrorState } from '@/components/error-state'
import { PageSkeleton, QueryErrorState, ReadOnlyNotice } from '@/components/states'
import { PageHeader } from '@/components/primitives/page-header'
import { PageContainer } from '@/components/primitives/page-container'
import { SaveBar } from '@/components/forms/SaveBar'
import { examplePlaceholder } from '@/components/forms/placeholders'
import { attentionSummary } from '@/components/forms/validation'
import { Button } from '@/components/ui/button'
import {
  RadioCards,
  SCard,
  NativeSelect,
  TextArea,
  TextInput,
  Field,
  type RadioCardOption,
} from '@/components/settings/kit'
import { useConfirm } from '@/hooks/useConfirm'
import { useDataSourceSchema } from '@/hooks/useDataSourceSchema'
import { useUnsavedChangesGuard } from '@/hooks/useUnsavedChangesGuard'
import { editPageTitle, usePageTitle } from '@/components/shell-chrome-context'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { isIntervalFinerThan } from '@/lib/metricFormat'
import { getMetricMonitoringPath } from '@/lib/monitoring'
import { useCanWriteProject } from '@/lib/permissions'
import {
  activeSignalsKey,
  dataSourcesKey,
  metricDefinitionKey,
  metricDrilldownKeys,
  metricGeneratedSqlKey,
  metricsCatalogKey,
  usersKey,
} from '@/lib/queryKeys'
import {
  METRIC_STATUSES,
  METRIC_STATUS_LABEL,
  type DataSource,
  type MetricDefinitionDetailResponse,
  type MetricDefinitionListResponse,
  type MetricKind,
  type MetricScanInterval,
  type MetricStatus,
} from '@/types'
import { EventCompositionFields } from './EventCompositionFields'
import { FactDefinitionFields } from './FactDefinitionFields'
import { MonitoringFields } from './MonitoringFields'
import { SqlDefinitionFields } from './SqlDefinitionFields'
import { TemplateGallery } from './TemplateGallery'
import { ColorSwatches } from './ColorSwatches'
import { SeriesPreviewCard } from './SeriesPreviewCard'
import { eventRosterQuery } from './eventRoster'
import { errorAria, focusField } from '@/lib/fieldErrors'
import {
  NEW_METRIC_KIND,
  columnsOfReferencedTables,
  draftFromMetric,
  nextMetricColor,
  savedDimensions,
  toIdentifier,
  validateDraft,
  type FactComposition,
  type MetricDraft,
} from './metricDraft'

type KindDimensions = Pick<MetricDraft, 'breakdownColumns' | 'appVersionColumn' | 'platformColumn'>
import { definitionDiffersFromStored } from './definitionChange'
import {
  buildCreatePayload,
  buildDefinitionPayload,
  buildUpdatePayload,
  type OperandColumns,
} from './metricPayload'
import {
  isPristineStarterSql,
  starterSql,
  type MetricTemplate,
  type SqlTemplateId,
} from './metricTemplates'
import { useFactTableDetails } from './useFactTableDetails'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

// What each kind measures and what it needs, in the words of the person
// choosing, most approachable first (MT-3). The stored kind names stay in the
// catalog's chips and filters. A kind the project cannot complete yet says so
// on its card, before it is picked (MT-3, MT-11).
function kindOptions({
  noEvents,
  noFactTables,
}: {
  noEvents: boolean
  noFactTables: boolean
}): (RadioCardOption & { value: MetricKind })[] {
  return [
    {
      value: 'event_composition',
      label: 'From tracked events',
      description: noEvents
        ? 'Count an event, or divide one event by another. No events are tracked in this project yet.'
        : 'Count an event, or divide one event by another (e.g. checkout conversion). No SQL needed.',
      dimmed: noEvents,
      icon: <Activity size={14} aria-hidden="true" className="text-fg-secondary" />,
    },
    {
      value: 'fact',
      label: 'From a fact table',
      description: noFactTables
        ? 'Sum, average or count rows of a reusable warehouse table. This project has no fact tables yet.'
        : 'Sum, average or count rows of a reusable warehouse table (e.g. revenue). Needs a fact table.',
      dimmed: noFactTables,
      icon: <Table2 size={14} aria-hidden="true" className="text-fg-secondary" />,
    },
    {
      value: 'sql',
      label: 'Custom SQL',
      description: 'Write a query that returns a time column and a value. For analysts.',
      icon: <Code2 size={14} aria-hidden="true" className="text-fg-secondary" />,
    },
  ]
}

/** Units offered as one-click chips beside the Unit input (MT-17). */
const UNIT_SUGGESTIONS = ['%', 'ms', '$', 'users'] as const

/** Why a draft or archived metric shows no data: said under Status (MT-1). */
const STATUS_HINT =
  'Only active metrics are collected on schedule and monitored. Draft and archived metrics are not.'

/** Every catalog row cached for `slug`, across the filtered pages. */
function cachedCatalogItems(
  qc: ReturnType<typeof useQueryClient>,
  slug: string,
): MetricDefinitionListResponse['items'] {
  return qc
    .getQueriesData<MetricDefinitionListResponse>({ queryKey: metricsCatalogKey(slug) })
    .flatMap(([, data]) => (data && Array.isArray(data.items) ? data.items : []))
}

/** What the history-loss confirm and the inline notice both say. */
const DEFINITION_CHANGE_MESSAGE =
  "This changes what the metric measures. Saving deletes its collected values, breakdowns and anomalies, and collection starts over from scratch."

/**
 * Shown on an edit once the definition differs from the saved one, so the
 * consequence is visible before Save rather than only in the confirm.
 */
function DefinitionChangeNotice() {
  return (
    <div
      role="status"
      className="mb-[18px] flex items-start gap-2 rounded-card border px-4 py-3 text-body-sm text-fg"
      style={{
        background: 'var(--warning-soft, var(--bg-sunken))',
        borderColor: 'color-mix(in oklab, var(--warning, var(--border)) 40%, var(--border))',
      }}
    >
      <AlertTriangle size={14} className="mt-px shrink-0" aria-hidden="true" style={{ color: 'var(--warning, var(--fg-muted))' }} />
      <span>{DEFINITION_CHANGE_MESSAGE}</span>
    </div>
  )
}

interface MetricFormProps {
  slug: string
  metric: MetricDefinitionDetailResponse | null
  dataSources: DataSource[]
  /** The data-source list failed; only a SQL metric needs it, so it is shown there. */
  dataSourcesError?: unknown
  onRetryDataSources?: () => void
  /** Cancel / back. */
  onClose: () => void
  /**
   * The header's "Metrics" link; defaults to {@link onClose}. The route sends
   * it to the catalog it names, while Cancel returns to wherever the editor
   * was opened from (MT-31).
   */
  onBack?: () => void
  /** After a successful save; defaults to {@link onClose}. */
  onSaved?: (metricId: string, created: boolean) => void
}

/**
 * Create / edit a catalog metric. The internal name remains immutable after
 * creation, while kind + collection config can be redefined through the
 * update `definition` block. Client-side validation mirrors the backend
 * discriminated-union rules; server 422s surface through {@link ErrorState}.
 *
 * All form state is one {@link MetricDraft}; the payload, validation and
 * change detection are pure functions of it (metricDraft.ts, metricPayload.ts),
 * and each kind renders its own section component.
 */
export function MetricForm({
  slug,
  metric,
  dataSources,
  dataSourcesError,
  onRetryDataSources,
  onClose,
  onBack,
  onSaved,
}: MetricFormProps) {
  const qc = useQueryClient()
  // Create, update and preview are all EditorUserDep (MET-6).
  const canWrite = useCanWriteProject()
  const isNew = !metric

  // A new metric takes the first colour no cached catalog metric uses (MT-35).
  const [draft, setDraft] = useState<MetricDraft>(() =>
    draftFromMetric(
      metric,
      metric ? undefined : nextMetricColor(cachedCatalogItems(qc, slug).map(item => item.color)),
    ),
  )
  const patch = (next: Partial<MetricDraft>) => setDraft(current => ({ ...current, ...next }))

  // Tracks whether the user has typed the internal name directly; once they
  // have, we stop auto-deriving it from the display name.
  const [nameEdited, setNameEdited] = useState(false)
  // Stands in for a display name with no Latin letters to derive from; minted
  // once so it does not churn while the user types.
  const [fallbackName] = useState(() => `metric_${Date.now().toString(36)}`)
  // Which starter scaffold seeded the SQL, if any. Non-null only while the SQL is
  // still template output; it is what lets a data-source switch re-render the
  // query for the newly-selected warehouse.
  const [sqlTemplateId, setSqlTemplateId] = useState<SqlTemplateId | null>(null)
  // Columns the last clean SQL preview returned — what the query projects.
  const [previewColumns, setPreviewColumns] = useState<string[] | null>(null)
  // Errors are shown only once a submit was attempted, then re-derived from the
  // draft on every render: fixing a field clears its message, and a field that
  // stops rendering takes its message with it (MET-18).
  const [submitAttempted, setSubmitAttempted] = useState(false)
  const { confirm, dialog: confirmDialog } = useConfirm()

  // Create-only starter gallery: shown pristine above the form; picking a
  // template or "Start from scratch" dismisses it. Never shown when editing.
  const [showTemplates, setShowTemplates] = useState(isNew)
  // The template that seeded the form, named in a slim banner that reopens the
  // gallery, so the choice is neither invisible nor final (MT-32).
  const [pickedTemplate, setPickedTemplate] = useState<MetricTemplate | null>(null)
  // Set when the Unit was filled in for the author because the metric became
  // a ratio, so the row can say why it changed (MT-17).
  const [unitAutoSet, setUnitAutoSet] = useState(false)
  // The last SQL preview failed and nothing it ran against has changed since:
  // creating from here asks first (MT-15).
  const [sqlPreviewFailed, setSqlPreviewFailed] = useState(false)

  const facts = useFactTableDetails(slug, draft, dataSources, { loadList: isNew })
  // The unfiltered first page of the event picker, shared with it by key: a
  // new metric's kind step says when the project has no events (MT-3).
  const eventRoster = useQuery({ ...eventRosterQuery(slug, ''), enabled: isNew })
  const noEvents = eventRoster.isSuccess && eventRoster.data.total === 0

  const [kindChosen, setKindChosen] = useState(!isNew)

  // Description, colour and — on create — the internal name sit behind "More
  // options" (MT-2): the display name derives the internal name, which stays
  // visible as a line under it. An edit whose metric has a description opens
  // with it shown.
  const [moreOpen, setMoreOpen] = useState(!!metric?.description)
  // The top bar names the edited metric after "Metrics", as the heading
  // does (MT-31).
  usePageTitle(metric ? editPageTitle(metric.display_name) : null)
  // Who can own the metric: the workspace roster, readable by any member.
  const usersQuery = useQuery({
    queryKey: usersKey(),
    queryFn: () => usersApi.list(),
    // The picker falls back to "No owner" plus the stored owner.
    meta: SILENT_ERROR_META,
  })
  const ownerOptions = useMemo(() => {
    const users = usersQuery.data ?? []
    const options = [
      { value: '', label: 'No owner' },
      ...users.map(user => ({ value: user.id, label: user.name || user.email })),
    ]
    // A stored owner the roster does not list (not loaded, or since removed)
    // stays selectable rather than being silently cleared on save.
    if (draft.ownerId && !users.some(user => user.id === draft.ownerId)) {
      options.push({ value: draft.ownerId, label: 'Current owner' })
    }
    return options
  }, [usersQuery.data, draft.ownerId])
  const operandColumns: OperandColumns = {
    numerator: facts.numerator.detail.columns,
    denominator: facts.denominator.detail.columns,
  }

  const selectedDataSource = dataSources.find(ds => ds.id === draft.dataSourceId)
  // The schema route is editor-only, so a read-only visitor would get a 403
  // for autocomplete they cannot use: skip the request instead.
  const { data: sqlSchemaData } = useDataSourceSchema(
    canWrite && draft.kind === 'sql' ? draft.dataSourceId || undefined : undefined,
  )

  // Columns this metric's own source can offer to the breakdown picker and the
  // column inputs: the fact table's for a fact metric; for SQL, what the last
  // preview returned, else the columns of the tables the query names.
  const sqlColumns = useMemo(() => {
    if (previewColumns) return previewColumns
    return columnsOfReferencedTables(sqlSchemaData?.tables ?? [], draft.metricSql)
  }, [previewColumns, sqlSchemaData, draft.metricSql])
  // The metric's own time column is never a breakdown (MT-16).
  const timeColumn =
    draft.kind === 'fact'
      ? facts.numerator.detail.timestampColumn ?? ''
      : draft.kind === 'sql'
        ? draft.sqlTimeColumn.trim()
        : ''
  const columnChoices = (
    draft.kind === 'fact' ? facts.numerator.detail.columns.map(column => column.name) : sqlColumns
  ).filter(column => column !== timeColumn)

  const fieldErrors = useMemo(
    () => (submitAttempted ? validateDraft(draft, isNew) : {}),
    [submitAttempted, draft, isNew],
  )
  // A problem inside the fold opens it: a hidden field can be neither read
  // nor focused.
  const moreShown = moreOpen || (isNew && !!fieldErrors['metric-name'])
  // Every "go to this field" — the submit, the error list, the Save bar —
  // goes through here, so a folded field is revealed before focus moves.
  const goToField = (key: string) => {
    if (key === 'metric-name' && !moreShown) {
      setMoreOpen(true)
      window.setTimeout(() => focusField(key), 0)
      return
    }
    focusField(key)
  }

  // The definition this form would send, against the one stored: the backend
  // deletes the metric's history when they differ (MET-1). Compared with the
  // real payload rather than a draft hydrated from the same metric, so a stored
  // shape the form cannot reproduce warns instead of wiping silently.
  const definitionPayload = buildDefinitionPayload(draft, operandColumns)
  const definitionChanged = metric !== null && definitionDiffersFromStored(metric, definitionPayload)
  // What the series preview dry-runs: the same definition a save sends (MT-9).
  const seriesRequest = definitionPayload.kind === 'sql' ? null : definitionPayload

  // Every input the author can change, as one comparable string; the first
  // render's value is the baseline (MET-5).
  const draftSnapshot = JSON.stringify(draft)
  const [initialSnapshot, setInitialSnapshot] = useState(draftSnapshot)

  // The events kind is the default only where it can be completed (MT-3): a
  // project with no events starts on a fact table if it has one, else on SQL.
  // Only an untouched form moves, and it stays untouched: the switch is the
  // form's starting point, not an edit the leave guard should ask about. Once
  // the author picks a kind or a template, their choice stands.
  const factTablesKnown = facts.noFactTables || facts.factTableOptions.length > 1
  if (
    isNew
    && !kindChosen
    && noEvents
    && factTablesKnown
    && draft.kind === NEW_METRIC_KIND
    && draftSnapshot === initialSnapshot
  ) {
    const startingDraft: MetricDraft = { ...draft, kind: facts.noFactTables ? 'sql' : 'fact' }
    setKindChosen(true)
    setDraft(startingDraft)
    setInitialSnapshot(JSON.stringify(startingDraft))
  }
  // A viewer's form is disabled and so never dirty.
  const unsaved = useUnsavedChangesGuard(canWrite && draftSnapshot !== initialSnapshot)

  const saveMut = useMutation({
    // Rendered inline at the foot of the form ("Could not save …").
    meta: SILENT_ERROR_META,
    // A create says its status through the button pressed: "Create and start
    // collecting" (active) or "Save as draft" (MT-1).
    // The owner rides along with the presentation fields (MT-25).
    mutationFn: (createStatus?: MetricStatus) =>
      metric
        ? metricsCatalogApi.update(slug, metric.id, {
            ...buildUpdatePayload(draft, operandColumns),
            owner_id: draft.ownerId || null,
          })
        : metricsCatalogApi.create(slug, {
            ...buildCreatePayload({ ...draft, status: createStatus ?? draft.status }, operandColumns),
            owner_id: draft.ownerId || null,
          }),
    onSuccess: (saved, createStatus) => {
      unsaved.release()
      void qc.invalidateQueries({ queryKey: metricsCatalogKey(slug) })
      void qc.invalidateQueries({ queryKey: metricGeneratedSqlKey(slug) })
      if (metric) {
        void qc.invalidateQueries({ queryKey: metricDefinitionKey(slug) })
        // A redefinition deleted the series, breakdowns and anomalies server-
        // side; the drilldown must not keep painting the old ones (MET-27).
        for (const key of metricDrilldownKeys(slug, metric.id)) {
          void qc.invalidateQueries({ queryKey: key })
        }
        void qc.invalidateQueries({ queryKey: activeSignalsKey(slug) })
      }
      toast.success(
        !isNew
          ? 'Metric saved.'
          : createStatus === 'active'
            ? 'Metric created. Collection starts on the next scheduled run.'
            : 'Metric saved as a draft. It is not collected until you set it to Active.',
      )
      if (onSaved) onSaved(saved.id, isNew)
      else onClose()
    },
  })

  /**
   * Selecting a warehouse re-renders starter SQL for it.
   *
   * The starter query is dialect-specific — `toStartOfInterval` on ClickHouse,
   * `date_bin` on PostgreSQL, `TIMESTAMP_TRUNC` on BigQuery — so SQL seeded before a
   * source was picked (or picked for a *different* source) simply cannot run on this
   * one. It regenerates ONLY while the SQL is still pristine template output, so a
   * hand-written query is NEVER silently clobbered.
   */
  const onDataSourceChange = (value: string) => {
    const nextDbType = dataSources.find(ds => ds.id === value)?.db_type
    setPreviewColumns(null)
    setDraft(current => ({
      ...current,
      dataSourceId: value,
      metricSql:
        sqlTemplateId !== null && isPristineStarterSql(sqlTemplateId, current.metricSql)
          ? starterSql(sqlTemplateId, nextDbType)
          : current.metricSql,
    }))
  }

  // The stored replay chunk belongs to the saved kind's collection; another
  // kind has none.
  const storedReplayChunk = (kind: MetricKind): MetricScanInterval | null =>
    metric?.kind === kind ? metric.replay_chunk_interval ?? null : null
  // What the save re-sends for `kind` at `interval`: the stored chunk, unless
  // it is finer than the interval — a 422 on a field this form does not show
  // (MET-10). Derived afresh on every change, so moving the interval past the
  // chunk and back restores it rather than losing it for good.
  const savedReplayChunk = (kind: MetricKind, interval: MetricScanInterval) => {
    const chunk = storedReplayChunk(kind)
    return chunk && isIntervalFinerThan(chunk, interval) ? null : chunk
  }
  // The stored chunk this save would clear, said beside the interval only
  // while the interval actually suppresses it.
  const storedChunk = storedReplayChunk(draft.kind)
  const clearedReplayChunk = storedChunk && draft.replayChunkInterval === null ? storedChunk : null

  const onIntervalChange = (next: MetricScanInterval) => {
    patch({ interval: next, replayChunkInterval: savedReplayChunk(draft.kind, next) })
  }

  // Dimension columns each kind had in this session, set aside on a kind switch.
  const [kindDimensions, setKindDimensions] = useState<
    Partial<Record<MetricKind, KindDimensions>>
  >({})

  // Switching kind swaps which config fields render. Dimension columns belong
  // to one kind's source (a data-source schema, or a fact table), so they are
  // not carried across (MET-19): the new kind gets back what it had earlier in
  // this session, else what was saved for it.
  const applyKind = (next: MetricKind) => {
    setSubmitAttempted(false)
    setKindDimensions(current => ({
      ...current,
      [draft.kind]: {
        breakdownColumns: draft.breakdownColumns,
        appVersionColumn: draft.appVersionColumn,
        platformColumn: draft.platformColumn,
      },
    }))
    const dimensions = kindDimensions[next] ?? savedDimensions(metric, next)
    setDraft(current => ({
      ...current,
      kind: next,
      ...dimensions,
      replayChunkInterval: savedReplayChunk(next, current.interval),
    }))
  }
  // The history-loss confirm moved to submit, where it covers every change of
  // meaning, not only this one (MET-1).
  const changeKind = (next: MetricKind) => {
    setKindChosen(true)
    if (next !== draft.kind) applyKind(next)
  }

  // A ratio reads as a fraction (0.08) until it has a unit: becoming one with
  // the Unit empty fills in `%` and says so (MT-17).
  const ratioUnit = (becomesRatio: boolean): Partial<MetricDraft> => {
    if (!becomesRatio || draft.unit.trim()) return {}
    setUnitAutoSet(true)
    return { unit: '%' }
  }
  const onFactCompositionChange = (next: FactComposition) => {
    setSubmitAttempted(false)
    patch({ factComposition: next, ...ratioUnit(next === 'ratio') })
  }
  const onEventCompositionPatch = (next: Partial<MetricDraft>) => {
    if (next.composition !== undefined && next.composition !== draft.composition) {
      setSubmitAttempted(false)
      patch({ ...next, ...ratioUnit(next.composition === 'ratio') })
      return
    }
    patch(next)
  }

  const onDisplayNameChange = (value: string) => {
    // Pre-fill the internal name from the display name until the user edits it
    // directly (creation only — the internal name is immutable afterwards).
    patch(
      isNew && !nameEdited
        ? { displayName: value, name: toIdentifier(value, fallbackName) }
        : { displayName: value },
    )
  }

  const onSubmit = async (createStatus?: MetricStatus) => {
    if (facts.loading || facts.error) return
    setSubmitAttempted(true)
    const errs = validateDraft(draft, isNew)
    const firstKey = Object.keys(errs)[0]
    if (firstKey) {
      goToField(firstKey)
      return
    }
    if (definitionChanged) {
      const ok = await confirm({
        title: 'Delete this metric’s history?',
        message: DEFINITION_CHANGE_MESSAGE,
        variant: 'danger',
        confirmLabel: 'Save and delete history',
      })
      if (!ok) return
    }
    // `previewColumns` is set only by a clean preview of the current inputs;
    // any edit or data-source change clears it. So a query never previewed is
    // asked about too, not only one whose preview failed (MT-15).
    if (isNew && draft.kind === 'sql' && (sqlPreviewFailed || previewColumns === null)) {
      const ok = await confirm({
        title: sqlPreviewFailed
          ? 'Create a metric whose preview failed?'
          : 'Create a metric that hasn’t previewed?',
        message: sqlPreviewFailed
          ? "The query hasn't previewed successfully, so collection will likely fail the same way. Create it anyway?"
          : 'Run Preview first to check the query returns what this metric expects. Collection fails on the first run if it does not. Create it anyway?',
        confirmLabel: 'Create anyway',
      })
      if (!ok) return
    }
    saveMut.mutate(isNew ? (createStatus ?? 'active') : undefined)
  }

  // Seed the create form from a starter template, then reveal the (now
  // prefilled) form. Only editable presentation + kind-shape fields are set;
  // project-specific refs (data source, fact table, events, columns) stay empty
  // so the user still points the metric at their own data.
  const applyTemplate = (template: MetricTemplate) => {
    const { seed } = template
    setKindChosen(true)
    applyKind(seed.kind)
    // A template's generic name ("Conversion") may already be taken: suffix it
    // the way Duplicate does, so the seeded internal name does not 409 (MT-32).
    const takenNames = new Set(cachedCatalogItems(qc, slug).map(item => item.display_name.toLowerCase()))
    let seededName = seed.displayName
    for (let n = 2; takenNames.has(seededName.toLowerCase()); n += 1) {
      seededName = `${seed.displayName} ${n}`
    }
    onDisplayNameChange(seededName)
    setDraft(current => ({
      ...current,
      unit: seed.unit,
      anomalyDetection: seed.anomalyDetection,
      interval: seed.interval ?? current.interval,
      composition: seed.composition ?? current.composition,
      factComposition: seed.factComposition ?? current.factComposition,
      numeratorOp: seed.aggregation
        ? { ...current.numeratorOp, aggregation: seed.aggregation }
        : current.numeratorOp,
      // Render for whatever source is selected right now (usually none, on the
      // create screen); a data-source pick re-renders it.
      metricSql:
        seed.sqlTemplate !== undefined
          ? starterSql(seed.sqlTemplate, selectedDataSource?.db_type)
          : current.metricSql,
      sqlTimeColumn: seed.sqlTimeColumn ?? current.sqlTimeColumn,
    }))
    if (seed.sqlTemplate !== undefined) setSqlTemplateId(seed.sqlTemplate)
    setUnitAutoSet(false)
    setPickedTemplate(template)
    setShowTemplates(false)
  }

  const errorEntries = Object.entries(fieldErrors)

  return (
    <div className="h-full overflow-y-auto">
      {/* The shell pads the page; the form adds none of its own (DS-3). */}
      <PageContainer width="narrow">
      {/* `noValidate`: every rule is checked by `validateDraft` and named inline
          and in the list above Save, never by a browser bubble (AU-4). */}
      <form
        noValidate
        onSubmit={e => {
          e.preventDefault()
          void onSubmit()
        }}
      >
        <PageHeader
          className="mb-[18px]"
          back={
            <button
              type="button"
              onClick={onBack ?? onClose}
              className="inline-flex items-center gap-1 text-caption transition-colors hover:text-[var(--fg)]"
              style={{ color: 'var(--fg-muted)' }}
            >
              {/* Names where it leads, like the fact-table editor's (MT-31). */}
              <ChevronLeft size={14} /> Metrics
            </button>
          }
          eyebrow="Observe · Metric"
          // The edited metric is named, so two open editors are told apart (MT-31).
          title={metric ? `${canWrite ? 'Edit' : 'Metric'} · ${metric.display_name}` : 'New metric'}
        />
        {!canWrite && <ReadOnlyNotice className="mb-[18px]" />}

        {/* A viewer gets the definition read-only: `disabled` on a fieldset
            reaches every native control inside it. `contents` keeps it out of
            the layout. */}
        <fieldset disabled={!canWrite} className="contents">
          {isNew && showTemplates && (
            <TemplateGallery onPick={applyTemplate} onSkip={() => setShowTemplates(false)} />
          )}
          {/* The gallery stays one click away once dismissed, whether a
              template was picked or the author started from scratch (MT-32). */}
          {isNew && !showTemplates && (
            <div
              className="mb-[18px] flex flex-wrap items-center gap-x-2 gap-y-1 rounded-card border px-4 py-2 text-body-sm border-border bg-bg-sunken text-fg-secondary"
            >
              {pickedTemplate ? (
                <span role="status">
                  Started from{' '}
                  <strong className="font-semibold text-fg">
                    {pickedTemplate.label}
                  </strong>
                </span>
              ) : (
                <span>Not sure where to begin?</span>
              )}
              <Button type="button" variant="ghost" size="xs" onClick={() => setShowTemplates(true)}>
                {pickedTemplate ? 'Change template' : 'Browse templates'}
              </Button>
            </div>
          )}

          {/* What the metric measures comes first: the kind decides every
              field below it, the unit included (MT-2). One column, full
              width — kit Field spends a fixed 232px on its label gutter from
              `sm` up, so nothing narrower than the page leaves a usable
              control (tripl-vv2f). */}
          <SCard title="What to measure" description="Where the metric's value comes from.">
            <Field label="Metric kind" stacked last>
              <RadioCards
                groupLabel="Metric kind"
                value={draft.kind}
                onChange={value => changeKind(value as MetricKind)}
                options={kindOptions({ noEvents, noFactTables: facts.noFactTables })}
              />
              {/* The way to the missing prerequisite, under the cards that
                  need it: a link cannot sit inside a radio card (MT-3). */}
              {isNew && (noEvents || facts.noFactTables) && (
                <p className="mt-[8px] text-caption text-fg-tertiary">
                  {noEvents && (
                    <>
                      No events yet.{' '}
                      <Link to={projectPath(currentOrgSlug(), slug, '/events')} className="underline underline-offset-2 text-fg">
                        Add events
                      </Link>
                      {facts.noFactTables ? ' · ' : null}
                    </>
                  )}
                  {facts.noFactTables && (
                    <>
                      No fact tables yet.{' '}
                      <Link
                        to={projectPath(currentOrgSlug(), slug, '/metrics/fact-tables/new')}
                        className="underline underline-offset-2 text-fg"
                      >
                        Create one
                      </Link>
                    </>
                  )}
                </p>
              )}
            </Field>
          </SCard>

          {definitionChanged && <DefinitionChangeNotice />}

          {draft.kind === 'sql' && (
            <SqlDefinitionFields
              slug={slug}
              draft={draft}
              patch={patch}
              errors={fieldErrors}
              canWrite={canWrite}
              dataSources={dataSources}
              dataSourcesError={dataSourcesError}
              onRetryDataSources={onRetryDataSources}
              onDataSourceChange={onDataSourceChange}
              onIntervalChange={onIntervalChange}
              clearedReplayChunk={clearedReplayChunk}
              schemaTables={sqlSchemaData?.tables}
              columnSuggestions={sqlColumns}
              onPreviewColumns={setPreviewColumns}
              onPreviewFailed={setSqlPreviewFailed}
            />
          )}
          {draft.kind === 'fact' && (
            <FactDefinitionFields
              slug={slug}
              draft={draft}
              patch={patch}
              errors={fieldErrors}
              facts={facts}
              onIntervalChange={onIntervalChange}
              onFactCompositionChange={onFactCompositionChange}
              clearedReplayChunk={clearedReplayChunk}
            />
          )}
          {draft.kind === 'event_composition' && (
            <EventCompositionFields
              slug={slug}
              draft={draft}
              patch={onEventCompositionPatch}
              errors={fieldErrors}
              disabled={!canWrite}
            />
          )}
          {/* The chart SQL metrics get from their Query card, for the two
              kinds with no query of their own (MT-9). */}
          {seriesRequest && (
            <SeriesPreviewCard
              slug={slug}
              draft={draft}
              request={seriesRequest}
              canWrite={canWrite}
            />
          )}

          <SCard title="Name and display">
            <Field
              label="Display name"
              htmlFor="metric-display-name"
              required
              error={fieldErrors['metric-display-name']}
              announceError={false}
            >
              <TextInput
                id="metric-display-name"
                value={draft.displayName}
                onChange={onDisplayNameChange}
                placeholder={examplePlaceholder('Checkout conversion')}
                aria-required
                {...errorAria(fieldErrors, 'metric-display-name')}
              />
              {/* The derived internal name stays in sight while its input
                  sits in the fold (MT-2). */}
              {isNew && (
                <p className="mt-[6px] flex flex-wrap items-center gap-x-1.5 text-caption text-fg-tertiary">
                  <span>Internal name</span>
                  <span className="mono" style={{ color: draft.name ? 'var(--fg-muted)' : 'var(--fg-faint)' }}>
                    {draft.name || 'derived from the display name'}
                  </span>
                  <Button type="button" variant="ghost" size="xs" onClick={() => goToField('metric-name')}>
                    Change
                  </Button>
                </p>
              )}
            </Field>
            {!isNew && (
              // After creation this row holds the name as text, not a
              // control: `false` names it as a group.
              <Field label="Internal name" htmlFor={false} hint="Can't be changed after creation.">
                <div className="mono text-body text-fg">
                  {draft.name}
                </div>
              </Field>
            )}
            <Field
              label="Unit"
              htmlFor="metric-unit"
              hint={
                unitAutoSet && draft.unit === '%'
                  ? 'Set to % for a ratio. Ratios with % display as percentages (0.08 → 8%).'
                  : 'Shown after the value. Ratios with % display as percentages (0.08 → 8%).'
              }
            >
              <div className="flex flex-wrap items-center gap-2">
                <div className="w-[120px]">
                  <TextInput
                    id="metric-unit"
                    value={draft.unit}
                    onChange={value => {
                      setUnitAutoSet(false)
                      patch({ unit: value })
                    }}
                    placeholder={examplePlaceholder('%')}
                  />
                </div>
                {UNIT_SUGGESTIONS.map(unit => (
                  <Button
                    key={unit}
                    type="button"
                    variant="outline"
                    size="xs"
                    aria-pressed={draft.unit === unit}
                    aria-label={`Use unit ${unit}`}
                    onClick={() => {
                      setUnitAutoSet(false)
                      patch({ unit })
                    }}
                  >
                    {unit}
                  </Button>
                ))}
              </div>
            </Field>
            {/* Who answers for the numbers, shown as an avatar in the catalog
                (MT-25). */}
            <Field label="Owner" htmlFor="metric-owner" last={isNew}>
              <NativeSelect
                id="metric-owner"
                value={draft.ownerId}
                onChange={value => patch({ ownerId: value })}
                options={ownerOptions}
              />
            </Field>
            {/* On create the Save buttons choose the status (MT-1); an edit
                keeps the select, with what each status means. */}
            {!isNew && (
              <Field label="Status" htmlFor="metric-status" hint={STATUS_HINT} last>
                <NativeSelect
                  id="metric-status"
                  value={draft.status}
                  onChange={value => patch({ status: value as MetricStatus })}
                  options={METRIC_STATUSES.map(s => ({ value: s, label: METRIC_STATUS_LABEL[s] }))}
                />
              </Field>
            )}
            {/* Less-used fields, folded (MT-2). The content stays mounted,
                only hidden: the fields keep their values and labels, and the
                fold opens itself for a field with a problem. */}
            <button
              type="button"
              aria-expanded={moreShown}
              aria-controls="metric-more-options"
              onClick={() => setMoreOpen(!moreShown)}
              className="flex w-full items-center gap-2 px-4 py-[13px] text-left transition-colors hover:bg-[var(--surface-hover)] border-t border-border-subtle"
            >
              <ChevronRight
                size={14}
                aria-hidden="true"
                className="shrink-0 transition-transform text-fg-tertiary"
                style={{ transform: moreShown ? 'rotate(90deg)' : undefined }}
              />
              <span className="text-body font-medium text-fg">
                More options
              </span>
              <span className="ml-auto text-caption text-fg-tertiary">
                {isNew ? 'Internal name, description, color' : 'Description, color'}
              </span>
            </button>
            <div
              id="metric-more-options"
              hidden={!moreShown}
              className="border-t border-border-subtle"
            >
              {isNew && (
                <Field
                  label="Internal name"
                  htmlFor="metric-name"
                  required
                  hint="Stable identifier used in queries."
                  error={fieldErrors['metric-name']}
                  announceError={false}
                >
                  <TextInput
                    id="metric-name"
                    value={draft.name}
                    onChange={value => {
                      setNameEdited(true)
                      patch({ name: value })
                    }}
                    mono
                    placeholder={examplePlaceholder('checkout_conversion')}
                    aria-required
                    {...errorAria(fieldErrors, 'metric-name')}
                  />
                </Field>
              )}
              <Field label="Description" htmlFor="metric-description">
                <TextArea
                  id="metric-description"
                  value={draft.description}
                  onChange={value => patch({ description: value })}
                  rows={2}
                  placeholder="What does this metric measure?"
                />
              </Field>
              <Field label="Color" htmlFor={false} last>
                <ColorSwatches
                  value={draft.color}
                  onChange={value => patch({ color: value })}
                  inputId="metric-color"
                />
              </Field>
            </div>
          </SCard>

          <MonitoringFields
            draft={draft}
            patch={patch}
            columnChoices={columnChoices}
            columnSource={draft.kind === 'fact' ? 'fact' : previewColumns ? 'preview' : 'schema'}
          />
        </fieldset>

        {errorEntries.length > 0 && (
          <div
            role="alert"
            className="mb-[18px] rounded-card border px-4 py-3 text-body-sm bg-danger-soft text-danger"
            style={{
              borderColor: 'color-mix(in oklab, var(--danger) 35%, var(--border))',
            }}
          >
            {/* Each message is a link to its field: a keyboard or screen-reader
                user lands on the control instead of hunting for it (MET-15). */}
            <ul className="list-disc space-y-1 pl-4">
              {errorEntries.map(([key, error]) => (
                <li key={key}>
                  <button
                    type="button"
                    onClick={() => goToField(key)}
                    className="text-left underline decoration-transparent underline-offset-2 hover:decoration-current focus-visible:decoration-current"
                  >
                    {error}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {saveMut.isError && (
          <div className="mb-[18px]">
            <ErrorState compact title="Could not save metric" error={saveMut.error} />
          </div>
        )}

        {/* Sticky, so Save and the reason it is blocked stay on screen on a
            2,500-3,600px form (MT-4). The status jumps to the first field.
            A new metric is created collecting, or parked as a draft: the old
            Draft default was never collected and nothing said so (MT-1). */}
        <SaveBar
          status={
            attentionSummary(errorEntries.length)
            ?? (isNew && canWrite ? 'Drafts are saved but not collected or monitored.' : null)
          }
          statusTone={errorEntries.length > 0 ? 'danger' : 'muted'}
          onStatusClick={errorEntries[0] ? () => goToField(errorEntries[0]![0]) : undefined}
        >
          <Button type="button" variant="outline" onClick={onClose}>
            {canWrite ? 'Cancel' : 'Close'}
          </Button>
          {canWrite && isNew && (
            <Button
              type="button"
              variant="outline"
              disabled={saveMut.isPending || facts.loading || facts.error != null}
              onClick={() => void onSubmit('draft')}
            >
              Save as draft
            </Button>
          )}
          {canWrite && (
            <Button type="submit" disabled={saveMut.isPending || facts.loading || facts.error != null}>
              {saveMut.isPending ? (
                <Loader2 className="animate-spin" aria-hidden="true" />
              ) : isNew ? (
                <Plus aria-hidden="true" />
              ) : (
                <Save aria-hidden="true" />
              )}
              {isNew ? 'Create and start collecting' : 'Save metric'}
            </Button>
          )}
        </SaveBar>
      </form>
      </PageContainer>
      {confirmDialog}
      {unsaved.dialog}
    </div>
  )
}

const EMPTY_DATA_SOURCES: DataSource[] = []

/**
 * Route wrapper: loads what MetricForm needs (data sources, and — when editing
 * — the metric itself) and renders the form full-page. Reached via
 * `/p/:slug/metrics/new` and `/p/:slug/metrics/:metricId/edit`.
 *
 * Only the metric itself blocks the editor. Events are searched by the picker
 * that needs them, and a failed data-source list is shown in the SQL Source
 * card: a fact or event metric does not need it, so it must not lock the whole
 * editor out (MET-28).
 */
export default function MetricEditPage() {
  const { slug, metricId } = useParams<{ slug: string; metricId?: string }>()
  const canWrite = useCanWriteProject()
  const navigate = useNavigate()
  const location = useLocation()
  const isNew = !metricId

  // Back to wherever the editor was opened from — the catalog, or the metric's
  // own drilldown — and to the catalog when it was opened directly (MET-29).
  const goBack = () => {
    if (location.key !== 'default') navigate(-1)
    else navigate(projectPath(currentOrgSlug(), slug, '/metrics'))
  }
  const onSaved = (savedId: string, created: boolean) => {
    // Replace the create route: Back from the new metric must not reopen an
    // empty "New metric" form.
    if (created) navigate(getMetricMonitoringPath(slug!, savedId), { replace: true })
    else goBack()
  }

  const dataSourcesQuery = useQuery({
    queryKey: dataSourcesKey(),
    queryFn: () => dataSourcesApi.list(),
    // Rendered inline in the SQL Source card.
    meta: SILENT_ERROR_META,
  })
  const metricQuery = useQuery({
    queryKey: metricDefinitionKey(slug, metricId ?? 'new'),
    queryFn: () => metricsCatalogApi.get(slug!, metricId!),
    enabled: !!slug && !!metricId,
  })

  // A viewer reads a metric on its drilldown, whose Definition card is the
  // read view; the editor showed them a disabled form with live borders,
  // required stars and author hints instead (#237 MT-28). "New metric" has
  // nothing to read, so it goes back to the catalog.
  if (!canWrite && slug) {
    return (
      <Navigate
        to={metricId ? getMetricMonitoringPath(slug, metricId) : projectPath(currentOrgSlug(), slug, '/metrics')}
        replace
      />
    )
  }

  // A deleted or unknown metric is "not found" with the way back, not a red
  // card offering a retry that can never succeed (#237 SH-33).
  if (metricQuery.error) {
    return (
      <PageContainer width="narrow">
        <QueryErrorState
          error={metricQuery.error}
          title="Could not load this metric"
          onRetry={() => void metricQuery.refetch()}
          notFound={{
            title: 'Metric not found',
            back: { to: projectPath(currentOrgSlug(), slug, '/metrics'), label: 'Back to metrics' },
          }}
        />
      </PageContainer>
    )
  }

  // Data sources still block while LOADING: a SQL metric opened before its
  // source's option exists would paint the select blank. The form's shape
  // while it does, not a centred "Loading…" (#237 SH-23).
  const isLoading = dataSourcesQuery.isLoading || (!isNew && metricQuery.isLoading)
  if (isLoading || !slug) {
    return (
      <PageContainer width="narrow">
        <PageSkeleton variant="form" label={isNew ? 'Loading metric editor…' : 'Loading metric…'} />
      </PageContainer>
    )
  }

  return (
    <MetricForm
      key={metricQuery.data?.id ?? 'new'}
      slug={slug}
      metric={metricQuery.data ?? null}
      dataSources={dataSourcesQuery.data ?? EMPTY_DATA_SOURCES}
      dataSourcesError={dataSourcesQuery.error ?? undefined}
      onRetryDataSources={() => void dataSourcesQuery.refetch()}
      onClose={goBack}
      onBack={() => navigate(projectPath(currentOrgSlug(), slug, '/metrics'))}
      onSaved={onSaved}
    />
  )
}

