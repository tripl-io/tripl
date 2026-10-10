import { api } from './client'
import { getAllPages } from './allPages'
import { ROSTER_PAGE_LIMIT } from './users'
import type { components } from '../types/api.gen'
import type { DefaultProjectRole, Project, Role, UserListItem } from '../types'

/**
 * The organization management API (F20 PR6, backend/src/tripl/api/v1/orgs.py).
 *
 * Every path here starts with `/orgs`, which the client never rewrites: these
 * routes name their organization themselves, whichever one the app is acting
 * in. Invitations stay on `/users/invitations` (api/invitations.ts), which the
 * client sends to the active organization.
 */

/**
 * One organization, with the caller's role in it and its default access to
 * its projects (`default_project_role`, F20 PR15). The field is spelled out
 * here until the generated types are regenerated from the backend.
 */
export type Org = components['schemas']['OrgResponse'] & {
  default_project_role: DefaultProjectRole
}
/**
 * `PATCH /orgs/{org}`: any subset of the name and the default project access
 * (an owner or admin). The slug is immutable, so it is not a field.
 */
export interface OrgUpdate {
  name?: string
  default_project_role?: DefaultProjectRole
}
/** What removing a member took away with the membership. */
export type OrgMemberRemoved = components['schemas']['OrgMemberRemoved']
/**
 * A member's single-use password reset link, to hand over by hand: the server
 * returns it this once. `reset_path` goes on the app's own address.
 */
export type MemberPasswordResetLink = components['schemas']['MemberPasswordResetLink']

function orgBase(org: string): string {
  return `/orgs/${encodeURIComponent(org)}`
}

export const orgsApi = {
  /** The caller's organizations, with their role in each. */
  list: () => api.get<Org[]>('/orgs'),
  /** A platform admin only; the creator becomes the owner. The slug is permanent. */
  create: (data: { name: string; slug: string }) => api.post<Org>('/orgs', data),
  get: (org: string) => api.get<Org>(orgBase(org)),
  /** Rename, or change the default project access. The slug cannot change. */
  update: (org: string, data: OrgUpdate) => api.patch<Org>(orgBase(org), data),
  /**
   * Start deleting the organization (202; a background job purges it). The
   * body repeats the slug as a typed confirmation. Refused for the default
   * organization.
   */
  delete: (org: string, confirmSlug: string) =>
    api.del<Org>(orgBase(org), { confirm_slug: confirmSlug }),

  /** Every member of the organization, oldest account first. */
  members: (org: string) =>
    getAllPages<UserListItem>(`${orgBase(org)}/members`, ROSTER_PAGE_LIMIT),
  updateMemberRole: (org: string, userId: string, role: Role) =>
    api.patch<UserListItem>(`${orgBase(org)}/members/${userId}`, { role }),
  removeMember: (org: string, userId: string) =>
    api.del<OrgMemberRemoved>(`${orgBase(org)}/members/${userId}`),
  /** Make another member an owner; the caller steps down to admin. */
  transferOwnership: (org: string, userId: string) =>
    api.post<UserListItem>(`${orgBase(org)}/transfer-ownership`, { user_id: userId }),
  /**
   * A password reset link for another member (an owner or admin), for an
   * instance that cannot email one. It replaces any earlier link of theirs.
   */
  createPasswordResetLink: (org: string, userId: string) =>
    api.post<MemberPasswordResetLink>(`${orgBase(org)}/members/${userId}/password-reset-link`),

  /**
   * One organization's projects, whichever organization the app acts in. The
   * `/p/{slug}` redirect asks each organization whether it holds the slug.
   */
  projects: (org: string) => api.get<Project[]>(`${orgBase(org)}/projects`),
}
