import { afterEach, describe, expect, it, vi } from 'vitest'

afterEach(() => {
  vi.unstubAllGlobals()
  vi.resetModules()
})

describe('shortcutLabel', () => {
  it('spells a shortcut with ⌘ on a Mac', async () => {
    vi.stubGlobal('navigator', { platform: 'MacIntel', userAgent: '' })
    vi.resetModules()
    const { shortcutLabel } = await import('./platform')
    expect(shortcutLabel('P')).toBe('⌘P')
  })

  it('spells a shortcut with Ctrl elsewhere', async () => {
    vi.stubGlobal('navigator', { platform: '', userAgent: 'Mozilla/5.0 (X11; Linux x86_64)' })
    vi.resetModules()
    const { shortcutLabel } = await import('./platform')
    expect(shortcutLabel('P')).toBe('Ctrl P')
  })
})
