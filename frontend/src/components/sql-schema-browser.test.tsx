import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { TableSchema } from '@/types/dataSourceSchema'
import { SqlSchemaBrowser } from './sql-schema-browser'

const TABLES: TableSchema[] = [
  {
    name: 'orders',
    columns: [
      { name: 'id', data_type: 'bigint' },
      { name: 'amount', data_type: 'numeric' },
    ],
  },
  {
    name: 'sessions',
    columns: [{ name: 'user_id', data_type: 'uuid' }],
  },
]

function openPanel() {
  fireEvent.click(screen.getByRole('button', { name: /Tables/ }))
}

describe('SqlSchemaBrowser', () => {
  it('keeps the panel collapsed until toggled', () => {
    render(<SqlSchemaBrowser tables={TABLES} onInsert={vi.fn()} />)
    expect(screen.queryByLabelText('Filter tables and columns')).toBeNull()
    openPanel()
    expect(screen.getByLabelText('Filter tables and columns')).toBeInTheDocument()
  })

  it('inserts the table name when its row is clicked', () => {
    const onInsert = vi.fn()
    render(<SqlSchemaBrowser tables={TABLES} onInsert={onInsert} />)
    openPanel()
    fireEvent.click(screen.getByRole('button', { name: 'orders' }))
    expect(onInsert).toHaveBeenCalledWith('orders')
  })

  it('reveals columns on expand and inserts a column name', () => {
    const onInsert = vi.fn()
    render(<SqlSchemaBrowser tables={TABLES} onInsert={onInsert} />)
    openPanel()
    fireEvent.click(screen.getByRole('button', { name: 'Toggle columns for orders' }))
    const amount = screen.getByRole('button', { name: /amount/ })
    expect(amount).toHaveTextContent('numeric')
    fireEvent.click(amount)
    expect(onInsert).toHaveBeenCalledWith('amount')
  })

  it('filters tables and columns by query', async () => {
    render(<SqlSchemaBrowser tables={TABLES} onInsert={vi.fn()} />)
    openPanel()
    fireEvent.change(screen.getByLabelText('Filter tables and columns'), {
      target: { value: 'amount' },
    })
    // The matching column is auto-revealed once the debounced filter applies;
    // the non-matching table disappears.
    expect(await screen.findByRole('button', { name: /amount/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'sessions' })).toBeNull()
  })

  // a search force-expands every matching table, so the list is capped.
  it('lists at most 50 matching tables and counts the rest', async () => {
    const many: TableSchema[] = Array.from({ length: 53 }, (_, index) => ({
      name: `events_${index}`,
      columns: [{ name: 'id', data_type: 'bigint' }],
    }))
    render(<SqlSchemaBrowser tables={many} onInsert={vi.fn()} />)
    openPanel()
    fireEvent.change(screen.getByLabelText('Filter tables and columns'), {
      target: { value: 'events' },
    })
    expect(await screen.findByText(/3 more matching tables/)).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /^Toggle columns for / })).toHaveLength(50)
    })
  })

  // A table matching by name is expanded with every column, so the table cap
  // alone still mounted 50 × all columns for a one-letter query.
  it('caps the column rows a search renders across all tables', async () => {
    const wide: TableSchema[] = Array.from({ length: 3 }, (_, table) => ({
      name: `wide_${table}`,
      columns: Array.from({ length: 300 }, (_, column) => ({
        name: `col_${table}_${column}`,
        data_type: 'text',
      })),
    }))
    render(<SqlSchemaBrowser tables={wide} onInsert={vi.fn()} />)
    openPanel()
    fireEvent.change(screen.getByLabelText('Filter tables and columns'), {
      target: { value: 'wide' },
    })
    expect(await screen.findByText(/100 more columns/)).toBeInTheDocument()
    expect(screen.getByText(/300 more columns/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /^col_/ })).toHaveLength(500)
  })
})
