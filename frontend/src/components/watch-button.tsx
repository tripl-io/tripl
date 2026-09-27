import { Bell, BellOff, ChevronDown, Eye, EyeOff } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useSubscription } from '@/hooks/useSubscription'
import type { SubscriptionEntityType, SubscriptionReason } from '@/types'

const REASON_LABEL: Record<SubscriptionReason, string> = {
  author: 'you created it',
  owner: 'you own it',
  commenter: 'you commented',
  reviewer: 'you review it',
  manual: 'you chose to watch it',
}

const ENTITY_NOUN: Record<SubscriptionEntityType, string> = {
  event: 'event',
  event_type: 'event type',
  metric: 'metric',
  branch: 'branch',
}

function watchTitle(entityType: SubscriptionEntityType, watching: boolean, reasons: readonly SubscriptionReason[]) {
  const noun = ENTITY_NOUN[entityType]
  if (!watching) return `Get notified about comments, signals and changes on this ${noun}`
  const why = reasons.map(reason => REASON_LABEL[reason]).filter(Boolean)
  return why.length > 0
    ? `Watching because ${why.join(', ')}. Click to stop.`
    : `Watching this ${noun}. Click to stop.`
}

/**
 * Watch / Unwatch on an event, event type, metric or branch page (#259).
 * Watching sends the reader notifications about the entity (comments, signals,
 * reviews); authors, owners, commenters and reviewers start out watching.
 * A muted watch reads "Muted", with Unmute and Unwatch behind it.
 * Hidden until the state is known, so it never flips under the pointer.
 */
export function WatchButton({
  slug,
  entityType,
  entityId,
  size = 'default',
}: {
  slug: string | undefined
  entityType: SubscriptionEntityType
  entityId: string | undefined
  size?: 'default' | 'sm'
}) {
  const { state, isPending, setWatching, setMuted } = useSubscription(slug, entityType, entityId)
  if (!state) return null
  const watching = state.watching
  // Still watching (the reasons stand) but muted: say so, and offer both ways
  // out — hear it again, or stop watching it altogether.
  if (watching && state.muted) {
    return (
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            type="button"
            variant="outline"
            size={size}
            title={`Muted: this ${ENTITY_NOUN[entityType]} does not notify you. Mentions still do.`}
            disabled={isPending}
          >
            <BellOff aria-hidden="true" />
            Muted
            <ChevronDown aria-hidden="true" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onSelect={() => setMuted(false)}>
            <Bell aria-hidden="true" />
            Unmute
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setWatching(false)}>
            <EyeOff aria-hidden="true" />
            Unwatch
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    )
  }
  const Icon = watching ? EyeOff : Eye
  return (
    <Button
      type="button"
      variant="outline"
      size={size}
      title={watchTitle(entityType, watching, state.reasons)}
      disabled={isPending}
      onClick={() => setWatching(!watching)}
    >
      <Icon aria-hidden="true" />
      {watching ? 'Unwatch' : 'Watch'}
    </Button>
  )
}

/**
 * Mute on an event's discussion (#259): the thread stays visible, but its new
 * comments stop notifying the reader. Mentions still reach them.
 */
export function ThreadMuteToggle({ slug, eventId }: { slug: string; eventId: string }) {
  const { state, isPending, setMuted } = useSubscription(slug, 'event', eventId)
  if (!state) return null
  const muted = state.muted
  const Icon = muted ? BellOff : Bell
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="h-7 gap-1.5 px-2 text-caption font-normal text-fg-tertiary"
      title={
        muted
          ? 'Muted: new comments here do not notify you. Mentions still do.'
          : 'Stop notifications about new comments in this thread'
      }
      disabled={isPending}
      onClick={() => setMuted(!muted)}
    >
      <Icon aria-hidden="true" />
      {muted ? 'Unmute' : 'Mute'}
    </Button>
  )
}
