import { describe, expect, it } from 'vitest'

import type { AlertInboxActionResponse, AlertInboxGroup } from '@/types'

import {
  ALERT_INBOX_STATUS,
  alertInboxStatusLabel,
  bulkInboxActionSuccessMessage,
  bulkMuteConfirmMessage,
  inboxActionSuccessMessage,
  incidentMagnitudeLabel,
  incidentMagnitudeTitle,
  incidentReasonLabel,
  incidentWorstDeltaLabel,
  isHandledInboxStatus,
  muteConfirmMessage,
  priorDecisionLabel,
  scopeHasDirection,
  stripValueErrorPrefix,
} from './alertStatus'

function makeGroup(overrides: Partial<AlertInboxGroup> = {}): AlertInboxGroup {
  return {
    correlation_group_id: 'grp-1',
    status: 'open',
    muted: false,
    muted_until: null,
    note: null,
    false_positive_count: 0,
    item_count: 1,
    delivery_count: 1,
    latest_bucket: '2026-08-11T10:00:00Z',
    first_delivery_at: '2026-08-11T09:00:00Z',
    latest_delivery_at: '2026-08-11T10:05:00Z',
    direction: 'drop',
    actual_count: 412,
    expected_count: 1010,
    percent_delta: -59.2,
    max_abs_percent_delta: null,
    scope_type: 'event',
    scope_types: ['event'],
    scope_ref: 'scope-a',
    event_id: null,
    scope_names: ['onboarding/reviews_carousel'],
    destination_names: ['TG'],
    rules: [{ id: 'rule-1', name: 'Volume rule' }],
    rule_names: ['Volume rule'],
    scan_names: ['Snowplow Pageviews (iOS)'],
    acted_at: null,
    acted_by: null,
    acted_by_name: null,
    ...overrides,
  }
}

describe('alert inbox status lexicon', () => {
  it('gives every status a human label, never the raw enum member', () => {
    // The badge printed `{group.status}`, so one of them literally read
    // "false_positive".
    expect(alertInboxStatusLabel('false_positive')).toBe('False positive')
    for (const lexeme of Object.values(ALERT_INBOX_STATUS)) {
      expect(lexeme.label).not.toContain('_')
    }
  })

  it('keeps open neutral and gives every handled state its own tone', () => {
    // 52 of 57 production groups are open. Tinting 52 of 57 rows destroys the
    // scanning benefit the colour exists for, so colour marks the minority
    // somebody has already decided about.
    expect(ALERT_INBOX_STATUS.open.tone).toBe('neutral')
    const handledTones = (['acknowledged', 'muted', 'resolved', 'false_positive'] as const).map(
      status => ALERT_INBOX_STATUS[status].tone,
    )
    expect(new Set(handledTones).size).toBe(handledTones.length)
    expect(handledTones).not.toContain('neutral')
  })

  it('counts everything but open as handled', () => {
    expect(isHandledInboxStatus('open')).toBe(false)
    expect(isHandledInboxStatus('acknowledged')).toBe(true)
    expect(isHandledInboxStatus('muted')).toBe(true)
  })
})

