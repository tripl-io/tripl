/**
 * Dependency graph and impact analysis (F04, #257).
 *
 * Hand-written until the backend's `dependency_service` schemas land in the
 * committed OpenAPI (`api.gen.ts`); switch each alias to
 * `components['schemas'][...]` once they do. The shapes follow the shared
 * contract: an edge names the dependent, why it depends (`relation`), and how
 * sure the server is (`certainty`). A `possible` edge is a SQL identifier match
 * — it warns, it never blocks.
 */

/** The kinds an edge may point at. `scan_config` (scan-level breakdown
 * columns, an event-type binding) and `detection_override` (a per-scope
 * detection setting) appear only as dependents: they have no "Used by" page of
 * their own, so they cannot be asked about. */
export const DEPENDENCY_KINDS = [
  'event',
  'event_type',
  'field',
  'variable',
  'metric',
  'fact_table',
  'alert_rule',
  'relation',
  'scan_config',
  'detection_override',
] as const

export type DependencyKind = (typeof DEPENDENCY_KINDS)[number]

/** The kinds a caller may ask about (`?entity=<kind>:<id>`, POST /impact). */
export type DependencyEntityKind = Exclude<DependencyKind, 'scan_config' | 'detection_override'>

export type DependencyCertainty = 'direct' | 'possible'

/** One entity a dependency points at, and why. */
export interface DependencyEdge {
  kind: DependencyKind
  id: string
  name: string
  /** Why the edge exists, e.g. "metric uses event in its composition". */
  relation: string
  certainty: DependencyCertainty
  /** An app path (`/p/{slug}/...`) without `?branch=`; null when there is no
   * page to link to. */
  url_hint: string | null
  /** 1 for a neighbour, 2 for a neighbour's neighbour (only with depth=2). */
  depth?: number
}

/** The entity asked about, as the server resolved it. */
export interface DependencyEntity {
  kind: DependencyEntityKind
  id: string
  /** Null when the id resolves to nothing. */
  name: string | null
  exists: boolean
}

/** `GET /projects/{slug}/dependencies?entity=<kind>:<id>&depth=1|2`. */
export interface DependenciesResponse {
  entity: DependencyEntity
  upstream: DependencyEdge[]
  downstream: DependencyEdge[]
  /** Direct downstream edges per kind. */
  counts_by_kind: Record<string, number>
  /** Possible (name-matched) downstream edges per kind, counted apart so a
   * SQL coincidence never inflates a direct count. */
  possible_counts_by_kind: Record<string, number>
}

/** `change` is what the branch impact reports for an entity edited in place
 * (a field's type); a confirm dialog sends one of the other three. */
export type ImpactChangeType = 'delete' | 'deprecate' | 'rename' | 'change'

/** One planned change a confirm dialog or a branch asks about. */
export interface ImpactChange {
  kind: DependencyEntityKind
  id: string
  change: ImpactChangeType
}

export interface ImpactItem {
  change: ImpactChange
  entity?: DependencyEntity
  /** `entity.name` repeated flat, for list rows. */
  name?: string | null
  affected: DependencyEdge[]
  /** "2 metrics and 1 alert rule". */
  summary: string
}

/** `POST /projects/{slug}/impact` and `GET /projects/{slug}/branches/{id}/impact`. */
export interface ImpactResponse {
  items: ImpactItem[]
}
