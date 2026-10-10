import type { components } from './api.gen'

// Plan branch, revision, diff and audit payloads, taken from the generated
// OpenAPI schema (`api.gen.ts`) rather than restated, so a backend change is a
// compile error where it is read. A field the backend declares with a `None`
// default is optional here even though the server always sends it: read it
// with `== null` or `??`.
type Schemas = components['schemas']

/**
 * A plan revision. `kind` says what produced it: a user snapshot, a branch's
 * merge base, or a merge; `branch_id` is the branch behind a `branch_base` or
 * `merge`, null once that branch is deleted. `entity_counts` is keyed by
 * entity collection (`event_types`, `fields`, `events`, `variables`,
 * `meta_fields`, `relations`).
 */
export type PlanRevisionSummary = Schemas['PlanRevisionSummary']
export type PlanRevisionKind = Schemas['PlanRevisionKind']
export type PlanRevisionDetail = Schemas['PlanRevisionDetail']
export type PlanRevisionList = Schemas['PlanRevisionList']

export type PlanBranchKind = Schemas['BranchKind']
export type PlanBranchStatus = Schemas['BranchStatus']
export type PlanBranchTransitionAction = Schemas['BranchTransitionRequest']['action']

/**
 * A branch row. `ahead` / `behind_base` are filled only when the list is asked
 * for them (`include_diff_counts`), and then only for open feature branches;
 * merged, closed and main rows keep them null, as does every row of a plain
 * list. `ahead` is the backend's raw count of reviewable entries — a rename
 * still counts as its removal plus its addition.
 */
export type PlanBranchSummary = Schemas['PlanBranchResponse']
export type PlanBranchList = Schemas['PlanBranchList']

export type PlanBranchReviewer = Schemas['BranchReviewerResponse']

/**
 * One approval. `stale` means the branch changed after it, so the merge gate
 * does not count it: counting rows without checking this shows a quota the
 * merge endpoint rejects (see the approvals chip in BranchesTab).
 */
export type PlanBranchApproval = Schemas['BranchApprovalResponse']

export type PlanBranchDetail = Schemas['PlanBranchDetailResponse']

export type ResolutionChoice = Schemas['MergeResolutionChoice']

/**
 * One conflicting field. `field` is a field of the entity, or `@presence`
 * when one side deleted the entity (or its parent) while the other edited or
 * added it; `base`/`ours`/`theirs` are then `"present"` or `"absent"`.
 * `dependents`, on a presence row whose "take main" deletes an event type, is
 * how many fields, events and relations this branch added or edited under it
 * go with it; 0 everywhere else.
 */
export type PlanBranchConflictField = Schemas['ConflictField']

/**
 * One conflicting entity. `parent` is the owning entity's name (the event
 * type of a field definition, for one); `label` a display name for the row
 * ("checkout.amount"); `added_on_both` that both sides added it since the
 * branch opened — typically an event authored here whose twin a scan made on
 * main.
 */
export type PlanBranchConflictEntity = Schemas['ConflictEntity']

/** The fields `behindNote` reads as absent from an instance that predates them. */
type ConflictsOverlapField = 'behind' | 'overlap_count' | 'merge_blocked' | 'updatable'

/**
 * A branch's conflicts with main (`GET /branches/{id}/conflicts`): the
 * generated `BranchConflictsResponse`, with the overlap fields optional because
 * `behindNote` deliberately reads an answer without them as "cannot say" (and
 * never as "safe to merge"). `behind` is the list's `behind_base` test;
 * `overlap_count` the distinct entities changed both here and on main;
 * `merge_blocked` that the merge would refuse as things stand (Update from
 * main clears it); `updatable` false for a branch whose base predates
 * complete merge baselines.
 */
export type PlanBranchConflicts = Omit<Schemas['BranchConflictsResponse'], ConflictsOverlapField> &
  Partial<Pick<Schemas['BranchConflictsResponse'], ConflictsOverlapField>>

/** Something that stops an update whatever is chosen (the preview's `blockers`). */
export type UpdateBlocker = Schemas['UpdateBlocker']

/** What main brought (preview) or what an update applied, per entity type. */
export type EntityChangeCount = Schemas['EntityChangeCount']

/**
 * `GET /branches/{id}/update-from-main`: a read-only look at the update.
 * `updatable` is false while `blockers` is non-empty; `main_hash` is sent
 * back so the update refuses (409 `main_moved`) if main changed in between.
 * Its `conflicts` are read as PlanBranchConflicts.
 */
export type UpdateFromMainPreview = Omit<Schemas['UpdateFromMainPreview'], 'conflicts'> & {
  conflicts: PlanBranchConflicts
}

export type UpdateFromMainResolution = Schemas['ResolutionCreate']

