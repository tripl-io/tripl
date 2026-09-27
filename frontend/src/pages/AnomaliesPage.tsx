import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { SignalsHeldNotice } from '@/components/source-freshness/signals-held-notice'
import { useSourceFreshness } from '@/hooks/useSourceFreshness'
import { holdingItems } from '@/lib/sourceFreshness'
import { Activity, ArrowDown, ArrowUp, Play, Settings2 } from 'lucide-react'
import { scansApi } from '@/api/scans'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { Panel } from '@/components/settings/kit'
import { PageContainer } from '@/components/primitives/page-container'
import { PageHeader } from '@/components/primitives/page-header'
import { TermHint, TERM_HINTS } from '@/components/term-hint'
import { Button } from '@/components/ui/button'
import { FilterBar, FilterSelect } from '@/components/ui/filter-bar'
import { Dot } from '@/components/primitives/dot'
import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { SectionSkeleton, StatValueSkeleton } from '@/components/states'
import { formatRelativeTime, formatTimestamp } from '@/lib/datetime'
import { APP_LOCALE, formatNumber } from '@/lib/format'
import { formatSignalEffect, formatSignalEffectDetail, getMonitoringPath } from '@/lib/monitoring'
import { getAlertingPath } from '@/lib/navigation'
import { alertInboxStatusLabel } from '@/lib/alertStatus'
import {
  DEFAULT_MAGNITUDE_LEVEL,
  MAGNITUDE_PRESETS,
  type MagnitudeLevel,
  compareSignalsByMagnitude,
  magnitudePresetLabel,
  relativeEffect,
  signalMagnitudeWord,
} from '@/lib/signalMagnitude'
import { signalDirectionColor, signalDirectionTone } from '@/lib/statusLexicon'
import { formatSignalValues } from '@/lib/signalMetricFormat'
import { useExpandedSignals } from '@/hooks/useExpandedSignals'
import {
  signalScopeLabel,
  signalScopeRefLabel,
  unnamedScopeLabel,
} from '@/lib/signalScope'
import {
  signalRowKey,
  signalSeriesLookupKey,
  signalSparkline,
  useSignalSeries,
} from '@/lib/monitoringSignalSeries'
import { Sparkline, SparklineSkeleton } from '@/components/primitives/sparkline'
import { VERDICT_OPTIONS, needsVerdict } from '@/lib/signalVerdict'
import type { MonitoringSignal, SignalSeries, SignalVerdictKind } from '@/types'
import { scansKey } from '@/lib/queryKeys'
import { SignalActions } from './anomalies/SignalActions'
import { countHiddenSignals, signalIncidentId, triageStatusLabel } from './anomalies/signalTriage'

// Change sits right after the scope and its trend, the one figure a reader
// scans for (MO-19). The trend column is sm+ only: its cell is `hidden` below,
// which takes it out of the phone card's grid altogether.
// Below `sm` the same four cells fold into a two-line card — scope and change on
// the first line, values and time on the second — instead of a 640px table in a
// sideways scroller that hid "how bad" and "how recent" off-screen (MO-20).
// The last column is the row's action menu (MO-4). On a phone it spans both
// lines of the two-line card, so the other cells keep their two columns.
const ANOMALY_GRID =
  'grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-x-3 gap-y-0.5 px-4 sm:grid-cols-[minmax(0,1.7fr)_80px_88px_minmax(0,1fr)_120px_28px]'

// ───────── Magnitude filter ─────────
//
// The presets, the threshold and relativeEffect() live in @/lib/signalMagnitude
// so this page, the Overview headline, the top-bar bell and the backend badge
// all rank and gate signals identically — they drifted apart twice when each
// surface kept its own copy (tripl-yfsj.1, tripl-jfm3.89).

// The four scopes that have a monitoring detail route (metric scope_ref is the
// metric definition id, routed via getMetricMonitoringPath); getMonitoringPath
// throws for the rest, so non-linkable signals render as a plain (unlinked) row.
function isLinkableScope(signal: MonitoringSignal): boolean {
  return (
    signal.scope_type === 'project_total'
    || signal.scope_type === 'event_type'
    || signal.scope_type === 'event'
    || signal.scope_type === 'metric'
  )
}

/**
 * Stand-in for a scope the server could not name (its event/metric was deleted
 * out from under the anomaly row).
 *
 * Deliberately not the `scope_ref`: a hex prefix reads as a name, and the same
 * incident the activity rail calls `spot_auto_change_model` then appears here as
 * "Event d4c684dd" — two names for one incident, depending on the page
 * (tripl-y4wt). The ref stays in the accessible name and the tooltip so the row
 * is still traceable.
 *
 * Words and not a shimmer bar. `animate-pulse` is this app's Skeleton
 * (`components/ui/skeleton.tsx`), and OverviewPage uses the identical `h-3 w-32`
 * one to mean "fetching" — while the table here is already gated on the
 * signals query's first load and a rendered row's name is server-resolved. So the
 * pulse could only ever mean "will never resolve" and read as "still arriving":
 * the operator waits and refreshes on a terminal state. `role="img"` stays so
 * the ref remains the accessible name rather than being replaced by the
 * stand-in wording.
 */
