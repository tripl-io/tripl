import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { ErrorState } from '@/components/error-state'
import { Button } from '@/components/ui/button'
import { SCard, NativeSelect, type SelectOption, Field } from '@/components/settings/kit'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type { FactOperandPayload } from '@/lib/factOperandConfig'
import { METRIC_AGGREGATIONS, type MetricAggregation, type MetricScanInterval } from '@/types'
import { FactFilterEditor } from './FactFilterEditor'
import { IntervalField } from './IntervalField'
import { errorAria, type FieldErrors } from '@/lib/fieldErrors'
import {
  FACT_COMPOSITIONS,
  needsDistinct,
  needsMeasure,
  operandErrors,
  type FactComposition,
  type FactOperandState,
  type MetricDraft,
} from './metricDraft'
import { toOperandPayload, withAggregation, withFactTable } from './metricPayload'
import type { FactTableDetails, OperandDetailState } from './useFactTableDetails'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

const AGGREGATION_LABEL: Record<MetricAggregation, string> = {
  count: 'Count',
  sum: 'Sum',
  avg: 'Average',
  min: 'Min',
  max: 'Max',
  count_distinct: 'Count distinct',
}

function toOptions(prefix: string, items: { value: string; label: string }[]): SelectOption[] {
  return [{ value: '', label: prefix }, ...items]
}

interface FactOperandEditorProps {
  slug: string
  idPrefix: string
  /** 'numerator' / 'denominator' for a ratio side; empty for a single operand. */
  label: string
  operand: FactOperandState
  onChange: (next: FactOperandState) => void
  factTableOptions: SelectOption[]
  state: OperandDetailState
  errors: FieldErrors
}

/**
 * Point-and-click editor for one fact operand: pick the fact table, the
 * aggregation, the measure/distinct column it requires, and its filters. The
 * column / row-filter dropdowns are populated from the loaded fact table; for
 * `count_distinct`, identifier columns are surfaced first.
 *
 * The operand owns its own filter dry-run ("Check filters"): it POSTs the exact
 * payload a save would send, so the backend compiles and EXECUTES the same SQL
 * the collector will. Each operand of a ratio checks independently.
 */
