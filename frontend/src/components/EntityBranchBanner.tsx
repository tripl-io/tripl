import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, GitCompare } from 'lucide-react'
import { Link } from 'react-router-dom'
import { planBranchesApi } from '@/api/planBranches'
import { Chip } from '@/components/primitives/chip'
import { Button } from '@/components/ui/button'
import { useBranchContext } from '@/hooks/useBranch'
import { planBranchesKey } from '@/lib/queryKeys'
import { STATUS_LABEL, STATUS_TONE } from '@/lib/branchStatus'

interface EntityBranchBannerProps {
  slug: string
  /** The branch the row actually lives on, as the server reported it. */
  rowBranchId: string | null | undefined
  /** The page's own path without any `?branch=`; the banner adds it. */
  path: string
  /** What the row is, for the sentence: "This event lives on…". */
  noun?: string
  /**
   * Where "View main plan" goes from a row that lives on a branch. Not `path`:
   * a branch row's id names the branch row, and reads are lenient, so main
   * rendered that same branch row again under a "you are viewing main" warning
   * and its Save 404'd. A page passes the row's main twin
   * (`main_event_id`) when the server names one, else somewhere on main that
   * exists (its list), or no link at all.
   */
  mainPath?: string
}

/**
 * Says which branch an entity page is showing, and when the row is not on the
 * branch the reader selected, offers the switch.
 *
 * A link pasted from a branch used to open a 404 ("Try again") in a fresh
 * session, and a main event opened while a branch was active did the same the
 * other way round; no page said which plan it was showing beyond the switcher
 * in the rail. The read endpoints now answer for any branch of
 * the project and report the row's `branch_id`, so this is the one place that
 * turns that into a sentence.
 */
export function EntityBranchBanner({
  slug,
  rowBranchId,
  path,
  noun = 'event',
  mainPath,
}: EntityBranchBannerProps) {
  const { branchId: activeBranchId, setBranchId } = useBranchContext()
  const { data } = useQuery({
    queryKey: planBranchesKey(slug),
    queryFn: () => planBranchesApi.list(slug),
    enabled: !!rowBranchId,
    staleTime: 60_000,
  })
  if (!rowBranchId || !data) return null
  const byId = new Map(data.items.map(b => [b.id, b]))
  const main = data.items.find(b => b.kind === 'main')
  const rowBranch = byId.get(rowBranchId)
  if (!rowBranch || !main) return null
  const rowIsMain = rowBranch.kind === 'main'
  const activeId = activeBranchId ?? main.id
  const mismatch = rowBranchId !== activeId

  if (!mismatch) {
    if (rowIsMain) return null
    // The info tone the shell's branch strip uses, with the status in the same
    // words every branch surface uses ("Ready for review"), not the raw enum.
    // Pages render it above their title.
    return (
      <div
        className="mb-4 flex flex-wrap items-center gap-x-2 gap-y-1 rounded-card border border-info/40 bg-info-soft px-3 py-2 text-body-sm text-fg-secondary"
        data-testid="entity-branch-banner"
      >
        <GitCompare className="size-3.5 shrink-0 text-info" aria-hidden="true" />
        <span>
          Branch copy on <span className="mono font-medium text-fg">{rowBranch.name}</span>
        </span>
        <Chip tone={STATUS_TONE[rowBranch.status]} size="xs">
          {STATUS_LABEL[rowBranch.status]}
        </Chip>
        {mainPath && (
          <Button asChild variant="link" size="sm" className="h-auto px-0">
            <Link to={mainPath} onClick={() => setBranchId(null, { updateUrl: false })}>
              View main plan
            </Link>
          </Button>
        )}
      </div>
    )
  }

  const target = rowIsMain ? path : `${path}${path.includes('?') ? '&' : '?'}branch=${encodeURIComponent(rowBranchId)}`
  const activeName = activeBranchId ? (byId.get(activeBranchId)?.name ?? 'another branch') : 'main'
  // The page below this banner reads a row the active branch does not have:
  // its edit form cannot save there. This is the first thing on
  // the page and an alert, and the way out is a real button, not an inline link.
  return (
    <div
      role="alert"
      className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-card border border-warning/50 bg-warning-soft px-3 py-2 text-body-sm text-warning"
      data-testid="entity-branch-banner"
    >
      <AlertTriangle className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="min-w-0 flex-1">
        This {noun} lives on{' '}
        <span className="font-medium">{rowIsMain ? 'the main plan' : `branch ${rowBranch.name}`}</span>
        {rowIsMain ? '' : ` (${STATUS_LABEL[rowBranch.status]})`}; you are viewing{' '}
        <span className="font-medium">{activeName}</span>.
      </span>
      <Button asChild size="sm">
        <Link
          to={target}
          onClick={() => setBranchId(rowIsMain ? null : rowBranchId, { updateUrl: false })}
        >
          {rowIsMain ? 'Switch to main' : `Switch to ${rowBranch.name}`}
        </Link>
      </Button>
    </div>
  )
}
