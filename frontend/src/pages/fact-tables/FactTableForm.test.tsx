import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MutationCache, QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { toast } from 'sonner'
import { surfaceMutationError } from '@/lib/errorFeedback'
import { createElement, type ReactNode } from 'react'
import { AuthContext } from '@/components/auth-context'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DataSource, FactTable } from '@/types'
import { FactTableForm } from './FactTableForm'

vi.mock('@/api/factTables', () => ({
  factTablesApi: {
    create: vi.fn().mockResolvedValue({ id: 'created' }),
    update: vi.fn().mockResolvedValue({ id: 'updated' }),
    preview: vi.fn(),
    remove: vi.fn(),
  },
}))

// CodeMirror needs real layout measurement jsdom can't provide; stub it with a
// plain textarea that forwards value/onChange/placeholder and the aria-label so
// the SQL editor stays queryable by accessible name.
vi.mock('@uiw/react-codemirror', () => ({
  default: ({
    value,
    onChange,
    placeholder,
    readOnly,
    'aria-label': ariaLabel,
  }: {
    value: string
    onChange: (v: string) => void
    placeholder?: string
    readOnly?: boolean
    'aria-label'?: string
  }) => (
    <textarea
      aria-label={ariaLabel}
      readOnly={readOnly}
      value={value}
      placeholder={placeholder}
      onChange={e => onChange(e.target.value)}
    />
  ),
}))

// The real SqlEditor, with its props recorded: the CodeMirror stub above has no
// view, so the aria attributes SqlEditor mirrors onto the contenteditable can
// only be checked as the props the form hands it.
const { sqlEditorProps } = vi.hoisted(() => ({
  sqlEditorProps: vi.fn<(props: Record<string, unknown>) => void>(),
}))
vi.mock('@/components/sql-editor', async importOriginal => {
  const actual = await importOriginal<typeof import('@/components/sql-editor')>()
  return {
    ...actual,
    SqlEditor: (props: Parameters<typeof actual.SqlEditor>[0]) => {
      sqlEditorProps(props as unknown as Record<string, unknown>)
      return <actual.SqlEditor {...props} />
    },
  }
})

// The SQL editor fetches the data-source schema for autocomplete; stub it so the
// form test never reaches the network.
const { useDataSourceSchemaMock } = vi.hoisted(() => ({
  useDataSourceSchemaMock: vi.fn<(dsId?: string) => { data: unknown }>(() => ({ data: undefined })),
}))
vi.mock('@/hooks/useDataSourceSchema', () => ({
  useDataSourceSchema: useDataSourceSchemaMock,
}))

import { factTablesApi } from '@/api/factTables'
import { ApiError } from '@/api/client'
import { at } from '@/test/at'
import { factTablesKey } from '@/lib/queryKeys'
import { personaAuth } from '@/test/persona'
import { SessionProject } from '@/test/PersonaProject'

const DATA_SOURCES = [{ id: 'ds-1', name: 'Warehouse' }] as unknown as DataSource[]

let queryClient: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  return createElement(QueryClientProvider, { client: queryClient }, children)
}

function renderForm(factTable: FactTable | null = null) {
  const onClose = vi.fn()
  render(
    createElement(FactTableForm, {
      slug: 'demo',
      factTable,
      dataSources: DATA_SOURCES,
      onClose,
    }),
    { wrapper },
  )
  return { onClose }
}

function fillRequired() {
  fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
    target: { value: 'Orders' },
  })
  fireEvent.change(screen.getByLabelText('Internal name', { exact: false }), {
    target: { value: 'orders' },
  })
  fireEvent.change(document.getElementById('fact-data-source')!, { target: { value: 'ds-1' } })
  fireEvent.change(screen.getByLabelText('Fact table SQL'), {
    target: { value: 'SELECT id, user_id, created_at FROM orders' },
  })
  fireEvent.change(document.getElementById('fact-timestamp')!, { target: { value: 'created_at' } })
}

function submit() {
  fireEvent.click(screen.getByRole('button', { name: /Create fact table|Save fact table/ }))
}

