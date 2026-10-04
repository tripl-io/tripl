import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { GRANULARITY_ORDER, RANGE_OPTIONS, type MetricsGranularity } from '@/lib/metrics'
import type { VersionFilter } from './chartSeries'

export const MONITORING_DETAIL_TABS = [
  'volume',
  'versions',
  'distribution',
  'heatmap',
  'breakdowns',
] as const
export type MonitoringDetailTab = (typeof MONITORING_DETAIL_TABS)[number]

/** Every scope opens on the same week; the Overview says "7d" too. */
export const DEFAULT_RANGE_DAYS = 7

export interface MonitoringDetailSearch {
  tab: MonitoringDetailTab
  rangeDays: number
  /** A manual granularity pick; null follows the scope's default. */
  granularity: MetricsGranularity | null
  versionFilter: VersionFilter
  distributionField: string
  breakdownColumn: string
  /** Breakdown values narrowed to; empty shows every value. */
  breakdownValues: string[]
}

export interface MonitoringDetailSearchActions {
  setTab: (tab: MonitoringDetailTab) => void
  setRangeDays: (days: number) => void
  /** A pick equal to the scope's current `fallback` default leaves the URL. */
  setGranularity: (granularity: MetricsGranularity, fallback: MetricsGranularity) => void
  setVersionFilter: (filter: VersionFilter) => void
  setDistributionField: (field: string) => void
  /** A new column has a different value set, so the value filter resets with it. */
  setBreakdownColumn: (column: string) => void
  setBreakdownValues: (values: string[]) => void
}

const RANGE_DAYS = new Set<number>(RANGE_OPTIONS.map(option => option.days))

/** Parse the page's search params; anything unknown degrades to its default. */
export function readMonitoringDetailSearch(params: URLSearchParams): MonitoringDetailSearch {
  const tab = params.get('tab')
  const range = Number(params.get('range'))
  const granularity = params.get('gran')
  return {
    tab: MONITORING_DETAIL_TABS.includes(tab as MonitoringDetailTab)
      ? (tab as MonitoringDetailTab)
      : 'volume',
    rangeDays: RANGE_DAYS.has(range) ? range : DEFAULT_RANGE_DAYS,
    granularity: GRANULARITY_ORDER.includes(granularity as MetricsGranularity)
      ? (granularity as MetricsGranularity)
      : null,
    versionFilter: params.get('version') === 'latest' ? 'latest' : 'all',
    distributionField: params.get('field') ?? '',
    breakdownColumn: params.get('column') ?? '',
    breakdownValues: params.getAll('value'),
  }
}

/**
 * The drilldown's search narrowed to one breakdown value: the Breakdowns tab
 * on `column` with only `value` selected, the rest of the view (range,
 * granularity) kept. The Why panel's per-value links (#255) open it.
 */
export function breakdownValueSearch(current: URLSearchParams | string, column: string, value: string): string {
  const params = new URLSearchParams(current)
  params.set('tab', 'breakdowns')
  params.set('column', column)
  params.delete('value')
  params.append('value', value)
  return `?${params.toString()}`
}

/**
 * The drilldown's view state — tab, range, granularity, version filter,
 * distribution field, breakdown column and values — lives in the URL, so a link
 * or a refresh reopens the same view and Back from a drilldown does not reset
 * it. `?tab=` and `?column=` were already read once at mount (the
 * event form deep-links a field to its split); now they are written back too.
 * Same idiom as AnomaliesPage: defaults stay out of the URL, and every write
 * uses `replace`, because flipping a filter is not a stop for the Back button.
 */
export function useMonitoringDetailSearch(): [MonitoringDetailSearch, MonitoringDetailSearchActions] {
  const [searchParams, setSearchParams] = useSearchParams()
  const update = useCallback(
    (mutate: (params: URLSearchParams) => void) => {
      setSearchParams(
        previous => {
          const params = new URLSearchParams(previous)
          mutate(params)
          return params
        },
        { replace: true },
      )
    },
    [setSearchParams],
  )

  const setOrDelete = (params: URLSearchParams, key: string, value: string, fallback: string) => {
    if (value === fallback) params.delete(key)
    else params.set(key, value)
  }

  const actions: MonitoringDetailSearchActions = {
    setTab: tab => update(params => setOrDelete(params, 'tab', tab, 'volume')),
    setRangeDays: days => update(params => setOrDelete(params, 'range', String(days), String(DEFAULT_RANGE_DAYS))),
    setGranularity: (granularity, fallback) =>
      update(params => setOrDelete(params, 'gran', granularity, fallback)),
    setVersionFilter: filter => update(params => setOrDelete(params, 'version', filter, 'all')),
    setDistributionField: field => update(params => setOrDelete(params, 'field', field, '')),
    setBreakdownColumn: column => update(params => {
      setOrDelete(params, 'column', column, '')
      params.delete('value')
    }),
    setBreakdownValues: values => update(params => {
      params.delete('value')
      for (const value of values) params.append('value', value)
    }),
  }

  return [readMonitoringDetailSearch(searchParams), actions]
}
