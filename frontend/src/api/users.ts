import { api } from './client'
import { getAllPages } from './allPages'
import type { Role, UserListItem } from '../types'

/**
 * The largest page the roster routes serve (`GET /users` and
 * `GET /orgs/{org}/members`, `limit` at most 1000): the roster is read in
 * pages this size, so an organization of any size arrives whole.
 */
export const ROSTER_PAGE_LIMIT = 1000

export const usersApi = {
  /** Every member of the active organization, oldest account first. */
  list: () => getAllPages<UserListItem>('/users', ROSTER_PAGE_LIMIT),
  updateRole: (userId: string, role: Role) =>
    api.patch<UserListItem>(`/users/${userId}`, { role }),
}
