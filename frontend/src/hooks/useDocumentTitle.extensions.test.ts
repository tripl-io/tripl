import { describe, expect, it, vi } from 'vitest'
import type { ExtensionRoute } from '@/extensions'
import { composeDocumentTitle, resolveTitleFromPath } from './useDocumentTitle'

// An Enterprise-shaped build: one extension page with a title, one without.
vi.mock('@/extensions', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/extensions')>()
  const Component = (() => null) as unknown as ExtensionRoute['Component']
  const routes: ExtensionRoute[] = [
    { path: '/sso/link', key: 'sso-link', title: 'Link your account', Component },
    { path: '/callback/:provider', key: 'callback', Component },
  ]
  return { ...actual, extensionRoutes: routes }
})

describe('resolveTitleFromPath: extension pages', () => {
  it('titles an extension page as it says', () => {
    // Single sign-on's account link used to read "Page not found".
    expect(resolveTitleFromPath('/sso/link')).toEqual({ label: 'Link your account' })
    expect(composeDocumentTitle(resolveTitleFromPath('/sso/link'))).toBe('Link your account · tripl')
  })

  it('titles an extension page without a title by the app alone, never as a 404', () => {
    expect(composeDocumentTitle(resolveTitleFromPath('/callback/github'))).toBe('tripl')
  })
})
