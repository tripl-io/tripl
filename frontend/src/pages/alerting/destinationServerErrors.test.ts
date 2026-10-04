import { describe, expect, it } from 'vitest'

import { attachDestinationServerErrors, describeDestinationServerError } from './destinationServerErrors'

describe('describeDestinationServerError', () => {
  it('names the input and says the rule in words', () => {
    expect(describeDestinationServerError('Webhook target_url must be a valid https URL')).toEqual({
      field: 'target_url',
      text: 'Use an https:// URL.',
    })
    expect(
      describeDestinationServerError('Webhook target_url must not point to a private or internal address'),
    ).toEqual({ field: 'target_url', text: "Private or internal addresses aren't allowed." })
  })

  it('swaps the API field name for the label when there is no plainer sentence', () => {
    expect(describeDestinationServerError('Linear team_id must be an alnum / dash / underscore id')).toEqual({
      field: 'linear_team_id',
      text: 'Team ID must be an alnum / dash / underscore id',
    })
  })

  it('leaves a sentence that is already in words alone', () => {
    expect(
      describeDestinationServerError('Slack webhook URL must start with https://hooks.slack.com/'),
    ).toEqual({ field: null, text: 'Slack webhook URL must start with https://hooks.slack.com/' })
  })
})

describe('attachDestinationServerErrors', () => {
  it('moves a whole-request refusal under the input it names', () => {
    const attached = attachDestinationServerErrors(
      { fields: {}, message: 'Webhook target_url must not point to a private or internal address' },
      ['name', 'target_url'] as const,
    )
    expect(attached).toEqual({
      fields: { target_url: "Private or internal addresses aren't allowed." },
      message: null,
    })
  })

  it('keeps a refusal about an input this channel does not render as the form message', () => {
    const attached = attachDestinationServerErrors(
      { fields: {}, message: 'Jira base_url must be a valid https URL' },
      ['name', 'target_url'] as const,
    )
    expect(attached).toEqual({ fields: {}, message: 'Use an https:// URL.' })
  })
})
