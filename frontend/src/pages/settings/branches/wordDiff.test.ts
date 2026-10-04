import { describe, expect, it } from 'vitest'
import { wordDiff } from './wordDiff'

const changed = (segments: { text: string; changed: boolean }[]) =>
  segments.filter((s) => s.changed).map((s) => s.text)

describe('wordDiff', () => {
  it('marks only the words each side does not share', () => {
    const result = wordDiff(
      'Fired when the user completes a purchase on the web checkout',
      'Fired when the user completes a purchase on the mobile checkout page',
    )

    expect(result).not.toBeNull()
    expect(changed(result!.before)).toEqual(['web'])
    expect(changed(result!.after)).toEqual(['mobile', ' page'])
    // Each side still reads as the whole string.
    expect(result!.before.map((s) => s.text).join('')).toBe(
      'Fired when the user completes a purchase on the web checkout',
    )
  })

  it('merges both sides into one paragraph in reading order', () => {
    const result = wordDiff(
      'Fired when the user completes a purchase on the web checkout',
      'Fired when the user completes a purchase on the mobile checkout page',
    )

    expect(result!.inline.filter((s) => s.kind !== 'same')).toEqual([
      { text: 'web', kind: 'removed' },
      { text: 'mobile', kind: 'added' },
      { text: ' page', kind: 'added' },
    ])
    expect(result!.inline.filter((s) => s.kind !== 'added').map((s) => s.text).join('')).toBe(
      'Fired when the user completes a purchase on the web checkout',
    )
  })

  it('leaves short values alone', () => {
    expect(wordDiff('USD', 'EUR')).toBeNull()
  })

  it('does not mark everything when the two share no word', () => {
    expect(
      wordDiff(
        'alpha beta gamma delta epsilon zeta eta theta',
        'one two three four five six seven eight nine',
      ),
    ).toBeNull()
  })
})
