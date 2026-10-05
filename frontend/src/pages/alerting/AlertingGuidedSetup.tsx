import type { ReactNode } from 'react'
import { Check } from 'lucide-react'
import { Link } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { Panel } from '@/components/settings/kit'
import { ReadOnlyNotice } from '@/components/states'
import { useCanWriteProject } from '@/lib/permissions'
import { cn } from '@/lib/utils'

import type { ChannelMeta } from './channelMeta'
import type { DestinationChannel } from './constants'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

interface AlertingGuidedSetupProps {
  slug: string
  channels: ChannelMeta[]
  /**
   * Whether the project has a scan. Without one nothing is ever detected, so
   * nothing can alert, and the checklist says so first.
   */
  hasScans: boolean
  onPickChannel: (channel: DestinationChannel) => void
}

/** One line per channel on its tile: what picking it does. */
const CHANNEL_HINT: Record<DestinationChannel, string> = {
  slack: 'Post to a channel',
  telegram: 'Message a chat or group',
  webhook: 'POST JSON to your endpoint',
  email: 'Send to a list of addresses',
  jira: 'Open an issue per alert',
  linear: 'Open an issue per alert',
  pagerduty: 'Page on-call, resolve on close',
  teams: 'Post a card to a channel',
}

type StepState = 'done' | 'current' | 'upcoming'

function StepMarker({ n, state }: { n: number; state: StepState }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'flex size-6 shrink-0 items-center justify-center rounded-full border text-caption font-semibold tnum',
        state === 'done' && 'border-transparent bg-(--success-soft) text-(--success)',
        state === 'current' && 'border-transparent bg-accent-solid text-accent-solid-fg',
        state === 'upcoming' && 'border-border bg-surface text-fg-subtle',
      )}
    >
      {state === 'done' ? <Check className="size-3.5" /> : n}
    </span>
  )
}

function Step({
  n,
  state,
  title,
  body,
  children,
}: {
  n: number
  state: StepState
  title: string
  body: string
  children?: ReactNode
}) {
  const stateLabel = state === 'done' ? 'done' : state === 'current' ? 'current step' : 'to do'
  return (
    <li className="flex gap-3" aria-current={state === 'current' ? 'step' : undefined}>
      <StepMarker n={n} state={state} />
      <div className="grid min-w-0 flex-1 gap-1">
        <p className={cn('m-0 text-body font-medium', state === 'upcoming' ? 'text-fg-subtle' : 'text-fg')}>
          <span>{title}</span>
          <span className="sr-only">{` (${stateLabel})`}</span>
        </p>
        <p className="m-0 text-body-sm text-fg-subtle">{body}</p>
        {children}
      </div>
    </li>
  )
}

/**
 * The first visit to alerting, as a checklist the reader can act on.
 *
 * The three numbered cards used to restate the flow as decoration, with the
 * real action — the channel buttons — below them under a small grey label; and
 * on a project with no scan they promised "three steps and you are live" when
 * nothing could ever fire. Now the channel picker IS step 1 (or 2),
 * each step says whether it is done, and a missing scan is step 0.
 */
export function AlertingGuidedSetup({ slug, channels, hasScans, onPickChannel }: AlertingGuidedSetupProps) {
  // The steps stay on screen for a viewer — they explain what alerting is on a
  // project that has none, which is exactly the question a viewer landing here
  // has. Only the controls that would 403 come off.
  const canWrite = useCanWriteProject()
  // Step numbers shift by one when the scan step is shown, so it reads 1-2-3(-4)
  // rather than starting at 0.
  const offset = hasScans ? 0 : 1
  const channelState: StepState = hasScans ? 'current' : 'upcoming'

  return (
    <Panel
      title="Set up alerting"
      subtitle={hasScans ? 'No destinations or rules yet' : 'Nothing is being watched yet'}
    >
      <div className="grid gap-5 p-4">
        <p className="m-0 max-w-prose text-body text-fg-subtle">
          Alerting sends the anomalies tripl detects to the channels your team already watches.
        </p>
        {!canWrite && (
          <ReadOnlyNotice>
            You have the viewer role in this project, so the first destination is created by an
            editor or owner. Once one exists, incidents and their deliveries show up here for everyone.
          </ReadOnlyNotice>
        )}

        <ol className="m-0 grid list-none gap-5 p-0">
          {!hasScans && (
            <Step
              n={1}
              state="current"
              title="Connect data and run a scan"
              body="Alerts come from anomalies, and anomalies come from scans. This project has none yet, so nothing can alert."
            >
              <div>
                <Button asChild size="sm" variant="outline">
                  <Link to={projectPath(currentOrgSlug(), slug, '/scans')}>Go to Scans</Link>
                </Button>
              </div>
            </Step>
          )}
          <Step
            n={1 + offset}
            state={channelState}
            title="Pick a channel"
            body="Where alerts land. You add its details next; nothing is sent until a rule says so."
          >
            {canWrite && (
              <div className="grid grid-cols-1 gap-2 pt-1 sm:grid-cols-2 lg:grid-cols-3">
                {channels.map(({ channel, label, Icon }) => (
                  <button
                    key={channel}
                    type="button"
                    // Named by the channel alone; the one-liner describes it.
                    aria-labelledby={`guided-channel-${channel}`}
                    aria-describedby={`guided-channel-${channel}-hint`}
                    onClick={() => onPickChannel(channel)}
                    className="flex items-center gap-3 rounded-card border border-border bg-surface px-3 py-2.5 text-left transition-colors hover:border-(--accent) hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50"
                  >
                    <span className="flex size-8 shrink-0 items-center justify-center rounded-control bg-bg-sunken">
                      <Icon aria-hidden="true" className="size-4" />
                    </span>
                    <span className="grid min-w-0">
                      <span id={`guided-channel-${channel}`} className="text-body-sm font-medium text-fg">{label}</span>
                      <span id={`guided-channel-${channel}-hint`} className="truncate text-caption text-fg-subtle">
                        {CHANNEL_HINT[channel]}
                      </span>
                    </span>
                  </button>
                ))}
              </div>
            )}
          </Step>
          <Step
            n={2 + offset}
            state="upcoming"
            title="Create a destination"
            body="Add the channel's credentials. Matched signals are delivered here."
          />
          <Step
            n={3 + offset}
            state="upcoming"
            title="Add your first rule"
            body="Choose what should alert — a rule opens prefilled on the new destination."
          />
        </ol>
      </div>
    </Panel>
  )
}
