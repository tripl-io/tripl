import { describe, expect, it } from 'vitest'

import { formatTimestamp } from './datetime'
import {
  describeHistoryChange,
  historyAuthor,
  historyFieldLabel,
  historyValueLabel,
} from './eventHistory'

// The keys `event_service._TRACKED_FIELDS` records an edit under.
const TRACKED_FIELDS = [
  'status',
  'name',
  'title',
  'description',
  'required_presence_threshold',
  'sunset_at',
  'superseded_by_event_id',
]

describe('historyFieldLabel', () => {
  it('names the known fields', () => {
    expect(historyFieldLabel('created')).toBe('Created')
    expect(historyFieldLabel('tags')).toBe('Tags')
    expect(historyFieldLabel('field:screen')).toBe('Field · screen')
    expect(historyFieldLabel('meta:owner')).toBe('Meta · owner')
  })

  it('names a signal verdict (#254)', () => {
    expect(historyFieldLabel('signal_verdict')).toBe('Signal verdict')
  })

  it('names every tracked attribute, never by its column', () => {
    expect(TRACKED_FIELDS.map(historyFieldLabel)).toEqual([
      'Status',
      'Name',
      'Title',
      'Description',
      'Required presence',
      'Sunset date',
      'Replaced by',
    ])
  })
})

describe('historyValueLabel', () => {
  it('passes free text through', () => {
    expect(historyValueLabel('title', 'Signup')).toBe('Signup')
    expect(historyValueLabel('name', 'Home Screen View')).toBe('Home Screen View')
    expect(historyValueLabel('field:screen', 'home')).toBe('home')
  })

  it('reads a status by its label', () => {
    expect(historyValueLabel('status', 'in_review')).toBe('In review')
    expect(historyValueLabel('status', 'live')).toBe('Live')
    expect(historyValueLabel('status', 'retired')).toBe('retired')
  })

  it('reads a threshold as a percentage, and its removal as the default', () => {
    expect(historyValueLabel('required_presence_threshold', '0.8')).toBe('80%')
    expect(historyValueLabel('required_presence_threshold', null)).toBe('default (95%)')
    expect(historyValueLabel('required_presence_threshold', 'n/a')).toBe('n/a')
  })

  it("reads Python's str(datetime) as a date", () => {
    const expected = formatTimestamp('2026-11-08T00:00:00+00:00')
    expect(expected).not.toBe('')
    expect(historyValueLabel('sunset_at', '2026-11-08 00:00:00+00:00')).toBe(expected)
    expect(historyValueLabel('sunset_at', '2026-11-08 00:00:00.123456+00:00')).toBe(
      formatTimestamp('2026-11-08T00:00:00.123+00:00'),
    )
    expect(historyValueLabel('sunset_at', 'soon')).toBe('soon')
  })

  it('names the current successor, and never prints an id', () => {
    const successor = { id: 'event-2', name: 'Home Screen Opened' }
    expect(historyValueLabel('superseded_by_event_id', 'event-2', { successor })).toBe('Home Screen Opened')
    expect(historyValueLabel('superseded_by_event_id', 'event-9', { successor })).toBe('another event')
    expect(historyValueLabel('superseded_by_event_id', 'event-2')).toBe('another event')
  })

  it('says a removed value was cleared', () => {
    for (const field of ['title', 'description', 'sunset_at', 'superseded_by_event_id', 'tags', 'meta:jira']) {
      expect(historyValueLabel(field, null)).toBe('cleared')
    }
    expect(historyValueLabel('created', null)).toBeNull()
  })

  it('reads a verdict with its reason and note', () => {
    expect(historyValueLabel('signal_verdict', 'tracking_bug')).toBe('Tracking bug')
    expect(historyValueLabel('signal_verdict', 'expected:campaign')).toBe('Expected · campaign')
    expect(historyValueLabel('signal_verdict', 'real_issue — Checkout fails on Android')).toBe(
      'Real issue — Checkout fails on Android',
    )
    expect(historyValueLabel('signal_verdict', 'expected:release — v5.2 — hotfix')).toBe(
      'Expected · release — v5.2 — hotfix',
    )
  })

  it('says a cleared verdict was cleared', () => {
    expect(historyValueLabel('signal_verdict', null)).toBe('cleared')
  })

  it('shows a verdict it does not recognise as recorded', () => {
    expect(historyValueLabel('signal_verdict', 'something_else')).toBe('something_else')
  })
})

describe('describeHistoryChange', () => {
  it('reads the first row as what the event was created as', () => {
    expect(describeHistoryChange({ field: 'created', new_value: 'Home View' })).toEqual({
      label: 'Created',
      connector: 'as',
      value: 'Home View',
    })
  })

  it('reads an edit as the attribute and its new value', () => {
    expect(describeHistoryChange({ field: 'status', new_value: 'live' })).toEqual({
      label: 'Status',
      connector: '→',
      value: 'Live',
    })
  })
})

describe('historyAuthor', () => {
  const names: Record<string, string> = { 'u-ana': 'Ana Ortiz' }
  const nameOf = (id: string) => names[id]

  it('credits a scan by its label', () => {
    expect(
      historyAuthor({ author_label: 'tripl (scan)', user_id: null, user_email: null }, nameOf),
    ).toBe('tripl (scan)')
  })

  it('credits a person by name, else by the email the entry recorded', () => {
    expect(historyAuthor({ user_id: 'u-ana', user_email: 'ana@example.com' }, nameOf)).toBe('Ana Ortiz')
    expect(historyAuthor({ user_id: 'u-gone', user_email: 'gone@example.com' }, nameOf)).toBe(
      'gone@example.com',
    )
    expect(historyAuthor({ user_id: null, user_email: null }, nameOf)).toBeNull()
  })
})
