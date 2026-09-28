/**
 * An ORGANIZATION role (F20 PR4): what `/auth/me`, the users API and
 * invitations speak. An owner and an admin hold every project of their
 * organization; a member holds exactly their project rows (`editor` /
 * `viewer`, see `ProjectMemberRole`). The instance-era owner/editor/viewer
 * vocabulary is gone; the API refuses it with 422.
 */
export type Role = 'owner' | 'admin' | 'member'

// A role's pill tone lives in components/settings/role-chip.tsx (ST-16); this
// list is the order and the words.
export const ROLE_OPTIONS: { value: Role; label: string }[] = [
  { value: 'owner', label: 'Owner' },
  { value: 'admin', label: 'Admin' },
  { value: 'member', label: 'Member' },
]

/** One organization the signed-in user belongs to, with their role there. */
export interface OrgMembership {
  slug: string
  name: string
  role: Role
}

export interface AuthUser {
  id: string
  email: string
  name: string | null
  /**
   * The role in the organization this session acts in; `null` when the user
   * belongs to none that applies.
   */
  role: Role | null
  /**
   * The operator flag: the instance-wide operator settings (security,
   * observability, system, server paths). It grants nothing inside an
   * organization.
   */
  is_platform_admin: boolean
  /**
   * Whether the account has confirmed its email address. Enforced only in
   * hosted mode (`GET /auth/status` → `email_verification_required`), where an
   * unverified session sees the "check your inbox" screen instead of the app.
   * Optional so hand-built fixtures read as verified; only a definite `false`
   * counts as unverified.
   */
  email_verified?: boolean
  /** Every organization membership. */
  orgs: OrgMembership[]
  /**
   * The organization this request acts in, when one is bound: an API key's
   * own. `null` for a browser session on `/auth/me`.
   */
  org?: string | null
  /** `read` / `write` for an API key; `null` for a browser session. */
  api_key_scope?: ApiKeyScope | null
  created_at: string
  updated_at: string
}

export interface UserListItem {
  id: string
  email: string
  name: string | null
  role: Role
  created_at: string
}

export type ApiKeyScope = 'read' | 'write'

export interface ApiKey {
  id: string
  name: string
  key_prefix: string
  scope: ApiKeyScope
  project_id: string | null
  expires_at: string | null
  revoked_at: string | null
  last_used_at: string | null
  created_at: string
}

export interface ApiKeyWithToken extends ApiKey {
  token: string
}
