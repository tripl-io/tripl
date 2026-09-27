/**
 * Lifecycle findings (#258): what the daily sunset watch reports about a
 * deprecated event and its successor. Written only by the backend beat task,
 * so every field here is read-only.
 *
 * - `sunset_overdue`: a deprecated event past its `sunset_at` that still
 *   received volume in the last 24h (`volume_24h`).
 * - `successor_silent`: the event a deprecated one names as its successor has
 *   received nothing in the last 7 days (`successor_volume_7d`, 0 when open).
 */
export type LifecycleFindingKind = 'sunset_overdue' | 'successor_silent'

export interface LifecycleFinding {
  id: string
  /** Always the DEPRECATED event the finding hangs on, whichever page shows it. */
  event_id: string
  kind: LifecycleFindingKind
  /** The successor on `successor_silent`; null on `sunset_overdue`. */
  related_event_id?: string | null
  related_event_name?: string | null
  first_seen_at: string
  last_seen_at: string
  /** Null while the condition still holds; stamped when a run finds it cleared. */
  resolved_at: string | null
  volume_24h?: number | null
  successor_volume_7d?: number | null
  /** Optional: the list endpoint may name the event so a roster needs no lookup. */
  event_name?: string | null
}

/** One side of a deprecated event's migration to its successor. */
export interface EventMigrationSide {
  event_id: string
  name: string
  daily_avg_7d: number
}

/**
 * `GET /projects/{slug}/events/{id}/migration`: how far traffic has moved from a
 * deprecated event to the one that replaces it. `ratio` is new ÷ old — how many
 * times the old daily volume the successor now receives (1,240 → 3,800/day is
 * 3.06), so it is not capped at 1. Null when the old side is silent (nothing to
 * divide by).
 */
export interface EventMigration {
  old: EventMigrationSide
  new: EventMigrationSide
  ratio: number | null
}