// What `fillRequired()`'s SQL introspects to. A save whose columns do not
// describe the current SQL previews first, so most saves reach this.
const PREVIEWED = {
  columns: [
    { name: 'id', type: 'bigint' },
    { name: 'user_id', type: 'uuid' },
    { name: 'created_at', type: 'timestamp' },
  ],
  identifier_candidates: ['user_id'],
}

/** The validation summary at the foot of the form. */
function summary() {
  return within(screen.getByRole('alert'))
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.mocked(factTablesApi.create).mockClear()
  vi.mocked(factTablesApi.update).mockClear()
  vi.mocked(factTablesApi.preview).mockReset()
  vi.mocked(factTablesApi.preview).mockResolvedValue(PREVIEWED)
  vi.mocked(factTablesApi.remove).mockReset()
})

afterEach(() => {
  queryClient.clear()
})

describe('FactTableForm', () => {
  it('rejects a create that is missing required fields', async () => {
    renderForm()

    submit()

    await screen.findByRole('alert')
    expect(summary().getByText('Display name is required.')).toBeInTheDocument()
    expect(summary().getByText('Internal name is required.')).toBeInTheDocument()
    expect(summary().getByText('A data source is required.')).toBeInTheDocument()
    expect(summary().getByText('The fact table SQL is required.')).toBeInTheDocument()
    expect(summary().getByText('A timestamp column is required.')).toBeInTheDocument()
    expect(factTablesApi.create).not.toHaveBeenCalled()
    expect(factTablesApi.preview).not.toHaveBeenCalled()
  })

  it('previews columns and persists them on create', async () => {
    vi.mocked(factTablesApi.preview).mockResolvedValue({
      columns: [
        { name: 'id', type: 'bigint' },
        { name: 'user_id', type: 'uuid' },
        { name: 'created_at', type: 'timestamp' },
      ],
      identifier_candidates: ['user_id'],
    })

    const { onClose } = renderForm()
    fillRequired()

    fireEvent.click(screen.getByRole('button', { name: /Preview columns/ }))

    // Preview was sent with the SQL + source + timestamp.
    await waitFor(() => expect(factTablesApi.preview).toHaveBeenCalledTimes(1))
    expect(factTablesApi.preview).toHaveBeenCalledWith('demo', {
      data_source_id: 'ds-1',
      sql: 'SELECT id, user_id, created_at FROM orders',
      timestamp_column: 'created_at',
    })

    // Returned columns render with their type badges.
    const list = await screen.findByLabelText('Fact table columns')
    expect(list).toHaveTextContent('id')
    expect(list).toHaveTextContent('bigint')
    expect(list).toHaveTextContent('user_id')
    // The suggestion is applied to the boxes, so no line repeats it.
    expect(screen.getByLabelText('Use user_id as an identifier column')).toBeChecked()
    expect(screen.queryByText(/Suggested identifiers:/)).not.toBeInTheDocument()

    submit()

    await waitFor(() => expect(factTablesApi.create).toHaveBeenCalledTimes(1))
    expect(factTablesApi.create).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({
        name: 'orders',
        display_name: 'Orders',
        data_source_id: 'ds-1',
        timestamp_column: 'created_at',
        sql: 'SELECT id, user_id, created_at FROM orders',
        columns: [
          { name: 'id', type: 'bigint' },
          { name: 'user_id', type: 'uuid' },
          { name: 'created_at', type: 'timestamp' },
        ],
        identifier_columns: ['user_id'],
      }),
    )
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('collects named row filters into the create payload', async () => {
    renderForm()
    fillRequired()

    fireEvent.click(screen.getByRole('button', { name: /Add row filter/ }))
    const nameInput = screen.getByLabelText('Row filter 1 name')
    const sqlInput = screen.getByLabelText('Row filter 1 SQL condition')
    fireEvent.change(nameInput, { target: { value: 'ios_only' } })
    fireEvent.change(sqlInput, { target: { value: "platform = 'ios'" } })

    submit()

    await waitFor(() => expect(factTablesApi.create).toHaveBeenCalledTimes(1))
    expect(factTablesApi.create).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({
        row_filters: [{ name: 'ios_only', sql: "platform = 'ios'" }],
      }),
    )
  })

  it('refuses a save with a half-filled row filter instead of dropping it', async () => {
    renderForm()
    fillRequired()
    fireEvent.click(screen.getByRole('button', { name: /Add row filter/ }))
    fireEvent.change(screen.getByLabelText('Row filter 1 name'), { target: { value: 'ios_only' } })

    submit()

    await screen.findByRole('alert')
    expect(
      summary().getByText(/Row filter 1 needs both a name and a SQL condition/),
    ).toBeInTheDocument()
    // Inline too, on the half that is missing: the condition is a compact SQL
    // editor, which mirrors these props onto its contenteditable.
    const conditionProps = sqlEditorProps.mock.calls
      .map(([props]) => props)
      .filter(props => props.ariaLabel === 'Row filter 1 SQL condition')
      .at(-1)
    expect(conditionProps).toMatchObject({ compact: true, ariaInvalid: true })
    expect(factTablesApi.create).not.toHaveBeenCalled()
  })

  it('refuses a save whose row filters repeat a name', async () => {
    renderForm()
    fillRequired()

    fireEvent.click(screen.getByRole('button', { name: /Add row filter/ }))
    fireEvent.click(screen.getByRole('button', { name: /Add row filter/ }))
    fireEvent.change(screen.getByLabelText('Row filter 1 name'), { target: { value: 'ios_only' } })
    fireEvent.change(screen.getByLabelText('Row filter 1 SQL condition'), {
      target: { value: "platform = 'ios'" },
    })
    // Trailing space on the repeat: the payload is trimmed, so the two names
    // collide in the request even though the inputs do not look identical.
    fireEvent.change(screen.getByLabelText('Row filter 2 name'), { target: { value: 'ios_only ' } })
    fireEvent.change(screen.getByLabelText('Row filter 2 SQL condition'), {
      target: { value: "platform = 'ipados'" },
    })

    submit()

    await screen.findByRole('alert')
    expect(summary().getByText(/Two row filters are named "ios_only"/)).toBeInTheDocument()
    expect(factTablesApi.create).not.toHaveBeenCalled()
  })

  it('preview keeps saved identifier picks and only adds new suggestions', async () => {
    const columns = [
      { name: 'paid', type: 'string' },
      { name: 'android', type: 'string' },
      { name: 'country', type: 'string' },
      { name: 'user_id', type: 'string' },
      { name: 'created_at', type: 'timestamp' },
    ]
    // The tightened count_distinct heuristic now suggests only user_id + country
    // and no longer suggests the previously-saved picks paid / android.
    vi.mocked(factTablesApi.preview).mockResolvedValue({
      columns,
      identifier_candidates: ['user_id', 'country'],
    })

    const existing = {
      id: 'ft-9',
      project_id: 'p-1',
      name: 'events',
      display_name: 'Events',
      description: '',
      color: '#6366f1',
      order: 0,
      data_source_id: 'ds-1',
      timestamp_column: 'created_at',
      columns,
      // Manual picks saved in an earlier session, including paid / android which
      // the current (stricter) heuristic no longer suggests.
      identifier_columns: ['paid', 'android', 'user_id'],
      row_filters: [],
      sql: 'SELECT paid, android, country, user_id, created_at FROM events',
      created_at: '2026-06-01T00:00:00Z',
      updated_at: '2026-06-20T00:00:00Z',
    } as unknown as FactTable

    renderForm(existing)

    // Saved picks render checked.
    expect(screen.getByLabelText('Use paid as an identifier column')).toBeChecked()
    expect(screen.getByLabelText('Use android as an identifier column')).toBeChecked()

    // First preview of the edit session: it must NOT silently uncheck saved picks
    // the heuristic dropped, but it DOES surface the newly-suggested column.
    fireEvent.click(screen.getByRole('button', { name: /Preview columns/ }))
    await waitFor(() =>
      expect(screen.getByLabelText('Use country as an identifier column')).toBeChecked(),
    )
    expect(screen.getByLabelText('Use paid as an identifier column')).toBeChecked()
    expect(screen.getByLabelText('Use android as an identifier column')).toBeChecked()
    expect(screen.getByLabelText('Use user_id as an identifier column')).toBeChecked()

    // In-session manual removal still persists across a re-preview: uncheck
    // user_id, then re-preview and confirm it stays unchecked while the saved
    // picks remain.
    fireEvent.click(screen.getByLabelText('Use user_id as an identifier column'))

    fireEvent.click(screen.getByRole('button', { name: /Preview columns/ }))
    await waitFor(() => expect(factTablesApi.preview).toHaveBeenCalledTimes(2))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /Preview columns/ })).toBeEnabled(),
    )
    expect(screen.getByLabelText('Use user_id as an identifier column')).not.toBeChecked()
    expect(screen.getByLabelText('Use paid as an identifier column')).toBeChecked()
    expect(screen.getByLabelText('Use country as an identifier column')).toBeChecked()

    submit()

    await waitFor(() => expect(factTablesApi.update).toHaveBeenCalledTimes(1))
    const [, , payload] = at(vi.mocked(factTablesApi.update).mock.calls, 0)
    const identifiers = (payload as { identifier_columns: string[] }).identifier_columns
    expect(identifiers).toEqual(expect.arrayContaining(['paid', 'android', 'country']))
    expect(identifiers).not.toContain('user_id')
    expect(identifiers).toHaveLength(3)
  })

  it('derives the internal name from the display name until the user edits it', () => {
    renderForm()
    const displayName = screen.getByLabelText('Display name', { exact: false })
    const internalName = screen.getByLabelText('Internal name', { exact: false })

    fireEvent.change(displayName, { target: { value: 'Заказы Café' } })
    expect(internalName).toHaveValue('zakazy_cafe')

    // A name with nothing Latin to derive from still fills the field.
    fireEvent.change(displayName, { target: { value: '订单' } })
    expect((internalName as HTMLInputElement).value).toMatch(/^fact_[a-z0-9]+$/)

    fireEvent.change(internalName, { target: { value: 'orders_v2' } })
    fireEvent.change(displayName, { target: { value: 'Paid orders' } })
    expect(internalName).toHaveValue('orders_v2')
  })

  it('renders the internal name read-only when editing and omits it from the update', async () => {
    const existing = {
      id: 'ft-9',
      project_id: 'p-1',
      name: 'orders',
      display_name: 'Orders',
      description: '',
      color: '#6366f1',
      order: 0,
      data_source_id: 'ds-1',
      timestamp_column: 'created_at',
      columns: [
        { name: 'id', type: 'bigint' },
        { name: 'created_at', type: 'timestamp' },
      ],
      identifier_columns: ['id'],
      row_filters: [],
      sql: 'SELECT id, created_at FROM orders',
      created_at: '2026-06-01T00:00:00Z',
      updated_at: '2026-06-20T00:00:00Z',
    } as unknown as FactTable

    renderForm(existing)

    // No editable internal-name input; the name is shown as static text.
    expect(document.getElementById('fact-name')).toBeNull()

    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Orders renamed' },
    })
    submit()

    await waitFor(() => expect(factTablesApi.update).toHaveBeenCalledTimes(1))
    const [, factTableId, payload] = at(vi.mocked(factTablesApi.update).mock.calls, 0)
    expect(factTableId).toBe('ft-9')
    expect(payload).not.toHaveProperty('name')
    expect(payload).toMatchObject({ display_name: 'Orders renamed' })
  })
})