/** `expected_main_hash` is the preview's `main_hash`; stored choices count only with it. */
export type UpdateFromMainRequest = Schemas['UpdateFromMainRequest']

/** `updated` is false when the branch already had everything on main. */
export type UpdateFromMainResult = Schemas['UpdateFromMainResult']

export type PlanBranchMergeResolution = Schemas['ResolutionResponse']

/** `POST /branches/{id}/resolutions/batch`: 1..5000 choices, all or none. */
export type PlanBranchResolutionBatchCreate = Schemas['ResolutionBatchCreate']

/** One stored row per (entity_type, entity_name, field_name) of the batch. */
export type PlanBranchResolutionBatchResponse = Schemas['ResolutionBatchResponse']

/**
 * `null` `id`/`created_at`/`updated_at` while the project rides the defaults
 * (the row materializes on the first PATCH).
 */
export type ProjectBranchSettings = Schemas['ProjectBranchSettingsResponse']

export type PlanBranchComment = Schemas['BranchCommentResponse']

/**
 * A branch's diff against its base. `summary` counts `added`, `removed` and
 * `changed`, plus `housekeeping` for the entries carrying a housekeeping
 * reason, which are left out of the other three. `renames` are the
 * removed/added pairs the merge will treat as one rename, computed by the same
 * function the merge applies.
 */
export type PlanBranchDiffSummary = Schemas['PlanBranchDiff']

/** One removed/added pair the merge has already decided is a rename. */
export type PlanDiffRename = Schemas['PlanDiffRename']

/**
 * "As merged": one value's place in the event main will hold after the merge.
 * `value` is absent on a conflict (the merge refuses); `previous` is main as
 * it is now; `branch_value` is the branch's side, set on a conflict or where
 * the merge drops it; `main_moved` means main changed it after the cut and
 * the branch did not.
 */
export type MergedValue = Schemas['MergedValue']
export type MergedState = MergedValue['state']
export type MergedPropertyValue = Schemas['MergedPropertyValue']
export type MergedProperty = Schemas['MergedProperty']

/**
 * `GET /branches/{id}/merge-preview/event`. `ref_id` is the id a `?merged=`
 * link names: the branch's, else main's, else the base's. `blocked` means
 * this event, its type, its fields or one of its properties blocks the merge;
 * `branch_merge_blocked` that the merge refuses for some reason, here or
 * elsewhere.
 */
export type MergedEventPreview = Schemas['MergedEventPreview']
export type MergedEventOutcome = MergedEventPreview['outcome']

/** Which event the preview is for: by any side's id, or by type and name. */
export type MergePreviewTarget = { eventId: string } | { eventType: string; eventName: string }

/**
 * One entry of a plan diff. `entity_id` is the entity's id — branch-side for
 * added/changed, base-side for removed; null on legacy snapshots. `warnings`
 * are things a reviewer should know that are not a change between the two
 * sides. `housekeeping` is set when the entry is the machine's doing rather
 * than the author's, with the reason in words; such entries are left out of
 * `summary`'s counts and folded in the UI. `field_changes` carry the raw
 * before/after of a `changed` entry; `before`/`after` the full entity state.
 */
export type PlanDiffEntry = Schemas['PlanDiffEntry']
export type PlanDiffEntityType = PlanDiffEntry['entity_type']
export type PlanDiffKind = PlanDiffEntry['kind']

/**
 * One member of a collection-valued field (an event field value, a tag, a
 * per-event override) that moved. `key` is the member's natural identifier;
 * `before`/`after` carry its value with the key stripped out.
 */
export type PlanValueChange = Schemas['PlanValueChange']

/** Raw before/after for one changed field; `items` break a collection down per member. */
export type PlanFieldChange = Schemas['PlanFieldChange']

export type PlanDiff = Schemas['PlanDiff']

/**
 * One row of the audit list, with no `payload`: AuditTab fetches a row's
 * payload only when it is expanded.
 *
 * `branch_id` null/empty means the write was NOT made through a branch-scoped
 * request — main, or an action with no plan-branch dimension at all — so it
 * must never be rendered as "main". `branch_id` is nulled when the branch is
 * deleted; `branch_name` is kept verbatim so the trail outlives it.
 */
export type AuditEntry = Schemas['AuditEntryResponse']

/** One entry WITH the request payload that produced it: `GET /audit/{id}`. */
export type AuditEntryDetail = Schemas['AuditEntryDetailResponse']

/**
 * The filter's action vocabulary, served by GET /audit/actions. `project`
 * lists actions recorded with a project (the only ones a project-scoped query
 * matches); `workspace` those recorded with none.
 */
export type AuditActionGroup = Schemas['AuditActionGroup']
export type AuditActionCatalog = Schemas['AuditActionCatalog']

export type AuditListResponse = Schemas['AuditListResponse']
