/**
 * Labels, tones and routes the Branches tab's components share. Kept out of the
 * component files so each of those stays component-only (react-refresh).
 */

import type { ChipTone } from '@/components/primitives/chip'
import { displayUser } from '@/hooks/useUsersById'
import { formatRelativeTime } from '@/lib/datetime'
import type {
  PlanBranchStatus,
  PlanBranchSummary,
  PlanBranchTransitionAction,
  PlanDiffEntityType,
  PlanDiffEntry,
  PlanDiffKind,
} from '@/types'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

export { STATUS_LABEL, STATUS_TONE } from '@/lib/branchStatus'

export const ALLOWED_TRANSITIONS: Record<PlanBranchStatus, PlanBranchTransitionAction[]> = {
  draft: ['submit', 'close'],
  ready_for_review: ['approve', 'request_changes', 'close'],
  changes_requested: ['submit', 'close'],
  // approve stays available so extra reviewers can stack approvals toward the
  // project's min_approvals quota.
  approved: ['approve', 'request_changes', 'reopen', 'close'],
  closed: ['reopen'],
  merged: [],
}

export const ACTION_LABEL: Record<PlanBranchTransitionAction, string> = {
  submit: 'Submit for review',
  request_changes: 'Request changes',
  approve: 'Approve',
  reopen: 'Reopen',
  close: 'Close',
}

/** Verdicts read the diff, so they wait for it; see FeatureBranchDetail. */
export const DIFF_VERDICTS: ReadonlySet<PlanBranchTransitionAction> = new Set([
  'approve',
  'request_changes',
])

// Maps a real diff kind to the mockup's tone-coded gutter symbol / chip label.
export const KIND_META: Record<PlanDiffKind, { tone: ChipTone; sym: string; label: string }> = {
  added: { tone: 'success', sym: '+', label: 'Added' },
  changed: { tone: 'warning', sym: '~', label: 'Modified' },
  removed: { tone: 'danger', sym: '−', label: 'Removed' },
}

// A rename has no diff kind of its own — it arrives as a removal plus an
// addition — but it is not a deletion, and a red "Removed" row sitting beside an
// unrelated green "Added" one says it is. This is what the paired row wears
// instead (tripl-amnn).
export const RENAMED_META: { tone: ChipTone; sym: string; label: string } = {
  tone: 'warning',
  sym: '→',
  label: 'Renamed',
}

// Human-readable entity labels for the expanded change detail.
export const ENTITY_LABEL: Record<PlanDiffEntityType, string> = {
  event_type: 'event type',
  field_definition: 'field',
  event: 'event',
  variable: 'property',
  meta_field: 'meta field',
  relation: 'relation',
}

const LANDED_STATUSES = new Set(['merged', 'closed'])

/**
 * Landed work — merged or closed — as opposed to a branch still in flight.
 *
 * The `kind` test is load-bearing rather than defensive: **main is created with
 * status `merged`**. A status-only predicate therefore files the production plan
 * under "Merged" and leaves the active tab with no base branch, which is also
 * why this split stays client-side — `BranchSwitcher` reads the very same
 * `planBranchesKey(slug)` cache entry, so filtering server-side would empty it
 * too.
 */
export function isLandedBranch(branch: PlanBranchSummary): boolean {
  return branch.kind !== 'main' && LANDED_STATUSES.has(branch.status)
}

/** The API serialises `created_by` as a bare user id; resolve it against the
 * project roster (GET /users is open to any authenticated user), preferring
 * the name and falling back to the email — same convention as EventRow. */
export function branchAuthor(branch: PlanBranchSummary, usersById: Map<string, string>): string {
  return displayUser(usersById, branch.created_by)
}

export function branchSubtitle(branch: PlanBranchSummary, usersById: Map<string, string>): string {
  if (branch.kind === 'main') return 'production'
  return `${branchAuthor(branch, usersById)} · ${formatRelativeTime(branch.updated_at)}`
}

/** The human title an event carries beside its scan name, so a reviewer reading
 * `tap_model_card` also sees "Tap on a model card" (tripl-kjhi.3). Branch side
 * first; a removed entry only has a base side. Events only — that is the one
 * entity whose `title` is a field the plan editor shows. */
export function eventTitle(entry: PlanDiffEntry): string | null {
  if (entry.entity_type !== 'event') return null
  for (const state of [entry.after, entry.before]) {
    const value = state?.title
    if (typeof value === 'string' && value.trim() !== '') return value
  }
  return null
}

/** Where a diff row points. Field definitions, meta fields and relations have no
 * detail route yet — those rows stay unlinked. */
