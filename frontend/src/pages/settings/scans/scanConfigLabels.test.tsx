import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ScanConfig } from '@/types'
import { ScanBadges } from './ScanConfigRow'
import { ScanConfigReadView } from './ScanConfigReadView'
import { NoneTag } from './scanLayout'

function config(overrides: Partial<ScanConfig> = {}): ScanConfig {
  return {
    id: 'sc-1',
    name: 'Demo scan',
    data_source_id: 'ds-1',
    base_query: 'select 1',
    time_column: 'event_ts',
    interval: '1h',
    event_type_id: null,
    event_type_column: 'category',
    event_name_format: '{action}',
    scan_lookback_hours: null,
    scan_row_limit: null,
    metrics_row_limit: null,
    json_value_paths: [],
    json_string_columns: [],
    metric_breakdown_columns: [],
    metric_breakdown_values_limit: null,
    distribution_drift_fields: [],
    replay_chunk_interval: null,
    app_version_column: 'app_version',
    platform_column: null,
    cardinality_threshold: 100,
    event_group_rules: Array.from({ length: 18 }, (_, i) => ({
      name: `group ${i}`,
      condition_logic: 'all',
      conditions: [],
    })),
    ...overrides,
  } as unknown as ScanConfig
}

describe('scan header badges (prelaunch)', () => {
  // The ⏱ character drew a missing-glyph box without an emoji font, and a
  // colour emoji among monochrome icons with one.
  it('marks the interval with a lucide clock, not an emoji', () => {
    render(<ScanBadges sc={config()} intervalLabel={{ '1h': 'Every hour' }} />)
    const chip = screen.getByText('Every hour')
    expect(chip.textContent).toBe('Every hour')
    expect(chip.querySelector('svg')).not.toBeNull()
    expect(document.body.textContent).not.toContain('⏱')
  })

  it('names the app version column as a column, and counts event group rules', () => {
    render(<ScanBadges sc={config()} intervalLabel={{ '1h': 'Every hour' }} />)
    expect(screen.getByText(/^App version column/)).toHaveTextContent('App version column app_version')
    expect(screen.getByText('app_version')).toHaveClass('mono')
    expect(screen.getByText('18 event group rules')).toBeInTheDocument()
    expect(screen.queryByText(/^Version /)).toBeNull()
    expect(screen.queryByText(/^Groups /)).toBeNull()
  })

  it('counts kept JSON values in words', () => {
    render(
      <ScanBadges sc={config({ json_value_paths: ['properties.plan'] })} intervalLabel={{ '1h': 'Every hour' }} />,
    )
    expect(screen.getByText('1 JSON value kept')).toBeInTheDocument()
  })
})

describe('scan settings read view (prelaunch)', () => {
  // The owner's Overview and the editor name these rows; a reader who cannot
  // edit used to see other words for the same settings.
  it('labels the rows the way the Overview and the editor do', () => {
    render(<ScanConfigReadView scanConfig={config()} dataSources={[]} eventTypes={[]} />)
    expect(screen.getByText('Event group rules')).toBeInTheDocument()
    expect(screen.getByText('Metric breakdowns')).toBeInTheDocument()
    expect(screen.getByText('Value limit')).toBeInTheDocument()
    expect(screen.getByText('Unlimited')).toBeInTheDocument()
    expect(screen.queryByText('Event groups')).toBeNull()
    expect(screen.queryByText('Values per breakdown')).toBeNull()
    expect(screen.queryByText('No limit')).toBeNull()
  })
})

describe('NoneTag', () => {
  it('reads "None" in sentence case, like the "Unlimited" beside it', () => {
    render(<NoneTag />)
    expect(screen.getByText('None')).toHaveClass('text-fg-tertiary')
  })
})