function UnnamedScope({ signal }: { signal: MonitoringSignal }) {
  const ref = signalScopeRefLabel(signal)
  return (
    <span
      role="img"
      aria-label={ref}
      title={ref}
      className="italic text-fg-tertiary"
    >
      {unnamedScopeLabel(signal)}
    </span>
  )
}

/** Scan id → scan name, for the facet below. Unrelated to scope naming. */
type ScanNames = ReadonlyMap<string, string>

const ALL_SCANS = 'all'
// Catalog-metric signals belong to no scan: MetricDefinition series are
// project-global, so their scan_config_id is NULL. They still need a home in a
// facet keyed by scan, or they become unreachable the moment a scan is picked —
// and, less kindly, a raw `null` used as a Map key made the label expression
// call .slice on it and white-screen the whole page.
const CATALOG_METRICS = 'catalog-metrics'
const facetKey = (scanConfigId: string | null): string => scanConfigId ?? CATALOG_METRICS
const facetLabel = (id: string, scanNames: ScanNames): string =>
  id === CATALOG_METRICS ? 'Catalog metrics' : (scanNames.get(id) ?? `Scan ${id.slice(0, 8)}`)

// ───────── Verdict filter (#254) ─────────
//
// The page opens on the signals nobody has answered yet: "Needs verdict" is
// the default, absent from the URL like every other default; `?verdict=all`
// clears it and `?verdict=<kind>` lists one verdict. Filtered here, on the one
// shared expanded list, rather than with the server's `?needs_verdict=true`:
// the bell and Overview read the same cache entry, and a second key would fork
// it (tripl-jfm3.119).
type VerdictFilter = 'needs' | 'all' | SignalVerdictKind
const DEFAULT_VERDICT_FILTER: VerdictFilter = 'needs'
const VERDICT_FILTERS: readonly VerdictFilter[] = [
  'needs',
  'all',
  ...VERDICT_OPTIONS.map((option) => option.verdict),
]
const toVerdictFilter = (value: string | null): VerdictFilter =>
  VERDICT_FILTERS.find((filter) => filter === value) ?? DEFAULT_VERDICT_FILTER

function matchesVerdictFilter(signal: MonitoringSignal, filter: VerdictFilter): boolean {
  if (filter === 'all') return true
  if (filter === 'needs') return needsVerdict(signal)
  return signal.verdict?.verdict === filter
}

/** `?level=` → a preset, degrading an absent or unknown value to the default. */
const toMagnitudeLevel = (value: string | null): MagnitudeLevel =>
  MAGNITUDE_PRESETS.find((preset) => preset.id === value)?.id ?? DEFAULT_MAGNITUDE_LEVEL