describe('what fired, on the card', () => {
  it('names direction and scope kind together', () => {
    expect(incidentReasonLabel('drop', ['event'])).toBe('drop · volume')
    expect(incidentReasonLabel('drop', ['release_regression'])).toBe('drop · release regression')
  })

  it('names every kind in a mixed group, not just the newest item\'s', () => {
    // Suppression is an exact match on the whole key; describing a mixed
    // incident by one member is how the mute's blast radius gets mis-read.
    expect(incidentReasonLabel('spike', ['event', 'release_regression'])).toBe(
      'spike · volume + release regression',
    )
  })

  it('writes the magnitude against its baseline', () => {
    expect(incidentMagnitudeLabel(makeGroup())).toContain('expected · -59.2%')
  })

  it('rounds and groups the counts so a baseline never reads as "88.318"', () => {
    // `expected_count` is a rolling baseline and arrives as a float. Raw, the
    // card read "197 vs 88.318 expected", which a reader used to a decimal comma
    // takes for 88,318 — and every other surface in the product already rounds.
    const label = incidentMagnitudeLabel(
      makeGroup({ actual_count: 197, expected_count: 88.318, percent_delta: 123.1 }),
    )
    expect(label).toBe('197 vs 88 expected · 123.1%')
    expect(label).not.toContain('88.318')
  })

  it('keeps a sub-unit metric value instead of collapsing it to "0 vs 0"', () => {
    // `metric` is a first-class alert scope and a `%` catalog metric STORES a
    // fraction (0.08 == 8%) — these columns were migrated integer → float for
    // exactly that (f9a0b1c2d3e4). Rounded, a purchase-conversion drop read
    // "0 vs 0 expected · -66.7%": a card contradicting both its own delta and
    // the "actual=4%, expected=12%" message the operator is holding.
    expect(
      incidentMagnitudeLabel(
        makeGroup({
          scope_type: 'metric',
          scope_types: ['metric'],
          actual_count: 0.04,
          expected_count: 0.12,
          percent_delta: -66.7,
        }),
      ),
    ).toBe('0.04 vs 0.12 expected · -66.7%')
    // A ratio metric has no ×100 to rescue it, and `toLocaleString()` alone caps
    // at three FRACTION digits — which would round it to "0" a second way.
    expect(
      incidentMagnitudeLabel(
        makeGroup({
          scope_type: 'metric',
          scope_types: ['metric'],
          actual_count: 0.000123,
          expected_count: 0.0009,
          percent_delta: -86.3,
        }),
      ),
    ).toBe('0.000123 vs 0.0009 expected · -86.3%')
  })

  it('groups thousands the way the activity rail does', () => {
    expect(
      incidentMagnitudeLabel(
        makeGroup({ actual_count: 42280, expected_count: 33375.6, percent_delta: 26.7 }),
      ),
    ).toContain('42,280 vs 33,376 expected')
  })

  it('keeps the unrounded counts for the tooltip', () => {
    // Rounding is for reading; reconciling a baseline against the detector needs
    // the precision, so it moves to the title rather than being dropped.
    expect(incidentMagnitudeTitle(makeGroup({ actual_count: 197, expected_count: 88.318 }))).toBe(
      'Unrounded: 197 actual, 88.318 expected',
    )
  })

  it('says a zero baseline in words, never as 0%', () => {
    // `percent_delta` is NULL exactly when there is nothing to divide by. The
    // stored 0.0 it replaced reported the largest possible relative move as the
    // smallest one.
    const label = incidentMagnitudeLabel(
      makeGroup({ actual_count: 412, expected_count: 0, percent_delta: null }),
    )
    expect(label).toBe('412 actual, none expected · no baseline')
    expect(label).not.toContain('0%')
  })

  it('reports the worst magnitude only when the group holds more than one item', () => {
    expect(incidentWorstDeltaLabel({ item_count: 1, max_abs_percent_delta: 92.4 })).toBeNull()
    expect(incidentWorstDeltaLabel({ item_count: 6, max_abs_percent_delta: 92.4 })).toBe(
      'worst 92.4% in this group',
    )
    expect(incidentWorstDeltaLabel({ item_count: 6, max_abs_percent_delta: null })).toBeNull()
  })

  it('surfaces a decision that the auto-reopen has undone', () => {
    // `_reopen_closed_incidents` resets a closed incident to open once the
    // scope goes quiet and never touches `acted_at` — so an incident somebody
    // closed last week was pixel-identical to one nobody had ever seen.
    const line = priorDecisionLabel(
      makeGroup({ acted_at: '2026-07-30T12:00:00Z', acted_by_name: 'V. Denisov' }),
    )
    expect(line).toMatch(/^Closed .* by V\. Denisov · firing again, latest /)
  })

  it('says nothing about a decision on a card that is still closed', () => {
    // A resolved card already says "Resolved"; the line exists for the case
    // where the status no longer shows that anything happened.
    expect(
      priorDecisionLabel(makeGroup({ status: 'resolved', acted_at: '2026-07-30T12:00:00Z' })),
    ).toBeNull()
    expect(priorDecisionLabel(makeGroup({ acted_at: null }))).toBeNull()
  })
})

describe('which signals have a direction', () => {
  it('gives volume, metric, release, freshness and lifecycle signals one', () => {
    for (const scope of [
      'project_total', 'event_type', 'event', 'metric', 'release_regression', 'source_freshness', 'lifecycle',
    ] as const) {
      expect(scopeHasDirection(scope), scope).toBe(true)
    }
  })

  it('gives the drifts none', () => {
    for (const scope of ['schema', 'distribution', 'variable_value_drift', 'property_drift'] as const) {
      expect(scopeHasDirection(scope), scope).toBe(false)
    }
  })
})

