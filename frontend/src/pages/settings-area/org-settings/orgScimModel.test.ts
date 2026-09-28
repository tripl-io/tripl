import { describe, expect, it } from 'vitest'
import type { ScimToken } from '@/api/scim'
import { scimBaseUrl, scimTokenLabel, sortScimTokens } from './orgScimModel'

function token(id: string, created_at: string, revoked_at: string | null = null): ScimToken {
  return { id, prefix: `tripl_scim_${id}`, created_at, last_used_at: null, revoked_at }
}

describe('orgScimModel', () => {
  it("prefers the server's base URL", () => {
    expect(scimBaseUrl('https://app.example.com', 'acme', 'https://tripl.example.com/scim/v2/acme')).toBe(
      'https://tripl.example.com/scim/v2/acme',
    )
  })

  it('builds the base URL from the origin when the server sends none', () => {
    expect(scimBaseUrl('https://app.example.com/', 'acme', null)).toBe('https://app.example.com/scim/v2/acme')
  })

  it('lists active tokens newest first, then revoked ones', () => {
    const sorted = sortScimTokens([
      token('a', '2026-09-01T00:00:00Z'),
      token('b', '2026-09-03T00:00:00Z', '2026-09-04T00:00:00Z'),
      token('c', '2026-09-02T00:00:00Z'),
    ])
    expect(sorted.map((t) => t.id)).toEqual(['c', 'a', 'b'])
  })

  it('shows a token by its prefix, visibly cut', () => {
    expect(scimTokenLabel({ prefix: 'tripl_scim_ab12' })).toBe('tripl_scim_ab12…')
  })
})