export default function AnomaliesPage() {
  const { slug } = useParams<{ slug: string }>()
  // Both facets live in the URL, not in component state, so a scan can hand its
  // own anomalies over — the "Signals added" counter on a scan run links to
  // `?scan=<id>` (tripl-3y7z.2) — and so opening a signal to investigate it and
  // pressing Back does not snap the magnitude filter back to Significant,
  // re-hiding 162 of 209 rows on acme-ios (tripl-ahg5). The rows themselves are
  // links off this route, so that Back is the page's primary path, not an
  // incidental one. Same idiom as MetricsCatalog's `?kind=`; `replace` — a
  // filter flip is not a place the Back button should stop.
  const [searchParams, setSearchParams] = useSearchParams()
  const scanId = searchParams.get('scan') ?? ALL_SCANS
  const setScanId = (next: string) => {
    setSearchParams(
      (previous) => {
        const params = new URLSearchParams(previous)
        if (next === ALL_SCANS) params.delete('scan')
        else params.set('scan', next)
        return params
      },
      { replace: true },
    )
  }
  // Unlike `?scan=`, an unknown `?level=` degrades to the default rather than
  // being preserved: a magnitude that does not exist names no subset a run could
  // have produced, so there is nothing to keep faith with.
  const level = toMagnitudeLevel(searchParams.get('level'))
  const setLevel = (next: MagnitudeLevel) => {
    setSearchParams(
      (previous) => {
        const params = new URLSearchParams(previous)
        if (next === DEFAULT_MAGNITUDE_LEVEL) params.delete('level')
        else params.set('level', next)
        return params
      },
      { replace: true },
    )
  }

  // expanded: surface every flagged scope — project_total, each event_type and
  // each event — instead of collapsing an incident's fan-out into one total row.
  // Shared key with the top bar and Overview (tripl-jfm3.119).
  // Each signal carries its own `scope_name`, so no catalog fetch is needed to
  // label the rows (tripl-y4wt).
  const signalsQuery = useExpandedSignals(slug)

  // Scan names for the facet below. Shares the app-wide ['scans', slug] key, so
  // no extra request when the user has already opened a scan settings page.
  const scansQuery = useQuery({
    queryKey: scansKey(slug),
    queryFn: () => scansApi.list(slug!),
    enabled: !!slug,
    staleTime: 60_000,
  })
  const scanNames: ScanNames = new Map(
    (scansQuery.data ?? []).map((s) => [s.id, s.name]),
  )
  // Scans whose source is late or overdue hold their drop signals (F16, #269);
  // the notice below says so, or this list reads quieter than the data is.
  const freshnessItems = useSourceFreshness(slug)

  // Muted and expected signals (MO-4 / JR-5) are left out of the list and every
  // count here, as they are from the bell, Overview and the sidebar badge;
  // `?hidden=1` brings them back, flagged, so a verdict can be reviewed or undone.
  const showHidden = searchParams.get('hidden') === '1'
  const setShowHidden = (next: boolean) => {
    setSearchParams(
      (previous) => {
        const params = new URLSearchParams(previous)
        if (next) params.set('hidden', '1')
        else params.delete('hidden')
        return params
      },
      { replace: true },
    )
  }
  const verdictFilter = toVerdictFilter(searchParams.get('verdict'))
  const setVerdictFilter = (next: VerdictFilter) => {
    setSearchParams(
      (previous) => {
        const params = new URLSearchParams(previous)
        if (next === DEFAULT_VERDICT_FILTER) params.delete('verdict')
        else params.set('verdict', next)
        return params
      },
      { replace: true },
    )
  }
  // Asking for one verdict asks for its signals, hidden or not: an "expected"
  // one is always hidden, and the filter would otherwise list nothing.
  const pickedVerdict = verdictFilter !== 'needs' && verdictFilter !== 'all'
  const allSignals = signalsQuery.data ?? []
  const hiddenTotal = countHiddenSignals(allSignals)
  const signals = showHidden || pickedVerdict ? allSignals : allSignals.filter((s) => !s.hidden)
  const total = signals.length
  // Verdict first: it decides which signals the page is about at all.
  const byVerdict = signals.filter((s) => matchesVerdictFilter(s, verdictFilter))
  // Row sparklines, one batched request beside the signals list rather than
  // inside its shared 30 s cache (MO-19).
  const seriesQuery = useSignalSeries(slug, signalsQuery.data)
  const activePreset = MAGNITUDE_PRESETS.find((p) => p.id === level) ?? MAGNITUDE_PRESETS[0]
  const threshold = activePreset.minRelEffect
  const byMagnitude = byVerdict.filter((s) => relativeEffect(s) >= threshold)

  // Scan facet. Every signal already carries its scan_config_id, and
  // useExpandedSignals is one unfiltered GET shared with the bell and Overview,
  // so this narrows the array already in memory — no query parameter, no second
  // request, and no cache key that would fork from the other two readers.
  //
  // It exists because one scan drowns the others out by size, not by noise. On
  // acme-ios the legacy "Old events (iOS)" scan watches 2060 of the project's
  // 2497 events and supplied 136 of 207 open event-scope signals (65.7%) when
  // this was measured; earlier audit samples put it as high as 95%, so treat the
  // share as "most of the page, varying" rather than a fixed number. Its
  // per-scope firing rate is 6.6% against the live scan's 17.4% — it fires LESS
  // often per scope, so this is a denominator effect and there is nothing to fix
  // in detection. The live stream simply needs to be reachable.
  //
  // Which scans get an option comes from the unfiltered list, while the count on
  // each comes from the magnitude-filtered one. Deriving both from the filtered
  // list made raising the level delete the option the user was standing on,
  // silently resetting them to "all" and refilling the page with the very rows
  // they had just excluded.
  const countsAtLevel = new Map<string, number>()
  for (const signal of byMagnitude) {
    const key = facetKey(signal.scan_config_id)
    countsAtLevel.set(key, (countsAtLevel.get(key) ?? 0) + 1)
  }
  const scanTotals = new Map<string, number>()
  for (const signal of byVerdict) {
    const key = facetKey(signal.scan_config_id)
    scanTotals.set(key, (scanTotals.get(key) ?? 0) + 1)
  }
  // `?scan=` naming a real scan with nothing open is the ordinary case, not a
  // dead link: a run from last week reports "Raised 2 anomaly signals", links
  // here, and by now both have closed. Dropping the filter then answers a
  // question nobody asked — a full list of some OTHER scan's anomalies, with no
  // control showing that a filter was discarded (tripl-3y7z.2).
  //
  // So the selection survives for any scan this project has, and only an id the
  // project does not have falls back to "all". While the scan list is still in
  // flight nothing is known to be missing, so the selection is kept then too
  // rather than flipped to "all" and back.
  const scanIsInProject =
    scanId === CATALOG_METRICS || scanNames.has(scanId) || !scansQuery.data
  const activeScanId = scanTotals.has(scanId) || scanIsInProject ? scanId : ALL_SCANS
  // Narrowed to the scan facet when one is picked; catalog metrics have no
  // scan source, so they hold nothing.
  const heldScans =
    activeScanId === CATALOG_METRICS
      ? []
      : holdingItems(freshnessItems, activeScanId === ALL_SCANS ? null : [activeScanId])
  const scanOptions = [...scanTotals.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([id]) => ({
      id,
      label: `${facetLabel(id, scanNames)} ${countsAtLevel.get(id) ?? 0}`,
    }))
  // A kept selection with nothing open still needs its own option, or the
  // control would render with no segment active and the page would look like it
  // had lost track of what the user asked for.
  if (activeScanId !== ALL_SCANS && !scanTotals.has(activeScanId)) {
    scanOptions.push({ id: activeScanId, label: `${facetLabel(activeScanId, scanNames)} 0` })
  }

  // Magnitude first, then scan, then rank biggest first — by relative effect,
  // as Overview and the bell do, with |z| only breaking ties. Ranking by |z|
  // alone let quiet-scope noise lead this list while Overview led with a
  // different anomaly (MON-14).
  const filtered =
    activeScanId === ALL_SCANS
      ? byMagnitude
      : byMagnitude.filter((s) => facetKey(s.scan_config_id) === activeScanId)
  const sorted = [...filtered].sort(compareSignalsByMagnitude)
  // What "Show hidden" would bring back into THIS view: hidden signals that pass
  // the magnitude level and scan facet. Counting the whole list offered rows the
  // filters would then keep out. `hiddenTotal` (unfiltered) still decides
  // whether anything is hidden at all.
  const hiddenInView = pickedVerdict
    ? 0
    : countHiddenSignals(
        allSignals.filter(
          (s) =>
            matchesVerdictFilter(s, verdictFilter)
            && relativeEffect(s) >= threshold
            && (activeScanId === ALL_SCANS || facetKey(s.scan_config_id) === activeScanId),
        ),
      )
  // No number when every hidden signal is outside the filters: the empty state
  // still offers the way back, and a "(0)" would read as "nothing is hidden".
  const showHiddenLabel =
    hiddenInView > 0 ? `Show hidden (${formatNumber(hiddenInView)})` : 'Show hidden'
  const visibleCount = filtered.length
  const hiddenCount = total - visibleCount
  // Split so the subtitle can name the filter responsible for each omission.
  const otherVerdictCount = total - byVerdict.length
  const belowLevelCount = byVerdict.length - byMagnitude.length
  const otherScansCount = byMagnitude.length - visibleCount
  // Rollup counts reflect what's actually shown (the filtered set).
  const spikes = filtered.filter((s) => s.direction === 'spike').length
  const drops = filtered.filter((s) => s.direction === 'drop').length
  // Loaded with nothing open at all — distinct from loading, from the error
  // state, and from "hidden by the filter" (which keeps the panel + control).
  const isEmpty = !signalsQuery.isError && !!signalsQuery.data && total === 0
  // Signals exist, but the current filters hide every one.
  const allFiltered = !isEmpty && total > 0 && visibleCount === 0
  // Which filter emptied the list decides which one the hint offers to drop.
  // The verdict filter first: with every open signal answered, lowering the
  // magnitude or widening the scan cannot bring a row back.
  const emptiedByVerdict = allFiltered && byVerdict.length === 0
  const emptiedByScan = allFiltered && !emptiedByVerdict && byMagnitude.length > 0
  // ...and "this scan has nothing open at ALL" is not "nothing at this level":
  // lowering the magnitude filter cannot help, and the user arrived from a run
  // that counted signals which have since closed. Say that instead.
  const scanHasNothingOpen =
    allFiltered
    && !emptiedByVerdict
    && activeScanId !== ALL_SCANS
    && (scanTotals.get(activeScanId) ?? 0) === 0
  const verdictFilterLabel =
    verdictFilter === 'needs'
      ? 'Needs verdict'
      : VERDICT_OPTIONS.find((option) => option.verdict === verdictFilter)?.label ?? 'All'
  const activeScanLabel =
    activeScanId === CATALOG_METRICS ? 'Catalog metrics' : (scanNames.get(activeScanId) ?? 'this scan')
  // Nothing open AND nothing that could ever open anything: no scan collects
  // volume on a schedule (a Catalog only scan has no interval). The reassuring
  // "No anomalies right now" was a false all-clear for a project that has not
  // started monitoring (MO-23). Decided only once the scan list has loaded.
  const monitoringIsOff =
    isEmpty && hiddenTotal === 0 && scansQuery.isSuccess && !(scansQuery.data ?? []).some((scan) => scan.interval)
  // First load: a skeleton of the stat strip and the table, never zeros.
  const isFirstLoad = signalsQuery.isPending && !signalsQuery.isError

  return (
    <PageContainer
      className={isEmpty ? 'flex min-h-[calc(100vh-7rem)] flex-col gap-6 space-y-0 pb-0' : undefined}
    >
      <PageHeader
        eyebrow="Observe"
        title="Anomalies"
        titleAddon={slug && <TermHint slug={slug} {...TERM_HINTS.scopes} />}
        // Two queues that looked alike (JR-6): say which one owes work.
        description={
          slug ? (
            <>
              Signals are what detection found. Incidents, in{' '}
              <Link to={getAlertingPath(slug)} className="text-accent">
                Alerting
              </Link>
              , are the ones an alert rule routed to your team. Give each signal a verdict —
              expected, tracking bug, false positive or real issue — from its row’s menu; on a
              routed signal it updates the incident. Acknowledge or mute the rest there too.
            </>
          ) : undefined
        }
        actions={
          slug ? (
            <Button asChild variant="outline" size="sm">
              <Link to={`/p/${slug}/settings/monitoring`} className="no-underline">
                <Settings2 aria-hidden="true" />
                Detection settings
              </Link>
            </Button>
          ) : undefined
        }
      />

      {slug && <SignalsHeldNotice slug={slug} items={heldScans} />}

      {/* Rollup */}
      {isFirstLoad ? (
        <SectionSkeleton variant="table" rows={5} label="Loading anomalies…" />
      ) : signalsQuery.isError ? (
        <ErrorState
          title="Anomalies unavailable"
          error={signalsQuery.error}
          onRetry={() => {
            void signalsQuery.refetch()
          }}
          retryLabel="Retry"
          compact
        />
      ) : monitoringIsOff ? null : (
        // Hidden when monitoring is off: three zeros say nothing there (MO-23).
        <MiniStatStrip boxed className={isEmpty ? 'opacity-60' : undefined}>
          {/* Neutral at zero, not green: an empty list is not praise (MO-17).
              A pending value is a skeleton, never a "0" (DS-25). */}
          <MiniStat
            label="Open signals"
            value={signalsQuery.data ? formatNumber(visibleCount) : <StatValueSkeleton />}
            tone={visibleCount > 0 ? 'danger' : 'neutral'}
            pulse={visibleCount > 0}
            delta={
              signalsQuery.data && hiddenCount > 0 ? `of ${formatNumber(total)}` : undefined
            }
          />
          {/* `valueTone`, not `tone`: these carry no delta, and `tone` paints
              only the delta — so the emphasis never rendered (MON-42). */}
          <MiniStat
            label="Spikes"
            value={signalsQuery.data ? formatNumber(spikes) : <StatValueSkeleton />}
            valueTone={spikes > 0 ? signalDirectionTone('spike') : 'neutral'}
          />
          <MiniStat
            label="Drops"
            value={signalsQuery.data ? formatNumber(drops) : <StatValueSkeleton />}
            valueTone={drops > 0 ? signalDirectionTone('drop') : 'neutral'}
          />
        </MiniStatStrip>
      )}

      {/* Signals table — or a centered empty state when nothing is firing */}
      {!signalsQuery.isError && !isFirstLoad &&
        (isEmpty ? (
          <div className="flex flex-1 items-center justify-center">
            {monitoringIsOff ? (
              <EmptyState
                icon={Activity}
                title="Monitoring isn’t running yet"
                description="Anomalies appear once a scan collects volume. Connect a source and run a scan with Catalog + monitoring."
                action={
                  slug ? (
                    <div className="flex flex-wrap justify-center gap-2">
                      <Button asChild size="sm">
                        <Link to={`/p/${slug}/scans`} className="no-underline">
                          <Play aria-hidden="true" />
                          Run a scan
                        </Link>
                      </Button>
                      <Button asChild variant="outline" size="sm">
                        <Link to={`/p/${slug}/settings/monitoring`} className="no-underline">
                          Detection settings
                        </Link>
                      </Button>
                    </div>
                  ) : undefined
                }
              />
            ) : (
              <EmptyState
                icon={Activity}
                title="No anomalies right now"
                description="When detection flags a spike or drop against the learned baseline, it shows up here. Tune sensitivity in detection settings."
                action={
                  // Everything open was muted or marked expected: say so, and
                  // offer the way back to those verdicts.
                  hiddenTotal > 0 ? (
                    <Button type="button" variant="outline" size="sm" onClick={() => setShowHidden(true)}>
                      {showHiddenLabel}
                    </Button>
                  ) : undefined
                }
              />
            )}
          </div>
        ) : (
          <Panel
            title="Active signals"
            subtitle={
              signalsQuery.data
                ? hiddenCount > 0
                  ? `${visibleCount} of ${total} open · ${[
                      otherVerdictCount > 0
                        ? verdictFilter === 'needs'
                          ? `${otherVerdictCount} with a verdict`
                          : `${otherVerdictCount} with another verdict`
                        : null,
                      belowLevelCount > 0
                        ? `${belowLevelCount} below ${activePreset.label.toLowerCase()}`
                        : null,
                      otherScansCount > 0 ? `${otherScansCount} in other scans` : null,
                    ]
                      .filter(Boolean)
                      .join(' · ')}`
                  : `${visibleCount} open`
                : undefined
            }
            right={
              // Filter chips, the one filter idiom (DS-15): each shows its
              // current value ("Magnitude: Significant") and applies instantly.
              // A segmented control is for switching views, not for filtering.
              <FilterBar className="min-w-0 max-w-full">
                {/* Muted / expected signals, back in the list on demand. */}
                {(hiddenInView > 0 || showHidden) && (
                  <Button
                    type="button"
                    variant={showHidden ? 'secondary' : 'ghost'}
                    size="sm"
                    aria-pressed={showHidden}
                    onClick={() => setShowHidden(!showHidden)}
                  >
                    {showHiddenLabel}
                  </Button>
                )}
                {/* What still needs somebody's call, by default (#254). */}
                <FilterSelect
                  label="Verdict"
                  value={verdictFilter}
                  onValueChange={(next) => setVerdictFilter(toVerdictFilter(next))}
                  anyValue="all"
                  anyLabel="All"
                  options={[
                    { value: 'needs', label: 'Needs verdict' },
                    ...VERDICT_OPTIONS.map((option) => ({ value: option.verdict, label: option.label })),
                  ]}
                />
                {/* Only worth the header room once there is something to choose
                    between: a single-scan project gains nothing from it. */}
                {scanOptions.length > 1 && (
                  <FilterSelect
                    label="Scan"
                    value={activeScanId}
                    onValueChange={setScanId}
                    anyValue={ALL_SCANS}
                    anyLabel={`All scans ${byMagnitude.length}`}
                    options={scanOptions.map((option) => ({ value: option.id, label: option.label }))}
                    className="max-w-[18rem]"
                  />
                )}
                {/* Each level names its bar in the % the rows show, so why a
                    row is or is not "Major" reads off the control (MO-3). */}
                <FilterSelect
                  label="Magnitude"
                  value={level}
                  onValueChange={(next) => setLevel(toMagnitudeLevel(next))}
                  anyValue="all"
                  anyLabel="All"
                  options={MAGNITUDE_PRESETS.map((preset) => ({
                    value: preset.id,
                    label: magnitudePresetLabel(preset),
                  }))}
                />
              </FilterBar>
            }
          >
            {allFiltered ? (
              <div className="px-4 py-10">
                <EmptyState
                  icon={Activity}
                  title={
                    emptiedByVerdict
                      ? verdictFilter === 'needs'
                        ? 'Every open signal has a verdict'
                        : `No open signals marked ${verdictFilterLabel.toLowerCase()}`
                      : scanHasNothingOpen
                      ? `No open anomalies from ${activeScanLabel}`
                      : emptiedByScan
                        ? `Nothing in ${activeScanLabel} at this level`
                        : `Nothing at the ${activePreset.label.toLowerCase()} level`
                  }
                  description={
                    emptiedByVerdict
                      ? 'Clear the verdict filter to review the answered ones.'
                      : scanHasNothingOpen
                      ? 'A signal closes once the metric comes back to normal, so the ones an earlier run raised may already be gone.'
                      : emptiedByScan
                        ? 'Other scans still have open signals at this magnitude.'
                        : 'Every open signal is smaller than this threshold. Lower the filter to see the smaller anomalies.'
                  }
                  action={
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => {
                        if (emptiedByVerdict) {
                          setVerdictFilter('all')
                          return
                        }
                        if (!scanHasNothingOpen && !emptiedByScan) {
                          setLevel('all')
                          return
                        }
                        setScanId(ALL_SCANS)
                        // Nothing open here at any level and nothing above the
                        // threshold elsewhere either — clearing one filter would
                        // hand back a second empty page.
                        if (byMagnitude.length === 0) setLevel('all')
                      }}
                    >
                      {emptiedByVerdict
                        ? `Show all verdicts (${formatNumber(total)})`
                        : scanHasNothingOpen || emptiedByScan
                        ? `Show all scans (${formatNumber(byMagnitude.length || total)})`
                        : `Show all ${formatNumber(total)}`}
                    </Button>
                  }
                />
              </div>
            ) : (
              <div>
                <div role="table" aria-label="Anomaly signals">
                  <div role="rowgroup">
                    {/* No header row on phones: each row is a two-line card
                        there, and every cell reads on its own (MO-20). */}
                    <div
                      role="row"
                      className={`${ANOMALY_GRID} hidden border-b py-2 micro-label sm:grid border-border-subtle text-fg-tertiary`}
                    >
                      <span role="columnheader">Anomaly</span>
                      <span role="columnheader">Trend</span>
                      <span role="columnheader" className="text-right">Change</span>
                      <span role="columnheader" className="text-right">Actual / expected</span>
                      {/* The cell leads with the bucket's absolute START (its
                          tooltip says so) and adds when the detector found it:
                          one absolute and one relative time, not two relative
                          ones that read as a contradiction (MON-40, MO-21). */}
                      <span role="columnheader" className="text-right">When</span>
                      <span role="columnheader">
                        <span className="sr-only">Actions</span>
                      </span>
                    </div>
                  </div>
                  <div role="rowgroup">
                    {sorted.map((signal) => (
                      <AnomalyRow
                        // Scan id too: a legacy and a live scan watching the
                        // same event open one signal each on the same bucket,
                        // and the three-part key collided (MON-16).
                        key={signalRowKey(signal)}
                        slug={slug}
                        signal={signal}
                        series={seriesQuery.byKey.get(signalSeriesLookupKey(signal))}
                        seriesPending={seriesQuery.isPending}
                      />
                    ))}
                  </div>
                </div>
              </div>
            )}
          </Panel>
        ))}
    </PageContainer>
  )
}

