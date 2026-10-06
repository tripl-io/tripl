import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DependencyEdge } from '@/types'
import { BranchImpactPanel } from './BranchImpactPanel'

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

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <BranchImpactPanel slug="demo" branchId="b-1" />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('BranchImpactPanel (#257)', () => {
  beforeEach(() => {
    vi.mocked(dependenciesApi.branchImpact).mockReset()
  })

  it('lists each change that touches something, with what it touches', async () => {
    vi.mocked(dependenciesApi.branchImpact).mockResolvedValue({
      items: [
        {
          change: { kind: 'event', id: 'e-1', change: 'rename' },
          name: 'checkout:completed',
          affected: [edge({}), edge({ kind: 'alert_rule', id: 'a-1', name: 'Drop' })],
          summary: '1 metric and 1 alert rule',
        },
        { change: { kind: 'field', id: 'f-1', change: 'delete' }, name: 'plan', affected: [], summary: '' },
      ],
    })
    renderPanel()

    expect(await screen.findByText('checkout:completed')).toBeInTheDocument()
    expect(screen.getByText('event renamed:')).toBeInTheDocument()
    expect(screen.getByText('1 change touches 1 metric and 1 alert rule.')).toBeInTheDocument()
    // A change that touches nothing is not listed.
    expect(screen.queryByText('plan')).toBeNull()
    expect(screen.getByRole('link', { name: 'Checkout rate' })).toBeInTheDocument()
    // The dependents fold under the change's line until asked for: one
    // property's 54 events used to push the branch's changes off the screen.
    const fold = screen.getByText('checkout:completed').closest('details')
    expect(fold).not.toBeNull()
    expect(fold).not.toHaveAttribute('open')
    expect(screen.getByText(/nothing here blocks it/)).toBeInTheDocument()
    expect(dependenciesApi.branchImpact).toHaveBeenCalledWith('demo', 'b-1', expect.anything())
  })

  it('links a deleted or renamed entity\'s dependents on main, a changed one\'s on the branch', async () => {
    vi.mocked(dependenciesApi.branchImpact).mockResolvedValue({
      items: [
        {
          change: { kind: 'event', id: 'e-1', change: 'delete' },
          name: 'checkout:completed',
          affected: [edge({ id: 'm-1', name: 'Checkout rate' })],
          summary: '1 metric',
        },
        {
          change: { kind: 'field', id: 'f-1', change: 'change' },
          name: 'plan',
          affected: [edge({ id: 'm-2', name: 'Plan mix' })],
          summary: '1 metric',
        },
        {
          change: { kind: 'event', id: 'e-2', change: 'deprecate' },
          name: 'cart:viewed',
          affected: [edge({ id: 'm-3', name: 'Cart views' })],
          summary: '1 metric',
        },
      ],
    })
    renderPanel()

    const onMain = await screen.findByRole('link', { name: 'Checkout rate' })
    expect(onMain.getAttribute('href')).not.toContain('branch=')
    expect(screen.getByRole('link', { name: 'Plan mix' }).getAttribute('href')).toContain('branch=b-1')
    expect(screen.getByRole('link', { name: 'Cart views' }).getAttribute('href')).toContain('branch=b-1')
  })

  it('says when nothing downstream is affected', async () => {
    vi.mocked(dependenciesApi.branchImpact).mockResolvedValue({ items: [] })
    renderPanel()
    expect(await screen.findByText('No change on this branch affects anything downstream.')).toBeInTheDocument()
    expect(screen.getByText(/renamed and changed \(edited in place/)).toBeInTheDocument()
  })
})
