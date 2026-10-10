import { describe, expect, it } from 'vitest'
import { templateTokenTitle } from './utils'

describe('templateTokenTitle', () => {
  it("names the property and lists the values it documents", () => {
    expect(templateTokenTitle('${platform}', { allowed_values: ['ios', 'android', 'web'] })).toBe(
      'Property ${platform}: filled in from observed values. Documented values: ios, android, web',
    )
  })

  it('says what the token stands for when nothing is documented or the property is unknown', () => {
    const meaning = 'Property ${platform}: filled in from observed values'
    expect(templateTokenTitle('${platform}', { allowed_values: [] })).toBe(meaning)
    expect(templateTokenTitle('${platform}', null)).toBe(meaning)
    expect(templateTokenTitle('${platform}', undefined)).toBe(meaning)
  })
})
