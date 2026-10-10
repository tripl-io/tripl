/**
 * Dependency graph helpers (F04, #257): the words, grouping and links every
 * "Used by" list, impact warning and branch Impact panel share, so the three
 * surfaces read the same sentence for the same edge.
 *
 * Warn only: nothing here disables a confirm. The one blocking check stays the
 * fact-table 409 on the server.
 */
import { getMetricMonitoringPath } from '@/lib/monitoring'
import { countOf } from '@/lib/plural'
import {
  DEPENDENCY_KINDS,
  type DependencyEdge,
  type DependencyKind,
  type ImpactChange,
  type ImpactChangeType,
} from '@/types'
import { currentOrgSlug, projectPath, withActiveOrg } from '@/lib/activeOrg'

export const DEPENDENCY_KIND_LABELS: Record<DependencyKind, { one: string; many: string }> = {
  event: { one: 'event', many: 'events' },
  event_type: { one: 'event type', many: 'event types' },
  field: { one: 'field', many: 'fields' },
  // The UI's name for a variable everywhere else: the Properties catalog.
  variable: { one: 'property', many: 'properties' },
  metric: { one: 'metric', many: 'metrics' },
  fact_table: { one: 'fact table', many: 'fact tables' },
  alert_rule: { one: 'alert rule', many: 'alert rules' },
  relation: { one: 'relation', many: 'relations' },
  scan_config: { one: 'scan', many: 'scans' },
  detection_override: { one: 'detection override', many: 'detection overrides' },
}

/** Group headings, capitalised plural: "Metrics", "Alert rules". */
export function dependencyKindHeading(kind: DependencyKind): string {
  const many = DEPENDENCY_KIND_LABELS[kind].many
  return many.charAt(0).toUpperCase() + many.slice(1)
}

/** Shown next to every `possible` edge, in the tooltip and for screen readers. */
export const POSSIBLE_EDGE_EXPLANATION =
  'Possible: matched by name, not by a stored reference — check the query, binding or scan column. '
  + 'Possible matches never block a change.'

/** "metric", "alert rule"; a kind this client does not know reads as itself. */
export function dependencyKindLabel(kind: string): string {
  return isDependencyKind(kind) ? DEPENDENCY_KIND_LABELS[kind].one : kind.replace(/_/g, ' ')
}

/** Consequence-first ordering: what breaks loudest first. */
const KIND_ORDER: readonly DependencyKind[] = [
  'metric',
  'alert_rule',
  'fact_table',
  'relation',
  'variable',
  'event',
  'event_type',
  'field',
  'scan_config',
  'detection_override',
]

function isDependencyKind(value: string): value is DependencyKind {
  return (DEPENDENCY_KINDS as readonly string[]).includes(value)
}

/**
 * One row per entity. Two hops, or a direct reference plus a SQL match, can
 * name the same metric twice; a direct edge wins over a possible one, since the
 * stored reference is the stronger claim.
 */
export function dedupeEdges(edges: readonly DependencyEdge[]): DependencyEdge[] {
  const byKey = new Map<string, DependencyEdge>()
  for (const edge of edges) {
    const key = `${edge.kind}:${edge.id}`
    const seen = byKey.get(key)
    if (!seen || (seen.certainty === 'possible' && edge.certainty === 'direct')) {
      byKey.set(key, edge)
    }
  }
  return [...byKey.values()]
}

export interface DependencyGroup {
  kind: DependencyKind
  edges: DependencyEdge[]
}

/** Edges grouped by kind, in a fixed order; within a group, direct before
 * possible, then by name. Unknown kinds (a server newer than this client) are
 * dropped rather than rendered with no label. */
export function groupEdgesByKind(edges: readonly DependencyEdge[]): DependencyGroup[] {
  const groups = new Map<DependencyKind, DependencyEdge[]>()
  for (const edge of dedupeEdges(edges)) {
    if (!isDependencyKind(edge.kind)) continue
    const list = groups.get(edge.kind) ?? []
    list.push(edge)
    groups.set(edge.kind, list)
  }
  return KIND_ORDER.flatMap((kind) => {
    const list = groups.get(kind)
    if (!list) return []
    const sorted = [...list].sort((a, b) => {
      if (a.certainty !== b.certainty) return a.certainty === 'direct' ? -1 : 1
      return a.name.localeCompare(b.name)
    })
    return [{ kind, edges: sorted }]
  })
}