describe('the confirmation names the whole suppression key', () => {
  it('spells scope, kind, direction, scan and rule, then what is not covered', () => {
    const message = muteConfirmMessage(makeGroup(), '2026-08-19T10:00:00Z')

    expect(message).toContain('drop · volume')
    expect(message).toContain('onboarding/reviews_carousel')
    expect(message).toContain('Snowplow Pageviews (iOS)')
    expect(message).toContain('Volume rule')
    // The user's model is "not this thing again"; the system's is a five-part
    // exact match. Every gap between them is an incident somebody believes they
    // silenced and did not.
    expect(message).toContain('Nothing else is silenced')
  })

  it('says an open-ended mute has no end date, instead of printing "Invalid Date"', () => {
    // The Inbox can now mute with no end at all, which arrives here
    // as a null. Handing that to `formatDateTime` renders "Invalid Date" in the
    // middle of the one sentence whose entire job is to state the blast radius
    // before anything goes quiet — on the most far-reaching mute of the lot.
    const message = muteConfirmMessage(makeGroup(), null)

    expect(message).toMatch(/until you unmute/i)
    expect(message).not.toContain('Invalid Date')
    // The whole key is still spelled: the open-ended branch replaces the date,
    // not the sentence.
    expect(message).toContain('drop · volume')
    expect(message).toContain('onboarding/reviews_carousel')
    expect(message).toContain('Snowplow Pageviews (iOS)')
    expect(message).toContain('Volume rule')
    expect(message).toContain('Nothing else is silenced')
  })
})

describe('an action says what it actually did', () => {
  function response(overrides: Partial<AlertInboxActionResponse> = {}): AlertInboxActionResponse {
    return { group: makeGroup(), overrides_written: null, ...overrides }
  }

  it('counts the scopes a false positive really tightened', () => {
    expect(
      inboxActionSuccessMessage('false_positive', 'open', response({ overrides_written: 3 })),
    ).toContain('tightened 3 scopes')
    expect(
      inboxActionSuccessMessage('false_positive', 'open', response({ overrides_written: 1 })),
    ).toContain('tightened 1 scope')
  })

  it('admits when it tightened nothing at all', () => {
    // The ratchet skips any scope kind outside RATCHETABLE_SCOPE_TYPES, and
    // release_regression is not in it — 10 of 57 production groups. The tooltip
    // promised a permanent detection change on every one of them.
    const message = inboxActionSuccessMessage(
      'false_positive',
      'open',
      response({ overrides_written: 0 }),
    )
    expect(message).toContain('no scopes tightened')
    expect(message).not.toContain('tightened 0')
  })

  it('calls lifting a mute "unmuted", and reopening anything else "reopened"', () => {
    expect(inboxActionSuccessMessage('reopen', 'muted', response())).toContain('Unmuted')
    expect(inboxActionSuccessMessage('reopen', 'resolved', response())).toContain('Reopened')
  })

  it('names the instant a mute lifts', () => {
    const message = inboxActionSuccessMessage(
      'mute',
      'open',
      response({ group: makeGroup({ status: 'muted', muted: true, muted_until: '2026-08-19T10:00:00Z' }) }),
    )
    expect(message).toMatch(/^Muted until /)
  })

  it('does not let an open-ended mute read like a timed one', () => {
    // This branch was unreachable while the API demanded a `muted_until`, and
    // it was a bare "Muted." — indistinguishable from a seven-day snooze, for
    // the one mute that never lapses on its own.
    const message = inboxActionSuccessMessage(
      'mute',
      'open',
      response({ group: makeGroup({ status: 'muted', muted: true, muted_until: null }) }),
    )
    expect(message).toMatch(/no end date/i)
    expect(message).toMatch(/until you unmute/i)
    expect(message).not.toBe('Muted.')
  })
})

describe('one sentence for a whole batch', () => {
  it('counts the incidents it silences, and says what stays loud', () => {
    // The count IS the blast radius here: the single-incident sentence spells
    // the whole five-part suppression key, which cannot be scaled up — at ten
    // incidents it is ten keys nobody reads, and the union of the parts would
    // state a radius WIDER than what is actually silenced.
    const message = bulkMuteConfirmMessage(3, '2026-08-19T10:00:00Z')

    expect(message).toContain('Silences 3 incidents at once')
    expect(message).toContain('Each one keeps its own suppression key')
    expect(message).toContain('nothing beyond the ones you selected is silenced')
  })

  it('says one incident in the singular, so the sentence is English at N=1', () => {
    // The bar appears at a selection of one, and this is the sentence an
    // operator reads before the very first bulk mute they ever send.
    expect(bulkMuteConfirmMessage(1, '2026-08-19T10:00:00Z')).toContain(
      'Silences 1 incident at once',
    )
  })

  it('carries the open-ended clause instead of printing "Invalid Date"', () => {
    // Shared with `muteConfirmMessage` through `muteDurationClause` precisely so
    // the two confirmations cannot describe the same wire value differently.
    // A bulk indefinite mute is the furthest-reaching thing this
    // page can do, so it is the last sentence that may go vague.
    const message = bulkMuteConfirmMessage(4, null)

    expect(message).toContain('Silences 4 incidents at once')
    expect(message).toContain('with no end date')
    expect(message).toMatch(/until you unmute it/i)
    // Where it will be found afterwards: an indefinite mute freezes the row's
    // sort key, so it sinks out of the 30-day window.
    expect(message).toContain('Muted filter')
    expect(message).not.toContain('Invalid Date')
  })
})