/**
 * A bucket's start as a short absolute time — "Today 18:00", "Sep 25, 18:00" —
 * which, unlike "1h ago", cannot read as contradicting "found 16m ago" under
 * it (MO-21).
 */
function formatShortWhen(iso: string, now: Date = new Date()): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const time = date.toLocaleTimeString(APP_LOCALE, {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  })
  if (date.toDateString() === now.toDateString()) return `Today ${time}`
  const dayOptions: Intl.DateTimeFormatOptions = { month: 'short', day: 'numeric' }
  if (date.getFullYear() !== now.getFullYear()) dayOptions.year = 'numeric'
  return `${date.toLocaleDateString(APP_LOCALE, dayOptions)}, ${time}`
}

/** The viewer's own zone, named, since the bucket is shown in it. */
function localTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone
  } catch {
    return 'local time'
  }
}

/**
 * One anomaly. A linkable row is a real link (MON-13): the label is an `<a>`
 * whose `::after` is stretched over the row, so the whole row stays the click
 * target while Cmd/Ctrl-click, middle-click and "open in new tab" work, and a
 * screen reader announces a link rather than a table row it cannot act on.
 */
function AnomalyRow({
  slug,
  signal,
  series,
  seriesPending,
}: {
  slug?: string
  signal: MonitoringSignal
  series: SignalSeries | undefined
  seriesPending: boolean
}) {
  const sparkline = signalSparkline(series)
  const label = signalScopeLabel(signal)
  const isDrop = signal.direction === 'drop'
  const DirIcon = isDrop ? ArrowDown : ArrowUp
  const severityColor = signalDirectionColor(signal.direction)
  const effectDetail = formatSignalEffectDetail(signal)
  const href = slug && isLinkableScope(signal) ? getMonitoringPath(slug, signal) : undefined
  const triageStatus = triageStatusLabel(signal)
  const incidentId = signalIncidentId(signal)
  const incidentStatus = signal.incident?.status ?? signal.incident_status ?? null
  // The "Spike on" / "Drop on" prefix is visual on sm+ only: on a phone the
  // arrow already carries the direction and the words cost the scope name
  // most of its width (MO-20). `sr-only` rather than `hidden` keeps it in the
  // link's accessible name at every width, since the arrow is aria-hidden.
  // The separating space sits outside the span: inside it, the name ran the
  // words together ("Spike onMetric · …"). Out of flow on phones, the prefix
  // leaves that space at the line start, where it collapses away.
  const text = (
    <>
      <span className="sr-only sm:not-sr-only">{isDrop ? 'Drop' : 'Spike'} on</span>{' '}
      {label ?? <UnnamedScope signal={signal} />}
    </>
  )
  const textClass = 'truncate text-body-sm font-medium'

  return (
    <div
      role="row"
      // The row height follows the Density setting (DS-9), as the Events
      // table's does.
      className={`${ANOMALY_GRID} relative min-h-(--row-h) border-b py-2 last:border-0 ${
        href ? 'transition-colors hover:bg-[var(--surface-hover)]' : ''
      } ${signal.hidden ? 'opacity-70' : ''} border-border-subtle`}
    >
      <span role="cell" className="flex min-w-0 items-center gap-2">
        {/* Static in a list: with every row pulsing, a flooded project
            shimmered and motion stopped meaning "new" (MO-18). */}
        <Dot tone={signalDirectionTone(signal.direction)} size={7} />
        <DirIcon aria-hidden="true" className="h-3.5 w-3.5 shrink-0" style={{ color: severityColor }} />
        {href ? (
          <Link
            to={href}
            data-anomaly-label=""
            className={`${textClass} no-underline outline-none after:absolute after:inset-0 after:rounded-sm focus-visible:after:ring-2 focus-visible:after:ring-inset focus-visible:after:ring-[var(--accent)] text-fg`}
          >
            {text}
          </Link>
        ) : (
          <span data-anomaly-label="" className={`${textClass} text-fg`}>
            {text}
          </span>
        )}
        {signal.incident_child && (
          <span
            // Dropped on phones, where it left the scope name a few letters.
            // `relative` lifts it over the row link so its tooltip still shows.
            className="relative hidden shrink-0 whitespace-nowrap text-micro sm:inline text-fg-tertiary"
            title={`This scope fired as part of a project-total ${isDrop ? 'drop' : 'spike'} on the same bucket`}
          >
            {/* Says what it means; "part of total" read as a data annotation
                (MO-22). A child is keyed to its parent by direction too, so a
                drop child sits under a total drop, never a spike. */}
            · within total {isDrop ? 'drop' : 'spike'}
          </span>
        )}
        {/* The verdict a user gave this signal (MO-4 / JR-5). A hidden one is
            listed only under "Show hidden". */}
        {triageStatus && (
          <span
            className="relative shrink-0 whitespace-nowrap text-micro text-fg-tertiary"
            title={(signal.verdict ? signal.verdict.note : signal.expected_note) ?? undefined}
          >
            · {triageStatus}
          </span>
        )}
        {/* The incident a rule routed this signal into, so the queue that
            owes work is one click away (JR-6). `relative` lifts it over the
            row link. */}
        {slug && incidentId && (
          <Link
            to={getAlertingPath(slug, { incidentId })}
            className="relative shrink-0 whitespace-nowrap text-micro no-underline hover:underline text-accent"
          >
            Incident
            {incidentStatus ? ` · ${alertInboxStatusLabel(incidentStatus).toLowerCase()}` : ''}
          </Link>
        )}
      </span>
      {/* The shape of the move, flagged bucket marked: a one-off spike, a
          sustained rise and a slow drift read differently at a glance (MO-19).
          A catalog-metric row has no scan series, so it stays blank. */}
      <span role="cell" className="hidden sm:block">
        {sparkline ? (
          <Sparkline
            data={sparkline.data}
            anomalyIdx={sparkline.anomalyIdx}
            width={80}
            height={20}
            variant="line-only"
          />
        ) : seriesPending && signal.scope_type !== 'metric' ? (
          <SparklineSkeleton width={80} height={20} />
        ) : null}
      </span>
      {/* The change first, in the direction colour: "+203%" where the row used
          to print z=40.7 (MO-2). The magnitude word rides underneath and the
          z-score in the tooltip, for whoever wants them (JR-31). Figures in
          sans with tabular digits: numbers, not code (DS-17). `relative` lifts
          the figure over the row link so its tooltip shows. */}
      <span role="cell" className="flex flex-col items-end text-right">
        <span
          className="tnum relative text-body-sm font-semibold"
          style={{ color: severityColor }}
          title={effectDetail}
        >
          {formatSignalEffect(signal)}
        </span>
        <span className="hidden text-micro sm:block text-fg-tertiary">
          {signalMagnitudeWord(signal)}
        </span>
      </span>
      <span
        role="cell"
        className="tnum truncate text-caption sm:text-right text-fg-tertiary"
      >
        {formatSignalValues(signal)}
      </span>
      <span role="cell" className="tnum text-right text-caption text-fg-tertiary">
        {/* `relative` lifts it over the row link, so the tooltip naming this as
            the bucket's start is reachable (MON-40). */}
        <time
          dateTime={signal.bucket}
          title={`Bucket starting ${formatTimestamp(signal.bucket)} (${localTimeZone()})`}
          className="relative"
        >
          {formatShortWhen(signal.bucket)}
        </time>
        {/* When the detector caught it, which can be long after the bucket
            began — an hourly bucket is flagged at the scan after it (MON-40). */}
        {signal.detected_at && (
          <time
            dateTime={signal.detected_at}
            title={`Detected ${formatTimestamp(signal.detected_at)} (${localTimeZone()})`}
            className="relative block text-micro text-fg-tertiary"
          >
            found {formatRelativeTime(signal.detected_at)}
          </time>
        )}
      </span>
      <span
        role="cell"
        className="relative col-start-3 row-span-2 row-start-1 flex justify-end sm:col-start-auto sm:row-span-1 sm:row-start-auto"
      >
        {slug && <SignalActions slug={slug} signal={signal} href={href} />}
      </span>
    </div>
  )
}
