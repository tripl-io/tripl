import { PageHeader } from '@/components/primitives/page-header'
import { PageContainer } from '@/components/primitives/page-container'
import { SaveBar } from '@/components/forms/SaveBar'
import { examplePlaceholder, sqlPlaceholder } from '@/components/forms/placeholders'
import { attentionSummary } from '@/components/forms/validation'
import { Button } from '@/components/ui/button'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Navigate, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronLeft, Eye, Loader2, Plus, Save, Trash2 } from 'lucide-react'
import { dataSourcesApi } from '@/api/dataSources'
import { factTablesApi } from '@/api/factTables'
import { toast } from 'sonner'
import { ColumnSuggestInput } from '@/components/column-suggest'
import { ErrorState } from '@/components/error-state'
import { SqlEditor } from '@/components/sql-editor'
import { useDataSourceSchema } from '@/hooks/useDataSourceSchema'
import { useConfirm } from '@/hooks/useConfirm'
import { ConfirmImpactMessage } from '@/components/dependencies/ImpactNotice'
import { UsedBySection } from '@/components/dependencies/UsedBySection'
import { useUnsavedChangesGuard } from '@/hooks/useUnsavedChangesGuard'
import { editPageTitle, usePageTitle } from '@/components/shell-chrome-context'
import { Chip, type ChipTone } from '@/components/primitives/chip'
import {
  SCard,
  NativeSelect,
  TextArea,
  TextInput,
  type SelectOption,
  Field,
} from '@/components/settings/kit'
import type {
  DataSource,
  FactTable,
  FactTableColumn,
  FactTableCreate,
  FactTableListResponse,
  FactTablePreviewResponse,
  FactTableRowFilter,
  FactTableUpdate,
} from '@/types'
import {
  dataSourcesKey,
  factTableKey,
  factTablesKey,
  metricGeneratedSqlKey,
  projectFactTableKey,
} from '@/lib/queryKeys'
import { useCanWriteProject } from '@/lib/permissions'
import { toIdentifier } from '@/lib/identifier'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { PageSkeleton, QueryErrorState, ReadOnlyNotice } from '@/components/states'
import { FactTableReadView } from './FactTableReadView'
import { ColorSwatches } from '@/pages/metrics/ColorSwatches'
import { nextMetricColor } from '@/pages/metrics/metricDraft'
import { useDebouncedRerun } from '@/pages/metrics/useDebouncedRerun'
import { uid } from '@/lib/uid'
// The shared settings field row and error wiring: the metric and fact-table
// editors report validation the same way — inline under the field, linked from
// the summary, focus moved to the first one (MET-35).
import {
  errorAria,
  fieldErrorId,
  focusField,
  type FieldErrors,
} from '@/lib/fieldErrors'
import { currentOrgSlug, projectPath } from '@/lib/navigation'


/** The inline "Could not preview columns" block; a failed save-time preview focuses it. */
const PREVIEW_ERROR_ID = 'fact-preview-error'

function toOptions(prefix: string, items: { value: string; label: string }[]): SelectOption[] {
  return [{ value: '', label: prefix }, ...items]
}

// Bucket a raw warehouse type string into a coarse family for the badge tone.
// The badge text always shows the raw type; the tone is purely a visual cue.
function typeTone(raw: string): ChipTone {
  const t = raw.toLowerCase()
  if (/(int|numeric|decimal|float|double|real|number|serial)/.test(t)) return 'accent'
  if (/(timestamp|date|time)/.test(t)) return 'info'
  if (/(bool)/.test(t)) return 'success'
  return 'neutral'
}

// A row-filter entry with a stable client-side id. The editable list keys by
// this id rather than the array index, so removing a middle row no longer
// reuses a DOM node and jumps focus/cursor to the wrong row. The id is stripped
// before building the create/update payload (the API expects only name + sql).
interface RowFilterDraft extends FactTableRowFilter {
  id: string
}

/**
 * The first row-filter name that appears twice, or `null`.
 *
 * A fact metric stores the NAME of the filter it applies, and the collector
 * resolves it to the FIRST stored filter with that name, so a repeat makes one
 * of the two SQL fragments permanently unreachable while both keep showing up in
 * the metric form's picker. The backend refuses such a payload outright
 * (`schemas/fact_table._reject_duplicate_filter_names`); this is only the
 * affordance that says so before a filled-in form round-trips to a 422.
 *
 * Fed the `cleanRowFilters()` output rather than the drafts so it judges exactly
 * what the request will carry — names trimmed, incomplete rows already dropped —
 * and cannot disagree with the validator it is mirroring. Returning the first
 * repeat rather than a count is what lets the message name the offending filter,
 * which matters because a fact table whose STORED filters already collide (the
 * backend validates input only, and never backfilled) will now fail a save the
 * user did not break.
 */
function firstRepeatedFilterName(filters: FactTableRowFilter[]): string | null {
  const seen = new Set<string>()
  for (const filter of filters) {
    if (seen.has(filter.name)) return filter.name
    seen.add(filter.name)
  }
  return null
}

interface FactTableFormProps {
  slug: string
  factTable: FactTable | null
  dataSources: DataSource[]
  onClose: () => void
}

/** What a column preview introspects: the source and the SQL. */
interface PreviewRequest {
  dataSourceId: string
  sql: string
  timestampColumn: string
}

/** The columns a save persists, with the identifier picks made on them. */
interface Introspection {
  columns: FactTableColumn[]
  identifierColumns: string[]
}

