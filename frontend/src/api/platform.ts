import { api } from './client'
import type { Role } from '../types'

/**
 * The platform console API (F20, backend/src/tripl/api/v1/platform_console.py).
 *
 * Every path starts with `/platform`, which the client never rewrites into an
 * organization: these routes are the operator's, across organizations. They
 * answer a platform admin's browser session only — an API key is refused.
 *
 * Hand-typed against the design contract until `api.gen.ts` is regenerated.
 * The console reads metadata and counts only; it never carries project content.
 */

/** `deleting` orgs appear in the console too, but cannot be suspended. */
export type PlatformOrgStatus = 'active' | 'suspended' | 'deleting'

/** One organization as the console lists it. */
export interface PlatformOrg {
  id: string
  slug: string
  name: string
  status: PlatformOrgStatus
  created_at: string
  suspended_at: string | null
  suspended_reason: string | null
  member_count: number
  project_count: number
  owner_emails: string[]
}

export interface PlatformOrgList {
  items: PlatformOrg[]
  total: number
}

export interface PlatformOrgMember {
  email: string
  name: string | null
  role: Role
}

export interface PlatformOrgProject {
  slug: string
  name: string
  created_at: string
}

/** One organization with its members and projects — metadata only. */
export interface PlatformOrgDetail extends PlatformOrg {
  members: PlatformOrgMember[]
  projects: PlatformOrgProject[]
}

export interface PlatformUser {
  id: string
  email: string
  name: string | null
  is_platform_admin: boolean
  email_verified: boolean
  created_at: string
  org_count: number
}

export interface PlatformUserList {
  items: PlatformUser[]
  total: number
}

/** A read-only step-in: the caller sees the organization as a viewer until `expires_at`. */
export interface StepIn {
  id: string
  org_slug: string
  expires_at: string
}

export interface PlatformOrgListParams {
  q?: string
  status?: PlatformOrgStatus
  limit?: number
  offset?: number
}

export interface PlatformUserListParams {
  q?: string
  limit?: number
  offset?: number
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const sp = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === '') continue
    sp.set(key, String(value))
  }
  const qs = sp.toString()
  return qs ? `?${qs}` : ''
}

function orgPath(slug: string): string {
  return `/platform/orgs/${encodeURIComponent(slug)}`
}

export const platformApi = {
  listOrgs: (params: PlatformOrgListParams = {}, signal?: AbortSignal) =>
    api.get<PlatformOrgList>(`/platform/orgs${query({ ...params })}`, signal),
  getOrg: (slug: string, signal?: AbortSignal) => api.get<PlatformOrgDetail>(orgPath(slug), signal),
  /** 409 for a deleting organization or the self-hosted default one. */
  suspendOrg: (slug: string, reason: string) =>
    api.post<PlatformOrg>(`${orgPath(slug)}/suspend`, { reason }),
  unsuspendOrg: (slug: string) => api.post<PlatformOrg>(`${orgPath(slug)}/unsuspend`),

  listUsers: (params: PlatformUserListParams = {}, signal?: AbortSignal) =>
    api.get<PlatformUserList>(`/platform/users${query({ ...params })}`, signal),
  /** 409 when revoking the last platform admin, or yourself. */
  setPlatformAdmin: (userId: string, grant: boolean) =>
    api.post<PlatformUser>(`/platform/users/${encodeURIComponent(userId)}/platform-admin`, { grant }),

  /** Start a read-only step-in; `ttl_minutes` is 5..240 (the server's default is 60). */
  stepIn: (slug: string, data: { reason: string; ttl_minutes: number }) =>
    api.post<StepIn>(`${orgPath(slug)}/step-in`, data),
  endStepIn: (id: string) => api.post<void>(`/platform/step-ins/${encodeURIComponent(id)}/end`),
  /** The caller's own step-ins; `active` keeps the unexpired, un-ended ones. */
  listStepIns: (active = true, signal?: AbortSignal) =>
    api.get<StepIn[]>(`/platform/step-ins${query({ active })}`, signal),
}

