import { api } from './client'
import type { ProjectMember, ProjectMemberRole } from '../types'

/**
 * Project membership. Anyone who can see the project may list its members;
 * adding, re-roling and removing are for the instance owner and the project's
 * creator, and the server enforces that.
 */
export const projectMembersApi = {
  list: (slug: string, signal?: AbortSignal) =>
    api.get<ProjectMember[]>(`/projects/${slug}/members`, signal),
  add: (slug: string, userId: string, role: ProjectMemberRole) =>
    api.post<ProjectMember>(`/projects/${slug}/members`, { user_id: userId, role }),
  updateRole: (slug: string, userId: string, role: ProjectMemberRole) =>
    api.patch<ProjectMember>(`/projects/${slug}/members/${userId}`, { role }),
  remove: (slug: string, userId: string) => api.del(`/projects/${slug}/members/${userId}`),
}
