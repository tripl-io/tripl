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
    expect(await screen.findByText('Off (TELEMETRY_ENABLED is not set)')).toBeInTheDocument()
    expect(screen.getByText('Never')).toBeInTheDocument()
    expect(screen.queryByTestId('telemetry-payload')).toBeNull()
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
