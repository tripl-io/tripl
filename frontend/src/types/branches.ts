export interface PlanRevisionEntityCounts {
  event_types: number
  fields: number
  events: number
  variables: number
  meta_fields: number
  relations: number
}

export interface PlanRevisionSummary {
  id: string
  project_id: string
  summary: string
  created_at: string
  created_by: string | null
  /** What produced it: a user snapshot, a branch's merge base, or a merge. */
  kind: PlanRevisionKind
  /** The branch behind a `branch_base` or `merge`; null once that branch is deleted. */
  branch_id: string | null
  entity_counts: PlanRevisionEntityCounts
}

export type PlanRevisionKind = 'snapshot' | 'branch_base' | 'merge'

export interface PlanRevisionDetail extends PlanRevisionSummary {
  payload: Record<string, unknown>
}

export type PlanBranchKind = 'main' | 'working'

export type PlanBranchStatus =
  | 'draft'
  | 'ready_for_review'
  | 'changes_requested'
  | 'approved'
  | 'merged'
  | 'closed'

export type PlanBranchTransitionAction =
  | 'submit'
  | 'request_changes'
  | 'approve'
  | 'reopen'
  | 'close'

export interface PlanBranchSummary {
  id: string
  project_id: string
  name: string
  kind: PlanBranchKind
  status: PlanBranchStatus
  description: string
  base_revision_id: string | null
  created_by: string | null
  merged_at: string | null
  merged_by: string | null
  created_at: string
  updated_at: string
}

export interface PlanBranchReviewer {
  id: string
  user_id: string
  created_at: string
}

export interface PlanBranchApproval {
  user_id: string | null
  approved_at: string
  /**
   * The branch changed after this approval, so the merge gate does not count
   * it. Counting rows without checking this shows a quota the merge endpoint
   * rejects — see the approvals chip in BranchesTab.
   */
  stale: boolean
}

export type ResolutionChoice = 'ours' | 'theirs'

export interface PlanBranchConflictField {
  /** A field of the entity, or `@presence` when one side deleted the entity
   * (or its parent) while the other edited or added it; `base`/`ours`/`theirs`
   * are then `"present"` or `"absent"`. */
  field: string
  base: unknown
  ours: unknown
  theirs: unknown
  choice: ResolutionChoice | null
  /** On a presence row whose "take main" deletes an event type: how many
   * fields, events and relations this branch added or edited under it go with
   * it. 0 everywhere else (the backend's `ConflictField.dependents`). */
  dependents: number
}

export interface PlanBranchConflictEntity {
  entity_type: string
  name: string
  /** The owning entity's name — the event type of a field definition, for
   * one. Absent on responses from an older instance. */
  parent?: string | null
  /** A display name for the row ("checkout.amount"). Absent on responses from
   * an older instance; fall back to `name`. */
  label?: string
  fields: PlanBranchConflictField[]
}

export interface PlanBranchConflicts {
  entities: PlanBranchConflictEntity[]
  unresolved_count: number
  /** Main changed since the branch's base: the same test as the list's
   * `behind_base`. Absent on responses from an older instance. */
  behind?: boolean
  /** Distinct entities changed both here and on main. */
  overlap_count?: number
  /** The merge would refuse as things stand — main changed an entity this
   * branch also changes in a way no field choice resolves for the merge.
   * Update from main clears it. */
  merge_blocked?: boolean
  /** Whether "Update from main" can run at all: false for a branch whose base
   * predates complete merge baselines. Anything else that would stop an
   * update is the preview's `blockers`. Absent on older instances (true). */
  updatable?: boolean
}

/** Something that stops an update whatever is chosen (the preview's `blockers`). */
export interface UpdateBlocker {
  kind: 'incomplete_base_snapshot' | 'ambiguous' | 'identity_clash'
  entity_type: PlanDiffEntityType | null
  name: string | null
  message: string
}

/** What main brought (preview) or what an update applied, per entity type. */
export interface EntityChangeCount {
  entity_type: PlanDiffEntityType
  added: number
  changed: number
  removed: number
  renamed: number
}

/** `GET /branches/{id}/update-from-main`: a read-only look at the update. */
export interface UpdateFromMainPreview {
  behind: boolean
  /** False while `blockers` is non-empty: the update would refuse. */
  updatable: boolean
  blockers: UpdateBlocker[]
  base_revision_id: string | null
  /** Main's plan hash as the preview saw it; sent back so the update refuses
   * (409 `main_moved`) if main changed in between. */
  main_hash: string
  main_changes: EntityChangeCount[]
  conflicts: PlanBranchConflicts
}

export interface UpdateFromMainResolution {
  entity_type: PlanDiffEntityType
  entity_name: string
  field_name: string
  choice: ResolutionChoice
}

export interface UpdateFromMainRequest {
  /** The preview's `main_hash`. Choices stored earlier count only with it. */
  expected_main_hash?: string | null
  resolutions?: UpdateFromMainResolution[]
}

export interface UpdateFromMainResult {
  /** False when the branch already had everything on main: nothing written. */
  updated: boolean
  branch: PlanBranchDetail
  applied: EntityChangeCount[]
  previous_base_revision_id: string | null
  base_revision_id: string | null
}

export interface PlanBranchMergeResolution {
  id: string
  branch_id: string
  entity_type: string
  entity_name: string
  field_name: string
  choice: ResolutionChoice
  resolved_by: string | null
  created_at: string
}

