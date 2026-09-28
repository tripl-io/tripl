import { useContext, useEffect, useMemo, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'

import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { ActiveOrgContext, type ActiveOrgValue } from '@/components/active-org-context'
import {
  orgFromLocation,
  pickActiveOrg,
  readLastOrgSlug,
  setCurrentOrgSlug,
  writeLastOrgSlug,
} from '@/lib/activeOrg'
import { activeOrgRole } from '@/lib/permissions'
import type { OrgMembership } from '@/types'

const NO_ORGS: readonly OrgMembership[] = []

/**
 * Decides which organization the app acts in and hands it to everything below
 * (F20 PR7). Mounted inside the auth provider and the router, above every route.
 *
 * It writes the module value (lib/activeOrg.ts) WHILE it renders: the API
 * client, the query keys and the link builders read it synchronously, and the
 * pages below render after this does, so each reads the organization its own
 * URL names. The value is a pure function of the URL, the session and the
 * remembered organization, so a render React throws away writes nothing wrong.
 *
 * It also re-provides the auth context with `user.role` set to the role in THIS
 * organization: `/auth/me` answers `role` for the default organization, and the
 * owner checks across the app read `user.role`. One override here keeps them
 * all answering for the organization on screen.
 */
export function ActiveOrgProvider({ children }: { children: ReactNode }) {
  const auth = useContext(AuthContext)
  const { pathname, search } = useLocation()
  const user = auth?.user ?? null
  const orgs = user?.orgs ?? NO_ORGS
  // `/o/:org/…`, or the `?org=` of a settings address.
  const urlOrg = orgFromLocation(pathname, search)
  const slug = pickActiveOrg(urlOrg, orgs, readLastOrgSlug())
  setCurrentOrgSlug(slug)

  // Remember an organization the user opened by address, once it is one of
  // theirs, so `/settings/*` and legacy links act in it next time — in this
  // tab first (see readLastOrgSlug).
  useEffect(() => {
    if (urlOrg && orgs.some((org) => org.slug === urlOrg)) writeLastOrgSlug(urlOrg)
  }, [urlOrg, orgs])

  const membership = orgs.find((org) => org.slug === slug) ?? null
  const value = useMemo<ActiveOrgValue>(() => ({ slug, membership, orgs }), [slug, membership, orgs])

  const role = activeOrgRole(user, slug)
  const scopedAuth = useMemo<AuthContextValue | null>(() => {
    if (!auth?.user || auth.user.role === role) return auth
    return { ...auth, user: { ...auth.user, role } }
  }, [auth, role])

  return (
    <ActiveOrgContext.Provider value={value}>
      <AuthContext.Provider value={scopedAuth}>{children}</AuthContext.Provider>
    </ActiveOrgContext.Provider>
  )
}
