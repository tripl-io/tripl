import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { healthApi } from '@/api/health'
import { ErrorState } from '@/components/error-state'
import { HealthGradeCounts } from '@/components/health/health-averages'
import { HealthWorstList } from '@/components/health/health-worst-list'
import { Chip } from '@/components/primitives/chip'
import { Sparkline, type SparklineVariant } from '@/components/primitives/sparkline'
import { Panel } from '@/components/settings/kit'
import { Skeleton } from '@/components/ui/skeleton'
import { useBranchLinkProps } from '@/hooks/useBranch'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import {
  HEALTH_GRADE_COLOR,
  HEALTH_GRADE_LABEL,
  formatHealthDelta,
  gradeForScore,
  healthAriaLabel,
  healthDeltaTone,
  PLAN_HEALTH_TREND_DAYS,
  trendLabel,
  trendScores,
} from '@/lib/health'
import { projectHealthKey } from '@/lib/queryKeys'
import type { ProjectHealthResponse } from '@/types/health'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/**
 * The Overview's "Plan health" card (F15, #268): the main plan's mean score,
 * its change against the snapshot a week ago, the trend from the daily
 * snapshots, how many events sit in each grade and the five least healthy,
 * each linking to its event.
 */
export function OverviewPlanHealthPanel({
  slug,
  sparklineVariant = 'line',
}: {
  slug: string
  sparklineVariant?: SparklineVariant
}) {
  const query = useQuery({
    queryKey: projectHealthKey(slug, PLAN_HEALTH_TREND_DAYS),
    queryFn: ({ signal }) => healthApi.project(slug, PLAN_HEALTH_TREND_DAYS, signal),
    enabled: !!slug,
    meta: SILENT_ERROR_META,
    staleTime: 60_000,
  })
  const data: ProjectHealthResponse | undefined = query.data
  // Health sort exists on main only: the link switches there, like the worst list.
  const branchLink = useBranchLinkProps()
  const sortLink = branchLink(projectPath(currentOrgSlug(), slug, '/events?sort=health'), null)

  return (
    <Panel
      title="Plan health"
      subtitle="Main plan, fixed weights. Open an event for its breakdown."
      right={
        data && data.scored_events > 0 ? (
          <Link
            to={sortLink.to}
            onClick={sortLink.onClick}
            className="rounded-md px-2 py-1 text-body-sm no-underline transition-colors hover:bg-[var(--surface-hover)] text-accent"
          >
            Least healthy first
          </Link>
        ) : undefined
      }
    >
      <div className="p-4">
        {query.isError ? (
          <ErrorState
            title="Plan health unavailable"
            error={query.error}
            onRetry={() => {
              void query.refetch()
            }}
            retryLabel="Retry"
            compact
          />
        ) : !data ? (
          <div aria-busy="true" aria-label="Loading plan health" className="space-y-2.5">
            <Skeleton className="h-6 w-24" />
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-2/3" />
          </div>
        ) : data.score === null || data.scored_events === 0 ? (
          <p className="text-body-sm text-fg-tertiary">
            No events on the main plan to score yet.
          </p>
        ) : (
          <PlanHealthBody slug={slug} data={data} sparklineVariant={sparklineVariant} />
        )}
      </div>
    </Panel>
  )
}

function PlanHealthBody({
  slug,
  data,
  sparklineVariant,
}: {
  slug: string
  data: ProjectHealthResponse
  sparklineVariant: SparklineVariant
}) {
  const score = data.score ?? 0
  const grade = data.grade ?? gradeForScore(score)
  const delta = formatHealthDelta(data.score, data.previous_score)
  const scores = trendScores(data.trend)
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-x-5 gap-y-3">
        <div className="flex flex-col gap-px">
          <span className="micro-label text-fg-tertiary">Score</span>
          <span className="flex items-baseline gap-2">
            <span
              className="tnum text-display font-semibold leading-none"
              style={{ color: HEALTH_GRADE_COLOR[grade] }}
              aria-label={healthAriaLabel(score)}
              role="img"
            >
              <span aria-hidden="true">{score}</span>
            </span>
            <span className="text-caption text-fg-tertiary">/100 · {HEALTH_GRADE_LABEL[grade]}</span>
          </span>
        </div>
        {delta && (
          <Chip
            tone={healthDeltaTone(data.score, data.previous_score)}
            size="sm"
            className="tnum"
            title={`Against ${data.previous_score}/100 a week ago`}
          >
            {delta}
            <span className="text-fg-tertiary"> vs 7 days ago</span>
          </Chip>
        )}
        {scores.length > 1 && (
          <div className="flex flex-col gap-px" title={trendLabel(scores, PLAN_HEALTH_TREND_DAYS)}>
            <span className="micro-label text-fg-tertiary">Trend · {PLAN_HEALTH_TREND_DAYS}d</span>
            <div role="img" aria-label={trendLabel(scores, PLAN_HEALTH_TREND_DAYS)}>
              <Sparkline data={scores} variant={sparklineVariant} width={120} height={24} />
            </div>
          </div>
        )}
      </div>
      <HealthGradeCounts counts={data} />
      <div>
        <h3 className="mb-1 micro-label text-fg-tertiary">Least healthy</h3>
        <HealthWorstList slug={slug} items={data.worst ?? []} />
      </div>
    </div>
  )
}
