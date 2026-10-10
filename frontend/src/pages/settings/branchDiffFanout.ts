import type { PlanBranchDiffSummary, PlanBranchSummary } from '@/types'
import { pairedDiffCounts } from './branches/branchDiffModel'
import { isLandedBranch } from './branches/branchMeta'

/**
 * Where the Branches tab's ahead/behind badges come from.
 *
 * They used to cost one `/branches/{id}/diff` per feature branch — a
 * server-side plan comparison measured at 2-3.5 s on a real project — capped at
 * eight rows. `GET /branches?include_diff_counts=true` returns
 * `ahead` / `behind_base` for every open branch off one shared main snapshot, so
 * the list now reads those and fires no diff at all. Merged and closed
 * branches come back without counts, and get no badge: a landed branch is not
 * ahead of anything.
 *
 * Lives outside the component files so they stay component-only
 * (react-refresh) and the policy is unit-testable on its own.
 */
export const DIFF_STALE_MS = 5 * 60 * 1000

export interface RowCounts {
  ahead: number
  behind: boolean
}

/**
 * The badge counts per branch id.
 *
 * The backend's `ahead` is already the reviewable total with each rename
 * counted once (plan_branch_service pairs them before counting), so every row
 * can show the list's number. The selected branch's diff is on screen anyway, so
 * its row counts through the same paired view as the strip and the Changes
 * panel: the numbers a reviewer can compare are computed one way.
 */
export function rowBadgeCounts(
  items: PlanBranchSummary[],
  selectedId: string | null,
  selectedDiff: PlanBranchDiffSummary | undefined,
): Map<string, RowCounts> {
  const counts = new Map<string, RowCounts>()
  for (const branch of items) {
    if (branch.kind === 'main' || isLandedBranch(branch)) continue
    if (branch.id === selectedId && selectedDiff) {
      counts.set(branch.id, {
        ahead: pairedDiffCounts(selectedDiff).total,
        behind: selectedDiff.behind_base,
      })
    } else if (typeof branch.ahead === 'number') {
      counts.set(branch.id, { ahead: branch.ahead, behind: branch.behind_base === true })
    }
  }
  return counts
}
