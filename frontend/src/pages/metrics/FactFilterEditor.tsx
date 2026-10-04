import { CheckCircle2, Loader2, Play, Plus, Trash2 } from 'lucide-react'
import { ChipListInput } from '@/components/chip-list-input'
import { SqlEditor } from '@/components/sql-editor'
import { NativeSelect, TextInput, type SelectOption } from '@/components/settings/kit'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { factColumnValueKind } from '@/lib/factColumnValueKind'
import {
  FACT_CONDITION_OPERATORS,
  conditionOperatorsFor,
  isListConditionOperator,
} from '@/lib/factOperandConfig'
import type { DbType } from '@/types/dataSources'
import type { TableSchema } from '@/types/dataSourceSchema'
import type { FactOperandPreviewResponse } from '@/types/metricsCatalog'
import type { FactTableColumn } from '@/types/factTables'
import { formatNumber } from '@/lib/format'

import {
  VALUELESS_CONDITION_OPERATORS,
  makeConditionFilter,
  makeNamedFilter,
  makeSqlFilter,
  withConditionOperator,
  type FactConditionFilter,
  type FactConditionOperator,
  type FactFilter,
} from './factFilters'
import { errorAria, fieldErrorId, type FieldErrors } from '@/lib/fieldErrors'
import { filterFieldId } from './metricDraft'
import { sqlPlaceholder } from '@/components/forms/placeholders'

interface FactFilterEditorProps {
  filters: FactFilter[]
  onChange: (next: FactFilter[]) => void
  /** Named filters defined on the operand's fact table. */
  namedOptions: string[]
  /** Columns defined on the operand's fact table. */
  conditionColumns?: FactTableColumn[]
  dialect?: DbType
  tables?: TableSchema[]
  disabled?: boolean
  /**
   * Prefix of each row's DOM id ({@link filterFieldId}); the row's first
   * control carries it so a validation message can link to it and focus it.
   */
  rowIdPrefix?: string
  /** Validation messages keyed by {@link filterFieldId}. */
  errors?: FieldErrors
  /**
   * Run the filters against the warehouse (a stateless dry-run of the compiled
   * WHERE). Omitted while the operand has no fact table to compile against, in
   * which case the Check button is not rendered at all.
   */
  onCheck?: () => void
  /** Why the check cannot run yet; set, the button is disabled and says so. */
  checkBlockedReason?: string
  checkPending?: boolean
  /** Last dry-run outcome; `error` set means the warehouse (or the compiler) said no. */
  checkResult?: FactOperandPreviewResponse | null
  /** Transport-level failure (the check never reached a verdict). */
  checkError?: string | null
}

const MENU_ITEM_CLASS = 'text-body-sm'

/** Each row's visible type, beside its number. */
const FILTER_KIND_LABEL: Record<FactFilter['kind'], string> = {
  named: 'Named',
  condition: 'Where',
  sql: 'SQL',
}

/**
 * Point-and-click editor for an operand's filter list. An "Add filter" menu
 * appends a named filter, a structured condition or a free-text SQL fragment;
 * each row is independently editable and removable. SQL filters use the shared
 * {@link SqlEditor} so they get highlighting, completion + Format.
 *
 * The menu is the shared Radix dropdown: it portals out of the card's
 * `overflow-hidden`, follows its trigger on scroll, flips at the viewport edge,
 * and gives the arrow-key / Escape / focus behaviour its `menu` role promises —
 * the hand-rolled one before it had none of that.
 *
 * "Check filters" dry-runs the list against the warehouse. Until it existed, a
 * fact metric's filters were only ever executed inside a Celery worker — so a
 * filter that the selected engine rejects was saved happily and failed later,
 * out of sight. The check compiles the filters exactly as the collector does and
 * runs them, so a green check means the collection will not fail on this SQL.
 */
