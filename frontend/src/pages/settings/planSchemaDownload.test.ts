// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'

import { downloadJson, planSchemaFilename } from './planSchemaDownload'

describe('planSchemaFilename (GH #262)', () => {
  const NOW = new Date('2026-09-27T12:00:00Z')

  it('names the file by slug, branch and date', () => {
    expect(planSchemaFilename('demo', 'main', NOW)).toBe('tripl-plan-schema-demo-main-2026-09-27.json')
  })

  it('leaves the branch out when there is none', () => {
    expect(planSchemaFilename('demo', null, NOW)).toBe('tripl-plan-schema-demo-2026-09-27.json')
  })

  it('puts a branch name in the file name without its slashes', () => {
    expect(planSchemaFilename('demo', 'feature/checkout v2', NOW)).toBe(
      'tripl-plan-schema-demo-feature-checkout-v2-2026-09-27.json',
    )
  })
})

describe('downloadJson (GH #262)', () => {
  it('saves pretty-printed JSON and revokes the URL only after a delay', async () => {
    const { createObjectURL, revokeObjectURL } = URL
    let blob: Blob | undefined
    vi.useFakeTimers()
    try {
      const revoke = vi.fn()
      URL.createObjectURL = vi.fn((b: Blob) => {
        blob = b
        return 'blob:schema-1'
      }) as typeof URL.createObjectURL
      URL.revokeObjectURL = revoke
      const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

      downloadJson('plan.json', { schemas: { 'screen_view/home': { type: 'object' } } })

      expect(click).toHaveBeenCalledTimes(1)
      expect(revoke).not.toHaveBeenCalled()
      vi.runAllTimers()
      expect(revoke).toHaveBeenCalledWith('blob:schema-1')
    } finally {
      vi.useRealTimers()
      URL.createObjectURL = createObjectURL
      URL.revokeObjectURL = revokeObjectURL
      vi.restoreAllMocks()
    }
    expect(blob?.type).toBe('application/json')
    const text = await blob!.text()
    expect(JSON.parse(text)).toEqual({ schemas: { 'screen_view/home': { type: 'object' } } })
    expect(text.endsWith('}\n')).toBe(true)
  })
})
