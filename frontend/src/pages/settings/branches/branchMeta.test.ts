import { describe, expect, it } from 'vitest'
import { branchNameProblem, suggestBranchName } from './branchMeta'

describe('branchNameProblem (PL-5)', () => {
  it('accepts ref-like names, ticket keys included', () => {
    expect(branchNameProblem('checkout/paywall-copy', [])).toBeNull()
    expect(branchNameProblem('PROJ-4770', [])).toBeNull()
    expect(branchNameProblem('feature_v2.1', [])).toBeNull()
  })

  it('leaves an empty name to the required check', () => {
    expect(branchNameProblem('   ', [])).toBeNull()
  })

  it('refuses spaces, punctuation and a leading separator', () => {
    expect(branchNameProblem('Bad name with spaces!!', [])).toBe('Branch names cannot contain spaces.')
    expect(branchNameProblem('bad!!', [])).toMatch(/only letters, numbers/)
    expect(branchNameProblem('-lead', [])).toMatch(/Start with a letter or number/)
  })

  it('refuses a name longer than the switcher can show', () => {
    expect(branchNameProblem('a'.repeat(65), [])).toBe('Use at most 64 characters.')
  })

  it('refuses a name another branch already has, ignoring case', () => {
    expect(branchNameProblem('Checkout-V2', ['main', 'checkout-v2'])).toBe(
      'A branch with this name already exists.',
    )
  })
})

describe('suggestBranchName', () => {
  it('slugifies what was typed', () => {
    expect(suggestBranchName('Bad name with spaces!!')).toBe('bad-name-with-spaces')
  })

  it('keeps a ticket key in upper case', () => {
    expect(suggestBranchName('PROJ-4770 fix copy')).toBe('PROJ-4770-fix-copy')
  })

  it('offers nothing when the name is already usable or nothing is left', () => {
    expect(suggestBranchName('checkout-v2')).toBeNull()
    expect(suggestBranchName('!!!')).toBeNull()
  })
})