export function FactFilterEditor({
  filters,
  onChange,
  namedOptions,
  conditionColumns = [],
  dialect,
  tables,
  disabled,
  rowIdPrefix,
  errors,
  onCheck,
  checkBlockedReason,
  checkPending = false,
  checkResult = null,
  checkError = null,
}: FactFilterEditorProps) {
  const setName = (id: string, name: string): void =>
    onChange(filters.map(f => (f.id === id && f.kind === 'named' ? { ...f, name } : f)))
  const setSql = (id: string, sql: string): void =>
    onChange(filters.map(f => (f.id === id && f.kind === 'sql' ? { ...f, sql } : f)))
  const replaceCondition = (next: FactConditionFilter): void =>
    onChange(filters.map(f => (f.id === next.id ? next : f)))
  const remove = (id: string): void => onChange(filters.filter(f => f.id !== id))
  const add = (filter: FactFilter): void => onChange([...filters, filter])

  const columnKind = (column: string) => {
    const match = conditionColumns.find(candidate => candidate.name === column)
    return match ? factColumnValueKind(match.type) : null
  }
  // Picking a column whose type rules out the current operator (`contains` on
  // a number) falls back to `=` rather than keeping a condition the backend
  // or the warehouse will reject.
  const setConditionColumn = (filter: FactConditionFilter, column: string): void => {
    const allowed = conditionOperatorsFor(columnKind(column)).some(
      meta => meta.value === filter.operator,
    )
    replaceCondition(allowed ? { ...filter, column } : withConditionOperator({ ...filter, column }, 'eq'))
  }

  const namedSelectOptions: SelectOption[] = [
    { value: '', label: 'Select filter…' },
    ...namedOptions.map(name => ({ value: name, label: name })),
  ]
  const conditionColumnOptions: SelectOption[] = [
    { value: '', label: 'Select column…' },
    ...conditionColumns.map(column => ({
      value: column.name,
      label: `${column.name} · ${column.type}`,
    })),
  ]
  const checkHintId = rowIdPrefix ? `${rowIdPrefix}-check-hint` : undefined

  return (
    <div className="flex flex-col gap-2">
      {filters.length > 0 && (
        <ul className="flex flex-col gap-2" aria-label="Operand filters">
          {filters.map((filter, index) => {
            const rowId = rowIdPrefix ? filterFieldId(rowIdPrefix, filter.id) : undefined
            const error = rowId ? errors?.[rowId] : undefined
            const aria = rowId ? errorAria(errors, rowId) : {}
            return (
              <li key={filter.id} className="flex flex-col gap-1">
                {/* Rows are ANDed, and numbered so "Filter 2: …" in an error
                    points at a row the reader can find. The controls
                    carry the same number in their names. */}
                {index > 0 && (
                  <span aria-hidden="true" className="micro-label text-fg-tertiary">
                    and
                  </span>
                )}
                <div className="flex items-start gap-2">
                  <span
                    aria-hidden="true"
                    className="flex h-8 w-[52px] shrink-0 items-center gap-1 text-caption text-fg-tertiary"
                  >
                    <span className="tnum">{index + 1}</span>
                    <span>{FILTER_KIND_LABEL[filter.kind]}</span>
                  </span>
                  <div className="min-w-0 flex-1">
                    {filter.kind === 'named' ? (
                      <NativeSelect
                        // Fills the row's cell; the kit's 280px cap left a
                        // hole in the middle of it.
                        width="fill"
                        id={rowId}
                        value={filter.name}
                        onChange={value => setName(filter.id, value)}
                        options={namedSelectOptions}
                        disabled={disabled}
                        aria-label={`Filter ${index + 1} named filter`}
                        {...aria}
                      />
                    ) : filter.kind === 'condition' ? (
                      <ConditionRow
                        id={rowId}
                        index={index}
                        filter={filter}
                        columnOptions={conditionColumnOptions}
                        columnKind={columnKind(filter.column)}
                        disabled={disabled}
                        aria={aria}
                        onColumn={column => setConditionColumn(filter, column)}
                        onChange={replaceCondition}
                      />
                    ) : (
                      <SqlEditor
                        id={rowId}
                        ariaLabel={`Filter ${index + 1} SQL`}
                        value={filter.sql}
                        onChange={value => setSql(filter.id, value)}
                        placeholder={sqlPlaceholder("A condition on this fact table's columns, e.g.", "status = 'completed' AND amount > 0")}
                        dialect={dialect}
                        tables={tables}
                        // A WHERE fragment: no gutter, Format or table
                        // browser per row; completion still works.
                        compact
                        readOnly={disabled}
                        ariaInvalid={aria['aria-invalid']}
                        ariaDescribedBy={aria['aria-describedby']}
                      />
                    )}
                  </div>
                  <button
                    type="button"
                    onClick={() => remove(filter.id)}
                    aria-label={`Remove filter ${index + 1}`}
                    className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-control border transition-colors hover:bg-[var(--surface-hover)] border-border text-fg-secondary"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
                {error && rowId && (
                  <p
                    id={fieldErrorId(rowId)}
                    className="text-body-sm leading-[1.45] text-danger"
                  >
                    {error}
                  </p>
                )}
              </li>
            )
          })}
        </ul>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <DropdownMenu>
          <DropdownMenuTrigger asChild disabled={disabled}>
            <button
              type="button"
              disabled={disabled}
              className="inline-flex h-8 items-center gap-[6px] rounded-control border px-3 text-body-sm font-medium transition-colors hover:bg-[var(--surface-hover)] disabled:opacity-60 border-border text-fg"
            >
              <Plus size={12} /> Add filter
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="min-w-[160px]" aria-label="Add filter">
            {namedOptions.length > 0 && (
              <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => add(makeNamedFilter())}>
                Named filter
              </DropdownMenuItem>
            )}
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => add(makeConditionFilter())}>
              Condition
            </DropdownMenuItem>
            <DropdownMenuItem className={MENU_ITEM_CLASS} onSelect={() => add(makeSqlFilter())}>
              SQL filter
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
        {onCheck && (
          <button
            type="button"
            disabled={disabled || checkPending || !!checkBlockedReason}
            onClick={onCheck}
            aria-describedby={checkBlockedReason ? checkHintId : undefined}
            className="inline-flex h-8 items-center gap-[6px] rounded-control border px-3 text-body-sm font-medium transition-colors hover:bg-[var(--surface-hover)] disabled:opacity-60 border-border text-fg"
          >
            {checkPending ? (
              <Loader2 size={12} className="animate-spin" />
            ) : (
              <Play size={12} />
            )}
            {checkPending ? 'Checking…' : 'Check filters'}
          </button>
        )}
      </div>
      {onCheck && checkBlockedReason && !disabled && (
        <p id={checkHintId} className="text-caption text-fg-tertiary">
          {checkBlockedReason}
        </p>
      )}

      <FilterCheckPanel result={checkResult} transportError={checkError} />
    </div>
  )
}