function FactOperandEditor({
  slug,
  idPrefix,
  label,
  operand,
  onChange,
  factTableOptions,
  state,
  errors,
}: FactOperandEditorProps) {
  const { detail, loading } = state
  const detailError = state.error != null
  // Stateless dry-run of this operand's compiled row filter. Warehouse/compiler
  // rejections come back as a 200 with `error` set and render inside the panel;
  // a transport failure (404 fact table, 403) lands in `checkMut.error`.
  const checkMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (payload: FactOperandPayload) => metricsCatalogApi.previewFactOperand(slug, payload),
  })

  // The check describes the operand it ran against, so ANY edit to the operand
  // invalidates it — cleared here, in the only place the operand changes.
  const set = <K extends keyof FactOperandState>(key: K, value: FactOperandState[K]): void => {
    checkMut.reset()
    onChange({ ...operand, [key]: value })
  }

  const checkError = checkMut.error
    ? checkMut.error instanceof Error && checkMut.error.message
      ? checkMut.error.message
      : 'The filter check could not be run.'
    : null

  // The backend validates the whole operand before it compiles a filter, so an
  // incomplete one came back as a raw 422 about a missing measure column. Say
  // what to fix instead, and only offer the check once it can run (MET-33).
  const blockers = Object.values(operandErrors(operand, idPrefix, label))
  const checkBlockedReason = blockers.length > 0
    ? `Complete the operand before checking its filters — ${blockers[0]}`
    : undefined

  const columnOptions = useMemo(
    () =>
      toOptions(
        'Select column…',
        detail.columns.map(column => ({ value: column.name, label: `${column.name} · ${column.type}` })),
      ),
    [detail.columns],
  )

  // Prefer identifier columns for count_distinct, but still allow any column.
  const distinctOptions = useMemo(() => {
    const identifiers = new Set(detail.identifierColumns)
    const ordered = [
      ...detail.columns.filter(column => identifiers.has(column.name)),
      ...detail.columns.filter(column => !identifiers.has(column.name)),
    ]
    return toOptions(
      'Select column…',
      ordered.map(column => ({
        value: column.name,
        label: identifiers.has(column.name) ? `${column.name} · id` : column.name,
      })),
    )
  }, [detail.columns, detail.identifierColumns])

  const columnHint = loading ? 'Loading columns…' : undefined
  const columnsDisabled = !operand.factTableId || loading || detailError

  return (
    <>
      <Field
        label="Fact table"
        htmlFor={`${idPrefix}-table`}
        required
        error={errors[`${idPrefix}-table`]}
        announceError={false}
      >
        <NativeSelect
          id={`${idPrefix}-table`}
          value={operand.factTableId}
          onChange={value => {
            checkMut.reset()
            onChange(withFactTable(operand, value))
          }}
          options={factTableOptions}
          aria-required
          {...errorAria(errors, `${idPrefix}-table`)}
        />
      </Field>
      <Field label="Aggregation" htmlFor={`${idPrefix}-aggregation`} required>
        <NativeSelect
          id={`${idPrefix}-aggregation`}
          value={operand.aggregation}
          onChange={value => {
            checkMut.reset()
            onChange(withAggregation(operand, value as MetricAggregation))
          }}
          options={METRIC_AGGREGATIONS.map(a => ({ value: a, label: AGGREGATION_LABEL[a] }))}
        />
      </Field>
      {needsMeasure(operand.aggregation) && (
        <Field
          label="Measure column"
          htmlFor={`${idPrefix}-measure`}
          required
          hint={columnHint ?? 'Column to aggregate (numeric preferred).'}
          error={errors[`${idPrefix}-measure`]}
          announceError={false}
        >
          <NativeSelect
            id={`${idPrefix}-measure`}
            value={operand.measureColumn}
            onChange={value => set('measureColumn', value)}
            options={columnOptions}
            disabled={columnsDisabled}
            aria-required
            {...errorAria(errors, `${idPrefix}-measure`)}
          />
        </Field>
      )}
      {needsDistinct(operand.aggregation) && (
        <Field
          label="Distinct column"
          htmlFor={`${idPrefix}-distinct`}
          required
          hint={columnHint ?? 'Column whose distinct values are counted.'}
          error={errors[`${idPrefix}-distinct`]}
          announceError={false}
        >
          <NativeSelect
            id={`${idPrefix}-distinct`}
            value={operand.distinctColumn}
            onChange={value => set('distinctColumn', value)}
            options={distinctOptions}
            disabled={columnsDisabled}
            aria-required
            {...errorAria(errors, `${idPrefix}-distinct`)}
          />
        </Field>
      )}
      {/* A filter list plus two buttons, so there is no one control the label
          names — and with no filters yet (the default) the generated id
          addressed nothing at all. `false` names the row as a group instead. */}
      <Field
        label="Filters"
        htmlFor={false}
        last
        stacked
        hint="Optional. Only rows matching all filters are counted."
      >
        <FactFilterEditor
          filters={operand.filters}
          onChange={filters => set('filters', filters)}
          namedOptions={detail.rowFilters}
          conditionColumns={detail.columns}
          dialect={detail.dialect}
          tables={detail.tables}
          disabled={columnsDisabled}
          rowIdPrefix={idPrefix}
          errors={errors}
          // Keep the check visible but disabled while detail metadata is loading
          // (or failed): serializing before column types resolve changes numbers
          // into strings and makes the preview disagree with the eventual save.
          onCheck={
            operand.factTableId
              ? () => checkMut.mutate(toOperandPayload(operand, detail.columns))
              : undefined
          }
          checkBlockedReason={checkBlockedReason}
          checkPending={checkMut.isPending}
          checkResult={checkMut.data ?? null}
          checkError={checkError}
        />
      </Field>
    </>
  )
}

