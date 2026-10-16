import { describe, expect, it } from 'vitest'
import {
  DEMO_PROVISION_ESTIMATE,
  DEMO_PROVISION_TIMEOUT_LABEL,
  PROVISIONING_PHASES,
  nextPhaseIndex,
  phaseIndexForStage,
  phaseLabel,
  provisioningProgressOf,
} from './provisioningPhases'

describe('provisioningPhases', () => {
  it('names the phases a demo is built in, in order', () => {
    expect(PROVISIONING_PHASES.map((phase) => phase.id)).toEqual(['plan', 'scan', 'history', 'signals'])
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

  it('reads the stage the server reports', () => {
    expect(phaseIndexForStage('plan')).toBe(0)
    expect(phaseIndexForStage('scan')).toBe(1)
    expect(phaseIndexForStage('history')).toBe(2)
    expect(phaseIndexForStage('signals')).toBe(3)
    // A shell waiting for a worker has written nothing yet.
    expect(phaseIndexForStage('init')).toBe(0)
  })

  it('knows no stage it cannot place, so the estimate takes over', () => {
    expect(phaseIndexForStage(null)).toBeNull()
    expect(phaseIndexForStage(undefined)).toBeNull()
    expect(phaseIndexForStage('')).toBeNull()
    expect(phaseIndexForStage('cancel_requested')).toBeNull()
    expect(phaseIndexForStage('something-newer')).toBeNull()
  })

  it('reads the history percentage loosely, clamped to 0..100', () => {
    expect(provisioningProgressOf({ generation_stage: 'history', generation_progress: 42 } as never)).toEqual({
      stage: 'history',
      percent: 42,
    })
    expect(provisioningProgressOf({ generation_stage: 'history', generation_progress: 140 } as never).percent).toBe(100)
    expect(provisioningProgressOf({ generation_stage: 'history', generation_progress: -3 } as never).percent).toBe(0)
    // An older server sends no percentage; a garbled one is no percentage.
    expect(provisioningProgressOf({ generation_stage: 'scan' })).toEqual({ stage: 'scan', percent: null })
    expect(provisioningProgressOf({ generation_stage: 'history', generation_progress: 'half' } as never).percent).toBeNull()
  })

  it('labels the active phase with its percentage', () => {
    expect(phaseLabel(2, 42)).toBe('Collecting history · 42%')
    expect(phaseLabel(2)).toBe('Collecting history')
  })

  it('states the wait in words', () => {
    expect(DEMO_PROVISION_ESTIMATE).toBe('about a minute')
    expect(DEMO_PROVISION_TIMEOUT_LABEL).toBe('5 minutes')
  })
})
