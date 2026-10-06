import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowRight, Info, ShieldCheck, ShieldX } from 'lucide-react'
import { projectsApi } from '@/api/projects'
import { reconciliationApi } from '@/api/reconciliation'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { Panel } from '@/components/settings/kit'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { TermHint } from '@/components/term-hint'
import { TERM_HINTS } from '@/lib/termHints'
import { SectionSkeleton, StatValueSkeleton } from '@/components/states'
import { Chip } from '@/components/primitives/chip'
import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { DEAD_EVENT_DAYS, formatPlanCoverage, planCoverageRatio } from '@/lib/coverage'
import { formatRelativeTime } from '@/lib/datetime'
import { formatNumber } from '@/lib/format'
import { eventNameLabel } from '@/lib/eventName'
import { getMonitoringPath } from '@/lib/monitoring'
import { coverageTone } from '@/lib/statusLexicon'
import { EventName } from '@/components/event-name'
import type { DeadEvent } from '@/api/reconciliation'
import { deadEventsKey, projectKey } from '@/lib/queryKeys'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

// Window for "is this event still emitting data". Shared with Reconciliation's
// Dead events panel — the "Triage in Reconciliation" link below hands off to
// it, and two different windows made the destination list disagree with the
// count that sent the user there.
const DEAD_DAYS = DEAD_EVENT_DAYS
const GAP_LIMIT = 50

// One-line clarifier for the headline number. "Plan coverage" sits one nav item
// from Reconciliation's data-match number and also reads as "coverage", so spell
// out that this counts implemented events, not events seen in warehouse data.
const PLAN_COVERAGE_HELP =
  'Share of active planned events marked implemented. Different from the Reconciliation data match, which measures how many planned events are actually seen in warehouse data.'

// The gap list is computed over a deliberately NARROWER population than the
// "Active events" stat above it: only implemented/live events that are old
// enough to have had a chance to emit can be "missing data".
// Said for the user, in an info tip beside the panel subtitle: the
// full-width line it used to be explained a cross-page discrepancy nobody had
// asked about and took four lines on a phone.
const GAP_BASIS_HELP = `Implemented events older than ${DEAD_DAYS} days that sent no data in the last ${DEAD_DAYS} days.`

// Every status that counts as active but not implemented: the coverage bar's
// remainder. Linked as an Events filter so "which ones?" has an answer.
const NOT_IMPLEMENTED_STATUSES = ['draft', 'in_review', 'ready_for_dev', 'deprecated'] as const

function notImplementedEventsPath(slug: string): string {
  const params = new URLSearchParams()
  for (const status of NOT_IMPLEMENTED_STATUSES) params.append('status', status)
  return projectPath(currentOrgSlug(), slug, `/events?${params}`)
}

