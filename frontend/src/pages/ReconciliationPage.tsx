import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { TermHint, TERM_HINTS } from '@/components/term-hint'
import { Fragment, useState } from 'react'
import { Panel } from '@/components/settings/kit'
import { Link, useParams } from 'react-router-dom'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, ChevronDown, GitCompare, Inbox, Info } from 'lucide-react'
import { EmptyState } from '@/components/empty-state'
import {
  MAX_SHADOW_BATCH,
  reconciliationApi,
  type CoverageBucket,
  type CoverageSummary,
  type DeadEvent,
  type ShadowEvent,
  type ShadowEventStatus,
} from '@/api/reconciliation'
import { eventTypesApi } from '@/api/eventTypes'
import { duplicatesApi } from '@/api/duplicates'
import { DuplicateHints } from '@/components/duplicates/DuplicateHints'
import { visibleDuplicates } from '@/components/duplicates/duplicateHints'
import type { DuplicateCheckResult } from '@/types'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { useDemoScenarioActions } from '@/demo/demoScenarioContext'
import { SCENARIO_SEEDED } from '@/demo/scenarioModel'
import { Chip } from '@/components/primitives/chip'
import { Dot } from '@/components/primitives/dot'
import { ErrorState } from '@/components/error-state'
import { EventName } from '@/components/event-name'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useActiveBranchId } from '@/hooks/useBranch'
import { useConfirm } from '@/hooks/useConfirm'
import { DEAD_EVENT_DAYS } from '@/lib/coverage'
import { formatRelativeTime } from '@/lib/datetime'
import { countOf } from '@/lib/plural'
import { eventNameLabel } from '@/lib/eventName'
import { getMonitoringPath } from '@/lib/monitoring'
import { coverageTone, toneVar } from '@/lib/statusLexicon'
import {
  branchEventsKey,
  deadEventsKey,
  eventTypesKey,
  projectDeadEventsKey,
  projectEventTypesKey,
  projectEventsKey,
  projectKey,
  projectQueryOptions,
  projectShadowEventsKey,
  reconciliationCoverageKey,
  shadowEventsPagesKey,
} from '@/lib/queryKeys'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { hasExecutedScanJob } from '@/components/onboarding-utils'
import { useCanWriteProject } from '@/lib/permissions'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

const COVERAGE_DAYS = 14 as const
// Deliberately NOT COVERAGE_DAYS. Dead events answer a different question than
// the data-match card ("has this implemented event gone silent?" vs "what share
// of occurrences matched the plan?"), and Coverage's "Instrumentation gaps"
// panel links straight into this one. While that panel used 30 days and this
// one 14, the hand-off silently widened the population — 14 days is a weaker
// silence test, so this list showed MORE events than the count the user clicked.
// Both now share one constant, which also matches the backend
// default and the Events page's "Silent > 30d" filter. The panel subtitle names
// the window, so the page never leaves the look-back implicit.
const DEAD_DAYS = DEAD_EVENT_DAYS
const SHADOW_TABS: readonly ShadowEventStatus[] = ['new', 'accepted', 'dismissed']
const SHADOW_TAB_LABEL: Record<ShadowEventStatus, string> = {
  new: 'New',
  accepted: 'Accepted',
  dismissed: 'Dismissed',
}
// The inbox reads one page at a time, and "Show more" asks for the next one by
// offset (DATA-39), so every row of a large inbox is reachable.
const SHADOW_PAGE_SIZE = 100
// Dead events arrive as one unpaginated list. Rendering every row (each with a
// Radix checkbox) froze large plans, so the panel shows them a page at a time
// (DATA-47).
const DEAD_PAGE_SIZE = 200

type BulkAction = 'accept' | 'dismiss'
interface BulkProgress {
  action: BulkAction
  done: number
  total: number
}

// One-line clarifier for the headline number. It reads as "coverage" but is a
// different measure than the Coverage page's plan-coverage KPI, so spell out the
// distinction to stop the two governance views looking contradictory. The unit
// is warehouse OCCURRENCES (rows), not catalog entries — every other surface
// uses "events" for the latter, so this one has to say which it means.
const DATA_MATCH_HELP = `Share of tracked event occurrences in warehouse data that matched a planned event, over the last ${COVERAGE_DAYS} days. Counts occurrences (warehouse rows), not catalog entries. Different from Coverage, which measures how many active events are marked implemented.`

// Ceiling for an imperfect match. `coverage_pct` arrives rounded to 2 dp, so a
// single unmatched occurrence in 672 million comes back as exactly 100.0 and any
// rounding of it prints "100%" — a perfect score over an imperfect match.
// 100% is reserved for matched === total; everything else is
// held just below it.
const MAX_IMPERFECT_MATCH_PCT = 99.9

/**
 * Headline percentage for the Data match card. "100%" is reserved for a
 * genuinely complete match; anything that would otherwise round UP to it is
 * rounded DOWN instead, so the headline never claims a perfect score over an
 * imperfect match.
 */
function formatMatchPct(summary: CoverageSummary): string {
  if (summary.total_count > 0 && summary.matched_count >= summary.total_count) {
    return '100%'
  }
  const rounded = Math.round(summary.coverage_pct)
  if (rounded < 100) return `${rounded}%`
  // Rounding up here would print a perfect score over an unmatched occurrence,
  // so round down to a tenth and hold the result below 100. A 672-million-row
  // window with one miss arrives as exactly 100.0 (the backend already rounded
  // to 2 dp) and lands on the 99.9 ceiling; a genuine 99.6 keeps its own value
  // instead of being inflated to it.
  const flooredToTenth = Math.floor(summary.coverage_pct * 10) / 10
  return `${Math.min(flooredToTenth, MAX_IMPERFECT_MATCH_PCT)}%`
}

// Coverage heatmap colour. Resolved through the shared status lexicon so good
// coverage reads green (success) — matching the overview KPI — instead of the
// brand/accent it painted before.
function coverageColor(pct: number): string {
  return toneVar(coverageTone(pct))
}

function bucketPct(bucket: CoverageBucket): number {
  if (bucket.total_count <= 0) return 0
  return Math.min(100, (bucket.matched_count / bucket.total_count) * 100)
}

/**
 * A day's match as a whole percent for labels. Like the headline, 100 is kept
 * for a day where every occurrence matched; an imperfect day never rounds up
 * to it.
 */
