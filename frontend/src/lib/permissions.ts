import { useContext } from 'react'

import { ActiveProjectContext } from '@/components/active-project-context'
import { AuthContext } from '@/components/auth-context'
import { currentOrgSlug } from '@/lib/activeOrg'
import type { AuthUser, Project, Role } from '@/types'

/**
 * The user's role in the organization the app acts in (F20 PR7).
 *
 * `/auth/me` answers `role` for the organization a browser session acts in by
 * default — the default organization — and lists every membership in `orgs`.
 * Once the app acts in another organization, `role` says nothing about it: an
 * owner of `default` is nobody in `acme`. So the role is read from `orgs` for
 * the active organization, and is `null` when the user holds none there (an
 * address naming someone else's organization, which the server answers 404).
 *
 * With no organization known — no provider, or a session from before
 * organizations (no `orgs`) — `role` is all there is, and it stands.
 */
export function activeOrgRole(
  user: Pick<AuthUser, 'role'> & Partial<Pick<AuthUser, 'orgs'>> | null | undefined,
  org: string | null = currentOrgSlug(),
): Role | null {
  if (!user) return null
  if (!org || !user.orgs || user.orgs.length === 0) return user.role ?? null
  return user.orgs.find((membership) => membership.slug === org)?.role ?? null
}

/**
 * May this organization role write?
 *
 * Always yes since F20 PR4: `role` is the ORGANIZATION role (owner | admin |
 * member), and none of them is read-only. Being read-only is a PROJECT matter
 * now — a `viewer` project row — which the server answers per project through
 * `ProjectResponse.can_mutate` / `my_role` (see {@link canWriteProject}). The
 * org-level write gate (`get_editor_user` without a slug: creating a project, a
 * write-scope API key) only asks for membership of the organization.
 *
 * Kept as the one seam the ~60 call sites go through, so a future read-only
 * organization role is a change here and nowhere else. A missing role (no
 * session yet, a component outside the auth provider, a user whose session acts
 * in no single organization) is not evidence of a read-only account: this gate
 * is an affordance over endpoints that enforce the rule themselves, so guessing
 * "read-only" would hide working controls — the failure the user cannot get past.
 */
export function canWrite(_role: Role | null | undefined): boolean {
  return true
}

/**
 * Whether the signed-in user's organization role may write (see {@link canWrite}),
 * read from the one auth context.
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
  return canWrite(activeOrgRole(auth?.user))
}

/**
 * May this user write inside this project (its plan, metrics, scans, alerting)?
 *
 * {@link canWrite} answers "may this role edit something". Every slug-scoped
 * write route also passes `require_project_mutation_access`
 * (backend/src/tripl/api/deps.py), which asks one question: is the caller an
 * owner or admin of the project's organization, or a member of this project
 * with an editing role? A viewer member only reads.
 *
 * The server answers that per request: `ProjectResponse.can_mutate` is the gate
 * evaluated for the caller, and when the project carries it, it decides.
 * `my_role` (the caller's project role) is the next best signal. The demo/creator rule below is only the fallback for a
 * project without either field (fixtures, a response from an older API).
 *
 * Missing information degrades to {@link canWrite}'s answer for the same reason
 * given there: no session or no project loaded yet is not evidence of a
 * read-only visitor.
 */
