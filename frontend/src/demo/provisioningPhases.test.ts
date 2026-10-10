import { describe, expect, it } from 'vitest'
import { PROVISIONING_PHASES, nextPhaseIndex } from './provisioningPhases'

describe('provisioningPhases', () => {
  it('narrates the expected create phases in order', () => {
    expect(PROVISIONING_PHASES.map((phase) => phase.id)).toEqual([
      'project',
      'events',
      'metrics',
      'monitors',
      'finalizing',
    ])
  })

  it('advances one phase at a time', () => {
    expect(nextPhaseIndex(0)).toBe(1)
    expect(nextPhaseIndex(2)).toBe(3)
  })

  it('clamps at the final phase — never past the end', () => {
    const last = PROVISIONING_PHASES.length - 1
    expect(nextPhaseIndex(last)).toBe(last)
    expect(nextPhaseIndex(last + 5)).toBe(last)
  })
})
