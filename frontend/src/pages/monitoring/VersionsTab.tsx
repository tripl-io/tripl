import { useMemo } from 'react'
import { Link } from 'react-router-dom'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { AlertTriangle, GitBranch } from 'lucide-react'
import { eventMetricsApi } from '@/api/eventMetrics'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { ErrorState } from '@/components/error-state'
import { ReleaseRegressionPanel } from '@/components/monitoring/release-regression-panel'
import { Chip } from '@/components/primitives/chip'
import { CodeToken } from '@/components/primitives/code-token'
import { EmptyState } from '@/components/empty-state'
import { ChartSkeleton } from '@/components/states'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardTitle } from '@/components/ui/card'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { MetricsMultiSeriesChart } from '@/components/ui/chart-lazy'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { formatNumber } from '@/lib/format'
import { adaptMetricVersions } from '@/lib/metricAdapters'
import type { MetricRollupMode, MetricsGranularity } from '@/lib/metrics'
import { appVersionAdoptionKey, appVersionSeriesRangeKey } from '@/lib/queryKeys'
import type { MonitoringScope } from '@/lib/monitoring'
import {
  buildVersionChartSeries,
  formatPercent,
  legendValueKindFor,
  type LegendValueKind,
  type VersionChartSeries,
  type VersionFilter,
} from './chartSeries'
import { ChartCardHeader, MetricsRangeControls } from './MetricsRangeControls'
import { SeriesSwatch } from './SeriesSwatch'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

export interface VersionsTabProps {
  slug: string
  scope: MonitoringScope
  scopeId: string
  scanConfigId: string | null
  rangeDays: number
  timeRange: { from: string; to: string }
  granularity: MetricsGranularity
  /** The series' collection granularity, always offered by the control. */
  nativeGranularity: MetricsGranularity | null
  rollupMode: MetricRollupMode
  refetchInterval: number | false
  versionFilter: VersionFilter
  seriesLabel: string
  valueFormatter?: (value: number) => string
  /** The chart tooltip and legend spelling; see MetricsChartProps.tooltipFormatter. */
  tooltipFormatter?: (value: number) => string
  onRangeDaysChange: (days: number) => void
  onGranularityChange: (granularity: MetricsGranularity) => void
  onVersionFilterChange: (filter: VersionFilter) => void
}

/**
 * The "By version" tab: the entity's series split by app version, the scan's
 * version adoption and its release regressions. Each card owns its query and
 * its error, so one failing endpoint no longer replaces the whole page (MON-8).
 *
 * Keyed on the range length, not on the live window's bounds: the bound moves
 * every five minutes, and a key that moved with it dropped the tab back to
 * "Loading…" each time (MON-3). The query functions read the live window, so a
 * refetch still asks for the current one.
 */
