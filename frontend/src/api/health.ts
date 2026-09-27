import { api } from './client'
import type {
  EventHealth,
  EventHealthListResponse,
  EventTypeHealth,
  EventTypeHealthListResponse,
  ProjectHealthResponse,
} from '../types'

/**
 * The batch endpoint's cap; the catalog chunks its loaded ids to it. The ids
 * travel in the query string (about 41 bytes each), and a request line over
 * 8 KB is answered 414 by a proxy with nginx's default header buffers, so 150
 * ids (about 6.2 KB) keep every chunk under that. Must equal the backend's
 * MAX_HEALTH_IDS (api/v1/health.py).
 */
export const HEALTH_BATCH_MAX_IDS = 150

/**
 * Plan health score (F15, #268). Every call answers about the MAIN plan: the
 * endpoints take no branch.
 *
 * Its own module rather than methods on `eventsApi` / `projectsApi`, like
 * sourceFreshness.ts, so the page tests that mock those modules wholesale do
 * not lose these calls to the mock. The list readers accept a bare array or an
 * `{ items }` envelope.
 */
export const healthApi = {
  /** `GET /projects/{slug}/health/events?ids=…` — 1..150 ids. */
  events: async (slug: string, ids: readonly string[], signal?: AbortSignal): Promise<EventHealth[]> => {
    if (ids.length === 0) return []
    const sp = new URLSearchParams()
    for (const id of ids) sp.append('ids', id)
    const response = await api.get<EventHealthListResponse | EventHealth[]>(
      `/projects/${slug}/health/events?${sp.toString()}`,
      signal,
    )
    return Array.isArray(response) ? response : (response?.items ?? [])
  },

  /** `GET /projects/{slug}/events/{eventId}/health` — 404 off the scored population. */
  event: (slug: string, eventId: string, signal?: AbortSignal): Promise<EventHealth> =>
    api.get<EventHealth>(`/projects/${slug}/events/${eventId}/health`, signal),

  /** `GET /projects/{slug}/health/event-types`. */
  eventTypes: async (slug: string, signal?: AbortSignal): Promise<EventTypeHealth[]> => {
    const response = await api.get<EventTypeHealthListResponse | EventTypeHealth[]>(
      `/projects/${slug}/health/event-types`,
      signal,
    )
    return Array.isArray(response) ? response : (response?.items ?? [])
  },

  /** `GET /projects/{slug}/health?trend_days=N` (1..365). */
  project: (slug: string, trendDays = 30, signal?: AbortSignal): Promise<ProjectHealthResponse> =>
    api.get<ProjectHealthResponse>(`/projects/${slug}/health?trend_days=${trendDays}`, signal),
}
