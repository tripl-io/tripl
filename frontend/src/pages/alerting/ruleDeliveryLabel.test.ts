import { describe, expect, it } from 'vitest'

import { ruleDeliveryHealthLabel } from './ruleDeliveryLabel'

const NOW = Date.parse('2026-08-12T12:00:00Z')

function rule(overrides: Partial<Parameters<typeof ruleDeliveryHealthLabel>[0]> = {}) {
  return {
    total_deliveries: 115,
    incident_count: 57,
    last_delivery_at: '2026-08-12T09:00:00Z',
    last_delivery_status: 'sent' as const,
    ...overrides,
  }
}

describe('ruleDeliveryHealthLabel', () => {
  it('names the delivery before its time, so it cannot be read as "Last fired"', () => {
    // It read "last 3h ago · sent": a time with no noun, beside a raw status.
    expect(ruleDeliveryHealthLabel(rule(), NOW)).toBe('115 deliveries · 57 incidents · last sent 3h ago')
  })

  it('says a failed or queued last delivery in words', () => {
    expect(ruleDeliveryHealthLabel(rule({ last_delivery_status: 'failed' }), NOW)).toBe(
      '115 deliveries · 57 incidents · last delivery failed 3h ago',
    )
    expect(ruleDeliveryHealthLabel(rule({ last_delivery_status: 'pending' }), NOW)).toBe(
      '115 deliveries · 57 incidents · last delivery queued 3h ago',
    )
  })

  it('agrees with its counts in the singular', () => {
    expect(
      ruleDeliveryHealthLabel(rule({ total_deliveries: 1, incident_count: 1 }), NOW),
    ).toBe('1 delivery · 1 incident · last sent 3h ago')
  })

  it('says plainly when a rule has never delivered', () => {
    expect(
      ruleDeliveryHealthLabel(
        rule({ total_deliveries: 0, incident_count: 0, last_delivery_at: null, last_delivery_status: null }),
        NOW,
      ),
    ).toBe('Never delivered')
  })

  it('leaves the time off rather than printing "last never"', () => {
    expect(ruleDeliveryHealthLabel(rule({ last_delivery_at: null }), NOW)).toBe(
      '115 deliveries · 57 incidents',
    )
  })
})