/** "a, b and c" */
function joinAnd(parts: readonly string[]): string {
  if (parts.length <= 1) return parts[0] ?? ''
  return `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`
}

/**
 * "2 metrics and 1 alert rule" — the client's own sentence, for when the
 * server's `summary` is missing (and for the branch panel's totals). Possible
 * matches are counted apart so a SQL coincidence never reads as a certainty:
 * "1 metric and 1 possible metric".
 */
export function summarizeEdges(edges: readonly DependencyEdge[]): string {
  const groups = groupEdgesByKind(edges)
  const parts: string[] = []
  for (const { kind, edges: list } of groups) {
    const label = DEPENDENCY_KIND_LABELS[kind]
    const direct = list.filter((e) => e.certainty === 'direct').length
    const possible = list.length - direct
    if (direct > 0) parts.push(countOf(direct, label.one, label.many))
    if (possible > 0) parts.push(countOf(possible, `possible ${label.one}`, `possible ${label.many}`))
  }
  return parts.length ? joinAnd(parts) : 'nothing'
}

/**
 * Where a dependent opens. The server's `url_hint` wins when it is an app path
 * (it knows, say, which event type a field belongs to); otherwise the route is
 * built from the kind. A field with no hint, or an unknown kind, stays unlinked.
 */
export function dependencyHref(slug: string, edge: Pick<DependencyEdge, 'kind' | 'id' | 'url_hint'>): string | null {
  if (edge.url_hint && edge.url_hint.startsWith('/') && !edge.url_hint.startsWith('//')) {
    // Server-written and org-less: it opens in the active organization.
    return withActiveOrg(edge.url_hint)
  }
  const id = encodeURIComponent(edge.id)
  switch (edge.kind) {
    case 'event':
      return projectPath(currentOrgSlug(), slug, `/monitoring/event/${id}`)
    case 'event_type':
      return projectPath(currentOrgSlug(), slug, `/event-types/${id}`)
    case 'variable':
      return projectPath(currentOrgSlug(), slug, `/variables/${id}`)
    case 'metric':
      return getMetricMonitoringPath(slug, id)
    case 'fact_table':
      return projectPath(currentOrgSlug(), slug, `/metrics/fact-tables/${id}/edit`)
    case 'alert_rule':
      return projectPath(currentOrgSlug(), slug, `/monitors/${id}`)
    case 'relation':
      return projectPath(currentOrgSlug(), slug, '/relations')
    case 'scan_config':
      return projectPath(currentOrgSlug(), slug, `/scans/${id}`)
    case 'detection_override':
      return projectPath(currentOrgSlug(), slug, '/settings/monitoring')
    default:
      return null
  }
}

export const IMPACT_CHANGE_LABELS: Record<ImpactChangeType, string> = {
  delete: 'deleted',
  deprecate: 'deprecated',
  rename: 'renamed',
  change: 'changed',
}

/** "deleted", "renamed"; an unknown change type reads as itself. */
export function impactChangeLabel(change: string): string {
  return Object.hasOwn(IMPACT_CHANGE_LABELS, change)
    ? IMPACT_CHANGE_LABELS[change as ImpactChangeType]
    : change
}

/** A change set's cache identity: order-insensitive, so the same selection
 * picked in a different order shares one request. */
export function impactChangesId(changes: readonly ImpactChange[]): string {
  return JSON.stringify(
    [...changes]
      .map((c) => [c.kind, c.id, c.change] as const)
      .sort((a, b) => a.join(':').localeCompare(b.join(':'))),
  )
}

/** More than this many ids go to POST /impact in one confirm: a "select all
 * 2,400" sweep would ask the server to resolve every one of them before the
 * dialog could say anything. Above it the dialog checks the first ones and
 * says so. */
export const IMPACT_MAX_CHANGES = 200
