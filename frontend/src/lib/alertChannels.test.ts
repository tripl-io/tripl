import { describe, expect, it } from 'vitest'

import { channelLabel, TICKET_CHANNELS } from './alertChannels'

describe('channelLabel', () => {
  it('names a channel as a reader does, not as the wire spells it', () => {
    expect(channelLabel('slack')).toBe('Slack')
    expect(channelLabel('linear')).toBe('Linear')
    expect(channelLabel('pagerduty')).toBe('PagerDuty')
    expect(channelLabel('teams')).toBe('Microsoft Teams')
    expect(channelLabel('demo_sink')).toBe('Local sink')
  })

  it('reads an unknown channel as itself rather than as nothing', () => {
    expect(channelLabel('opsgenie')).toBe('opsgenie')
  })
})

describe('TICKET_CHANNELS', () => {
  it('holds the channels whose every send opens an issue', () => {
    expect([...TICKET_CHANNELS].sort()).toEqual(['jira', 'linear'])
    // PagerDuty opens an incident, but a retry updates it under the same dedup
    // key rather than opening a second one, so it is not a ticket channel.
    expect(TICKET_CHANNELS.has('pagerduty')).toBe(false)
  })
})
