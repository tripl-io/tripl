import type { EventCommentAction, EventPhotoComment } from '../types'
import { api } from './client'

/**
 * The discussion on an event itself, as opposed to on one of its attachments.
 *
 * No `branch` argument, deliberately: an event has ONE discussion, and the
 * server resolves a branch copy to its twin on main before reading or writing.
 * Passing a branch here would invite two threads on one conversation.
 *
 * On the shared client, not raw `fetch`: that kept a private copy of error
 * unwrapping and lost the 401 re-auth prompt, the `X-Request-ID` a support
 * reference needs, and `ApiError`.
 */
export const eventCommentsApi = {
  list: (slug: string, eventId: string): Promise<EventPhotoComment[]> =>
    api.get<EventPhotoComment[]>(`/projects/${slug}/events/${eventId}/comments`),

  create: (
    slug: string,
    eventId: string,
    body: string,
    parentId: string | null = null,
  ): Promise<EventPhotoComment> =>
    api.post<EventPhotoComment>(`/projects/${slug}/events/${eventId}/comments`, {
      body,
      parent_id: parentId,
    }),

  /**
   * Resolve, snooze or reopen one thread.
   *
   * An `/actions` sub-resource rather than a PATCH on the comment: the body
   * names an intent and the server decides which of the five resolution columns
   * move. Refused on a reply — the thread is the unit that gets answered.
   */
  action: (
    slug: string,
    eventId: string,
    commentId: string,
    action: EventCommentAction,
    snoozedUntil?: string,
  ): Promise<EventPhotoComment> =>
    api.post<EventPhotoComment>(
      `/projects/${slug}/events/${eventId}/comments/${commentId}/actions`,
      snoozedUntil ? { action, snoozed_until: snoozedUntil } : { action },
    ),

  remove: async (slug: string, eventId: string, commentId: string): Promise<void> => {
    await api.del<void>(`/projects/${slug}/events/${eventId}/comments/${commentId}`)
  },
}
