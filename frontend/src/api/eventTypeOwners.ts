import { api } from './client'
import type { EventTypeOwner } from '../types'

export const eventTypeOwnersApi = {
  /** Every owner of every live event type in the project, in one request;
   * group them by `event_type_id`. */
  listForProject: (slug: string) =>
    api.get<EventTypeOwner[]>(`/projects/${slug}/event-type-owners`),
  list: (slug: string, eventTypeId: string) =>
    api.get<EventTypeOwner[]>(`/projects/${slug}/event-types/${eventTypeId}/owners`),
  add: (slug: string, eventTypeId: string, userId: string) =>
    api.post<EventTypeOwner>(
      `/projects/${slug}/event-types/${eventTypeId}/owners`,
      { user_id: userId },
    ),
  remove: (slug: string, eventTypeId: string, ownerId: string) =>
    api.del(`/projects/${slug}/event-types/${eventTypeId}/owners/${ownerId}`),
}