interface FactDefinitionFieldsProps {
  slug: string
  draft: MetricDraft
  patch: (next: Partial<MetricDraft>) => void
  errors: FieldErrors
  facts: FactTableDetails
  onIntervalChange: (next: MetricScanInterval) => void
  onFactCompositionChange: (next: FactComposition) => void
  clearedReplayChunk: MetricScanInterval | null
}

/** The fact metric's Fact card (composition + interval) and its operand card(s). */
export function FactDefinitionFields({
  slug,
  draft,
  patch,
  errors,
  facts,
  onIntervalChange,
  onFactCompositionChange,
  clearedReplayChunk,
}: FactDefinitionFieldsProps) {
  // With no fact table to point at, Calculate and Interval are dead ends:
  // the card says what to do instead, with the way there (MT-11).
  if (facts.noFactTables) {
    return (
      <SCard title="Fact table">
        <div
          className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-[15px] text-body-sm text-fg-tertiary"
        >
          <span>No fact tables yet. A fact metric aggregates one, so create it first.</span>
          <Button asChild size="sm" variant="outline">
            <Link to={projectPath(currentOrgSlug(), slug, '/metrics/fact-tables/new')} className="no-underline">
              <Plus aria-hidden="true" />
              New fact table
            </Link>
          </Button>
        </div>
      </SCard>
    )
  }

  return (
    <>
      <SCard title="Fact" description="Aggregate a reusable fact table into one value per bucket.">
        {/* "Calculate", not "Composition": the kind already says where the
            value comes from, this says how (MT-18). */}
        <Field
          label="Calculate"
          htmlFor="metric-fact-composition"
          required
        >
          <NativeSelect
            id="metric-fact-composition"
            value={draft.factComposition}
            onChange={value => onFactCompositionChange(value as FactComposition)}
            options={FACT_COMPOSITIONS.map(c => ({
              value: c,
              label: c === 'single' ? 'One aggregation' : 'Ratio of two aggregations (A ÷ B)',
            }))}
          />
        </Field>
        <IntervalField
          id="metric-fact-interval"
          value={draft.interval}
          onChange={onIntervalChange}
          replayChunkInterval={draft.replayChunkInterval}
          clearedReplayChunk={clearedReplayChunk}
        />
      </SCard>

      {/* Stacked, not side by side: every row inside is a kit Field, and a
          half-width card leaves its controls ~125px wide (tripl-vv2f). */}
      {draft.factComposition === 'single' ? (
        <SCard title="Aggregation">
          <FactOperandEditor
            slug={slug}
            idPrefix="metric-fact"
            label=""
            operand={draft.numeratorOp}
            onChange={next => patch({ numeratorOp: next })}
            factTableOptions={facts.factTableOptions}
            state={facts.numerator}
            errors={errors}
          />
        </SCard>
      ) : (
        <>
          <SCard title="Numerator">
            <FactOperandEditor
              slug={slug}
              idPrefix="metric-fact-num"
              label="numerator"
              operand={draft.numeratorOp}
              onChange={next => patch({ numeratorOp: next })}
              factTableOptions={facts.factTableOptions}
              state={facts.numerator}
              errors={errors}
            />
          </SCard>
          <SCard title="Denominator" description="May reference a different fact table.">
            <FactOperandEditor
              slug={slug}
              idPrefix="metric-fact-den"
              label="denominator"
              operand={draft.denominatorOp}
              onChange={next => patch({ denominatorOp: next })}
              factTableOptions={facts.factTableOptions}
              state={facts.denominator}
              errors={errors}
            />
          </SCard>
        </>
      )}

      {facts.error != null && (
        <div className="mb-[18px]">
          <ErrorState compact title="Could not load fact table details" error={facts.error} />
        </div>
      )}
    </>
  )
}
