import { api } from './client'
import type {
  ImplementationTicket,
  MergedEventPreview,
  MergePreviewTarget,
  PlanBranchComment,
  PlanBranchConflicts,
  PlanBranchDetail,
  PlanBranchDiffSummary,
  PlanBranchList,
  PlanBranchMergeResolution,
  PlanBranchResolutionBatchCreate,
  PlanBranchResolutionBatchResponse,
  PlanBranchReviewer,
  PlanBranchSummary,
  PlanBranchTransitionAction,
  PlanDiffEntityType,
  UpdateFromMainPreview,
  UpdateFromMainRequest,
  UpdateFromMainResult,
} from '../types'
import type { components } from '../types/api.gen'

type Schemas = components['schemas']

// --- Move or copy changes to another branch ---------------------------------
// The generated `BranchTransfer*` schemas (backend/src/tripl/schemas/
// plan_branch.py). A field the backend declares with a `None` default is
// optional here even though the server always sends it.

export type BranchTransferMode = Schemas['BranchTransferRequest']['mode']

/** One diff row, addressed the way a revert addresses it (no `field`). */
export type BranchTransferEntryRef = Schemas['BranchTransferEntryRef']

/**
 * The request body. `target_branch_id` null previews against a branch cut from
 * main now; only with `dry_run`. `dry_run` defaults to false server-side, so a
 * real transfer may leave it out (the generated type marks a defaulted field
 * required).
 */
export type BranchTransferRequest = Omit<Schemas['BranchTransferRequest'], 'dry_run'> & {
  dry_run?: boolean
}

/** One listed row; `needed_by` names the selected row a carried one is needed by. */
export type BranchTransferItem = Schemas['BranchTransferItem']

export type BranchTransferConflictReason =
  | 'target_exists'
  | 'target_changed'
  | 'target_missing'
  | 'identity_clash'
  | 'ambiguous_rename'
  | 'has_discussion'

/**
 * One refused row, as the 409 `transfer_conflicts` lists it. Hand-written: an
 * HTTPException detail is not part of the OpenAPI schema, so there is no
 * generated type to alias.
 */
export interface BranchTransferConflict {
  entity_type: PlanDiffEntityType
  name: string
  parent: string | null
  field: string | null
  reason: BranchTransferConflictReason
  message: string
}

export type BranchTransferResult = Schemas['BranchTransferResult']