/** Whether a reload/tab-close right now would get the browser's prompt. */
function reloadIsGuarded(): boolean {
  const event = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(event)
  return event.defaultPrevented
}

describe('FactTableForm unsaved-changes guard', () => {
  it('arms the reload prompt once the draft differs from what the form opened with', () => {
    renderForm()
    expect(reloadIsGuarded()).toBe(false)

    fillRequired()
    expect(reloadIsGuarded()).toBe(true)
  })

  it('leaves an untouched edit form unguarded, row filters included', () => {
    renderForm({
      id: 'ft-1',
      project_id: 'p-1',
      name: 'orders',
      display_name: 'Orders',
      description: '',
      color: '#6366f1',
      order: 0,
      data_source_id: 'ds-1',
      timestamp_column: 'created_at',
      columns: [],
      identifier_columns: [],
      // Each row gets a fresh client-side id on mount; those are not edits.
      row_filters: [{ name: 'ios_only', sql: "platform = 'ios'" }],
      sql: 'SELECT id, created_at FROM orders',
      created_at: '2026-06-01T00:00:00Z',
      updated_at: '2026-06-20T00:00:00Z',
    } as unknown as FactTable)
    expect(reloadIsGuarded()).toBe(false)
  })
})

const VIEWER = personaAuth('viewer')