export function entityPath(slug: string, entry: PlanDiffEntry): string | null {
  if (!entry.entity_id) return null
  switch (entry.entity_type) {
    case 'event':
      return projectPath(currentOrgSlug(), slug, `/events/all/${entry.entity_id}`)
    case 'event_type':
      return projectPath(currentOrgSlug(), slug, `/event-types/${entry.entity_id}`)
    case 'variable':
      return projectPath(currentOrgSlug(), slug, `/variables/${entry.entity_id}`)
    default:
      return null
  }
}

/** Where a diff row's Edit action points, per entity type.
 *
 * An event has an editor ROUTE, and `/events/:tab/:eventId/edit` is a
 * first-class one, so this skips the list route that would otherwise bounce
 * through EventsPage.
 *
 * A variable's editor is its own page now (AU-26), Definition tab first — the
 * same address `entityPath` focuses. The row keeps the Edit action anyway
 * (tripl-htfn.2): without it, fixing a variable from a branch review cost
 * exactly the clicks tripl-h2sx.1 removed for events: expand the row, find the
 * small link after Revert.
 *
 * Event types are deliberately absent. `entityPath` already lands them on
 * EventTypeDetail, which IS their editor, so a second affordance to the same
 * page would not be closing the same gap.
 */
export function entityEditPath(
  slug: string,
  entityType: PlanDiffEntry['entity_type'],
  entityId: string,
): string | null {
  switch (entityType) {
    case 'event':
      return projectPath(currentOrgSlug(), slug, `/events/all/${entityId}/edit`)
    case 'variable':
      return projectPath(currentOrgSlug(), slug, `/variables/${entityId}`)
    default:
      return null
  }
}

/** Branch names read like refs everywhere they appear (mono, in the switcher,
 * in `?branch=` links): a letter or digit first, then letters, digits and
 * `- _ / .`, at most 64 characters so the switcher can show them. Upper case
 * stays allowed: production branches are named after their tracker ticket
 * (`PROJ-4770`, see lib/branchTicket). */
const BRANCH_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9/_.-]*$/
export const BRANCH_NAME_MAX = 64
export const BRANCH_NAME_HINT = 'Letters, numbers and - _ / . only, e.g. checkout/paywall-copy or PROJ-4770.'

/** Why a new branch name cannot be used, or null when it can (PL-5). Empty is
 * the caller's "Required", shown only after a submit. */
export function branchNameProblem(name: string, existing: readonly string[]): string | null {
  const trimmed = name.trim()
  if (!trimmed) return null
  if (trimmed.length > BRANCH_NAME_MAX) return `Use at most ${BRANCH_NAME_MAX} characters.`
  if (!BRANCH_NAME_RE.test(trimmed)) {
    return /\s/.test(trimmed)
      ? 'Branch names cannot contain spaces.'
      : 'Start with a letter or number, and use only letters, numbers and - _ / .'
  }
  const lower = trimmed.toLowerCase()
  if (existing.some((other) => other.trim().toLowerCase() === lower)) {
    return 'A branch with this name already exists.'
  }
  return null
}

/** A usable name close to what was typed ("Bad name with spaces!!" →
 * "bad-name-with-spaces"), or null when nothing usable is left. */
export function suggestBranchName(name: string): string | null {
  const slug = name
    .trim()
    .replace(/\s+/g, '-')
    .replace(/[^A-Za-z0-9/_.-]/g, '')
    .replace(/-{2,}/g, '-')
    .replace(/^[^A-Za-z0-9]+|[-_/.]+$/g, '')
    .slice(0, BRANCH_NAME_MAX)
  const hasUpperKey = /^[A-Z][A-Z0-9]+-\d+/.test(slug)
  const suggestion = hasUpperKey ? slug : slug.toLowerCase()
  return suggestion && suggestion !== name.trim() ? suggestion : null
}

/** Readable labels for the entity keys a diff's full state carries (PL-12).
 * Keys missing here are humanised ("sunset_at" → "Sunset at"). */
const STATE_KEY_LABEL: Record<string, string> = {
  event_type_name: 'Event type',
  sunset_at: 'Sunset date',
  superseded_by: 'Superseded by',
  metric_breakdown_columns: 'Metric breakdowns',
  field_values: 'Field values',
  meta_values: 'Meta fields',
  source_name: 'Source',
  variable_type: 'Property type',
  json_schema: 'JSON Schema',
}

export function stateKeyLabel(key: string): string {
  const known = STATE_KEY_LABEL[key]
  if (known) return known
  const words = key.replace(/_/g, ' ').trim()
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : key
}

/** A state value that says nothing: null, empty text, an empty list or map. */
export function isEmptyStateValue(value: unknown): boolean {
  if (value === null || value === undefined) return true
  if (typeof value === 'string') return value.trim() === ''
  if (Array.isArray(value)) return value.length === 0
  if (typeof value === 'object') return Object.keys(value as object).length === 0
  return false
}
