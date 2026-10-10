import { describe, expect, it } from 'vitest'
import { eventAttributeLabel } from '@/lib/eventAttributes'
import { ACTION_LABEL, branchNameProblem, stateKeyLabel, suggestBranchName } from './branchMeta'

describe('stateKeyLabel', () => {
  it('names an event key the way the "as merged" sheet and the event page do', () => {
    for (const key of ['superseded_by', 'source_name', 'sunset_at', 'reviewed', 'meta_values']) {
      expect(stateKeyLabel(key)).toBe(eventAttributeLabel(key))
    }
    expect(stateKeyLabel('superseded_by')).toBe('Replaced by')
    expect(stateKeyLabel('source_name')).toBe('Scan identity')
    expect(stateKeyLabel('reviewed')).toBe('Verified')
  })

  it('keeps the words of the keys only other entities carry', () => {
    expect(stateKeyLabel('variable_type')).toBe('Property type')
    expect(stateKeyLabel('json_schema')).toBe('JSON Schema')
  })

  it('reads a key nothing names as words', () => {
    expect(stateKeyLabel('display_name')).toBe('Display name')
  })
})

describe('ACTION_LABEL', () => {
  it('names the branch on the transitions a bare verb made read as closing the panel', () => {
    expect(ACTION_LABEL.close).toBe('Close branch')
    expect(ACTION_LABEL.reopen).toBe('Reopen branch')
  })
})

describe('branchNameProblem', () => {
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
