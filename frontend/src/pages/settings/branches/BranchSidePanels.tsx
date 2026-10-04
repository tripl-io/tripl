import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { planBranchesApi } from '@/api/planBranches'
import { trackerConfigApi } from '@/api/trackerConfig'
import { CommentThread } from '@/components/comment-thread'
import { ImplementationTicketRow } from '@/components/implementation-ticket-row'
import { Panel } from '@/components/settings/kit'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { useDemoScenarioActions } from '@/demo/demoScenarioContext'
import { displayUser } from '@/hooks/useUsersById'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type { PlanBranchSummary } from '@/types'
import { planBranchCommentsKey, planBranchTicketsKey, trackerConfigKey } from '@/lib/queryKeys'
import { TICKET_POLL_MS, TICKET_POLL_WINDOW_MS } from './branchQueryKeys'

/**
 * The tracker ticket a merge opened for this branch.
 *
 * The mapping had been persisted for a while but was unreachable from the
 * UI, so a merge that opened a Jira issue left no way back to it. The panel is
 * hidden — not empty — when there is no ticket: only a merge with the project's
 * implementation tracker enabled creates one, so "no ticket" is the normal
 * state of every other branch. The request is skipped before the merge for the
 * same reason: the list is provably empty until then.
 *
 * `mergedAt` is set when the merge happened in this session. The worker writes
 * the ticket after the merge response, so the first fetch usually finds
 * nothing; with the tracker enabled the panel then says the ticket is on its
 * way and polls for it for a bounded window rather than caching the empty
 * answer.
 */
export function ImplementationTicketsPanel({
  slug,
  branch,
  mergedAt,
}: {
  slug: string
  branch: PlanBranchSummary
  mergedAt: number | null
}) {
  // The merge whose polling window has run out; derived rather than reset in
  // the effect, so a second merge in the same session reopens the window.
  const [expiredMerge, setExpiredMerge] = useState<number | null>(null)
  useEffect(() => {
    if (mergedAt === null) return
    const timer = window.setTimeout(
      () => setExpiredMerge(mergedAt),
      Math.max(0, mergedAt + TICKET_POLL_WINDOW_MS - Date.now()),
    )
    return () => window.clearTimeout(timer)
  }, [mergedAt])
  const windowOpen = mergedAt !== null && expiredMerge !== mergedAt

  const { data: tracker } = useQuery({
    queryKey: trackerConfigKey(slug),
    queryFn: () => trackerConfigApi.get(slug),
    enabled: mergedAt !== null,
    // Only decides whether to wait for a ticket; a failure just means "don't".
    meta: SILENT_ERROR_META,
  })
  const awaiting = windowOpen && tracker?.enabled === true

  const { data: tickets } = useQuery({
    queryKey: planBranchTicketsKey(slug, branch.id),
    queryFn: () => planBranchesApi.listImplementationTickets(slug, branch.id),
    enabled: branch.status === 'merged' || mergedAt !== null,
    refetchInterval: (query) =>
      awaiting && (query.state.data?.length ?? 0) === 0 ? TICKET_POLL_MS : false,
  })

  if (!tickets || tickets.length === 0) {
    if (!awaiting) return null
    return (
      <Panel title="Implementation ticket" subtitle="opening in the tracker">
        <p
          role="status"
          className="px-4 py-3 text-body-sm text-fg-tertiary"
        >
          Creating the tracker ticket for this merge…
        </p>
      </Panel>
    )
  }

  return (
    <Panel
      title={tickets.length === 1 ? 'Implementation ticket' : 'Implementation tickets'}
      subtitle="opened in the tracker when this branch merged"
    >
      {tickets.map((ticket) => (
        <ImplementationTicketRow key={ticket.id} ticket={ticket} />
      ))}
    </Panel>
  )
}

export function CommentsPanel({
  slug,
  branchId,
  usersById,
}: {
  slug: string
  branchId: string
  usersById: Map<string, string>
}) {
  const { notifyStepCompleted } = useDemoScenarioActions()

  // The panel used to render a flat list off a single-line <Input>, while
  // PlanBranchComment has carried `parent_id` and the service has validated it
  // all along — a review remark could be made but never answered in place. The
  // shared thread already does the threading; what it did NOT have was the
  // author, which this panel always showed, so that moved into the component
  // for both callers rather than being lost here.
  //
  // A plain card, as on the event page's Discussion, not a Panel: the Panel's
  // "Comments / 1" header sat right above the thread's own "Comments (1)"
  // heading, two titles with the inner one larger.
  return (
    <section aria-label="Comments" className="rounded-card border border-border bg-surface">
      {/* A DOM element under the mark: the coach anchors on the ref it clones
          onto its child, and CommentThread takes none, so wrapping the thread
          directly left the step "not visible" on the very page it lives on. */}
      <ScenarioCoachMark step="branches/comment">
        <div>
          <CommentThread
            queryKey={planBranchCommentsKey(slug, branchId)}
            list={() => planBranchesApi.listComments(slug, branchId)}
            create={(body, parentId) =>
              planBranchesApi.createComment(slug, branchId, body, parentId ?? undefined)
            }
            remove={(commentId) => planBranchesApi.deleteComment(slug, branchId, commentId)}
            authorName={(comment) => displayUser(usersById, comment.user_id)}
            // Posting review feedback lands the branches chapter's last step.
            onCreated={() => notifyStepCompleted('branches/comment')}
            heading="Comments"
            emptyText="No comments yet."
            composerId="branch-comment-body"
            // @ offers the project's members; a mention notifies them (#259).
            mentionSlug={slug}
            className="flex flex-col gap-2 p-4"
          />
        </div>
      </ScenarioCoachMark>
    </section>
  )
}
