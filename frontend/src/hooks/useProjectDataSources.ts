import { useContext, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { dataSourcesApi } from '@/api/dataSources'
import { ActiveProjectContext } from '@/components/active-project-context'
import type { ErrorFeedbackMeta } from '@/lib/errorFeedback'
import { dataSourcesKey } from '@/lib/queryKeys'
import type { DataSource } from '@/types'

/**
 * Sources a project surface may offer: workspace-wide ones (`project_id` null)
 * and the ones owned by this project.
 *
 * `GET /data-sources` also returns sources owned by OTHER projects — a demo
 * project's synthetic warehouse, for one — and `DataSourceResponse.project_id`
 * exists so project surfaces can leave those out (backend
 * schemas/data_source.py). Without the filter the New scan, New metric and New
 * fact table pickers offered another project's warehouse, which the server then
 * refused on save (services/data_source_scope.py), and "no data sources" checks
 * counted it. Same rule as the Overview's Source-health rail.
 *
 * Outside the app shell there is no active project, and the list is unfiltered.
 */
export function filterProjectDataSources(
  sources: DataSource[],
  projectId: string | undefined,
): DataSource[] {
  if (!projectId) return sources
  return sources.filter(source => source.project_id == null || source.project_id === projectId)
}

/**
 * The workspace data-source query, scoped to the active project. `data` stays
 * undefined until the list has loaded, so a caller can tell "none" from "not
 * yet" through `isSuccess` (or `isLoading`, for a page that waits on it).
 *
 * `meta` is the query's error-feedback meta: `SILENT_ERROR_META` for a caller
 * that renders the failure itself.
 */
export function useProjectDataSources({ meta }: { meta?: ErrorFeedbackMeta } = {}) {
  const projectId = useContext(ActiveProjectContext)?.id
  const { data, isSuccess, isLoading, isError, error, refetch } = useQuery({
    queryKey: dataSourcesKey(),
    queryFn: () => dataSourcesApi.list(),
    meta,
  })
  const scoped = useMemo(
    () => (data ? filterProjectDataSources(data, projectId) : undefined),
    [data, projectId],
  )
  return { data: scoped, isSuccess, isLoading, isError, error, refetch }
}
