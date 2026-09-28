import { Suspense } from 'react'
import { Navigate, Outlet, useLocation } from 'react-router-dom'

import { useAuth } from '@/components/auth-context'
import { PageSkeleton } from '@/components/states/skeletons'
import { orgHomePath } from '@/lib/activeOrg'
import { lazyWithReload } from '@/lib/lazyWithReload'

// Only someone in several organizations has to ask which one holds the slug;
// that part loads on demand, off everyone's first load.
const MultiOrgLegacyRedirect = lazyWithReload(() =>
  import('./legacy-project-resolver').then((m) => ({ default: m.MultiOrgLegacyRedirect })),
)

/**
 * A legacy `/p/:slug/…` address (F20 PR7). Moves it, with its query string and
 * hash, to the organization that holds the project — see
 * `resolveLegacyProjectOrg`: the one organization of the user's whose project
 * list has the slug, else the default organization if they are in it, else
 * their only one. A user in no organization (a session from before
 * organizations) stays on the legacy address, which the server resolves in the
 * default organization.
 *
 * With one organization there is nothing to ask; with several, each is asked
 * for its projects first.
 */
export function LegacyProjectRoute() {
  const auth = useAuth()
  const location = useLocation()
  const orgs = auth.user?.orgs ?? []
  const [only] = orgs
  if (!only) return <Outlet />
  if (orgs.length === 1) {
    return <Navigate to={`${orgHomePath(only.slug)}${location.pathname}${location.search}${location.hash}`} replace />
  }
  return (
    <Suspense fallback={<PageSkeleton variant="list" label="Opening the project…" />}>
      <MultiOrgLegacyRedirect orgs={orgs} />
    </Suspense>
  )
}
