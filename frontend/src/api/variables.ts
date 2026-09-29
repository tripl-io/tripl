import { api, withBranch } from './client'
import type { PropertySchema, Variable, VariableListPage, VariableType, VariableValueContext } from '../types'

// The list endpoint pages server-side (offset/limit, server default 200). Every
// caller here wants the project's whole variable set in ONE round trip — the
// settings table, the event-form autocomplete and the events table all match
// `${var}` references against it — so ask for a page at the backend ceiling
// instead of silently getting the first 200.
export const VARIABLES_PAGE_LIMIT = 5000

/** Which variables to return: everything, only what something refers to, or only
 *  what nothing does. `unused` is decided by the backend's retirement predicate,
 *  not by a zero event count — see the note in VariablesTab for why that
 *  distinction matters under a select-all checkbox. */
export type VariableUsageFilter = 'all' | 'used' | 'unused'

const listPage = (
  slug: string,
  branchId?: string | null,
  {
    offset = 0,
    limit = VARIABLES_PAGE_LIMIT,
    usage = 'all',
  }: { offset?: number; limit?: number; usage?: VariableUsageFilter } = {},
) =>
  api.get<VariableListPage>(
    withBranch(
      `/projects/${slug}/variables?offset=${offset}&limit=${limit}&usage=${usage}`,
      branchId,
    ),
  )

export const variablesApi = {
  /** Page envelope — use when the caller needs `total` (truncation check, counts). */
  listPage,
  /** Items only, for callers that just need the set to match names against. */
  list: (slug: string, branchId?: string | null) =>
    listPage(slug, branchId).then(page => page.items),
  create: (
    slug: string,
    data: {
      name: string
      variable_type?: VariableType
      description?: string
      allowed_values?: string[]
      bindings?: string[]
      json_schema?: PropertySchema | null
    },
    branchId?: string | null,
  ) => api.post<Variable>(withBranch(`/projects/${slug}/variables`, branchId), data),
  update: (
    slug: string,
    id: string,
    data: {
      name?: string
      variable_type?: VariableType
      description?: string
      allowed_values?: string[]
      bindings?: string[]
      excluded_from_scans?: boolean
      /** null clears the schema. */
      json_schema?: PropertySchema | null
    },
    branchId?: string | null,
  ) => api.patch<Variable>(withBranch(`/projects/${slug}/variables/${id}`, branchId), data),
  /** Full per-event breakdown for ONE variable. Never fetch this per list row —
   * the list response already carries `event_names` and `sample_values`. */
  values: (slug: string, id: string, branchId?: string | null) =>
    api.get<VariableValueContext[]>(
      withBranch(`/projects/${slug}/variables/${id}/values`, branchId),
    ),
  /** Drop this variable's observed contexts, keeping the variable itself along
   * with its description, documented values, bindings, per-event overrides and
   * drift verdicts. Pass `contextId` to clear one (event, field) row instead of
   * all of them. `withBranch` appends with `&` when a query string is already
   * there, so the order below is safe. */
  clearValues: (slug: string, id: string, branchId?: string | null, contextId?: string) =>
    api.del(
      withBranch(
        `/projects/${slug}/variables/${id}/values${
          contextId ? `?context_id=${encodeURIComponent(contextId)}` : ''
        }`,
        branchId,
      ),
    ),
  del: (slug: string, id: string, branchId?: string | null) =>
    api.del(withBranch(`/projects/${slug}/variables/${id}`, branchId)),
  bulkUpdate: (
    slug: string,
    data: {
      variable_ids: string[]
      variable_type?: VariableType
      description?: string
      allowed_values_add?: string[]
      allowed_values_remove?: string[]
    },
    branchId?: string | null,
  ) => api.post<void>(withBranch(`/projects/${slug}/variables/bulk-update`, branchId), data),
  bulkDelete: (slug: string, variableIds: string[], branchId?: string | null) =>
    api.post<void>(withBranch(`/projects/${slug}/variables/bulk-delete`, branchId), {
      variable_ids: variableIds,
    }),
}
