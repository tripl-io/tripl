import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { FieldDefinition } from '@/types'
import { FieldValueControl } from './eventFormFields'

const PLATFORM_FIELD = {
  id: 'f-platform',
  event_type_id: 'et-1',
  name: 'platform',
  display_name: 'Platform',
  field_type: 'enum',
  is_required: false,
  enum_options: ['ios', 'android', 'web'],
  order: 0,
} as unknown as FieldDefinition

describe('FieldValueControl choice picker', () => {
  it('shows a stored ${property} token an enum does not list, instead of "—"', () => {
    // The events table read `${platform}` while this picker read "—" for the
    // same stored value.
    const onChange = vi.fn()
    render(<FieldValueControl field={PLATFORM_FIELD} value="${platform}" onChange={onChange} variables={[]} />)

    const select = screen.getByRole('combobox')
    expect(select).toHaveValue('${platform}')
    expect(within(select).getAllByRole('option').map(option => option.textContent)).toEqual([
      '—',
      '${platform}',
      'ios',
      'android',
      'web',
    ])
    fireEvent.change(select, { target: { value: 'ios' } })
    expect(onChange).toHaveBeenCalledWith('ios')
  })

  it('adds no extra option for a listed value or an empty one', () => {
    const { rerender } = render(
      <FieldValueControl field={PLATFORM_FIELD} value="ios" onChange={() => {}} variables={[]} />,
    )
    expect(within(screen.getByRole('combobox')).getAllByRole('option')).toHaveLength(4)

    rerender(<FieldValueControl field={PLATFORM_FIELD} value="" onChange={() => {}} variables={[]} />)
    expect(within(screen.getByRole('combobox')).getAllByRole('option')).toHaveLength(4)
  })

  it('treats a boolean the same way', () => {
    const flag = { ...PLATFORM_FIELD, field_type: 'boolean', enum_options: null } as unknown as FieldDefinition
    render(<FieldValueControl field={flag} value="${is_trial}" onChange={() => {}} variables={[]} />)

    expect(screen.getByRole('combobox')).toHaveValue('${is_trial}')
  })
})
