import { Link } from 'react-router-dom'
import {
  AtSign,
  CheckCheck,
  GitBranch,
  GitMerge,
  CircleHelp,
  MessageSquare,
  Reply,
  Sunset,
  Shapes,
  TrendingUp,
  UserCheck,
  type LucideIcon,
} from 'lucide-react'
import { Dot } from '@/components/primitives/dot'
import { useMarkNotificationsRead, useNotificationList } from '@/hooks/useNotifications'
import { formatRelativeTime } from '@/lib/datetime'
import { cn } from '@/lib/utils'
import type { AppNotification, NotificationKind } from '@/types'
import { withActiveOrg } from '@/lib/navigation'

const KIND_ICON: Record<NotificationKind, LucideIcon> = {
  comment: MessageSquare,
  reply: Reply,
  mention: AtSign,
  open_question: CircleHelp,
  signal: TrendingUp,
  branch_review_requested: GitBranch,
  branch_approved: UserCheck,
  branch_merged: GitMerge,
  lifecycle: Sunset,
  property_drift: Shapes,
}

/** Only an in-app path is followed; anything else opens the bell's owner nowhere. */
function safeInAppPath(url: string): string | null {
  // A server-written `/p/…` address opens under the active organization (F20 PR7).
  return url.startsWith('/') && !url.startsWith('//') ? withActiveOrg(url) : null
}

/**
 * The bell's Notifications tab (#259): what happened to the things the reader
 * watches, across their projects. A row opens its page and marks itself read;
 * "Mark all read" clears the badge.
 */
export function NotificationsInbox({
  unreadCount,
  onNavigate,
}: {
  unreadCount: number
  /** Closes the popover when a row is followed. */
  onNavigate?: () => void
}) {
  const listQuery = useNotificationList()
  const markRead = useMarkNotificationsRead()
  const page = listQuery.data
  const items = page && Array.isArray(page.items) ? page.items : []

  const open = (notification: AppNotification) => {
    if (!notification.read_at) markRead.mutate({ ids: [notification.id] })
    onNavigate?.()
  }

  return (
    <>
      <div className="flex items-center gap-2 border-b px-3.5 py-2 border-border-subtle">
        <span className="tnum text-micro text-fg-tertiary">
          {unreadCount > 0 ? `${unreadCount} unread` : 'All caught up'}
        </span>
        <div className="flex-1" />
        <button
          type="button"
          onClick={() => markRead.mutate({ all: true })}
          disabled={unreadCount === 0 || markRead.isPending}
          className="flex items-center gap-1 rounded-md px-1.5 py-0.5 text-caption font-medium transition-colors hover:bg-[var(--surface-active)] disabled:opacity-50 text-fg-secondary"
        >
          <CheckCheck className="size-3.5" aria-hidden="true" />
          Mark all read
        </button>
      </div>
      {listQuery.isPending ? (
        <p className="px-4 py-8 text-center text-caption text-fg-tertiary">Loading notifications…</p>
      ) : listQuery.isError ? (
        <p className="px-4 py-8 text-center text-caption text-fg-tertiary">
          Notifications could not be loaded.
        </p>
      ) : items.length === 0 ? (
        <p className="px-4 py-8 text-center text-caption text-fg-tertiary">
          Nothing yet. Watch an event, event type, metric or branch to hear about it here.
        </p>
      ) : (
        <ul aria-label="Notifications" className="m-0 flex max-h-[420px] list-none flex-col gap-px overflow-y-auto p-2">
          {items.map(notification => (
            <li key={notification.id}>
              <NotificationRow notification={notification} onOpen={() => open(notification)} />
            </li>
          ))}
        </ul>
      )}
    </>
  )
}

function NotificationRow({
  notification,
  onOpen,
}: {
  notification: AppNotification
  onOpen: () => void
}) {
  const Icon = KIND_ICON[notification.kind] ?? MessageSquare
  const unread = !notification.read_at
  const path = safeInAppPath(notification.url)
  const meta = [
    notification.project_name ?? notification.project_slug ?? null,
    formatRelativeTime(notification.created_at),
  ].filter((part): part is string => !!part)
  const body = (
    <>
      <Icon className="mt-0.5 size-3.5 shrink-0 text-fg-tertiary" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <div className={cn('truncate text-body-sm', unread ? 'font-semibold' : 'font-normal')} title={notification.title}>
          {notification.title}
        </div>
        {notification.body && (
          <div className="mt-0.5 line-clamp-2 text-caption text-fg-secondary">{notification.body}</div>
        )}
        <div className="tnum mt-0.5 truncate text-micro text-fg-tertiary">{meta.join(' · ')}</div>
      </div>
      {unread && (
        <span className="mt-1.5 shrink-0">
          <Dot tone="accent" size={7} />
          <span className="sr-only">Unread</span>
        </span>
      )}
    </>
  )
  const className =
    'flex w-full gap-2 rounded-md px-1.5 py-2 text-left no-underline transition-colors hover:bg-[var(--surface-active)] text-inherit'
  return path ? (
    <Link to={path} onClick={onOpen} className={className}>
      {body}
    </Link>
  ) : (
    <button type="button" onClick={onOpen} className={className}>
      {body}
    </button>
  )
}
