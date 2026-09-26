import { useContext } from 'react'

import { ActiveProjectContext } from '@/components/active-project-context'
import { AuthContext } from '@/components/auth-context'
import type { AuthUser, Project, Role } from '@/types'

/**
 * May this role write?
 *
 * Mirrors `require_editor` in backend/src/tripl/api/deps.py, which rejects
 * exactly ONE role and lets everything else through. Spelled "not a viewer"
 * rather than "owner or editor" for that reason: the day a role is added
 * between the two, an allow-list here would strip write affordances from an
 * account the API still accepts, and the page would be pre-empting a 403 that
 * never comes.
 *
 * The same reasoning covers a missing role: no session yet, or a component
 * mounted outside the auth provider, is not evidence of a viewer. This gate is
 * an affordance over an endpoint that enforces the rule itself, so guessing
 * "viewer" would hide working controls from an editor — the worse of the two
 * failures, because it is the one the user cannot get past.
 */
export function canWrite(role: Role | null | undefined): boolean {
  return role !== 'viewer'
}

/**
 * Whether the signed-in user may write, read from the one auth context.
 *
 * The role has exactly one source in this app — `AuthContext`, filled from
 * /auth/me — and this reads it rather than introducing a second. It goes
 * through `useContext` instead of `useAuth` because `useAuth` throws without a
 * provider: this is a display predicate, and a component rendered outside the
 * provider should degrade to "no role information" (see {@link canWrite})
 * rather than crash the surface it was gating.
 */
export function useCanWrite(): boolean {
  const auth = useContext(AuthContext)
  return canWrite(auth?.user?.role)
}

/**
 * May this user write inside this project (its plan, metrics, scans, alerting)?
 *
 * {@link canWrite} answers "may this role edit something". Every slug-scoped
 * write route also passes `require_project_mutation_access`
 * (backend/src/tripl/api/deps.py), which since project membership (tripl-vefw)
 * asks one question: is the caller the instance owner, or a member of this
 * project with an editing role? A viewer member only reads, whatever their
 * instance role.
 *
 * The server answers that per request: `ProjectResponse.can_mutate` is the gate
 * evaluated for the caller, and when the project carries it, it decides.
 * `my_role` (the caller's membership role, capped by their instance role) is
 * the next best signal. The demo/creator rule below is only the fallback for a
 * project without either field (fixtures, a response from an older API).
 *
 * Missing information degrades to {@link canWrite}'s answer for the same reason
 * given there: no session or no project loaded yet is not evidence of a
 * read-only visitor.
 */
export function canWriteProject(
  user: Pick<AuthUser, 'id' | 'role'> | null | undefined,
  project: Pick<Project, 'is_demo' | 'created_by_user_id' | 'can_mutate' | 'my_role'> | null | undefined,
): boolean {
  if (!canWrite(user?.role)) return false
  if (typeof project?.can_mutate === 'boolean') return project.can_mutate
  // Project membership (tripl-vefw): a viewer member only reads, however the
  // instance role reads. `can_mutate` already folds this in when present.
  if (project?.my_role === 'viewer') return false
  if (!user || !project?.is_demo) return true
  if (isOwner(user.role)) return true
  return project.created_by_user_id != null && project.created_by_user_id === user.id
}

/**
 * {@link canWriteProject} for the signed-in user and the project the app shell
 * resolved for the URL ({@link ActiveProjectContext}). Outside the shell there
 * is no project, and the answer is {@link canWrite}'s.
 */
export function useCanWriteProject(): boolean {
  const auth = useContext(AuthContext)
  const project = useContext(ActiveProjectContext)
  return canWriteProject(auth?.user, project)
}

/**
 * Is this role the instance owner?
 *
 * Mirrors `require_owner` in backend/src/tripl/api/deps.py (`role != "owner"` is
 * rejected). Unlike {@link canWrite} this IS an allow-list, because the backend's
 * rule is one: exactly one role passes. A missing role is therefore "not an
 * owner" — owner-only surfaces (data sources, scan authoring, project deletion)
 * stay hidden until the session says otherwise, as they always have.
 */
export function isOwner(role: Role | null | undefined): boolean {
  return role === 'owner'
}

/** {@link isOwner} for the signed-in user, read the same way as {@link useCanWrite}. */
export function useIsOwner(): boolean {
  const auth = useContext(AuthContext)
  return isOwner(auth?.user?.role)
}

/**
 * May this user edit the project itself (name, slug, retention) or manage the
 * demo it is?
 *
 * Mirrors the backend's pair of gates on `PATCH /projects/{slug}` and the demo
 * reset/delete routes: `EditorUserDep` (not a viewer) AND `_is_project_manager`
 * (an owner, or the user who created the project). A creator who has since been
 * demoted to viewer fails the first half, which is why the role is checked and
 * not only the id.
 */
export function canManageProject(
  user: Pick<AuthUser, 'id' | 'role'> | null | undefined,
  project: Pick<Project, 'created_by_user_id'> | null | undefined,
): boolean {
  if (!user) return false
  if (isOwner(user.role)) return true
  return (
    canWrite(user.role) &&
    project?.created_by_user_id != null &&
    project.created_by_user_id === user.id
  )
}

/** {@link canManageProject} for the signed-in user. */
export function useCanManageProject(
  project: Pick<Project, 'created_by_user_id'> | null | undefined,
): boolean {
  const auth = useContext(AuthContext)
  return canManageProject(auth?.user, project)
}

/**
 * The reason on a control only an owner can use, for the few places where the
 * control stays visible (disabled) because its presence explains something.
 * `action` completes "Only an owner can …".
 */
export function ownerOnlyReason(action: string): string {
  return `Only an owner can ${action}.`
}

/**
 * Why the write controls are missing — said ONCE per section.
 *
 * Deliberately not attached to individual controls: the alerting page carries
 * ~80 write affordances across its three sections, and a tooltip on each of
 * them is a page that explains itself eighty times and reads once. Naming all
 * three jobs in one sentence lets the same string sit at the head of whichever
 * section the reader is actually on (tripl-oxkt.9).
 */
export const VIEWER_READ_ONLY_NOTICE =
  'Read-only: your account has the viewer role. Acting on incidents, changing destinations and rules, and retrying deliveries are done by an editor or owner.'

/**
 * The same notice for a surface that is not alerting: says what the role is and
 * who can act, without listing one page's jobs on another.
 */
export const VIEWER_READ_ONLY_HINT =
  'Read-only: your account has the viewer role. Changes here are made by an editor or owner.'

/**
 * May this user change who belongs to this project (add, re-role, remove)?
 *
 * Mirrors the gates on the member mutation routes of `/projects/{slug}/members`:
 * the manager check (the instance owner, or the user who created the project)
 * AND `EditorUserDep`. So a creator also needs an editing role: not a viewer
 * instance-wide, and not a viewer member of this project (`my_role`) or
 * otherwise read-only in it (`can_mutate === false`). The instance owner passes
 * outright. The server enforces the rule either way; this only decides which
 * controls are drawn.
 */
export function canManageProjectMembers(
  user: Pick<AuthUser, 'id' | 'role'> | null | undefined,
  project: Pick<Project, 'created_by_user_id' | 'can_mutate' | 'my_role'> | null | undefined,
): boolean {
  if (!user) return false
  if (isOwner(user.role)) return true
  if (!canWrite(user.role)) return false
  if (project?.my_role === 'viewer' || project?.can_mutate === false) return false
  return project?.created_by_user_id != null && project.created_by_user_id === user.id
}