export function VersionsTab({
  slug,
  scope,
  scopeId,
  scanConfigId,
  rangeDays,
  timeRange,
  granularity,
  nativeGranularity,
  rollupMode,
  refetchInterval,
  versionFilter,
  seriesLabel,
  valueFormatter,
  tooltipFormatter,
  onRangeDaysChange,
  onGranularityChange,
  onVersionFilterChange,
}: VersionsTabProps) {
  const appVersionScope = scope === 'metric' || !scanConfigId
    ? null
    : { scope_type: scope, scope_ref: scope === 'project_total' ? scanConfigId : scopeId }

  const seriesQuery = useQuery({
    queryKey: appVersionSeriesRangeKey(slug, scope, scopeId, scanConfigId, rangeDays),
    queryFn: () => {
      if (scope === 'metric') {
        return metricsCatalogApi.getVersions(slug, scopeId, timeRange).then(adaptMetricVersions)
      }
      return eventMetricsApi.getAppVersionSeries(slug, scanConfigId!, {
        scope_type: appVersionScope!.scope_type,
        scope_ref: appVersionScope!.scope_ref,
        ...timeRange,
      })
    },
    enabled: !!scopeId && (scope === 'metric' || !!appVersionScope),
    refetchInterval,
    placeholderData: keepPreviousData,
    meta: SILENT_ERROR_META,
  })

  const adoptionQuery = useQuery({
    queryKey: appVersionAdoptionKey(slug, scanConfigId, rangeDays),
    queryFn: () => eventMetricsApi.getAppVersionAdoption(slug, scanConfigId!, timeRange),
    // No catalog adoption endpoint — the metric scope leaves this card empty.
    enabled: scope !== 'metric' && !!scanConfigId,
    refetchInterval,
    placeholderData: keepPreviousData,
    meta: SILENT_ERROR_META,
  })

  const selectedVersionFilter: VersionFilter = versionFilter === 'latest' && !seriesQuery.data?.latest_version
    ? 'all'
    : versionFilter
  const legendKind = legendValueKindFor(rollupMode)

  const versionChartSeries = useMemo(
    () => buildVersionChartSeries(
      seriesQuery.data?.series ?? [],
      granularity,
      selectedVersionFilter,
      seriesQuery.data?.latest_version,
      rollupMode,
    ),
    [seriesQuery.data?.latest_version, seriesQuery.data?.series, granularity, selectedVersionFilter, rollupMode],
  )
  const adoptionChartSeries = useMemo(
    () => buildVersionChartSeries(
      adoptionQuery.data?.series ?? [],
      granularity,
      selectedVersionFilter,
      adoptionQuery.data?.latest_version,
    ),
    [adoptionQuery.data?.latest_version, adoptionQuery.data?.series, granularity, selectedVersionFilter],
  )
  const adoptionTotal = useMemo(
    () => adoptionQuery.data?.totals.reduce((sum, point) => sum + point.count, 0) ?? 0,
    [adoptionQuery.data?.totals],
  )
  const latestAdoptionTotal = useMemo(
    () => adoptionQuery.data?.series.find(series => series.is_latest)?.total_count ?? 0,
    [adoptionQuery.data?.series],
  )
  const latestAdoptionShare = adoptionTotal > 0 ? latestAdoptionTotal / adoptionTotal : null

  // The header/tab "latest" is the backend's activation-gated release: Wave 1
  // made is_latest reflect the gated release, so it already agrees with the
  // maturity-gated "latest active release" the ReleaseRegressionPanel derives.
  // When that newest release has NOT yet taken a real share of traffic
  // (is_active=false) it's a pre-release / not-yet-rolled-out build, and drops
  // the primary "latest" highlight for a distinct pre-release treatment.
  const latestVersion = seriesQuery.data?.latest_version ?? null
  const latestVersionIsActive = useMemo(() => {
    const data = seriesQuery.data
    if (!data?.latest_version) return false
    const info = data.versions.find(entry => entry.is_latest && !entry.is_other)
      ?? data.series.find(entry => entry.is_latest && !entry.is_other)
    return info?.is_active ?? false
  }, [seriesQuery.data])
  // Warn on the Latest filter whenever the newest release is not yet a rolled-out
  // active release. The backend `is_active` already honors each scan's own
  // app_version_active_share_min, so this is the authoritative signal.
  const latestIsPreRelease = latestVersion !== null && !latestVersionIsActive

  // The tab is offered because the scan names a version column, but that
  // column can still be empty. Two chart cards with controls around one grey
  // line each said nothing about why (MO-28): one empty state says it once.
  const seriesSettled = !seriesQuery.isLoading && !seriesQuery.isError
  const adoptionSettled = scope === 'metric' || (!adoptionQuery.isLoading && !adoptionQuery.isError)
  const noVersionData = seriesSettled && adoptionSettled
    && (seriesQuery.data?.series.length ?? 0) === 0
    && (adoptionQuery.data?.series.length ?? 0) === 0
  if (noVersionData) {
    const column = adoptionQuery.data?.app_version_column
    return (
      <Card>
        <CardContent>
          <EmptyState
            icon={GitBranch}
            title="No app versions collected yet"
            description={
              <>
                {column
                  ? <>The scan reads <CodeToken>{column}</CodeToken> but found no values</>
                  : 'No app version values were found'}
                {` in the last ${rangeDays} days. Check that your SDK sends the app version with each event.`}
              </>
            }
            action={scope !== 'metric' && scanConfigId ? (
              <Button asChild variant="outline" size="sm">
                <Link to={projectPath(currentOrgSlug(), slug, `/scans/${scanConfigId}`)}>Open scan settings</Link>
              </Button>
            ) : undefined}
          />
        </CardContent>
      </Card>
    )
  }

  return (
    <>
      {/* The shared section-card geometry (DS-4 / MO-10): a header bar with a
          12.5px h2 and the controls, then the chart in the card body. */}
      <Card>
        <ChartCardHeader
          title={(
            <>
              <GitBranch aria-hidden="true" className="size-4 text-fg-tertiary" />
              <CardTitle as="h2">By version</CardTitle>
              {latestVersion && (
                latestIsPreRelease ? (
                  // A warning flag, the one pill idiom (DS-6).
                  <Chip
                    tone="warning"
                    icon={<AlertTriangle aria-hidden="true" />}
                    title="Newest release by version, but it hasn't taken a real share of traffic yet — treat it as a pre-release / not-yet-rolled-out build."
                  >
                    <span>pre-release {latestVersion}</span>
                    {latestAdoptionShare !== null && (
                      <span className="opacity-80">· {formatPercent(latestAdoptionShare)}</span>
                    )}
                  </Chip>
                ) : (
                  <Chip variant="outline">
                    <span>latest {latestVersion}</span>
                    {latestAdoptionShare !== null && (
                      <span className="text-fg-tertiary">· {formatPercent(latestAdoptionShare)}</span>
                    )}
                  </Chip>
                )
              )}
            </>
          )}
        >
          {/* Two views of one chart: the shared segmented control (DS-16),
              the same height as the range control beside it. */}
          <SegmentedControl
            aria-label="Versions shown"
            value={selectedVersionFilter}
            onChange={onVersionFilterChange}
            options={[
              { value: 'all', label: 'All versions' },
              {
                value: 'latest',
                disabled: !latestVersion,
                title: latestIsPreRelease
                  ? 'The newest release is a pre-release with little traffic — not yet rolled out.'
                  : undefined,
                label: (
                  <>
                    Latest
                    {latestIsPreRelease && (
                      <AlertTriangle aria-hidden="true" className="size-3 text-[var(--warning)]" />
                    )}
                  </>
                ),
              },
            ]}
          />
          <MetricsRangeControls
            rangeDays={rangeDays}
            granularity={granularity}
            nativeGranularity={nativeGranularity}
            onRangeDaysChange={onRangeDaysChange}
            onGranularityChange={onGranularityChange}
          />
        </ChartCardHeader>
        <CardContent>
          {seriesQuery.isError ? (
            <ErrorState
              compact
              title="Could not load version metrics"
              error={seriesQuery.error}
              onRetry={() => void seriesQuery.refetch()}
            />
          ) : seriesQuery.isLoading ? (
            <ChartSkeleton height={280} label="Loading version metrics…" />
          ) : (
            <>
              <MetricsMultiSeriesChart
                series={versionChartSeries}
                height={280}
                granularity={granularity}
                seriesLabel={seriesLabel}
                valueFormatter={valueFormatter}
                tooltipFormatter={tooltipFormatter}
                emptyLabel="No version metrics available"
                from={timeRange.from}
                to={timeRange.to}
              />
              <VersionLegend
                series={versionChartSeries}
                latestShare={latestAdoptionShare}
                valueFormatter={tooltipFormatter ?? valueFormatter}
                valueKind={legendKind}
              />
            </>
          )}
        </CardContent>
      </Card>

      <Card>
        <ChartCardHeader
          title={(
            <>
              <CardTitle as="h2">Version adoption</CardTitle>
              {latestAdoptionShare !== null && (
                latestIsPreRelease ? (
                  <Chip tone="warning" icon={<AlertTriangle aria-hidden="true" />}>
                    Pre-release {formatPercent(latestAdoptionShare)}
                  </Chip>
                ) : (
                  <Chip variant="outline">
                    Latest {formatPercent(latestAdoptionShare)}
                  </Chip>
                )
              )}
            </>
          )}
        >
          {/* A column name: a code token, not a pill (DS-6). */}
          {adoptionQuery.data?.app_version_column && (
            <CodeToken>{adoptionQuery.data.app_version_column}</CodeToken>
          )}
        </ChartCardHeader>
        <CardContent>
          {adoptionQuery.isError ? (
            <ErrorState
              compact
              title="Could not load version adoption"
              error={adoptionQuery.error}
              onRetry={() => void adoptionQuery.refetch()}
            />
          ) : adoptionQuery.isLoading ? (
            <ChartSkeleton height={240} label="Loading adoption…" />
          ) : (
            <>
              <MetricsMultiSeriesChart
                series={adoptionChartSeries}
                height={240}
                granularity={granularity}
                emptyLabel="No adoption data available"
                from={timeRange.from}
                to={timeRange.to}
              />
              <VersionLegend series={adoptionChartSeries} latestShare={latestAdoptionShare} />
            </>
          )}
        </CardContent>
      </Card>

      {scope !== 'metric' && scanConfigId && (
        <ReleaseRegressionPanel slug={slug} scanConfigId={scanConfigId} />
      )}
    </>
  )
}

