import { afterEach, describe, expect, it, vi } from 'vitest'
import { setCurrentOrgSlug } from '@/lib/activeOrg'
import { api, orgScopedPath } from './client'
import { photoFileUrl } from './eventPhotos'

/**
 * The client addresses the active organization (F20 PR7): the ~227 call sites
 * keep writing `/projects/…`, and the request goes out as `/orgs/{org}/…`.
 */

function mockFetch() {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
    Promise.resolve(new Response(JSON.stringify({}), { status: 200, headers: { 'Content-Type': 'application/json' } })),
  )
}

function urlOf(call: unknown[]): string {
  const input = call[0] as RequestInfo | URL
  return typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
}

afterEach(() => {
  vi.restoreAllMocks()
  setCurrentOrgSlug(null)
})

describe('orgScopedPath', () => {
  it.each([
    ['/projects', '/orgs/acme/projects'],
    ['/projects/web/events?status=live', '/orgs/acme/projects/web/events?status=live'],
    ['/data-sources/ds-1/test', '/orgs/acme/data-sources/ds-1/test'],
    ['/users', '/orgs/acme/users'],
    ['/users/invitations', '/orgs/acme/users/invitations'],
    ['/audit?limit=50', '/orgs/acme/audit?limit=50'],
    ['/activity/projects/web', '/orgs/acme/activity/projects/web'],
    ['/me/api-keys', '/orgs/acme/me/api-keys'],
  ])('moves %s under the organization', (path, expected) => {
    expect(orgScopedPath(path, 'acme')).toBe(expected)
  })

  it.each([
    '/orgs',
    '/orgs/acme/members',
    '/auth/me',
    '/auth/invitations/tok',
    '/settings',
    '/settings/photo-limits',
    '/project-templates',
    // A prefix is a whole first segment, not the start of one.
    '/projects-archive',
    '/meta',
  ])('passes %s through', (path) => {
    expect(orgScopedPath(path, 'acme')).toBe(path)
  })

  it('leaves every path alone while no organization is known', () => {
    expect(orgScopedPath('/projects/web', null)).toBe('/projects/web')
  })
})

describe('api requests', () => {
  it('send org-scoped paths to the active organization', async () => {
    const fetchSpy = mockFetch()
    setCurrentOrgSlug('acme')

    await api.get('/projects/web/events')
    await api.get('/auth/me')

    expect(urlOf(fetchSpy.mock.calls[0]!)).toBe('/api/v1/orgs/acme/projects/web/events')
    expect(urlOf(fetchSpy.mock.calls[1]!)).toBe('/api/v1/auth/me')
  })

  it('keep the legacy path with no organization', async () => {
    const fetchSpy = mockFetch()

    await api.get('/projects')

    expect(urlOf(fetchSpy.mock.calls[0]!)).toBe('/api/v1/projects')
  })

  it('send a DELETE body when one is given', async () => {
    const fetchSpy = mockFetch()

    await api.del('/orgs/acme', { confirm_slug: 'acme' })

    const init = fetchSpy.mock.calls[0]![1] as RequestInit
    expect(init.method).toBe('DELETE')
    expect(init.body).toBe(JSON.stringify({ confirm_slug: 'acme' }))
  })
})

describe('event photo files', () => {
  afterEach(() => {
    setCurrentOrgSlug(null)
  })

  it('loads a server-built org-less file URL from the active organization', () => {
    setCurrentOrgSlug('acme')
    expect(photoFileUrl('/api/v1/projects/web/events/e1/photos/p1/file')).toBe(
      '/api/v1/orgs/acme/projects/web/events/e1/photos/p1/file',
    )
  })

  it('leaves an org-qualified or external URL alone', () => {
    setCurrentOrgSlug('acme')
    expect(photoFileUrl('/api/v1/orgs/beta/projects/web/events/e1/photos/p1/file')).toBe(
      '/api/v1/orgs/beta/projects/web/events/e1/photos/p1/file',
    )
    expect(photoFileUrl('https://cdn.example.com/p1.png')).toBe('https://cdn.example.com/p1.png')
  })
})
