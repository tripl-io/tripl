import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { eventMetricsApi } from '@/api/eventMetrics'
import { ErrorState } from '@/components/error-state'
import { Chip, type ChipTone } from '@/components/primitives/chip'
import { SectionSkeleton } from '@/components/states'
import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select'
import { formatTimestamp } from '@/lib/datetime'
import { APP_LOCALE, formatNumber, formatPercent } from '@/lib/format'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import type { DistributionDriftBand, DistributionDriftPoint } from '@/types'
import { distributionDriftsKey } from '@/lib/queryKeys'
import { ChartCardHeader } from './MetricsRangeControls'

export type DistributionScope =
  | { scope_type: 'project_total'; scope_ref: string; scan_config_id: string }
  | { scope_type: 'event_type'; scope_ref: string }

/**
 * The Distribution tab: population-stability drift of the scan's configured
 * fields for this scope. Owns its query and its error, keyed on the
 * range length rather than the moving live bounds.
 */
export function DistributionTab({
  slug,
  distributionScope,
  rangeDays,
  timeRange,
  refetchInterval,
  selectedField,
  onSelectedFieldChange,
}: {
  slug: string
  distributionScope: DistributionScope | null
  rangeDays: number
  timeRange: { from: string; to: string }
  refetchInterval: number | false
  selectedField: string
  onSelectedFieldChange: (field: string) => void
}) {
  const query = useQuery({
    queryKey: distributionDriftsKey(slug, distributionScope, rangeDays),
    queryFn: () => eventMetricsApi.getDistributionDrifts(slug, {
      scope_type: distributionScope!.scope_type,
      scope_ref: distributionScope!.scope_ref,
      scan_config_id: 'scan_config_id' in distributionScope!
        ? distributionScope!.scan_config_id
        : undefined,
      ...timeRange,
    }),
    enabled: !!distributionScope,
    refetchInterval,
    placeholderData: keepPreviousData,
    meta: SILENT_ERROR_META,
  })
  return (
    <DistributionDriftPanel
      data={query.data?.data ?? []}
      fields={query.data?.fields ?? []}
      isLoading={query.isLoading}
      error={query.isError ? query.error : undefined}
      onRetry={() => void query.refetch()}
      selectedField={selectedField}
      onSelectedFieldChange={onSelectedFieldChange}
    />
  )
}

/** A drift band is a status, so it is a toned Chip like every other. */
function driftBandTone(band: DistributionDriftBand): ChipTone {
  if (band === 'significant') return 'danger'
  if (band === 'minor') return 'warning'
  return 'success'
}

/**
 * A drift bucket's label. Daily buckets start at UTC midnight and were printed
 * with a meaningless "12:00 AM"; they print the date alone.
 */
function formatDriftBucket(bucket: string, daily: boolean): string {
  if (!daily) return formatTimestamp(bucket)
  const date = new Date(bucket)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleDateString(APP_LOCALE, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
}

function isUtcMidnight(bucket: string): boolean {
  const date = new Date(bucket)
  return !Number.isNaN(date.getTime())
    && date.getUTCHours() === 0 && date.getUTCMinutes() === 0 && date.getUTCSeconds() === 0
}

/** Which bar is which: nothing said the grey one was the baseline. */
function ShareBarLegend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-fg-tertiary" data-testid="distribution-legend">
      <span className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className="h-2 w-4 rounded-full bg-fg-tertiary" />
        Baseline (earlier window)
      </span>
      <span className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className="h-2 w-4 rounded-full bg-primary" />
        Latest bucket
      </span>
    </div>
  )
}

function DistributionShareBar({
  label,
  baselineShare,
  currentShare,
}: {
  label: string
  baselineShare: number
  currentShare: number
}) {
  return (
    <div className="grid gap-2 rounded-md border bg-background p-3">
      <div className="flex items-center justify-between gap-3 text-body-sm">
        <span className="min-w-0 truncate font-mono">{label}</span>
        <span className="shrink-0 text-fg-tertiary">
          {formatPercent(baselineShare)} → {formatPercent(currentShare)}
        </span>
      </div>
      <div className="grid gap-1.5">
        <div className="h-2 rounded-full bg-muted" title="Baseline">
          <div
            className="h-2 rounded-full bg-fg-tertiary"
            style={{ width: `${Math.max(2, baselineShare * 100)}%` }}
          />
        </div>
        <div className="h-2 rounded-full bg-muted" title="Latest bucket">
          <div
            className="h-2 rounded-full bg-primary"
            style={{ width: `${Math.max(2, currentShare * 100)}%` }}
          />
        </div>
      </div>
    </div>
  )
}

