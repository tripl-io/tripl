import { render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { PlanBranchSummary } from '@/types'
import { BranchList } from './BranchList'

function makeBranch(overrides: Partial<PlanBranchSummary>): PlanBranchSummary {
  return {
    id: 'main-1',
    project_id: 'p-1',
    name: 'main',
    kind: 'main',
    status: 'merged',
    description: '',
    base_revision_id: null,
    created_by: null,
    merged_at: null,
    merged_by: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

const MAIN = makeBranch({})
const FEATURE = makeBranch({
  id: 'feat-1',
  name: 'feature/checkout-funnel',
  kind: 'working',
  status: 'draft',
})

function renderList(selectedId: string | null, activeBranchId: string | null) {
  render(
    <BranchList
      items={[MAIN, FEATURE]}
      selectedId={selectedId}
      activeBranchId={activeBranchId}
      countsByBranch={new Map()}
      usersById={new Map()}
      onSelect={vi.fn()}
    />,
  )
  return {
    mainRow: screen.getByRole('button', { name: /^main/ }),
    featureRow: screen.getByRole('button', { name: /feature\/checkout-funnel/ }),
  }
}

describe('BranchList current-branch badge', () => {
  it('marks the switcher’s branch "Current", whichever row is open', () => {
    // Main is what the app shows; the feature branch is the row open on the right.
    const { mainRow, featureRow } = renderList('feat-1', null)

    expect(featureRow).toHaveAttribute('aria-current', 'true')
    const badge = within(mainRow).getByText('Current')
    expect(badge).toHaveAttribute('title', expect.stringMatching(/branch picker in the sidebar/))
    expect(within(featureRow).queryByText('Current')).toBeNull()
    expect(screen.queryByText(/You.re here/)).toBeNull()
  })

  it('moves to the feature branch once the switcher is on it', () => {
    const { mainRow, featureRow } = renderList('main-1', 'feat-1')

    expect(within(featureRow).getByText('Current')).toBeInTheDocument()
    expect(within(mainRow).queryByText('Current')).toBeNull()
  })
})
