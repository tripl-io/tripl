import { Panel } from '@/components/settings/kit'
import { LoadingState } from '@/components/primitives/loading-state'
import { DEPENDENCY_KIND_LABELS, dedupeEdges, summarizeEdges } from '@/lib/dependencies'
import type { DependencyEntityKind } from '@/types'
import { UsedByList } from './UsedByList'
import { useEntityDependencies } from './useDependencies'

type Entity = { kind: DependencyEntityKind; id: string }

/**
 * The body of a "Used by" section, without a card: what depends on `entity`
 * downstream, grouped by kind. Used bare inside an expanded field row, and
 * inside {@link UsedBySection}'s panel everywhere else.
 */
export function UsedByBody({
  slug,
  entity,
  branchId,
  depth = 1,
  headingLevel = 3,
}: {
  slug: string
  entity: Entity
  /** Defaults to the branch on screen. */
  branchId?: string | null
  depth?: 1 | 2
  headingLevel?: 3 | 4
}) {
  const query = useEntityDependencies(slug, entity, { depth, branchId })
  const noun = DEPENDENCY_KIND_LABELS[entity.kind].one

  if (query.isPending) return <LoadingState label="Checking what uses this…" />
  if (query.isError) {
    return (
      <p className="m-0 text-body-sm text-fg-tertiary" role="status">
        Could not load what uses this {noun}.{' '}
        <button
          type="button"
          className="text-accent underline-offset-2 hover:underline"
          onClick={() => void query.refetch()}
        >
          Retry
        </button>
      </p>
    )
  }
  const edges = dedupeEdges(query.data.downstream)
  if (edges.length === 0) {
    return (
      <p className="m-0 text-body-sm text-fg-tertiary">
        Nothing in this project depends on this {noun}.
      </p>
    )
  }
  const hasPossible = edges.some((edge) => edge.certainty === 'possible')
  return (
    <div className="flex flex-col gap-3">
      <p className="m-0 text-body-sm text-fg-muted">Used by {summarizeEdges(edges)}.</p>
      <UsedByList slug={slug} edges={edges} headingLevel={headingLevel} />
      {hasPossible && (
        <p className="m-0 text-caption text-fg-tertiary">
          Items marked possible are matched by name, not by a stored reference; check the query,
          binding or scan column before relying on them.
        </p>
      )}
    </div>
  )
}

/**
 * "Used by" (F04, #257): on the event, event type, variable, metric and
 * fact-table pages. Read-only and shown to every member, viewers included;
 * it never blocks anything — only fact tables block, on the server.
 */
export function UsedBySection({
  slug,
  entity,
  branchId,
  depth = 1,
  className,
}: {
  slug: string
  entity: Entity
  branchId?: string | null
  depth?: 1 | 2
  className?: string
}) {
  return (
    <Panel
      className={className}
      title="Used by"
      subtitle="What would be affected if this changed or were deleted"
      bodyClassName="px-4 py-3"
    >
      <UsedByBody slug={slug} entity={entity} branchId={branchId} depth={depth} />
    </Panel>
  )
}
