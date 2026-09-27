import { describe, expect, it } from 'vitest'
import type { DependencyEdge } from '@/types'
import {
  dedupeEdges,
  dependencyHref,
  groupEdgesByKind,
  impactChangesId,
  summarizeEdges,
} from './dependencies'

function edge(overrides: Partial<DependencyEdge>): DependencyEdge {
  return {
    kind: 'metric',
    id: 'm-1',
    name: 'Checkout rate',
    relation: 'metric uses event in its composition',
    certainty: 'direct',
    url_hint: null,
    ...overrides,
  }
}

describe('dependency helpers (#257)', () => {
  it('keeps one row per entity, a direct edge over a possible one', () => {
    const rows = dedupeEdges([
      edge({ certainty: 'possible', relation: 'sql mentions column' }),
      edge({}),
      edge({ id: 'm-2', name: 'Other' }),
    ])
    expect(rows).toHaveLength(2)
    expect(rows.find((r) => r.id === 'm-1')?.certainty).toBe('direct')
  })

  it('groups by kind, metrics first, direct before possible', () => {
    const groups = groupEdgesByKind([
      edge({ kind: 'relation', id: 'r-1', name: 'links' }),
      edge({ id: 'm-2', name: 'Alpha', certainty: 'possible' }),
      edge({ kind: 'alert_rule', id: 'a-1', name: 'Drop' }),
      edge({ id: 'm-1', name: 'Zeta' }),
    ])
    expect(groups.map((g) => g.kind)).toEqual(['metric', 'alert_rule', 'relation'])
    expect(groups[0]?.edges.map((e) => e.name)).toEqual(['Zeta', 'Alpha'])
  })

  it('drops kinds the client does not know', () => {
    const unknown = edge({ kind: 'dashboard' as DependencyEdge['kind'] })
    expect(groupEdgesByKind([unknown])).toEqual([])
  })

  it('says "2 metrics and 1 alert rule", counting possible matches apart', () => {
    expect(
      summarizeEdges([
        edge({ id: 'm-1' }),
        edge({ id: 'm-2' }),
        edge({ kind: 'alert_rule', id: 'a-1' }),
      ]),
    ).toBe('2 metrics and 1 alert rule')
    expect(
      summarizeEdges([edge({ id: 'm-1' }), edge({ id: 'm-2', certainty: 'possible' })]),
    ).toBe('1 metric and 1 possible metric')
    expect(summarizeEdges([])).toBe('nothing')
  })

  it('links by url_hint when it is an app path, else by kind', () => {
    expect(dependencyHref('demo', edge({ url_hint: '/p/demo/event-types/et-1' }))).toBe(
      '/p/demo/event-types/et-1',
    )
    // An absolute or protocol-relative hint is never followed off the app.
    expect(dependencyHref('demo', edge({ url_hint: '//evil.example/x' }))).toBe(
      '/p/demo/monitoring/metric/m-1',
    )
    expect(dependencyHref('demo', edge({ kind: 'alert_rule', id: 'a-1' }))).toBe('/p/demo/monitors/a-1')
    expect(dependencyHref('demo', edge({ kind: 'field', id: 'f-1' }))).toBeNull()
  })

  it('identifies a change set independently of its order', () => {
    const a = { kind: 'event' as const, id: 'e-1', change: 'delete' as const }
    const b = { kind: 'event' as const, id: 'e-2', change: 'delete' as const }
    expect(impactChangesId([a, b])).toBe(impactChangesId([b, a]))
  })
})
