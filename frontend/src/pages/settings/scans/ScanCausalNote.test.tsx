import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { ScanConfig } from '@/types'

import { ScanCausalNote } from './ScanCausalNote'

/** Only the two columns scanModeOf derives from matter here. */
function config(time_column: string | null, interval: string | null) {
  return { time_column, interval } as Pick<ScanConfig, 'time_column' | 'interval'>
}

function noteText(ui: React.ReactElement): string {
  const { unmount } = render(ui)
  const text = screen.getByTestId('scan-causal-note').textContent ?? ''
  unmount()
  return text
}

describe('ScanCausalNote — a scan says what it produces', () => {
  it('names metric points AND anomaly detection for a monitoring scan', () => {
    // The whole point of the note: the chain from a scan run to the Telegram
    // message. Dropping either half leaves the user with "Scan: Snowplow Events
    // (iOS)" in Telegram and no screen that connects it to the scan.
    const note = noteText(<ScanCausalNote variant="config" config={config('created_at', '1h')} />)

    expect(note).toContain('metric points')
    expect(note).toContain('anomaly detection')
    // The cadence is stated, lower-cased into the sentence.
    expect(note).toContain('Runs every hour.')
  })

  // `run_scan` (backend/src/tripl/worker/tasks/scan.py) writes events and
  // variables and no `EventMetric`; metric points come from `collect_metrics`,
  // dispatched only by the scheduled `check_metrics_due`. The note sits directly
  // above a `Run now` button, so promising points "every run" is a promise the
  // user disproves in one click — and the run report for that run names none.
  it('ties metric points to the schedule, not to a run the user starts', () => {
    const note = noteText(<ScanCausalNote variant="config" config={config('created_at', '1h')} />)

    expect(note).toContain('collected on that schedule')
    expect(note).toContain('not by Run now')
    expect(note).not.toMatch(/(each|every) run[^.;]*metric points/i)
  })

  it('states the consequences a catalog-only scan does NOT have', () => {
    // Catalog only is a legitimate choice, so the note is not a warning — but it
    // must still say that no anomaly and no alert will ever come from this scan,
    // which is the silence the user would otherwise have to infer.
    expect(noteText(<ScanCausalNote variant="config" config={config(null, null)} />)).toContain(
      'no metric points, no anomalies, no alerts',
    )
  })

  it('says a scheduled scan with no time column never collects metrics', () => {
    // The misconfigured quadrant: the user asked for a schedule and gets
    // nothing. Collapsing it into "Catalog only" would launder the fault.
    const note = noteText(<ScanCausalNote variant="config" config={config(null, '1h')} />)

    expect(note).toContain('collects no metric points')
    expect(note).toContain('Add a time column to fix it.')
  })

  // The note is rendered on the scan's own page, directly above Recent runs —
  // which lists this scan's completed manual runs, because trigger_scan has no
  // dispatchability guard. A warning the page underneath falsifies is a warning
  // the user learns to dismiss, and this is the one badge carrying a real fault.
  it('does not claim a never-dispatched scan is never run, which its own run history disproves', () => {
    const note = noteText(<ScanCausalNote variant="config" config={config(null, '1h')} />)

    expect(note).toContain('the scheduler never runs it')
    expect(note).toContain('Runs you start by hand still add events to your plan.')
    expect(note).not.toMatch(/so it is never run/)
  })

  it('adds only what the monitoring radio lacks: when points arrive, where output shows (#247 DA-13)', () => {
    const note = noteText(<ScanCausalNote variant="form" mode="monitoring" />)

    // Per-schedule for the points — the split the backend makes, and the one
    // the config note makes too.
    expect(note).toContain('recorded on the schedule, not by Run now')
    expect(note).not.toMatch(/metric points every run/)
    expect(note).toContain('Signals appear on Anomalies; alerts go out from Alerting.')
    // Not a restatement of the radio's own description.
    expect(note).not.toContain('tracking plan')
  })

  it('has nothing to add under Catalog only, whose radio already says it all (#247 DA-13)', () => {
    const { rerender } = render(<ScanCausalNote variant="form" mode="monitoring" />)
    expect(screen.getByTestId('scan-causal-note')).toHaveTextContent('Metric points are recorded')

    rerender(<ScanCausalNote variant="form" mode="catalog" />)
    expect(screen.queryByTestId('scan-causal-note')).toBeNull()
  })

  it('tones only the fault as a warning', () => {
    const { rerender } = render(<ScanCausalNote variant="config" config={config('ts', '1d')} />)
    expect(screen.getByTestId('scan-causal-note')).toHaveStyle({ color: 'var(--fg-subtle)' })

    rerender(<ScanCausalNote variant="config" config={config(null, '1d')} />)
    expect(screen.getByTestId('scan-causal-note')).toHaveStyle({ color: 'var(--warning)' })

    // Catalog-only is a deliberate answer and must stay neutral.
    rerender(<ScanCausalNote variant="config" config={config(null, null)} />)
    expect(screen.getByTestId('scan-causal-note')).toHaveStyle({ color: 'var(--fg-subtle)' })
  })

  it('degrades to a cadence-free sentence for an interval it has no label for', () => {
    // A new interval added to the backend enum must not print a raw token.
    const note = noteText(<ScanCausalNote variant="config" config={config('ts', '30m')} />)

    expect(note).toContain('Runs on its schedule.')
    expect(note).not.toContain('30m')
  })
})
