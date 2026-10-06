import { api } from './client'
import type {
  EntityChangeCount,
  ImplementationTicket,
  MergedEventPreview,
  MergePreviewTarget,
  PlanBranchComment,
  PlanBranchConflicts,
  PlanBranchDetail,
  PlanBranchDiffSummary,
  PlanBranchMergeResolution,
  PlanBranchResolutionBatchCreate,
  PlanBranchResolutionBatchResponse,
  PlanBranchReviewer,
  PlanBranchSummary,
  PlanBranchTransitionAction,
  PlanDiffEntityType,
  PlanDiffKind,
  ResolutionChoice,
  UpdateFromMainPreview,
  UpdateFromMainRequest,
  UpdateFromMainResult,
} from '../types'

// --- Move or copy changes to another branch ---------------------------------
// Hand-written mirrors of the Pydantic schemas in backend/src/tripl/schemas/
// plan_branch.py (`BranchTransfer*`); reconciled with api.gen.ts on regeneration.

export type BranchTransferMode = 'move' | 'copy'

/** One diff row, addressed the way a revert addresses it (no `field`). */
export interface BranchTransferEntryRef {
  entity_type: PlanDiffEntityType
  name: string
  parent?: string | null
  entity_id?: string | null
}

export interface BranchTransferRequest {
  /** Null previews against a branch cut from main now; only with `dry_run`. */
  target_branch_id: string | null
  mode: BranchTransferMode
  entries: BranchTransferEntryRef[]
  dry_run?: boolean
}

export interface BranchTransferItem {
  entity_type: PlanDiffEntityType
  name: string
  parent: string | null
  entity_id: string | null
  kind: PlanDiffKind
  /** The selected row a carried one is needed by. */
  needed_by: string | null
}

export type BranchTransferConflictReason =
  | 'target_exists'
  | 'target_changed'
  | 'target_missing'
  | 'identity_clash'
  | 'ambiguous_rename'
  | 'has_discussion'

/** One refused row, as the 409 `transfer_conflicts` lists it. */
export interface BranchTransferConflict {
  entity_type: PlanDiffEntityType
  name: string
  parent: string | null
  field: string | null
  reason: BranchTransferConflictReason
  message: string
}

export interface BranchTransferResult {
  mode: BranchTransferMode
  dry_run: boolean
  target_branch_id: string | null
  target_branch_name: string | null
  applied: BranchTransferItem[]
  carried: BranchTransferItem[]
  skipped: BranchTransferItem[]
  warnings: string[]
  target_counts: EntityChangeCount[]
  source_diff: PlanBranchDiffSummary | null
}

/**
 * A branch row as `GET /branches` returns it. `ahead` / `behind_base` are filled
 * only when the list is asked for them (`include_diff_counts`), and then only
 * for open feature branches; merged, closed and main rows keep them null, as
 * does every row of a plain list. `ahead` is the backend's raw count of
 * reviewable entries — a rename still counts as its removal plus its addition.
 */
export interface PlanBranchListItem extends PlanBranchSummary {
  ahead?: number | null
  behind_base?: boolean | null
}

export interface PlanBranchListResponse {
  items: PlanBranchListItem[]
  total: number
}

export const planBranchesApi = {
  /** `include_diff_counts` costs one plan snapshot per open branch plus one
   * for main, so only the Branches tab's badges ask for it — the switcher and
   * everything else read the plain list. */
  list: (slug: string, options: { include_diff_counts?: boolean } = {}) =>
    api.get<PlanBranchListResponse>(
      `/projects/${slug}/branches${options.include_diff_counts ? '?include_diff_counts=true' : ''}`,
    ),

  get: (slug: string, branchId: string) =>
    api.get<PlanBranchDetail>(`/projects/${slug}/branches/${branchId}`),

  create: (slug: string, data: { name: string; description?: string }) =>
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
    data: {
      entity_type: PlanDiffEntityType
      name: string
      parent?: string | null
      field?: string | null
      /** The diff entry's own `entity_id`: two events (or relations) may share a
       * name, and then only the id says which entry is meant. */
      entity_id?: string | null
    },
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
    data: {
      entity_type: string
      entity_name: string
      field_name: string
      choice: ResolutionChoice
    },
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

  deleteResolution: (slug: string, branchId: string, resolutionId: string) =>
    api.del(
      `/projects/${slug}/branches/${branchId}/resolutions/${resolutionId}`,
    ),

  /** Tracker tickets opened when this branch merged. Read-only: the backend
   * writes them from the merge worker, never from a client. */
  listImplementationTickets: (slug: string, branchId: string) =>
    api.get<ImplementationTicket[]>(
      `/projects/${slug}/branches/${branchId}/implementation-tickets`,
    ),
}
