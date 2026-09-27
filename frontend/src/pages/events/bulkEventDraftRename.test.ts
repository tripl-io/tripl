import { describe, expect, it } from 'vitest'
import { replaceBulkLineName } from './bulkEventDraft'

describe('replaceBulkLineName (F12, #265)', () => {
  it('replaces a whole line that has no title', () => {
    expect(replaceBulkLineName('ScreenOpen\npaywall_view', 1, 'screen_open')).toBe(
      'screen_open\npaywall_view',
    )
  })

  it('keeps the title after the tab', () => {
    expect(replaceBulkLineName('a\nPaywallView\tPaywall shown', 2, 'paywall_view')).toBe(
      'a\npaywall_view\tPaywall shown',
    )
  })

  it('counts blank lines, as the preview numbers them', () => {
    expect(replaceBulkLineName('a\n\nb', 3, 'c')).toBe('a\n\nc')
  })

  it('leaves the paste alone for a line that is not there', () => {
    expect(replaceBulkLineName('a', 4, 'c')).toBe('a')
  })
})
