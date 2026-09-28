import { api } from './client'
import type { components } from '../types/api.gen'

/**
 * Organization groups (F20, backend/src/tripl/api/v1/org_groups.py): named sets
 * of an organization's members. Any member reads them; owners and admins
 * manage them. `/orgs/...` paths are never rewritten by the client.
 */
export type OrgGroup = components['schemas']['OrgGroupResponse']
export type OrgGroupDetail = components['schemas']['OrgGroupDetail']
export type OrgGroupMember = components['schemas']['OrgGroupMemberResponse']

function base(org: string): string {
  return `/orgs/${encodeURIComponent(org)}/groups`
}

export const orgGroupsApi = {
  list: (org: string) => api.get<OrgGroup[]>(base(org)),
  get: (org: string, groupId: string) => api.get<OrgGroupDetail>(`${base(org)}/${groupId}`),
  create: (org: string, data: { name: string; description?: string }) =>
    api.post<OrgGroupDetail>(base(org), data),
  /** Sparse: an omitted field is unchanged. */
  update: (org: string, groupId: string, data: { name?: string; description?: string }) =>
    api.patch<OrgGroupDetail>(`${base(org)}/${groupId}`, data),
  delete: (org: string, groupId: string) => api.del<void>(`${base(org)}/${groupId}`),
  /** The user must be a member of the organization. */
  addMember: (org: string, groupId: string, userId: string) =>
    api.post<OrgGroupMember>(`${base(org)}/${groupId}/members`, { user_id: userId }),
  removeMember: (org: string, groupId: string, userId: string) =>
    api.del<void>(`${base(org)}/${groupId}/members/${userId}`),
}

// Query keys live here, not in lib/queryKeys.ts, which is in the entry chunk:
// only the lazy Groups page reads them. Rooted at THAT organization.
/** An organization's groups. */
export const orgGroupsKey = (org: string) => [org, 'orgGroups'] as const
/** One group with its members; under {@link orgGroupsKey}, so a list refresh covers it. */
export const orgGroupKey = (org: string, groupId: string) => [org, 'orgGroups', groupId] as const
/** An organization's members, for the member picker. */
export const orgMembersKey = (org: string) => [org, 'orgMembers'] as const
