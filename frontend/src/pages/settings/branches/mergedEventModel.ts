/**
 * "As merged": the pure half of the sheet that shows one event as main will
 * hold it after the merge. The projection itself is the backend's
 * (`GET /branches/{id}/merge-preview/event`, which reads the merge's own gate);
 * this only decides how a state looks and which rows can open the sheet.
 */

import type { ChipTone } from '@/components/primitives/chip-variants'
import type { MergePreviewTarget, MergedState, PlanDiffEntry } from '@/types'

export interface StateMeta {
  tone: ChipTone | null
  /** The word a screen reader hears and the chip shows: colour never stands alone. */
  label: string
  strike: boolean
}

// The diff's own tones (KIND_META): green added, amber changed, red removed.
// Unchanged is quiet text; a conflict is danger, and labelled as one.
const STATE_META: Record<MergedState, StateMeta> = {
  added: { tone: 'success', label: 'Added', strike: false },
  changed: { tone: 'warning', label: 'Changed', strike: false },
  unchanged: { tone: null, label: 'Unchanged', strike: false },
  removed: { tone: 'neutral', label: 'Removed', strike: true },
  conflict: { tone: 'danger', label: 'Conflict', strike: false },
}

export function stateMeta(state: MergedState): StateMeta {
  return STATE_META[state] ?? STATE_META.unchanged
}

/** Above this many unchanged rows the sheet opens with them hidden. */
export const HIDE_UNCHANGED_THRESHOLD = 12

export function visibleRows<T extends { state: MergedState }>(
  rows: readonly T[],
  hideUnchanged: boolean,
): T[] {
  return hideUnchanged ? rows.filter((row) => row.state !== 'unchanged') : [...rows]
}

/** The event row's target: the branch-side id. A rename is rendered by its
 * removal, whose own id is the base side's, so the paired addition's id is
 * passed in. Null for a legacy entry with no id. */
export function previewTargetForEntry(
  entry: Pick<PlanDiffEntry, 'entity_type' | 'entity_id'>,
  renamedEntityId?: string | null,
): MergePreviewTarget | null {
  if (entry.entity_type !== 'event') return null
  const id = renamedEntityId ?? entry.entity_id ?? null
  return id ? { eventId: id } : null
}

interface OverrideMember {
  event_type_name?: unknown
  event_name?: unknown
}

/**
 * A variable row's per-event override items, each mapped to the event it
 * names. The item's key is the snapshot's `"<type>.<event>"` join, which a
 * dotted event name makes ambiguous to split, so the member is looked up in
 * the entry's own list (`after`, else `before`) instead. A key two members
 * share — namesake events — gets no link.
 */
export function previewTargetsForOverrides(
  entry: Pick<PlanDiffEntry, 'entity_type' | 'field_changes' | 'after' | 'before'>,
): Map<string, MergePreviewTarget> {
  const out = new Map<string, MergePreviewTarget>()
  if (entry.entity_type !== 'variable') return out
  const change = (entry.field_changes ?? []).find((c) => c.field === 'event_value_overrides')
  if (!change) return out
  const after = listOf(entry.after?.event_value_overrides)
  const before = listOf(entry.before?.event_value_overrides)
  for (const item of change.items ?? []) {
    const fromAfter = matching(after, item.key)
    const found = fromAfter.length > 0 ? fromAfter : matching(before, item.key)
    const [member] = found
    if (found.length !== 1 || !member) continue
    out.set(item.key, {
      eventType: String(member.event_type_name),
      eventName: String(member.event_name),
    })
  }
  return out
}

function listOf(value: unknown): OverrideMember[] {
  return Array.isArray(value)
    ? value.filter((item): item is OverrideMember => typeof item === 'object' && item !== null)
    : []
}

function matching(members: OverrideMember[], key: string): OverrideMember[] {
  return members.filter(
    (member) =>
      typeof member.event_type_name === 'string' &&
      typeof member.event_name === 'string' &&
      `${member.event_type_name}.${member.event_name}` === key,
  )
}

/** The search params a target is kept in, so Back closes the sheet. */
export const MERGED_PARAM = 'merged'
export const MERGED_TYPE_PARAM = 'merged_type'
export const MERGED_NAME_PARAM = 'merged_name'

export function targetFromParams(params: URLSearchParams): MergePreviewTarget | null {
  const id = params.get(MERGED_PARAM)
  if (id) return { eventId: id }
  const eventType = params.get(MERGED_TYPE_PARAM)
  const eventName = params.get(MERGED_NAME_PARAM)
  return eventType && eventName ? { eventType, eventName } : null
}

export function paramsWithTarget(
  params: URLSearchParams,
  target: MergePreviewTarget | null,
): URLSearchParams {
  const next = new URLSearchParams(params)
  next.delete(MERGED_PARAM)
  next.delete(MERGED_TYPE_PARAM)
  next.delete(MERGED_NAME_PARAM)
  if (target && 'eventId' in target) next.set(MERGED_PARAM, target.eventId)
  else if (target) {
    next.set(MERGED_TYPE_PARAM, target.eventType)
    next.set(MERGED_NAME_PARAM, target.eventName)
  }
  return next
}

const ATTRIBUTE_LABEL: Record<string, string> = {
  title: 'Title',
  description: 'Description',
  status: 'Status',
  source_name: 'Scan identity',
  owner_id: 'Owner',
  reviewed: 'Reviewed',
  sunset_at: 'Sunset',
  superseded_by: 'Replaced by',
  metric_breakdown_columns: 'Metric breakdown',
  required_presence_threshold: 'Required presence',
}

export function attributeLabel(key: string): string {
  return ATTRIBUTE_LABEL[key] ?? key
}
