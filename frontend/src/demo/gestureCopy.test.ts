// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'
import { gestureCopy, isCoarsePointer } from './gestureCopy'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('gestureCopy', () => {
  it('leaves the words alone for a mouse', () => {
    expect(gestureCopy('Click Run now.', false)).toBe('Click Run now.')
    expect(gestureCopy('Then click here', false)).toBe('Then click here')
  })

  it('says tap on a touch screen', () => {
    expect(gestureCopy('Click here', true)).toBe('Tap here')
    expect(gestureCopy('Name it, then click here', true)).toBe('Name it, then tap here')
    expect(gestureCopy('Open its page first: click Take me there.', true)).toBe(
      'Open its page first: tap Take me there.',
    )
    expect(gestureCopy('Write a line. Click Comment.', true)).toBe('Write a line. Tap Comment.')
  })

  it('never renames a name that holds the word', () => {
    // The seeded "Buy Button Click" event, and an event key.
    expect(gestureCopy('Click the Buy Button Click row under Changes.', true)).toBe(
      'Tap the Buy Button Click row under Changes.',
    )
    expect(gestureCopy('Open button_click', true)).toBe('Open button_click')
  })

  it('asks the browser when not told', () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn((query: string) => ({ matches: query === '(pointer: coarse)', media: query })),
    )

    expect(gestureCopy('Click here')).toBe('Tap here')
  })
})

describe('isCoarsePointer', () => {
  it('is true for a touch screen', () => {
    vi.stubGlobal(
      'matchMedia',
      vi.fn((query: string) => ({ matches: query === '(pointer: coarse)', media: query })),
    )

    expect(isCoarsePointer()).toBe(true)
  })

  it('is false for a mouse, and where there is no matchMedia', () => {
    vi.stubGlobal('matchMedia', vi.fn((query: string) => ({ matches: false, media: query })))
    expect(isCoarsePointer()).toBe(false)

    vi.stubGlobal('matchMedia', undefined)
    expect(isCoarsePointer()).toBe(false)
  })
})