export interface PlanBranchDetail extends PlanBranchSummary {
  reviewers: PlanBranchReviewer[]
  approvals: PlanBranchApproval[]
}

export interface PlanBranchList {
  items: PlanBranchSummary[]
  total: number
}

export interface ProjectBranchSettings {
  /** Null while the project rides the defaults (row materializes on first PATCH). */
  id: string | null
  project_id: string
  min_approvals: number
  block_self_approval: boolean
  created_at: string | null
  updated_at: string | null
}

export interface PlanBranchComment {
  id: string
  branch_id: string
  parent_id: string | null
  user_id: string | null
  body: string
  created_at: string
  updated_at: string
}

export interface PlanBranchDiffSummary {
  entries: PlanDiffEntry[]
  /** `housekeeping` counts the entries carrying a `housekeeping` reason; they
   * are left out of the other three. Absent on responses from an older instance. */
  summary: { added: number; removed: number; changed: number; housekeeping?: number }
  behind_base: boolean
  /** Removed/added pairs the merge will treat as one rename, computed by the
   * same function the merge applies — so the UI reports the merge's decision
   * rather than guessing at it. Absent on responses from an older instance. */
  renames?: PlanDiffRename[]
}

/** One removed/added pair the merge has already decided is a rename. */
export interface PlanDiffRename {
  entity_type: PlanDiffEntityType
  parent: string | null
  removed_name: string
  added_name: string
}

export interface PlanRevisionList {
  items: PlanRevisionSummary[]
  total: number
}

export type PlanDiffEntityType =
  | 'event_type'
  | 'field_definition'
  | 'event'
  | 'variable'
  | 'meta_field'
  | 'relation'

export type PlanDiffKind = 'added' | 'removed' | 'changed'

/** One member of a collection-valued field (an event field value, a tag, a
 * per-event override) that moved. `key` is the member's natural identifier;
 * `before`/`after` carry its value with the key stripped out. */
export interface PlanValueChange {
  key: string
  kind: PlanDiffKind
  before: unknown
  after: unknown
}

/** Raw before/after values for a single changed field (structured mirror of
 * the human-readable `changes` strings). Present on `changed` entries. */
export interface PlanFieldChange {
  field: string
  before: unknown
  after: unknown
  /** Per-member breakdown for collection-valued fields; empty for scalars. */
  items?: PlanValueChange[]
}

export interface PlanDiffEntry {
  entity_type: PlanDiffEntityType
  kind: PlanDiffKind
  name: string
  parent: string | null
  /** Id of the entity the entry describes — branch-side for added/changed,
   * base-side for removed. Null on legacy snapshots that predate id capture. */
  entity_id?: string | null
  changes: string[]
  /** Things a reviewer should know that are not a change between the two sides
   * — an event on a scan-governed type with no scan identity, or a name more
   * than one event (or a pair of fields more than one relation) holds, which
   * the diff, the merge and a revert cannot tell apart. Plain sentences.
   * Absent on responses from an older instance. */
  warnings?: string[]
  /** Set when the entry is the machine's doing rather than the author's — a
   * scan-minted variable nobody used being retired, or a removal main has
   * already made — with the reason in words. Left out of `summary`'s counts
   * and folded in the UI. Absent on responses from an older
   * instance. */
  housekeeping?: string | null
  /** Per-field before/after for `changed` entries; empty/absent otherwise.
   * Optional to match the OpenAPI shape (Pydantic default → not required). */
  field_changes?: PlanFieldChange[]
  /** Full entity state on the base side; null/absent for `added` entries. */
  before?: Record<string, unknown> | null
  /** Full entity state on the branch side; null/absent for `removed` entries. */
  after?: Record<string, unknown> | null
}

export interface PlanDiff {
  revision_id: string
  compare_to: string
  entries: PlanDiffEntry[]
  summary: { added: number; removed: number; changed: number }
}

/**
 * One row of the audit list, with no `payload` — the list response does not
 * carry one. AuditTab renders a payload only for the rows a reader expanded, so
 * a page of them crossed the wire to be displayed nowhere; the row now fetches
 * its own on expand.
 */
export interface AuditEntry {
  id: string
  created_at: string
  user_id: string | null
  user_email: string
  project_id: string | null
  project_slug: string
  /**
   * The plan branch the write was scoped to. Null/empty means the write was NOT
   * made through a branch-scoped request — main, or an action with no
   * plan-branch dimension at all (alerting, scans, data sources, users, API
   * keys) — so it must never be rendered as "main". `branch_id`
   * is nulled when the branch is deleted; `branch_name` is kept verbatim so the
   * trail outlives it.
   */
  branch_id: string | null
  branch_name: string
  action: string
  target_type: string
  target_id: string | null
  target_name: string
}

/** One entry WITH the request payload that produced it: `GET /audit/{id}`. */
export interface AuditEntryDetail extends AuditEntry {
  payload: Record<string, unknown>
}

// Mirrors AuditActionGroup / AuditActionCatalog (schemas/audit.py), served by
// GET /audit/actions: the filter's action vocabulary, owned by the backend.
export interface AuditActionGroup {
  label: string
  actions: string[]
}

export interface AuditActionCatalog {
  /** Actions recorded with a project: the only ones a project-scoped query matches. */
  project: AuditActionGroup[]
  /** Actions recorded with no project: only the unfiltered workspace feed matches them. */
  workspace: AuditActionGroup[]
}

export interface AuditListResponse {
  items: AuditEntry[]
  total: number
}