/**
 * Identity of the input a column list was introspected from. The columns are
 * only true for the source + SQL they came from; any change to either makes
 * them a guess (MET-9).
 */
function introspectionKey(dataSourceId: string, sql: string): string {
  return JSON.stringify([dataSourceId, sql.trim()])
}

/**
 * Re-derive the identifier selection from fresh suggestions while preserving
 * the user's manual picks relative to the previous suggestion set: columns the
 * user checked beyond the old suggestions stay checked (if they still exist),
 * columns the user unchecked in this session stay unchecked. On the first
 * preview of an edit session the previous suggestion set is empty, so every
 * saved `identifier_column` counts as a manual pick and is UNION-ed with the
 * new candidates — a preview may ADD newly-suggested columns but never
 * silently DROPS a saved pick (tripl-4qfr).
 */
function mergeIdentifierPicks(
  current: readonly string[],
  previousCandidates: readonly string[],
  res: FactTablePreviewResponse,
): string[] {
  const existingNames = new Set(res.columns.map(column => column.name))
  const manuallyAdded = current.filter(
    name => !previousCandidates.includes(name) && existingNames.has(name),
  )
  const manuallyRemoved = new Set(previousCandidates.filter(name => !current.includes(name)))
  const next = res.identifier_candidates.filter(name => !manuallyRemoved.has(name))
  return [...next, ...manuallyAdded.filter(name => !next.includes(name))]
}

/** The message for a timestamp column the introspected SQL does not return. */
function timestampColumnError(timestampColumn: string, columns: FactTableColumn[]): string | null {
  const wanted = timestampColumn.trim()
  if (!wanted || columns.length === 0) return null
  if (columns.some(column => column.name === wanted)) return null
  return `"${wanted}" is not a column of this SQL. Pick one of the previewed columns.`
}

const rowFilterFieldId = (filterId: string, part: 'name' | 'sql') =>
  `fact-row-filter-${filterId}-${part}`

/**
 * Create / edit a fact table. `name` is the per-project identity and is
 * immutable after creation (the backend rejects a change), so it is read-only
 * when editing. The SQL is a full read-only SELECT/CTE; "Preview columns"
 * introspects it server-side and populates the persisted `columns` +
 * `identifier_columns`, and a save whose columns no longer match the SQL runs
 * that preview itself first. Validation messages sit under their fields, with
 * a linked summary at the foot, the same way the metric form reports them.
 */
