import { afterEach, describe, expect, it, vi } from 'vitest'
import { setCurrentOrgSlug } from '@/lib/activeOrg'
import { auditExportUrl, auditWebhookApi, dayAfter } from './auditExport'

/**
 * F20: the audit export and webhook are addressed by the organization in the
 * path, never by the client's `/audit` rewrite that follows the active one.
 */

function mockFetch(status = 200, body: unknown = { ok: true }) {
  return vi.spyOn(globalThis, 'fetch').mockImplementation(() =>
    Promise.resolve(
      status === 204
        ? new Response(null, { status })
        : new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }),
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

describe('auditExportUrl', () => {
  it('builds the download address for the named organization, the last day included', () => {
    setCurrentOrgSlug('other')
    // The server's `to` is exclusive: the day after the last one picked.
    expect(auditExportUrl('acme', { format: 'csv', from: '2026-01-01', to: '2026-01-31' })).toBe(
      '/api/v1/orgs/acme/audit/export?format=csv&from=2026-01-01&to=2026-02-01',
    )
  })

  it('adds the action filter only when one is picked, and encodes the slug', () => {
    expect(
      auditExportUrl('a b', { format: 'json', from: '2026-01-01', to: '2026-01-01', action: 'org.member_role_changed' }),
    ).toBe('/api/v1/orgs/a%20b/audit/export?format=json&from=2026-01-01&to=2026-01-02&action=org.member_role_changed')
  })

  it('steps over month, year and leap-day ends in UTC', () => {
    expect(dayAfter('2026-09-30')).toBe('2026-10-01')
    expect(dayAfter('2026-12-31')).toBe('2027-01-01')
    expect(dayAfter('2028-02-28')).toBe('2028-02-29')
    expect(dayAfter('not-a-day')).toBe('not-a-day')
  })
})

describe('auditWebhookApi', () => {
  it('names the organization in every path', async () => {
    setCurrentOrgSlug('other')
    const fetchSpy = mockFetch()

    await auditWebhookApi.get('acme')
    await auditWebhookApi.save('acme', { url: 'https://siem.example.com/hook', enabled: true })
    await auditWebhookApi.rotateSecret('acme')
    await auditWebhookApi.test('acme')
    await auditWebhookApi.deliveries('acme', { status: 'dead', limit: 50 })
    await auditWebhookApi.deliveries('acme')

    expect(calls(fetchSpy)).toEqual([
      { url: '/api/v1/orgs/acme/audit/webhook', method: 'GET', body: undefined },
      {
        url: '/api/v1/orgs/acme/audit/webhook',
        method: 'PUT',
        body: JSON.stringify({ url: 'https://siem.example.com/hook', enabled: true }),
      },
      { url: '/api/v1/orgs/acme/audit/webhook/rotate-secret', method: 'POST', body: undefined },
      { url: '/api/v1/orgs/acme/audit/webhook/test', method: 'POST', body: undefined },
      { url: '/api/v1/orgs/acme/audit/webhook/deliveries?status=dead&limit=50', method: 'GET', body: undefined },
      { url: '/api/v1/orgs/acme/audit/webhook/deliveries', method: 'GET', body: undefined },
    ])
  })

  it('reads the server’s 200 {configured: false} as none', async () => {
    mockFetch(200, {
      configured: false,
      url: '',
      enabled: false,
      secret_configured: false,
      last_success_at: null,
      last_error: null,
      last_error_at: null,
    })
    await expect(auditWebhookApi.get('acme')).resolves.toBeNull()
  })

  it('returns a configured webhook as it is', async () => {
    const saved = {
      configured: true,
      url: 'https://siem.example.com/hook',
      enabled: true,
      secret_configured: true,
      last_success_at: null,
      last_error: null,
      last_error_at: null,
    }
    mockFetch(200, saved)
    await expect(auditWebhookApi.get('acme')).resolves.toEqual(saved)
  })

  it('reads a missing webhook as none', async () => {
    mockFetch(404, { detail: 'No audit webhook' })
    await expect(auditWebhookApi.get('acme')).resolves.toBeNull()
  })

  it('still fails on any other error', async () => {
    mockFetch(403, { detail: 'Organization owner required' })
    await expect(auditWebhookApi.get('acme')).rejects.toMatchObject({ status: 403 })
  })

  it('deletes with DELETE', async () => {
    const fetchSpy = mockFetch(204)
    await auditWebhookApi.remove('acme')
    expect(calls(fetchSpy)).toEqual([{ url: '/api/v1/orgs/acme/audit/webhook', method: 'DELETE', body: undefined }])
  })
})
