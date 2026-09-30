import { QueryClient } from '@tanstack/react-query'
import { describe, expect, it, vi } from 'vitest'
import {
  DEFAULT_REQUIRED_PRESENCE,
  formatPresence,
  formatThreshold,
  invalidatePropertyEntries,
} from './propertyEntries'
import { branchEventsKey, branchPropertyEntriesKey, branchVariableOverridesKey } from './queryKeys'

describe('property list formatting (F23)', () => {
  it('formats a presence rate, with an em dash when none was measured', () => {
    expect(formatPresence(null)).toBe('—')
    expect(formatPresence(undefined)).toBe('—')
    expect(formatPresence(0)).toBe('0%')
    expect(formatPresence(0.004)).toBe('<1%')
    expect(formatPresence(0.995)).toBe('>99%')
    expect(formatPresence(0.87)).toBe('87%')
    expect(formatPresence(1)).toBe('100%')
  })

  it('formats a threshold, falling back to the default', () => {
    expect(formatThreshold(null)).toBe(`${DEFAULT_REQUIRED_PRESENCE * 100}%`)
    expect(formatThreshold(0.8)).toBe('80%')
    expect(formatThreshold(0.925)).toBe('92.5%')
  })

  it('invalidates both sides of the list and the filtered events lists', () => {
    const qc = new QueryClient()
    const spy = vi.spyOn(qc, 'invalidateQueries')
    invalidatePropertyEntries(qc, 'demo', null)
    expect(spy.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      branchPropertyEntriesKey('demo', null),
      branchVariableOverridesKey('demo', null),
      branchEventsKey('demo', null),
    ])
  })
})
