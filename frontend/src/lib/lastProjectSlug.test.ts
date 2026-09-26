import { afterEach, describe, expect, it } from 'vitest'

import { LAST_PROJECT_SLUG_KEY, forgetLastProjectSlug } from './lastProjectSlug'

describe('forgetLastProjectSlug', () => {
  afterEach(() => {
    localStorage.clear()
  })

  it('forgets the remembered project when it is the one that 404ed', () => {
    localStorage.setItem(LAST_PROJECT_SLUG_KEY, 'gone')
    forgetLastProjectSlug('gone')
    expect(localStorage.getItem(LAST_PROJECT_SLUG_KEY)).toBeNull()
  })

  it('keeps a different remembered project', () => {
    localStorage.setItem(LAST_PROJECT_SLUG_KEY, 'kept')
    forgetLastProjectSlug('gone')
    expect(localStorage.getItem(LAST_PROJECT_SLUG_KEY)).toBe('kept')
  })
})