describe('FactTableForm for a viewer', () => {
  const SAVED = {
    id: 'ft-1',
    project_id: 'p-1',
    name: 'orders',
    display_name: 'Orders',
    description: '',
    color: '#6366f1',
    order: 0,
    data_source_id: 'ds-1',
    timestamp_column: 'created_at',
    columns: [],
    identifier_columns: [],
    row_filters: [],
    sql: 'SELECT id, created_at FROM orders',
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-20T00:00:00Z',
  } as unknown as FactTable

  it('shows the SQL read-only and never asks for the editor-only schema', () => {
    useDataSourceSchemaMock.mockClear()
    render(
      createElement(
        AuthContext.Provider,
        { value: VIEWER },
        createElement(SessionProject, { session: VIEWER }, createElement(FactTableForm, {
          slug: 'demo',
          factTable: SAVED,
          dataSources: DATA_SOURCES,
          onClose: vi.fn(),
        })),
      ),
      { wrapper },
    )

    expect(screen.getByLabelText('Fact table SQL')).toHaveAttribute('readonly')
    expect(useDataSourceSchemaMock).toHaveBeenCalled()
    expect(useDataSourceSchemaMock.mock.calls.every(([dsId]) => dsId === undefined)).toBe(true)
  })

  it('asks an editor for the schema of the chosen source', () => {
    useDataSourceSchemaMock.mockClear()
    renderForm(SAVED)

    expect(screen.getByLabelText('Fact table SQL')).not.toHaveAttribute('readonly')
    expect(useDataSourceSchemaMock).toHaveBeenCalledWith('ds-1')
  })
})

