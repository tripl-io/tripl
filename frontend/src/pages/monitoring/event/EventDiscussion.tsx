import { eventCommentsApi } from '@/api/eventComments'
import { CommentThread } from '@/components/comment-thread'
import { ThreadMuteToggle } from '@/components/watch-button'
import { displayUser, useUsersById } from '@/hooks/useUsersById'
import { eventCommentsKey } from '@/lib/queryKeys'

/**
 * The event's discussion on its detail page. Viewers are sent here from the
 * edit URL, and the editor was the only place the thread was drawn, so
 * a viewer lost the one way to read it. Same thread and cache key as the edit
 * page; CommentThread hides the composer from anyone who cannot write.
 *
 * `initialBody` starts the composer with a draft — a tracking-bug verdict's
 * "Open a comment on the event" (#254). The composer reads it once, at mount,
 * so the caller remounts this (a new `key`) to hand over a new draft.
 */
export function EventDiscussion({
  slug,
  eventId,
  initialBody,
}: {
  slug: string
  eventId: string
  initialBody?: string
}) {
  const usersById = useUsersById()
  return (
    <CommentThread
      queryKey={eventCommentsKey(slug, eventId)}
      list={() => eventCommentsApi.list(slug, eventId)}
      create={(body, parentId) => eventCommentsApi.create(slug, eventId, body, parentId)}
      remove={commentId => eventCommentsApi.remove(slug, eventId, commentId)}
      onAction={(commentId, action, snoozedUntil) =>
        eventCommentsApi.action(slug, eventId, commentId, action, snoozedUntil)
      }
      authorName={comment => displayUser(usersById, comment.user_id)}
      heading="Discussion"
      emptyText="Nothing raised yet. Questions and notes here stay out of the spec."
      composerId="event-detail-discussion-body"
      initialBody={initialBody}
      className="flex flex-col rounded-card border bg-(--surface) p-4"
      // @ offers the project's members; a mention notifies them (#259).
      mentionSlug={slug}
      headerAction={<ThreadMuteToggle slug={slug} eventId={eventId} />}
    />
  )
}
