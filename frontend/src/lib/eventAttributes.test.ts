import { describe, expect, it } from 'vitest'

import { EVENT_ATTRIBUTE_LABEL, eventAttributeLabel } from './eventAttributes'
import { REVIEW_STATUS } from './statusLexicon'

describe('eventAttributeLabel', () => {
  it('names the successor the same under either key the backend uses', () => {
    expect(eventAttributeLabel('superseded_by')).toBe('Replaced by')
    expect(eventAttributeLabel('superseded_by_event_id')).toBe('Replaced by')
  })

  it('uses the words the event form, the event page and the docs use', () => {
    expect(eventAttributeLabel('name')).toBe('Name')
    expect(eventAttributeLabel('source_name')).toBe('Scan identity')
    expect(eventAttributeLabel('owner_id')).toBe('Owner')
    expect(eventAttributeLabel('sunset_at')).toBe('Sunset date')
    expect(eventAttributeLabel('metric_breakdown_columns')).toBe('Metric breakdowns')
    expect(eventAttributeLabel('required_presence_threshold')).toBe('Required presence')
    expect(eventAttributeLabel('meta_values')).toBe('Meta fields')
  })

  it('calls the reviewed flag what the events list calls it', () => {
    expect(eventAttributeLabel('reviewed')).toBe('Verified')
    expect(EVENT_ATTRIBUTE_LABEL.reviewed).toBe(REVIEW_STATUS.reviewed.label)
  })

  it('reads a key it does not know yet as words, not as a column', () => {
    expect(eventAttributeLabel('first_seen_at')).toBe('First seen at')
    expect(eventAttributeLabel('constructor')).toBe('Constructor')
    expect(eventAttributeLabel('')).toBe('')
  })
})
