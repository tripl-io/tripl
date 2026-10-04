import { api } from './client'
import type {
  ImplementationTicket,
  PlanBranchComment,
  PlanBranchConflicts,
  PlanBranchDetail,
  PlanBranchDiffSummary,
  PlanBranchMergeResolution,
  PlanBranchReviewer,
  PlanBranchSummary,
  PlanBranchTransitionAction,
  PlanDiffEntityType,
  ResolutionChoice,
  UpdateFromMainPreview,
  UpdateFromMainRequest,
  UpdateFromMainResult,
} from '../types'

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
