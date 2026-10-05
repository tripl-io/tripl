/**
 * The channel as a reader names it. Plain strings, no icons, so the app shell
 * (the top-bar bell) can name a channel without pulling a pages/ module into
 * the entry chunk. `pages/alerting/channelMeta` pairs these with the glyphs.
 */
export const CHANNEL_LABELS = {
  slack: 'Slack',
  telegram: 'Telegram',
  webhook: 'Webhook',
  email: 'Email',
  jira: 'Jira',
  linear: 'Linear',
  pagerduty: 'PagerDuty',
  teams: 'Microsoft Teams',
} as const satisfies Record<string, string>

/**
 * "Slack", not the raw `slack` / `demo_sink` type the API carries.
 * The demo sink is not a creatable channel, but it is a destination
 * people see. Takes any string: a delivery's `channel` is not narrowed to
 * AlertDestinationType, and an unknown one reads as itself.
 */
export function channelLabel(type: string): string {
  if (type === 'demo_sink') return 'Local sink'
  return (CHANNEL_LABELS as Record<string, string>)[type] ?? type
}

/**
 * Channels where a delivery opens an issue rather than posting a message. A
 * retry there is a second ticket, not a repeated message, so every retry
 * control asks first.
 */
export const TICKET_CHANNELS: ReadonlySet<string> = new Set(['jira', 'linear'])
