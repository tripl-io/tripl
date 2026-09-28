import { afterEach, describe, expect, it, vi } from 'vitest'
import { setCurrentOrgSlug } from '@/lib/activeOrg'
import { orgSettingsApi } from './orgSettings'
import { serviceSettingsApi } from './serviceSettings'

/**
 * F20 PR9: an organization's settings are addressed by the organization in the
 * path, the Platform console by `/platform/settings`, never by the legacy
 * `/settings` that guesses an organization for a user in several.
 */

function mockFetch() {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
    Promise.resolve(
      new Response(JSON.stringify({ ok: true, message: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    ),
  )
}

function calls(fetchSpy: ReturnType<typeof mockFetch>): Array<{ url: string; method: string; body?: string }> {
  return fetchSpy.mock.calls.map(([input, init]) => ({
    url: typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url,
    method: (init?.method ?? 'GET').toUpperCase(),
    body: init?.body ? String(init.body) : undefined,
  }))
}

afterEach(() => {
  vi.restoreAllMocks()
  setCurrentOrgSlug(null)
})

describe('orgSettingsApi', () => {
  it('names the organization in every path, whichever one is active', async () => {
    setCurrentOrgSlug('other')
    const fetchSpy = mockFetch()

    await orgSettingsApi.get('acme')
    await orgSettingsApi.update('acme', { limits: { scan_row_limit_default: 1000 } })
    await orgSettingsApi.testAi('acme')
    await orgSettingsApi.testEmail('acme')

    expect(calls(fetchSpy)).toEqual([
      { url: '/api/v1/orgs/acme/settings', method: 'GET', body: undefined },
      {
        url: '/api/v1/orgs/acme/settings',
        method: 'PATCH',
        body: JSON.stringify({ limits: { scan_row_limit_default: 1000 } }),
      },
      { url: '/api/v1/orgs/acme/settings/ai/test', method: 'POST', body: '{}' },
      { url: '/api/v1/orgs/acme/settings/email/test', method: 'POST', body: '{}' },
    ])
  })

  it("reads and writes the organization's tracker defaults under its own path (F20 PR12)", async () => {
    setCurrentOrgSlug('other')
    const fetchSpy = mockFetch()

    await orgSettingsApi.getTrackers('acme')
    await orgSettingsApi.updateTrackers('acme', { linear: { team_id: 'ENG' } })

    expect(calls(fetchSpy)).toEqual([
      { url: '/api/v1/orgs/acme/settings/trackers', method: 'GET', body: undefined },
      {
        url: '/api/v1/orgs/acme/settings/trackers',
        method: 'PATCH',
        body: JSON.stringify({ linear: { team_id: 'ENG' } }),
      },
    ])
  })
})

describe('serviceSettingsApi (Platform)', () => {
  it('reads and writes the operator scope at /platform/settings', async () => {
    const fetchSpy = mockFetch()

    await serviceSettingsApi.get()
    await serviceSettingsApi.update({ runtime: { app_base_url: 'https://tripl.example.com' } })
    await serviceSettingsApi.testEmail()

    expect(calls(fetchSpy).map(call => `${call.method} ${call.url}`)).toEqual([
      'GET /api/v1/platform/settings',
      'PATCH /api/v1/platform/settings',
      'POST /api/v1/platform/settings/email/test',
    ])
  })

  it("quotes the active organization's row caps", async () => {
    const fetchSpy = mockFetch()

    setCurrentOrgSlug('acme')
    await serviceSettingsApi.rowLimitDefaults()
    setCurrentOrgSlug(null)
    await serviceSettingsApi.rowLimitDefaults()

    expect(calls(fetchSpy).map(call => call.url)).toEqual([
      '/api/v1/orgs/acme/settings/row-limits',
      '/api/v1/settings/row-limits',
    ])
  })
})
