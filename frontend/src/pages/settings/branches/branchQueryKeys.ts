/**
 * Which of the Branches tab's caches each branch action has to refresh.
 *
 * Every key used to be a string literal retyped at each reader and each
 * invalidation, which is how a merge came to refresh the branch list but not
 * the conflicts panel, nor any of main's plan caches. The key
 * builders themselves live in lib/queryKeys.ts with every other key.
 */

import type { QueryClient } from '@tanstack/react-query'
import {
  branchDependenciesKey,
  branchEventHistoryKey,
  branchEventKey,
  branchEventsKey,
  branchImpactKey,
  eventTagsKey,
  eventTypesKey,
  metaFieldsKey,
  planBranchConflictsKey,
  planBranchCountsKey,
  planBranchDetailKey,
  planBranchDiffKey,
  planBranchMergePreviewRootKey,
  planBranchesKey,
  planBranchTicketsKey,
  planBranchUpdatePreviewKey,
  projectDependenciesKey,
  projectEventHistoryKey,
  projectEventKey,
  projectEventsKey,
  projectEventTagsKey,
  projectEventTypesKey,
  projectMetaFieldsKey,
  projectPlanRevisionsKey,
  projectRelationsKey,
  projectVariablesKey,
  relationsKey,
  variablesKey,
} from '@/lib/queryKeys'

/** After a create, a revert, a merge, a delete or a reopen: the only actions
 * that change a branch's content or bring a branch back into the counted set. */
export function invalidateBranchCounts(qc: QueryClient, slug: string) {
  void qc.invalidateQueries({ queryKey: planBranchCountsKey(slug) })
}

/**
 * After a tracked merge the worker writes the ticket a moment after the merge
 * response (`create_implementation_ticket.delay`), so the panel polls while the
 * list is still empty — for a bounded window, because a tracker that failed
 * must not be polled forever.
 */
export const TICKET_POLL_MS = 2000
export const TICKET_POLL_WINDOW_MS = 30_000

/** Everything the review screen shows about one branch. */
export function invalidateBranchReview(qc: QueryClient, slug: string, branchId: string) {
  void qc.invalidateQueries({ queryKey: planBranchesKey(slug) })
  void qc.invalidateQueries({ queryKey: planBranchDiffKey(slug, branchId) })
  void qc.invalidateQueries({ queryKey: planBranchDetailKey(slug, branchId) })
  // Main may have gained a conflicting edit since the panel loaded; a merge
  // refused for "field conflicts below" must find them below.
  void qc.invalidateQueries({ queryKey: planBranchConflictsKey(slug, branchId) })
  void qc.invalidateQueries({ queryKey: planBranchTicketsKey(slug, branchId) })
  // The Impact panel is computed from the diff (#257).
  void qc.invalidateQueries({ queryKey: branchImpactKey(slug, branchId) })
  // An open "As merged" sheet: a revert or an update from main changes it.
  void qc.invalidateQueries({ queryKey: planBranchMergePreviewRootKey(slug, branchId) })
}

/**
 * Every plan cache main feeds, across all branches: after a merge the events,
 * variables, event types, meta fields, relations and history of main are all
 * different, and a reviewer who clicks through to Events must not see main as
 * it was before the merge for the minute of `staleTime`.
 */
export function invalidateMainPlan(qc: QueryClient, slug: string) {
  for (const queryKey of [
    projectEventTypesKey(slug),
    projectVariablesKey(slug),
    projectEventsKey(slug),
    // The single-event reader and editor, its tags and its history: an editor
    // opened on pre-merge data saves it straight back over the merge.
    projectEventKey(slug),
    projectEventTagsKey(slug),
    projectEventHistoryKey(slug),
    projectMetaFieldsKey(slug),
    projectRelationsKey(slug),
    projectPlanRevisionsKey(slug),
    // "Used by" lists and impact read the plan (#257).
    projectDependenciesKey(slug),
  ]) {
    void qc.invalidateQueries({ queryKey })
  }
}

/** The branch's own plan caches: a revert rewrites the branch, so its editors
 * must not keep showing the reverted state. */
export function invalidateBranchPlan(qc: QueryClient, slug: string, branchId: string) {
  for (const queryKey of [
    eventTypesKey(slug, branchId),
    variablesKey(slug, branchId),
    branchEventsKey(slug, branchId),
    // EventForm edits from ['event', slug, branchId, eventId]; a stale copy
    // there would PUT the reverted values back (no version check).
    branchEventKey(slug, branchId),
    eventTagsKey(slug, branchId),
    branchEventHistoryKey(slug, branchId),
    metaFieldsKey(slug, branchId),
    relationsKey(slug, branchId),
    branchDependenciesKey(slug, branchId),
  ]) {
    void qc.invalidateQueries({ queryKey })
  }
}

/**
 * After "Update from main": the branch has a new base and main's changes on
 * it, so its diff, detail, conflicts, row counts, the preview and its own plan
 * caches are all stale. Main itself is untouched.
 */
export function invalidateBranchUpdated(qc: QueryClient, slug: string, branchId: string) {
  invalidateBranchReview(qc, slug, branchId)
  invalidateBranchCounts(qc, slug)
  invalidateBranchPlan(qc, slug, branchId)
  void qc.invalidateQueries({ queryKey: planBranchUpdatePreviewKey(slug, branchId) })
}
