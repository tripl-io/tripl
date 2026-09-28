import { useQueries } from '@tanstack/react-query'
import { Navigate, useLocation, useParams } from 'react-router-dom'

import { orgsApi } from '@/api/orgs'
import { PageSkeleton } from '@/components/states/skeletons'
import { orgHomePath, resolveLegacyProjectOrg } from '@/lib/activeOrg'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { orgProjectsKey } from '@/lib/queryKeys'
import type { OrgMembership } from '@/types'

/**
 * The half of the legacy `/p/:slug` redirect that someone in several
 * organizations needs: ask each organization for its projects, then go to the
 * one that holds the slug ({@link resolveLegacyProjectOrg}). Loaded on demand —
 * see `LegacyProjectRoute`.
 */
export function MultiOrgLegacyRedirect({ orgs }: { orgs: readonly OrgMembership[] }) {
  const location = useLocation()
  const { slug } = useParams<{ slug: string }>()
  const holdings = useQueries({
    queries: orgs.map((org) => ({
      queryKey: orgProjectsKey(org.slug),
      queryFn: () => orgsApi.projects(org.slug),
      meta: SILENT_ERROR_META,
    })),
  })
  if (holdings.some((query) => query.isPending)) {
    return <PageSkeleton variant="list" label="Opening the project…" />
  }
  const holders = orgs.filter((_, index) => holdings[index]?.data?.some((project) => project.slug === slug))
  const org = resolveLegacyProjectOrg(orgs, holders) ?? orgs[0]?.slug ?? null
  return (
    <Navigate
      to={`${orgHomePath(org)}${location.pathname}${location.search}${location.hash}`}
      replace
    />
  )
}