export default function CoveragePage() {
  const { slug } = useParams<{ slug: string }>()

  const projectQuery = useQuery({
    queryKey: projectKey(slug),
    queryFn: () => projectsApi.get(slug!),
    enabled: !!slug,
  })
  const deadQuery = useQuery({
    queryKey: deadEventsKey(slug, DEAD_DAYS),
    queryFn: () => reconciliationApi.deadEvents(slug!, DEAD_DAYS),
    enabled: !!slug,
    staleTime: 60_000,
  })

  const summary = projectQuery.data?.summary
  const deadItems = deadQuery.data?.items ?? []
  const deadTotal = deadQuery.data?.total ?? deadItems.length

  // Plan (spec) coverage uses the canonical definition shared across the app:
  // the share of active (non-archived) events that are implemented.
  const active = summary?.active_event_count ?? 0
  const implemented = summary?.implemented_event_count ?? 0
  // Arithmetic remainder of the coverage bar. It is NOT the "In review" tile:
  // events that are neither implemented nor awaiting review (draft, ready for
  // dev …) land here too, so the bar is labelled "not implemented" rather than
  // "pending" to stop the two adjacent numbers reading as the same bucket.
  const notImplemented = Math.max(0, active - implemented)
  const coverageRatio = planCoverageRatio(implemented, active)

  const isNewProject = !!summary && summary.event_count === 0
  const noGaps = !!deadQuery.data && !deadQuery.isError && deadItems.length === 0

  return (
    <PageContainer>
      <PageHeader
        eyebrow="Govern"
        title="Coverage"
        titleAddon={slug && <TermHint slug={slug} {...TERM_HINTS.coverage} />}
        description="How much of your active plan is implemented, and which implemented events have gone quiet."
        actions={
          slug ? (
            <Button asChild variant="outline" size="sm">
              <Link to={projectPath(currentOrgSlug(), slug, '/reconciliation')} className="no-underline">
                <ArrowRight aria-hidden="true" />
                Reconciliation
              </Link>
            </Button>
          ) : undefined
        }
      />

      {/* Rollup */}
      {projectQuery.isError ? (
        <ErrorState
          title="Coverage unavailable"
          error={projectQuery.error}
          onRetry={() => {
            void projectQuery.refetch()
          }}
          retryLabel="Retry"
          compact
        />
      ) : (
        <>
          <MiniStatStrip boxed>
            <div title={PLAN_COVERAGE_HELP}>
              <MiniStat
                label="Plan coverage"
                value={summary ? formatPlanCoverage(implemented, active) : <StatValueSkeleton />}
                // The shared thresholds (they take a percent), so this tile and
                // Reconciliation cannot drift apart.
                tone={summary && active > 0 ? coverageTone(coverageRatio * 100) : 'neutral'}
                labelAddon={<Info className="h-3 w-3 shrink-0" aria-hidden />}
              />
            </div>
            <MiniStat
              label="Active events"
              value={summary ? formatNumber(active) : <StatValueSkeleton />}
            />
            <MiniStat
              label="Implemented"
              value={summary ? formatNumber(implemented) : <StatValueSkeleton />}
            />
            {/* "In review", the one name for the status count everywhere
                (#238); it was "Awaiting review" here only. */}
            <MiniStat
              label="In review"
              value={summary ? formatNumber(summary.review_pending_event_count) : <StatValueSkeleton />}
              tone={summary && summary.review_pending_event_count > 0 ? 'warning' : 'neutral'}
            />
            <MiniStat
              label="Archived"
              value={summary ? formatNumber(summary.archived_event_count) : <StatValueSkeleton />}
            />
          </MiniStatStrip>

          {/* Coverage bar: implemented vs pending across the active plan */}
          {summary && active > 0 && (
            <CoverageBar
              slug={slug}
              implemented={implemented}
              notImplemented={notImplemented}
              inReview={summary.review_pending_event_count}
              coverageLabel={formatPlanCoverage(implemented, active)}
            />
          )}
        </>
      )}

      {/* New project: nothing planned yet */}
      {isNewProject ? (
        <div className="flex min-h-[40vh] items-center justify-center">
          <EmptyState
            icon={ShieldCheck}
            title="No events to cover yet"
            description="Once your tracking plan has events, this page shows how much of it is implemented and which implemented events have no data."
            action={
              slug ? (
                <Button asChild size="sm">
                  <Link to={projectPath(currentOrgSlug(), slug, '/events')} className="no-underline">
                    Go to events
                    <ArrowRight className="h-3.5 w-3.5" />
                  </Link>
                </Button>
              ) : undefined
            }
          />
        </div>
      ) : (
        /* Instrumentation gaps: active events with no data in the window */
        <Panel
          title="Instrumentation gaps"
          subtitle={
            deadQuery.data ? (
              <span className="inline-flex items-center gap-1">
                {`${formatNumber(deadTotal)} implemented event${deadTotal === 1 ? '' : 's'} with no data in the last ${DEAD_DAYS} days`}
                <InfoTip help={GAP_BASIS_HELP} />
              </span>
            ) : undefined
          }
          right={
            slug && deadItems.length > 0 ? (
              <Link
                to={projectPath(currentOrgSlug(), slug, '/reconciliation')}
                className="flex items-center gap-1 text-caption no-underline hover:underline text-fg-secondary"
              >
                Triage in Reconciliation
                <ArrowRight className="h-3 w-3" />
              </Link>
            ) : undefined
          }
        >
          {deadQuery.isError ? (
            <div className="p-4">
              <ErrorState
                title="Gaps unavailable"
                error={deadQuery.error}
                onRetry={() => {
                  void deadQuery.refetch()
                }}
                retryLabel="Retry"
                compact
              />
            </div>
          ) : deadQuery.isLoading ? (
            <SectionSkeleton variant="rows" rows={3} label="Loading coverage gaps…" />
          ) : noGaps ? (
            <div
              className="flex items-center gap-2 px-4 py-6 text-body-sm text-fg-secondary"
            >
              <ShieldCheck className="h-4 w-4 text-success" />
              Every implemented event has recent data — no coverage gaps.
            </div>
          ) : (
            <div className="divide-y border-border-subtle">
              {deadItems.slice(0, GAP_LIMIT).map((item) => (
                <GapRow key={item.event_id} item={item} slug={slug} />
              ))}
              {deadItems.length > GAP_LIMIT && (
                <div className="px-4 py-2 text-caption text-fg-tertiary">
                  Showing {GAP_LIMIT} of {formatNumber(deadTotal)} — see Reconciliation for the full list.
                </div>
              )}
            </div>
          )}
        </Panel>
      )}
    </PageContainer>
  )
}

