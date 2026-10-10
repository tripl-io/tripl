import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import type { DependencyEdge } from '@/types'
import { UsedByList } from './UsedByList'

function edge(overrides: Partial<DependencyEdge>): DependencyEdge {
  return {
    kind: 'event',
    id: 'e-1',
    name: 'Home Screen View',
    relation: 'event belongs to event type',
    certainty: 'direct',
    url_hint: null,
    ...overrides,
  }
}

function renderList(edges: DependencyEdge[], compact = false) {
  return render(
    <MemoryRouter>
      <UsedByList slug="demo" edges={edges} compact={compact} />
    </MemoryRouter>,
  )
}

describe('UsedByList relation sentences', () => {
  it('says a reason every row shares once, under the group heading', () => {
    renderList([
      edge({}),
      edge({ id: 'e-2', name: 'Buy Button Click' }),
      edge({ id: 'e-3', name: 'Purchase Completed' }),
    ])
    expect(screen.getAllByText('event belongs to event type')).toHaveLength(1)
    expect(screen.getByTestId('used-by-shared-relation')).toHaveTextContent('event belongs to event type')
    expect(within(screen.getByRole('list', { name: 'events' })).getAllByRole('listitem')).toHaveLength(3)
  })

  it('keeps the reason on each row when the reasons differ', () => {
    renderList([
      edge({ relation: 'event uses property in a field value' }),
      edge({ id: 'e-2', name: 'Buy Button Click', relation: 'property observed on event' }),
    ])
    expect(screen.queryByTestId('used-by-shared-relation')).toBeNull()
    expect(screen.getByText('event uses property in a field value')).toBeInTheDocument()
    expect(screen.getByText('property observed on event')).toBeInTheDocument()
  })

  it('keeps a lone row its own reason', () => {
    renderList([edge({})])
    expect(screen.queryByTestId('used-by-shared-relation')).toBeNull()
    expect(screen.getByText('event belongs to event type')).toBeInTheDocument()
  })

  it('never folds a possible match: its sentence says where the name matched', () => {
    renderList([
      edge({ kind: 'metric', id: 'm-1', name: 'Revenue', relation: 'metric SQL mentions the column by name', certainty: 'possible' }),
      edge({ kind: 'metric', id: 'm-2', name: 'Orders', relation: 'metric SQL mentions the column by name', certainty: 'possible' }),
    ])
    expect(screen.queryByTestId('used-by-shared-relation')).toBeNull()
    expect(screen.getAllByText('metric SQL mentions the column by name')).toHaveLength(2)
  })

  it('shows no folded reason in compact rows', () => {
    renderList([edge({}), edge({ id: 'e-2', name: 'Buy Button Click' })], true)
    expect(screen.queryByTestId('used-by-shared-relation')).toBeNull()
    expect(screen.queryByText('event belongs to event type')).toBeNull()
  })
})
