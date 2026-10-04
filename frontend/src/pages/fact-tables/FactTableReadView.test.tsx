import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { DataSource, FactTable } from '@/types'
import { FactTableReadView } from './FactTableReadView'

const FACT_TABLE = {
  id: 'ft-1',
  project_id: 'p-1',
  name: 'orders',
  display_name: 'Orders',
  description: '',
  color: '#888888',
  data_source_id: 'ds-1',
  sql: 'SELECT * FROM orders',
  timestamp_column: 'created_at',
  identifier_columns: ['user_id'],
  columns: [
    { name: 'created_at', type: 'timestamp' },
    { name: 'amount', type: 'number' },
  ],
  row_filters: [],
  order: 0,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
} as unknown as FactTable

const DATA_SOURCES = [{ id: 'ds-1', name: 'Warehouse' }] as unknown as DataSource[]

describe('FactTableReadView (#237)', () => {
  it('shows a viewer the definition as text, titled after the table, with no form controls', () => {
    const onClose = vi.fn()
    render(<FactTableReadView factTable={FACT_TABLE} dataSources={DATA_SOURCES} onClose={onClose} />)

    expect(screen.getByRole('heading', { level: 1, name: 'Orders' })).toBeInTheDocument()
    expect(screen.getByRole('note')).toBeInTheDocument()
    expect(screen.getByText('Warehouse')).toBeInTheDocument()
    expect(screen.getByLabelText('Fact table SQL')).toHaveTextContent('SELECT * FROM orders')
    // Empty values read as a muted dash, not a blank input.
    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByRole('combobox')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Fact tables/ }))
    expect(onClose).toHaveBeenCalled()
  })
})