function CoverageBar({
  slug,
  implemented,
  notImplemented,
  inReview,
  coverageLabel,
}: {
  slug: string | undefined
  implemented: number
  notImplemented: number
  /** The "In review" tile's count; part of the not-implemented remainder. */
  inReview: number
  /**
   * The headline's own formatting (`formatPlanCoverage`). `Math.round` here
   * announced 322 of 323 as "100% of active events are implemented".
   */
  coverageLabel: string
}) {
  const total = implemented + notImplemented
  const implementedPct = total > 0 ? (implemented / total) * 100 : 0
  // In review is a subset of the remainder; the rest are drafts, events ready
  // for development and deprecated ones. Said under the bar so the tiles and
  // the bar add up at a glance.
  const inReviewShare = Math.min(inReview, notImplemented)
  const otherShare = notImplemented - inReviewShare
  const notImplementedCount = (
    <>
      <span
        className="font-semibold"
        style={{ color: notImplemented > 0 ? 'var(--warning)' : 'var(--fg)' }}
      >
        {formatNumber(notImplemented)}
      </span>{' '}
      not implemented
    </>
  )
  return (
    <div className="rounded-card border px-4 py-3 border-border-subtle">
      <div className="mb-2 flex items-center justify-between text-caption text-fg-secondary">
        <span>
          <span className="font-semibold text-fg">
            {formatNumber(implemented)}
          </span>{' '}
          implemented
        </span>
        {/* "Which ones?" answered: the remainder opens the Events list
            filtered to exactly those statuses. */}
        {slug && notImplemented > 0 ? (
          <Link
            to={notImplementedEventsPath(slug)}
            className="no-underline hover:underline text-inherit"
          >
            {notImplementedCount}
          </Link>
        ) : (
          <span>{notImplementedCount}</span>
        )}
      </div>
      <div
        className="flex h-2 overflow-hidden rounded-full bg-bg-sunken"
        role="img"
        aria-label={`${coverageLabel} of active events are implemented; ${formatNumber(notImplemented)} are not implemented yet.`}
      >
        <div style={{ width: `${implementedPct}%`, background: 'var(--success)' }} />
        <div style={{ width: `${100 - implementedPct}%`, background: 'var(--warning)' }} />
      </div>
      {notImplemented > 0 && (
        <p className="tnum mt-2 text-caption text-fg-tertiary">
          {`Not implemented: ${formatNumber(inReviewShare)} in review, ${formatNumber(otherShare)} draft, ready for dev or deprecated.`}
        </p>
      )}
    </div>
  )
}

/**
 * An info icon that shows `help` on hover or focus, and names it for a screen
 * reader. Carries its own provider, as TermHint does, so the page renders it
 * without the app's root one (page tests mount without it).
 */
function InfoTip({ help }: { help: string }) {
  return (
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            className="inline-flex shrink-0 rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
            aria-label={help}
          >
            <Info className="size-3 text-fg-tertiary" aria-hidden="true" />
          </button>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs whitespace-normal">{help}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

// Same drill-down and name rendering as Reconciliation's dead-event rows:
// the two lists show the same events one page apart.
function GapRow({ item, slug }: { item: DeadEvent; slug: string | undefined }) {
  const label = eventNameLabel(item.name)
  return (
    // Row height follows the Density setting.
    <div className="flex min-h-(--row-h) items-center gap-3 px-(--panel-pad) py-1">
      <ShieldX className="h-3.5 w-3.5 shrink-0 text-warning" aria-hidden="true" />
      {slug ? (
        <Link
          to={getMonitoringPath(slug, { scope_type: 'event', scope_ref: item.event_id })}
          className="min-w-0 flex-1 truncate text-body-sm font-medium hover:underline text-fg"
          title={label}
        >
          <EventName name={item.name} />
        </Link>
      ) : (
        <span
          className="min-w-0 flex-1 truncate text-body-sm font-medium text-fg"
          title={label}
        >
          <EventName name={item.name} />
        </span>
      )}
      {item.event_type_name && (
        <Chip variant="outline" size="xs">
          {item.event_type_name}
        </Chip>
      )}
      <span className="tnum w-28 shrink-0 text-right text-micro text-fg-tertiary">
        {item.last_seen_at ? formatRelativeTime(item.last_seen_at) : 'Never seen'}
      </span>
    </div>
  )
}