function DistributionDriftPanel({
  data,
  fields,
  isLoading,
  error,
  onRetry,
  selectedField,
  onSelectedFieldChange,
}: {
  data: DistributionDriftPoint[]
  fields: string[]
  isLoading: boolean
  /** The drift request's failure, shown inside the tab. */
  error?: unknown
  onRetry?: () => void
  selectedField: string
  onSelectedFieldChange: (field: string) => void
}) {
  const activeField = fields.includes(selectedField) ? selectedField : fields[0] ?? ''
  const rows = data
    .filter(row => !activeField || row.field_name === activeField)
    .sort((left, right) => left.bucket.localeCompare(right.bucket))
  const latest = rows.at(-1)
  const tableRows = [...rows].reverse().slice(0, 12)
  const daily = rows.length > 0 && rows.every(row => isUtcMidnight(row.bucket))
  // The biggest movers first, whichever way they moved.
  const movers = [...(latest?.top_movers ?? [])]
    .sort((left, right) =>
      Math.abs(right.current_share - right.baseline_share) - Math.abs(left.current_share - left.baseline_share))
    .slice(0, 6)

  if (error) {
    return (
      <ErrorState
        compact
        title="Could not load distribution drift"
        error={error}
        onRetry={onRetry}
      />
    )
  }

  if (isLoading) {
    return <SectionSkeleton variant="chart" label="Loading distribution data…" />
  }

  if (!data.length || !fields.length) {
    return (
      <Card>
        <CardContent className="flex h-56 flex-col items-center justify-center gap-1 text-center text-body text-fg-tertiary">
          <p>No distribution drift data available</p>
          {/* "the scan" is the one resolved above (`scanConfigId`) — this scope's
              samples come from that scan and no other, so "run a scan" pointed the
              reader at the wrong control as well as at the wire's noun. */}
          <p className="max-w-md text-body-sm">
            Add fields to{' '}
            <span className="font-mono">distribution_drift_fields</span> on the scan,
            then run it to start collecting distribution samples for this scope.
          </p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-4">
      {/* The shared section-card geometry: a header bar with
          the 12.5px h2 and the field picker, then the body. */}
      <Card>
        <ChartCardHeader title={<CardTitle as="h2">Distribution</CardTitle>}>
          <Select value={activeField} onValueChange={onSelectedFieldChange}>
            <SelectTrigger className="w-full sm:w-[220px]" aria-label="Distribution field">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {fields.map(field => (
                <SelectItem key={field} value={field}>
                  {field}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </ChartCardHeader>
        <CardContent className="space-y-4">
          {/* The one KPI idiom; unboxed, as it already sits in a card.
              A 2×2 grid on a phone: stacked one per row the four stats took
              about 200px there. */}
          {latest && (
            <MiniStatStrip phoneGrid>
              <MiniStat label="Bucket" value={formatDriftBucket(latest.bucket, daily)} />
              <MiniStat label="Drift (PSI)" value={latest.psi.toFixed(3)} />
              <MiniStat
                label="Band"
                value={<Chip tone={driftBandTone(latest.band)}>{latest.band}</Chip>}
              />
              <MiniStat
                label="Rows"
                value={`${formatNumber(latest.baseline_total)} → ${formatNumber(latest.current_total)}`}
              />
            </MiniStatStrip>
          )}
          {/* What PSI and the band mean, in the same thresholds the detector
              uses (docs: use/anomaly-detection). Visible, not a hover title,
              so it reaches touch readers too. */}
          <p className="text-caption text-fg-tertiary" data-testid="psi-explainer">
            Drift (PSI, Population Stability Index) compares this field's mix
            of values with the earlier window: below 0.10 is stable, 0.10–0.25
            minor, 0.25 and above significant.
          </p>

          {movers.length > 0 && <ShareBarLegend />}
          {movers.length > 0 && (
            <div className="grid gap-3 md:grid-cols-2">
              {movers.map(mover => (
                <DistributionShareBar
                  key={mover.value}
                  label={mover.value}
                  baselineShare={mover.baseline_share}
                  currentShare={mover.current_share}
                />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        {/* Titled like every other section card: it was the one untitled
            card on the page. */}
        <CardHeader>
          <CardTitle as="h2">Drift history · last 12 buckets</CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {/* A phone drops the Band column: the PSI beside it carries the same
              verdict. Top contribution stays — the movers card above covers
              only the latest bucket, so for every older bucket this column is
              the one place that says what moved. */}
          <Table aria-label="Drift by bucket">
            <TableHeader className="bg-muted/40">
              <TableRow className="hover:bg-transparent">
                <TableHead scope="col" className="px-4">Bucket</TableHead>
                <TableHead scope="col" className="px-4">Drift (PSI)</TableHead>
                <TableHead scope="col" className="hidden px-4 md:table-cell">Band</TableHead>
                <TableHead scope="col" className="px-4">Top contribution</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {tableRows.map(row => {
                const topMover = row.top_movers[0]
                return (
                  <TableRow key={row.id}>
                    <TableCell className="px-4 py-3 text-fg-tertiary">
                      {formatDriftBucket(row.bucket, daily)}
                    </TableCell>
                    <TableCell className="px-4 py-3 font-medium">{row.psi.toFixed(3)}</TableCell>
                    <TableCell className="hidden px-4 py-3 md:table-cell">
                      <Chip tone={driftBandTone(row.band)}>{row.band}</Chip>
                    </TableCell>
                    <TableCell className="px-4 py-3">
                      {topMover ? (
                        <span className="font-mono text-body-sm">
                          {topMover.value}: {formatPercent(topMover.baseline_share)} → {formatPercent(topMover.current_share)}
                        </span>
                      ) : (
                        <span className="text-fg-tertiary">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
