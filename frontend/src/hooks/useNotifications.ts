import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { notificationsApi } from '@/api/notifications'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import {
  myNotificationsListKey,
  myNotificationsRootKey,
  myNotificationsUnreadCountKey,
} from '@/lib/queryKeys'

/** How often the bell asks for the unread count; realtime is not required (#259). */
export const UNREAD_POLL_MS = 60_000

/** How many rows the bell's Notifications tab shows per page. */
export const NOTIFICATION_PAGE_SIZE = 20

/**
 * The bell's unread count: every minute, and again whenever the window
 * regains focus. A failure keeps the last answer (or none) — the bell is not
 * where a network error gets reported.
 */
export function useUnreadNotificationCount() {
  return useQuery({
    meta: SILENT_ERROR_META,
    queryKey: myNotificationsUnreadCountKey(),
    queryFn: ({ signal }) => notificationsApi.unreadCount(signal),
    refetchInterval: UNREAD_POLL_MS,
    refetchOnWindowFocus: true,
    staleTime: 15_000,
    select: data => (typeof data?.unread === 'number' ? data.unread : 0),
  })
}

/** The latest notifications, read and unread, newest first. */
export function useNotificationList(unreadOnly = false) {
  return useQuery({
    meta: SILENT_ERROR_META,
    queryKey: myNotificationsListKey(unreadOnly),
    queryFn: ({ signal }) =>
      notificationsApi.list({ unread: unreadOnly, limit: NOTIFICATION_PAGE_SIZE }, signal),
    staleTime: 15_000,
  })
}

/** Mark some notifications read, or all of them; refreshes the list and the count. */
export function useMarkNotificationsRead() {
  const qc = useQueryClient()
  return useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: (target: { ids: string[] } | { all: true }) =>
      'all' in target ? notificationsApi.markAllRead() : notificationsApi.markRead(target.ids),
    onSettled: () => qc.invalidateQueries({ queryKey: myNotificationsRootKey() }),
  })
}
