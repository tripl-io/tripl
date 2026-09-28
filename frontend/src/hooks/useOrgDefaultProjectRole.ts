import { useQuery } from '@tanstack/react-query'

import { orgsApi } from '@/api/orgs'
import { useActiveOrg } from '@/components/active-org-context'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { orgKey } from '@/lib/queryKeys'
import type { DefaultProjectRole } from '@/types'

/**
 * The active organization's default access to its projects (F20 PR15): what a
 * member with no membership row in a project gets there. Shares the cache
 * entry of Settings › Organization › Details, so a change there shows at once.
 *
 * `'none'` until it is known (no organization, still loading, or a failed
 * read): the conservative answer, which offers only the people the project's
 * own rows and the organization's owners and admins name. The server enforces
 * the real rule either way.
 */
export function useOrgDefaultProjectRole({ enabled = true }: { enabled?: boolean } = {}): DefaultProjectRole {
  const { slug } = useActiveOrg()
  const { data } = useQuery({
    queryKey: orgKey(slug ?? ''),
    queryFn: () => orgsApi.get(slug as string),
    enabled: enabled && !!slug,
    meta: SILENT_ERROR_META,
    staleTime: 60_000,
  })
  return data?.default_project_role ?? 'none'
}
