import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ScanConfigPreview } from '@/types'

import { DistributionDriftPicker } from './DistributionDriftPicker'
import { MetricBreakdownPicker } from './MetricBreakdownPicker'
import {
  MAX_PROPERTY_FIELDS,
  isValidPropertyField,
  propertyOptions,
} from './propertyFields'

const columns: ScanConfigPreview['columns'] = [
  { name: 'platform', type_name: 'String', is_nullable: false },
  { name: 'props', type_name: 'JSON', is_nullable: true },
]

const jsonColumns: ScanConfigPreview['json_columns'] = [
  {
    column: 'props',
    paths: [
      { full_path: 'props.plan', path: 'plan', sample_values: ['pro', 'free'] },
      { full_path: 'props.cart.total', path: 'cart.total', sample_values: ['9.99'] },
      { full_path: 'props.bad-key', path: 'bad-key', sample_values: [] },
    ],
  },
]

function renderBreakdown(selected: string[], onToggle = vi.fn()) {
  render(
    <MetricBreakdownPicker
      columns={columns}
      selectedColumns={selected}
      eventTypeColumn=""
      timeColumn=""
      appVersionColumn=""
      platformColumn=""
      onToggleColumn={onToggle}
      jsonColumns={jsonColumns}
    />,
  )
  return onToggle
}

describe('property fields', () => {
  it('accepts <json_column>.<path> with identifier segments only', () => {
    expect(isValidPropertyField('props.plan')).toBe(true)
    expect(isValidPropertyField('props.cart.total')).toBe(true)
    expect(isValidPropertyField('platform')).toBe(false)
    expect(isValidPropertyField('props.bad-key')).toBe(false)
    expect(isValidPropertyField('props.')).toBe(false)
    expect(isValidPropertyField("props.x'; DROP")).toBe(false)
  })

  it('offers discovered paths and keeps a saved one the preview did not sample', () => {
    const options = propertyOptions(jsonColumns, ['platform', 'props.legacy'])
    expect(options.map(option => option.fullPath)).toEqual([
      'props.cart.total',
      'props.legacy',
      'props.plan',
    ])
  })
})

describe('MetricBreakdownPicker properties', () => {
  it('lists the JSON paths of the preview and toggles one', () => {
    const onToggle = renderBreakdown([])
    fireEvent.click(screen.getByRole('checkbox', { name: 'Breakdown by props.plan' }))
    expect(onToggle).toHaveBeenCalledWith('props.plan')
    // The JSON column itself is still not a plain breakdown column.
    expect(screen.queryByRole('checkbox', { name: 'Breakdown by props' })).toBeNull()
    expect(screen.queryByRole('checkbox', { name: 'Breakdown by props.bad-key' })).toBeNull()
  })

  it('adds a typed path and refuses one of an unknown column', () => {
    const onToggle = renderBreakdown([])
    const input = screen.getByRole('textbox', { name: 'Breakdown by property path' })
    fireEvent.change(input, { target: { value: 'events.plan' } })
    expect(screen.getByText('events is not a JSON column of this query.')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Add' })).toHaveProperty('disabled', true)

    fireEvent.change(input, { target: { value: 'props.user.tier' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(onToggle).toHaveBeenCalledWith('props.user.tier')
  })

  it('stops offering new properties at the cap', () => {
    const selected = Array.from({ length: MAX_PROPERTY_FIELDS }, (_, index) => `props.p${index}`)
    renderBreakdown(selected)
    expect(screen.getByRole('checkbox', { name: 'Breakdown by props.plan' })).toHaveProperty(
      'disabled',
      true,
    )
    expect(screen.getByText(/At most 10 properties/)).toBeTruthy()
  })
})

describe('DistributionDriftPicker properties', () => {
  it('offers properties next to the scalar columns', () => {
    const onToggle = vi.fn()
    render(
      <DistributionDriftPicker
        columns={columns}
        selectedFields={['props.plan']}
        eventTypeColumn=""
        timeColumn=""
        appVersionColumn=""
        platformColumn=""
        onToggleField={onToggle}
        jsonColumns={jsonColumns}
      />,
    )
    const box = screen.getByRole('checkbox', { name: 'Distribution props.plan' })
    expect(box.getAttribute('data-state')).toBe('checked')
    fireEvent.click(box)
    expect(onToggle).toHaveBeenCalledWith('props.plan')
    expect(screen.getByRole('checkbox', { name: 'Distribution platform' })).toBeTruthy()
  })
})
