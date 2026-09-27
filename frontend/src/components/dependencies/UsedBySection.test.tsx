import { fireEvent, render, screen, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DependenciesResponse, DependencyEdge } from '@/types'
import { UsedBySection } from './UsedBySection'

vi.mock('@/api/dependencies', () => ({
  dependenciesApi: { get: vi.fn(), impact: vi.fn(), branchImpact: vi.fn() },
}))

import { dependenciesApi } from '@/api/dependencies'

function edge(overrides: Partial<DependencyEdge>): DependencyEdge {
  return {
    kind: 'metric',
    id: 'm-1',
    name: 'Checkout rate',
    relation: 'metric uses event in its composition',
    certainty: 'direct',
    url_hint: null,
    ...overrides,
  }
}

function response(downstream: DependencyEdge[]): DependenciesResponse {
  return {
    entity: { kind: 'event', id: 'e-1', name: 'checkout', exists: true },
    upstream: [],
    downstream,
    counts_by_kind: {},
    possible_counts_by_kind: {},
  }
}

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UsedBySection slug="demo" entity={{ kind: 'event', id: 'e-1' }} branchId="b-1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('UsedBySection (#257)', () => {
  beforeEach(() => {
    vi.mocked(dependenciesApi.get).mockReset()
  })

  it('lists dependents grouped by kind, with links and the reason', async () => {
    vi.mocked(dependenciesApi.get).mockResolvedValue(
      response([
        edge({}),
        edge({ id: 'm-2', name: 'Revenue' }),
        edge({ kind: 'alert_rule', id: 'a-1', name: 'Checkout drop', relation: 'alert rule filters on event' }),
      ]),
    )
    renderSection()

    expect(await screen.findByText('Used by 2 metrics and 1 alert rule.')).toBeInTheDocument()
    const metrics = screen.getByRole('list', { name: 'metrics' })
    expect(within(metrics).getAllByRole('listitem')).toHaveLength(2)
    // Links follow the kind's route (the branch on screen rides along when
    // there is one; this harness has none).
    expect(screen.getByRole('link', { name: 'Checkout rate' })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/metric/m-1',
    )
    expect(screen.getByRole('link', { name: 'Checkout drop' })).toHaveAttribute(
      'href',
      '/p/demo/monitors/a-1',
    )
    expect(screen.getByText('alert rule filters on event')).toBeInTheDocument()
    expect(dependenciesApi.get).toHaveBeenCalledWith(
      'demo',
      { kind: 'event', id: 'e-1' },
      { depth: 1, branchId: 'b-1' },
      expect.anything(),
    )
  })

  it('marks a SQL match as possible and explains it', async () => {
    vi.mocked(dependenciesApi.get).mockResolvedValue(
      response([edge({ certainty: 'possible', relation: 'column named in SQL' })]),
    )
    renderSection()

    const badge = await screen.findByRole('button', { name: /Possible: matched by name, not by a stored reference/ })
    expect(badge).toHaveTextContent('possible')
    expect(screen.getByText(/matched by name, not by a stored reference; check the query/)).toBeInTheDocument()
    expect(screen.getByText('Used by 1 possible metric.')).toBeInTheDocument()
    expect(screen.getByText('column named in SQL')).toBeInTheDocument()
  })

  it('says so when nothing depends on the entity', async () => {
    vi.mocked(dependenciesApi.get).mockResolvedValue(response([]))
    renderSection()
    expect(await screen.findByText('Nothing in this project depends on this event.')).toBeInTheDocument()
  })

  it('offers a retry when the lookup fails', async () => {
    vi.mocked(dependenciesApi.get).mockRejectedValueOnce(new Error('boom'))
    vi.mocked(dependenciesApi.get).mockResolvedValueOnce(response([]))
    renderSection()
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Nothing in this project depends on this event.')).toBeInTheDocument()
  })
})
