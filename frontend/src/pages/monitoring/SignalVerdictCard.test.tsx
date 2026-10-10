import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { MonitoringSignal, SignalVerdictInfo } from '@/types'
import { SignalVerdictCard } from './SignalVerdictCard'

vi.mock('@/api/eventMetrics', () => ({
  eventMetricsApi: {
    setSignalVerdict: vi.fn(),
    clearSignalVerdict: vi.fn(),
  },
}))
vi.mock('@/api/alerting', () => ({
  alertingApi: {
    notifySignalOwners: vi.fn(),
  },
}))
vi.mock('sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}))

import { alertingApi } from '@/api/alerting'
import { eventMetricsApi } from '@/api/eventMetrics'

function makeSignal(overrides: Partial<MonitoringSignal> = {}): MonitoringSignal {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event',
    scope_ref: 'ev-1',
    state: 'latest_scan',
    event_id: 'ev-1',
    event_type_id: 'et-1',
    bucket: '2026-09-25T18:00:00Z',
    actual_count: 10,
    expected_count: 40,
    stddev: 5,
    z_score: -6,
    direction: 'drop',
    scope_name: 'signup_completed',
    incident_child: false,
    muted: false,
    expected: false,
    hidden: false,
    attribution_status: 'not_computed',
    unit: null,
    detected_at: null,
    ...overrides,
  }
}

const VERDICT: SignalVerdictInfo = {
  verdict: 'tracking_bug',
  expected_reason: null,
  note: 'Param renamed in 5.2',
  author_name: 'Ann Lee',
  created_at: '2026-09-25T19:00:00Z',
  source: 'signal',
}

function renderCard(props: Partial<Parameters<typeof SignalVerdictCard>[0]> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <SignalVerdictCard slug="demo" signal={makeSignal()} canWrite {...props} />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(eventMetricsApi.setSignalVerdict).mockResolvedValue({})
  vi.mocked(eventMetricsApi.clearSignalVerdict).mockResolvedValue(undefined)
})