export function canWriteProject(
  user: Pick<AuthUser, 'id' | 'role'> & Partial<Pick<AuthUser, 'orgs'>> | null | undefined,
  project: Pick<Project, 'is_demo' | 'created_by_user_id' | 'can_mutate' | 'my_role'> | null | undefined,
): boolean {
  const role = activeOrgRole(user)
  if (!canWrite(role)) return false
  if (typeof project?.can_mutate === 'boolean') return project.can_mutate
  // Project membership (tripl-vefw): a viewer member only reads.
  // `can_mutate` already folds this in when present.
  if (project?.my_role === 'viewer') return false
  if (!user || !project?.is_demo) return true
  if (isOwner(role)) return true
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
 * Is this organization role an owner or an admin of the organization?
 *
 * Mirrors the owner gates in backend/src/tripl/api/deps.py (`get_owner_user`,
 * `get_key_reachable_owner_user`), which since F20 PR4 admit an org owner or
 * admin: data sources, scan authoring, project deletion, the audit log, members
 * and invitations. An org owner or admin is also project role `owner` in every
 * project of the organization. The name is kept so its ~60 call sites read the
 * same; "owner" in the UI's "Only an owner can …" means this pair.
 *
 * Unlike {@link canWrite} this IS an allow-list, because the backend's rule is
 * one. A missing role is therefore "not an owner" — owner-only surfaces stay
 * hidden until the session says otherwise, as they always have. Owner-vs-admin
 * (managing owners) is asked with `role === 'owner'` where it matters.
 */
export function isOwner(role: Role | null | undefined): boolean {
  return role === 'owner' || role === 'admin'
}

/** {@link isOwner} for the signed-in user, read the same way as {@link useCanWrite}. */
export function useIsOwner(): boolean {
  const auth = useContext(AuthContext)
  return isOwner(activeOrgRole(auth?.user))
}

/**
 * Is the signed-in user an OWNER (not an admin) of the active organization?
 * Deleting the organization and transferring or managing ownership are theirs.
 */
export function useIsOrgOwner(): boolean {
  const auth = useContext(AuthContext)
  return activeOrgRole(auth?.user) === 'owner'
}

/**
 * Is this user a platform admin (`users.is_platform_admin`)?
 *
 * Mirrors `require_platform_admin` in backend/src/tripl/api/deps.py: the
 * operator settings (security, observability, the system block, the server
 * paths and the photo size cap) are theirs alone. It is NOT an organization
 * role: a platform admin gets nothing inside an organization from it, and an
 * organization owner gets no operator settings from theirs. A missing flag is
 * "not a platform admin".
 */
export function isPlatformAdmin(
  user: Pick<AuthUser, 'is_platform_admin'> | null | undefined,
): boolean {
  return user?.is_platform_admin === true
}

/** {@link isPlatformAdmin} for the signed-in user, read the same way as {@link useIsOwner}. */
export function useIsPlatformAdmin(): boolean {
  const auth = useContext(AuthContext)
  return isPlatformAdmin(auth?.user)
}

/**
 * May this user edit the project itself (name, slug, retention) or manage the
 * demo it is?
 *
 * Mirrors the backend's pair of gates on `PATCH /projects/{slug}` and the demo
 * reset/delete routes: `EditorUserDep` (an editing project role) AND
 * `_is_project_manager` (project role owner, i.e. an owner or admin of the
 * organization, or the user who created the project). The editor half is the
 * project's `can_mutate` / `my_role`: a creator who is a viewer of the project
 * manages nothing.
 */
export function canManageProject(
  user: Pick<AuthUser, 'id' | 'role'> & Partial<Pick<AuthUser, 'orgs'>> | null | undefined,
  project: Pick<Project, 'created_by_user_id' | 'can_mutate' | 'my_role'> | null | undefined,
): boolean {
  if (!user) return false
  const role = activeOrgRole(user)
  if (isOwner(role)) return true
  // The editor half: a creator who only views this project now (a `viewer`
  // row) fails `EditorUserDep` before the creator check is reached.
  if (project?.my_role === 'viewer' || project?.can_mutate === false) return false
  return (
    canWrite(role) &&
    project?.created_by_user_id != null &&
    project.created_by_user_id === user.id
  )
}

/** {@link canManageProject} for the signed-in user. */
export function useCanManageProject(
  project: Pick<Project, 'created_by_user_id' | 'can_mutate' | 'my_role'> | null | undefined,
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
  'Read-only: you have the viewer role in this project. Acting on incidents, changing destinations and rules, and retrying deliveries are done by an editor or owner.'

/**
 * The same notice for a surface that is not alerting: says what the role is and
 * who can act, without listing one page's jobs on another.
 */
export const VIEWER_READ_ONLY_HINT =
  'Read-only: you have the viewer role in this project. Changes here are made by an editor or owner.'

/**
 * May this user change who belongs to this project (add, re-role, remove)?
 *
 * Mirrors the gates on the member mutation routes of `/projects/{slug}/members`:
 * the manager check (an owner or admin of the organization, or the user who
 * created the project) AND `EditorUserDep`. So a creator also needs an editing
 * role: not a viewer member of this project (`my_role`) or otherwise read-only
 * in it (`can_mutate === false`). An org owner or admin passes outright. The server enforces the rule either way; this only decides which
 * controls are drawn.
 */
export function canManageProjectMembers(
  user: Pick<AuthUser, 'id' | 'role'> & Partial<Pick<AuthUser, 'orgs'>> | null | undefined,
  project: Pick<Project, 'created_by_user_id' | 'can_mutate' | 'my_role'> | null | undefined,
): boolean {
  if (!user) return false
  const role = activeOrgRole(user)
  if (isOwner(role)) return true
  if (!canWrite(role)) return false
  if (project?.my_role === 'viewer' || project?.can_mutate === false) return false
  return project?.created_by_user_id != null && project.created_by_user_id === user.id
}
