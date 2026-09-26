import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { MonitoringSignal, SignalTriageState, SignalVerdict } from '@/types'
import AnomaliesPage from '../AnomaliesPage'

vi.mock('@/api/eventMetrics', () => ({
  eventMetricsApi: {
    getActiveSignals: vi.fn(),
    getSignalSeries: vi.fn(),
    acknowledgeSignal: vi.fn(),
    unacknowledgeSignal: vi.fn(),
    muteSignalScope: vi.fn(),
    unmuteSignalScope: vi.fn(),
    setSignalVerdict: vi.fn(),
    clearSignalVerdict: vi.fn(),
  },
}))
vi.mock('@/api/scans', () => ({
  scansApi: { list: vi.fn() },
}))
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

import { toast } from 'sonner'
import { eventMetricsApi } from '@/api/eventMetrics'
import { scansApi } from '@/api/scans'

const STATE: SignalTriageState = {
  acknowledged_at: null,
  muted: false,
  muted_until: null,
  expected: false,
  expected_note: null,
  hidden: false,
}

function makeSignal(overrides: Partial<MonitoringSignal>): MonitoringSignal {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event_type',
    scope_ref: 'et-1',
    state: 'latest_scan',
    event_id: null,
    event_type_id: 'et-1',
    bucket: '2026-09-25T18:00:00Z',
    actual_count: 120,
    expected_count: 40,
    stddev: 5,
    z_score: 8,
    direction: 'spike',
    scope_name: 'Signup',
    incident_child: false,
    unit: null,
    detected_at: null,
    ...overrides,
  }
}

function LocationProbe() {
  const location = useLocation()
  return <div>anomalies-location:{location.search}</div>
}

function DetailProbe() {
  const location = useLocation()
  const draft = (location.state as { commentDraft?: string } | null)?.commentDraft
  return <div>detail-draft:{draft}</div>
}

