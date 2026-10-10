import type { components } from './api.gen'
import type { EventTypeBrief } from './eventTypes'
import type { LifecycleFinding } from './lifecycle'

/** The backend's 7-value EventStatus, not a free-form string. */
export type EventStatus = components['schemas']['EventStatus']

export type VariableValueKind = 'low' | 'high'

export interface EventFieldVariableValue {
  id: string
  variable_id: string
  variable_name: string
  source_column: string
  value_kind: VariableValueKind
  observed_count: number
  values: string[]
  /** Optional to mirror the backend default: an older response omits it. */
  excluded_from_scans?: boolean
  /** When these values were last WRITTEN — not when a scan last confirmed
   *  them. Optional because `SearchEventVariableValue` extends this interface
   *  and the search response is hand-built without the column; declaring it
   *  required would make that type claim a field its endpoint never sends. */
  updated_at?: string
}

export interface EventFieldValue {
  id: string
  field_definition_id: string
  value: string
  /**
   * A hand-typed value scans will never overwrite again. Optional to mirror the
   * backend default: an older response omits it, and a missing flag must read
   * as "the scan still maintains this", not as "frozen".
   */
  is_authored?: boolean
  variable_values?: EventFieldVariableValue[]
  /** The values the last observing scan saw when this field's rows disagreed.
   *  Only the single-event reads attach it; null in lists and when the field
   *  had one value. A branch copy carries its main twin's. */
  observed_values?: EventFieldObservedValues | null
}

/** Mirrors `ObservedFieldValue` in backend/src/tripl/schemas/event.py. */
export interface ObservedFieldValue {
  value: string
  /** Summed breakdown-row count; null when the adapter returned no counts. */
  count: number | null
  /** `count / total_count` over the full set, values past the cap included. */
  share: number | null
}

/** Mirrors `EventFieldObservedValues` in backend/src/tripl/schemas/event.py. */
export interface EventFieldObservedValues {
  distinct_count: number
  total_count: number | null
  /** Busiest first, at most 20. */
  values: ObservedFieldValue[]
  /** Rows carried by the values past the 20 kept; null when counts are unknown. */
  other_count: number | null
  observed_at: string
  scan_config_id: string | null
}

export interface EventMetaValue {
  id: string
  meta_field_definition_id: string
  value: string
}

export interface EventTag {
  id: string
  name: string
}

export interface Event {
  id: string
  project_id: string
  event_type_id: string
  event_type: EventTypeBrief
  name: string
  /**
   * The key a scan matches this event on — not `name`, which can be renamed
   * freely without the scan losing the event. Null until a scan sees it or a
   * naming rule governs its type.
   */
  source_name: string | null
  /** Free-text label shown beside the identity; never part of it. */
  title: string
  /** Presence at or above which a scanned JSON property counts as always carried
   *  (F23); null: the default, 0.95. Optional: an older response omits it. */
  required_presence_threshold?: number | null
  description: string
  order: number
  status: EventStatus
  sunset_at: string | null
  /**
   * The event that replaced this one — documentation and nothing else: no
   * matcher, collector or coverage count reads it. Ids are branch-local, so on
   * a branch copy this names that branch's own row, never main's. Absent from
   * list items, which do not carry it.
   */
  superseded_by_event_id?: string | null
  last_seen_at: string | null
  /** Oldest metric bucket with traffic; null until a collection sees the event,
   * and on list responses, which do not compute it. */
  first_seen_at?: string | null
  /** A branch copy's twin on main; null on main, for a branch-only event, and on
   *  every response but the single-event read. */
  main_event_id?: string | null
  owner_id: string | null
  reviewed: boolean
  metric_breakdown_columns: string[]
  drift_count: number
  tags: EventTag[]
  field_values: EventFieldValue[]
  meta_values: EventMetaValue[]
  created_at: string
  updated_at: string
  /** The branch the row lives on. `GET /events/{id}` answers for any branch of
   * the project, so a reader can tell when the row is not on the branch it is
   * looking at. Absent on list items and on responses from an older instance. */
  branch_id?: string | null
  /** Open lifecycle findings on this event (#258): past sunset and still
   *  sending, or a silent successor. Single-event read only; optional so an
   *  older instance that never sends it reads as "none". */
  lifecycle_findings?: LifecycleFinding[]
}

export interface EventMutationResponse extends Event {
  warnings: string[]
}

export interface EventChange {
  id: string
  event_id: string
  user_id: string | null
  user_email: string | null
  /**
   * Who to credit when no person made the change: `"tripl (scan)"` on a
   * transition the metrics worker made from data (auto-live, #258). Null on a
   * person's edit, which `user_email` names.
   */
  author_label?: string | null
  field: string
  old_value: string | null
  new_value: string | null
  created_at: string
}

export type SchemaDriftType =
  | 'new_field'
  | 'missing_field'
  | 'type_changed'
  | 'enum_violation'
  | 'required_null_violation'
  | 'regex_violation'
  | 'range_violation'

export interface SchemaDrift {
  id: string
  event_type_id: string
  scan_config_id: string | null
  field_name: string
  drift_type: SchemaDriftType
  observed_type: string | null
  declared_type: string | null
  sample_value: string | null
  status: 'open' | 'accepted' | 'snoozed' | 'false_positive'
  resolution_note: string | null
  snoozed_until: string | null
  resolved_at: string | null
  resolved_by: string | null
  detected_at: string
}

