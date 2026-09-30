import { createContext, useContext } from 'react'

import type { OrgMembership } from '@/types'

/**
 * The organization the app acts in (F20 PR7), resolved by `ActiveOrgProvider`
 * from the URL's `/o/:org`, else the last organization used, else the user's
 * first. `slug` is `null` while none is known (no session, a user in none).
 */
export interface ActiveOrgValue {
  slug: string | null
  /** The user's membership there; `null` when they hold none (a foreign link). */
  membership: OrgMembership | null
  /** Every organization the user belongs to. */
  orgs: readonly OrgMembership[]
}

const NO_ORGS: readonly OrgMembership[] = []

export const ActiveOrgContext = createContext<ActiveOrgValue>({
  slug: null,
  membership: null,
  orgs: NO_ORGS,
})

/** The active organization. Outside the provider: none. */
export function useActiveOrg(): ActiveOrgValue {
  return useContext(ActiveOrgContext)
}
