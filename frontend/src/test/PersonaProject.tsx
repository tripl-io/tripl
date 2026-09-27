import type { ReactNode } from 'react'

import { ActiveProjectContext } from '@/components/active-project-context'
import type { AuthContextValue } from '@/components/auth-context'
import type { Project } from '@/types'

import { isViewerSession, viewerProject, type Persona } from './persona'

/**
 * Makes the tree read-only for a `viewer` persona, the way the app shell does
 * for a project the caller only views; any other persona renders `children`
 * as is (no project context: writes are the organization role's answer).
 */
export function PersonaProject({
  persona,
  project,
  children,
}: {
  persona: Persona
  project?: Partial<Project>
  children?: ReactNode
}) {
  if (persona !== 'viewer') return <>{children}</>
  return (
    <ActiveProjectContext.Provider value={viewerProject(project)}>
      {children}
    </ActiveProjectContext.Provider>
  )
}

/**
 * {@link PersonaProject} for a render helper that only holds the session: a
 * viewer session (from {@link personaAuth}) gets the read-only project.
 */
export function SessionProject({
  session,
  project,
  children,
}: {
  session: AuthContextValue | null | undefined
  project?: Partial<Project>
  children?: ReactNode
}) {
  return (
    <PersonaProject persona={isViewerSession(session) ? 'viewer' : 'member'} project={project}>
      {children}
    </PersonaProject>
  )
}
