import { describe, expect, it } from 'vitest'
import { PASSWORD_MIN_LENGTH, passwordPolicyError } from './passwordPolicy'

// The same rule as `validate_password_strength` in backend/src/tripl/schemas/auth.py:
// a form that agrees with it never sends a password the server refuses.
describe('passwordPolicyError', () => {
  it('accepts a password that meets the whole policy', () => {
    expect(passwordPolicyError('Password123!')).toBeNull()
  })

  it('names only what is missing', () => {
    expect(passwordPolicyError('correcthorsebattery')).toBe('Add a number and a symbol.')
    expect(passwordPolicyError('correcthorse1battery')).toBe('Add a symbol.')
    expect(passwordPolicyError('correct-horse-battery')).toBe('Add a number.')
  })

  it('leads with the length when the password is short', () => {
    expect(passwordPolicyError('short')).toBe(
      `Use at least ${PASSWORD_MIN_LENGTH} characters, with a number and a symbol.`,
    )
    expect(passwordPolicyError('sh0rt!')).toBe(`Use at least ${PASSWORD_MIN_LENGTH} characters.`)
  })

  it('counts characters as the server does, not UTF-16 units', () => {
    // Five emoji, a digit and a symbol: twelve UTF-16 units, seven characters.
    expect(passwordPolicyError('😀😀😀😀😀1!')).toBe(
      `Use at least ${PASSWORD_MIN_LENGTH} characters.`,
    )
  })

  it('takes a non-Latin password the server takes', () => {
    // Python's isdigit()/isalnum() are Unicode-aware; an ASCII \d or \w is not.
    expect(passwordPolicyError('пароль-٣-пароль')).toBeNull()
  })

  it('does not count whitespace as the symbol', () => {
    expect(passwordPolicyError('correct horse 1')).toBe('Add a symbol.')
  })
})
