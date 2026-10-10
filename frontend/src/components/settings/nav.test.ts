import { describe, expect, it } from 'vitest'
import { sectionIsWide } from './nav'

describe('sectionIsWide', () => {
  it('widens the sections built around a table', () => {
    expect(sectionIsWide('data-sources')).toBe(true)
    expect(sectionIsWide('api-keys')).toBe(true)
  })

  it('keeps the people sections narrow: they are rows and one-field forms', () => {
    for (const path of ['project/members', 'members', 'organization/groups', 'invitations']) {
      expect(sectionIsWide(path)).toBe(false)
    }
  })
})
