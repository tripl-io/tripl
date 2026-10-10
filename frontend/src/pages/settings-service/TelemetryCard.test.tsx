import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { serviceSettingsApi, type TelemetryStatus } from '@/api/serviceSettings'
import { TelemetryCard } from './TelemetryCard'

/** Settings › Platform › Runtime › Usage telemetry (C2): read-only. */

const OFF: TelemetryStatus = {
  enabled: false,
  reason: 'disabled',
  endpoint: 'https://telemetry.tripl.io/v1/ping',
  instance_id: null,
  last_attempt_at: null,
  last_delivered: null,
  last_payload: null,
}

function renderCard(status: TelemetryStatus) {
  vi.spyOn(serviceSettingsApi, 'telemetry').mockResolvedValue(status)
  mount()
}

function mount() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <TelemetryCard />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('TelemetryCard', () => {
  it('says it is off, and why', async () => {
    renderCard(OFF)
    expect(await screen.findByText('Off (TELEMETRY_ENABLED=false)')).toBeInTheDocument()
    expect(screen.getByText('Never')).toBeInTheDocument()
    expect(screen.queryByTestId('telemetry-payload')).toBeNull()
  })

  it.each([
    ['do not track', 'Off (DO_NOT_TRACK is set)'],
    ['enterprise default', 'Off (Enterprise default; TELEMETRY_ENABLED=true turns it on)'],
  ])('names %s as the reason', async (reason, label) => {
    renderCard({ ...OFF, reason })
    expect(await screen.findByText(label)).toBeInTheDocument()
  })

  /**
   * The card is the app's one disclosure of an on-by-default ping. It named
   * only TELEMETRY_ENABLED, never said a restart is needed, and had no link to
   * what the ping holds.
   */
  it('says how to turn it off, and links to what it sends', async () => {
    renderCard({ ...OFF, enabled: true, reason: null })
    expect(await screen.findByText('On')).toBeInTheDocument()
    const description = screen.getByText(/One anonymous ping a day/)
    expect(description).toHaveTextContent('TELEMETRY_ENABLED=false')
    expect(description).toHaveTextContent('DO_NOT_TRACK=1')
    expect(description).toHaveTextContent(/restart/)
    expect(screen.getByRole('link', { name: /What it sends/ })).toHaveAttribute(
      'href',
      'https://docs.tripl.io/run/telemetry',
    )
  })

  /** A failed status read used to remove the disclosure along with the rows. */
  it('keeps the disclosure when the status cannot be read', async () => {
    vi.spyOn(serviceSettingsApi, 'telemetry').mockRejectedValue(new Error('boom'))
    mount()
    expect(await screen.findByText('Unknown (the status could not be read)')).toBeInTheDocument()
    expect(screen.getByText(/One anonymous ping a day/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /What it sends/ })).toBeInTheDocument()
  })

  it('shows exactly what it last sent', async () => {
    renderCard({
      ...OFF,
      enabled: true,
      reason: null,
      instance_id: 'i-1',
      last_attempt_at: '2026-10-05T07:13:00Z',
      last_delivered: false,
      last_payload: { schema: 1, projects: '1-10', warehouse_engines: ['clickhouse'] },
    })
    expect(await screen.findByText('On')).toBeInTheDocument()
    expect(screen.getByText(/\(not delivered\)/)).toBeInTheDocument()
    const payload = screen.getByTestId('telemetry-payload')
    expect(payload).toHaveTextContent('"projects": "1-10"')
    expect(payload).toHaveTextContent('"clickhouse"')
  })
})
