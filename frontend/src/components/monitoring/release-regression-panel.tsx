import { formatNumber } from '@/lib/format'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { PackageX, TrendingDown } from 'lucide-react'

import { eventMetricsApi } from '@/api/eventMetrics'
import { Chip } from '@/components/primitives/chip'
import { CodeToken } from '@/components/primitives/code-token'
import { CountBadge } from '@/components/primitives/count-badge'
import { SectionSkeleton } from '@/components/states'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { ErrorState } from '@/components/error-state'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { getScopeNavigationTarget } from '@/lib/monitoring'
import type { ReleaseComparabilityItem, ReleaseRegressionItem } from '@/types'
import { releaseRegressionsKey } from '@/lib/queryKeys'

interface ReleaseRegressionPanelProps {
  slug: string
  scanConfigId: string
  enabled?: boolean
}

// The app locale, not the browser's: the chart beside this list already
// prints its numbers in it (DS-30).
function formatCount(value: number): string {
  return formatNumber(Math.round(value))
}

function formatPct(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}

function versionPair(verdict: ReleaseComparabilityItem): string {
  if (!verdict.version) return ''
  return verdict.previous_version
    ? ` (${verdict.version} vs ${verdict.previous_version})`
    : ` (${verdict.version})`
}

/**
 * Why the pass could not judge the release, in the operator's terms. The
 * backend distinguishes "no comparison happened" from "the comparison was made
 * and withheld", and those are different things to wait for.
 */
function withheldReason(verdict: ReleaseComparabilityItem): string {
  switch (verdict.reason) {
    case 'no_baseline':
      return 'Fewer than two released versions have taken enough traffic to compare.'
    case 'baseline_no_volume':
      return `The baseline release has no volume in the comparison window${versionPair(verdict)}.`
    case 'population_mismatch':
      return (
        `The new release is still drawing a different population than the baseline` +
        `${versionPair(verdict)}: ${formatPct(verdict.emerging_share)} of its volume sits in ` +
        `scopes the baseline barely visited, above the ${formatPct(verdict.max_emerging_share)} ` +
        `bound. Composition-normalized findings are withheld until the mix settles; events that ` +
        `went completely silent are still reported.`
      )
    default:
      return 'The latest release cannot be judged yet.'
  }
}

function RegressionRow({ slug, item }: { slug: string; item: ReleaseRegressionItem }) {
  const isMissing = item.kind === 'missing'
  const Icon = isMissing ? PackageX : TrendingDown
  const dropPct = Math.max(0, Math.round((1 - item.ratio) * 100))
  // The row names another event (or event type) than the page it sits on, so
  // it links there instead of being dead text (MON-41). Through the
  // release-regression navigation rule: a place to LOOK at the entity, never
  // offered as evidence for the regression. It reads the event
  // page off `event_id` and the event-type page off `scope_ref`.
  const target = getScopeNavigationTarget(slug, {
    scope_type: 'release_regression',
    scope_ref: item.scope_ref,
    event_id: item.event_id,
  })
  return (
    <div className="flex items-start justify-between gap-3 py-2">
      <div className="flex min-w-0 flex-1 items-start gap-2">
        <Icon aria-hidden="true" className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
        <div className="min-w-0">
          <p className="truncate text-body font-medium">
            {target ? (
              <Link to={target.path} className="underline-offset-2 hover:underline">
                {item.scope_name}
              </Link>
            ) : item.scope_name}
          </p>
          <p className="text-body-sm text-fg-tertiary">
            {isMissing ? 'Disappeared in' : 'Dropped in'}{' '}
            <span className="font-mono">{item.version}</span>
            {' (was '}
            <span className="font-mono">{item.previous_version}</span>
            {')'}
          </p>
        </div>
      </div>
      <div className="flex items-center gap-2 whitespace-nowrap text-right text-body-sm">
        {isMissing
          ? <Chip tone="danger" size="xs">missing</Chip>
          : <Chip variant="outline" size="xs">{`-${dropPct}%`}</Chip>}
        {/* Counts, so sans with tabular digits (DS-17). */}
        <span className="tnum text-fg-tertiary">
          {formatCount(item.observed_count)} / {formatCount(item.expected_count)}
        </span>
      </div>
    </div>
  )
}

