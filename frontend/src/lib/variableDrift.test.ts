// @vitest-environment jsdom
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { VariableValueDriftStatus } from '@/api/variableDrifts'
import { formatDateTime } from './datetime'
import {
  collapsedDriftLabel,
  driftReviewState,
  driftStatusNote,
  MAX_DRIFT_TIMER_DELAY_MS,
  nextDriftSnoozeExpiry,
  useDriftReviewClock,
} from './variableDrift'
import { at } from '@/test/at'

const NOW = Date.parse('2026-09-01T12:00:00Z')
const IN_A_WEEK = '2026-09-08T12:00:00Z'
const IN_AN_HOUR = '2026-09-01T13:00:00Z'
const LAST_WEEK = '2026-08-25T12:00:00Z'
const MINUTE_MS = 60 * 1000
const HOUR_MS = 60 * MINUTE_MS
const DAY_MS = 24 * HOUR_MS

const drift = (status: VariableValueDriftStatus, snoozedUntil: string | null = null) => ({
  status,
  snoozed_until: snoozedUntil,
})

describe('driftReviewState', () => {
  it('reads an open drift as active', () => {
    expect(driftReviewState(drift('open'), NOW)).toBe('active')
  })

  it('reads a snooze that has not run out as neither active nor resolved', () => {
    // The third state the panels had no room for: the backend counts this row
    // as NOT open — so the table badge says zero — while a two-state reading
    // left it in the warning-toned active list.
    expect(driftReviewState(drift('snoozed', IN_A_WEEK), NOW)).toBe('snoozed')
  })

  it('puts a LAPSED snooze back on the open list, as the backend count does', () => {
    // `_active_drift_predicates` excludes only FUTURE-snoozed rows. Claiming a
    // spent snooze still defers review would put this page back out of step
    // with the badge, just in the other direction.
    expect(driftReviewState(drift('snoozed', LAST_WEEK), NOW)).toBe('active')
  })

  it('treats the deadline itself as spent, matching `snoozed_until <= now`', () => {
    expect(driftReviewState(drift('snoozed', '2026-09-01T12:00:00Z'), NOW)).toBe('active')
  })

  it('treats a snooze carrying no deadline as active, the way a NULL is counted', () => {
    expect(driftReviewState(drift('snoozed'), NOW)).toBe('active')
  })

  it('treats an unparseable deadline as active rather than hiding the row', () => {
    expect(driftReviewState(drift('snoozed', 'not a timestamp'), NOW)).toBe('active')
  })

  it('reads both resolutions as resolved', () => {
    expect(driftReviewState(drift('accepted'), NOW)).toBe('resolved')
    expect(driftReviewState(drift('false_positive'), NOW)).toBe('resolved')
  })
})

describe('driftStatusNote', () => {
  it('says when a snoozed drift comes back', () => {
    // `snoozed_until` was fetched and rendered nowhere, so neither surface said
    // when the deferral ends.
    expect(driftStatusNote(drift('snoozed', IN_A_WEEK), NOW)).toBe(
      `snoozed until ${formatDateTime(IN_A_WEEK)}`,
    )
  })

  it('falls back to the bare status when no snooze is in force', () => {
    expect(driftStatusNote(drift('snoozed', LAST_WEEK), NOW)).toBe('snoozed')
  })

  it('names a resolution plainly', () => {
    expect(driftStatusNote(drift('false_positive'), NOW)).toBe('false positive')
    expect(driftStatusNote(drift('accepted'), NOW)).toBe('accepted')
  })
})

describe('collapsedDriftLabel', () => {
  it('names only what the group actually holds', () => {
    expect(collapsedDriftLabel({ snoozed: 0, resolved: 2 })).toBe('resolved')
    expect(collapsedDriftLabel({ snoozed: 1, resolved: 0 })).toBe('snoozed')
  })

  it('does not call a snoozed row resolved when the group mixes the two', () => {
    expect(collapsedDriftLabel({ snoozed: 1, resolved: 1 })).toBe('snoozed or resolved')
  })
})

