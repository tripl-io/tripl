/**
 * Plan health score (F15, #268).
 *
 * Hand-written from `backend/src/tripl/schemas/health.py` until those schemas
 * land in the committed OpenAPI (`api.gen.ts`); switch each alias to
 * `components['schemas'][...]` once they do. Every figure is about the MAIN
 * plan's non-archived events: the endpoints take no branch.
 *
 * The weights are fixed in v1 (25/20/15/15/10/15) and not configurable per
 * project. A component that does not apply to an event is excluded and the
 * others are rescaled to 100 ("renormalized").
 */

export type HealthComponentKey =
  | 'implemented_seen'
  | 'contract'
  | 'drifts'
  | 'signals'
  | 'freshness'
  | 'documentation'

/** healthy >= 80, warning >= 50, otherwise unhealthy. */
export type HealthGrade = 'healthy' | 'warning' | 'unhealthy'

/** One of the six parts of an event's score. */
export interface HealthComponent {
  key: HealthComponentKey
  label: string
  /** The fixed weight: 25/20/15/15/10/15. */
  weight: number
  applies: boolean
  /** Why the component does not count; set when `applies` is false. */
  excluded_reason: string | null
  /** 0..1; null when excluded. */
  value: number | null
  /** Percent of the score after renormalization, 1 decimal; null when excluded. */
  effective_weight: number | null
  /** `effective_weight * value`, 1 decimal; null when excluded. */
  points: number | null
  /** e.g. "2 of 9 contract rules failing: amount (range), plan (enum)". */
  detail: string
  /** e.g. `{ violated: 2, total: 9 }`. */
  counts: Record<string, number>
}

export interface EventHealth {
  event_id: string
  event_type_id: string
  name: string
  /** 0..100. */
  score: number
  grade: HealthGrade
  /** True when any component is excluded and the rest were rescaled. */
  renormalized: boolean
  excluded: HealthComponentKey[]
  /** The detail of the component that lost the most points. */
  top_issue: string | null
  /** Always all six, in the fixed component order. */
  components: HealthComponent[]
}

/** `GET /projects/{slug}/health/events?ids=…` */
export interface EventHealthListResponse {
  items: EventHealth[]
  computed_at: string
}

export interface EventHealthBrief {
  event_id: string
  name: string
  score: number
  grade: HealthGrade
  top_issue: string | null
}

/** A component's mean value over the events it applies to. */
export interface ComponentAverage {
  key: HealthComponentKey
  value: number | null
  applies_count: number
}

export interface EventTypeHealth {
  event_type_id: string
  /** Mean of its events' scores; null when it has no scored events. */
  score: number | null
  grade: HealthGrade | null
  scored_events: number
  healthy_count: number
  warning_count: number
  unhealthy_count: number
  component_averages: ComponentAverage[]
  /** Up to five, least healthy first. */
  worst: EventHealthBrief[]
}

/** `GET /projects/{slug}/health/event-types` */
export interface EventTypeHealthListResponse {
  items: EventTypeHealth[]
}

export interface ProjectHealthTrendPoint {
  /** YYYY-MM-DD. */
  day: string
  score: number | null
  scored_events: number
}

/** `GET /projects/{slug}/health?trend_days=30` */
export interface ProjectHealthResponse {
  score: number | null
  grade: HealthGrade | null
  scored_events: number
  healthy_count: number
  warning_count: number
  unhealthy_count: number
  component_averages: ComponentAverage[]
  /** The five lowest scores (score asc, name, id). */
  worst: EventHealthBrief[]
  /** Ascending by day, from the daily snapshots. */
  trend: ProjectHealthTrendPoint[]
  /** The snapshot 7 days before today, for the delta. */
  previous_score: number | null
  computed_at: string
}
