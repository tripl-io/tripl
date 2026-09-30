import { describe, expect, it } from 'vitest'

import type { ProjectMember, UserListItem } from '@/types'

import { projectCandidates } from './projectCandidates'

const member = (user_id: string, name: string, role: ProjectMember['role'] = 'editor'): ProjectMember => ({
  user_id,
  name,
  email: `${user_id}@x.io`,
  role,
  added_at: '2026-01-01T00:00:00Z',
})

const user = (id: string, role: UserListItem['role']): UserListItem => ({
  id,
  name: id,
  email: `${id}@x.io`,
  role,
  created_at: '2026-01-01T00:00:00Z',
})

describe('projectCandidates', () => {
  it('offers the members, then the org owners and admins who hold no member row', () => {
    const got = projectCandidates(
      [member('u-ada', 'Ada')],
      [user('u-ada', 'member'), user('u-boss', 'owner'), user('u-linus', 'member'), user('u-root', 'admin')],
    )
    expect(got.map((c) => c.user_id)).toEqual(['u-ada', 'u-boss', 'u-root'])
  })

  it('does not list an owner twice when they are also a member', () => {
    const got = projectCandidates([member('u-boss', 'Boss')], [user('u-boss', 'owner')])
    expect(got).toEqual([{ user_id: 'u-boss', name: 'Boss', email: 'u-boss@x.io' }])
  })

  it('copes with either list still loading', () => {
    expect(projectCandidates(undefined, undefined)).toEqual([])
    expect(projectCandidates(undefined, [user('u-boss', 'owner')]).map((c) => c.user_id)).toEqual(['u-boss'])
    expect(projectCandidates([member('u-ada', 'Ada')], null).map((c) => c.user_id)).toEqual(['u-ada'])
  })

  // F20 PR15: the organization's default access to projects.
  it('leaves out a member whose row says No access', () => {
    const got = projectCandidates(
      [member('u-ada', 'Ada'), member('u-linus', 'Linus', 'none')],
      [user('u-ada', 'member'), user('u-linus', 'member')],
    )
    expect(got.map((c) => c.user_id)).toEqual(['u-ada'])
  })

  it('offers every member without a row when the default gives them the project', () => {
    const roster = [
      user('u-ada', 'member'),
      user('u-linus', 'member'),
      user('u-grace', 'member'),
      user('u-boss', 'owner'),
    ]
    const members = [member('u-ada', 'Ada', 'viewer'), member('u-grace', 'Grace', 'none')]

    for (const role of ['viewer', 'editor'] as const) {
      expect(projectCandidates(members, roster, role).map((c) => c.user_id)).toEqual([
        'u-ada',
        'u-linus',
        'u-boss',
      ])
    }
    expect(projectCandidates(members, roster, 'none').map((c) => c.user_id)).toEqual(['u-ada', 'u-boss'])
  })
})