function VersionLegend({
  series,
  latestShare,
  valueFormatter,
  valueKind = 'total',
}: {
  series: VersionChartSeries[]
  latestShare?: number | null
  // Same convention as the charts: the formatted string carries its own unit
  // (percent-unit catalog metrics render stored fractions ×100).
  valueFormatter?: (value: number) => string
  valueKind?: LegendValueKind
}) {
  if (!series.length) return null
  const shareSuffix = latestShare != null ? ` · ${formatPercent(latestShare)}` : ''
  return (
    <div className="mt-3 flex flex-wrap gap-2">
      {series.map(item => {
        const value = valueFormatter ? valueFormatter(item.legendValue) : formatNumber(item.legendValue)
        return (
          <div
            key={`${item.version}-${item.isOther}`}
            className="flex min-w-0 items-center gap-2 rounded-md border bg-background px-2 py-1 text-body-sm"
          >
            <SeriesSwatch color={item.color} dash={item.dash} />
            <span className="min-w-0 truncate font-mono">{item.isOther ? 'Other' : item.version}</span>
            {item.isLatest && (
              <Chip size="xs">latest{shareSuffix}</Chip>
            )}
            {item.isPreRelease && (
              <Chip tone="warning" size="xs">pre-release{shareSuffix}</Chip>
            )}
            <span className="shrink-0 text-fg-tertiary">
              {valueKind === 'latest' ? `latest value: ${value}` : value}
            </span>
          </div>
        )
      })}
    </div>
  )
}
