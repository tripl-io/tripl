import { describe, expect, it } from 'vitest'

import { describeTestFailure, destinationScheduleLabel } from './destinationCardLabels'

describe('destinationScheduleLabel', () => {
  it('names the weekday cron and folds in what is held', () => {
    expect(destinationScheduleLabel('0 9 * * 1-5', 'UTC', 3)).toBe(
      'Weekdays at 09:00 UTC · 3 held for next digest',
    )
  })

  it('says nothing about held alerts when none are held', () => {
    expect(destinationScheduleLabel('0 9 * * 1-5', null, 0)).toBe('Weekdays at 09:00')
  })
})

describe('describeTestFailure', () => {
  it('turns a proxy refusal into a sentence and keeps the raw text', () => {
    expect(describeTestFailure('<urlopen error Tunnel connection failed: 403 Forbidden>')).toEqual({
      summary: "Couldn't reach the URL: a network proxy blocked the request.",
      detail: '<urlopen error Tunnel connection failed: 403 Forbidden>',
    })
  })

  it('reads an HTTP status', () => {
    expect(describeTestFailure('HTTP Error 404: Not Found').summary).toBe(
      'The URL was not found (HTTP 404). Check the address.',
    )
  })

  it('shows a channel message that is already in words as it is', () => {
    expect(describeTestFailure('Forbidden: bot was blocked by the user')).toEqual({
      summary: 'Forbidden: bot was blocked by the user',
      detail: null,
    })
  })
})

describe('describeTestFailure with the server classification', () => {
  it("reads the status code off the response, whatever the message's wording", () => {
    expect(
      describeTestFailure('HTTP 403 from https://api.telegram.org: Forbidden', {
        error_kind: 'http_status',
        http_status: 403,
      }),
    ).toEqual({
      summary: 'The channel rejected the credentials (HTTP 403). Check the token or URL.',
      detail: 'HTTP 403 from https://api.telegram.org: Forbidden',
    })
  })

  it('names a DNS failure from its kind alone', () => {
    expect(describeTestFailure('<urlopen error [Errno -2]>', { error_kind: 'dns' }).summary).toBe(
      "Couldn't find that host. Check the URL.",
    )
  })

  it('shows a configuration refusal as the server wrote it', () => {
    expect(
      describeTestFailure('Telegram bot_token must match <digits>:<token>', { error_kind: 'config' }),
    ).toEqual({ summary: 'Telegram bot_token must match <digits>:<token>', detail: null })
  })

  it("reads the channel client's own HTTP wording without a kind", () => {
    expect(describeTestFailure('HTTP 404 from https://hooks.slack.com').summary).toBe(
      'The URL was not found (HTTP 404). Check the address.',
    )
  })
})
