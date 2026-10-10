import type { ChipTone } from '@/components/primitives/chip'
import type { DotTone } from '@/components/primitives/dot'
import type { components } from '@/types/api.gen'

// Derived from the OpenAPI-generated types rather than retyped, so a status
// added or renamed on the backend becomes a compile error here — every Record
// below is keyed by this union and stops being exhaustive. Retyping it is how
// `Event.status` came to be a plain `string` in the first place.
export type EventStatus = components['schemas']['EventStatus']

export const EVENT_STATUSES: EventStatus[] = [
  'draft',
  'in_review',
  'ready_for_dev',
  'implemented',
  'live',
  'deprecated',
  'archived',
]

// Sentence case, like every other label in the app, so a status chip and the
// stat or help text beside it on the same screen spell the status the same way.
export const EVENT_STATUS_LABELS: Record<EventStatus, string> = {
  draft: 'Draft',
  in_review: 'In review',
  ready_for_dev: 'Ready for dev',
  implemented: 'Implemented',
  live: 'Live',
  deprecated: 'Deprecated',
  archived: 'Archived',
}

/**
 * Canonical status tone — matches the design mockup `STATUS_TONE`.
 * Drives Chip-based status rendering (events table, detail, edit) and the
 * status dot below. `ChipTone` and `DotTone` share the same tone vocabulary.
 */
export const EVENT_STATUS_TONE: Record<EventStatus, ChipTone> = {
  draft: 'neutral',
  in_review: 'warning',
  ready_for_dev: 'info',
  implemented: 'success',
  live: 'success',
  // Neutral, not warning: In review is already amber, and a deprecated event
  // needs no action, so the two no longer share one colour.
  deprecated: 'neutral',
  archived: 'neutral',
}

/**
 * Status indicator dot tone (used in EventRow). Derived from the canonical
 * tone scale so the dot and the status chip never drift apart. Tones are
 * shared between statuses (Implemented and Live are both green), so a dot is
 * never the only carrier of a status: it sits beside the status chip or names
 * the status itself (`Dot`'s `label`).
 */
export const EVENT_STATUS_DOT_TONE: Record<EventStatus, DotTone> = EVENT_STATUS_TONE
