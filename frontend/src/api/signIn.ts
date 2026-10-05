/**
 * Browser sign-in through an identity provider, the parts every provider shares:
 * Sign in with Google (Community) and an organization's single sign-on use the
 * same `next` rule and come back to `/auth?sso_error=<code>` with the same codes.
 */

/**
 * Only a same-origin relative path is a place to come back to: it starts with
 * one `/`, never `//` (another host) or `/\` (which some browsers read as one).
 * The server checks it again; this keeps the SPA from asking for a refusal.
 */
export function safeNextPath(next: string | null | undefined): string | null {
  if (!next || !next.startsWith('/') || next.startsWith('//') || next.startsWith('/\\')) return null
  return next
}

/** The address that begins signing in through an instance-wide provider. */
function instanceStartUrl(provider: 'google' | 'oidc', next?: string | null): string {
  const path = `/api/v1/auth/${provider}/start`
  const safe = safeNextPath(next)
  return safe && safe !== '/' ? `${path}?next=${encodeURIComponent(safe)}` : path
}

/** The address that begins Sign in with Google, the instance-wide client. */
export function googleStartUrl(next?: string | null): string {
  return instanceStartUrl('google', next)
}

/** The address that begins signing in through the instance's OpenID Connect provider. */
export function oidcStartUrl(next?: string | null): string {
  return instanceStartUrl('oidc', next)
}

/**
 * The codes the callback puts on `/auth?sso_error=`; never the provider's own
 * text. Exactly the constants of `backend/src/tripl/services/oidc/flow.py` and
 * `sso_login_service.py` (an organization's), plus Sign in with Google's
 * `signup_closed` (`google_login_service.py`).
 */
export type SignInErrorCode =
  | 'signup_closed'
  | 'sso_unavailable'
  | 'invalid_state'
  | 'idp_error'
  | 'idp_denied'
  | 'invalid_token'
  | 'email_missing'
  | 'email_not_verified'
  | 'email_domain_not_allowed'
  | 'membership_removed'
  | 'rate_limited'
  | 'sso_failed'
  | 'saml_invalid'
  | 'saml_signature_invalid'
  | 'saml_replay'
  | 'saml_unsolicited'
  | 'encrypted_assertion_unsupported'

const SIGN_IN_ERROR_MESSAGES: Record<SignInErrorCode, string> = {
  signup_closed:
    'No account here has that address, and this instance is not taking new sign-ups. Ask an administrator for an invitation.',
  sso_unavailable: 'Single sign-on is not turned on for this organization.',
  invalid_state:
    'That single sign-on attempt expired or was already used. Start signing in again.',
  idp_error:
    'Your identity provider did not complete the sign-in. Try again, or ask your administrator to check the single sign-on setup.',
  idp_denied: 'The sign-in was cancelled or refused at your identity provider.',
  invalid_token:
    'The sign-in answer from your identity provider could not be verified. Try again, or ask your administrator to check the single sign-on setup.',
  email_missing:
    'Your identity provider did not send an email address, so tripl cannot sign you in with it.',
  email_not_verified:
    'Your identity provider did not confirm your email address, so tripl cannot sign you in with it.',
  email_domain_not_allowed:
    "Your email address's domain is not one this organization signs in with single sign-on.",
  membership_removed:
    'You were removed from this organization. Ask an administrator to invite you again.',
  rate_limited: 'Too many sign-in attempts. Wait a minute, then try again.',
  sso_failed: 'Single sign-on could not finish. Try again.',
  saml_invalid:
    'The sign-in answer from your identity provider was not valid for this organization. Try again, or ask your administrator to check the single sign-on setup.',
  saml_signature_invalid:
    'The sign-in answer from your identity provider was not signed with a certificate this organization trusts. Ask your administrator to check the single sign-on setup.',
  saml_replay: 'That sign-in answer was already used. Start signing in again.',
  saml_unsolicited:
    'Sign-ins started from your identity provider are not supported. Start from the tripl sign-in page with Sign in with SSO.',
  encrypted_assertion_unsupported:
    'Your identity provider encrypted its sign-in answer, which tripl does not support. Ask your administrator to turn assertion encryption off.',
}

/** Words for a `sso_error` code; an unknown code still says the sign-in failed. */
export function signInErrorMessage(code: string): string {
  return (
    SIGN_IN_ERROR_MESSAGES[code as SignInErrorCode] ??
    'Single sign-on did not complete. Try again, or sign in another way.'
  )
}
