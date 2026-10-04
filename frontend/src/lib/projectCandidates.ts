import { isOwner } from '@/lib/permissions'
import type { DefaultProjectRole, ProjectMember, UserListItem } from '@/types'

/** Someone who can be picked as a reviewer or an event-type owner in a project. */
export interface ProjectCandidate {
  user_id: string
  name: string | null
  email: string
}

/**
 * Who a per-project picker (branch reviewers, event-type owners, @mentions)
 * may offer: everyone who can see the project.
 *
 * - The project's members with a granting row (`editor` / `viewer`). A
 *   `'none'` row opts its member out of the project, whatever the default.
 * - Every owner and admin of the organization: they see and write every
 *   project of it without ever holding a member row (F20 PR4).
 * - When the organization's default access (`defaultRole`, F20 PR15) is not
 *   `'none'`, every other member of the roster without a row too: the default
 *   gives them the project.
 *
 * De-duplicated by user id, members first in their own order, then the roster
 * in its order.
 */
export function projectCandidates(
  members: readonly ProjectMember[] | null | undefined,
  users: readonly UserListItem[] | null | undefined,
  defaultRole: DefaultProjectRole = 'none',
): ProjectCandidate[] {
  const seen = new Set<string>()
  const optedOut = new Set<string>()
  const out: ProjectCandidate[] = []
  for (const m of members ?? []) {
    if (m.role === 'none') {
      optedOut.add(m.user_id)
      continue
    }
    if (seen.has(m.user_id)) continue
    seen.add(m.user_id)
    out.push({ user_id: m.user_id, name: m.name, email: m.email })
  }
  for (const u of users ?? []) {
    if (seen.has(u.id)) continue
    // An owner or admin always sees the project; anyone else only through the
    // default, and not past a 'none' row of their own.
    if (!isOwner(u.role) && (defaultRole === 'none' || optedOut.has(u.id))) continue
    seen.add(u.id)
    out.push({ user_id: u.id, name: u.name, email: u.email })
  }
  return out
}
