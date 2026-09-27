// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { aiApi } from '@/api/ai'
import { incidentSummaryApi, type IncidentSummaryResponse } from '@/api/incidentSummary'
import { ActiveProjectContext } from '@/components/active-project-context'
import type { Project } from '@/types'

import { IncidentSummary } from './IncidentSummary'

vi.mock('@/api/ai', () => ({
  aiApi: { status: vi.fn() },
}))

vi.mock('@/api/incidentSummary', () => ({
  incidentSummaryApi: {
    get: vi.fn(),
    ensure: vi.fn(),
    regenerate: vi.fn(),
  },
}))

const GID = '5b0c1d2e-0000-4000-8000-000000000001'

const READY: IncidentSummaryResponse = {
  correlation_group_id: GID,
  state: 'ready',
  disabled_reason: null,
  current_facts_hash: 'h1',
  summary: {
    sentences: [
      {
        text: 'Checkout completions dropped 42% below the expected count.',
        role: 'what_broke',
        fact_ids: [1],
        generated: true,
      },
      {
        text: 'The drop is concentrated in app version 5.2.0.',
        role: 'cause',
        fact_ids: [2],
        generated: true,
      },
    ],
    facts: [
      {
        id: 1,
        kind: 'incident',
        text: 'Drop on checkout_completed: 580 vs 1000 expected (-42%).',
        href: `/p/demo-shop/alerting?incident=${GID}`,
      },
      {
        id: 2,
        kind: 'attribution',
        text: 'app_version 5.2.0 explains 80% of the change.',
        href: '/p/demo-shop/monitoring/event/ev-1',
      },
    ],
    cause_known: true,
    facts_hash: 'h1',
    generated_at: '2026-09-27T10:00:00Z',
  },
}

function newClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
}

function wrap(ui: ReactNode, client: QueryClient = newClient()) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  )
}

const MISSING: IncidentSummaryResponse = {
  correlation_group_id: GID,
  state: 'missing',
  disabled_reason: null,
  current_facts_hash: 'h1',
  summary: null,
}

