import { useQuery } from '@tanstack/react-query'

import { eventMetricsApi } from '@/api/eventMetrics'
import { signalSeriesKey } from '@/lib/queryKeys'
import type { MonitoringSignal, SignalSeries, SignalSeriesScope } from '@/types'

/** The most scopes one series request carries; the server rejects more. */
export const SIGNAL_SERIES_MAX_SCOPES = 500

/** Unique per open signal: the backend keys signals on scan config + scope + bucket. */
export function signalRowKey(signal: {
  scan_config_id: string | null
  scope_type: string | null
  scope_ref: string
  bucket: string
}): string {
  return `${signal.scan_config_id ?? 'metric'}:${signal.scope_type}:${signal.scope_ref}:${signal.bucket}`
}

/**
 * Row lookup that survives the server re-spelling the bucket ("…Z" vs
 * "…+00:00"): the bucket is compared as an instant.
 */
export function signalSeriesLookupKey(signal: {
  scan_config_id: string | null
  scope_type: string | null
  scope_ref: string
  bucket: string
}): string {
  return signalRowKey({ ...signal, bucket: String(new Date(signal.bucket).getTime()) })
}

/**
 * The signals a row sparkline can be drawn for. Catalog-metric signals belong
 * to no scan and their series live elsewhere, so they are left out rather than
 * sent to come back empty.
 */
export function signalSeriesScopes(signals: readonly MonitoringSignal[]): SignalSeriesScope[] {
  const scopes: SignalSeriesScope[] = []
  for (const signal of signals) {
    if (!signal.scan_config_id) continue
    if (
      signal.scope_type !== 'project_total' &&
      signal.scope_type !== 'event_type' &&
      signal.scope_type !== 'event'
    ) {
      continue
    }
    scopes.push({
      scan_config_id: signal.scan_config_id,
      scope_type: signal.scope_type,
      scope_ref: signal.scope_ref,
      bucket: signal.bucket,
    })
    if (scopes.length >= SIGNAL_SERIES_MAX_SCOPES) break
  }
  return scopes
}

/**
 * The counts to draw and which point is the flagged bucket, or null when the
 * series has nothing to draw. Matched by time rather than by string: the
 * server echoes the bucket in its own ISO spelling.
 */
export function signalSparkline(
  series: SignalSeries | undefined,
): { data: number[]; anomalyIdx: number | null } | null {
  if (!series || series.data.length < 2) return null
  const flagged = new Date(series.bucket).getTime()
  const index = series.data.findIndex((point) => new Date(point.bucket).getTime() === flagged)
  return {
    data: series.data.map((point) => point.count),
    anomalyIdx: index >= 0 ? index : null,
  }
}

/**
 * Row sparklines for the Anomalies list, one batched request.
 *
 * Deliberately not part of the signals payload: that list is cached for 30 s
 * and shared with the bell, Overview and the Events page, none of which draws
 * a sparkline. The key sits under `activeSignalsKey`, so a `signals.updated`
 * invalidation refreshes the sparklines with the rows they belong to.
 */
export function useSignalSeries(
  slug: string | undefined,
  signals: readonly MonitoringSignal[] | undefined,
) {
  const scopes = signals ? signalSeriesScopes(signals) : []
  const query = useQuery({
    queryKey: signalSeriesKey(slug, scopes.map(signalRowKey)),
    queryFn: () => eventMetricsApi.getSignalSeries(slug!, scopes),
    enabled: !!slug && scopes.length > 0,
    staleTime: 60_000,
  })
  const byKey = new Map<string, SignalSeries>()
  for (const series of query.data ?? []) byKey.set(signalSeriesLookupKey(series), series)
  return { byKey, isPending: query.isPending && scopes.length > 0 }
}
