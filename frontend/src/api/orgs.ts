import { api } from './client'
import type { components } from '../types/api.gen'
import type { Project, Role, UserListItem } from '../types'

/**
 * The organization management API (F20 PR6, backend/src/tripl/api/v1/orgs.py).
 *
 * Every path here starts with `/orgs`, which the client never rewrites: these
 * routes name their organization themselves, whichever one the app is acting
 * in. Invitations stay on `/users/invitations` (api/invitations.ts), which the
 * client sends to the active organization.
 */

/** One organization, with the caller's role in it. */
export type Org = components['schemas']['OrgResponse']
/** What removing a member took away with the membership. */
export type OrgMemberRemoved = components['schemas']['OrgMemberRemoved']

function orgBase(org: string): string {
  return `/orgs/${encodeURIComponent(org)}`
}

export const orgsApi = {
  /** The caller's organizations, with their role in each. */
  list: () => api.get<Org[]>('/orgs'),
  /** A platform admin only; the creator becomes the owner. The slug is permanent. */
  create: (data: { name: string; slug: string }) => api.post<Org>('/orgs', data),
  get: (org: string) => api.get<Org>(orgBase(org)),
  /** Rename. Only the name: the slug cannot change. */
  rename: (org: string, name: string) => api.patch<Org>(orgBase(org), { name }),
  /**
   * Start deleting the organization (202; a background job purges it). The
   * body repeats the slug as a typed confirmation. Refused for the default
   * organization.
   */
  delete: (org: string, confirmSlug: string) =>
    api.del<Org>(orgBase(org), { confirm_slug: confirmSlug }),

  members: (org: string) => api.get<UserListItem[]>(`${orgBase(org)}/members`),
  updateMemberRole: (org: string, userId: string, role: Role) =>
    api.patch<UserListItem>(`${orgBase(org)}/members/${userId}`, { role }),
  removeMember: (org: string, userId: string) =>
    api.del<OrgMemberRemoved>(`${orgBase(org)}/members/${userId}`),
  /** Make another member an owner; the caller steps down to admin. */
  transferOwnership: (org: string, userId: string) =>
    api.post<UserListItem>(`${orgBase(org)}/transfer-ownership`, { user_id: userId }),

  /**
   * One organization's projects, whichever organization the app acts in. The
   * `/p/{slug}` redirect asks each organization whether it holds the slug.
   */
  projects: (org: string) => api.get<Project[]>(`${orgBase(org)}/projects`),
}