describe('nextDriftSnoozeExpiry', () => {
  it('picks the soonest deadline still in force', () => {
    expect(
      nextDriftSnoozeExpiry([drift('snoozed', IN_A_WEEK), drift('snoozed', IN_AN_HOUR)], NOW),
    ).toBe(Date.parse(IN_AN_HOUR))
  })

  it('ignores every deadline `driftReviewState` already reads as spent or unusable', () => {
    // Waking for one of these would re-render the panel and change nothing: the
    // classification has already put all five rows on the active list.
    expect(
      nextDriftSnoozeExpiry(
        [
          drift('open'),
          drift('accepted'),
          drift('snoozed'),
          drift('snoozed', LAST_WEEK),
          drift('snoozed', 'not a timestamp'),
        ],
        NOW,
      ),
    ).toBeNull()
  })
})

describe('useDriftReviewClock', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date(NOW))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  const snoozedFor = (ms: number) => drift('snoozed', new Date(NOW + ms).toISOString())

  it('moves a lapsing snooze back onto the active list without a remount', () => {
    const rows = [snoozedFor(MINUTE_MS)]
    const { result } = renderHook(() => useDriftReviewClock(rows))

    expect(driftReviewState(at(rows, 0), result.current)).toBe('snoozed')

    act(() => {
      vi.advanceTimersByTime(MINUTE_MS)
    })

    // The whole repair. The clock was `useState(() => Date.now())`, whose lazy
    // initializer runs once per mount, so this row went on reading as snoozed —
    // collapsed behind the toggle, offering only Un-snooze — for as long as the
    // panel stayed open, while the badge the backend recomputes per request had
    // already counted it as open again. Nothing remounted and no new rows
    // arrived here; only the deadline passed.
    expect(driftReviewState(at(rows, 0), result.current)).toBe('active')
  })

  it('arms nothing when no snooze is in force', () => {
    const { result } = renderHook(() => useDriftReviewClock([drift('open'), drift('accepted')]))
    const seeded = result.current

    act(() => {
      vi.advanceTimersByTime(30 * DAY_MS)
    })

    // One timeout per deadline, not a tick: with no deadline there is nothing
    // the clock could learn by waking, so it never does — a month of an open
    // page costs zero re-renders.
    expect(result.current).toBe(seeded)
  })

  it('wakes for the nearest deadline, then re-arms for the one behind it', () => {
    const soon = snoozedFor(MINUTE_MS)
    const later = snoozedFor(HOUR_MS)
    const rows = [later, soon]
    const { result } = renderHook(() => useDriftReviewClock(rows))

    act(() => {
      vi.advanceTimersByTime(MINUTE_MS)
    })
    expect(driftReviewState(soon, result.current)).toBe('active')
    expect(driftReviewState(later, result.current)).toBe('snoozed')

    act(() => {
      vi.advanceTimersByTime(HOUR_MS - MINUTE_MS)
    })
    expect(driftReviewState(later, result.current)).toBe('active')
  })

  it('reaches a snooze further out than a 32-bit timeout in stages', () => {
    // `setTimeout` wraps a delay past ~24.8 days and fires it immediately, so an
    // unclamped delay would wake, find the row still snoozed, and — with the
    // deadline unchanged — never arm anything again.
    const rows = [snoozedFor(40 * DAY_MS)]
    const { result } = renderHook(() => useDriftReviewClock(rows))

    act(() => {
      vi.advanceTimersByTime(MAX_DRIFT_TIMER_DELAY_MS)
    })
    expect(driftReviewState(at(rows, 0), result.current)).toBe('snoozed')

    act(() => {
      vi.advanceTimersByTime(40 * DAY_MS - MAX_DRIFT_TIMER_DELAY_MS)
    })
    expect(driftReviewState(at(rows, 0), result.current)).toBe('active')
  })

  it('clears its timer on unmount', () => {
    // A panel closed part-way through a snooze must not leave a timeout behind
    // that wakes up to set state on a tree that is gone. Counted relatively,
    // because React's own scheduler may hold faked timers of its own.
    const idle = vi.getTimerCount()
    const { unmount } = renderHook(() => useDriftReviewClock([snoozedFor(MINUTE_MS)]))
    const mounted = vi.getTimerCount()
    expect(mounted).toBeGreaterThan(idle)

    unmount()

    expect(vi.getTimerCount()).toBeLessThan(mounted)
  })
})
