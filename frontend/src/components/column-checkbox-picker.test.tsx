import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ColumnCheckboxPicker } from './column-checkbox-picker'

const many = Array.from({ length: 20 }, (_, i) => `col_${String(i).padStart(2, '0')}`)

describe('ColumnCheckboxPicker', () => {
  it('shows a short list whole, with no filter input', () => {
    render(<ColumnCheckboxPicker columns={['platform', 'country']} value={[]} onChange={vi.fn()} />)

    expect(screen.getByRole('checkbox', { name: 'Break down by platform' })).toBeInTheDocument()
    expect(screen.queryByLabelText('Filter columns')).toBeNull()
  })

  it('filters a long list and keeps ticked columns in view', () => {
    render(<ColumnCheckboxPicker columns={many} value={['col_03']} onChange={vi.fn()} />)

    fireEvent.change(screen.getByLabelText('Filter columns'), { target: { value: 'col_1' } })

    expect(screen.getByRole('checkbox', { name: 'Break down by col_12' })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'Break down by col_05' })).toBeNull()
    expect(screen.getByRole('checkbox', { name: 'Break down by col_03' })).toBeChecked()
  })
})
