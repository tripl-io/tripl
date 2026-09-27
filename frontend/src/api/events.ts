import { api, withBranch } from './client'
import type {
  Event,
  EventChange,
  EventIdentityHoldersResponse,
  EventListResponse,
  EventMutationResponse,
} from '../types'
import type { components, operations } from '../types/api.gen'
import type { ImplementationTicket } from '../types/tracker'
import type { EventMigration } from '../types/lifecycle'

type Schemas = components['schemas']
type ListQuery = NonNullable<
  operations['list_events_api_v1_projects__slug__events_get']['parameters']['query']
>

/**
 * The list endpoint's query parameters, derived from the generated OpenAPI
 * types rather than restated by hand. The hand-written copy drifted: it had no
 * `has_open_questions`, so the toolbar's "Open questions" filter reached the URL
 * and saved views but never the server, and the list came back unfiltered (EVT-1).
 * `branch` travels separately through `withBranch`; `null` is the backend's
 * "absent", which callers express by omitting the key.
 *
 * About `limit`: omitting it does NOT mean "every event". No limit is emitted,
 * so the server's own default applies — `limit: int = Query(200, ge=1,
 * le=10000)` in backend/src/tripl/api/v1/events.py — and the caller gets a
 * silently truncated page with no signal that anything was left behind. That is
 * exactly how the variables tab's override picker came to offer only the first
 * 200 events of a larger project (tripl-46am). A caller that renders a roster
 * must pass a limit it chose and read `total` to say what it did not show.
 */
export type EventListParams = {
  [K in Exclude<keyof ListQuery, 'branch'>]?: NonNullable<ListQuery[K]>
}

/**
 * Serializes every key of `params`, so a parameter added to the generated type
 * cannot be accepted by `list` and then dropped on the way to the URL. Empty
 * strings and empty arrays read as "no filter", like the controls that set them.
 */
export function eventListSearchParams(params: EventListParams = {}): URLSearchParams {
  const sp = new URLSearchParams()
  for (const [key, value] of Object.entries(params) as [string, unknown][]) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) {
      for (const item of value) sp.append(key, String(item))
    } else {
      sp.set(key, String(value))
    }
  }
  return sp
}

/**
 * Request bodies, typed from the generated schemas. Fields the backend defaults
 * (description, tags, status, …) come out required in the generated type, so
 * only the two with no default stay required here.
 */
export type EventCreateBody = Pick<Schemas['EventCreate'], 'event_type_id' | 'name'>
  & Partial<Omit<Schemas['EventCreate'], 'event_type_id' | 'name'>>
export type EventUpdateBody = Schemas['EventUpdate']
export type EventBulkUpdateBody = Omit<Schemas['EventBulkUpdate'], 'event_ids'>
type EventMoveBody = Schemas['EventMove']

export const eventsApi = {
  /** `signal` cancels the request — pass react-query's from a `queryFn`, or
   *  an AbortController's when a newer lookup supersedes this one. */
  list: (
    slug: string,
    params?: EventListParams,
    branchId?: string | null,
    signal?: AbortSignal,
  ) => {
    const qs = eventListSearchParams(params).toString()
    const path = `/projects/${slug}/events${qs ? `?${qs}` : ''}`
    return api.get<EventListResponse>(withBranch(path, branchId), signal)
  },
  /**
   * Which of `names` an event of this type already holds as its scan identity,
   * by exact match and by the rule create refuses on (EVT-37). One request for
   * a whole list, where each name used to be a substring search of its own.
   */
  byNames: (
    slug: string,
    eventTypeId: string,
    names: readonly string[],
    branchId?: string | null,
    signal?: AbortSignal,
  ) => {
    const sp = new URLSearchParams({ event_type_id: eventTypeId })
    for (const name of names) sp.append('names', name)
    return api.get<EventIdentityHoldersResponse>(
      withBranch(`/projects/${slug}/events/by-names?${sp}`, branchId),
      signal,
    )
  },
  tags: (slug: string, branchId?: string | null) =>
    api.get<string[]>(withBranch(`/projects/${slug}/events/tags`, branchId)),
  get: (slug: string, id: string, branchId?: string | null) =>
    api.get<Event>(withBranch(`/projects/${slug}/events/${id}`, branchId)),
  /** Tracker tickets that named this event, oldest first, across every branch
   * that merged. Read-only: the backend writes them from the merge worker,
   * never from a client. A branch copy reads through to its main twin. */
  implementationTickets: (slug: string, id: string, branchId?: string | null) =>
    api.get<ImplementationTicket[]>(
      withBranch(`/projects/${slug}/events/${id}/implementation-tickets`, branchId),
    ),
  /** A deprecated event's 7-day daily average next to its successor's (#258). */
  migration: (slug: string, id: string, branchId?: string | null, signal?: AbortSignal) =>
    api.get<EventMigration>(
      withBranch(`/projects/${slug}/events/${id}/migration`, branchId),
      signal,
    ),
  create: (slug: string, data: EventCreateBody, branchId?: string | null) =>
    api.post<EventMutationResponse>(withBranch(`/projects/${slug}/events`, branchId), data),
  /** `superseded_by_event_id` is update-only: a brand-new event has no
   *  predecessor to name, so `EventCreate` does not accept it. */
  update: (slug: string, id: string, data: EventUpdateBody, branchId?: string | null) =>
    api.patch<EventMutationResponse>(withBranch(`/projects/${slug}/events/${id}`, branchId), data),
  del: (slug: string, id: string, branchId?: string | null) =>
    api.del(withBranch(`/projects/${slug}/events/${id}`, branchId)),
  /**
   * `name` is ignored where a scan rule names the event type: the server
   * generates it from the field values, exactly as the single create does, so
   * both doors author the same event for the same payload.
   */
  bulkCreate: (slug: string, data: EventCreateBody[], branchId?: string | null) =>
    api.post<Event[]>(withBranch(`/projects/${slug}/events/bulk`, branchId), data),
  bulkDelete: (slug: string, eventIds: string[], branchId?: string | null) =>
    api.post<void>(withBranch(`/projects/${slug}/events/bulk-delete`, branchId), { event_ids: eventIds }),
  bulkUpdate: (
    slug: string,
    eventIds: string[],
    data: EventBulkUpdateBody,
    branchId?: string | null,
  ) => api.post<void>(
    withBranch(`/projects/${slug}/events/bulk-update`, branchId),
    { event_ids: eventIds, ...data },
  ),
  move: (slug: string, id: string, data: EventMoveBody, branchId?: string | null) =>
    api.patch<Event>(withBranch(`/projects/${slug}/events/${id}/move`, branchId), data),
  reorder: (slug: string, eventIds: string[], branchId?: string | null) =>
    api.patch<Event[]>(withBranch(`/projects/${slug}/events/reorder`, branchId), { event_ids: eventIds }),
  history: (slug: string, eventId: string, branchId?: string | null) =>
    api.get<EventChange[]>(withBranch(`/projects/${slug}/events/${eventId}/history`, branchId)),
}
