import { afterEach, describe, expect, it } from 'vitest'
import { setCurrentOrgSlug } from './activeOrg'
import {
  activeOrgRole,
  canManageProject,
  canManageProjectMembers,
  canWriteProject,
  isOwner,
  isPlatformAdmin,
} from './permissions'
import type { AuthUser } from '@/types'

/**
 * Permissions read the role in the ACTIVE organization (F20 PR7), not the
 * default organization's that `/auth/me` answers in `role`.
 */

const USER: Pick<AuthUser, 'id' | 'role' | 'orgs' | 'is_platform_admin'> = {
  id: 'u-1',
  // What /auth/me says for a browser session: the default organization's role.
  role: 'owner',
  is_platform_admin: false,
  orgs: [
    { slug: 'default', name: 'Default', role: 'owner' },
    { slug: 'acme', name: 'Acme', role: 'member' },
    { slug: 'beta', name: 'Beta', role: 'admin' },
  ],
}

const DEMO = { is_demo: true, created_by_user_id: 'someone-else', can_mutate: undefined, my_role: undefined }

afterEach(() => {
  setCurrentOrgSlug(null)
})

describe('activeOrgRole', () => {
  it('is the role in the active organization', () => {
    setCurrentOrgSlug('acme')
    expect(activeOrgRole(USER)).toBe('member')
    setCurrentOrgSlug('beta')
    expect(activeOrgRole(USER)).toBe('admin')
    expect(activeOrgRole(USER, 'default')).toBe('owner')
  })

  it('is no role in an organization the user is not in', () => {
    setCurrentOrgSlug('elsewhere')
    expect(activeOrgRole(USER)).toBeNull()
    expect(isOwner(activeOrgRole(USER))).toBe(false)
  })

  it('falls back to `role` with no organization known, or no memberships listed', () => {
    expect(activeOrgRole(USER)).toBe('owner')
    setCurrentOrgSlug('acme')
    expect(activeOrgRole({ role: 'admin', orgs: [] })).toBe('admin')
    expect(activeOrgRole(null)).toBeNull()
  })
})

describe('project gates per organization', () => {
  it('lets a default-org owner manage a project in default but not in an org where they are a member', () => {
    setCurrentOrgSlug('default')
    expect(canManageProject(USER, { created_by_user_id: 'x' })).toBe(true)
    expect(canManageProjectMembers(USER, { created_by_user_id: 'x' })).toBe(true)
    expect(canWriteProject(USER, DEMO)).toBe(true)

    setCurrentOrgSlug('acme')
    expect(canManageProject(USER, { created_by_user_id: 'x' })).toBe(false)
    expect(canManageProjectMembers(USER, { created_by_user_id: 'x' })).toBe(false)
    // A demo someone else made is not a member's to write.
    expect(canWriteProject(USER, DEMO)).toBe(false)
  })

  it('treats an admin of the active organization as owner-level', () => {
    setCurrentOrgSlug('beta')
    expect(canManageProject(USER, { created_by_user_id: 'x' })).toBe(true)
    expect(canWriteProject(USER, DEMO)).toBe(true)
  })

  it('keeps the platform flag independent of any organization', () => {
    setCurrentOrgSlug('acme')
    expect(isPlatformAdmin({ ...USER, is_platform_admin: true })).toBe(true)
    expect(isPlatformAdmin(USER)).toBe(false)
  })
})
