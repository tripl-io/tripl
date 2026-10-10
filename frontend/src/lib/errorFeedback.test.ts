import type { Mutation, Query } from '@tanstack/react-query'
import { toast } from 'sonner'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'

import {
  errorToastId,
  surfaceError,
  surfaceMutationError,
  surfaceQueryError,
} from './errorFeedback'

vi.mock('sonner', () => ({ toast: { error: vi.fn() } }))

function query(opts: { data?: unknown; silent?: boolean } = {}) {
  return {
    meta: opts.silent ? { silent: true } : undefined,
    state: { data: opts.data },
  } as unknown as Query<unknown, unknown, unknown>
}

function mutation(opts: { silent?: boolean } = {}) {
  return { meta: opts.silent ? { silent: true } : undefined } as unknown as Mutation<
    unknown,
    unknown,
    unknown
  >
}

function apiError(status: number, message: string, requestId?: string) {
  return new ApiError(message, status, requestId)
}

beforeEach(() => {
  vi.mocked(toast.error).mockClear()
})

describe('surfaceQueryError', () => {
  it('toasts a first-load failure with its reference and a stable id', () => {
    surfaceQueryError(apiError(503, 'Backend is unavailable', 'req-1'), query())

    expect(toast.error).toHaveBeenCalledWith('Backend is unavailable\nReference: req-1', {
      id: 'error:503:Backend is unavailable',
    })
  })

  it('stays quiet for a query that renders its own error', () => {
    surfaceQueryError(apiError(500, 'boom'), query({ silent: true }))
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('stays quiet for a background refetch of data still on screen', () => {
    // A poll during an outage: the page still shows the last good data, and a
    // toast per query per interval was the spam this policy removes.
    surfaceQueryError(apiError(503, 'Backend is unavailable'), query({ data: [] }))
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('leaves a 401 to the re-auth flow', () => {
    surfaceQueryError(apiError(401, 'Not authenticated'), query())
    expect(toast.error).not.toHaveBeenCalled()
  })
})

describe('surfaceMutationError', () => {
  it('toasts a mutation nobody renders an error for', () => {
    surfaceMutationError(new Error('Save failed'), undefined, undefined, mutation())
    expect(toast.error).toHaveBeenCalledWith('Save failed', { id: 'error:client:Save failed' })
  })

  it('stays quiet for a mutation that shows its error inline', () => {
    surfaceMutationError(new Error('Save failed'), undefined, undefined, mutation({ silent: true }))
    expect(toast.error).not.toHaveBeenCalled()
  })
})

describe('surfaceError', () => {
  it('toasts the message with its reference and the dedupe id', () => {
    surfaceError(apiError(422, 'bad chat id', 'req-9'))

    expect(toast.error).toHaveBeenCalledWith('bad chat id\nReference: req-9', {
      id: 'error:422:bad chat id',
    })
  })

  it('leaves a 401 to the re-auth flow', () => {
    surfaceError(apiError(401, 'Not authenticated'))
    expect(toast.error).not.toHaveBeenCalled()
  })

  it('toasts a client-side error as it is', () => {
    surfaceError(new Error('Save failed'))
    expect(toast.error).toHaveBeenCalledWith('Save failed', { id: 'error:client:Save failed' })
  })
})

describe('errorToastId', () => {
  it('is the same for two polls of one outage, whatever their request ids', () => {
    expect(errorToastId(apiError(503, 'down', 'req-1'))).toBe(
      errorToastId(apiError(503, 'down', 'req-2')),
    )
  })

  it('tells different failures apart', () => {
    expect(errorToastId(apiError(503, 'down'))).not.toBe(errorToastId(apiError(403, 'down')))
  })
})
