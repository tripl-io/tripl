import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Loader2, Play } from 'lucide-react'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { ColumnSuggestInput } from '@/components/column-suggest'
import { NoProjectDataSource } from '@/components/data-sources/no-project-data-source'
import { ErrorState } from '@/components/error-state'
import { FieldError } from '@/components/forms/FieldError'
import { DisabledReason, disabledReasonAria } from '@/components/states'
import { SqlEditor } from '@/components/sql-editor'
import { SCard, NativeSelect, type SelectOption, Field } from '@/components/settings/kit'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type {
  DataSource,
  MetricPreviewRequest,
  MetricPreviewResponse,
  MetricScanInterval,
} from '@/types'
import type { TableSchema } from '@/types/dataSourceSchema'
import { IntervalField } from './IntervalField'
import { MetricPreviewPanel } from './MetricPreviewPanel'
import { SqlComparePanel, type CompareSide } from './SqlComparePanel'
import { starterSql } from './metricTemplates'
import { examplePlaceholder, sqlPlaceholder } from '@/components/forms/placeholders'
import { errorAria, fieldErrorId, type FieldErrors } from '@/lib/fieldErrors'
import type { MetricDraft } from './metricDraft'

/** The saved SQL metric's query, compared against the draft's over one window. */
export interface SavedSqlQuery {
  dataSourceId: string | null
  sql: string
  timeColumn: string
  valueColumn: string | null
}

interface SqlDefinitionFieldsProps {
  slug: string
  /** Set only for an existing SQL metric; enables "Compare with saved". */
  savedQuery?: SavedSqlQuery | null
  draft: MetricDraft
  patch: (next: Partial<MetricDraft>) => void
  errors: FieldErrors
  canWrite: boolean
  dataSources: readonly DataSource[]
  /** The data-source list failed: shown here, the only card that needs it. */
  dataSourcesError?: unknown
  onRetryDataSources?: () => void
  onDataSourceChange: (id: string) => void
  onIntervalChange: (next: MetricScanInterval) => void
  clearedReplayChunk: MetricScanInterval | null
  schemaTables?: TableSchema[]
  /** Column names offered by the time/value inputs. */
  columnSuggestions: string[]
  /**
   * Columns the last clean preview returned, for the breakdown picker; `null`
   * once an edit invalidates that preview.
   */
  onPreviewColumns: (columns: string[] | null) => void
  /**
   * Whether the last preview failed (true) or no longer applies (false), so
   * Create can ask before saving a query that just errored.
   */
  onPreviewFailed?: (failed: boolean) => void
}

/**
 * The SQL metric's Source card (data source + interval) and Query card (SQL,
 * dry-run preview, time/value columns). The preview lives here because every
 * input it depends on does: editing any of them clears it.
 */
