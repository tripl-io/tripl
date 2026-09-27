import { describe, expect, it } from 'vitest'

import {
  VIEWER_READ_ONLY_HINT,
  VIEWER_READ_ONLY_NOTICE,
  canManageProject,
  canManageProjectMembers,
  canWrite,
  canWriteProject,
  isOwner,
  isPlatformAdmin,
  ownerOnlyReason,
} from './permissions'

describe('canWrite', () => {
  it('lets every organization role write (read-only is a project role now)', () => {
    expect(canWrite('owner')).toBe(true)
    expect(canWrite('admin')).toBe(true)
    expect(canWrite('member')).toBe(true)
  })

  it('does not read a missing role as read-only', () => {
    // No session yet, a component mounted outside the auth provider, or a
    // session acting in no single organization. Absence of evidence is not
    // evidence of a read-only account, and guessing wrong here hides working
    // controls — the failure the user cannot get past.
    expect(canWrite(null)).toBe(true)
    expect(canWrite(undefined)).toBe(true)
  })
})

describe('VIEWER_READ_ONLY_NOTICE', () => {
  it('names the project role and all three things the page can no longer do', () => {
    // One sentence, rendered once per section: the page carries ~80 write
    // affordances and explaining each of them individually is a page that
    // repeats itself eighty times.
    expect(VIEWER_READ_ONLY_NOTICE).toMatch(/viewer role in this project/)
    expect(VIEWER_READ_ONLY_NOTICE).toMatch(/incidents/)
    expect(VIEWER_READ_ONLY_NOTICE).toMatch(/destinations and rules/)
    expect(VIEWER_READ_ONLY_NOTICE).toMatch(/retrying deliveries/)
    expect(VIEWER_READ_ONLY_HINT).toMatch(/viewer role in this project/)
  })
})

describe('isOwner', () => {
  it('passes an org owner and an org admin, as the owner gates do', () => {
    expect(isOwner('owner')).toBe(true)
    expect(isOwner('admin')).toBe(true)
    expect(isOwner('member')).toBe(false)
  })

  it('reads a missing role as not an owner', () => {
    // Unlike canWrite this is an allow-list on the backend too, so an
    // owner-only surface stays hidden until the session says otherwise.
    expect(isOwner(null)).toBe(false)
    expect(isOwner(undefined)).toBe(false)
  })
})

describe('isPlatformAdmin', () => {
  it('passes only the operator flag, whatever the org role', () => {
    expect(isPlatformAdmin({ is_platform_admin: true })).toBe(true)
    expect(isPlatformAdmin({ is_platform_admin: false })).toBe(false)
    expect(isPlatformAdmin(null)).toBe(false)
    expect(isPlatformAdmin(undefined)).toBe(false)
  })
})

describe('canManageProject', () => {
  const project = { created_by_user_id: 'u-1' }

  it('lets an org owner or admin manage any project', () => {
    expect(canManageProject({ id: 'u-9', role: 'owner' }, project)).toBe(true)
    expect(canManageProject({ id: 'u-9', role: 'admin' }, project)).toBe(true)
  })

  it('lets the member who created the project manage it', () => {
    expect(canManageProject({ id: 'u-1', role: 'member' }, project)).toBe(true)
  })

  it('stops a creator who only views the project, as EditorUserDep does', () => {
    expect(
      canManageProject({ id: 'u-1', role: 'member' }, { created_by_user_id: 'u-1', my_role: 'viewer' }),
    ).toBe(false)
    expect(
      canManageProject({ id: 'u-1', role: 'member' }, { created_by_user_id: 'u-1', can_mutate: false }),
    ).toBe(false)
  })

  it("stops a member on someone else's project, as _require_project_manager does", () => {
    expect(canManageProject({ id: 'u-2', role: 'member' }, project)).toBe(false)
  })

  it('treats a project with no recorded creator as admin-managed', () => {
    expect(canManageProject({ id: 'u-1', role: 'member' }, { created_by_user_id: null })).toBe(false)
    expect(canManageProject({ id: 'u-1', role: 'member' }, undefined)).toBe(false)
  })

  it('refuses without a user', () => {
    expect(canManageProject(null, project)).toBe(false)
  })
})