function bucketLabelPct(bucket: CoverageBucket): number {
  if (bucket.total_count > 0 && bucket.matched_count >= bucket.total_count) return 100
  return Math.min(Math.round(bucketPct(bucket)), 99)
}

function hasBucketData(bucket: CoverageBucket): boolean {
  return bucket.total_count > 0
}

function pluralize(count: number, noun: string): string {
  return `${count.toLocaleString()} ${noun}${count === 1 ? '' : 's'}`
}

export default function ReconciliationPage() {
  const { slug } = useParams<{ slug: string }>()
  const branchId = useActiveBranchId()
  const qc = useQueryClient()
  const { notifyStepCompleted } = useDemoScenarioActions()
  // Accept, dismiss and archive are EditorUserDep (DATA-7); a viewer reads the
  // reconciliation without the checkboxes and buttons that only answer 403.
  const canWrite = useCanWriteProject()

  // Dead events are computed on the main branch only (the endpoint takes no
  // `?branch`), so on a feature branch archiving them would write straight to
  // main from a view that reads as branch-scoped. The panel says so and does
  // not offer the action there (DATA-42).
  const onFeatureBranch = branchId != null
  const canArchive = canWrite && !onFeatureBranch
  const { confirm, dialog } = useConfirm()

  const [shadowStatus, setShadowStatus] = useState<ShadowEventStatus>('new')
  const [selectedShadow, setSelectedShadow] = useState<ReadonlySet<string>>(() => new Set())
  const [bulkProgress, setBulkProgress] = useState<BulkProgress | null>(null)
  const [bulkNotice, setBulkNotice] = useState<string | null>(null)
  const [acceptingId, setAcceptingId] = useState<string | null>(null)
  // The row whose accept is waiting on the duplicate check (F12, #265).
  const [checkingAcceptId, setCheckingAcceptId] = useState<string | null>(null)
  const [selectedEventType, setSelectedEventType] = useState<Record<string, string>>({})
  const [rowError, setRowError] = useState<Record<string, string>>({})
  // A Set, not an array: select-all used `includes` over arrays, O(n²) on a
  // large plan (DATA-47).
  const [selectedDead, setSelectedDead] = useState<ReadonlySet<string>>(() => new Set())
  const [deadShown, setDeadShown] = useState(DEAD_PAGE_SIZE)
  const [deadError, setDeadError] = useState<string | null>(null)
  const [archiveNotice, setArchiveNotice] = useState<string | null>(null)

  const coverageQuery = useQuery({
    queryKey: reconciliationCoverageKey(slug, COVERAGE_DAYS),
    queryFn: () => reconciliationApi.coverage(slug!, COVERAGE_DAYS),
    enabled: !!slug,
  })

  const shadowQuery = useInfiniteQuery({
    queryKey: shadowEventsPagesKey(slug, branchId, shadowStatus),
    queryFn: ({ pageParam }) =>
      reconciliationApi.shadowEvents(
        slug!,
        { status: shadowStatus, limit: SHADOW_PAGE_SIZE, offset: pageParam },
        branchId,
      ),
    initialPageParam: 0,
    getNextPageParam: (lastPage, pages) => {
      const loaded = pages.reduce((sum, page) => sum + page.items.length, 0)
      return lastPage.items.length > 0 && loaded < lastPage.total ? loaded : undefined
    },
    enabled: !!slug,
  })

  const deadQuery = useQuery({
    queryKey: deadEventsKey(slug, DEAD_DAYS),
    queryFn: () => reconciliationApi.deadEvents(slug!, DEAD_DAYS),
    enabled: !!slug,
  })

  // Whether any scan has run here: the empty state below is for a project no
  // scan has read yet (JR-4). The shell already holds this query (Layout), so
  // it is a cache hit; silent, since a failure only means the panels show.
  const projectQuery = useQuery({
    ...projectQueryOptions(slug),
    enabled: !!slug,
    meta: SILENT_ERROR_META,
  })

  const eventTypesQuery = useQuery({
    queryKey: eventTypesKey(slug, branchId),
    queryFn: () => eventTypesApi.list(slug!, branchId),
    enabled: !!slug,
    staleTime: 60_000,
  })

  // Everything a reconciliation write can change outside its own list
  // (DATA-41). Accept adds a planned event and archive retires some, so the
  // project summary behind Coverage's counts, the event and event-type lists,
  // the data match and the dead list all move; before this they sat stale for
  // up to a minute and Coverage contradicted the action just taken.
  const invalidatePlan = () => {
    void qc.invalidateQueries({ queryKey: projectKey(slug) })
    void qc.invalidateQueries({ queryKey: branchEventsKey(slug, branchId) })
    void qc.invalidateQueries({ queryKey: projectEventsKey(slug) })
    void qc.invalidateQueries({ queryKey: projectEventTypesKey(slug) })
    void qc.invalidateQueries({ queryKey: reconciliationCoverageKey(slug, COVERAGE_DAYS) })
    void qc.invalidateQueries({ queryKey: projectDeadEventsKey(slug) })
  }

  // Dismiss only flips a candidate's status, so it refreshes the inbox alone;
  // accept also adds a planned event and refreshes the plan as well.
  const invalidateShadow = () => {
    void qc.invalidateQueries({ queryKey: projectShadowEventsKey(slug) })
  }

  const clearRowError = (id: string) =>
    setRowError((prev) => {
      const next = { ...prev }
      delete next[id]
      return next
    })

  const acceptMutation = useMutation({
    mutationFn: ({
      id,
      eventTypeId,
      name,
    }: {
      id: string
      eventTypeId?: string
      name?: string
    }) => reconciliationApi.acceptShadowEvent(slug!, id, { event_type_id: eventTypeId, name }, branchId),
    onSuccess: (_data, { id }) => {
      clearRowError(id)
      invalidateShadow()
      invalidatePlan()
      // Accepting a shadow event lands the reconcile chapter's step — inert
      // outside the demo scenario (the reducer drops every other step).
      notifyStepCompleted('reconcile/accept-shadow')
    },
    onError: (err: unknown, { id }) => {
      const msg = err instanceof Error ? err.message : 'Accept failed'
      setRowError((prev) => ({ ...prev, [id]: msg }))
    },
  })

  const dismissMutation = useMutation({
    mutationFn: (id: string) => reconciliationApi.dismissShadowEvent(slug!, id, branchId),
    onSuccess: (_data, id) => {
      clearRowError(id)
      invalidateShadow()
    },
    onError: (err: unknown, id) => {
      const msg = err instanceof Error ? err.message : 'Dismiss failed'
      setRowError((prev) => ({ ...prev, [id]: msg }))
    },
  })

  // Dead-events list is resolved on the default branch (the deadEvents query
  // sends no `?branch`), so archive must target the same branch to keep the
  // selected ids valid — otherwise the atomic endpoint 404s. It is therefore
  // only offered on main (`canArchive`); revisit if dead-events ever becomes
  // branch-aware.
  const archiveMutation = useMutation({
    mutationFn: (eventIds: string[]) => reconciliationApi.archiveDeadEvents(slug!, eventIds),
    onSuccess: (result) => {
      setSelectedDead(new Set())
      setDeadError(null)
      setArchiveNotice(`${pluralize(result.archived_count, 'event')} archived.`)
      invalidatePlan()
    },
    onError: (err: unknown) => {
      setDeadError(err instanceof Error ? err.message : 'Archive failed')
    },
  })

  // Before a shadow event joins the plan, ask whether the plan already has it
  // under another name (F12, #265): a scan that saw "Paywall Screen View" next
  // to a planned "Paywall View" should say so, not add a second entry
  // silently. Advisory and best-effort — a failed check accepts as before, and
  // the name is never offered for rewriting: it is what the app sends.
  const confirmNotDuplicate = async (item: ShadowEvent, eventTypeId: string | undefined) => {
    if (!slug || !eventTypeId) return true
    setCheckingAcceptId(item.id)
    let result: DuplicateCheckResult | undefined
    try {
      const response = await duplicatesApi.check(
        slug,
        [{ name: item.event_name, event_type_id: eventTypeId }],
        branchId,
      )
      result = response.items[0]
    } catch {
      return true
    } finally {
      setCheckingAcceptId(null)
    }
    if (!result || visibleDuplicates(result).length === 0) return true
    return confirm({
      title: 'Accept a possible duplicate?',
      message: (
        <>
          <p className="m-0">
            Accepting adds “{eventNameLabel(item.event_name)}” to the plan as an event of its own.
          </p>
          <DuplicateHints
            slug={slug}
            name={item.event_name}
            result={{ duplicates: result.duplicates, lint: [], suggestion: null }}
          />
        </>
      ),
      confirmLabel: 'Accept anyway',
      variant: 'primary',
    })
  }

  const acceptWithCheck = async (item: ShadowEvent, eventTypeId: string | undefined) => {
    if (!(await confirmNotDuplicate(item, eventTypeId))) return
    acceptMutation.mutate({ id: item.id, eventTypeId })
  }

  const handleAccept = (item: ShadowEvent) => {
    if (!item.event_type_name && !selectedEventType[item.id]) {
      setAcceptingId(item.id)
      return
    }
    void acceptWithCheck(item, item.event_type_id ?? selectedEventType[item.id] ?? undefined)
  }

  const coverage = coverageQuery.data
  // The pages read as one list. Counts come from the newest page, which is the
  // most recent answer to "how many are there".
  const shadowPages = shadowQuery.data?.pages
  const lastShadowPage = shadowPages?.[shadowPages.length - 1]
  const shadow = shadowPages && lastShadowPage
    ? {
        items: shadowPages.flatMap((page) => page.items),
        total: lastShadowPage.total,
        new_count: lastShadowPage.new_count,
      }
    : undefined
  const dead = deadQuery.data
  const eventTypes = eventTypesQuery.data ?? []
  const shadowIsEmpty = !!shadow && shadow.items.length === 0 && !shadowQuery.isError
  // No scan has ever run, and so nothing read, nothing unexpected, nothing
  // silent: every panel would only report an absence of data. Keyed on an
  // executed scan (JR-4), not on empty panels alone: a project whose shadow
  // events were all accepted or dismissed and that read nothing lately still
  // needs its Accepted / Dismissed history, and has scans. Decided once every
  // query answered, so a slow one never flashes the empty state over a
  // populated page.
  const projectSummary = projectQuery.data?.summary
  const nothingToReconcile =
    !!projectSummary &&
    !hasExecutedScanJob(projectSummary) &&
    !!coverage &&
    coverage.summary.total_count === 0 &&
    shadowStatus === 'new' &&
    shadowIsEmpty &&
    !!dead &&
    dead.items.length === 0 &&
    !deadQuery.isError

  // Bulk triage (DATA-39) works on the "new" rows on screen. The selection is
  // read through the current rows, so an id that a refetch dropped is never
  // acted on.
  const shadowSelectable = canWrite && shadowStatus === 'new'
  const newShadowItems = (shadow?.items ?? []).filter((item) => item.status === 'new')
  const selectedShadowItems = newShadowItems.filter((item) => selectedShadow.has(item.id))
  // Accept needs an event type; an untyped row is accepted on its own, where
  // the type picker is.
  const acceptableShadowItems = selectedShadowItems.filter((item) => !!item.event_type_id)
  const allShadowSelected =
    newShadowItems.length > 0 && newShadowItems.every((item) => selectedShadow.has(item.id))
  const bulkRunning = bulkProgress !== null

  const toggleShadowSelection = (id: string) =>
    setSelectedShadow((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const selectShadowTab = (tab: ShadowEventStatus) => {
    setShadowStatus(tab)
    setSelectedShadow(new Set())
    setBulkNotice(null)
  }

  // One batch request per MAX_SHADOW_BATCH rows (DATA-39). The server handles
  // each row on its own, so one refused row does not sink the rest; each
  // refusal lands on its own row, in the server's words.
  const runBulk = async (action: BulkAction, items: ShadowEvent[]) => {
    if (!slug || items.length === 0) return
    setBulkNotice(null)
    setAcceptingId(null)
    let succeeded = 0
    setBulkProgress({ action, done: 0, total: items.length })
    const fallback = action === 'accept' ? 'Accept failed' : 'Dismiss failed'
    for (let start = 0; start < items.length; start += MAX_SHADOW_BATCH) {
      const chunk = items.slice(start, start + MAX_SHADOW_BATCH)
      try {
        const response = await reconciliationApi.batchShadowEvents(
          slug,
          {
            action,
            items: chunk.map((item) =>
              action === 'accept'
                ? { candidate_id: item.id, event_type_id: item.event_type_id ?? undefined }
                : { candidate_id: item.id },
            ),
          },
          branchId,
        )
        for (const result of response.results) {
          if (result.ok) {
            succeeded += 1
            clearRowError(result.candidate_id)
          } else {
            const msg = result.error ?? fallback
            setRowError((prev) => ({ ...prev, [result.candidate_id]: msg }))
          }
        }
      } catch (err) {
        // The request itself failed: nothing in this chunk is known to have
        // happened, so every row of it says so.
        const msg = err instanceof Error ? err.message : fallback
        setRowError((prev) => {
          const next = { ...prev }
          for (const item of chunk) next[item.id] = msg
          return next
        })
      }
      setBulkProgress({ action, done: Math.min(start + chunk.length, items.length), total: items.length })
    }
    setBulkProgress(null)
    setSelectedShadow(new Set())
    // Same demo-scenario step a single accept lands; inert outside the demo.
    if (action === 'accept' && succeeded > 0) notifyStepCompleted('reconcile/accept-shadow')
    const verb = action === 'accept' ? 'accepted' : 'dismissed'
    const failed = items.length - succeeded
    setBulkNotice(
      `${pluralize(succeeded, 'event')} ${verb}.${failed > 0 ? ` ${failed.toLocaleString()} failed; see the rows below.` : ''}`,
    )
    invalidateShadow()
    if (action === 'accept') invalidatePlan()
  }

  const deadItems = dead?.items ?? []
  const shownDeadItems = deadItems.slice(0, deadShown)
  const shownDeadIds = shownDeadItems.map((item) => item.event_id)
  // Read the selection through the current list: an id that dropped out of a
  // refetch used to stay selected, and the atomic archive endpoint then 404ed
  // the whole batch (DATA-47).
  const selectedDeadIds = deadItems
    .map((item) => item.event_id)
    .filter((id) => selectedDead.has(id))
  const allDeadSelected =
    shownDeadIds.length > 0 && shownDeadIds.every((id) => selectedDead.has(id))
  const hasDeadSelection = selectedDeadIds.length > 0
  const someDeadSelected = shownDeadIds.some((id) => selectedDead.has(id))
  const hiddenDeadCount = deadItems.length - shownDeadItems.length

  const toggleDeadSelection = (id: string) =>
    setSelectedDead((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const toggleSelectAllDead = (checked: boolean) =>
    setSelectedDead(checked ? new Set(shownDeadIds) : new Set())

  // Archive asks first and reports what it did (DATA-40): select-all plus one
  // click used to retire the whole list silently.
  const handleArchive = async () => {
    const ids = selectedDeadIds
    if (ids.length === 0) return
    const ok = await confirm({
      title: 'Archive dead events',
      message: `Archive ${pluralize(ids.length, 'planned event')}? Archived events leave the active plan and stop counting towards Coverage.`,
      confirmLabel: 'Archive',
      variant: 'danger',
    })
    if (!ok) return
    setArchiveNotice(null)
    archiveMutation.mutate(ids)
  }

  return (
    <PageContainer>
      {dialog}
      <PageHeader
        eyebrow="Govern"
        title="Reconciliation"
        titleAddon={slug && <TermHint slug={slug} {...TERM_HINTS.reconciliation} />}
        description="Compare what your plan defines against what your data sources actually send."
      />

      {!canWrite && <ReadOnlyNotice />}

      {nothingToReconcile ? (
        // A first-run project read "No unexpected events" and "No dead
        // events" under a big "0%": reassurance that was really an absence
        // of data. One state that says what the page needs instead (DA-29).
        <EmptyState
          icon={GitCompare}
          title="Nothing to reconcile yet"
          description="Reconciliation compares your plan with what a scan reads from your warehouse. No scan has run in this project yet; run one first."
          action={
            slug ? (
              <Button asChild size="lg">
                <Link to={projectPath(currentOrgSlug(), slug, '/scans')} className="no-underline">
                  Go to Scans
                  <ArrowRight aria-hidden="true" />
                </Link>
              </Button>
            ) : undefined
          }
        />
      ) : (
        <>
          {/* Data match — share of planned events actually seen in data (distinct from plan coverage) */}
          {/* The window is a static label on the panel it describes, not a disabled
              button that read as a greyed-out date picker (DATA-44). Dead events
              runs on the shared DEAD_EVENT_DAYS window and names it itself, so a
              page-level "Last 14 days" would misdescribe that panel. */}
          <Panel
            title="Data match"
            right={<Chip size="xs">Last {COVERAGE_DAYS} days</Chip>}
            subtitle={
              coverage
                ? `${coverage.summary.matched_count.toLocaleString()} of ${coverage.summary.total_count.toLocaleString()} tracked event occurrences matched a planned event · ${coverage.days}d`
                : undefined
            }
          >
            {coverageQuery.isError && (
              <div className="p-4">
                <ErrorState
                  title="Data match unavailable"
                  error={coverageQuery.error}
                  onRetry={() => {
                    void coverageQuery.refetch()
                  }}
                  retryLabel="Retry"
                  compact
                />
              </div>
            )}
            {coverageQuery.isLoading && (
              <SectionSkeleton variant="rows" rows={2} label="Loading data match…" />
            )}
            {coverage && (
              <div className="flex items-center gap-6 p-4">
                <div className="flex min-w-[120px] flex-col gap-0.5">
                  {/* The hero figure: the display step of the type scale (DS-13),
                      sans with tabular digits rather than mono (DS-17). */}
                  {/* No occurrences is not a result: a neutral "—", not a big
                      accent "0%" (DA-29). */}
                  <span
                    className="tnum text-display font-semibold leading-none"
                    style={{
                      color: coverage.summary.total_count > 0 ? 'var(--accent)' : 'var(--fg-faint)',
                    }}
                  >
                    {coverage.summary.total_count > 0 ? formatMatchPct(coverage.summary) : '—'}
                  </span>
                  <span
                    className="inline-flex items-center gap-1 text-caption text-fg-tertiary"
                    title={DATA_MATCH_HELP}
                  >
                    occurrences matched
                    <Info
                      className="h-3 w-3 shrink-0 text-fg-tertiary"
                      aria-hidden
                    />
                  </span>
                </div>
                <CoverageStrip items={coverage.items} days={coverage.days} />
              </div>
            )}
          </Panel>

          <div
            // items-start: a one-line panel no longer stretches to its
            // neighbour's height (DA-29).
            className={`grid grid-cols-1 items-start gap-3 ${
              shadowIsEmpty ? 'lg:grid-cols-[auto_1fr]' : 'lg:grid-cols-[1.5fr_1fr]'
            }`}
          >
            {/* Shadow events inbox */}
            <Panel
              title="Shadow events inbox"
              subtitle="Seen in data, missing from plan"
              // No panel tone: a queue beside the neutral Dead events panel, not
              // an alert. The warning sits on the New count alone (DA-33).
              right={
                // Three views of one inbox: the shared segmented control (DS-16)
                // rather than a fourth hand-rolled look. Switching mid-run would
                // clear the selection and land the run's result notice in the
                // other view's panel, so it is disabled while one runs.
                <SegmentedControl
                  aria-label="Shadow event status"
                  size="sm"
                  value={shadowStatus}
                  onChange={selectShadowTab}
                  options={SHADOW_TABS.map((tab) => ({
                    value: tab,
                    disabled: bulkRunning,
                    label: (
                      <>
                        {SHADOW_TAB_LABEL[tab]}
                        {/* The space sits outside the span: inside it, the
                            accessible name collapsed to "New250". */}
                        {tab === 'new' && shadow && shadow.new_count > 0 && (
                          <>
                            {' '}
                            <Chip tone="warning" size="xs" className="tnum">
                              {shadow.new_count.toLocaleString()}
                            </Chip>
                          </>
                        )}
                      </>
                    ),
                  }))}
                />
              }
            >
              {shadowQuery.isError && (
                <div className="p-4">
                  <ErrorState
                    title="Shadow events unavailable"
                    error={shadowQuery.error}
                    onRetry={() => {
                      void shadowQuery.refetch()
                    }}
                    retryLabel="Retry"
                    compact
                  />
                </div>
              )}
              {shadowQuery.isLoading && (
                <SectionSkeleton variant="rows" rows={3} label="Loading unplanned events…" />
              )}
              {shadowIsEmpty &&
                (shadowStatus === 'new' ? (
                  <div className="flex min-h-[240px] flex-col items-center justify-center gap-1.5 px-4 py-6 text-center">
                    <Inbox className="h-4 w-4 text-fg-tertiary" aria-hidden />
                    <div className="text-body-sm font-medium text-fg-secondary">
                      No new events
                    </div>
                    <div className="text-micro text-fg-tertiary">
                      No unexpected events seen in the last {COVERAGE_DAYS} days.
                    </div>
                  </div>
                ) : (
                  <div
                    className="flex min-h-[240px] flex-col items-center justify-center px-4 py-6 text-center text-body-sm text-fg-tertiary"
                  >
                    No {shadowStatus} events.
                  </div>
                ))}
              {shadowSelectable && newShadowItems.length > 0 && (
                <div className="flex flex-wrap items-center gap-2.5 px-4 py-2">
                  <Checkbox
                    // Mixed while only some rows are picked, and a click from there
                    // clears the selection rather than selecting everything (EV-26).
                    checked={
                      allShadowSelected ? true : selectedShadowItems.length > 0 ? 'indeterminate' : false
                    }
                    onCheckedChange={() =>
                      setSelectedShadow(
                        selectedShadowItems.length === 0
                          ? new Set(newShadowItems.map((item) => item.id))
                          : new Set(),
                      )
                    }
                    disabled={bulkRunning}
                    aria-label="Select all new shadow events"
                  />
                  {/* The inbox's one primary; the rows' own Accept is outline (DA-32). */}
                  <Button
                    size="sm"
                    disabled={bulkRunning || acceptableShadowItems.length === 0}
                    onClick={() => {
                      void runBulk('accept', acceptableShadowItems)
                    }}
                    title="Accept the selected events that already have an event type"
                  >
                    {acceptableShadowItems.length > 0
                      ? `Accept ${acceptableShadowItems.length} selected`
                      : 'Accept selected'}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={bulkRunning || selectedShadowItems.length === 0}
                    onClick={() => {
                      void runBulk('dismiss', selectedShadowItems)
                    }}
                  >
                    {selectedShadowItems.length > 0
                      ? `Dismiss ${selectedShadowItems.length} selected`
                      : 'Dismiss selected'}
                  </Button>
                  {selectedShadowItems.length > acceptableShadowItems.length && !bulkRunning && (
                    <span className="text-micro text-fg-tertiary">
                      Rows without an event type are accepted one at a time.
                    </span>
                  )}
                </div>
              )}
              {(bulkProgress || bulkNotice) && (
                <div role="status" className="px-4 pb-2 text-caption text-fg-secondary">
                  {bulkProgress
                    ? `${bulkProgress.action === 'accept' ? 'Accepting' : 'Dismissing'} ${bulkProgress.done} of ${bulkProgress.total}…`
                    : bulkNotice}
                </div>
              )}
              {shadow?.items.map((item) => {
                const isActing =
                  bulkRunning ||
                  checkingAcceptId === item.id ||
                  (acceptMutation.isPending && acceptMutation.variables?.id === item.id) ||
                  (dismissMutation.isPending && dismissMutation.variables === item.id)
                const needsEventTypeSelect = acceptingId === item.id && !item.event_type_name
                return (
                  <ShadowRow
                    key={item.id}
                    item={item}
                    slug={slug}
                    isActing={isActing}
                    needsEventTypeSelect={needsEventTypeSelect}
                    eventTypes={eventTypes}
                    selectedEventTypeId={selectedEventType[item.id] ?? ''}
                    error={rowError[item.id]}
                    onAccept={canWrite ? () => handleAccept(item) : undefined}
                    onDismiss={canWrite ? () => {
                      setAcceptingId(null)
                      dismissMutation.mutate(item.id)
                    } : undefined}
                    onSelectEventType={(value) =>
                      setSelectedEventType((prev) => ({ ...prev, [item.id]: value }))
                    }
                    onConfirm={() => {
                      const eventTypeId = selectedEventType[item.id]
                      if (!eventTypeId) return
                      setAcceptingId(null)
                      void acceptWithCheck(item, eventTypeId)
                    }}
                    onCancel={() => setAcceptingId(null)}
                    confirmDisabled={!selectedEventType[item.id] || acceptMutation.isPending}
                    selected={selectedShadow.has(item.id)}
                    // The checkbox stays mounted but disabled during a bulk run, so
                    // the rows do not shift sideways while it runs.
                    selectDisabled={bulkRunning}
                    onToggleSelect={
                      shadowSelectable && item.status === 'new'
                        ? () => toggleShadowSelection(item.id)
                        : undefined
                    }
                  />
                )
              })}
              {/* The inbox is paged; it used to stop at 100 rows without saying so,
                  and then at 500 with no way past them (DATA-39). */}
              {shadow && shadow.total > shadow.items.length && (
                <div
                  className="flex flex-wrap items-center gap-2.5 border-t px-4 py-2 text-caption border-border-subtle text-fg-tertiary"
                >
                  <span>
                    Showing {shadow.items.length.toLocaleString()} of {shadow.total.toLocaleString()}
                  </span>
                  {shadowQuery.hasNextPage && (
                    <Button
                      size="xs"
                      variant="outline"
                      disabled={bulkRunning || shadowQuery.isFetching}
                      onClick={() => {
                        void shadowQuery.fetchNextPage()
                      }}
                    >
                      {shadowQuery.isFetchingNextPage ? 'Loading…' : 'Show more'}
                    </Button>
                  )}
                </div>
              )}
            </Panel>

            {/* Dead events */}
            <Panel
              title="Dead events"
              // Name the window and the population. Coverage links here from its
              // "Instrumentation gaps" panel, so leaving this as "not seen recently"
              // made two adjacent surfaces look like they disagreed about the same
              // question. Both now compute over DEAD_EVENT_DAYS, so
              // this subtitle and Coverage's report the same number.
              subtitle={`Implemented events with no data in the last ${DEAD_DAYS} days${
                onFeatureBranch ? ' · main branch' : ''
              }`}
              right={
                canArchive && deadItems.length > 0 ? (
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={!hasDeadSelection || archiveMutation.isPending}
                    onClick={() => {
                      void handleArchive()
                    }}
                    title="Archive the selected planned events"
                  >
                    {archiveMutation.isPending
                      ? 'Archiving…'
                      : hasDeadSelection
                        ? `Archive ${selectedDeadIds.length} selected`
                        : 'Archive selected'}
                  </Button>
                ) : undefined
              }
            >
              {deadItems.length > 0 && (
                // The advice says when archiving is right rather than "often
                // expected", which hinted it was often wrong without saying when
                // (DA-34). "Planned" also disagreed with the "Implemented" subtitle.
                <div className="flex flex-col gap-1.5 px-4 py-2">
                  <p className="text-caption text-fg-tertiary">
                    Seasonal or rarely fired events can show up here; archive only what you have
                    retired.
                  </p>
                  {canArchive && (
                    <div className="flex items-center gap-2.5">
                      <Checkbox
                        id="dead-select-all"
                        // Mixed while only some rows are picked; a click from there
                        // clears the selection (EV-26).
                        checked={allDeadSelected ? true : someDeadSelected ? 'indeterminate' : false}
                        onCheckedChange={() => toggleSelectAllDead(!someDeadSelected)}
                        aria-label="Select all dead events"
                      />
                      <label
                        htmlFor="dead-select-all"
                        className="text-caption text-fg-secondary"
                      >
                        Select all
                      </label>
                    </div>
                  )}
                </div>
              )}
              {canWrite && onFeatureBranch && deadItems.length > 0 && (
                <div className="px-4 pb-2 text-micro text-fg-tertiary">
                  Dead events are checked on the main branch, and archiving them changes main. Switch
                  to main to archive them.
                </div>
              )}
              {archiveNotice && (
                <div role="status" className="px-4 pb-2 text-caption text-fg-secondary">
                  {archiveNotice}
                </div>
              )}
              {deadError && (
                <div
                  className="px-4 pb-2 text-caption text-danger"
                  role="alert"
                >
                  {deadError}
                </div>
              )}
              {deadQuery.isError && (
                <div className="p-4">
                  <ErrorState
                    title="Dead events unavailable"
                    error={deadQuery.error}
                    onRetry={() => {
                      void deadQuery.refetch()
                    }}
                    retryLabel="Retry"
                    compact
                  />
                </div>
              )}
              {deadQuery.isLoading && (
                <SectionSkeleton variant="rows" rows={3} label="Loading dead events…" />
              )}
              {dead && dead.items.length === 0 && !deadQuery.isError && (
                <div className="flex min-h-[240px] flex-col items-center justify-center px-4 py-7 text-center text-body-sm text-fg-tertiary">
                  No dead events in the last {dead.days} days.
                </div>
              )}
              {shownDeadItems.map((item) => (
                <DeadRow
                  key={item.event_id}
                  item={item}
                  slug={slug}
                  selected={selectedDead.has(item.event_id)}
                  onToggle={canArchive ? toggleDeadSelection : undefined}
                />
              ))}
              {hiddenDeadCount > 0 && (
                <div
                  className="flex flex-wrap items-center gap-2.5 border-t px-4 py-2 text-caption border-border-subtle text-fg-tertiary"
                >
                  <span>
                    Showing {shownDeadItems.length.toLocaleString()} of{' '}
                    {deadItems.length.toLocaleString()}
                  </span>
                  <Button
                    size="xs"
                    variant="outline"
                    onClick={() => setDeadShown((shown) => shown + DEAD_PAGE_SIZE)}
                  >
                    Show {Math.min(hiddenDeadCount, DEAD_PAGE_SIZE).toLocaleString()} more
                  </Button>
                </div>
              )}
            </Panel>
          </div>
        </>
      )}
    </PageContainer>
  )
}


// Coverage is "steady" when every bucket has data and rounds to the same
// whole percent — the histogram then carries no signal worth its visual
// weight. A bucket without data is never steady: the gap is the signal.
function hasCoverageVariation(items: CoverageBucket[]): boolean {
  const [head] = items
  if (items.length < 2 || !head) return false
  const first = bucketLabelPct(head)
  return items.some((bucket) => !hasBucketData(bucket) || bucketLabelPct(bucket) !== first)
}

type BucketUnit = 'hour' | 'day'

const HOUR_MS = 60 * 60 * 1000

/**
 * What one bucket spans, read off the spacing of the buckets themselves. The
 * backend buckets per scan run (hourly for the demo), not per day, so counting
 * buckets as days printed "on each of the last 335 days" inside a 14-day panel
 * (DA-3). Anything under a day reads as hourly; a single bucket or unparsable
 * timestamps fall back to days.
 */
function bucketUnit(items: CoverageBucket[]): BucketUnit {
  let smallest = Number.POSITIVE_INFINITY
  for (let i = 1; i < items.length; i += 1) {
    const prev = items[i - 1]
    const next = items[i]
    if (!prev || !next) continue
    const gap = Date.parse(next.bucket) - Date.parse(prev.bucket)
    if (Number.isFinite(gap) && gap > 0) smallest = Math.min(smallest, gap)
  }
  return smallest < 24 * HOUR_MS ? 'hour' : 'day'
}

/** Spoken summary of the histogram: the window, the range, the latest bucket, and the gaps. */
function describeDataMatch(items: CoverageBucket[], days: number, unit: BucketUnit): string {
  const withData = items.filter(hasBucketData)
  const parts = [`Data match per ${unit} over the last ${pluralize(days, 'day')}`]
  const latest = withData[withData.length - 1]
  if (latest) {
    const pcts = withData.map(bucketLabelPct)
    parts.push(
      `lowest ${Math.min(...pcts)}%`,
      `highest ${Math.max(...pcts)}%`,
      `latest ${bucketLabelPct(latest)}% on ${latest.bucket}`,
    )
  }
  const noData = items.length - withData.length
  if (noData > 0) parts.push(`${pluralize(noData, unit)} without data`)
  return parts.join('; ')
}

// Faint reference lines on the fixed 0–100% scale, so a bar's height reads as
// a value rather than only relative to its neighbours (LIVE-28).
const GRIDLINES_PCT = [100, 50] as const

function CoverageStrip({ items, days }: { items: CoverageBucket[]; days: number }) {
  const [head] = items
  if (!head) {
    return (
      <div className="flex-1 text-caption text-fg-tertiary">
        No data-match history yet.
      </div>
    )
  }
  const unit = bucketUnit(items)
  if (!hasCoverageVariation(items) && hasBucketData(head)) {
    // Constant coverage carries no per-bucket signal. A flat line across most
    // of the card said nothing without a scale (LIVE-28), so say it in words.
    // The window comes from `days`, never from the bucket count (DA-3).
    const steadyPct = bucketLabelPct(head)
    return (
      <div className="flex flex-1 items-center">
        <div
          role="img"
          aria-label={`Data match steady at ${steadyPct}% across the window`}
          className="inline-flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-caption border-border-subtle text-fg-secondary"
        >
          <Dot tone={coverageTone(steadyPct)} size={6} />
          Stable at {steadyPct}% in every {unit} with data over the last {pluralize(days, 'day')}
        </div>
      </div>
    )
  }
  return (
    <div className="flex-1">
      {/* role="img" with a spoken summary; the per-bucket values are in the
          visually hidden table below. The bars' `title`s cannot be reached by
          touch, keyboard or a screen reader, and their colour alone carried
          the tone (DATA-43). */}
      <div className="relative h-14" role="img" aria-label={describeDataMatch(items, days, unit)}>
        {GRIDLINES_PCT.map((line) => (
          <div
            key={line}
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-0 border-t border-dashed"
            style={{ bottom: `${line}%`, borderColor: 'var(--border-subtle)' }}
          />
        ))}
        <div className="relative flex h-full items-end gap-0.5">
          {items.map((bucket) => {
            if (!hasBucketData(bucket)) {
              // No occurrences is not "0% matched": a neutral dashed outline,
              // not the 2%-high danger-red bar it used to be.
              return (
                <div
                  key={bucket.bucket}
                  title={`${bucket.bucket}: no data`}
                  className="h-full flex-1 rounded-sm border border-dashed border-border"
                />
              )
            }
            const pct = bucketPct(bucket)
            return (
              <div
                key={bucket.bucket}
                title={`${bucket.bucket}: ${bucketLabelPct(bucket)}%`}
                className="flex-1 rounded-sm"
                style={{
                  height: `${Math.max(pct, 2)}%`,
                  background: coverageColor(pct),
                  opacity: 0.85,
                }}
              />
            )
          })}
        </div>
      </div>
      <table className="sr-only">
        <caption>Data match per {unit}</caption>
        <thead>
          <tr>
            <th scope="col">{unit === 'hour' ? 'Hour' : 'Day'}</th>
            <th scope="col">Matched</th>
          </tr>
        </thead>
        <tbody>
          {items.map((bucket) => (
            <tr key={bucket.bucket}>
              <td>{bucket.bucket}</td>
              <td>{hasBucketData(bucket) ? `${bucketLabelPct(bucket)}%` : 'no data'}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div
        className="tnum mt-1.5 flex justify-between text-micro text-fg-tertiary"
      >
        <span>−{days}d</span>
        <span aria-hidden="true">scale 0–100%</span>
        <span>today</span>
      </div>
    </div>
  )
}

/** The rows the collector saw for this identity, one small key/value list each. */
function ShadowSamples({ id, samples }: { id: string; samples: ReadonlyArray<Record<string, string>> }) {
  return (
    <ul id={id} className="flex flex-col gap-1.5" aria-label="Sample properties">
      {samples.map((sample, index) => (
        <li
          key={index}
          className="rounded-md border px-2.5 py-1.5 border-border-subtle bg-bg-sunken"
        >
          <dl className="grid grid-cols-[minmax(0,max-content)_minmax(0,1fr)] gap-x-3 gap-y-0.5 text-caption">
            {Object.entries(sample).map(([key, value]) => (
              <Fragment key={key}>
                <dt className="mono truncate text-fg-tertiary">{key}</dt>
                <dd className="mono break-all text-fg">{value}</dd>
              </Fragment>
            ))}
          </dl>
        </li>
      ))}
    </ul>
  )
}

function ShadowRow({
  item,
  slug,
  isActing,
  needsEventTypeSelect,
  eventTypes,
  selectedEventTypeId,
  error,
  onAccept,
  onDismiss,
  onSelectEventType,
  onConfirm,
  onCancel,
  confirmDisabled,
  selected = false,
  selectDisabled = false,
  onToggleSelect,
}: {
  item: ShadowEvent
  slug: string | undefined
  isActing: boolean
  needsEventTypeSelect: boolean
  eventTypes: ReadonlyArray<{ id: string; display_name: string }>
  selectedEventTypeId: string
  error?: string
  /** Omitted for a viewer, as is `onDismiss`. */
  onAccept?: () => void
  onDismiss?: () => void
  onSelectEventType: (value: string) => void
  onConfirm: () => void
  onCancel: () => void
  confirmDisabled: boolean
  selected?: boolean
  selectDisabled?: boolean
  /** Omitted when the row cannot be bulk-selected (a viewer, a resolved row). */
  onToggleSelect?: () => void
}) {
  const [samplesOpen, setSamplesOpen] = useState(false)
  const samples = item.sample_properties ?? []
  const samplesId = `shadow-samples-${item.id}`
  return (
    // Row height follows the Density setting (DS-9).
    <div
      className="flex min-h-(--row-h) flex-col justify-center gap-2 border-t px-4 py-2 border-border-subtle"
    >
      <div className="flex items-center gap-2.5">
        {onToggleSelect && (
          <Checkbox
            checked={selected}
            onCheckedChange={onToggleSelect}
            disabled={selectDisabled}
            aria-label={`Select ${eventNameLabel(item.event_name)}`}
          />
        )}
        <div className="min-w-0 flex-1">
          <span className="mono text-body-sm text-fg">
            <EventName name={item.event_name} />
          </span>
          {/* Each separator opens the item after it, so a wrapped line
              starts with "·" instead of leaving one dangling at the end of
              the line above (DA-32). The scan links to where it was seen. */}
          <div
            className="mt-0.5 flex flex-wrap items-center gap-x-1.5 text-micro [&>*+*]:before:mr-1.5 [&>*+*]:before:content-['·'] text-fg-tertiary"
          >
            {slug ? (
              <Link to={projectPath(currentOrgSlug(), slug, `/scans/${item.scan_config_id}`)} className="hover:underline">
                {item.scan_config_name}
              </Link>
            ) : (
              <span>{item.scan_config_name}</span>
            )}
            <span className="tnum">{item.observed_count.toLocaleString()} seen</span>
            <span>last seen {formatRelativeTime(item.last_seen_at)}</span>
            {/* What the event looks like before it is accepted: the rows the
                latest collection saw for it (DA-32). */}
            {samples.length > 0 && (
              <button
                type="button"
                className="inline-flex items-center gap-0.5 hover:underline"
                aria-expanded={samplesOpen}
                aria-controls={samplesId}
                onClick={() => setSamplesOpen(open => !open)}
              >
                {samplesOpen ? 'Hide samples' : `Show ${countOf(samples.length, 'sample', 'samples')}`}
                <ChevronDown
                  aria-hidden
                  className={`size-3 transition-transform ${samplesOpen ? 'rotate-180' : ''}`}
                />
              </button>
            )}
          </div>
        </div>
        {item.event_type_name ? (
          // Labelled: a bare "Click" chip did not say it was the event type.
          <Chip variant="outline" size="xs">type: {item.event_type_name}</Chip>
        ) : (
          <span className="shrink-0 text-micro text-fg-tertiary">
            no type
          </span>
        )}
        {item.status === 'new' && onAccept && onDismiss && (
          <div className="flex shrink-0 gap-1.5">
            {/* Exactly one row coaches: the seeded shadow candidate. */}
            <ScenarioCoachMark
              step="reconcile/accept-shadow"
              when={item.event_name === SCENARIO_SEEDED.shadowCandidateName}
            >
              {/* Outline: a column of solid primaries down a long inbox left
                  no primary at all. The bulk "Accept N selected" is the
                  queue's action (DA-32). */}
              <Button size="sm" variant="outline" disabled={isActing} onClick={onAccept}>
                Accept
              </Button>
            </ScenarioCoachMark>
            <Button size="sm" variant="ghost" disabled={isActing} onClick={onDismiss}>
              Dismiss
            </Button>
          </div>
        )}
      </div>
      {samplesOpen && samples.length > 0 && <ShadowSamples id={samplesId} samples={samples} />}
      {needsEventTypeSelect && (
        <div className="flex flex-wrap items-center gap-2">
          <label
            htmlFor={`event-type-select-${item.id}`}
            className="text-caption text-fg-tertiary"
          >
            Choose event type:
          </label>
          <Select value={selectedEventTypeId} onValueChange={onSelectEventType}>
            <SelectTrigger
              id={`event-type-select-${item.id}`}
              // The height of the size="sm" buttons beside it.
              className="h-(--control-h) w-auto min-w-40"
            >
              <SelectValue placeholder="Select…" />
            </SelectTrigger>
            <SelectContent>
              {eventTypes.map((et) => (
                <SelectItem key={et.id} value={et.id}>
                  {et.display_name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button size="sm" variant="default" disabled={confirmDisabled} onClick={onConfirm}>
            Confirm
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
        </div>
      )}
      {error && <span role="alert" className="text-caption text-destructive">{error}</span>}
    </div>
  )
}

function DeadRow({
  item,
  slug,
  selected,
  onToggle,
}: {
  item: DeadEvent
  slug: string | undefined
  selected: boolean
  /** Omitted for a viewer: selecting is only ever for Archive. */
  onToggle?: (id: string) => void
}) {
  const isNever = !item.last_seen_at
  return (
    <div
      className="flex min-h-(--row-h) items-center gap-2.5 border-t px-4 py-2 border-border-subtle"
    >
      {onToggle && (
        <Checkbox
          checked={selected}
          onCheckedChange={() => onToggle(item.event_id)}
          aria-label={`Select ${eventNameLabel(item.name)}`}
        />
      )}
      <Dot tone="neutral" size={6} />
      <Link
        to={slug ? getMonitoringPath(slug, { scope_type: 'event', scope_ref: item.event_id }) : '#'}
        className="min-w-0 flex-1 truncate text-body-sm hover:underline text-fg-secondary"
        title={eventNameLabel(item.name)}
      >
        <EventName name={item.name} />
      </Link>
      {item.event_type_name && <Chip variant="outline" size="xs">{item.event_type_name}</Chip>}
      <span
        className="tnum shrink-0 text-micro"
        style={{ color: isNever ? 'var(--warning)' : 'var(--fg-faint)' }}
      >
        {formatRelativeTime(item.last_seen_at)}
      </span>
    </div>
  )
}
