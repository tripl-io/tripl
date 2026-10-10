import { REVIEW_STATUS } from '@/lib/statusLexicon'

/**
 * An event's attributes as a person reads them, keyed by the backend's own
 * names: the keys an event's state carries in the branch diff and the merge
 * preview (`superseded_by`), and the columns an edit records in the event's
 * history (`superseded_by_event_id`). The single and bulk event forms, the
 * event page and its recent activity, the branch diff and the "as merged"
 * sheet label an attribute from here, so one attribute has one name on every
 * surface. The words are the ones the docs use.
 */
export const EVENT_ATTRIBUTE_LABEL = {
  name: 'Name',
  title: 'Title',
  description: 'Description',
  event_type_name: 'Event type',
  status: 'Status',
  source_name: 'Scan identity',
  owner_id: 'Owner',
  // "Verified", the word every event surface uses for this flag; see REVIEW_STATUS.
  reviewed: REVIEW_STATUS.reviewed.label,
  sunset_at: 'Sunset date',
  superseded_by: 'Replaced by',
  superseded_by_event_id: 'Replaced by',
  tags: 'Tags',
  field_values: 'Field values',
  meta_values: 'Meta fields',
  metric_breakdown_columns: 'Metric breakdowns',
  required_presence_threshold: 'Required presence',
} as const satisfies Record<string, string>

type EventAttributeKey = keyof typeof EVENT_ATTRIBUTE_LABEL

function isEventAttributeKey(key: string): key is EventAttributeKey {
  // Own keys only: `key in` would answer for "constructor" too.
  return Object.hasOwn(EVENT_ATTRIBUTE_LABEL, key)
}

/**
 * The label of one event attribute. A key the map does not name yet reads
 * as words ("some_new_field" → "Some new field") rather than as the column.
 */
export function eventAttributeLabel(key: string): string {
  if (isEventAttributeKey(key)) return EVENT_ATTRIBUTE_LABEL[key]
  const words = key.replace(/_/g, ' ').trim()
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : key
}
