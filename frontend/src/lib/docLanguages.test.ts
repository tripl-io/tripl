// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest'
import { setCurrentOrgSlug } from './activeOrg'
import { languageName, ORIGINAL_LANG, storeDocLanguage, storedDocLanguage } from './docLanguages'

describe('docLanguages', () => {
  afterEach(() => window.localStorage.clear())

  it('names a code the way people read it, and falls back to the code', () => {
    expect(languageName('de')).toBe('German')
    expect(languageName(ORIGINAL_LANG)).toBe('Original')
    expect(languageName('zz-unknown-tag')).toBe('zz-unknown-tag')
  })

  it('remembers the chosen language per project', () => {
    expect(storedDocLanguage('demo')).toBeNull()
    storeDocLanguage('demo', 'de')
    expect(storedDocLanguage('demo')).toBe('de')
    expect(storedDocLanguage('other')).toBeNull()
  })

  it('keeps the choice inside the active organization', () => {
    setCurrentOrgSlug('acme')
    storeDocLanguage('demo', 'de')
    expect(window.localStorage.getItem('o:acme:tripl.docs.lang.demo')).toBe('de')
    setCurrentOrgSlug('other')
    expect(storedDocLanguage('demo')).toBeNull()
    setCurrentOrgSlug(null)
  })
})
