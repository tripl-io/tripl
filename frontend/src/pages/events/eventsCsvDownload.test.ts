// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'

import { downloadCsv } from './eventsCsv'

describe('downloadCsv', () => {
  it('revokes the object URL only after the download has had time to start', () => {
    const { createObjectURL, revokeObjectURL } = URL
    vi.useFakeTimers()
    try {
      const revoke = vi.fn()
      URL.createObjectURL = vi.fn(() => 'blob:csv-1')
      URL.revokeObjectURL = revoke
      const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})

      downloadCsv('events.csv', 'a,b')

      expect(click).toHaveBeenCalledTimes(1)
      expect(revoke).not.toHaveBeenCalled()
      vi.runAllTimers()
      expect(revoke).toHaveBeenCalledWith('blob:csv-1')
    } finally {
      vi.useRealTimers()
      URL.createObjectURL = createObjectURL
      URL.revokeObjectURL = revokeObjectURL
      vi.restoreAllMocks()
    }
  })
})
