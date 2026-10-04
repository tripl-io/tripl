import type { ReactNode } from 'react'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { usersApi } from '@/api/users'
import {
  USER_PENDING_LABEL,
  USER_UNAVAILABLE_LABEL,
  displayUser,
  useUsersById,
} from './useUsersById'

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

const ADA = {
  id: 'u-1',
  email: 'ada@example.com',
  name: 'Ada',
  role: 'member' as const,
  created_at: '2026-01-01T00:00:00Z',
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('displayUser', () => {
  it('keeps "unknown" for a deleted account, whose id is null', () => {
    expect(displayUser(new Map(), null)).toBe('unknown')
    expect(displayUser(Object.assign(new Map(), { status: 'pending' as const }), null)).toBe(
      'unknown',
    )
  })

  it('does not call anyone unknown while the roster is loading', () => {
    vi.spyOn(usersApi, 'list').mockReturnValue(new Promise(() => {}))
    const { result } = renderHook(() => useUsersById(), { wrapper })

    expect(displayUser(result.current, 'u-1')).toBe(USER_PENDING_LABEL)
  })

  it('says the name is unavailable when the roster failed', async () => {
    vi.spyOn(usersApi, 'list').mockRejectedValue(new Error('boom'))
    const { result } = renderHook(() => useUsersById(), { wrapper })

    await waitFor(() => expect(result.current.status).toBe('error'))
    expect(displayUser(result.current, 'u-1')).toBe(USER_UNAVAILABLE_LABEL)
  })

  it('resolves names once loaded, and "unknown" for an id not on the roster', async () => {
    vi.spyOn(usersApi, 'list').mockResolvedValue([ADA])
    const { result } = renderHook(() => useUsersById(), { wrapper })

    await waitFor(() => expect(result.current.status).toBe('success'))
    expect(result.current.get('u-1')).toBe('Ada')
    expect(displayUser(result.current, 'u-1')).toBe('Ada')
    expect(displayUser(result.current, 'u-gone')).toBe('unknown')
  })

  it('still accepts a plain Map from existing callers', () => {
    expect(displayUser(new Map([['u-1', 'Ada']]), 'u-1')).toBe('Ada')
    expect(displayUser(new Map(), 'u-1')).toBe('unknown')
  })
})