describe('a batch says what it did, in the plural', () => {
  it('acknowledges and resolves by the number asked for', () => {
    expect(bulkInboxActionSuccessMessage('acknowledge', 1, null)).toBe('Acknowledged 1 incident.')
    expect(bulkInboxActionSuccessMessage('acknowledge', 12, null)).toBe(
      'Acknowledged 12 incidents.',
    )
    expect(bulkInboxActionSuccessMessage('resolve', 1, null)).toBe('Resolved 1 incident.')
    expect(bulkInboxActionSuccessMessage('resolve', 3, null)).toBe('Resolved 3 incidents.')
  })

  it('makes no per-incident promise about what happens next', () => {
    // "It stays quiet until the scope goes quiet, then reopens" is true of each
    // member individually and, over a batch, reads as a promise about the batch
    // — which is not a thing that exists: there is no group object, so there is
    // nothing to reopen as a unit.
    const message = bulkInboxActionSuccessMessage('acknowledge', 12, null)
    expect(message).not.toMatch(/scope goes quiet/i)
    expect(message).not.toMatch(/reopens/i)
  })

  it('names the instant a bulk mute lifts, and never says "Invalid Date"', () => {
    const message = bulkInboxActionSuccessMessage('mute', 4, '2026-08-19T10:00:00Z')
    expect(message).toMatch(/^Muted 4 incidents until /)
    expect(message).not.toContain('Invalid Date')
  })

  it('does not let an open-ended bulk mute read like a timed one', () => {
    expect(bulkInboxActionSuccessMessage('mute', 1, null)).toBe(
      'Muted 1 incident — no end date. They stay quiet until you unmute them.',
    )
    expect(bulkInboxActionSuccessMessage('mute', 7, null)).toBe(
      'Muted 7 incidents — no end date. They stay quiet until you unmute them.',
    )
  })

  it('uses both words for a reopen, because one slot does both jobs', () => {
    // `reopen` clears an acknowledge, a resolve and a false positive AND lifts a
    // mute. A mixed selection is the normal case in bulk, so there is no single
    // previous status to key the wording off the way the single-incident message
    // does.
    expect(bulkInboxActionSuccessMessage('reopen', 2, null)).toBe(
      'Reopened 2 incidents — alerts resume.',
    )
  })

  it('says where a note landed', () => {
    expect(bulkInboxActionSuccessMessage('note', 1, null)).toBe('Note saved on 1 incident.')
    expect(bulkInboxActionSuccessMessage('note', 5, null)).toBe('Note saved on 5 incidents.')
  })
})

describe('a server rule reaches the toast as English', () => {
  it('drops the "Value error, " Pydantic prepends to a model_validator message', () => {
    // The one message a bulk caller can realistically provoke is the long
    // false-positive refusal, which ends by naming the way to do it anyway, one
    // incident at a time. Prefixed, that well-argued policy reads like the page
    // broke.
    expect(
      stripValueErrorPrefix(
        'Value error, false_positive cannot be applied in bulk — mark them one incident at a time.',
      ),
    ).toBe('false_positive cannot be applied in bulk — mark them one incident at a time.')
  })

  it('leaves a message that never carried the prefix exactly as it is', () => {
    // The other 422 this route can raise is the length cap, which comes from a
    // field constraint and is not prefixed at all.
    expect(stripValueErrorPrefix('List should have at most 200 items after validation')).toBe(
      'List should have at most 200 items after validation',
    )
  })

  it('only strips a LEADING prefix, and only one', () => {
    // Anywhere but the front, those two words are the server's own text.
    expect(stripValueErrorPrefix('Rejected: Value error, nope')).toBe(
      'Rejected: Value error, nope',
    )
    expect(stripValueErrorPrefix('Value error, Value error, nope')).toBe('Value error, nope')
  })
})
