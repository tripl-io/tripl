import { useMemo } from 'react'
import { ChevronDown, Plus, Webhook } from 'lucide-react'

import { EmptyState } from '@/components/empty-state'
import { Panel } from '@/components/settings/kit'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { VIEWER_READ_ONLY_NOTICE, useCanWriteProject } from '@/lib/permissions'
import { ReadOnlyNotice } from '@/components/states'
import type { AlertDestination } from '@/types'

import { CHANNEL_META } from './channelMeta'
import { DestinationCard } from './DestinationCard'
import type { DestinationChannel } from './constants'

interface DestinationsSectionProps {
  slug: string
  destinations: AlertDestination[]
  isDemo: boolean
  onCreateDestination: (channel: DestinationChannel) => void
  onEditDestination: (destination: AlertDestination) => void
  onDeleteDestination: (destination: AlertDestination) => void
  /**
   * The destination whose delete is in flight, if any. Its control is inert
   * until the request settles, so a second confirm cannot fire a second DELETE.
   * Optional: absent means nothing is being deleted.
   */
  deletingDestinationId?: string | null
}

/**
 * The "Destinations" section: the per-channel cards and the add-a-channel
 * affordances.
 *
 * It was "Destinations & rules" and carried both. Rules — and the read-only
 * rule → destination summary panel that sat above them — moved to the Monitors
 * section, where they are shown with the firing state that made a
 * second screen necessary in the first place. What is left here is one object:
 * a channel.
 *
 * The create/edit dialog deliberately stays with the page: guided setup calls
 * the same `onCreateDestination` while this section is not mounted at all.
 */
export function DestinationsSection({
  slug,
  destinations,
  isDemo,
  onCreateDestination,
  onEditDestination,
  onDeleteDestination,
  deletingDestinationId = null,
}: DestinationsSectionProps) {
  // A demo's local sink has no entry in CHANNEL_META (it is not a channel anyone
  // can add), so it fell straight through the per-channel grouping below and its
  // card was never rendered: a demo's Destinations panel showed only the
  // permanently-disabled Slack example, while the sink that actually receives the
  // seeded deliveries stayed invisible — even though its rules did appear under
  // Routing rules. It gets its own group.
  const localSinks = useMemo(
    () => destinations.filter((destination) => destination.type === 'demo_sink'),
    [destinations],
  )
  // One flat list in channel-catalogue order: each card carries its own
  // channel icon and name now, so a "Slack ①" subheading over a single card
  // was a heading for nothing.
  const channelDestinations = useMemo(
    () =>
      CHANNEL_META.flatMap(({ channel }) =>
        destinations.filter(destination => destination.type === channel),
      ),
    [destinations],
  )

  const hasDestinations = destinations.length > 0
  // Creating, editing and deleting a destination or a rule are all editor-only
  // (deps.py `require_editor`), so a viewer gets the configuration as a
  // read-only report: every value stays on screen, nothing offers to change it.
  const canWrite = useCanWriteProject()
  // One source of truth for the channel buttons so the zero-state CTA and the
  // populated-state "add another" row stay in sync.
  const channelButtons = CHANNEL_META.map(({ channel, label, Icon }) => (
    <Button key={channel} variant="outline" size="sm" onClick={() => onCreateDestination(channel)}>
      <Icon aria-hidden="true" />
      {label}
    </Button>
  ))
  const demoChannelNotice = (
    <p className="text-body-sm text-fg-tertiary">
      This demo is local-only: alerts render to a built-in sink and are never sent to Slack,
      Telegram, a webhook, email, Jira, Linear, PagerDuty or Microsoft Teams. Create a real
      project to connect a channel.
    </p>
  )

  // The section's primary action, top-right like Monitors' "Add rule":
  // adding a channel used to mean finding the dashed strip at the
  // bottom of the list.
  const addDestinationMenu = canWrite && !isDemo && (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button size="sm">
          <Plus aria-hidden="true" />
          Add destination
          <ChevronDown aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {CHANNEL_META.map(({ channel, label, Icon }) => (
          <DropdownMenuItem key={channel} onSelect={() => onCreateDestination(channel)}>
            <Icon aria-hidden="true" />
            {label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )

  return (
    <>
    {/* Once, above everything this section can no longer offer to change —
        rather than a tooltip on each of the switches, pencils and bins that
        are simply absent below. */}
    {!canWrite && (
      <ReadOnlyNotice>{VIEWER_READ_ONLY_NOTICE}</ReadOnlyNotice>
    )}
    <Panel
      title="Destinations"
      subtitle="Signals route to destinations via rules."
      right={hasDestinations ? addDestinationMenu : undefined}
    >
      <div className="min-w-0 space-y-3 p-4">
        {!hasDestinations && (
          <EmptyState
            size="sm"
            headingLevel={3}
            icon={Webhook}
            title="No alert destinations"
            description="Connect Slack, Telegram, email, a webhook, Jira, Linear, PagerDuty or Microsoft Teams, then attach rules to it."
            action={
              isDemo ? (
                <div className="max-w-sm">{demoChannelNotice}</div>
              ) : !canWrite ? undefined : (
                <div className="flex flex-col items-center gap-2">
                  <span className="text-body-sm font-medium text-fg-tertiary">Add a channel</span>
                  <div className="flex flex-wrap items-center justify-center gap-2">
                    {channelButtons}
                  </div>
                </div>
              )
            }
          />
        )}

        {/* No delete affordance on a local sink: it is part of the demo
            scenario and owns its seeded rules and deliveries. Reset re-creates
            it. */}
        {localSinks.map(destination => (
          <DestinationCard
            key={destination.id}
            slug={slug}
            destination={destination}
            canWrite={canWrite}
            onEditDestination={onEditDestination}
          />
        ))}

        {channelDestinations.map(destination => (
          <DestinationCard
            key={destination.id}
            slug={slug}
            destination={destination}
            canWrite={canWrite}
            onEditDestination={onEditDestination}
            onDeleteDestination={onDeleteDestination}
            isDeleting={deletingDestinationId === destination.id}
          />
        ))}

        {/* The per-channel buttons stay under a populated list too, so every
            type is one click away without opening the menu above. Gone
            entirely for a viewer: "Add another channel" over a row of buttons
            that answer 403 is an invitation, not information. */}
        {hasDestinations && (canWrite || isDemo) && (
          <div className="flex flex-wrap items-center gap-2 rounded-card border border-dashed p-3">
            {isDemo ? (
              demoChannelNotice
            ) : (
              <>
                <span className="text-body-sm font-medium text-fg-tertiary">Add another channel</span>
                {channelButtons}
              </>
            )}
          </div>
        )}
      </div>
    </Panel>
    </>
  )
}
