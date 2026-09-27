import { useContext, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Chip } from '@/components/primitives/chip'
import { Button } from '@/components/ui/button'
import { Loader2, MessageCircle, Trash2 } from 'lucide-react'
import { formatDateTime } from '@/lib/datetime'
import { isThreadUnanswered, threadStateLabel } from '@/components/commentThreadState'
import type { EventCommentAction, EventCommentStatus } from '@/types'
import { useCanWriteProject, useIsOwner } from '@/lib/permissions'
import { eventsRootKey, projectMembersQueryOptions, usersKey } from '@/lib/queryKeys'
import { usersApi } from '@/api/users'
import { projectCandidates } from '@/lib/projectCandidates'
import { useConfirm } from '@/hooks/useConfirm'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { AuthContext } from '@/components/auth-context'
import { ReadOnlyNotice } from '@/components/states/read-only-notice'
import { MentionComposer } from '@/components/mention-composer'
import { MentionText, type MentionPeople } from '@/components/mention-text'
import type { MentionCandidate } from '@/lib/mentions'

/**
 * The shape the thread renders. Both anchors — a photo and an event — keep
 * their comments in the same table, so this is the same row either way.
 */
export interface ThreadComment {
  id: string
  parent_id: string | null
  body: string
  created_at: string
  /** Null once the author's account is gone — the FK is SET NULL, because a
   *  deleted account must not take the discussion with it. */
  user_id?: string | null
  /** Resolution state, on the event thread only. The branch-review thread has
   *  no such columns, so these stay optional and the controls stay hidden
   *  unless a caller passes `onAction`. */
  status?: EventCommentStatus
  snoozed_until?: string | null
}

export interface CommentThreadProps {
  /** TanStack key for the list; posting and deleting invalidate it. */
  queryKey: readonly unknown[]
  list: () => Promise<ThreadComment[]>
  create: (body: string, parentId: string | null) => Promise<unknown>
  remove: (commentId: string) => Promise<unknown>
  heading?: string
  emptyText?: string
  /** Keeps the composer's label unique when two threads share a page. */
  composerId?: string
  /** Text the composer opens with.
   *
   *  Read once, at mount. It exists for handing a note ACROSS a navigation:
   *  a question drafted while creating an event is posted the moment the event
   *  exists, and if that post fails the author lands here with the words they
   *  wrote still in the box rather than losing them (tripl-htfn.1). Making it
   *  live would fight the reader for their own textarea. */
  initialBody?: string
  className?: string
  /** Resolves a comment to a display name. A callback rather than a roster
   *  map, so the component stays free of the users query and each caller
   *  resolves however it already does. Without it the thread stays anonymous,
   *  which is what it was. */
  authorName?: (comment: ThreadComment) => string
  /** Fired after a comment is posted. The branch panel's demo scenario marks
   *  its last step here, and losing that would strand the chapter. */
  onCreated?: () => void
  /** Resolve / snooze / reopen one thread. Omitted by the branch-review thread,
   *  whose table has no resolution columns — without it no control renders and
   *  the component behaves exactly as it did. */
  onAction?: (
    commentId: string,
    action: EventCommentAction,
    snoozedUntil?: string,
  ) => Promise<unknown>
  /** The project whose members the composer offers after `@` (#259). Without
   *  it the composer is a plain textarea; mentions in bodies render as chips
   *  either way. */
  mentionSlug?: string
  /** Drawn at the right of the heading: the event thread's mute toggle (#259). */
  headerAction?: ReactNode
}

/** How long "snooze" parks a thread. A week is long enough to stop the nag and
 *  short enough that the question comes back while it still matters; the API
 *  takes any date, so a caller that wants a picker can have one later. */
const SNOOZE_DAYS = 7

/**
 * One comment thread, anchored by whatever the callbacks point at.
 *
 * The composer is a div with a button rather than a `<form>` on purpose. A
 * thread has to be mountable inside the event form, and a nested `<form>` is
 * invalid HTML — the browser drops the inner one, so the composer's submit
 * would save the EVENT instead of posting the comment. Nothing is lost: a
 * textarea never submitted on Enter anyway.
 */
