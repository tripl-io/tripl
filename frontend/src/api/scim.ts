import { api } from './client'

/**
 * SCIM 2.0 provisioning (F20): an organization's SCIM bearer tokens and its
 * admin-group mapping (backend/src/tripl/api/v1/org_scim.py). The SCIM protocol
 * itself lives outside `/api/v1`, at `/scim/v2/{org}`, and is called by the
 * identity provider with one of these tokens, never by the app.
 *
 * Hand-written rather than read from `api.gen.ts`: the surface is small and
 * owner-only. `/orgs/...` paths are never rewritten by the client: the
 * organization is named in the path.
 */

/** One SCIM token. The secret itself is never returned after creation. */
export interface ScimToken {
  id: string
  /** The first characters of the token (`tripl_scim_…`), to tell tokens apart. */
  prefix: string
  created_at: string
  /** Who created it, when the server says (`null` once that account is gone). */
  created_by_email?: string | null
  /** The last SCIM request made with it; `null` if never used. */
  last_used_at: string | null
  /** Set once revoked; a revoked token is refused. */
  revoked_at: string | null
}

/** A token just created: the only response that carries the secret. */
export interface ScimTokenCreated {
  id: string
  prefix: string
  /** The full bearer token. Shown once; the server keeps only its hash. */
  token: string
  created_at: string
}

/** The organization's SCIM settings. */
export interface ScimConfig {
  /** The SCIM base URL to give the identity provider (`{app}/scim/v2/{org}`). */
  base_url: string
  /** Members of this group are made organization admins; `null` for no mapping. */
  admin_group_id: string | null
  /** The mapped group's name, when the server includes it. */
  admin_group_name?: string | null
  /** How many unrevoked tokens the organization has, when the server includes it. */
  active_tokens?: number
}

function base(org: string): string {
  return `/orgs/${encodeURIComponent(org)}/scim`
}

export const scimApi = {
  listTokens: (org: string) => api.get<ScimToken[]>(`${base(org)}/tokens`),
  createToken: (org: string) => api.post<ScimTokenCreated>(`${base(org)}/tokens`, {}),
  revokeToken: (org: string, tokenId: string) =>
    api.del<void>(`${base(org)}/tokens/${encodeURIComponent(tokenId)}`),
  getConfig: (org: string) => api.get<ScimConfig>(`${base(org)}/config`),
  updateConfig: (org: string, data: { admin_group_id: string | null }) =>
    api.put<ScimConfig>(`${base(org)}/config`, data),
}

// Query keys live here, not in lib/queryKeys.ts (the entry chunk): only the
// lazy Provisioning page reads them. Rooted at THAT organization.
/** An organization's SCIM tokens. */
export const orgScimTokensKey = (org: string) => [org, 'orgScim', 'tokens'] as const
/** An organization's SCIM settings. */
export const orgScimConfigKey = (org: string) => [org, 'orgScim', 'config'] as const
