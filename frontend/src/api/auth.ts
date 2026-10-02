import { api } from './client'
import type { AuthUser } from '@/types'

/** Neutral response to a password-reset request. `email_configured` is an
 *  instance-wide flag (never per-account), so the UI can show the right copy
 *  without leaking whether the submitted address is registered. */
export interface PasswordResetRequestResponse {
  message: string
  email_configured: boolean
}

export interface PasswordResetConfirmResponse {
  message: string
}

/** Unauthenticated instance probe for the auth screen. Both flags are
 *  instance-wide (never per-account): `has_users` gates the "first account
 *  becomes owner" note, `registration_enabled` says whether POST /auth/register
 *  would be accepted right now, so a closed instance can hide the sign-up form
 *  instead of letting a visitor discover the policy from a 403. */
export interface AuthStatusResponse {
  has_users: boolean
  registration_enabled: boolean
  /** Whether the instance can send mail, so the forgot-password form can say
   *  up front that no link will come (ST-24). Always sent; optional so probes
   *  mocked before it still type, and only a definite `false` changes the UI. */
  email_configured?: boolean
  /** `hosted`: public sign-up creates an organization, and an account must
   *  verify its address before it can use the app. Optional so probes mocked
   *  before F20's hosted mode still type; absent reads as `self_hosted`. */
  deployment_mode?: DeploymentMode
  /** True exactly in hosted mode: an unverified session is refused everywhere
   *  but `/auth/*`, so the app shows the "check your inbox" screen instead. */
  email_verification_required?: boolean
  /** The operator configured a Google client: the page offers "Continue with
   *  Google" (tripl-sav5.2). */
  google_sign_in?: boolean
  /** A public demo: no password sign-ups (Google only), and the app refuses
   *  whatever would reach outside the instance. */
  public_demo?: boolean
}

export type DeploymentMode = 'self_hosted' | 'hosted'

/** Sign-up. `org_name` / `org_slug` are required in hosted mode (the new
 *  account creates and owns that organization) and ignored when self-hosted. */
export interface RegisterRequest {
  email: string
  password: string
  name?: string
  org_name?: string
  org_slug?: string
}

export const authApi = {
  me: () => api.get<AuthUser>('/auth/me'),
  status: () => api.get<AuthStatusResponse>('/auth/status'),
  login: (data: { email: string; password: string }) =>
    api.post<AuthUser>('/auth/login', data),
  register: (data: RegisterRequest) =>
    api.post<AuthUser>('/auth/register', data),
  logout: () => api.post<void>('/auth/logout'),
  // Self-service password reset. `request` always resolves 200 with a neutral
  // message (no user enumeration); `confirm` redeems the emailed token.
  requestPasswordReset: (data: { email: string }) =>
    api.post<PasswordResetRequestResponse>('/auth/password-reset/request', data),
  confirmPasswordReset: (data: { token: string; new_password: string }) =>
    api.post<PasswordResetConfirmResponse>('/auth/password-reset/confirm', data),
  // Email verification (F20). `request` mails a fresh link to the signed-in
  // account (204; 503 when the instance cannot send mail); `confirm` redeems
  // the emailed token without a session (204; one neutral 400 for an unknown,
  // expired or used token).
  verifyEmailRequest: () => api.post<void>('/auth/verify-email/request'),
  verifyEmailConfirm: (data: { token: string }) =>
    api.post<void>('/auth/verify-email/confirm', data),
}
