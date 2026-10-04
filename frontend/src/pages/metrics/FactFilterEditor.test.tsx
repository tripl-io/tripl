import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createElement, useState } from 'react'
import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { FactOperandPreviewResponse } from '@/types/metricsCatalog'
import { FactFilterEditor } from './FactFilterEditor'
import {
  filterRowErrors,
  filtersFromConfig,
  filtersToPayload,
  makeConditionFilter,
  makeNamedFilter,
  makeSqlFilter,
  splitAndedFragments,
  stripRedundantOuterParens,
  type FactFilter,
} from './factFilters'
import type { FactOperandConfig } from '@/lib/factOperandConfig'

// Radix drives the dropdown through pointer-capture APIs jsdom omits.
beforeAll(() => {
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.setPointerCapture = vi.fn()
  Element.prototype.releasePointerCapture = vi.fn()
})

/** The editor's config view, built from the loose shape the old signature took. */
function config(
  rowFilters: string[],
  filterSql: string,
  conditions: FactOperandConfig['conditions'] = [],
): Pick<FactOperandConfig, 'rowFilters' | 'conditions' | 'filterSql'> {
  return { rowFilters, conditions, filterSql: filterSql || null }
}

// CodeMirror needs real layout measurement jsdom cannot provide; the SQL-filter
// row is not under test here, so stub the editor with a plain textarea.
vi.mock('@/components/sql-editor', () => ({
  SqlEditor: ({
    ariaLabel,
    value,
    onChange,
    readOnly,
  }: {
    ariaLabel?: string
    value: string
    onChange: (value: string) => void
    readOnly?: boolean
  }) =>
    createElement('textarea', {
      'aria-label': ariaLabel,
      value,
      readOnly,
      onChange: (event: { target: { value: string } }) => onChange(event.target.value),
    }),
}))

const clean = (rowCount: number): FactOperandPreviewResponse => ({
  columns: ['created_at', 'amount'],
  row_count: rowCount,
  error: null,
})