describe('ownerOnlyReason', () => {
  it('names the role that can act', () => {
    expect(ownerOnlyReason('edit scans')).toBe('Only an owner can edit scans.')
  })
})

describe('canWriteProject', () => {
  const member = { id: 'u-member', role: 'member' as const }
  const owner = { id: 'u-owner', role: 'owner' as const }
  const admin = { id: 'u-admin', role: 'admin' as const }

  it('lets any org role write on a real project without a server answer', () => {
    const project = { is_demo: false, created_by_user_id: 'someone-else' }
    expect(canWriteProject(member, project)).toBe(true)
    expect(canWriteProject(owner, project)).toBe(true)
  })

  it("closes another user's demo to a member, as ProjectMutationScope does", () => {
    const demo = { is_demo: true, created_by_user_id: 'someone-else' }
    expect(canWriteProject(member, demo)).toBe(false)
    expect(canWriteProject(owner, demo)).toBe(true)
    expect(canWriteProject(admin, demo)).toBe(true)
  })

  it('lets the creator of a demo write in it', () => {
    expect(canWriteProject(member, { is_demo: true, created_by_user_id: 'u-member' })).toBe(true)
  })

  it("follows the server's can_mutate when the project carries it", () => {
    // A real project another member created: the role/demo rule alone would
    // say yes, but require_project_mutation_access answers 403.
    const closed = { is_demo: false, created_by_user_id: 'other-member', can_mutate: false }
    expect(canWriteProject(member, closed)).toBe(false)
    const open = { is_demo: true, created_by_user_id: 'someone-else', can_mutate: true }
    expect(canWriteProject(member, open)).toBe(true)
  })

  it('reads a viewer project row as read-only when can_mutate is absent', () => {
    expect(canWriteProject(member, { is_demo: false, created_by_user_id: null, my_role: 'viewer' })).toBe(
      false,
    )
  })

  it('falls back to the role/demo rule when can_mutate is absent', () => {
    expect(canWriteProject(member, { is_demo: false, created_by_user_id: 'other-member' })).toBe(true)
  })

  it('degrades to canWrite when the user or project is not known yet', () => {
    expect(canWriteProject(member, undefined)).toBe(true)
    expect(canWriteProject(undefined, { is_demo: true, created_by_user_id: 'x' })).toBe(true)
  })
})

describe('canManageProjectMembers', () => {
  const owner = { id: 'u-owner', role: 'owner' as const }
  const admin = { id: 'u-admin', role: 'admin' as const }
  const member = { id: 'u-member', role: 'member' as const }

  it('lets an org owner or admin manage any project, whatever it says', () => {
    for (const user of [owner, admin]) {
      expect(canManageProjectMembers(user, { created_by_user_id: 'someone-else' })).toBe(true)
      expect(
        canManageProjectMembers(user, { created_by_user_id: null, can_mutate: false, my_role: 'viewer' }),
      ).toBe(true)
      expect(canManageProjectMembers(user, undefined)).toBe(true)
    }
  })

  it('lets the creator manage members while they hold an editing role', () => {
    expect(canManageProjectMembers(member, { created_by_user_id: 'u-member' })).toBe(true)
    expect(
      canManageProjectMembers(member, { created_by_user_id: 'u-member', can_mutate: true, my_role: 'editor' }),
    ).toBe(true)
  })

  it('stops a creator who is only a viewer member of the project', () => {
    expect(
      canManageProjectMembers(member, { created_by_user_id: 'u-member', my_role: 'viewer' }),
    ).toBe(false)
    expect(
      canManageProjectMembers(member, { created_by_user_id: 'u-member', can_mutate: false }),
    ).toBe(false)
  })

  it('stops a member who did not create the project, and a missing session', () => {
    expect(
      canManageProjectMembers(member, { created_by_user_id: 'other', can_mutate: true, my_role: 'editor' }),
    ).toBe(false)
    expect(canManageProjectMembers(member, undefined)).toBe(false)
    expect(canManageProjectMembers(undefined, { created_by_user_id: 'u-member' })).toBe(false)
  })
})