export function CommentThread({
  queryKey,
  list,
  create,
  remove,
  heading = 'Comments',
  emptyText = 'No comments yet. Start the thread.',
  composerId = 'comment-body',
  initialBody = '',
  className = 'flex h-full min-h-[400px] flex-col rounded-md border bg-card p-3',
  authorName,
  onCreated,
  onAction,
  mentionSlug,
  headerAction,
}: CommentThreadProps) {
  const queryClient = useQueryClient()
  // Every comment write (post, reply, resolve, delete) is EditorUserDep on the
  // backend, so a viewer reads the thread and is offered none of them.
  const canWrite = useCanWriteProject()
  const isOwner = useIsOwner()
  const currentUserId = useContext(AuthContext)?.user?.id ?? null
  const { confirm, dialog } = useConfirm()
  const [body, setBody] = useState(initialBody)
  const [replyTo, setReplyTo] = useState<string | null>(null)

  const commentsQuery = useQuery({ queryKey: queryKey, queryFn: list })
  // The @ list: the project's members plus the organization's owners and
  // admins, who see every project without a member row (tripl-vefw) — the server notifies exactly
  // those. Members are fetched only for someone who can post.
  const membersQuery = useQuery({
    ...projectMembersQueryOptions(mentionSlug),
    enabled: !!mentionSlug && canWrite,
    meta: SILENT_ERROR_META,
    staleTime: 60_000,
  })
  // The roster names the owners for the list and the people behind the chips
  // for every reader (`GET /users` is open to any signed-in user). Shared with
  // useUsersById, so a page that already resolves authors pays nothing more.
  const usersQuery = useQuery({
    queryKey: usersKey(),
    queryFn: () => usersApi.list(),
    enabled: !!mentionSlug,
    meta: SILENT_ERROR_META,
    staleTime: 60_000,
  })
  const members = Array.isArray(membersQuery.data) ? membersQuery.data : []
  const users = Array.isArray(usersQuery.data) ? usersQuery.data : []
  const mentionCandidates: MentionCandidate[] | undefined = mentionSlug
    ? projectCandidates(members, users)
        .filter(candidate => candidate.user_id !== currentUserId)
        .map(candidate => ({
          userId: candidate.user_id,
          name: candidate.name || candidate.email,
          email: candidate.email,
        }))
    : undefined
  // Who a chip's user id is now: a renamed member shows their current name,
  // an id nobody knows keeps the name stored in the token.
  const mentionPeople = new Map<string, { name: string; email: string }>()
  for (const u of users) mentionPeople.set(u.id.toLowerCase(), { name: u.name || u.email, email: u.email })
  for (const m of members) mentionPeople.set(m.user_id.toLowerCase(), { name: m.name || m.email, email: m.email })

  // A thread with resolution state (the event discussion — the one caller that
  // passes `onAction`) feeds the catalog's "?N" badge and its "Open questions"
  // filter. A post opens a question and a delete can close one, so both have to
  // reach the list the way resolve always did (EVT-29).
  const refreshCatalog = () => {
    if (onAction) void queryClient.invalidateQueries({ queryKey: eventsRootKey() })
  }

  const createMut = useMutation({
    mutationFn: () => create(body.trim(), replyTo),
    onSuccess: () => {
      setBody('')
      setReplyTo(null)
      void queryClient.invalidateQueries({ queryKey: queryKey })
      refreshCatalog()
      onCreated?.()
    },
  })

  const deleteMut = useMutation({
    // Its failure is said inline, under the thread.
    meta: SILENT_ERROR_META,
    mutationFn: (commentId: string) => remove(commentId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKey })
      refreshCatalog()
    },
  })

  const actionMut = useMutation({
    mutationFn: ({
      commentId,
      action,
      snoozedUntil,
    }: {
      commentId: string
      action: EventCommentAction
      snoozedUntil?: string
    }) => onAction!(commentId, action, snoozedUntil),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKey })
      // The catalog's open-question count and filter read the same threads, so
      // a resolve here has to reach the list the user came from.
      void queryClient.invalidateQueries({ queryKey: eventsRootKey() })
    },
  })

  // Array.isArray, not `?? []`: an error body or a stubbed fetch answering the
  // wrong shape must render an empty thread, not throw out of the page that
  // hosts it. This panel is a sidecar — it never gets to take the form down.
  const comments = Array.isArray(commentsQuery.data) ? commentsQuery.data : []
  const topLevel = comments.filter(comment => comment.parent_id === null)
  const repliesByParent = new Map<string, ThreadComment[]>()
  for (const comment of comments) {
    if (comment.parent_id) {
      const siblings = repliesByParent.get(comment.parent_id) ?? []
      siblings.push(comment)
      repliesByParent.set(comment.parent_id, siblings)
    }
  }

  // Delete used to fire on one click, on anybody's comment, and a parent took
  // its replies with it (EVT-29). An editor deletes their own words; the owner
  // moderates. A row whose shape carries no author (`user_id` absent, not null)
  // cannot be attributed, so it keeps the editor's control it always had.
  const canDelete = (comment: ThreadComment) =>
    canWrite
    && (isOwner || comment.user_id === undefined || (currentUserId !== null && comment.user_id === currentUserId))

  const confirmDelete = async (commentId: string) => {
    const replies = repliesByParent.get(commentId)?.length ?? 0
    const ok = await confirm({
      title: 'Delete comment',
      message:
        replies > 0
          ? `Delete this comment and its ${replies === 1 ? 'reply' : `${replies} replies`}? This cannot be undone.`
          : 'Delete this comment? This cannot be undone.',
      confirmLabel: 'Delete',
      variant: 'danger',
    })
    if (ok) deleteMut.mutate(commentId)
  }

  const submit = () => {
    if (!body.trim() || createMut.isPending) return
    createMut.mutate()
  }

  return (
    <div className={className}>
      {dialog}
      <div className="mb-2 flex items-center gap-2 text-body font-semibold">
        <MessageCircle className="h-4 w-4 text-fg-tertiary" />
        {heading}
        <span className="text-body-sm font-normal text-fg-tertiary">({comments.length})</span>
        {headerAction && <div className="ml-auto flex items-center">{headerAction}</div>}
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto pr-1 text-body">
        {commentsQuery.isLoading ? (
          <div className="text-body-sm text-fg-tertiary">Loading…</div>
        ) : topLevel.length === 0 ? (
          <div className="text-body-sm text-fg-tertiary">{emptyText}</div>
        ) : (
          topLevel.map(comment => (
            <CommentItem
              key={comment.id}
              comment={comment}
              replies={repliesByParent.get(comment.id) ?? []}
              onReply={canWrite ? () => setReplyTo(comment.id) : undefined}
              canDelete={canDelete}
              onDelete={id => void confirmDelete(id)}
              deletePending={deleteMut.isPending}
              replyingTo={replyTo}
              authorName={authorName}
              mentionPeople={mentionPeople}
              onAction={
                onAction && canWrite
                  ? (action, snoozedUntil) =>
                      actionMut.mutate({ commentId: comment.id, action, snoozedUntil })
                  : undefined
              }
              actionPending={actionMut.isPending}
            />
          ))
        )}
      </div>
      {deleteMut.isError && (
        <p role="alert" className="mt-2 text-body-sm text-destructive">
          Could not delete the comment: {deleteMut.error instanceof Error ? deleteMut.error.message : 'unknown error'}
        </p>
      )}
      {!canWrite ? (
        <ReadOnlyNotice className="mt-3">Only editors and owners can comment.</ReadOnlyNotice>
      ) : (
        <div className="mt-3 flex flex-col gap-2 border-t pt-3">
          {replyTo && (
            <div className="flex items-center justify-between rounded-sm bg-muted px-2 py-1 text-body-sm">
              <span>Replying to comment</span>
              <button
                type="button"
                className="text-fg-tertiary hover:text-foreground"
                onClick={() => setReplyTo(null)}
              >
                cancel
              </button>
            </div>
          )}
          <label htmlFor={composerId} className="sr-only">Write a comment</label>
          {/* Enter belongs to the text — a comment is often several lines — so
              posting is the shortcut every chat box uses, Cmd/Ctrl+Enter. */}
          <MentionComposer
            id={composerId}
            value={body}
            onChange={setBody}
            onSubmit={submit}
            candidates={mentionCandidates}
            placeholder={mentionCandidates ? 'Write a comment… (@ to mention)' : 'Write a comment…'}
            className="min-h-[60px] w-full rounded-md border bg-background px-2 py-1 text-body"
          />
          <div className="flex items-center justify-end gap-2">
            <Button
              type="button"
              size="sm"
              onClick={submit}
              disabled={!body.trim() || createMut.isPending}
            >
              {createMut.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
              {replyTo ? 'Reply' : 'Comment'}
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}

function CommentItem({
  comment,
  replies,
  onReply,
  canDelete,
  onDelete,
  deletePending,
  replyingTo,
  authorName,
  mentionPeople,
  onAction,
  actionPending,
}: {
  comment: ThreadComment
  replies: ThreadComment[]
  /** Omitted for a viewer: replying is an editor action. */
  onReply?: () => void
  /** Whether this reader may delete a given comment (its author, or the owner). */
  canDelete: (comment: ThreadComment) => boolean
  onDelete: (id: string) => void
  deletePending?: boolean
  replyingTo: string | null
  authorName?: (comment: ThreadComment) => string
  /** Current names (and emails) behind mention chips, by lower-case user id. */
  mentionPeople?: MentionPeople
  onAction?: (action: EventCommentAction, snoozedUntil?: string) => void
  actionPending?: boolean
}) {
  const unanswered = isThreadUnanswered(comment)
  const stateLabel = threadStateLabel(comment)
  const snooze = () => {
    const until = new Date()
    until.setDate(until.getDate() + SNOOZE_DAYS)
    onAction?.('snooze', until.toISOString())
  }
  return (
    <div className="space-y-2">
      <div className="rounded-md border bg-muted/30 px-2 py-1.5">
        <div className="flex items-center justify-between gap-2 text-body-sm text-fg-tertiary">
          <span>
            {authorName ? `${authorName(comment)} · ` : ''}
            {formatDateTime(comment.created_at)}
          </span>
          <div className="flex items-center gap-2">
            {/* A thread's state is a status: the shared pill (DS-6). */}
            {stateLabel && <Chip size="xs">{stateLabel}</Chip>}
            {onAction && (unanswered ? (
              <>
                <button
                  type="button"
                  className="hover:text-foreground"
                  disabled={actionPending}
                  onClick={() => onAction('resolve')}
                >
                  resolve
                </button>
                <button
                  type="button"
                  className="hover:text-foreground"
                  disabled={actionPending}
                  onClick={snooze}
                >
                  snooze
                </button>
              </>
            ) : (
              <button
                type="button"
                className="hover:text-foreground"
                disabled={actionPending}
                onClick={() => onAction('reopen')}
              >
                reopen
              </button>
            ))}
            {onReply && (
              <button type="button" className="hover:text-foreground" onClick={onReply}>
                {replyingTo === comment.id ? 'replying…' : 'reply'}
              </button>
            )}
            {canDelete(comment) && (
              <button
                type="button"
                aria-label="Delete comment"
                className="hover:text-destructive"
                disabled={deletePending}
                onClick={() => onDelete(comment.id)}
              >
                <Trash2 className="h-3 w-3" aria-hidden="true" />
              </button>
            )}
          </div>
        </div>
        <MentionText body={comment.body} people={mentionPeople} />
      </div>
      {replies.length > 0 && (
        <div className="ml-4 space-y-2 border-l pl-3">
          {replies.map(reply => (
            <div key={reply.id} className="rounded-md border bg-muted/20 px-2 py-1.5">
              <div className="flex items-center justify-between gap-2 text-body-sm text-fg-tertiary">
                <span>
                  {authorName ? `${authorName(reply)} · ` : ''}
                  {formatDateTime(reply.created_at)}
                </span>
                {canDelete(reply) && (
                  <button
                    type="button"
                    aria-label="Delete comment"
                    className="hover:text-destructive"
                    disabled={deletePending}
                    onClick={() => onDelete(reply.id)}
                  >
                    <Trash2 className="h-3 w-3" aria-hidden="true" />
                  </button>
                )}
              </div>
              <MentionText body={reply.body} people={mentionPeople} />
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
