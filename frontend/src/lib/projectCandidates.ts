import type { ProjectMember, UserListItem } from '@/types'

/** Someone who can be picked as a reviewer or an event-type owner in a project. */
export interface ProjectCandidate {
  user_id: string
  name: string | null
  email: string
}

/**
 * Who a per-project picker (branch reviewers, event-type owners) may offer.
 *
 * The project's members, plus every instance owner: an owner sees and writes
 * every project without ever holding a member row (tripl-vefw), so the members
 * list alone would leave them out, and the server accepts them. Anyone else on
 * the roster cannot see the project, so is not offered.
 *
 * De-duplicated by user id, members first in their own order, then the owners
 * who are not already listed, in roster order.
 */
export function projectCandidates(
  members: readonly ProjectMember[] | null | undefined,
  users: readonly UserListItem[] | null | undefined,
): ProjectCandidate[] {
  const seen = new Set<string>()
  const out: ProjectCandidate[] = []
  for (const m of members ?? []) {
    if (seen.has(m.user_id)) continue
    seen.add(m.user_id)
    out.push({ user_id: m.user_id, name: m.name, email: m.email })
  }
  for (const u of users ?? []) {
    if (u.role !== 'owner' || seen.has(u.id)) continue
    seen.add(u.id)
    out.push({ user_id: u.id, name: u.name, email: u.email })
  }
  return out
}