function renderAnomalies(entry = '/p/demo/anomalies') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <LocationProbe />
        <Routes>
          <Route path="/p/:slug/anomalies" element={<AnomaliesPage />} />
          <Route path="/p/:slug/monitoring/event/:eventId" element={<DetailProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function rowOf(name: string): HTMLElement {
  const [label] = screen.getAllByText((_content, element) =>
    !!element?.hasAttribute('data-anomaly-label') && (element.textContent ?? '').endsWith(name),
  )
  if (!label) throw new Error(`No anomaly row labelled ${name}`)
  return label.closest('[role="row"]') as HTMLElement
}

async function openMenu(name: string): Promise<void> {
  await screen.findAllByText((_content, element) =>
    !!element?.hasAttribute('data-anomaly-label') && (element.textContent ?? '').endsWith(name),
  )
  fireEvent.keyDown(within(rowOf(name)).getByRole('button', { name: 'Signal actions' }), {
    key: 'Enter',
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(scansApi.list).mockResolvedValue([])
  vi.mocked(eventMetricsApi.getSignalSeries).mockResolvedValue([])
  vi.mocked(eventMetricsApi.acknowledgeSignal).mockResolvedValue({
    ...STATE,
    acknowledged_at: '2026-09-25T19:00:00Z',
  })
  vi.mocked(eventMetricsApi.muteSignalScope).mockResolvedValue({ ...STATE, muted: true, hidden: true })
  vi.mocked(eventMetricsApi.unmuteSignalScope).mockResolvedValue(undefined)
  vi.mocked(eventMetricsApi.setSignalVerdict).mockResolvedValue({})
  vi.mocked(eventMetricsApi.clearSignalVerdict).mockResolvedValue(undefined)
})

const VERDICT: SignalVerdict = {
  verdict: 'tracking_bug',
  expected_reason: null,
  note: 'Param renamed',
  author_name: 'Ann Lee',
  created_at: '2026-09-25T19:00:00Z',
  source: 'signal',
}

describe('AnomaliesPage — triage (MO-4 / JR-5)', () => {
  it('offers acknowledge, mark as expected and the three mute lengths on an unrouted signal', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([makeSignal({})])
    renderAnomalies()
    await openMenu('Signup')

    expect(await screen.findByRole('menuitem', { name: 'Acknowledge' })).toBeInTheDocument()
    expect(screen.getByRole('menuitemradio', { name: 'Mark as expected…' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Mute for 24 hours' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Mute for 7 days' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Mute until unmuted' })).toBeInTheDocument()
  })

  it('keeps triage in the inbox for a signal routed to an incident', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ incident_id: 'group-1', incident_status: 'open' }),
    ])
    renderAnomalies()
    await openMenu('Signup')

    expect(await screen.findByRole('menuitem', { name: 'Open incident' })).toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Acknowledge' })).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: /^Mute / })).not.toBeInTheDocument()
  })

  it('acknowledges with the signal key and confirms with an Undo', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([makeSignal({})])
    renderAnomalies()
    await openMenu('Signup')
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Acknowledge' }))

    await waitFor(() =>
      expect(eventMetricsApi.acknowledgeSignal).toHaveBeenCalledWith('demo', {
        scan_config_id: 'scan-1',
        scope_type: 'event_type',
        scope_ref: 'et-1',
        bucket: '2026-09-25T18:00:00Z',
      }),
    )
    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        'Signal acknowledged',
        expect.objectContaining({ action: expect.objectContaining({ label: 'Undo' }) }),
      ),
    )
  })

  it('mutes the scope for the chosen length', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([makeSignal({})])
    renderAnomalies()
    await openMenu('Signup')
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Mute for 7 days' }))

    await waitFor(() =>
      expect(eventMetricsApi.muteSignalScope).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ scope_ref: 'et-1' }),
        '7d',
      ),
    )
  })

  it('marks as expected with the reason and note picked in the dialog (#254)', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([makeSignal({})])
    renderAnomalies()
    await openMenu('Signup')
    fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Mark as expected…' }))

    const dialog = await screen.findByRole('dialog', { name: 'Mark as expected' })
    const submit = within(dialog).getByRole('button', { name: 'Mark as expected' })
    // The reason is required for an expected verdict.
    expect(submit).toBeDisabled()
    expect(submit).toHaveAccessibleDescription('Pick a reason to save.')
    fireEvent.click(
      within(within(dialog).getByRole('group', { name: 'Reason (required)' })).getByRole('button', {
        name: 'Campaign',
      }),
    )
    fireEvent.change(within(dialog).getByLabelText(/^Note/), {
      target: { value: '  Campaign launch ' },
    })
    fireEvent.click(submit)

    await waitFor(() =>
      expect(eventMetricsApi.setSignalVerdict).toHaveBeenCalledWith('demo', {
        scan_config_id: 'scan-1',
        scope_type: 'event_type',
        scope_ref: 'et-1',
        bucket: '2026-09-25T18:00:00Z',
        verdict: 'expected',
        expected_reason: 'campaign',
        note: 'Campaign launch',
      }),
    )
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: 'Mark as expected' })).not.toBeInTheDocument(),
    )
  })

  it('records a verdict on a routed signal too, saying it updates the incident', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ incident: { id: 'group-1', status: 'open' } }),
    ])
    renderAnomalies()
    await openMenu('Signup')
    fireEvent.click(await screen.findByRole('menuitemradio', { name: 'False positive…' }))

    const dialog = await screen.findByRole('dialog', { name: 'Mark as a false positive' })
    expect(within(dialog).getByText(/marks its incident a false positive/)).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Mark as a false positive' }))

    await waitFor(() =>
      expect(eventMetricsApi.setSignalVerdict).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ verdict: 'false_positive', expected_reason: null, note: null }),
      ),
    )
  })

  it('clears a verdict the signal carries, and offers a comment on its event for a tracking bug', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({
        scope_type: 'event',
        scope_ref: 'ev-1',
        event_id: 'ev-1',
        scope_name: 'Signup',
        verdict: VERDICT,
      }),
    ])
    renderAnomalies('/p/demo/anomalies?verdict=all')
    await openMenu('Signup')
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Clear verdict' }))
    await waitFor(() =>
      expect(eventMetricsApi.clearSignalVerdict).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ scope_ref: 'ev-1' }),
      ),
    )

    await openMenu('Signup')
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Open a comment on the event' }))
    expect(await screen.findByText(/detail-draft:Tracking bug: spike at/)).toBeInTheDocument()
  })

  it('marks the current verdict checked for assistive tech', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([makeSignal({ verdict: VERDICT })])
    renderAnomalies('/p/demo/anomalies?verdict=all')
    await openMenu('Signup')
    expect(await screen.findByRole('menuitemradio', { name: 'Tracking bug…' })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    expect(screen.getByRole('menuitemradio', { name: 'Real issue…' })).toHaveAttribute(
      'aria-checked',
      'false',
    )
  })

  it("asks before clearing a routed signal's verdict, which reopens its incident", async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ incident: { id: 'group-1', status: 'acknowledged' }, verdict: VERDICT }),
    ])
    renderAnomalies('/p/demo/anomalies?verdict=all')
    await openMenu('Signup')
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Clear verdict and reopen incident' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(eventMetricsApi.clearSignalVerdict).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Clear and reopen' }))
    await waitFor(() => expect(eventMetricsApi.clearSignalVerdict).toHaveBeenCalled())
  })

  it('offers the undo of each verdict a signal already carries', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      makeSignal({ acknowledged_at: '2026-09-25T19:00:00Z', muted: true, hidden: true }),
    ])
    renderAnomalies('/p/demo/anomalies?hidden=1')
    await openMenu('Signup')

    expect(await screen.findByRole('menuitem', { name: 'Undo acknowledge' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Unmute scope' }))
    await waitFor(() => expect(eventMetricsApi.unmuteSignalScope).toHaveBeenCalled())
  })
})

