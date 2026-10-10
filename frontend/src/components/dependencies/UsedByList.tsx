import { Link } from 'react-router-dom'
import { Chip } from '@/components/primitives/chip'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { useActiveBranchId, useBranchLinkProps } from '@/hooks/useBranch'
import {
  DEPENDENCY_KIND_LABELS,
  POSSIBLE_EDGE_EXPLANATION,
  dependencyHref,
  dependencyKindHeading,
  groupEdgesByKind,
} from '@/lib/dependencies'
import type { DependencyEdge } from '@/types'

/**
 * The "possible" marker: a SQL identifier match, not a stored reference. The
 * explanation is in the tooltip AND in the accessible name, so a keyboard or
 * screen-reader user gets it without hovering. Carries its own provider, as
 * InfoTip (components/info-tip.tsx) does, so it renders without the app's root
 * one.
 */
export function PossibleBadge() {
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="inline-flex shrink-0 rounded-full outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
            aria-label={POSSIBLE_EDGE_EXPLANATION}
          >
            <Chip tone="warning" variant="outline" size="xs">
              possible
            </Chip>
          </button>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs whitespace-normal">{POSSIBLE_EDGE_EXPLANATION}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

function EdgeName({
  slug,
  edge,
  linked,
  linkBranchId,
}: {
  slug: string
  edge: DependencyEdge
  linked: boolean
  /** Overrides the branch on screen; `null` links to main. */
  linkBranchId?: string | null
}) {
  const activeBranchId = useActiveBranchId()
  const branchId = linkBranchId === undefined ? activeBranchId : linkBranchId
  const linkProps = useBranchLinkProps()
  const href = linked ? dependencyHref(slug, edge) : null
  if (!href) return <span className="font-medium text-fg">{edge.name}</span>
  // Keep the reader on the branch they are on; a hint that already names a
  // branch is taken as written.
  const props = href.includes('branch=') ? { to: href } : linkProps(href, branchId)
  return (
    <Link {...props} className="font-medium text-accent underline-offset-2 hover:underline">
      {edge.name}
    </Link>
  )
}

/**
 * The one sentence every row of a group shares, or null. Only direct edges
 * fold: a possible edge's sentence says where the name matched, so it stays on
 * its row. One row keeps its own sentence; there is nothing to fold.
 */
function sharedRelation(edges: readonly DependencyEdge[]): string | null {
  if (edges.length < 2) return null
  const first = edges[0]!.relation
  if (!first) return null
  return edges.every((e) => e.certainty === 'direct' && e.relation === first) ? first : null
}

/**
 * Dependents grouped by kind, each with the sentence that says why it depends
 * ("metric uses event in its composition") and a "possible" badge on a SQL
 * match. When every row of a group gives the same reason, it is said once under
 * the heading instead of on each row. `linked={false}` for places a navigation
 * would strand an open dialog.
 */
export function UsedByList({
  slug,
  edges,
  linked = true,
  headingLevel = 3,
  compact = false,
  linkBranchId,
}: {
  slug: string
  edges: readonly DependencyEdge[]
  linked?: boolean
  headingLevel?: 3 | 4
  /** Tighter rows and no relation sentence — for a confirm dialog. A
   * possible edge keeps its sentence: it is the only thing that says where the
   * name matched. */
  compact?: boolean
  /** The branch a linked name opens on. Omitted: the branch on screen. `null`:
   * main. */
  linkBranchId?: string | null
}) {
  const groups = groupEdgesByKind(edges)
  const Heading = headingLevel === 4 ? 'h4' : 'h3'
  return (
    <div className="flex flex-col gap-3" data-testid="used-by-list">
      {groups.map(({ kind, edges: list }) => {
        const shared = compact ? null : sharedRelation(list)
        return (
          <div key={kind}>
            <Heading className="m-0 mb-1 text-caption font-semibold uppercase tracking-wide text-fg-tertiary">
              {dependencyKindHeading(kind)}{' '}
              <span className="font-normal tabular-nums">({list.length})</span>
            </Heading>
            {shared && (
              <p className="m-0 mb-1 text-caption text-fg-tertiary" data-testid="used-by-shared-relation">
                {shared}
              </p>
            )}
            <ul
              className="m-0 flex list-none flex-col gap-1 p-0"
              aria-label={DEPENDENCY_KIND_LABELS[kind].many}
            >
              {list.map((edge) => (
                <li
                  key={`${edge.kind}:${edge.id}`}
                  className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5 text-body-sm"
                >
                  <EdgeName slug={slug} edge={edge} linked={linked} linkBranchId={linkBranchId} />
                  {edge.certainty === 'possible' && <PossibleBadge />}
                  {!shared && (!compact || edge.certainty === 'possible') && edge.relation && (
                    <span className="text-caption text-fg-tertiary">{edge.relation}</span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )
      })}
    </div>
  )
}