export function SqlDefinitionFields({
  slug,
  savedQuery,
  draft,
  patch,
  errors,
  canWrite,
  dataSources,
  dataSourcesError,
  onRetryDataSources,
  onDataSourceChange,
  onIntervalChange,
  clearedReplayChunk,
  schemaTables,
  columnSuggestions,
  onPreviewColumns,
  onPreviewFailed,
}: SqlDefinitionFieldsProps) {
  // Last dry-run result, for the inputs it ran against.
  const [preview, setPreview] = useState<MetricPreviewResponse | null>(null)
  // Saved vs draft over the same window; null until "Compare with saved" runs.
  const [compare, setCompare] = useState<{ saved: CompareSide; draft: CompareSide } | null>(null)
  const [comparing, setComparing] = useState(false)

  // Stateless dry-run of the current SQL config against the warehouse. User
  // mistakes come back as 200 with `error` set and render inside the result
  // panel; transport failures land in `previewMut.error` and render inline.
  const previewMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (payload: MetricPreviewRequest) => metricsCatalogApi.preview(slug, payload),
  })

  // Which input Preview still needs, said under the button rather than
  // leaving it silently grey.
  const previewBlocker = !draft.dataSourceId
    ? 'Pick a data source to preview.'
    : !draft.metricSql.trim()
      ? 'Write the query to preview it.'
      : !draft.sqlTimeColumn.trim()
        ? 'Name the time column to preview.'
        : null
  const canPreview = previewBlocker === null && !previewMut.isPending

  const onPreview = () => {
    setPreview(null)
    // The result is taken through the PER-CALL callback, not the options-level
    // one: TanStack drops per-call callbacks for a mutation `reset()` detached,
    // so a run still in flight when the SQL is edited can no longer paint its
    // result beside the edited query.
    previewMut.mutate(
      {
        data_source_id: draft.dataSourceId,
        sql: draft.metricSql,
        time_column: draft.sqlTimeColumn.trim(),
        value_column: draft.sqlValueColumn.trim() || null,
        interval: draft.interval,
      },
      {
        onSuccess: result => {
          setPreview(result)
          onPreviewFailed?.(!!result.error)
          if (!result.error && result.columns?.length) onPreviewColumns(result.columns)
        },
        onError: () => onPreviewFailed?.(true),
      },
    )
  }

  // The preview only describes the inputs it ran against; editing any of them
  // — the interval included, which the request carries — invalidates it.
  // The columns it returned go with it: the breakdown picker must not keep
  // offering what an edited query no longer projects.
  const resetPreview = () => {
    setPreview(null)
    setCompare(null)
    previewMut.reset()
    onPreviewColumns(null)
    onPreviewFailed?.(false)
  }

  // Dry-runs the saved query and the draft over the SAME data source, interval
  // and window. Each side settles on its own, so a timeout or error names the
  // side that failed instead of hiding the other. Nothing is saved.
  const compareBlocker =
    previewBlocker ??
    (!savedQuery
      ? 'Only a saved SQL metric can be compared.'
      : !savedQuery.dataSourceId
        ? 'The saved query has no data source to compare against.'
        : null)
  const canCompare = compareBlocker === null && !comparing

  const runSide = async (request: MetricPreviewRequest, label: string): Promise<CompareSide> => {
    try {
      const result = await metricsCatalogApi.preview(slug, request)
      return result.error
        ? { points: [], error: `${label}: ${result.error}` }
        : { points: result.points ?? [], error: null }
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'the request failed'
      const timedOut = /timed? ?out|timeout/i.test(message)
      return { points: [], error: timedOut ? `${label} timed out.` : `${label} failed: ${message}` }
    }
  }

  const onCompare = async () => {
    if (!savedQuery || !savedQuery.dataSourceId) return
    setComparing(true)
    setCompare(null)
    const timing = { interval: draft.interval }
    const [saved, draftSide] = await Promise.all([
      runSide(
        {
          data_source_id: savedQuery.dataSourceId,
          sql: savedQuery.sql,
          time_column: savedQuery.timeColumn,
          value_column: savedQuery.valueColumn,
          ...timing,
        },
        'Saved definition',
      ),
      runSide(
        {
          data_source_id: draft.dataSourceId,
          sql: draft.metricSql,
          time_column: draft.sqlTimeColumn.trim(),
          value_column: draft.sqlValueColumn.trim() || null,
          ...timing,
        },
        'Draft',
      ),
    ])
    setCompare({ saved, draft: draftSide })
    setComparing(false)
  }

  const dataSourceOptions: SelectOption[] = [
    { value: '', label: 'Select data source…' },
    ...dataSources.map(ds => ({ value: ds.id, label: ds.name })),
  ]
  // The list loaded and holds nothing this project may use: say so where the
  // select would be, rather than a select with only its placeholder.
  const noDataSources = dataSourcesError == null && dataSources.length === 0

  return (
    <>
      <SCard title="Source" description="Where the query runs, and how often.">
        <Field
          label="Data source"
          htmlFor={noDataSources ? false : 'metric-sql-data-source'}
          errorId={fieldErrorId('metric-sql-data-source')}
          required
          error={errors['metric-sql-data-source']}
          announceError={false}
        >
          {noDataSources ? (
            <NoProjectDataSource id="metric-sql-data-source" />
          ) : (
            <NativeSelect
              id="metric-sql-data-source"
              value={draft.dataSourceId}
              onChange={value => {
                resetPreview()
                onDataSourceChange(value)
              }}
              options={dataSourceOptions}
              aria-required
              {...errorAria(errors, 'metric-sql-data-source')}
            />
          )}
          {dataSourcesError != null && (
            <div className="mt-[8px]">
              <ErrorState
                compact
                title="Could not load data sources"
                error={dataSourcesError}
                onRetry={onRetryDataSources}
              />
            </div>
          )}
        </Field>
        <IntervalField
          id="metric-sql-interval"
          value={draft.interval}
          onChange={next => {
            resetPreview()
            onIntervalChange(next)
          }}
          replayChunkInterval={draft.replayChunkInterval}
          clearedReplayChunk={clearedReplayChunk}
        />
      </SCard>

      {/* The query itself, below the Source card that says where it runs. */}
      <SCard title="Query" description="A custom query returning one numeric value per bucket.">
        <Field
          label="Metric SQL"
          htmlFor="metric-sql-query"
          required
          stacked
        >
          <SqlEditor
            id="metric-sql-query"
            ariaLabel="Metric SQL"
            value={draft.metricSql}
            onChange={value => {
              resetPreview()
              patch({ metricSql: value })
            }}
            // A commented-out example in the selected warehouse's dialect: a
            // complete query as the placeholder read as SQL already in the
            // editor, so "SQL is required" looked wrong.
            placeholder={sqlPlaceholder(
              'Return a time column and a numeric value, e.g.',
              starterSql('event-volume', dataSources.find(ds => ds.id === draft.dataSourceId)?.db_type),
            )}
            dialect={dataSources.find(ds => ds.id === draft.dataSourceId)?.db_type}
            tables={schemaTables}
            minHeight="220px"
            readOnly={!canWrite}
            ariaRequired
            ariaInvalid={!!errors['metric-sql-query']}
            ariaDescribedBy={errors['metric-sql-query'] ? fieldErrorId('metric-sql-query') : undefined}
            // Right under the editor, not under the Preview row 60-100px
            // below, where it read like a preview failure.
            error={<FieldError inputId="metric-sql-query" message={errors['metric-sql-query']} />}
          />
          <div className="mt-[10px] flex flex-wrap items-center gap-[10px]">
            <button
              type="button"
              onClick={onPreview}
              disabled={!canPreview}
              {...disabledReasonAria('metric-sql-preview', previewBlocker)}
              className="inline-flex h-8 items-center gap-[6px] rounded-control border px-3 text-body-sm font-medium transition-colors hover:bg-[var(--surface-hover)] disabled:opacity-50 border-border text-fg-secondary"
            >
              {previewMut.isPending ? (
                <Loader2 className="animate-spin" size={12} />
              ) : (
                <Play size={12} />
              )}
              {previewMut.isPending ? 'Running…' : 'Preview'}
            </button>
            {previewBlocker ? (
              <DisabledReason id="metric-sql-preview" reason={previewBlocker} />
            ) : (
              <span className="text-caption text-fg-tertiary">
                Dry-run against the data source over recent buckets; nothing is saved.
              </span>
            )}
          </div>
          {previewMut.isError && (
            <div className="mt-[10px]">
              <ErrorState compact title="Preview failed" error={previewMut.error} />
            </div>
          )}
          {preview && <MetricPreviewPanel result={preview} color={draft.color} unit={draft.unit} />}
          {savedQuery && (
            <div className="mt-[10px] flex flex-wrap items-center gap-[10px]">
              <button
                type="button"
                onClick={onCompare}
                disabled={!canCompare}
                {...disabledReasonAria('metric-sql-compare', compareBlocker)}
                className="inline-flex h-8 items-center gap-[6px] rounded-control border px-3 text-body-sm font-medium transition-colors hover:bg-[var(--surface-hover)] disabled:opacity-50 border-border text-fg-secondary"
              >
                {comparing ? <Loader2 className="animate-spin" size={12} /> : null}
                {comparing ? 'Comparing…' : 'Compare with saved'}
              </button>
              {compareBlocker ? (
                <DisabledReason id="metric-sql-compare" reason={compareBlocker} />
              ) : (
                <span className="text-caption text-fg-tertiary">
                  Runs the saved and draft queries over the same window; nothing is saved.
                </span>
              )}
            </div>
          )}
          {compare && (
            <SqlComparePanel saved={compare.saved} draft={compare.draft} unit={draft.unit} />
          )}
        </Field>
        <Field
          label="Time column"
          htmlFor="metric-sql-time"
          required
          hint="The bucket/time column returned by the query."
          error={errors['metric-sql-time']}
          announceError={false}
        >
          <div className="max-w-[280px]">
            <ColumnSuggestInput
              id="metric-sql-time"
              value={draft.sqlTimeColumn}
              onChange={value => {
                resetPreview()
                patch({ sqlTimeColumn: value })
              }}
              suggestions={columnSuggestions}
              placeholder={examplePlaceholder('bucket')}
              aria-required
              {...errorAria(errors, 'metric-sql-time')}
            />
          </div>
        </Field>
        <Field
          label="Value column"
          htmlFor="metric-sql-value"
          last
          hint="The projected measure column. Defaults to value."
        >
          <div className="max-w-[280px]">
            <ColumnSuggestInput
              id="metric-sql-value"
              value={draft.sqlValueColumn}
              onChange={value => {
                resetPreview()
                patch({ sqlValueColumn: value })
              }}
              suggestions={columnSuggestions}
              placeholder={examplePlaceholder('value')}
            />
          </div>
        </Field>
      </SCard>
    </>
  )
}
