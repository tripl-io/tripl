/**
 * Client-side source of truth for the password policy, kept in lockstep with the
 * backend enforcement in `tripl/schemas/auth.py` (`validate_password_strength`:
 * >= 12 characters, at least one number and one symbol). Every form that sets a
 * password — sign-up and reset (AuthPage), accepting an invitation (InvitePage)
 * — shows `PASSWORD_POLICY_HINT` and checks with `passwordPolicyError`, so what
 * the form says and refuses can never drift from what the server accepts.
 *
 * Client validation is a UX affordance only — the schema validator is the real
 * security boundary.
 */
export const PASSWORD_MIN_LENGTH = 12

export const PASSWORD_POLICY_HINT = 'At least 12 characters, with a number and symbol.'

// Unicode classes, as the server's `str.isdigit()` and `str.isalnum()` are: a
// symbol is anything that is not a letter, a number or whitespace. An ASCII
// `\d` or `\w` would disagree with the server on any non-Latin password.
const DIGIT = /\p{Nd}/u
const SYMBOL = /[^\p{L}\p{N}\s]/u

/**
 * What a new password still lacks, as one sentence, or `null` when the server
 * will take it. An empty field is the form's own "Required", not this.
 *
 * It names only what is missing — "Add a symbol." — where the form used to
 * check the length alone and leave the number and the symbol to a 422.
 */
export function passwordPolicyError(value: string): string | null {
  // Code points, as Python's `len()` counts them: an emoji is one character
  // there and two UTF-16 units in `value.length`.
  const short = [...value].length < PASSWORD_MIN_LENGTH
  const needs = [!DIGIT.test(value) && 'a number', !SYMBOL.test(value) && 'a symbol'].filter(
    (need): need is string => typeof need === 'string',
  )
  const needed = needs.join(' and ')
  if (short) {
    return needs.length > 0
      ? `Use at least ${PASSWORD_MIN_LENGTH} characters, with ${needed}.`
      : `Use at least ${PASSWORD_MIN_LENGTH} characters.`
  }
  return needs.length > 0 ? `Add ${needed}.` : null
}
