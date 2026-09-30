import type { AuthContextValue } from '@/components/auth-context'
import type { Project, Role } from '@/types'

import { authAs } from './auth'

/**
 * Who a test signs in as. An organization role, or `viewer`: since F20 PR4 no
 * organization role is read-only, so a read-only visitor is an organization
 * `member` whose row in THIS project is `viewer`. The server says so through
 * the project's `my_role` / `can_mutate`, which `useCanWriteProject` reads from
 * `ActiveProjectContext` (see `PersonaProject`).
 */
export type Persona = Role | 'viewer'

// The sessions personaAuth made for a viewer, so a render helper that is only
// handed the session can still give the tree the viewer's read-only project.
const VIEWER_SESSIONS = new WeakSet<AuthContextValue>()

/** The session for `persona`: a viewer signs in as an organization member. */
export function personaAuth(persona: Persona, id = `${persona}-1`): AuthContextValue {
  const session = authAs(persona === 'viewer' ? 'member' : persona, id)
  if (persona === 'viewer') VIEWER_SESSIONS.add(session)
  return session
}

/** Whether `session` is one {@link personaAuth} made for a viewer. */
export function isViewerSession(session: AuthContextValue | null | undefined): boolean {
  return session != null && VIEWER_SESSIONS.has(session)
}

/** A project as the server answers it to a viewer member: read-only. */
export function viewerProject(overrides: Partial<Project> = {}): Project {
  return {
    slug: 'demo',
    is_demo: false,
    my_role: 'viewer',
    can_mutate: false,
    ...overrides,
  } as Project
}