describe('AnomaliesPage — hidden signals (MO-4 / JR-5)', () => {
  const signals = [
    makeSignal({ scope_ref: 'et-1', scope_name: 'Signup' }),
    makeSignal({ scope_ref: 'et-2', event_type_id: 'et-2', scope_name: 'Checkout', muted: true, hidden: true }),
    makeSignal({ scope_ref: 'et-3', event_type_id: 'et-3', scope_name: 'Login', expected: true, hidden: true }),
    makeSignal({ scope_ref: 'et-4', event_type_id: 'et-4', scope_name: 'Search', acknowledged_at: '2026-09-25T19:00:00Z' }),
  ]

  it('leaves muted and expected signals out of the list and the counts by default', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue(signals)
    renderAnomalies()

    const table = await screen.findByRole('table', { name: 'Anomaly signals' })
    expect(within(table).getAllByRole('row')).toHaveLength(3) // header + Signup + Search
    expect(within(table).queryByText(/Checkout/)).not.toBeInTheDocument()
    // Acknowledged stays listed, and says so.
    expect(within(rowOf('Search')).getByText(/Acknowledged/)).toBeInTheDocument()
    expect(screen.getByText('2 open')).toBeInTheDocument()
  })

  it('brings hidden signals back, flagged, behind "Show hidden (n)"', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue(signals)
    renderAnomalies()

    const toggle = await screen.findByRole('button', { name: 'Show hidden (2)' })
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(toggle)

    expect(await screen.findByText('anomalies-location:?hidden=1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Show hidden (2)' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(within(rowOf('Checkout')).getByText(/Muted/)).toBeInTheDocument()
    expect(within(rowOf('Login')).getByText(/Expected/)).toBeInTheDocument()
  })

  it('counts only the hidden signals the magnitude level would show', async () => {
    // A muted Minor move (5% over baseline) is hidden too, but the default
    // Significant level keeps it out even with hidden signals shown, so it is
    // not part of the n the button promises.
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue([
      ...signals,
      makeSignal({
        scope_ref: 'et-5',
        event_type_id: 'et-5',
        scope_name: 'Minor',
        actual_count: 42,
        expected_count: 40,
        muted: true,
        hidden: true,
      }),
    ])
    renderAnomalies()

    expect(await screen.findByRole('button', { name: 'Show hidden (2)' })).toBeInTheDocument()
  })

  it('offers "Show hidden" from the empty state when every open signal is hidden', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue(signals.slice(1, 2))
    renderAnomalies()

    expect(await screen.findByText('No anomalies right now')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show hidden (1)' }))
    expect(await screen.findByText('anomalies-location:?hidden=1')).toBeInTheDocument()
  })
})

describe('AnomaliesPage — verdict filter (#254)', () => {
  const signals = [
    makeSignal({ scope_ref: 'et-1', scope_name: 'Signup' }),
    makeSignal({ scope_ref: 'et-2', event_type_id: 'et-2', scope_name: 'Checkout', verdict: VERDICT }),
    makeSignal({
      scope_ref: 'et-3',
      event_type_id: 'et-3',
      scope_name: 'Login',
      verdict: { ...VERDICT, verdict: 'real_issue', source: 'incident' },
      incident: { id: 'group-3', status: 'acknowledged' },
    }),
  ]

  it('lists only the signals that need a verdict by default', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue(signals)
    renderAnomalies()

    const table = await screen.findByRole('table', { name: 'Anomaly signals' })
    expect(within(table).getAllByRole('row')).toHaveLength(2) // header + Signup
    expect(screen.getByText('1 of 3 open · 2 with a verdict')).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: 'Verdict filter: Needs verdict' })).toBeInTheDocument()
  })

  it('shows every verdict under ?verdict=all, each row naming its verdict', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue(signals)
    renderAnomalies('/p/demo/anomalies?verdict=all')

    const table = await screen.findByRole('table', { name: 'Anomaly signals' })
    expect(within(table).getAllByRole('row')).toHaveLength(4)
    expect(within(rowOf('Checkout')).getByText(/Tracking bug/)).toBeInTheDocument()
    expect(within(rowOf('Login')).getByText(/Real issue/)).toBeInTheDocument()
    expect(within(rowOf('Login')).getByRole('link', { name: /Incident · acknowledged/ })).toBeInTheDocument()
  })

  it('lists one verdict when one is picked', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue(signals)
    renderAnomalies('/p/demo/anomalies?verdict=tracking_bug')

    const table = await screen.findByRole('table', { name: 'Anomaly signals' })
    expect(within(table).getAllByRole('row')).toHaveLength(2) // header + Checkout
    expect(within(table).getByText(/Checkout/)).toBeInTheDocument()
  })

  it('says so when every open signal has a verdict, and clears the filter', async () => {
    vi.mocked(eventMetricsApi.getActiveSignals).mockResolvedValue(signals.slice(1))
    renderAnomalies()

    expect(await screen.findByText('Every open signal has a verdict')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Show all verdicts (2)' }))
    expect(await screen.findByText('anomalies-location:?verdict=all')).toBeInTheDocument()
  })
})