describe('FactTableForm save failure', () => {
  it('says it once, inline, and keeps the app-wide toast quiet', async () => {
    // The backstop main.tsx registers, so the test sees what the app does.
    queryClient = new QueryClient({
      mutationCache: new MutationCache({ onError: surfaceMutationError }),
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    const toastError = vi.spyOn(toast, 'error')
    vi.mocked(factTablesApi.create).mockRejectedValueOnce(new Error('Name already taken'))
    renderForm()

    fillRequired()
    submit()

    expect(await screen.findByText('Could not save fact table')).toBeInTheDocument()
    expect(toastError).not.toHaveBeenCalled()
    toastError.mockRestore()
  })
})

const SAVED_ORDERS = {
  id: 'ft-7',
  project_id: 'p-1',
  name: 'orders',
  display_name: 'Orders',
  description: '',
  color: '#6366f1',
  order: 0,
  data_source_id: 'ds-1',
  timestamp_column: 'created_at',
  columns: [
    { name: 'id', type: 'bigint' },
    { name: 'created_at', type: 'timestamp' },
  ],
  identifier_columns: [],
  row_filters: [],
  sql: 'SELECT id, created_at FROM orders',
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-20T00:00:00Z',
} as unknown as FactTable

describe('FactTableForm columns follow the SQL', () => {
  it('introspects the columns on a create that was never previewed', async () => {
    renderForm()
    fillRequired()

    submit()

    await waitFor(() => expect(factTablesApi.create).toHaveBeenCalledTimes(1))
    expect(factTablesApi.preview).toHaveBeenCalledTimes(1)
    expect(factTablesApi.create).toHaveBeenCalledWith(
      'demo',
      expect.objectContaining({
        columns: PREVIEWED.columns,
        identifier_columns: ['user_id'],
      }),
    )
  })

  it('re-reads the columns when the SQL changed after they were stored', async () => {
    vi.mocked(factTablesApi.preview).mockResolvedValue({
      columns: [
        { name: 'id', type: 'bigint' },
        { name: 'amount', type: 'numeric' },
        { name: 'created_at', type: 'timestamp' },
      ],
      identifier_candidates: [],
    })
    renderForm(SAVED_ORDERS)

    fireEvent.change(screen.getByLabelText('Fact table SQL'), {
      target: { value: 'SELECT id, amount, created_at FROM orders' },
    })
    expect(screen.getByText(/changed since these columns were read/)).toBeInTheDocument()
    submit()

    await waitFor(() => expect(factTablesApi.update).toHaveBeenCalledTimes(1))
    const [, , payload] = at(vi.mocked(factTablesApi.update).mock.calls, 0)
    expect((payload as { columns: { name: string }[] }).columns.map(c => c.name)).toEqual([
      'id',
      'amount',
      'created_at',
    ])
  })

  it('does not preview again when the stored columns still describe the SQL', async () => {
    renderForm(SAVED_ORDERS)
    fireEvent.change(screen.getByLabelText('Display name', { exact: false }), {
      target: { value: 'Orders v2' },
    })

    submit()

    await waitFor(() => expect(factTablesApi.update).toHaveBeenCalledTimes(1))
    expect(factTablesApi.preview).not.toHaveBeenCalled()
  })

  it('refuses a timestamp column the SQL does not return', async () => {
    renderForm()
    fillRequired()
    fireEvent.change(document.getElementById('fact-timestamp')!, { target: { value: 'ts' } })

    submit()

    await waitFor(() => expect(factTablesApi.preview).toHaveBeenCalledTimes(1))
    // The reason shows twice by design: in the summary (a link to the field)
    // and under the field, which is also its accessible description.
    await screen.findByRole('alert')
    expect(summary().getByText(/"ts" is not a column of this SQL/)).toBeInTheDocument()
    const timestamp = document.getElementById('fact-timestamp')!
    expect(timestamp).toHaveAttribute('aria-invalid', 'true')
    await waitFor(() => expect(timestamp).toHaveFocus())
    expect(timestamp).toHaveAccessibleDescription(/"ts" is not a column of this SQL/)
    expect(factTablesApi.create).not.toHaveBeenCalled()
  })

  it('holds the save when the automatic preview fails', async () => {
    vi.mocked(factTablesApi.preview).mockRejectedValue(new Error('relation "orders" does not exist'))
    renderForm()
    fillRequired()

    submit()

    expect(await screen.findByText('Could not preview columns')).toBeInTheDocument()
    expect(factTablesApi.create).not.toHaveBeenCalled()
    // Focus follows to the reason, not left on Save (the Preview button was
    // still disabled when focus used to be moved).
    await waitFor(() => expect(document.getElementById('fact-preview-error')).toHaveFocus())
  })
})

describe('FactTableForm validation matches the metric form', () => {
  it('marks the field, says why under it, and moves focus to the first one', async () => {
    renderForm()

    submit()

    const displayName = screen.getByLabelText('Display name', { exact: false })
    await waitFor(() => expect(displayName).toHaveFocus())
    expect(displayName).toHaveAttribute('aria-invalid', 'true')
    expect(displayName).toHaveAccessibleDescription('Display name is required.')
  })

  it('marks the SQL editor required, and invalid with its message when empty', async () => {
    renderForm()
    const lastSqlProps = () => sqlEditorProps.mock.calls.at(-1)?.[0]
    expect(lastSqlProps()).toMatchObject({ ariaRequired: true })
    expect(lastSqlProps()?.ariaInvalid).toBeFalsy()

    submit()

    await waitFor(() =>
      expect(lastSqlProps()).toMatchObject({
        ariaRequired: true,
        ariaInvalid: true,
        ariaDescribedBy: 'fact-sql-error',
      }),
    )
    expect(document.getElementById('fact-sql-error')).toHaveTextContent(
      'The fact table SQL is required.',
    )
  })

  it('does not point the read-only internal name label at a missing control', () => {
    renderForm(SAVED_ORDERS)

    // A caption for the static name, not a <label> aimed at an id no control has.
    expect(screen.getByText('Internal name').closest('label')).toBeNull()
  })
})

describe('FactTableForm delete', () => {
  it('deletes after confirmation and closes the editor', async () => {
    vi.mocked(factTablesApi.remove).mockResolvedValue(undefined)
    const { onClose } = renderForm(SAVED_ORDERS)

    fireEvent.click(screen.getByRole('button', { name: 'Delete fact table' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(factTablesApi.remove).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete fact table' }))

    await waitFor(() => expect(factTablesApi.remove).toHaveBeenCalledWith('demo', 'ft-7'))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
  })

  it('shows which metrics still read the table in the dialog when the server refuses', async () => {
    vi.mocked(factTablesApi.remove).mockRejectedValue(
      new ApiError("Cannot delete this fact table. This fact table is read by 1 metric: 'revenue'.", 409),
    )
    const { onClose } = renderForm(SAVED_ORDERS)

    fireEvent.click(screen.getByRole('button', { name: 'Delete fact table' }))
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Delete fact table' }))

    // In the dialog, as the list row's delete reports it, and the dialog stays
    // open for another go.
    expect(
      await within(dialog).findByText(/^Could not delete the fact table: .*read by 1 metric: 'revenue'/),
    ).toBeInTheDocument()
    expect(screen.queryByText('Could not delete fact table')).toBeNull()
    expect(onClose).not.toHaveBeenCalled()
  })

  it('is not offered on a new fact table', () => {
    renderForm()
    expect(screen.queryByRole('button', { name: 'Delete fact table' })).toBeNull()
  })
})

describe('FactTableForm columns card', () => {
  it('fills an empty timestamp from the only timestamp column the preview found', async () => {
    renderForm()
    fireEvent.change(document.getElementById('fact-data-source')!, { target: { value: 'ds-1' } })
    fireEvent.change(screen.getByLabelText('Fact table SQL'), {
      target: { value: 'SELECT id, user_id, created_at FROM orders' },
    })

    fireEvent.click(screen.getByRole('button', { name: /Preview columns/ }))

    await waitFor(() => expect(document.getElementById('fact-timestamp')).toHaveValue('created_at'))
    expect(screen.getByText(/Detected: created_at/)).toBeInTheDocument()
  })

  it('offers unused identifier suggestions as one click', async () => {
    renderForm()
    fillRequired()
    fireEvent.click(screen.getByRole('button', { name: /Preview columns/ }))
    const userId = await screen.findByLabelText('Use user_id as an identifier column')
    fireEvent.click(userId)
    expect(userId).not.toBeChecked()

    fireEvent.click(screen.getByRole('button', { name: 'Use suggestions' }))
    expect(userId).toBeChecked()
  })
})

describe('FactTableForm follow-ups', () => {
  it('re-reads the columns by itself after the first Preview once typing pauses', async () => {
    renderForm()
    fillRequired()
    fireEvent.click(screen.getByRole('button', { name: /Preview columns/ }))
    await waitFor(() => expect(factTablesApi.preview).toHaveBeenCalledTimes(1))
    await screen.findByLabelText('Use user_id as an identifier column')

    fireEvent.change(screen.getByLabelText('Fact table SQL'), {
      target: { value: 'SELECT id, user_id, created_at, amount FROM orders' },
    })
    expect(screen.getByText(/The columns are read again in a moment/)).toBeInTheDocument()

    await waitFor(() => expect(factTablesApi.preview).toHaveBeenCalledTimes(2), { timeout: 3000 })
    expect(at(vi.mocked(factTablesApi.preview).mock.calls, 1)[1]).toEqual(
      expect.objectContaining({ sql: 'SELECT id, user_id, created_at, amount FROM orders' }),
    )
  })

  it('never reads the columns on its own before the first Preview', async () => {
    renderForm()
    fillRequired()
    fireEvent.change(screen.getByLabelText('Fact table SQL'), {
      target: { value: 'SELECT id, created_at FROM orders' },
    })

    await new Promise(resolve => setTimeout(resolve, 1200))
    expect(factTablesApi.preview).not.toHaveBeenCalled()
    expect(screen.getByText(/They are read from the SQL when you preview or save/)).toBeInTheDocument()
  })

  it('gives a new table the first palette colour no listed table uses', () => {
    queryClient.setQueryData(factTablesKey('demo'), {
      total: 1,
      items: [{ id: 'ft-1', color: '#6366f1' }],
    })
    renderForm()

    expect(screen.getByRole('button', { name: 'Indigo' })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.getByRole('button', { name: 'Sky' })).toHaveAttribute('aria-pressed', 'true')
    fireEvent.click(screen.getByRole('button', { name: 'Emerald' }))
    expect(document.getElementById('fact-color')).toHaveValue('#10b981')
  })
})
