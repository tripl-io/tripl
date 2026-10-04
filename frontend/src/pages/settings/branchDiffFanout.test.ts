import { describe, expect, it } from 'vitest'
import type { PlanBranchListItem } from '@/api/planBranches'
import type { PlanBranchDiffSummary } from '@/types'
import { rowBadgeCounts } from './branchDiffFanout'

function makeBranch(overrides: Partial<PlanBranchListItem>): PlanBranchListItem {
  return {
    id: 'b-1',
    project_id: 'p-1',
    name: 'feature',
    kind: 'working',
    status: 'draft',
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

const MAIN = makeBranch({ id: 'main', name: 'main', kind: 'main', status: 'merged' })

// A branch whose only change is one rename: two raw entries, one paired row.
const RENAME_DIFF: PlanBranchDiffSummary = {
  behind_base: false,
  summary: { added: 1, removed: 1, changed: 0 },
  entries: [],
  renames: [{ entity_type: 'variable', parent: null, removed_name: 'a', added_name: 'b' }],
}

// The badges used to cost one 2-3.5 s diff per branch; the list endpoint now
// carries the counts.
describe('rowBadgeCounts', () => {
  it('reads each open branch its counts from the list', () => {
    const items = [
      MAIN,
      makeBranch({ id: 'b-1', ahead: 3, behind_base: false }),
      makeBranch({ id: 'b-2', ahead: 0, behind_base: true }),
    ]

    const counts = rowBadgeCounts(items, null, undefined)

    expect(counts.get('b-1')).toEqual({ ahead: 3, behind: false })
    expect(counts.get('b-2')).toEqual({ ahead: 0, behind: true })
    expect(counts.has('main')).toBe(false)
  })

  it('shows no badge for a landed branch, or one the list did not count', () => {
    const items = [
      makeBranch({ id: 'merged', status: 'merged', ahead: 2, behind_base: false }),
      makeBranch({ id: 'closed', status: 'closed' }),
      makeBranch({ id: 'uncounted', ahead: null, behind_base: null }),
    ]

    expect(rowBadgeCounts(items, 'merged', RENAME_DIFF).size).toBe(0)
  })

  it('counts the selected branch through its loaded diff, a rename as one', () => {
    const items = [makeBranch({ id: 'b-1', ahead: 2, behind_base: false })]

    expect(rowBadgeCounts(items, 'b-1', RENAME_DIFF).get('b-1')).toEqual({
      ahead: 1,
      behind: false,
    })
    // Until that diff arrives, the list's count stands in.
    expect(rowBadgeCounts(items, 'b-1', undefined).get('b-1')).toEqual({
      ahead: 2,
      behind: false,
    })
  })
})