// The filter dry-run: until it existed, a fact metric's filters were first
// executed inside a Celery worker, so a filter the selected warehouse rejects
// was saved happily and failed later, where nobody could see it.
describe('FactFilterEditor filter check', () => {
  const renderEditor = (props: Partial<Parameters<typeof FactFilterEditor>[0]> = {}) =>
    render(
      <FactFilterEditor
        filters={[]}
        onChange={vi.fn()}
        namedOptions={['exclude_test']}
        {...props}
      />,
    )

  it('offers no check when there is nothing to compile against (no onCheck)', () => {
    renderEditor()
    expect(screen.queryByRole('button', { name: /check filters/i })).toBeNull()
  })

  it('runs the check on click', () => {
    const onCheck = vi.fn()
    renderEditor({ onCheck })
    fireEvent.click(screen.getByRole('button', { name: /check filters/i }))
    expect(onCheck).toHaveBeenCalledTimes(1)
  })

  it('disables the button while the check is in flight', () => {
    renderEditor({ onCheck: vi.fn(), checkPending: true })
    const button = screen.getByRole('button', { name: /checking/i })
    expect(button).toBeDisabled()
  })

  it("surfaces the warehouse's own rejection verbatim as an alert", () => {
    // A 200 with `error` set: a verdict on the user's filter, not a server fault.
    renderEditor({
      onCheck: vi.fn(),
      checkResult: {
        columns: [],
        row_count: 0,
        error: "Code: 47. DB::Exception: Missing columns: 'amont'",
      },
    })
    expect(screen.getByRole('alert')).toHaveTextContent("Missing columns: 'amont'")
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('reports a clean run', () => {
    renderEditor({ onCheck: vi.fn(), checkResult: clean(1) })
    expect(screen.getByRole('status')).toHaveTextContent(/ran clean against the warehouse/i)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says how many rows the filters matched', () => {
    renderEditor({ onCheck: vi.fn(), checkResult: clean(12340) })
    expect(screen.getByRole('status')).toHaveTextContent('matched 12,340 rows in the last 7 days')
  })

  it('distinguishes "valid SQL, but matched nothing" from a failure', () => {
    renderEditor({ onCheck: vi.fn(), checkResult: clean(0) })
    const status = screen.getByRole('status')
    expect(status).toHaveTextContent(/ran clean/i)
    expect(status).toHaveTextContent(/matched no rows/i)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('renders a transport failure as an alert too', () => {
    renderEditor({ onCheck: vi.fn(), checkError: 'Fact table not found' })
    expect(screen.getByRole('alert')).toHaveTextContent('Fact table not found')
  })

  it('shows no panel before the first check', () => {
    renderEditor({ onCheck: vi.fn() })
    expect(screen.queryByRole('alert')).toBeNull()
    expect(screen.queryByRole('status')).toBeNull()
  })
})

const namedNames = (filters: FactFilter[]): string[] =>
  filters
    .filter((f): f is Extract<FactFilter, { kind: 'named' }> => f.kind === 'named')
    .map(f => f.name)
const conditions = (filters: FactFilter[]): Extract<FactFilter, { kind: 'condition' }>[] =>
  filters.filter(
    (f): f is Extract<FactFilter, { kind: 'condition' }> => f.kind === 'condition',
  )

describe('filtersToPayload', () => {
  it('maps named filters to row_filters, deduped and order-preserved', () => {
    expect(
      filtersToPayload([makeNamedFilter('a'), makeNamedFilter('b'), makeNamedFilter('a')]),
    ).toEqual({ row_filters: ['a', 'b'], filter_sql: null, conditions: [] })
  })

  it('parenthesises and ANDs free-text SQL fragments', () => {
    expect(filtersToPayload([makeSqlFilter('x = 1'), makeSqlFilter('y > 0')])).toEqual({
      row_filters: [],
      filter_sql: '(x = 1) AND (y > 0)',
      conditions: [],
    })
  })

  it('combines named + SQL and drops blank entries', () => {
    expect(
      filtersToPayload([
        makeNamedFilter('a'),
        makeNamedFilter(''),
        makeSqlFilter('   '),
        makeSqlFilter('z = 2'),
      ]),
    ).toEqual({ row_filters: ['a'], filter_sql: 'z = 2', conditions: [] })
  })

  it('stores a single SQL fragment verbatim (collection wraps it before ANDing)', () => {
    expect(filtersToPayload([makeSqlFilter("platform = 'ios'")]).filter_sql).toBe(
      "platform = 'ios'",
    )
  })

  it('maps structured conditions and omits incomplete rows', () => {
    expect(
      filtersToPayload([
        makeConditionFilter('amount', 'gt', '3'),
        makeConditionFilter('user_id', 'is_not_null'),
        makeConditionFilter('', 'eq', 'x'),
        makeConditionFilter('country', 'eq', '   '),
      ]),
    ).toEqual({
      row_filters: [],
      filter_sql: null,
      conditions: [
        { column: 'amount', operator: 'gt', value: '3' },
        { column: 'user_id', operator: 'is_not_null' },
      ],
    })
  })

  it('serializes condition values from the selected warehouse column type', () => {
    expect(
      filtersToPayload(
        [
          makeConditionFilter('amount', 'gt', '3.5'),
          makeConditionFilter('quantity', 'in', ['1', '2', '3']),
          makeConditionFilter('is_trial', 'eq', 'true'),
          makeConditionFilter('postal_code', 'eq', '00123'),
        ],
        [
          { name: 'amount', type: 'number' },
          { name: 'quantity', type: 'number' },
          { name: 'is_trial', type: 'bool' },
          { name: 'postal_code', type: 'string' },
        ],
      ).conditions,
    ).toEqual([
      { column: 'amount', operator: 'gt', value: 3.5 },
      { column: 'quantity', operator: 'in', value: [1, 2, 3] },
      { column: 'is_trial', operator: 'eq', value: true },
      { column: 'postal_code', operator: 'eq', value: '00123' },
    ])
  })

  it('keeps each IN value whole, commas included', () => {
    expect(
      filtersToPayload(
        [makeConditionFilter('customer', 'in', ['Smith, John', 'Doe, Jane'])],
        [{ name: 'customer', type: 'string' }],
      ).conditions,
    ).toEqual([{ column: 'customer', operator: 'in', value: ['Smith, John', 'Doe, Jane'] }])
  })
})

describe('filtersFromConfig', () => {
  it('reads a row_filters array plus filter_sql', () => {
    const filters = filtersFromConfig(config(['a', 'b'], 'x = 1'))
    expect(filters.map(f => f.kind)).toEqual(['named', 'named', 'sql'])
    expect(namedNames(filters)).toEqual(['a', 'b'])
  })

  it('reads structured conditions before filter_sql', () => {
    const filters = filtersFromConfig(
      config(['a'], 'x = 1', [
        { column: 'amount', operator: 'gt', value: '3' },
        { column: 'user_id', operator: 'is_not_null' },
      ]),
    )
    expect(filters.map(f => f.kind)).toEqual(['named', 'condition', 'condition', 'sql'])
    expect(conditions(filters)).toMatchObject([
      { column: 'amount', operator: 'gt', value: '3' },
      { column: 'user_id', operator: 'is_not_null', value: '' },
    ])
  })

  it('reads list values back into one chip each', () => {
    const filters = filtersFromConfig(
      config([], '', [{ column: 'customer', operator: 'in', value: ['Smith, John', 'Doe'] }]),
    )
    expect(conditions(filters)).toMatchObject([
      { operator: 'in', values: ['Smith, John', 'Doe'], value: '' },
    ])
  })

  it('splits SQL fragments it joined itself back into separate rows', () => {
    const saved = filtersToPayload([makeSqlFilter('a = 1 OR b = 2'), makeSqlFilter('c = 3')])
    const filters = filtersFromConfig(config([], saved.filter_sql!))
    expect(filters).toMatchObject([
      { kind: 'sql', sql: 'a = 1 OR b = 2' },
      { kind: 'sql', sql: 'c = 3' },
    ])
  })

  it('returns an empty list for empty config', () => {
    expect(filtersFromConfig(config([], ''))).toEqual([])
  })
})

// The metric edit page loads `filter_sql` back into the editor with
// `filtersFromConfig` and re-serialises it with `filtersToPayload` on save.
// That round trip MUST be a fixed point — the historical bug was
// serialize(parse(x)) === `(${x})`, so every open→save of the edit form grew
// the stored expression by one paren layer.
describe('filter_sql round-trip idempotency', () => {
  const roundTrip = (filterSql: string): string | null =>
    filtersToPayload(filtersFromConfig(config([], filterSql))).filter_sql

  it('is a fixed point: saving a loaded filter_sql never adds parentheses', () => {
    const first = filtersToPayload([makeSqlFilter("platform = 'ios'")]).filter_sql
    expect(first).not.toBeNull()
    const second = roundTrip(first!)
    expect(second).toBe(first)
    expect(roundTrip(second!)).toBe(second)
  })

  it('keeps multi-fragment AND grouping stable across round trips', () => {
    const first = filtersToPayload([
      makeSqlFilter('a = 1 OR b = 2'),
      makeSqlFilter('c = 3'),
    ]).filter_sql
    // The OR fragment stays parenthesised so ANDing cannot change precedence.
    expect(first).toBe('(a = 1 OR b = 2) AND (c = 3)')
    expect(roundTrip(first!)).toBe(first)
  })

  // Stripping them on load used to be a "self-heal", but any change to the
  // stored filter_sql deletes the metric's history on save: an
  // untouched load→save now sends it back exactly.
  it('keeps stored redundant parens verbatim instead of rewriting them', () => {
    const polluted = "((((platform = 'ios'))))"
    expect(filtersFromConfig(config([], polluted))).toMatchObject([{ kind: 'sql', sql: polluted }])
    expect(roundTrip(polluted)).toBe(polluted)
  })

  it('splits a stored string only when it is exactly the join the form writes', () => {
    expect(filtersFromConfig(config([], '(a = 1) AND (b = 2)'))).toMatchObject([
      { kind: 'sql', sql: 'a = 1' },
      { kind: 'sql', sql: 'b = 2' },
    ])
    for (const stored of ['(a = 1) and (b = 2)', '(a = 1)\nAND (b = 2)', '((a = 1)) AND (b = 2)']) {
      expect(filtersFromConfig(config([], stored))).toMatchObject([{ kind: 'sql', sql: stored }])
      expect(roundTrip(stored)).toBe(stored)
    }
  })

  it('never strips parens that affect AND/OR evaluation order', () => {
    // The leading paren closes before the end of the string: NOT a redundant
    // outer wrap, so unwrapping it would change how AND binds against OR.
    const mixed = "(status = 'a' OR status = 'b') AND amount > 5"
    expect(filtersFromConfig(config([], mixed))).toMatchObject([{ kind: 'sql', sql: mixed }])
    expect(roundTrip(mixed)).toBe(mixed)
  })

  it('ignores parentheses inside quoted literals when unwrapping', () => {
    expect(stripRedundantOuterParens("(name = '(nested)')")).toBe("name = '(nested)'")
    // Two rows are each unwrapped before they are joined, so none gains a layer.
    expect(
      filtersToPayload([makeSqlFilter("(name = '(nested)')"), makeSqlFilter('x = 1')]).filter_sql,
    ).toBe("(name = '(nested)') AND (x = 1)")
    // A stray closing paren inside a literal must not fool the scanner.
    const literal = "note = ')'"
    expect(roundTrip(literal)).toBe(literal)
  })
})

describe('splitAndedFragments', () => {
  it('splits only a string made entirely of parenthesised groups joined by AND', () => {
    expect(splitAndedFragments('(a = 1) AND (b = 2)')).toEqual(['a = 1', 'b = 2'])
    expect(splitAndedFragments('(a) and (b) AND (c)')).toEqual(['a', 'b', 'c'])
    // A user's own mixed expression stays one row.
    expect(splitAndedFragments('(a = 1) AND b = 2')).toEqual(['(a = 1) AND b = 2'])
    // AND inside a group or a literal is not a split point.
    expect(splitAndedFragments("(a = 'x AND y')")).toEqual(["(a = 'x AND y')"])
    expect(splitAndedFragments('')).toEqual([])
  })
})

// A row the payload cannot express used to be dropped on save, so the metric
// saved LESS filtered than the editor showed.
describe('filterRowErrors', () => {
  it('names every incomplete row by id and passes complete ones', () => {
    const named = makeNamedFilter()
    const sql = makeSqlFilter('  ')
    const noColumn = makeConditionFilter('', 'eq', 'x')
    const noValue = makeConditionFilter('country', 'eq', ' ')
    const noValues = makeConditionFilter('country', 'in', [])
    const valueless = makeConditionFilter('user_id', 'is_null')
    const complete = makeConditionFilter('amount', 'gt', '3')
    const errors = filterRowErrors([named, sql, noColumn, noValue, noValues, valueless, complete])
    expect(Object.keys(errors).sort()).toEqual(
      [named.id, sql.id, noColumn.id, noValue.id, noValues.id].sort(),
    )
    expect(errors[named.id]).toMatch(/Pick a named filter/)
    expect(errors[noValue.id]).toMatch(/Enter a value/)
  })
})

function StatefulEditor(props: Partial<Parameters<typeof FactFilterEditor>[0]>) {
  const [filters, setFilters] = useState<FactFilter[]>(props.filters ?? [])
  return (
    <FactFilterEditor
      namedOptions={['exclude_test']}
      conditionColumns={[
        { name: 'amount', type: 'number' },
        { name: 'country', type: 'string' },
        { name: 'is_trial', type: 'bool' },
      ]}
      {...props}
      filters={filters}
      onChange={setFilters}
    />
  )
}

describe('FactFilterEditor Add filter menu', () => {
  it('is a real menu: keyboard opens it, focus moves in, Escape closes it', async () => {
    render(<StatefulEditor />)
    const trigger = screen.getByRole('button', { name: 'Add filter' })
    fireEvent.keyDown(trigger, { key: 'Enter' })

    const menu = await screen.findByRole('menu')
    expect(within(menu).getByRole('menuitem', { name: 'Condition' })).toBeInTheDocument()
    await waitFor(() => expect(menu).toContainElement(document.activeElement as HTMLElement))

    fireEvent.keyDown(menu, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull())
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
  })

  it('appends the picked kind of filter', async () => {
    render(<StatefulEditor />)
    fireEvent.keyDown(screen.getByRole('button', { name: 'Add filter' }), { key: 'Enter' })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'SQL filter' }))
    expect(screen.getByLabelText('Filter 1 SQL')).toBeInTheDocument()
  })
})

describe('FactFilterEditor conditions', () => {
  const operatorLabels = () =>
    Array.from(
      (screen.getByLabelText('Filter 1 condition operator') as HTMLSelectElement).options,
    ).map(option => option.textContent)

  it('offers only the operators that fit the column type', () => {
    render(<StatefulEditor filters={[makeConditionFilter()]} />)
    const column = screen.getByLabelText('Filter 1 condition column')

    fireEvent.change(column, { target: { value: 'amount' } })
    expect(operatorLabels()).toContain('>')
    expect(operatorLabels()).not.toContain('contains')
    expect(operatorLabels()).not.toContain('is true')

    fireEvent.change(column, { target: { value: 'is_trial' } })
    expect(operatorLabels()).toContain('is true')
    expect(operatorLabels()).not.toContain('>')
  })

  it('still shows a stored operator the column type does not suit', () => {
    render(<StatefulEditor filters={[makeConditionFilter('amount', 'contains', '3')]} />)
    const operator = screen.getByLabelText('Filter 1 condition operator')
    expect(operator).toHaveValue('contains')
    expect(operator).toHaveDisplayValue('contains (not typical for this column)')
    // Only the row's own operator is added; other text operators stay out.
    expect(operatorLabels()).not.toContain('like')
  })

  it('falls back to "=" when the new column rules the operator out', () => {
    render(<StatefulEditor filters={[makeConditionFilter('country', 'contains', 'x')]} />)
    fireEvent.change(screen.getByLabelText('Filter 1 condition column'), {
      target: { value: 'amount' },
    })
    expect(screen.getByLabelText('Filter 1 condition operator')).toHaveValue('eq')
  })

  it('takes IN values one chip at a time, so a value may contain a comma', () => {
    render(<StatefulEditor filters={[makeConditionFilter('country', 'eq', 'Smith, John')]} />)
    fireEvent.change(screen.getByLabelText('Filter 1 condition operator'), {
      target: { value: 'in' },
    })
    // The typed scalar carries over as the first chip, whole.
    expect(screen.getByText('Smith, John')).toBeInTheDocument()
    const input = screen.getByLabelText('Filter 1 condition values')
    fireEvent.change(input, { target: { value: 'Doe, Jane' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(screen.getByText('Doe, Jane')).toBeInTheDocument()
  })
})

describe('FactFilterEditor row errors and disabled state', () => {
  it('links a row error to its control', () => {
    const filter = makeNamedFilter()
    const rowId = `op-filter-${filter.id}`
    render(
      <FactFilterEditor
        filters={[filter]}
        onChange={vi.fn()}
        namedOptions={['exclude_test']}
        rowIdPrefix="op"
        errors={{ [rowId]: 'Filter 1: Pick a named filter, or remove this row.' }}
      />,
    )
    const control = screen.getByLabelText('Filter 1 named filter')
    expect(control).toHaveAttribute('id', rowId)
    expect(control).toHaveAttribute('aria-invalid', 'true')
    expect(control).toHaveAccessibleDescription('Filter 1: Pick a named filter, or remove this row.')
  })

  it('makes a SQL row read-only while the editor is disabled', () => {
    render(
      <FactFilterEditor
        filters={[makeSqlFilter('x = 1')]}
        onChange={vi.fn()}
        namedOptions={[]}
        disabled
      />,
    )
    expect(screen.getByLabelText('Filter 1 SQL')).toHaveAttribute('readonly')
  })

  it('explains why the check cannot run yet', () => {
    render(
      <FactFilterEditor
        filters={[]}
        onChange={vi.fn()}
        namedOptions={[]}
        rowIdPrefix="op"
        onCheck={vi.fn()}
        checkBlockedReason="Complete the operand before checking its filters — A measure column is required."
      />,
    )
    const button = screen.getByRole('button', { name: /check filters/i })
    expect(button).toBeDisabled()
    expect(button).toHaveAccessibleDescription(/A measure column is required/)
  })
})