export interface SchemaDriftList {
  items: SchemaDrift[]
  total: number
}

// Slim shape returned by GET /events: drops nested event_type since the
// frontend already has EventTypes cached and looks them up by id, and adds two
// values the list endpoint computes per row and the detail response does not
// carry — `monitored` (alert-rule coverage) and `open_question_count`
// (unanswered discussion threads, read through to the main twin on a branch).
export type EventListItem = Omit<Event, 'event_type'> & {
  monitored: boolean
  open_question_count?: number
  /** True while the event has an open lifecycle finding (#258); drives the
   *  catalog's "Lifecycle" chip. Absent on an older instance. */
  lifecycle_warning?: boolean
}

export interface EventListResponse {
  items: EventListItem[]
  total: number
}

/** One looked-up identity an event already holds (GET /events/by-names). */
export interface EventIdentityHolder {
  /** The name that was asked about. */
  identity: string
  event_id: string
  /** The holder's own name; differs from `identity` for a renamed scanned event. */
  name: string
  source_name: string | null
}

export interface EventIdentityHoldersResponse {
  items: EventIdentityHolder[]
}

export type EventPhotoKind = 'photo' | 'figma'

export interface EventPhoto {
  id: string
  event_id: string
  project_id: string
  kind: EventPhotoKind
  original_filename: string
  content_type: string
  size_bytes: number
  storage_backend: 'local' | 'gcs' | null
  sort_order: number
  url: string
  external_url: string | null
  uploaded_by_user_id: string | null
  created_at: string
}

/** GET /orgs/{org}/settings/photo-limits: what the upload endpoint takes. */
export interface PhotoLimits {
  photo_max_size_mb: number
  /** Lower-case content types the organization accepts (F20 PR11). */
  photo_allowed_mime: string[]
}

export interface EventPhotoComment {
  id: string
  /** Exactly one anchor is set, enforced by ck_event_photo_comment_one_anchor:
   *  a comment pinned to one attachment, or the event's own discussion. Both
   *  are nullable here for that reason — the type claimed a photo_id was
   *  always present, which stopped being true when the event anchor landed. */
  photo_id: string | null
  event_id?: string | null
  parent_id: string | null
  user_id: string | null
  body: string
  /** Resolution state of the THREAD. Present on every row because both anchors
   *  share one table, but only a top-level comment can be acted on — the server
   *  refuses an action on a reply. A `snoozed` thread whose `snoozed_until` has
   *  passed counts as open again; `isThreadUnanswered` is the one place that
   *  decides it. */
  status?: EventCommentStatus
  resolution_note?: string | null
  snoozed_until?: string | null
  resolved_at?: string | null
  resolved_by?: string | null
  created_at: string
  updated_at: string
}

export type EventCommentStatus = 'open' | 'resolved' | 'snoozed'

export type EventCommentAction = 'resolve' | 'snooze' | 'reopen'

export type VariableType = 'string' | 'number' | 'boolean' | 'date' | 'datetime' | 'json' | 'string_array' | 'number_array'

/** The JSON Schema subset `backend/src/tripl/core/property_schema.py` accepts. */
export interface PropertySchema {
  type: 'string' | 'number' | 'integer' | 'boolean' | 'array' | 'object'
  description?: string
  format?: string
  pattern?: string
  minLength?: number
  maxLength?: number
  minimum?: number
  maximum?: number
  exclusiveMinimum?: number
  exclusiveMaximum?: number
  multipleOf?: number
  enum?: (string | number | boolean)[]
  items?: PropertySchema
  minItems?: number
  maxItems?: number
  uniqueItems?: boolean
  properties?: Record<string, PropertySchema>
  required?: string[]
  additionalProperties?: boolean
}

export interface Variable {
  id: string
  project_id: string
  name: string
  source_name: string | null
  variable_type: VariableType
  description: string
  allowed_values: string[]
  bindings: string[]
  /** JSON Schema fragment refining `variable_type`; null when the type is just
   *  `variable_type`. Optional: an older response omits it. */
  json_schema?: PropertySchema | null
  excluded_from_scans?: boolean
  open_drift_count?: number
  /** Events whose property list carries it (F23); `event_count` is where scans saw it. */
  listed_event_count?: number
  /** Of those, the events that require it. */
  required_event_count?: number
  /** Events whose field or meta values name its `${token}` (the `usage` filter's token scan). */
  value_event_count?: number
  event_count?: number
  context_count?: number
  low_context_count?: number
  high_context_count?: number
  /** Observed values unioned across every context, de-duplicated and capped
   * server-side — enough for the list row's chips without a per-row request. */
  sample_values?: string[]
  /** Distinct event names this variable was observed in, alphabetical and
   * capped server-side; `event_count` carries the untruncated total. */
  event_names?: string[]
  /** The same events as id + name, so each can link to its event.
   * Ordered by name, capped like `event_names`; two events may share a name. */
  event_refs?: { id: string; name: string }[]
}

/** Envelope returned by `GET /projects/{slug}/variables` (offset/limit paged). */
export interface VariableListPage {
  items: Variable[]
  total: number
}

export interface VariableValueContext extends EventFieldVariableValue {
  event_id: string
  event_name: string
  field_definition_id: string
  field_name: string
  field_display_name: string
}
