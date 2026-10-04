import { describe, expect, it } from 'vitest'

import { metaFieldDeleteMessage } from './metaFieldDelete'

describe('metaFieldDeleteMessage', () => {
  it('counts values and events when the usage is known', () => {
    expect(metaFieldDeleteMessage('Jira link', { value_count: 1, event_count: 1 })).toBe(
      "Removes 1 Jira link value from 1 event. This can't be undone.",
    )
  })

  it('says only the field goes when no event holds a value', () => {
    expect(metaFieldDeleteMessage('Jira link', { value_count: 0, event_count: 0 })).toBe(
      "No event holds a Jira link value, so only the field itself is removed. This can't be undone.",
    )
  })

  it('names the loss when the count is unknown', () => {
    expect(metaFieldDeleteMessage('Jira link', null)).toBe(
      "Removes every Jira link value from the events that carry one. This can't be undone.",
    )
  })
})