interface ConditionRowProps {
  id?: string
  index: number
  filter: FactConditionFilter
  columnOptions: SelectOption[]
  columnKind: ReturnType<typeof factColumnValueKind> | null
  disabled?: boolean
  aria: ReturnType<typeof errorAria>
  onColumn: (column: string) => void
  onChange: (next: FactConditionFilter) => void
}

/**
 * Column / operator / value for one condition. Operators are narrowed to the
 * column's type, and `in` / `not in` take one chip per value, so a value that
 * itself contains a comma can be matched.
 */
function ConditionRow({
  id,
  index,
  filter,
  columnOptions,
  columnKind,
  disabled,
  aria,
  onColumn,
  onChange,
}: ConditionRowProps) {
  // The row's own operator is always listed, even when the column's type does
  // not suit it (a stored `contains` on a number column): a select whose value
  // is not among its options paints the first option instead, so the row would
  // claim `=` while the metric filters with something else.
  const fitting = new Set(conditionOperatorsFor(columnKind).map(meta => meta.value))
  const operatorOptions: SelectOption[] = FACT_CONDITION_OPERATORS.filter(
    meta => fitting.has(meta.value) || meta.value === filter.operator,
  ).map(meta => ({
    value: meta.value,
    label: fitting.has(meta.value) ? meta.label : `${meta.label} (not typical for this column)`,
  }))
  const takesValue = !VALUELESS_CONDITION_OPERATORS.has(filter.operator)
  return (
    <div className="grid grid-cols-1 gap-2 sm:grid-cols-[minmax(150px,1fr)_120px_minmax(150px,1fr)]">
      <NativeSelect
        width="fill"
        id={id}
        value={filter.column}
        onChange={onColumn}
        options={columnOptions}
        disabled={disabled}
        aria-label={`Filter ${index + 1} condition column`}
        {...aria}
      />
      <NativeSelect
        width="fill"
        value={filter.operator}
        onChange={value =>
          onChange(withConditionOperator(filter, value as FactConditionOperator))
        }
        options={operatorOptions}
        disabled={disabled}
        aria-label={`Filter ${index + 1} condition operator`}
      />
      {takesValue && (isListConditionOperator(filter.operator) ? (
        <ChipListInput
          values={filter.values}
          onChange={values => onChange({ ...filter, values })}
          placeholder="Add a value, press Enter"
          ariaLabel={`Filter ${index + 1} condition values`}
        />
      ) : (
        <TextInput
          value={filter.value}
          onChange={value => onChange({ ...filter, value })}
          placeholder="Value"
          disabled={disabled}
          aria-label={`Filter ${index + 1} condition value`}
        />
      ))}
    </div>
  )
}

interface FilterCheckPanelProps {
  result: FactOperandPreviewResponse | null
  transportError: string | null
}

/**
 * Outcome of the filter dry-run. A compiler or warehouse rejection arrives as a
 * 200 with `error` set (it is a verdict on the user's filters, not a server
 * fault) and renders in the danger style with the engine's own wording; a
 * transport failure renders the same way. A clean run says so explicitly —
 * "no rows matched" is NOT an error, so it is called out separately, since valid
 * SQL that matches nothing is a filter-value mistake, not a SQL mistake.
 */
function FilterCheckPanel({ result, transportError }: FilterCheckPanelProps) {
  const message = transportError ?? result?.error ?? null
  if (message) {
    return (
      <div
        role="alert"
        className="rounded-card border px-4 py-3 text-body-sm bg-danger-soft text-danger"
        style={{
          borderColor: 'color-mix(in oklab, var(--danger) 35%, var(--border))',
        }}
      >
        {message}
      </div>
    )
  }
  if (!result) return null
  return (
    <div
      role="status"
      className="flex items-center gap-[6px] rounded-card border px-4 py-3 text-body-sm border-border text-fg-secondary"
    >
      <CheckCircle2 size={14} style={{ color: 'var(--success, var(--fg-muted))' }} />
      {result.row_count > 0
        ? `Filters ran clean against the warehouse and matched ${formatNumber(result.row_count)} ${result.row_count === 1 ? 'row' : 'rows'} in the last 7 days.`
        : 'Filters ran clean against the warehouse, but matched no rows in the last 7 days.'}
    </div>
  )
}
