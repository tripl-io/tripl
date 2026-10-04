import { createContext, useContext } from 'react'
import type { QueryClient } from '@tanstack/react-query'
import type { AuthUser } from '@/types'

/** The session query — `GET /auth/me`. Everything else in the cache is protected data. */
export const AUTH_QUERY_KEY = ['auth', 'me'] as const

/**
 * Drop every cached query except the session. Removing `['auth', 'me']` with
 * the rest would drop the signed-in user to 'loading' and unmount the whole app
 * behind the route guard until it answered again.
 */
export function clearProtectedQueries(
  queryClient: QueryClient,
  keep: (queryKey: readonly unknown[]) => boolean = () => false,
) {
  queryClient.removeQueries({
    predicate: (query) => query.queryKey[0] !== 'auth' && !keep(query.queryKey),
  })
}

export type AuthStatus = 'loading' | 'authenticated' | 'anonymous' | 'error'

export interface AuthContextValue {
  user: AuthUser | null
  status: AuthStatus
  error: Error | null
  isLoggingOut: boolean
  logout: () => Promise<void>
  refresh: () => void
  /**
   * True while the session-expired dialog is asking for the password over the
   * app. ErrorState reads it to show a 401 as "waiting for you to
   * sign in" only while that dialog is there to sign in with.
   * Optional so hand-built contexts (tests) read as "not expired".
   */
  sessionExpired?: boolean
}

export const AuthContext = createContext<AuthContextValue | null>(null)

export function useAuth() {
  const value = useContext(AuthContext)
  if (value === null) {
    throw new Error('useAuth must be used within AuthProvider')
  }
  return value
}