describe('IncidentSummary (F14, #267)', () => {
  beforeEach(() => {
    vi.mocked(aiApi.status).mockReset()
    vi.mocked(incidentSummaryApi.get).mockReset()
    vi.mocked(incidentSummaryApi.ensure).mockReset()
    vi.mocked(incidentSummaryApi.regenerate).mockReset()
  })

  it('renders nothing and requests no summary while AI is off', async () => {
    vi.mocked(aiApi.status).mockResolvedValue({ enabled: false })
    const { container } = wrap(
      <IncidentSummary slug="demo-shop" correlationGroupId={GID} canWrite defaultOpen />,
    )
    await waitFor(() => expect(aiApi.status).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
    expect(incidentSummaryApi.get).not.toHaveBeenCalled()
    expect(incidentSummaryApi.ensure).not.toHaveBeenCalled()
  })

  it('renders nothing when the server says disabled', async () => {
    vi.mocked(aiApi.status).mockResolvedValue({ enabled: true })
    vi.mocked(incidentSummaryApi.get).mockResolvedValue({
      correlation_group_id: GID,
      state: 'disabled',
      disabled_reason: 'demo',
      current_facts_hash: null,
      summary: null,
    })
    const { container } = wrap(
      <IncidentSummary slug="demo-shop" correlationGroupId={GID} canWrite defaultOpen />,
    )
    await waitFor(() => expect(incidentSummaryApi.get).toHaveBeenCalled())
    await waitFor(() => expect(container).toBeEmptyDOMElement())
    expect(incidentSummaryApi.ensure).not.toHaveBeenCalled()
  })

  it('renders nothing and requests nothing in a demo project', async () => {
    vi.mocked(aiApi.status).mockResolvedValue({ enabled: true })
    vi.mocked(incidentSummaryApi.get).mockResolvedValue(READY)
    const demo = { slug: 'demo-shop', is_demo: true } as Project
    const { container } = wrap(
      <ActiveProjectContext.Provider value={demo}>
        <IncidentSummary slug="demo-shop" correlationGroupId={GID} canWrite />
      </ActiveProjectContext.Provider>,
    )
    await waitFor(() => expect(aiApi.status).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()
    expect(screen.queryByRole('button', { name: 'Summary' })).toBeNull()
    expect(incidentSummaryApi.get).not.toHaveBeenCalled()
  })

  it('after a failed first generation, a remount offers Retry and does not generate again', async () => {
    vi.mocked(aiApi.status).mockResolvedValue({ enabled: true })
    vi.mocked(incidentSummaryApi.get).mockResolvedValue(MISSING)
    vi.mocked(incidentSummaryApi.ensure).mockResolvedValue({ ...MISSING, state: 'failed' })
    const client = newClient()
    const first = wrap(
      <IncidentSummary slug="demo-shop" correlationGroupId={GID} canWrite defaultOpen />,
      client,
    )
    expect(await screen.findByText('Summary unavailable.')).toBeInTheDocument()
    first.unmount()

    // The cached "failed" is gone; GET answers "missing" for the same facts again.
    client.removeQueries({ queryKey: ['incidentSummary', 'demo-shop', GID], exact: true })
    wrap(<IncidentSummary slug="demo-shop" correlationGroupId={GID} canWrite defaultOpen />, client)
    await waitFor(() => expect(incidentSummaryApi.get).toHaveBeenCalledTimes(2))
    expect(await screen.findByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(incidentSummaryApi.ensure).toHaveBeenCalledTimes(1)
  })

  it('stays collapsed and fetches nothing until opened', async () => {
    vi.mocked(aiApi.status).mockResolvedValue({ enabled: true })
    vi.mocked(incidentSummaryApi.get).mockResolvedValue(READY)
    wrap(<IncidentSummary slug="demo-shop" correlationGroupId={GID} canWrite={false} />)

    const toggle = await screen.findByRole('button', { name: 'Summary' })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(incidentSummaryApi.get).not.toHaveBeenCalled()

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(
      await screen.findByText('Checkout completions dropped 42% below the expected count.'),
    ).toBeInTheDocument()
    expect(incidentSummaryApi.get).toHaveBeenCalledWith('demo-shop', GID)
  })

  it('generates once when the summary is missing', async () => {
    vi.mocked(aiApi.status).mockResolvedValue({ enabled: true })
    vi.mocked(incidentSummaryApi.get).mockResolvedValue({
      correlation_group_id: GID,
      state: 'missing',
      disabled_reason: null,
      current_facts_hash: 'h1',
      summary: null,
    })
    vi.mocked(incidentSummaryApi.ensure).mockResolvedValue(READY)
    wrap(<IncidentSummary slug="demo-shop" correlationGroupId={GID} canWrite defaultOpen />)

    expect(
      await screen.findByText('The drop is concentrated in app version 5.2.0.'),
    ).toBeInTheDocument()
    expect(incidentSummaryApi.ensure).toHaveBeenCalledTimes(1)
    expect(incidentSummaryApi.ensure).toHaveBeenCalledWith('demo-shop', GID)
  })

  it('links each citation to its fact', async () => {
    vi.mocked(aiApi.status).mockResolvedValue({ enabled: true })
    vi.mocked(incidentSummaryApi.get).mockResolvedValue(READY)
    wrap(<IncidentSummary slug="demo-shop" correlationGroupId={GID} canWrite defaultOpen />)

    const first = await screen.findByRole('link', {
      name: 'Source 1: Drop on checkout_completed: 580 vs 1000 expected (-42%).',
    })
    expect(first).toHaveAttribute('href', `/p/demo-shop/alerting?incident=${GID}`)
    expect(
      screen.getByRole('link', { name: 'Source 2: app_version 5.2.0 explains 80% of the change.' }),
    ).toHaveAttribute('href', '/p/demo-shop/monitoring/event/ev-1')
    expect(screen.getByText('Sources')).toBeInTheDocument()
    expect(incidentSummaryApi.ensure).not.toHaveBeenCalled()
  })

  it('shows the unknown-cause sentence muted', async () => {
    vi.mocked(aiApi.status).mockResolvedValue({ enabled: true })
    vi.mocked(incidentSummaryApi.get).mockResolvedValue({
      ...READY,
      summary: {
        ...READY.summary!,
        sentences: [
          ...READY.summary!.sentences.slice(0, 1),
          {
            text: 'The cause is unknown: no attribution, release or past verdict points to one.',
            role: 'cause',
            fact_ids: [],
            generated: false,
          },
        ],
        cause_known: false,
      },
    })
    wrap(<IncidentSummary slug="demo-shop" correlationGroupId={GID} canWrite defaultOpen />)

    const unknown = await screen.findByText(/The cause is unknown/)
    expect(unknown).toHaveClass('text-fg-tertiary')
  })

  it('says the summary is unavailable when generation failed, with Retry for editors', async () => {
    vi.mocked(aiApi.status).mockResolvedValue({ enabled: true })
    const failed: IncidentSummaryResponse = {
      correlation_group_id: GID,
      state: 'failed',
      disabled_reason: null,
      current_facts_hash: 'h1',
      summary: null,
    }
    vi.mocked(incidentSummaryApi.get).mockResolvedValue({ ...failed, state: 'missing' })
    vi.mocked(incidentSummaryApi.ensure).mockResolvedValue(failed)
    wrap(<IncidentSummary slug="demo-shop" correlationGroupId={GID} canWrite defaultOpen />)

    expect(await screen.findByText('Summary unavailable.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(incidentSummaryApi.ensure).toHaveBeenCalledTimes(2))
  })

  it('offers a viewer neither Regenerate nor Retry', async () => {
    vi.mocked(aiApi.status).mockResolvedValue({ enabled: true })
    vi.mocked(incidentSummaryApi.get).mockResolvedValue(READY)
    wrap(
      <IncidentSummary slug="demo-shop" correlationGroupId={GID} canWrite={false} defaultOpen />,
    )
    await screen.findByText('Checkout completions dropped 42% below the expected count.')
    expect(screen.queryByRole('button', { name: 'Regenerate' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it('lets an editor regenerate', async () => {
    vi.mocked(aiApi.status).mockResolvedValue({ enabled: true })
    vi.mocked(incidentSummaryApi.get).mockResolvedValue(READY)
    vi.mocked(incidentSummaryApi.regenerate).mockResolvedValue(READY)
    wrap(<IncidentSummary slug="demo-shop" correlationGroupId={GID} canWrite defaultOpen />)

    fireEvent.click(await screen.findByRole('button', { name: 'Regenerate' }))
    await waitFor(() =>
      expect(incidentSummaryApi.regenerate).toHaveBeenCalledWith('demo-shop', GID),
    )
  })
})
