import type { AuthContextValue } from '@/components/auth-context'
import type { Role } from '@/types'

/** A signed-in session with `role`, for `<AuthContext.Provider value={…}>`. */
export function authAs(role: Role, id = `${role}-1`): AuthContextValue {
  return {
    user: {
      id,
      email: `${role}@example.com`,
      name: role,
      role,
      is_platform_admin: false,
      orgs: [{ slug: 'default', name: 'Default', role }],
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    status: 'authenticated',
    error: null,
    isLoggingOut: false,
    logout: async () => {},
    refresh: () => {},
  }
}