/**
 * Events (and event types) that disappeared or dropped in the latest active
 * release, from the release-regression summary endpoint. Rendered inside the
 * "By version" tab, so it only appears for scans with an app version column.
 *
 * The list covers the WHOLE scan, not the entity whose page it sits on, and
 * says so in its title: on one event's tab it read as that event's
 * regressions (MON-41).
 */
export function ReleaseRegressionPanel({
  slug,
  scanConfigId,
  enabled = true,
}: ReleaseRegressionPanelProps) {
  const query = useQuery({
    // The panel says so itself, below; a toast on top would say it twice.
    meta: SILENT_ERROR_META,
    queryKey: releaseRegressionsKey(slug, scanConfigId),
    queryFn: () => eventMetricsApi.getReleaseRegressions(slug, scanConfigId),
    enabled: enabled && !!slug && !!scanConfigId,
  })

  const items = query.data?.items ?? []
  const comparability = query.data?.comparability ?? []
  // A withheld verdict on any evaluated scope means the empty list below is not
  // a clean bill of health. Suppression keeps `missing` rows, so a withheld
  // verdict and a non-empty list coexist and both have to be shown.
  const withheld = comparability.filter(verdict => !verdict.comparable)
  const withheldReasons = [...new Set(withheld.map(withheldReason))]
  const judged = comparability.length > 0

  return (
    // The shared section-card geometry (DS-4 / MO-10): a header bar with the
    // 12.5px h2, then the list.
    <Card>
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle as="h2">
            Release regressions <span className="font-normal text-fg-tertiary">· whole scan</span>
          </CardTitle>
          {query.data?.latest_version && (
            <div className="flex items-center gap-1.5">
              <span className="text-caption text-fg-tertiary">latest active release</span>
              {/* A version is an identifier: a code token, not a pill (DS-6). */}
              <CodeToken>{query.data.latest_version}</CodeToken>
            </div>
          )}
        </div>
        {items.length > 0 && (
          // The count badge is decorative; the number is spoken beside it.
          <span className="inline-flex items-center">
            <CountBadge count={items.length} urgent />
            <span className="sr-only">
              {`${items.length} ${items.length === 1 ? 'regression' : 'regressions'}`}
            </span>
          </span>
        )}
      </CardHeader>
      <CardContent>
        {query.isLoading ? (
          <SectionSkeleton variant="list" rows={2} label="Loading regressions…" />
        ) : query.isError ? (
          // A failed request used to fall through to "No release comparison has
          // run for this scan yet" — an outage reading as a quiet release, the
          // worst possible reading for a regression detector (MON-30).
          <ErrorState
            title="Release regressions unavailable"
            error={query.error}
            onRetry={() => {
              void query.refetch()
            }}
            retryLabel="Retry"
            compact
          />
        ) : (
          <>
            {withheld.length > 0 && (
              <div className="mb-4 rounded-md border border-dashed p-3">
                <p className="text-body font-medium">Cannot be judged yet</p>
                {/* Every distinct reason, not only the first scope's: two
                    partitions can be withheld for different reasons. */}
                {withheldReasons.map(reason => (
                  <p key={reason} className="mt-1 text-body-sm text-fg-tertiary">
                    {reason}
                  </p>
                ))}
              </div>
            )}
            {items.length > 0 ? (
              <div className="divide-y">
                {items.map(item => (
                  <RegressionRow key={`${item.scope_type}:${item.scope_ref}`} slug={slug} item={item} />
                ))}
              </div>
            ) : withheld.length > 0 ? null : judged ? (
              <p className="text-body text-fg-tertiary">
                No events regressed in the latest release.
              </p>
            ) : (
              <p className="text-body text-fg-tertiary">
                No release comparison has run for this scan yet.
              </p>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}