export const planBranchesApi = {
  /** `include_diff_counts` costs one plan snapshot per open branch plus one
   * for main, so only the Branches tab's badges ask for it — the switcher and
   * everything else read the plain list. */
  list: (slug: string, options: { include_diff_counts?: boolean } = {}) =>
    api.get<PlanBranchList>(
      `/projects/${slug}/branches${options.include_diff_counts ? '?include_diff_counts=true' : ''}`,
    ),

  get: (slug: string, branchId: string) =>
    api.get<PlanBranchDetail>(`/projects/${slug}/branches/${branchId}`),

  create: (slug: string, data: Schemas['PlanBranchCreate']) =>
    api.post<PlanBranchSummary>(`/projects/${slug}/branches`, data),

  delete: (slug: string, branchId: string) =>
    api.del(`/projects/${slug}/branches/${branchId}`),

  transition: (
    slug: string,
    branchId: string,
    action: PlanBranchTransitionAction,
  ) =>
    api.post<PlanBranchDetail>(
      `/projects/${slug}/branches/${branchId}/transition`,
      { action },
    ),

  addReviewer: (slug: string, branchId: string, userId: string) =>
    api.post<PlanBranchReviewer>(
      `/projects/${slug}/branches/${branchId}/reviewers`,
      { user_id: userId },
    ),

  removeReviewer: (slug: string, branchId: string, userId: string) =>
    api.del(`/projects/${slug}/branches/${branchId}/reviewers/${userId}`),

  listComments: (slug: string, branchId: string) =>
    api.get<PlanBranchComment[]>(
      `/projects/${slug}/branches/${branchId}/comments`,
    ),

  createComment: (
    slug: string,
    branchId: string,
    body: string,
    parentId?: string,
  ) =>
    api.post<PlanBranchComment>(
      `/projects/${slug}/branches/${branchId}/comments`,
      { body, parent_id: parentId ?? null },
    ),

  deleteComment: (slug: string, branchId: string, commentId: string) =>
    api.del(
      `/projects/${slug}/branches/${branchId}/comments/${commentId}`,
    ),

  diff: (slug: string, branchId: string) =>
    api.get<PlanBranchDiffSummary>(
      `/projects/${slug}/branches/${branchId}/diff`,
    ),

  /** One event as main will hold it after this branch merges ("As merged").
   * Read-only; answers 409 with a `{message}` detail where the merge itself
   * would refuse (an old base), on a landed branch, or for a namesake. */
  mergePreview: (slug: string, branchId: string, target: MergePreviewTarget) => {
    const query =
      'eventId' in target
        ? `event_id=${encodeURIComponent(target.eventId)}`
        : `event_type=${encodeURIComponent(target.eventType)}&event_name=${encodeURIComponent(target.eventName)}`
    return api.get<MergedEventPreview>(
      `/projects/${slug}/branches/${branchId}/merge-preview/event?${query}`,
    )
  },

  merge: (slug: string, branchId: string) =>
    api.post<PlanBranchDetail>(
      `/projects/${slug}/branches/${branchId}/merge`,
      undefined,
    ),

  /** Undo one entry of the branch's diff — the whole entity, or one field of it
   * — back to the branch's base state. Responds with the resulting diff. */
  revert: (
    slug: string,
    branchId: string,
    /** `entity_id` is the diff entry's own: two events (or relations) may
     * share a name, and then only the id says which entry is meant. */
    data: Schemas['BranchRevertRequest'],
  ) =>
    api.post<PlanBranchDiffSummary>(
      `/projects/${slug}/branches/${branchId}/revert`,
      data,
    ),

  /** Move or copy rows of this branch's diff onto another open branch.
   * `dry_run` makes every write and rolls it back. Refuses with 409
   * `{transfer_conflicts, message}`, `{transfer_base_mismatch, message,
   * behind_branch_ids}` or `{transfer_constraint_violation, message}`. */
  transfer: (slug: string, branchId: string, body: BranchTransferRequest) =>
    api.post<BranchTransferResult>(
      `/projects/${slug}/branches/${branchId}/transfer`,
      body,
    ),

  getConflicts: (slug: string, branchId: string) =>
    api.get<PlanBranchConflicts>(
      `/projects/${slug}/branches/${branchId}/conflicts`,
    ),

  /** What "Update from main" would bring in, and the overlaps to decide
   * first. Read-only. */
  getUpdatePreview: (slug: string, branchId: string) =>
    api.get<UpdateFromMainPreview>(
      `/projects/${slug}/branches/${branchId}/update-from-main`,
    ),

  /** Three-way merge of main INTO the branch. Refuses with 409
   * `{unresolved_conflicts, conflicts}` while an overlap has no choice, and
   * with `{main_moved}` when `expected_main_hash` is stale. */
  updateFromMain: (slug: string, branchId: string, data: UpdateFromMainRequest) =>
    api.post<UpdateFromMainResult>(
      `/projects/${slug}/branches/${branchId}/update-from-main`,
      data,
    ),

  saveResolution: (
    slug: string,
    branchId: string,
    data: Schemas['ResolutionCreate'],
  ) =>
    api.post<PlanBranchMergeResolution>(
      `/projects/${slug}/branches/${branchId}/resolutions`,
      data,
    ),

  /** Many choices in one call (a "for all" action); one bad item saves none. */
  saveResolutions: (slug: string, branchId: string, data: PlanBranchResolutionBatchCreate) =>
    api.post<PlanBranchResolutionBatchResponse>(
      `/projects/${slug}/branches/${branchId}/resolutions/batch`,
      data,
    ),

  /** Tracker tickets opened when this branch merged. Read-only: the backend
   * writes them from the merge worker, never from a client. */
  listImplementationTickets: (slug: string, branchId: string) =>
    api.get<ImplementationTicket[]>(
      `/projects/${slug}/branches/${branchId}/implementation-tickets`,
    ),
}
