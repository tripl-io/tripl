import { Panel } from '@/components/settings/kit'
import { LoadingState } from '@/components/primitives/loading-state'
import { UsedByList } from '@/components/dependencies/UsedByList'
import { useBranchImpact } from '@/components/dependencies/useDependencies'
import {
  dedupeEdges,
  dependencyKindLabel,
  impactChangeLabel,
  summarizeEdges,
} from '@/lib/dependencies'
import { countOf } from '@/lib/plural'
import type { ImpactItem } from '@/types'

function changedName(item: ImpactItem): string {
  const name = item.name ?? item.entity?.name
  if (name) return name
  return `${dependencyKindLabel(item.change.kind)} ${item.change.id.slice(0, 8)}`
}

/**
 * The branch a dependent's link opens on. A deprecation or an in-place change
 * leaves the entity on the branch, so its dependents are read there; a delete
 * or rename means the dependents are the ones main still has, which is where
 * they break after the merge.
 */
function linkBranchFor(item: ImpactItem, branchId: string): string | null {
  const change = item.change.change
  return change === 'change' || change === 'deprecate' ? branchId : null
}

/**
 * "Impact" (F04, #257): what the branch's deletes, deprecations, renames and
 * in-place changes
 * touch downstream — the metrics, alert rules, relations and variables a
 * reviewer would otherwise find out about after the merge. Computed on the
 * server from the branch diff, with the same rename pairing the diff shows.
 *
 * A review aid: it never blocks approve or merge. Shown to every member,
 * viewers included.
 */
export function BranchImpactPanel({ slug, branchId }: { slug: string; branchId: string }) {
  const query = useBranchImpact(slug, branchId)

  if (query.isPending) {
    return (
      <Panel title="Impact" bodyClassName="px-4 py-3">
        <LoadingState label="Working out what this branch's changes touch…" />
      </Panel>
    )
  }
  if (query.isError) {
    return (
      <Panel title="Impact" bodyClassName="px-4 py-3">
        <p className="m-0 text-body-sm text-fg-tertiary" role="status">
          Could not work out what this branch&apos;s changes touch.{' '}
          <button
            type="button"
            className="text-accent underline-offset-2 hover:underline"
            onClick={() => void query.refetch()}
          >
            Retry
          </button>
        </p>
      </Panel>
    )
  }

  const touching = query.data.items.filter((item) => item.affected.length > 0)
  const allAffected = dedupeEdges(touching.flatMap((item) => item.affected))
  const subtitle =
    touching.length === 0
      ? 'No change on this branch affects anything downstream.'
      : `${countOf(touching.length, 'change touches', 'changes touch')} ${summarizeEdges(allAffected)}.`

  return (
    <Panel
      title="Impact"
      subtitle={subtitle}
      subtitleTone={touching.length > 0 ? 'warning' : undefined}
      bodyClassName="px-4 py-3"
    >
      {touching.length === 0 ? (
        <p className="m-0 text-body-sm text-fg-tertiary">
          Deleted, deprecated, renamed and changed (edited in place, such as a field&apos;s type)
          events, event types, fields and variables are checked against the metrics, alert rules,
          relations and variables that use them.
        </p>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-4 p-0" aria-label="Changes with downstream impact">
          {touching.map((item) => (
            <li key={`${item.change.kind}:${item.change.id}:${item.change.change}`}>
              <p className="m-0 mb-2 text-body-sm">
                <span className="mono font-medium text-fg">{changedName(item)}</span>{' '}
                <span className="text-fg-muted">
                  {dependencyKindLabel(item.change.kind)} {impactChangeLabel(item.change.change)}:
                </span>{' '}
                <span className="text-fg">{item.summary || summarizeEdges(item.affected)}</span>
              </p>
              <div className="pl-3">
                <UsedByList
                  slug={slug}
                  edges={item.affected}
                  headingLevel={4}
                  linkBranchId={linkBranchFor(item, branchId)}
                />
              </div>
            </li>
          ))}
        </ul>
      )}
      <p className="m-0 mt-3 text-caption text-fg-tertiary">
        A review aid: dependents are not changed by the merge, and nothing here blocks it.
      </p>
    </Panel>
  )
}
