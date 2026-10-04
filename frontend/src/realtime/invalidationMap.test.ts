import { describe, expect, it, vi } from 'vitest'
import { QueryClient, type InfiniteData, type QueryKey } from '@tanstack/react-query'
import {
  PROJECT_EVENT_TYPES,
  invalidateForEvent,
  invalidationKeysFor,
  isProjectEventType,
} from './invalidationMap'
import {
  eventsMetricsKey,
  overviewKpiSeriesKey,
  overviewTopEventsKey,
  overviewVolumeRootKey,
} from '@/lib/queryKeys'

const SLUG = 'demo'

function hasKey(keys: QueryKey[], target: QueryKey): boolean {
  return keys.some((key) => JSON.stringify(key) === JSON.stringify(target))
}

describe('isProjectEventType', () => {
  it('accepts known event types and rejects others', () => {
    expect(isProjectEventType('scan_job.updated')).toBe(true)
    expect(isProjectEventType('metric_collection.updated')).toBe(true)
    expect(isProjectEventType('hello')).toBe(false)
    expect(isProjectEventType('nonsense')).toBe(false)
  })
})

describe('invalidationKeysFor', () => {
  it.each(['activity.created', 'signals.updated'] as const)(
    '%s refreshes the alert inbox, which does not poll while the stream is live',
    (type) => {
      const keys = invalidationKeysFor(type, SLUG)
      expect(hasKey(keys, ['alertInbox', SLUG])).toBe(true)
      expect(hasKey(keys, ['alertInboxGroup', SLUG])).toBe(true)
      expect(hasKey(keys, ['alertDeliveriesAny', SLUG])).toBe(true)
    },
  )

  it('returns a non-empty, slug-scoped key set for every event type', () => {
    for (const type of PROJECT_EVENT_TYPES) {
      const keys = invalidationKeysFor(type, SLUG)
      expect(keys.length).toBeGreaterThan(0)
    }
  })

  it('scan_job.updated refreshes scan + activity + events surfaces', () => {
    const keys = invalidationKeysFor('scan_job.updated', SLUG)
    expect(hasKey(keys, ['scans', SLUG])).toBe(true)
    expect(hasKey(keys, ['scanJobs', SLUG])).toBe(true)
    expect(hasKey(keys, ['activity', SLUG])).toBe(true)
    expect(hasKey(keys, ['activity', 'workspace'])).toBe(true)
    expect(hasKey(keys, ['overview'])).toBe(true)
    expect(hasKey(keys, eventsMetricsKey(SLUG))).toBe(true)
  })

  it('metric_collection.updated refreshes metrics, monitors, reconciliation and overview', () => {
    const keys = invalidationKeysFor('metric_collection.updated', SLUG)
    expect(hasKey(keys, ['metrics-catalog', SLUG])).toBe(true)
    expect(hasKey(keys, ['monitoringMetrics', SLUG])).toBe(true)
    // The events-tab dynamics chart (TabMetricsCard) does not poll while the
    // stream is live, so its prefix must be here or the chart never refreshes.
    expect(hasKey(keys, eventsMetricsKey(SLUG))).toBe(true)
    expect(hasKey(keys, ['eventWindowMetrics', SLUG])).toBe(true)
    expect(hasKey(keys, ['reconciliation'])).toBe(true)
    // this project's volume chart and top events, not the whole
    // Overview root (every project) and not the KPI series.
    expect(hasKey(keys, ['overview'])).toBe(false)
    expect(hasKey(keys, overviewVolumeRootKey(SLUG))).toBe(true)
    expect(hasKey(keys, overviewTopEventsKey(SLUG))).toBe(true)
    expect(hasKey(keys, overviewKpiSeriesKey(SLUG))).toBe(false)
  })

  it('refreshes the By version series with the adoption chart beside it', () => {
    const collection = invalidationKeysFor('metric_collection.updated', SLUG)
    expect(hasKey(collection, ['appVersionSeries', SLUG])).toBe(true)
    expect(hasKey(collection, ['appVersionAdoption', SLUG])).toBe(true)
    expect(hasKey(collection, ['chartAnnotations', SLUG])).toBe(true)
    expect(hasKey(collection, ['breakdownTimeline', SLUG])).toBe(true)
    expect(hasKey(invalidationKeysFor('signals.updated', SLUG), ['appVersionSeries', SLUG])).toBe(true)
  })

  it('signals.updated refreshes anomalies, monitors and notifications', () => {
    const keys = invalidationKeysFor('signals.updated', SLUG)
    // One key for every signal surface — the bell, Overview and the Anomalies
    // page share it, so the retired per-surface prefix must
    // NOT come back as a second entry that silently matches nothing.
    expect(hasKey(keys, ['activeSignals', SLUG])).toBe(true)
    expect(hasKey(keys, ['anomalies', 'signals', SLUG])).toBe(false)
    expect(hasKey(keys, ['monitors-summary', SLUG])).toBe(true)
    expect(hasKey(keys, ['topbarNotifications', SLUG])).toBe(true)
    // the Overview's signal reads are the shared list above.
    expect(hasKey(keys, ['overview'])).toBe(false)
  })

  it('activity.created refreshes the activity rail and notification surfaces', () => {
    const keys = invalidationKeysFor('activity.created', SLUG)
    expect(hasKey(keys, ['activity', SLUG])).toBe(true)
    expect(hasKey(keys, ['topbarNotifications', SLUG])).toBe(true)
    expect(hasKey(keys, ['alertDeliveries', SLUG])).toBe(true)
  })

  it('project_summary.updated refreshes the project list and overview', () => {
    const keys = invalidationKeysFor('project_summary.updated', SLUG)
    expect(hasKey(keys, ['projects'])).toBe(true)
    expect(hasKey(keys, ['project', SLUG])).toBe(true)
  })
})

describe('invalidateForEvent', () => {
  it('invalidates every mapped key exactly once', () => {
    const queryClient = new QueryClient()
    const invalidateQueries = vi.spyOn(queryClient, 'invalidateQueries')

    invalidateForEvent(queryClient, 'scan_job.updated', SLUG)

    const expected = invalidationKeysFor('scan_job.updated', SLUG)
    expect(invalidateQueries).toHaveBeenCalledTimes(expected.length)
    for (const key of expected) {
      expect(invalidateQueries).toHaveBeenCalledWith({ queryKey: key })
    }
  })

  it('refreshes the events lists the way a bulk edit does, not page by page', () => {
    // A scan landing re-requested every page of a list nobody had on screen.
    const queryClient = new QueryClient()
    const key = ['events', SLUG, null, 'list']
    queryClient.setQueryData<InfiniteData<{ items: string[] }>>(key, {
      pages: [{ items: ['a'] }, { items: ['b'] }, { items: ['c'] }],
      pageParams: [0, 1, 2],
    })

    invalidateForEvent(queryClient, 'scan_job.updated', SLUG)

    const after = queryClient.getQueryData<InfiniteData<unknown>>(key)!
    expect(after.pageParams).toEqual([0])
    expect(queryClient.getQueryState(key)?.isInvalidated).toBe(true)
    queryClient.clear()
  })
})
