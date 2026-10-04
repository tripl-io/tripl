import { describe, expect, it } from 'vitest'
import { UNNAMED_EVENT_LABEL, eventNameLabel } from './eventName'

describe('eventNameLabel', () => {
  it('replaces a name that would paint nothing', () => {
    // The production row is the first case; the others are the
    // shapes the same defect arrives in on a nullable API field.
    expect(eventNameLabel('')).toBe(UNNAMED_EVENT_LABEL)
    expect(eventNameLabel('   ')).toBe(UNNAMED_EVENT_LABEL)
    expect(eventNameLabel(null)).toBe(UNNAMED_EVENT_LABEL)
    expect(eventNameLabel(undefined)).toBe(UNNAMED_EVENT_LABEL)
  })

  it('leaves a segment-shaped name alone, because it is a real identity', () => {
    // Deliberately narrower than "looks broken": these names have a click
    // target and an accessible name already, and `EventName` paints each empty
    // piece as ∅. Replacing them would hide a real event behind a placeholder.
    expect(eventNameLabel('checkout')).toBe('checkout')
    expect(eventNameLabel('::')).toBe('::')
    expect(eventNameLabel(':services')).toBe(':services')
    expect(eventNameLabel('onboarding:start:')).toBe('onboarding:start:')
  })
})
