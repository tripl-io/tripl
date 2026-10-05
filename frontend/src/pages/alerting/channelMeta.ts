import { createElement } from 'react'
import { ClipboardList, Globe, Inbox, Mail, MessageSquare, MessagesSquare, Send, Siren, Ticket, type LucideIcon, type LucideProps } from 'lucide-react'

import { CHANNEL_LABELS, channelLabel } from '@/lib/alertChannels'
import type { AlertDestinationType } from '@/types'

import type { DestinationChannel } from './constants'

export interface ChannelMeta {
  channel: DestinationChannel
  label: string
  Icon: LucideIcon
}

// Channel catalogue — drives both the per-channel sections and the compact
// add-channel affordance, so every type stays addable from one place.
export const CHANNEL_META: ChannelMeta[] = [
  // A chat bubble for Slack, not lucide's Webhook glyph: that is the same
  // concept as the separate Webhook channel below. Lucide has no
  // brand marks.
  { channel: 'slack', label: CHANNEL_LABELS.slack, Icon: MessageSquare },
  { channel: 'telegram', label: CHANNEL_LABELS.telegram, Icon: Send },
  { channel: 'webhook', label: CHANNEL_LABELS.webhook, Icon: Globe },
  { channel: 'email', label: CHANNEL_LABELS.email, Icon: Mail },
  { channel: 'jira', label: CHANNEL_LABELS.jira, Icon: Ticket },
  { channel: 'linear', label: CHANNEL_LABELS.linear, Icon: ClipboardList },
  { channel: 'pagerduty', label: CHANNEL_LABELS.pagerduty, Icon: Siren },
  // Not MessageSquare: that is Slack's glyph above, and two channels must not
  // share one.
  { channel: 'teams', label: CHANNEL_LABELS.teams, Icon: MessagesSquare },
]

// The labels live in lib/ so the app shell can name a channel without
// importing this page module (and its icons); re-exported for page callers.
export { channelLabel }

/** The channel's icon, or a generic one for the demo sink. */
export function channelIcon(type: AlertDestinationType): LucideIcon {
  return CHANNEL_META.find(meta => meta.channel === type)?.Icon ?? Inbox
}

/**
 * The channel's icon as an element. A component, so call sites render
 * `<ChannelGlyph type=… />` rather than a component picked during render.
 */
export function ChannelGlyph({ type, ...props }: { type: AlertDestinationType } & LucideProps) {
  return createElement(channelIcon(type), props)
}