export function FactTableForm({ slug, factTable, dataSources, onClose }: FactTableFormProps) {
  const qc = useQueryClient()
  // Fact-table writes and previews are editor-only (MET-6).
  const canWrite = useCanWriteProject()
  const isNew = !factTable
  const { confirm, dialog: confirmDialog } = useConfirm()
  // The top bar names the edited table after "Fact tables", as the heading
  // does (MT-31).
  usePageTitle(factTable ? editPageTitle(factTable.display_name) : null)

  const [displayName, setDisplayName] = useState(factTable?.display_name ?? '')
  const [name, setName] = useState(factTable?.name ?? '')
  // Same pre-fill as the metric form (MET-34): the internal name follows the
  // display name until the user types into it directly.
  const [nameEdited, setNameEdited] = useState(false)
  // Stands in for a display name with no Latin letters to derive from; minted
  // once so it does not churn while the user types.
  const [fallbackName] = useState(() => `fact_${Date.now().toString(36)}`)
  const onDisplayNameChange = (value: string) => {
    setDisplayName(value)
    // Creation only — the internal name is immutable afterwards.
    if (isNew && !nameEdited) setName(toIdentifier(value, fallbackName))
  }
  const onNameChange = (value: string) => {
    setNameEdited(true)
    setName(value)
  }
  const [description, setDescription] = useState(factTable?.description ?? '')
  // A new table takes the first palette colour no listed table uses, as a new
  // metric does (MT-35).
  const [color, setColor] = useState(
    () =>
      factTable?.color
      ?? nextMetricColor(
        (qc.getQueryData<FactTableListResponse>(factTablesKey(slug))?.items ?? []).map(t => t.color),
      ),
  )
  const [dataSourceId, setDataSourceId] = useState(factTable?.data_source_id ?? '')
  const [sql, setSql] = useState(factTable?.sql ?? '')
  const [timestampColumn, setTimestampColumn] = useState(factTable?.timestamp_column ?? '')
  // The column a preview filled the empty timestamp in with, said beside it
  // until the author changes it (MT-19).
  const [detectedTimestamp, setDetectedTimestamp] = useState<string | null>(null)
  const onTimestampChange = (value: string) => {
    setDetectedTimestamp(null)
    setTimestampColumn(value)
  }

  // Persisted introspection: populated by a successful preview, seeded from the
  // existing fact table when editing.
  const [columns, setColumns] = useState<FactTableColumn[]>(factTable?.columns ?? [])
  // A row filter runs over this table's own output, so its editor completes
  // against the introspected columns, not every table in the warehouse.
  const rowFilterTables = useMemo(
    () => [
      {
        name: name || 'fact_table',
        columns: columns.map(column => ({ name: column.name, data_type: column.type })),
      },
    ],
    [name, columns],
  )
  // The source + SQL the columns above describe. A saved table's stored columns
  // describe its stored SQL; a table saved with none describes nothing yet.
  const [introspectedFor, setIntrospectedFor] = useState<string | null>(() =>
    factTable && factTable.columns.length > 0
      ? introspectionKey(factTable.data_source_id ?? '', factTable.sql)
      : null,
  )
  const columnsAreCurrent = introspectedFor === introspectionKey(dataSourceId, sql)
  const [identifierColumns, setIdentifierColumns] = useState<string[]>(
    factTable?.identifier_columns ?? [],
  )
  // The backend's *suggested* identifiers for this session. There are no
  // suggestions until a preview runs, so this starts empty even when editing:
  // the saved `identifier_columns` are the user's picks, NOT prior suggestions.
  // Seeding this from `identifier_columns` (tripl-4qfr) made the first preview of
  // an edit session treat saved manual picks as stale suggestions and silently
  // uncheck the ones the tightened count_distinct heuristic no longer returns.
  const [identifierCandidates, setIdentifierCandidates] = useState<string[]>([])
  const [rowFilters, setRowFilters] = useState<RowFilterDraft[]>(() =>
    (factTable?.row_filters ?? []).map(filter => ({ ...filter, id: uid() })),
  )

  // Messages appear once a save was tried, then follow the edits live.
  const [submitAttempted, setSubmitAttempted] = useState(false)

  const dataSourceOptions = useMemo(
    () => toOptions('Select data source…', dataSources.map(ds => ({ value: ds.id, label: ds.name }))),
    [dataSources],
  )

  // Drive the SQL editor's dialect highlighting + schema-aware autocomplete from
  // the selected data source (same wiring as the scans base-query editor).
  const selectedDataSource = useMemo(
    () => dataSources.find(ds => ds.id === dataSourceId),
    [dataSources, dataSourceId],
  )
  // The schema route is editor-only, so a read-only visitor would get a 403
  // for autocomplete they cannot use: skip the request instead.
  const { data: sqlSchemaData } = useDataSourceSchema(canWrite ? dataSourceId || undefined : undefined)

  // Timestamp-typed columns first: they are the only sensible picks.
  const timestampSuggestions = useMemo(
    () =>
      [...columns]
        .sort((a, b) => Number(typeTone(b.type) === 'info') - Number(typeTone(a.type) === 'info'))
        .map(column => column.name),
    [columns],
  )

  const applyPreview = (res: FactTablePreviewResponse, request: PreviewRequest): Introspection => {
    // Returned for a save in this same tick; the functional update keeps a
    // pick the user toggled while the preview was in flight.
    const nextIdentifiers = mergeIdentifierPicks(identifierColumns, identifierCandidates, res)
    setColumns(res.columns)
    setIdentifierColumns(current => mergeIdentifierPicks(current, identifierCandidates, res))
    setIdentifierCandidates(res.identifier_candidates)
    setIntrospectedFor(introspectionKey(request.dataSourceId, request.sql))
    // An empty timestamp takes the one timestamp-typed column, when there is
    // exactly one: the author no longer types a name the preview already knows.
    if (!request.timestampColumn.trim()) {
      const timeColumns = res.columns.filter(column => typeTone(column.type) === 'info')
      if (timeColumns.length === 1) {
        setTimestampColumn(timeColumns[0]!.name)
        setDetectedTimestamp(timeColumns[0]!.name)
      }
    }
    return { columns: res.columns, identifierColumns: nextIdentifiers }
  }

  const previewMut = useMutation({
    // Rendered inline under the button ("Could not preview columns").
    meta: SILENT_ERROR_META,
    mutationFn: (request: PreviewRequest): Promise<FactTablePreviewResponse> =>
      factTablesApi.preview(slug, {
        data_source_id: request.dataSourceId || null,
        sql: request.sql,
        timestamp_column: request.timestampColumn.trim() || null,
      }),
  })

  const currentPreviewRequest = (): PreviewRequest => ({ dataSourceId, sql, timestampColumn })

  // The source + SQL last sent for introspection, whatever came back: a failed
  // read is not retried on a timer, only after the next edit or click.
  const [attemptedFor, setAttemptedFor] = useState<string | null>(null)
  // Set by the first Preview click: from then on an edit to the SQL or the
  // source re-reads the columns by itself once typing pauses (MT-19). Never
  // before it — each read runs the query against the warehouse.
  const [autoPreview, setAutoPreview] = useState(false)

  const startPreview = () => {
    const request = currentPreviewRequest()
    setAttemptedFor(introspectionKey(request.dataSourceId, request.sql))
    previewMut.mutate(request, { onSuccess: res => applyPreview(res, request) })
  }
  const runPreview = () => {
    setAutoPreview(true)
    startPreview()
  }
  const currentInputKey = introspectionKey(dataSourceId, sql)
  const autoPreviewPending =
    canWrite
    && autoPreview
    && !columnsAreCurrent
    && !!sql.trim()
    && !!dataSourceId
    && attemptedFor !== currentInputKey
  useDebouncedRerun({
    armed: autoPreview && canWrite,
    stale: autoPreviewPending && !previewMut.isPending,
    inputKey: currentInputKey,
    run: startPreview,
  })

  function cleanRowFilters(): FactTableRowFilter[] {
    return rowFilters
      .map(f => ({ name: f.name.trim(), sql: f.sql.trim() }))
      .filter(f => f.name && f.sql)
  }

  /** Field id → message, in the order the fields appear on the page. */
  function validate(): Record<string, string> {
    const errs: Record<string, string> = {}
    if (!displayName.trim()) errs['fact-display-name'] = 'Display name is required.'
    if (isNew && !name.trim()) errs['fact-name'] = 'Internal name is required.'
    if (!dataSourceId) errs['fact-data-source'] = 'A data source is required.'
    if (!sql.trim()) errs['fact-sql'] = 'The fact table SQL is required.'
    if (!timestampColumn.trim()) {
      errs['fact-timestamp'] = 'A timestamp column is required.'
    } else if (columnsAreCurrent) {
      const mismatch = timestampColumnError(timestampColumn, columns)
      if (mismatch) errs['fact-timestamp'] = mismatch
    }
    // A half-filled row used to be dropped on save without a word, so a metric
    // naming that filter lost it (MET-3). Only an entirely blank row is dropped.
    rowFilters.forEach((filter, index) => {
      const hasName = !!filter.name.trim()
      const hasSql = !!filter.sql.trim()
      if (hasName !== hasSql) {
        errs[rowFilterFieldId(filter.id, hasName ? 'sql' : 'name')] =
          `Row filter ${index + 1} needs both a name and a SQL condition — complete it or remove it.`
      }
    })
    const repeated = firstRepeatedFilterName(cleanRowFilters())
    if (repeated !== null) {
      const second = rowFilters.filter(filter => filter.name.trim() === repeated)[1]
      if (second) {
        errs[rowFilterFieldId(second.id, 'name')] =
          `Two row filters are named "${repeated}". Metrics reference a filter by name, ` +
          'so each name can only be used once.'
      }
    }
    return errs
  }

  const fieldErrors: FieldErrors = submitAttempted ? validate() : {}
  const errorEntries = Object.entries(fieldErrors)

  function buildCreatePayload(introspection: Introspection): FactTableCreate {
    return {
      color,
      columns: introspection.columns,
      data_source_id: dataSourceId || null,
      description,
      display_name: displayName.trim(),
      identifier_columns: introspection.identifierColumns,
      name: name.trim(),
      row_filters: cleanRowFilters(),
      sql,
      timestamp_column: timestampColumn.trim(),
    }
  }

  function buildUpdatePayload(introspection: Introspection): FactTableUpdate {
    // `name` is immutable, so it is intentionally excluded from the update.
    return {
      color,
      columns: introspection.columns,
      data_source_id: dataSourceId || null,
      description,
      display_name: displayName.trim(),
      identifier_columns: introspection.identifierColumns,
      row_filters: cleanRowFilters(),
      sql,
      timestamp_column: timestampColumn.trim(),
    }
  }

  // Every input a save would send, as one comparable string; the first render's
  // value is the baseline (MET-5). Row filters drop their client-side ids,
  // which are fresh on every mount.
  const draftSnapshot = JSON.stringify({
    displayName, name, description, color, dataSourceId, sql, timestampColumn,
    columns, identifierColumns,
    rowFilters: rowFilters.map(filter => [filter.name, filter.sql]),
  })
  const [initialSnapshot] = useState(draftSnapshot)
  // A viewer's form is disabled and so never dirty.
  const unsaved = useUnsavedChangesGuard(canWrite && draftSnapshot !== initialSnapshot)

  const saveMut = useMutation({
    // Rendered inline at the foot of the form ("Could not save …").
    meta: SILENT_ERROR_META,
    mutationFn: (introspection: Introspection) =>
      factTable
        ? factTablesApi.update(slug, factTable.id, buildUpdatePayload(introspection))
        : factTablesApi.create(slug, buildCreatePayload(introspection)),
    onSuccess: () => {
      unsaved.release()
      void qc.invalidateQueries({ queryKey: factTablesKey(slug) })
      if (factTable) void qc.invalidateQueries({ queryKey: projectFactTableKey(slug) })
      void qc.invalidateQueries({ queryKey: metricGeneratedSqlKey(slug) })
      onClose()
    },
  })

  const deleteMut = useMutation({
    // Rendered inline: a 409 names the metrics that still read this table, and
    // that list is the whole point of the message (MET-36).
    meta: SILENT_ERROR_META,
    mutationFn: (id: string) => factTablesApi.remove(slug, id),
    onSuccess: () => {
      unsaved.release()
      void qc.invalidateQueries({ queryKey: factTablesKey(slug) })
      void qc.invalidateQueries({ queryKey: projectFactTableKey(slug) })
      toast.success('Fact table deleted.')
      onClose()
    },
  })

  const onDelete = async () => {
    if (!factTable) return
    const ok = await confirm({
      title: 'Delete this fact table?',
      // The metrics that read it, listed before the attempt (#257). These are
      // the one dependents that DO block: the server refuses with a 409.
      message: (
        <ConfirmImpactMessage
          message={
            `"${factTable.display_name}" disappears from every fact metric's picker. A fact table ` +
            'that metrics still read cannot be deleted; the refusal names them.'
          }
          slug={slug}
          branchId={null}
          mode="blocks"
          changes={[{ kind: 'fact_table', id: factTable.id, change: 'delete' }]}
        />
      ),
      confirmLabel: 'Delete fact table',
      variant: 'danger',
    })
    if (ok) deleteMut.mutate(factTable.id)
  }

  // Focus asked for after an awaited preview has to wait for the render that
  // carries its result. Focusing at once hit a Preview button still disabled by
  // the pending mutation (TanStack notifies on a later tick), or a timestamp
  // input whose error and aria-describedby did not exist yet, so focus stayed
  // on Save. The id waits here until a commit shows a focusable target.
  const pendingFocusRef = useRef<string | null>(null)
  useEffect(() => {
    const fieldId = pendingFocusRef.current
    if (!fieldId) return
    const el = document.getElementById(fieldId)
    if (!el || (el instanceof HTMLButtonElement && el.disabled)) return
    pendingFocusRef.current = null
    focusField(fieldId)
  })

  const onSubmit = async () => {
    pendingFocusRef.current = null
    setSubmitAttempted(true)
    const firstKey = Object.keys(validate())[0]
    if (firstKey) {
      focusField(firstKey)
      return
    }
    // Columns are what every fact metric on this table picks its measure,
    // breakdown and condition columns from. Saving without a preview stored
    // none, and editing the SQL after one stored the old list (MET-9), so a
    // save whose columns do not describe this source + SQL introspects first.
    let introspection: Introspection = { columns, identifierColumns }
    if (!columnsAreCurrent) {
      const request = currentPreviewRequest()
      setAttemptedFor(introspectionKey(request.dataSourceId, request.sql))
      let res: FactTablePreviewResponse
      try {
        res = await previewMut.mutateAsync(request)
      } catch {
        // The preview's own inline error says why; the save waits for it.
        pendingFocusRef.current = PREVIEW_ERROR_ID
        return
      }
      introspection = applyPreview(res, request)
      if (timestampColumnError(timestampColumn, res.columns)) {
        pendingFocusRef.current = 'fact-timestamp'
        return
      }
    }
    saveMut.mutate(introspection)
  }

  // The backend's suggestions the picks do not match yet, offered as one click
  // instead of a line repeating what the ticked boxes already show (MT-20).
  const unusedSuggestions = identifierCandidates.filter(
    candidate =>
      !identifierColumns.includes(candidate) && columns.some(column => column.name === candidate),
  )
  const applySuggestions = () => {
    setIdentifierColumns(current => [...current, ...unusedSuggestions.filter(c => !current.includes(c))])
  }

  const toggleIdentifier = (columnName: string) => {
    setIdentifierColumns(current =>
      current.includes(columnName)
        ? current.filter(c => c !== columnName)
        : [...current, columnName],
    )
  }

  const addRowFilter = () => {
    setRowFilters(current => [...current, { id: uid(), name: '', sql: '' }])
  }

  const updateRowFilter = (id: string, patch: Partial<FactTableRowFilter>) => {
    setRowFilters(current =>
      current.map(filter => (filter.id === id ? { ...filter, ...patch } : filter)),
    )
  }

  const removeRowFilter = (id: string) => {
    setRowFilters(current => current.filter(filter => filter.id !== id))
  }

  const busy = saveMut.isPending || previewMut.isPending || deleteMut.isPending

  return (
    <div className="h-full overflow-y-auto">
      {unsaved.dialog}
      {confirmDialog}
      {/* The metric editor's width and heading, so the two sibling editors
          read as one family (MET-35); the shell pads the page (DS-3). */}
      <PageContainer width="narrow">
      {/* `noValidate`: rules are checked in `validate` and named inline, never
          by a browser bubble (AU-4). */}
      <form
        noValidate
        onSubmit={e => {
          e.preventDefault()
          void onSubmit()
        }}
      >
        <button
          type="button"
          onClick={onClose}
          className="mb-[14px] inline-flex items-center gap-1 text-caption transition-colors hover:text-[var(--fg)]"
          style={{ color: 'var(--fg-muted)' }}
        >
          <ChevronLeft size={14} /> Fact tables
        </button>
        <PageHeader
          className="mb-[18px]"
          eyebrow="Observe · Fact table"
          // The edited table is named, so two open editors are told apart (MT-31).
          title={
            factTable
              ? `${canWrite ? 'Edit' : 'Fact table'} · ${factTable.display_name}`
              : 'New fact table'
          }
        />
        {!canWrite && <ReadOnlyNotice className="mb-[18px]" />}

        {/* A viewer gets the definition read-only: `disabled` on a fieldset
            reaches every native control inside it. `contents` keeps it out of
            the layout. */}
        <fieldset disabled={!canWrite} className="contents">
          <SCard title="Details">
            <Field
              label="Display name"
              htmlFor="fact-display-name"
              required
              error={fieldErrors['fact-display-name']}
              announceError={false}
            >
              <TextInput
                id="fact-display-name"
                value={displayName}
                onChange={onDisplayNameChange}
                placeholder={examplePlaceholder('Orders')}
                aria-required
                {...errorAria(fieldErrors, 'fact-display-name')}
              />
            </Field>
            <Field
              label="Internal name"
              // After creation this row holds the name as text, not a control:
              // `false` names it as a group instead of pointing the label at a
              // generated id nothing carries (MET-35).
              htmlFor={isNew ? 'fact-name' : false}
              required={isNew}
              hint={isNew ? 'Stable identifier used by fact metrics.' : "Can't be changed after creation."}
              error={isNew ? fieldErrors['fact-name'] : undefined}
              announceError={false}
            >
              {isNew ? (
                <TextInput
                  id="fact-name"
                  value={name}
                  onChange={onNameChange}
                  mono
                  placeholder={examplePlaceholder('orders')}
                  aria-required
                  {...errorAria(fieldErrors, 'fact-name')}
                />
              ) : (
                <div className="mono text-body text-fg">
                  {name}
                </div>
              )}
            </Field>
            <Field label="Description" htmlFor="fact-description">
              <TextArea
                id="fact-description"
                value={description}
                onChange={setDescription}
                rows={2}
                placeholder="What does this fact table represent?"
              />
            </Field>
            {/* Palette swatches plus a custom picker, as on the metric form
                (MT-35). No single control for a <label>: the row names the group. */}
            <Field label="Color" htmlFor={false} last>
              <ColorSwatches value={color} onChange={setColor} inputId="fact-color" />
            </Field>
          </SCard>

          <SCard title="Source" description="The warehouse query this table reads. A read-only SELECT (WITH … SELECT works too).">
            <Field
              label="Data source"
              htmlFor="fact-data-source"
              required
              error={fieldErrors['fact-data-source']}
              announceError={false}
            >
              <NativeSelect
                id="fact-data-source"
                value={dataSourceId}
                onChange={setDataSourceId}
                options={dataSourceOptions}
                aria-required
                {...errorAria(fieldErrors, 'fact-data-source')}
              />
            </Field>
            <Field
              label="SQL"
              htmlFor="fact-sql"
              required
              stacked
              last
              error={fieldErrors['fact-sql']}
              announceError={false}
            >
              <SqlEditor
                id="fact-sql"
                ariaLabel="Fact table SQL"
                value={sql}
                onChange={setSql}
                // Visibly a comment, never a query already in the editor (MT-6).
                placeholder={sqlPlaceholder('A read-only SELECT over one table or view, e.g.', 'SELECT id, user_id, amount, created_at FROM orders')}
                dialect={selectedDataSource?.db_type}
                tables={sqlSchemaData?.tables}
                minHeight="160px"
                readOnly={!canWrite}
                ariaRequired
                ariaInvalid={errorAria(fieldErrors, 'fact-sql')['aria-invalid']}
                ariaDescribedBy={errorAria(fieldErrors, 'fact-sql')['aria-describedby']}
              />
            </Field>
          </SCard>

          <SCard
            title="Columns"
            description="Detected from your query. Mark ID columns (user, order) to count them distinct. Saving reads them again if the query changed."
          >
            <div className="px-4 py-[15px] border-b border-border-subtle">
              <button
                id="fact-preview-columns"
                type="button"
                onClick={runPreview}
                disabled={previewMut.isPending || !sql.trim() || !dataSourceId}
                className="inline-flex h-8 items-center gap-[6px] rounded-control border px-3 text-body-sm font-medium transition-colors hover:bg-[var(--surface-hover)] disabled:opacity-60 border-border text-fg"
              >
                {previewMut.isPending ? (
                  <Loader2 className="animate-spin" size={12} />
                ) : (
                  <Eye size={12} />
                )}
                Preview columns
              </button>

              {previewMut.isError && (
                <div id={PREVIEW_ERROR_ID} tabIndex={-1} className="mt-3 outline-none">
                  <ErrorState compact title="Could not preview columns" error={previewMut.error} />
                </div>
              )}

              {canWrite && !columnsAreCurrent && !previewMut.isPending && (
                <p className="mt-3 text-body-sm text-warning">
                  {autoPreviewPending
                    ? 'The SQL or data source changed. The columns are read again in a moment.'
                    : columns.length > 0
                      ? 'The SQL or data source changed since these columns were read. They refresh when you preview or save.'
                      : 'No columns yet. They are read from the SQL when you preview or save.'}
                </p>
              )}

              {columns.length > 0 && (
                <div className="mt-4">
                  {/* A header row says what the box marks: it is not "include
                      this column", it is "count this one distinct" (MT-20). */}
                  <div
                    aria-hidden="true"
                    className="mb-2 grid grid-cols-[minmax(0,1fr)_auto_72px] items-center gap-3 px-3 micro-label text-fg-tertiary"
                  >
                    <span>Column</span>
                    <span>Type</span>
                    <span
                      className="text-center"
                      title="Identifiers (user, session, order ids) can be counted distinct in fact metrics."
                    >
                      Identifier
                    </span>
                  </div>
                  <ul className="space-y-1.5" aria-label="Fact table columns">
                    {columns.map(column => (
                      <li
                        key={column.name}
                        className="grid grid-cols-[minmax(0,1fr)_auto_72px] items-center gap-3 rounded-md border px-3 py-2 border-border-subtle"
                      >
                        <span className="mono truncate text-body-sm text-fg">
                          {column.name}
                        </span>
                        <Chip tone={typeTone(column.type)} size="xs">
                          {column.type}
                        </Chip>
                        <span className="flex justify-center">
                          <input
                            type="checkbox"
                            checked={identifierColumns.includes(column.name)}
                            onChange={() => toggleIdentifier(column.name)}
                            aria-label={`Use ${column.name} as an identifier column`}
                            title="Identifiers (user, session, order ids) can be counted distinct in fact metrics."
                          />
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {unusedSuggestions.length > 0 && (
                <div className="mt-3 flex flex-wrap items-center gap-2 text-body-sm text-fg-tertiary">
                  <span>
                    Suggested identifiers:{' '}
                    <span className="mono text-fg">
                      {unusedSuggestions.join(', ')}
                    </span>
                  </span>
                  <Button type="button" variant="outline" size="xs" onClick={applySuggestions}>
                    Use suggestions
                  </Button>
                </div>
              )}
            </div>
            {/* After the preview that discovers the columns, not before it in
                the Source card, where it was free text asked too early (MT-19). */}
            <Field
              label="Timestamp column"
              htmlFor="fact-timestamp"
              required
              last
              hint={
                detectedTimestamp && detectedTimestamp === timestampColumn
                  ? `Detected: ${detectedTimestamp}, the query's only timestamp column. Used to bucket facts over time.`
                  : 'The column used to bucket facts over time. Preview columns to pick from what the query returns.'
              }
              error={fieldErrors['fact-timestamp']}
              announceError={false}
            >
              <ColumnSuggestInput
                id="fact-timestamp"
                value={timestampColumn}
                onChange={onTimestampChange}
                suggestions={timestampSuggestions}
                placeholder={examplePlaceholder('created_at')}
                aria-required
                {...errorAria(fieldErrors, 'fact-timestamp')}
              />
            </Field>
          </SCard>

          <SCard
            title="Row filters"
            description="Reusable named WHERE fragments fact metrics can apply."
          >
            <div className="px-4 py-[15px]">
              {rowFilters.length === 0 ? (
                <div className="text-body-sm text-fg-tertiary">
                  No row filters yet.
                </div>
              ) : (
                <>
                  {/* What each input holds: once filled, nothing said which was
                      the reusable name and which the condition (MT-22). The
                      inputs carry the same words in their names. */}
                  <div
                    aria-hidden="true"
                    className="mb-2 hidden grid-cols-[180px_minmax(0,1fr)_32px] gap-2 micro-label sm:grid text-fg-tertiary"
                  >
                    <span>Name</span>
                    <span>Condition (SQL WHERE)</span>
                  </div>
                  <ul className="space-y-3 sm:space-y-2" aria-label="Row filters">
                    {rowFilters.map((filter, index) => {
                      const nameId = rowFilterFieldId(filter.id, 'name')
                      const sqlId = rowFilterFieldId(filter.id, 'sql')
                      const rowError = fieldErrors[nameId] ?? fieldErrors[sqlId]
                      return (
                        <li key={filter.id}>
                          {/* A phone gets name + remove on one line and the SQL
                              condition full width under them: side by side, the
                              condition was under 80px wide at 375px (MET-20). */}
                          <div className="grid grid-cols-[minmax(0,1fr)_32px] items-start gap-2 sm:grid-cols-[180px_minmax(0,1fr)_32px]">
                            <div className="min-w-0">
                              <TextInput
                                id={nameId}
                                value={filter.name}
                                onChange={value => updateRowFilter(filter.id, { name: value })}
                                placeholder={examplePlaceholder('mobile_only')}
                                aria-label={`Row filter ${index + 1} name`}
                                {...errorAria(fieldErrors, nameId)}
                              />
                            </div>
                            <div className="col-span-2 row-start-2 min-w-0 sm:col-span-1 sm:col-start-2 sm:row-start-1">
                              {/* A WHERE fragment over this table: highlighted and
                                  completed against its columns, with no gutter or
                                  Format button per row (MT-22). */}
                              <SqlEditor
                                id={sqlId}
                                ariaLabel={`Row filter ${index + 1} SQL condition`}
                                value={filter.sql}
                                onChange={value => updateRowFilter(filter.id, { sql: value })}
                                placeholder={examplePlaceholder("platform = 'ios'")}
                                dialect={selectedDataSource?.db_type}
                                tables={rowFilterTables}
                                compact
                                readOnly={!canWrite}
                                ariaInvalid={errorAria(fieldErrors, sqlId)['aria-invalid']}
                                ariaDescribedBy={errorAria(fieldErrors, sqlId)['aria-describedby']}
                              />
                            </div>
                            <button
                              type="button"
                              onClick={() => removeRowFilter(filter.id)}
                              aria-label={`Remove row filter ${index + 1}`}
                              className="col-start-2 row-start-1 inline-flex h-8 w-8 items-center justify-center rounded-control border transition-colors hover:bg-[var(--surface-hover)] sm:col-start-3 border-border text-fg-secondary"
                            >
                              <Trash2 size={14} />
                            </button>
                          </div>
                          {rowError && (
                            <p
                              id={fieldErrorId(fieldErrors[nameId] ? nameId : sqlId)}
                              className="mt-[6px] text-body-sm leading-[1.45] text-danger"
                            >
                              {rowError}
                            </p>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                </>
              )}
              <button
                type="button"
                onClick={addRowFilter}
                className="mt-3 inline-flex h-8 items-center gap-[6px] rounded-control border px-3 text-body-sm font-medium transition-colors hover:bg-[var(--surface-hover)] border-border text-fg"
              >
                <Plus size={12} /> Add row filter
              </button>
            </div>
          </SCard>
        </fieldset>

        {errorEntries.length > 0 && (
          <div
            role="alert"
            className="mb-[18px] rounded-card border px-4 py-3 text-body-sm bg-danger-soft text-danger"
            style={{
              borderColor: 'color-mix(in oklab, var(--danger) 35%, var(--border))',
            }}
          >
            <ul className="list-disc space-y-1 pl-4">
              {errorEntries.map(([key, error]) => (
                <li key={key}>
                  <button
                    type="button"
                    onClick={() => focusField(key)}
                    className="text-left underline decoration-transparent underline-offset-2 hover:decoration-current focus-visible:decoration-current"
                  >
                    {error}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* The metrics that read this table (#257) — the same set the server
            refuses a delete or a breaking edit over. Fact tables are
            project-level, so main's graph. */}
        {factTable && (
          <UsedBySection
            className="mb-[18px]"
            slug={slug}
            entity={{ kind: 'fact_table', id: factTable.id }}
            branchId={null}
          />
        )}

        {saveMut.isError && (
          <div className="mb-[18px]">
            <ErrorState compact title="Could not save fact table" error={saveMut.error} />
          </div>
        )}

        {deleteMut.isError && (
          <div className="mb-[18px]">
            <ErrorState compact title="Could not delete fact table" error={deleteMut.error} />
          </div>
        )}

        {/* Sticky, so Save and the reason it is blocked stay on screen on a
            long form (MT-4). The status jumps to the first flagged field. */}
        <SaveBar
          status={attentionSummary(errorEntries.length)}
          statusTone="danger"
          onStatusClick={errorEntries[0] ? () => focusField(errorEntries[0]![0]) : undefined}
        >
          {canWrite && factTable && (
            // Bare red, as destructive actions on a detail page are (DS-20).
            <Button
              type="button"
              variant="danger"
              onClick={() => {
                void onDelete()
              }}
              disabled={busy}
              // On a phone Save leads, then Cancel, and this sits apart at the
              // foot instead of between them (MT-36).
              className="max-sm:order-3 max-sm:mt-2 max-sm:w-full"
            >
              {deleteMut.isPending ? (
                <Loader2 className="animate-spin" aria-hidden="true" />
              ) : (
                <Trash2 aria-hidden="true" />
              )}
              Delete fact table
            </Button>
          )}
          <Button type="button" variant="outline" onClick={onClose} className="max-sm:order-2 max-sm:w-full">
            {canWrite ? 'Cancel' : 'Close'}
          </Button>
          {canWrite && (
            <Button type="submit" disabled={busy} className="max-sm:order-1 max-sm:w-full">
              {saveMut.isPending || previewMut.isPending ? (
                <Loader2 className="animate-spin" aria-hidden="true" />
              ) : isNew ? (
                <Plus aria-hidden="true" />
              ) : (
                <Save aria-hidden="true" />
              )}
              {isNew ? 'Create fact table' : 'Save fact table'}
            </Button>
          )}
        </SaveBar>
      </form>
      </PageContainer>
    </div>
  )
}

const EMPTY_DATA_SOURCES: DataSource[] = []

/**
 * Route wrapper: loads the data the form needs (data sources, and — when
 * editing — the fact table itself) and renders the form full-page. Reached via
 * `/p/:slug/metrics/fact-tables/new` and
 * `/p/:slug/metrics/fact-tables/:factTableId/edit` (fact tables are a tab under
 * Metrics), so closing returns to the Fact tables tab.
 */
export default function FactTableEditPage() {
  const { slug, factTableId } = useParams<{ slug: string; factTableId?: string }>()
  const canWrite = useCanWriteProject()
  const navigate = useNavigate()
  const isNew = !factTableId

  const goBack = () => navigate(projectPath(currentOrgSlug(), slug, '/metrics/fact-tables'))

  const dataSourcesQuery = useQuery({
    queryKey: dataSourcesKey(),
    queryFn: () => dataSourcesApi.list(),
  })
  const factTableQuery = useQuery({
    queryKey: factTableKey(slug, factTableId),
    queryFn: () => factTablesApi.get(slug!, factTableId!),
    enabled: !!slug && !!factTableId,
  })

  // "New fact table" has nothing for a viewer to read (#237 MT-28).
  if (!canWrite && isNew && slug) return <Navigate to={projectPath(currentOrgSlug(), slug, '/metrics/fact-tables')} replace />

  // A deleted or unknown fact table is "not found" with the way back; only a
  // real failure keeps the retry (#237 SH-33).
  const loadError = dataSourcesQuery.error ?? factTableQuery.error
  if (loadError) {
    return (
      <PageContainer width="narrow">
        <QueryErrorState
          error={factTableQuery.error ?? loadError}
          title={isNew ? 'Could not load data sources' : 'Could not load this fact table'}
          onRetry={() => {
            void Promise.all([
              dataSourcesQuery.refetch(),
              ...(factTableId ? [factTableQuery.refetch()] : []),
            ])
          }}
          notFound={{
            title: 'Fact table not found',
            back: { to: projectPath(currentOrgSlug(), slug, '/metrics/fact-tables'), label: 'Back to fact tables' },
          }}
        />
      </PageContainer>
    )
  }

  const isLoading = dataSourcesQuery.isLoading || (!isNew && factTableQuery.isLoading)
  if (isLoading || !slug) {
    return (
      <PageContainer width="narrow">
        <PageSkeleton variant="form" label={isNew ? 'Loading fact table editor…' : 'Loading fact table…'} />
      </PageContainer>
    )
  }

  // A viewer reads the definition; a disabled form is not a read view.
  if (!canWrite && factTableQuery.data) {
    return (
      <FactTableReadView
        slug={slug}
        factTable={factTableQuery.data}
        dataSources={dataSourcesQuery.data ?? EMPTY_DATA_SOURCES}
        onClose={goBack}
      />
    )
  }

  return (
    <FactTableForm
      slug={slug}
      factTable={factTableQuery.data ?? null}
      dataSources={dataSourcesQuery.data ?? EMPTY_DATA_SOURCES}
      onClose={goBack}
    />
  )
}