describe('SignalVerdictCard (#254)', () => {
  it('says an unrouted signal without a verdict needs one', () => {
    renderCard()
    expect(screen.getByRole('region', { name: 'Signal' })).toBeInTheDocument()
    expect(screen.getByText('Not routed')).toBeInTheDocument()
    expect(screen.getByText('Needs a verdict.')).toBeInTheDocument()
  })

  it('requires a reason for expected, then saves it with the note', async () => {
    renderCard()
    fireEvent.click(screen.getByRole('button', { name: 'Expected' }))
    const save = screen.getByRole('button', { name: 'Save verdict' })
    expect(save).toBeDisabled()
    // Says why it is disabled, tied to the button.
    expect(save).toHaveAccessibleDescription('Pick a reason to save.')

    fireEvent.click(
      within(screen.getByRole('group', { name: 'Reason (required)' })).getByRole('button', {
        name: 'Campaign',
      }),
    )
    expect(screen.queryByText('Pick a reason to save.')).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText(/^Note/), {
      target: { value: ' Spring sale ' },
    })
    fireEvent.click(save)

    await waitFor(() =>
      expect(eventMetricsApi.setSignalVerdict).toHaveBeenCalledWith('demo', {
        scan_config_id: 'scan-1',
        scope_type: 'event',
        scope_ref: 'ev-1',
        bucket: '2026-09-25T18:00:00Z',
        verdict: 'expected',
        expected_reason: 'campaign',
        note: 'Spring sale',
      }),
    )
  })

  it('shows the current verdict with its author and note, and clears it', async () => {
    renderCard({ signal: makeSignal({ verdict: VERDICT }) })
    expect(screen.getByText('Tracking bug', { selector: '[data-slot="chip"]' })).toBeInTheDocument()
    expect(screen.getByText(/by Ann Lee/)).toBeInTheDocument()
    expect(screen.getByText('Param renamed in 5.2', { selector: 'p' })).toBeInTheDocument()

    // Nothing changed yet: Save says so.
    expect(screen.getByRole('button', { name: 'Save verdict' })).toHaveAccessibleDescription(
      'No changes to save.',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Clear verdict' }))
    await waitFor(() => expect(eventMetricsApi.clearSignalVerdict).toHaveBeenCalled())
  })

  it("asks before clearing a routed signal's verdict, since that reopens its incident", async () => {
    renderCard({
      signal: makeSignal({ incident: { id: 'group-1', status: 'acknowledged' }, verdict: VERDICT }),
    })
    fireEvent.click(screen.getByRole('button', { name: 'Clear verdict and reopen incident' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(eventMetricsApi.clearSignalVerdict).not.toHaveBeenCalled()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Clear and reopen' }))
    await waitFor(() =>
      expect(eventMetricsApi.clearSignalVerdict).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ scope_ref: 'ev-1' }),
      ),
    )
  })

  it('names the incident and says a verdict updates it; its verdict is not cleared here', () => {
    renderCard({
      signal: makeSignal({
        incident: { id: 'group-1', status: 'acknowledged' },
        verdict: { ...VERDICT, verdict: 'real_issue', source: 'incident' },
      }),
    })
    // The accessible name starts with the chip's visible text.
    const link = screen.getByRole('link', { name: /^Incident · Acknowledged/ })
    expect(link.getAttribute('href')).toContain('group-1')
    expect(screen.getByText(/From the incident/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'False positive' }))
    expect(screen.getByText(/saving marks the incident a false positive/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Clear verdict' })).not.toBeInTheDocument()
  })

  it('offers a prefilled comment on the event for a tracking bug', () => {
    const onOpenComment = vi.fn()
    renderCard({ onOpenComment })
    expect(screen.queryByRole('button', { name: /Open a comment/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Tracking bug' }))
    fireEvent.change(screen.getByLabelText(/Note/), { target: { value: 'SDK regression' } })
    fireEvent.click(screen.getByRole('button', { name: 'Open a comment on the event' }))
    expect(onOpenComment).toHaveBeenCalledWith(expect.stringMatching(/^Tracking bug: drop at /))
    expect(onOpenComment.mock.calls[0]?.[0]).toContain('SDK regression')
  })

  it('is read-only for a viewer', () => {
    renderCard({ canWrite: false, signal: makeSignal({ verdict: VERDICT }), onOpenComment: vi.fn() })
    expect(screen.getByText(/by Ann Lee/)).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Verdict' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Open a comment/ })).not.toBeInTheDocument()
  })
})

describe('SignalVerdictCard owners (F07, #260)', () => {
  const owners = [{ user_id: 'u-1', name: 'anna' }]

  it('emails the owners of an unrouted signal on request', async () => {
    vi.mocked(alertingApi.notifySignalOwners).mockResolvedValue([
      { user_id: 'u-1', name: 'anna', email: 'anna@x.io', status: 'sent' },
    ])
    renderCard({ signal: makeSignal({ owners }) })

    expect(screen.getByText('Event type owner: @anna')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Notify owners of signup_completed by email' }))

    expect(await screen.findByText('Emailed anna.')).toBeInTheDocument()
    expect(alertingApi.notifySignalOwners).toHaveBeenCalledWith('demo', {
      scan_config_id: 'scan-1',
      scope_type: 'event',
      scope_ref: 'ev-1',
      bucket: '2026-09-25T18:00:00Z',
    })
  })

  it('says why an owner inside the manual cooldown was not emailed again', async () => {
    vi.mocked(alertingApi.notifySignalOwners).mockResolvedValue([
      { user_id: 'u-1', name: 'anna', email: 'anna@x.io', status: 'skipped', error: 'notified 3 minutes ago', sent_at: null },
    ])
    renderCard({ signal: makeSignal({ owners }) })

    fireEvent.click(screen.getByRole('button', { name: 'Notify owners of signup_completed by email' }))

    expect(await screen.findByText('Not sent to anna (notified 3 minutes ago).')).toBeInTheDocument()
  })

  it('leaves the button to the incident card on a routed signal', () => {
    renderCard({
      signal: makeSignal({ owners, incident: { id: 'grp-1', status: 'open' } }),
    })
    expect(screen.getByText('Event type owner: @anna')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Notify owners/ })).toBeNull()
  })

  it('offers no button to a viewer', () => {
    renderCard({ signal: makeSignal({ owners }), canWrite: false })
    expect(screen.queryByRole('button', { name: /Notify owners/ })).toBeNull()
  })
})
