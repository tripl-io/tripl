import { describe, expect, it } from 'vitest'

import {
  VIEWER_READ_ONLY_NOTICE,
  canManageProject,
  canManageProjectMembers,
  canWrite,
  canWriteProject,
  isOwner,
  ownerOnlyReason,
} from './permissions'

describe('canWrite', () => {
  it('lets an owner and an editor write', () => {
    expect(canWrite('owner')).toBe(true)
    expect(canWrite('editor')).toBe(true)
  })

  it('stops exactly the role the API stops', () => {
    // deps.py `require_editor` rejects "viewer" and nothing else, and this is
    // the whole reason the alerting page needed gating: every Ack, Mute, delete
    // and retry on it answered 403 for this one role (tripl-oxkt.9).
    expect(canWrite('viewer')).toBe(false)
  })

  it('does not read a missing role as a viewer', () => {
    // No session yet, or a component mounted outside the auth provider. Absence
    // of evidence is not evidence of a viewer, and guessing wrong here hides
    // working controls from an editor — the failure the user cannot get past.
    expect(canWrite(null)).toBe(true)
    expect(canWrite(undefined)).toBe(true)
  })
})

describe('VIEWER_READ_ONLY_NOTICE', () => {
  it('names the role and all three things the page can no longer do', () => {
    // One sentence, rendered once per section: the page carries ~80 write
    // affordances and explaining each of them individually is a page that
    // repeats itself eighty times.
    expect(VIEWER_READ_ONLY_NOTICE).toMatch(/viewer role/)
    expect(VIEWER_READ_ONLY_NOTICE).toMatch(/incidents/)
    expect(VIEWER_READ_ONLY_NOTICE).toMatch(/destinations and rules/)
    expect(VIEWER_READ_ONLY_NOTICE).toMatch(/retrying deliveries/)
  })
})

describe('isOwner', () => {
  it('passes exactly the owner, as require_owner does', () => {
    expect(isOwner('owner')).toBe(true)
    expect(isOwner('editor')).toBe(false)
    expect(isOwner('viewer')).toBe(false)
  })

  it('reads a missing role as not an owner', () => {
    // Unlike canWrite this is an allow-list on the backend too, so an
    // owner-only surface stays hidden until the session says otherwise.
    expect(isOwner(null)).toBe(false)
    expect(isOwner(undefined)).toBe(false)
  })
})

describe('canManageProject', () => {
  const project = { created_by_user_id: 'u-1' }

  it('lets an owner manage any project', () => {
    expect(canManageProject({ id: 'u-9', role: 'owner' }, project)).toBe(true)
  })

  it('lets the editor who created the project manage it', () => {
    expect(canManageProject({ id: 'u-1', role: 'editor' }, project)).toBe(true)
  })

  it("stops an editor on someone else's project, as _require_project_manager does", () => {
    expect(canManageProject({ id: 'u-2', role: 'editor' }, project)).toBe(false)
  })

  it('stops a creator who has since been demoted to viewer', () => {
    // The routes take EditorUserDep before they look at the creator.
    expect(canManageProject({ id: 'u-1', role: 'viewer' }, project)).toBe(false)
  })

  it('treats a project with no recorded creator as owner-managed', () => {
    expect(canManageProject({ id: 'u-1', role: 'editor' }, { created_by_user_id: null })).toBe(false)
    expect(canManageProject({ id: 'u-1', role: 'editor' }, undefined)).toBe(false)
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
  const editor = { id: 'u-editor', role: 'editor' as const }
  const owner = { id: 'u-owner', role: 'owner' as const }
  const viewer = { id: 'u-viewer', role: 'viewer' as const }

  it('follows canWrite on a real project', () => {
    const project = { is_demo: false, created_by_user_id: 'someone-else' }
    expect(canWriteProject(editor, project)).toBe(true)
    expect(canWriteProject(owner, project)).toBe(true)
    expect(canWriteProject(viewer, project)).toBe(false)
  })

  it("closes another user's demo to an editor, as ProjectMutationScope does", () => {
    const demo = { is_demo: true, created_by_user_id: 'someone-else' }
    expect(canWriteProject(editor, demo)).toBe(false)
    expect(canWriteProject(owner, demo)).toBe(true)
  })

  it('lets the creator of a demo write in it, unless demoted to viewer', () => {
    expect(canWriteProject(editor, { is_demo: true, created_by_user_id: 'u-editor' })).toBe(true)
    expect(canWriteProject(viewer, { is_demo: true, created_by_user_id: 'u-viewer' })).toBe(false)
  })

  it("follows the server's can_mutate when the project carries it", () => {
    // A real project another EDITOR created: the role/demo rule alone would say
    // yes, but require_project_mutation_access answers 403.
    const closed = { is_demo: false, created_by_user_id: 'other-editor', can_mutate: false }
    expect(canWriteProject(editor, closed)).toBe(false)
    const open = { is_demo: true, created_by_user_id: 'someone-else', can_mutate: true }
    expect(canWriteProject(editor, open)).toBe(true)
  })

  it('never lets can_mutate reopen a project to a viewer', () => {
    expect(
      canWriteProject(viewer, { is_demo: false, created_by_user_id: null, can_mutate: true }),
    ).toBe(false)
  })

  it('falls back to the role/demo rule when can_mutate is absent', () => {
    expect(canWriteProject(editor, { is_demo: false, created_by_user_id: 'other-editor' })).toBe(true)
  })

  it('degrades to canWrite when the user or project is not known yet', () => {
    expect(canWriteProject(editor, undefined)).toBe(true)
    expect(canWriteProject(undefined, { is_demo: true, created_by_user_id: 'x' })).toBe(true)
    expect(canWriteProject(viewer, undefined)).toBe(false)
  })
})

describe('canManageProjectMembers', () => {
  const owner = { id: 'u-owner', role: 'owner' as const }
  const editor = { id: 'u-editor', role: 'editor' as const }
  const viewer = { id: 'u-viewer', role: 'viewer' as const }

  it('lets the instance owner manage any project, whatever it says', () => {
    expect(canManageProjectMembers(owner, { created_by_user_id: 'someone-else' })).toBe(true)
    expect(
      canManageProjectMembers(owner, { created_by_user_id: null, can_mutate: false, my_role: 'viewer' }),
    ).toBe(true)
    expect(canManageProjectMembers(owner, undefined)).toBe(true)
  })

  it('lets the creator manage members while they hold an editing role', () => {
    expect(canManageProjectMembers(editor, { created_by_user_id: 'u-editor' })).toBe(true)
    expect(
      canManageProjectMembers(editor, { created_by_user_id: 'u-editor', can_mutate: true, my_role: 'editor' }),
    ).toBe(true)
  })

  it('stops a creator demoted to viewer instance-wide', () => {
    expect(canManageProjectMembers(viewer, { created_by_user_id: 'u-viewer' })).toBe(false)
  })

  it('stops a creator who is only a viewer member of the project', () => {
    expect(
      canManageProjectMembers(editor, { created_by_user_id: 'u-editor', my_role: 'viewer' }),
    ).toBe(false)
    expect(
      canManageProjectMembers(editor, { created_by_user_id: 'u-editor', can_mutate: false }),
    ).toBe(false)
  })

  it('stops an editor who did not create the project, and a missing session', () => {
    expect(
      canManageProjectMembers(editor, { created_by_user_id: 'other', can_mutate: true, my_role: 'editor' }),
    ).toBe(false)
    expect(canManageProjectMembers(editor, undefined)).toBe(false)
    expect(canManageProjectMembers(undefined, { created_by_user_id: 'u-editor' })).toBe(false)
  })
})
