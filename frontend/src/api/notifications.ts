import { api } from './client'
import type {
  MarkNotificationsReadResponse,
  NotificationPage,
  NotificationPrefs,
  NotificationPrefsUpdate,
  SubscriptionEntityType,
  SubscriptionState,
  UnreadNotificationCount,
} from '../types'

export interface NotificationListParams {
  unread?: boolean
  limit?: number
  cursor?: string | null
}

/**
 * The reader's own notifications, across every project they are a member of
 * (#259). The server filters by membership at read time too, so a row from a
 * project the reader has since left never comes back.
 */
export const notificationsApi = {
  list: (params: NotificationListParams = {}, signal?: AbortSignal) => {
    const query = new URLSearchParams()
    if (params.unread) query.set('unread', 'true')
    if (params.limit !== undefined) query.set('limit', String(params.limit))
    if (params.cursor) query.set('cursor', params.cursor)
    const qs = query.toString()
    return api.get<NotificationPage>(`/me/notifications${qs ? `?${qs}` : ''}`, signal)
  },
  unreadCount: (signal?: AbortSignal) =>
    api.get<UnreadNotificationCount>('/me/notifications/unread-count', signal),
  markRead: (ids: string[]) =>
    api.post<MarkNotificationsReadResponse>('/me/notifications/read', { ids }),
  markAllRead: () =>
    api.post<MarkNotificationsReadResponse>('/me/notifications/read', { all: true }),
  getPrefs: (signal?: AbortSignal) => api.get<NotificationPrefs>('/me/notification-prefs', signal),
  updatePrefs: (patch: NotificationPrefsUpdate) =>
    api.patch<NotificationPrefs>('/me/notification-prefs', patch),
}

const subscriptionPath = (slug: string, entityType: SubscriptionEntityType, entityId: string) =>
  `/projects/${slug}/subscriptions/${entityType}/${entityId}`

/** Watch / unwatch / mute one entity for the reader (#259). */
export const subscriptionsApi = {
  get: (slug: string, entityType: SubscriptionEntityType, entityId: string, signal?: AbortSignal) =>
    api.get<SubscriptionState>(subscriptionPath(slug, entityType, entityId), signal),
  watch: (slug: string, entityType: SubscriptionEntityType, entityId: string) =>
    api.put<SubscriptionState>(subscriptionPath(slug, entityType, entityId), {}),
  unwatch: (slug: string, entityType: SubscriptionEntityType, entityId: string) =>
    api.del<SubscriptionState>(subscriptionPath(slug, entityType, entityId)),
  setMuted: (slug: string, entityType: SubscriptionEntityType, entityId: string, muted: boolean) =>
    api.patch<SubscriptionState>(subscriptionPath(slug, entityType, entityId), { muted }),
}
