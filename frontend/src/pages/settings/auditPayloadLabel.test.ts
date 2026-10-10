import { describe, expect, it } from 'vitest'
import { payloadKeyLabel } from './auditSentences'

describe('payloadKeyLabel', () => {
  it("reads a plan entity's keys as the plan's own pages name them", () => {
    expect(payloadKeyLabel('reviewed', 'event')).toBe('Verified')
    expect(payloadKeyLabel('variable_type', 'variable')).toBe('Property type')
  })

  it("keeps another entity's keys in its own words: a metric's reviewed flag is not 'Verified'", () => {
    // metric_definition.bulk_update files `reviewed`; metrics call that flag
    // "Reviewed".
    expect(payloadKeyLabel('reviewed', 'metric_definition')).toBe('Reviewed')
    expect(payloadKeyLabel('status', 'plan_branch')).toBe('Status')
    expect(payloadKeyLabel('default_project_role', 'organization')).toBe('Default project role')
  })
})
