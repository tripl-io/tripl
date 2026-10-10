import { describe, expect, it } from 'vitest'
import type { DependencyEdge } from '@/types'
import { DEPENDENCY_KIND_LABELS, dependencyKindHeading, summarizeEdges } from './dependencies'

function variableEdge(id: string): DependencyEdge {
  return {
    kind: 'variable',
    id,
    name: `prop_${id}`,
    relation: 'event field value references variable',
    certainty: 'direct',
    url_hint: null,
  }
}

// A variable is a "property" everywhere the UI names it (the Properties
// catalog, its detail page, the property picker), so Used by and the impact
// notices say so too rather than the API's word.
describe('dependency labels for a variable', () => {
  it('heads the group "Properties"', () => {
    expect(dependencyKindHeading('variable')).toBe('Properties')
  })

  it('counts them as properties', () => {
    expect(summarizeEdges([variableEdge('v-1')])).toBe('1 property')
    expect(summarizeEdges([variableEdge('v-1'), variableEdge('v-2')])).toBe('2 properties')
  })

  it('never says "variable"', () => {
    expect(Object.values(DEPENDENCY_KIND_LABELS.variable).join(' ')).not.toMatch(/variable/)
  })
})
