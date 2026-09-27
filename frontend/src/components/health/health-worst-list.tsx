import { Link } from 'react-router-dom'

import { HealthBadge } from '@/components/health/health-badge'
import { useBranchLinkProps } from '@/hooks/useBranch'
import { getMonitoringPath } from '@/lib/monitoring'
import type { EventHealthBrief } from '@/types/health'

/**
 * The least healthy events (F15, #268), each linking to its page with the
 * issue that cost it the most. The scores are the MAIN plan's, so every link
 * opens main whatever branch is selected.
 */
export function HealthWorstList({
  slug,
  items,
  emptyText = 'No scored events yet.',
}: {
  slug: string
  items: readonly EventHealthBrief[]
  emptyText?: string
}) {
  const branchLink = useBranchLinkProps()
  if (items.length === 0) {
    return <p className="text-body-sm text-fg-tertiary">{emptyText}</p>
  }
  return (
    <ol className="flex flex-col divide-y divide-border-subtle" aria-label="Least healthy events">
      {items.map((item) => {
        const link = branchLink(
          getMonitoringPath(slug, { scope_type: 'event', scope_ref: item.event_id }),
          null,
        )
        return (
          <li key={item.event_id} className="flex items-center gap-2.5 py-1.5">
            <HealthBadge score={item.score} grade={item.grade} />
            <div className="min-w-0 flex-1">
              <Link
                to={link.to}
                onClick={link.onClick}
                className="block truncate text-body-sm text-fg hover:underline underline-offset-4"
                title={item.name}
              >
                {item.name}
              </Link>
              {item.top_issue && (
                <p className="truncate text-caption text-fg-tertiary" title={item.top_issue}>
                  {item.top_issue}
                </p>
              )}
            </div>
          </li>
        )
      })}
    </ol>
  )
}
