import { describe, expect, it } from 'vitest'
import { isValidSlug, slugify } from './slug'

describe('slugify', () => {
  it('lowercases and hyphenates a plain name', () => {
    expect(slugify('My Project')).toBe('my-project')
    expect(slugify('  Web -- App  2 ')).toBe('web-app-2')
  })

  it('folds diacritics instead of dropping the letters', () => {
    expect(slugify('Café Ölmotor')).toBe('cafe-olmotor')
  })

  it('falls back to project-<n> when nothing Latin survives', () => {
    expect(slugify('Аналитика')).toBe('project-1')
    expect(slugify('数据', ['beta'])).toBe('project-1')
  })

  it('picks the first project-<n> not already taken', () => {
    // Two projects left after a delete: numbering from the count gave
    // project-3, which is taken, and the create failed with a 409.
    expect(slugify('Аналитика', ['project-2', 'project-3'])).toBe('project-1')
    expect(slugify('Аналитика', ['project-1', 'project-2', 'project-4'])).toBe('project-3')
  })

  it('leaves an empty name empty', () => {
    expect(slugify('')).toBe('')
    expect(slugify('   ')).toBe('')
  })

  it('always produces a slug the pattern accepts', () => {
    for (const name of ['My Project', 'Café', 'Аналитика', '---x---', 'A_B.C']) {
      expect(isValidSlug(slugify(name))).toBe(true)
    }
  })
})

describe('isValidSlug', () => {
  it('matches the backend pattern', () => {
    expect(isValidSlug('mobile-app')).toBe(true)
    expect(isValidSlug('a1')).toBe(true)
    expect(isValidSlug('Mobile')).toBe(false)
    expect(isValidSlug('a--b')).toBe(false)
    expect(isValidSlug('-a')).toBe(false)
    expect(isValidSlug('')).toBe(false)
  })
})
