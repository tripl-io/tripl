import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { MonitoringSignal } from '@/types'
import { SignalSummary } from './SignalSummary'

function signal(overrides: Partial<MonitoringSignal> = {}): MonitoringSignal {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event',
    scope_ref: 'event-1',
    state: 'recent',
    event_id: 'event-1',
    event_type_id: null,
    bucket: '2026-09-25T18:00:00Z',
    actual_count: 200,
    expected_count: 100,
    stddev: 10,
    z_score: 10,
    direction: 'spike',
    incident_child: false,
    unit: null,
    detected_at: null,
    ...overrides,
  }
}

const formatActual = (value: number) => `${value} events`
const formatExpected = (value: number) => String(value)

function summaryText() {
  return screen.getByTestId('signal-summary').textContent ?? ''
}

describe('SignalSummary', () => {
  it('reads a spike as one sentence with its reason', () => {
    render(
      <SignalSummary signal={signal()} formatActual={formatActual} formatExpected={formatExpected} sigmaThreshold={4} />,
    )
    expect(summaryText()).toContain('200 events, 100% above the expected 100 (10.0σ).')
    expect(summaryText()).toContain('Why flagged: 10.0σ from the expected value; anything past 4σ is flagged.')
  })

  it('reads a partial drop as "below", without a signed percentage', () => {
    render(
      <SignalSummary
        signal={signal({ direction: 'drop', actual_count: 60, z_score: -4.5 })}
        formatActual={formatActual}
        formatExpected={formatExpected}
      />,
    )
    expect(summaryText()).toContain('60 events, 40% below the expected 100 (4.5σ).')
    expect(summaryText()).toContain('Why flagged: 4.5σ from the expected value, outside the normal range.')
  })

  it('does not lean on the clamped z-score when a drop bottoms out', () => {
    render(
      <SignalSummary
        signal={signal({ direction: 'drop', actual_count: 0, expected_count: 120, z_score: -20 })}
        formatActual={formatActual}
        formatExpected={formatExpected}
      />,
    )
    expect(summaryText()).toContain('dropped to zero, against an expected 120.')
    expect(summaryText()).not.toContain('σ')
  })

  it('names a missing baseline instead of dividing by it', () => {
    render(
      <SignalSummary
        signal={signal({ actual_count: 137, expected_count: 0, z_score: 9.1 })}
        formatActual={formatActual}
        formatExpected={formatExpected}
      />,
    )
    expect(summaryText()).toContain('137 events, with no baseline to compare against.')
    expect(summaryText()).toContain('Why flagged: it fired where nothing was expected.')
  })
})

describe('SignalSummary — Annotate (metric-scope handoff)', () => {
  it('offers Annotate on the flagged bucket only when given a handler', () => {
    const onAnnotate = vi.fn()
    const { rerender } = render(
      <SignalSummary signal={signal()} formatActual={formatActual} formatExpected={formatExpected} />,
    )
    expect(screen.queryByRole('button', { name: 'Annotate' })).toBeNull()

    rerender(
      <SignalSummary
        signal={signal()}
        formatActual={formatActual}
        formatExpected={formatExpected}
        onAnnotate={onAnnotate}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Annotate' }))
    expect(onAnnotate).toHaveBeenCalledWith('2026-09-25T18:00:00Z')
  })
})
