import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
} from '@tanstack/react-query'
import { DndContext, closestCenter, type DragEndEvent } from '@dnd-kit/core'
import {
  SortableContext,
  arrayMove,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import {
  Archive,
  ArchiveRestore,
  CheckCircle2,
  Copy,
  GripVertical,
  LineChart,
  MoreVertical,
  Pencil,
  Plus,
  RefreshCw,
  X,
} from 'lucide-react'
import { toast } from 'sonner'
import { ApiError } from '@/api/client'
import { metricsCatalogApi } from '@/api/metricsCatalog'
import { factTablesApi } from '@/api/factTables'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { useDemoScenarioActions, useScenarioArtifacts } from '@/demo/demoScenarioContext'
import { Panel } from '@/components/settings/kit'
import { FilterBar, FilterSearch, FilterSelect } from '@/components/ui/filter-bar'
import { Chip, type ChipTone } from '@/components/primitives/chip'
import { Dot } from '@/components/primitives/dot'
import { MiniStat, MiniStatStrip } from '@/components/primitives/mini-stat'
import { SectionSkeleton, StatValueSkeleton } from '@/components/states'
import { Sparkline } from '@/components/primitives/sparkline'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'
import {
  startMetricCollectionWatch,
  useIsMetricCollectionWatched,
} from '@/hooks/useMetricCollectionWatcher'
import { useNow } from '@/hooks/useNow'
import { useEventsDndSensors } from '@/pages/events/useEventsDndSensors'
import { formatDateTime, formatRelativeTime } from '@/lib/datetime'
import { formatNumber } from '@/lib/format'
import { METRIC_INTERVAL_LABEL, formatMetricValue } from '@/lib/metricFormat'
import { factOperandConfigToPayload, readFactOperandConfig } from '@/lib/factOperandConfig'
import { getMetricMonitoringPath } from '@/lib/monitoring'
import { getErrorMessage } from '@/lib/utils'
import {
  METRIC_KINDS,
  METRIC_KIND_LABEL,
  METRIC_STATUS_LABEL,
  METRIC_STATUSES,
  type EventCompositionMetricCreate,
  type FactMetricCreate,
  type MetricCreate,
  type MetricDefinitionListItem,
  type MetricDefinitionListResponse,
  type MetricDefinitionDetailResponse,
  type MetricKind,
  type MetricScanInterval,
  type MetricStatus,
  type SqlMetricCreate,
} from '@/types'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanWriteProject } from '@/lib/permissions'
import { factTablesKey, metricsCatalogKey, metricsCatalogListKey, usersKey } from '@/lib/queryKeys'
import { listCatalogPage, type CatalogListParams } from './catalogRequests'
import { usersApi } from '@/api/users'
import { UserAvatar } from '@/components/ui/user-avatar'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

// The metric name gets the widest flexible track on purpose. Its cell packs a
// dot, a truncating name and a nowrap kind chip, so the widest chip ("Event
// composition", ~109px) left only ~125px for a name needing ~127px at 1.3fr —
// "Purchase conversion" clipped to save two characters while Latest held
// "10.95 %" (~55px of glyphs) in ~200px. 2fr:1fr moves ~50px to the
// name; Latest still fits its widest realistic value ("1,234,567 sessions").
const METRIC_GRID =
  'grid grid-cols-[18px_20px_minmax(0,2fr)_minmax(0,1fr)_104px_84px_84px_28px] items-center gap-3 px-4'
  // Tablet (md-lg): the Updated column goes, or the grid ran 22px wider than
  // its card and clipped the row menus at 768.
  + ' max-lg:grid-cols-[18px_20px_minmax(0,2fr)_minmax(0,1fr)_104px_84px_28px]'
  // Below md each row is a two-line card instead of a 748px-wide strip the
  // phone scrolls sideways through: handle, checkbox, name and menu on top,
  // the latest value and status under the name. The trend sparkline and the
  // relative "updated" time are the columns a phone does without.
  + ' max-md:grid-cols-[18px_20px_minmax(0,1fr)_auto_28px] max-md:gap-y-1'
/**
 * A viewer can neither reorder, select nor act on a row, so their grid has no
 * handle, checkbox or menu tracks: those left an empty 44px gutter.
 */
const VIEWER_GRID =
  'grid grid-cols-[minmax(0,2fr)_minmax(0,1fr)_104px_84px_84px] items-center gap-3 px-4'
  + ' max-lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)_104px_84px]'
  + ' max-md:grid-cols-[minmax(0,1fr)_auto] max-md:gap-y-1'
/** Where each cell sits in the phone card; no effect from md up. */
const PHONE_CELL = {
  grip: 'max-md:col-start-1 max-md:row-start-1',
  select: 'max-md:col-start-2 max-md:row-start-1',
  name: 'max-md:col-span-2 max-md:col-start-3 max-md:row-start-1',
  latest: 'max-md:col-start-3 max-md:row-start-2',
  status: 'max-md:col-start-4 max-md:row-start-2',
  menu: 'max-md:col-start-5 max-md:row-start-1',
  dropped: 'max-md:hidden',
} as const
/** {@link PHONE_CELL} for the viewer grid, which starts at the name. */
const VIEWER_PHONE_CELL = {
  ...PHONE_CELL,
  name: 'max-md:col-span-2 max-md:col-start-1 max-md:row-start-1',
  latest: 'max-md:col-start-1 max-md:row-start-2',
  status: 'max-md:col-start-2 max-md:row-start-2',
} as const
/** The Updated column: dropped on tablets as well as phones. */
const UPDATED_CELL = 'max-lg:hidden'

/**
 * The kind chip in a row: short, so it stops eating the name at tablet width.
 * The full label rides in its title.
 */
const KIND_CHIP_LABEL: Record<MetricKind, string> = {
  fact: 'Fact',
  sql: 'SQL',
  event_composition: 'Events',
}

const STATUS_TONE: Record<MetricStatus, ChipTone> = {
  draft: 'neutral',
  active: 'success',
  archived: 'warning',
}

// The filter chips' options. "Not filtering" is the chip's own `any`,
// which Radix Select needs because it cannot carry the URL's empty value.
const ANY_FILTER = 'any'
const KIND_FILTER_OPTIONS: { value: MetricKind; label: string }[] = [
  { value: 'fact', label: 'Fact' },
  { value: 'sql', label: 'SQL' },
  { value: 'event_composition', label: 'Event composition' },
]
const STATUS_FILTER_OPTIONS = METRIC_STATUSES.map(status => ({
  value: status,
  label: METRIC_STATUS_LABEL[status],
}))
type ReviewFilter = 'reviewed' | 'unreviewed'
const REVIEW_FILTER_OPTIONS: { value: ReviewFilter; label: string }[] = [
  { value: 'reviewed', label: 'Reviewed' },
  { value: 'unreviewed', label: 'Not reviewed' },
]

// Interval → milliseconds, for the staleness threshold.
const INTERVAL_MS: Record<MetricScanInterval, number> = {
  '15m': 15 * 60_000,
  '1h': 60 * 60_000,
  '6h': 6 * 60 * 60_000,
  '1d': 24 * 60 * 60_000,
  '1w': 7 * 24 * 60 * 60_000,
}

// A metric is "stale" once its freshest bucket is older than this many
// intervals — long enough that a scheduled collection was almost certainly
// missed, not merely running a little late.
const STALE_INTERVAL_MULTIPLIER = 3

type SignalFilter = 'anomalies' | 'stale'
const SIGNAL_FILTERS: readonly SignalFilter[] = ['anomalies', 'stale']

/** The URL search params the catalog's filters live in. */
type FilterParam = 'q' | 'status' | 'kind' | 'review' | 'signal' | 'fact_table'

/** How long the search box waits after the last keystroke before writing `q`. */
const SEARCH_URL_WRITE_MS = 250

/** How long an archive/restore toast (and its Undo) stays up. */
const ARCHIVE_TOAST_MS = 10_000

// The list endpoint caps a page at 1000 rows (backend metrics_catalog.py).
const CATALOG_PAGE_SIZE = 1000
// A catalog this many pages deep is not a real project; stop rather than loop
// against a server whose `total` keeps moving.
const MAX_CATALOG_PAGES = 20

/**
 * The whole filtered catalog, page after page. The screen renders, counts,
 * selects and reorders "the catalog", so a single default page (200 rows) left
 * everything past it invisible while the header still said "250 total", and a
 * drag sent a partial `metric_ids` list. If the server's total still
 * outruns what arrived (rows added mid-walk, or the page cap), the caller sees
 * `items.length < total` and says so instead of pretending.
 */
async function fetchWholeCatalog(
  slug: string,
  params: Omit<CatalogListParams, 'offset' | 'limit'>,
): Promise<MetricDefinitionListResponse> {
  const first = await listCatalogPage(slug, { ...params, offset: 0, limit: CATALOG_PAGE_SIZE })
  let items = first.items
  for (let page = 1; page < MAX_CATALOG_PAGES && items.length < first.total; page += 1) {
    const next = await listCatalogPage(slug, {
      ...params,
      offset: items.length,
      limit: CATALOG_PAGE_SIZE,
    })
    if (next.items.length === 0) break
    items = [...items, ...next.items]
  }
  return { ...first, items }
}

// A latest-scan signal (state !== 'recent') is an active anomaly on the most
// recent scan; a 'recent' signal means the newest scan was already clean.
// Mirrors the per-row `isActiveSignal` check in MetricRow.
function hasActiveSignal(metric: MetricDefinitionListItem): boolean {
  return !!metric.latest_signal && metric.latest_signal.state !== 'recent'
}

// Staleness path: MetricDefinitionListItem DOES carry a
// last-bucket timestamp (`latest_bucket`) alongside the collection `interval`,
// so we compute real staleness rather than fabricating a signal or falling back
// to a Draft count. Only ACTIVE metrics can be stale — drafts never started
// collecting and archived metrics were stopped on purpose — and only when the
// freshest bucket is older than STALE_INTERVAL_MULTIPLIER × the interval. A
// metric with no bucket or no interval is treated as not-yet-collecting.
function isStaleMetric(metric: MetricDefinitionListItem, now: number): boolean {
  if (metric.status !== 'active') return false
  if (!metric.latest_bucket || !metric.interval) return false
  const intervalMs = INTERVAL_MS[metric.interval]
  if (!intervalMs) return false
  const bucketTs = Date.parse(metric.latest_bucket)
  if (Number.isNaN(bucketTs)) return false
  return now - bucketTs > STALE_INTERVAL_MULTIPLIER * intervalMs
}

// A metric's internal name is a lowercase [a-z0-9_] identifier. Derive a unique
// copy name: `<name>_copy`, then `_2` / `_3`… on collision against the loaded
// catalog. The source name is already a valid identifier, so the suffix keeps it
// one.
// Name clashes the duplicate retries through before giving up.
const MAX_COPY_NAME_ATTEMPTS = 5

function makeCopyName(baseName: string, existing: ReadonlySet<string>): string {
  const root = `${baseName}_copy`
  if (!existing.has(root)) return root
  let suffix = 2
  while (existing.has(`${root}_${suffix}`)) suffix += 1
  return `${root}_${suffix}`
}

/**
 * Map a loaded metric definition to a fresh create payload for "Duplicate as
 * draft": copy presentation + kind-specific collection config verbatim for all
 * three kinds, overriding identity (display/internal name), forcing
 * `status: 'draft'`, appending it to the end (`order: 0`) and clearing review. The response nests kind config under `config`; the create
 * union expects it at the shapes {@link MetricForm} builds, so each kind is
 * remapped explicitly.
 */
function buildDuplicatePayload(
  def: MetricDefinitionDetailResponse,
  displayName: string,
  name: string,
): MetricCreate {
  const config = def.config
  const strOrNull = (key: string): string | null =>
    typeof config[key] === 'string' ? (config[key] as string) : null
  const base = {
    anomaly_detection_enabled: def.anomaly_detection_enabled,
    app_version_column: def.app_version_column,
    breakdown_columns: def.breakdown_columns,
    breakdown_values_limit: def.breakdown_values_limit,
    color: def.color,
    description: def.description,
    display_name: displayName,
    name,
    // 0 appends the copy to the end of the catalog; the source's own order
    // made the backend keep it, so the two tied for one position.
    order: 0,
    owner_id: def.owner_id,
    platform_column: def.platform_column,
    // A fresh draft has not been reviewed, whatever its source was.
    reviewed: false,
    status: 'draft' as const,
    unit: def.unit,
  }

  if (def.kind === 'sql') {
    const payload: SqlMetricCreate = {
      ...base,
      kind: 'sql',
      interval: def.interval ?? '1h',
      data_source_id: def.data_source_id ?? '',
      config: {
        metric_sql: typeof config['metric_sql'] === 'string' ? (config['metric_sql'] as string) : '',
        time_column: typeof config['time_column'] === 'string' ? (config['time_column'] as string) : '',
        value_column: strOrNull('value_column'),
      },
      replay_chunk_interval: def.replay_chunk_interval,
    }
    return payload
  }

  if (def.kind === 'fact') {
    if (def.composition === 'ratio') {
      const payload: FactMetricCreate = {
        ...base,
        kind: 'fact',
        composition: 'ratio',
        interval: def.interval ?? '1h',
        numerator: factOperandConfigToPayload(readFactOperandConfig(config['numerator'])),
        denominator: factOperandConfigToPayload(readFactOperandConfig(config['denominator'])),
        replay_chunk_interval: def.replay_chunk_interval,
      }
      return payload
    }
    const payload: FactMetricCreate = {
      ...base,
      kind: 'fact',
      composition: 'single',
      interval: def.interval ?? '1h',
      // One narrowing reader for every stored operand: it validates
      // the aggregation, filters `row_filters` to strings and folds a legacy
      // single `row_filter` in, which this copy used to lose.
      ...factOperandConfigToPayload(
        readFactOperandConfig(config, {
          factTableId: def.fact_table_id,
          aggregation: def.aggregation,
        }),
      ),
      fact_table_id: def.fact_table_id,
      replay_chunk_interval: def.replay_chunk_interval,
    }
    return payload
  }

  const payload: EventCompositionMetricCreate = {
    ...base,
    kind: 'event_composition',
    composition: def.composition ?? 'single',
    numerator_event_id: def.numerator_event_id,
    numerator_event_type_id: def.numerator_event_type_id,
    denominator_event_id: def.denominator_event_id,
    denominator_event_type_id: def.denominator_event_type_id,
    user_id_column: strOrNull('user_id_column'),
  }
  return payload
}

/** Refresh every catalog list in the project — after any write or a settled collect. */
function invalidateCatalog(qc: QueryClient, slug: string | undefined): void {
  void qc.invalidateQueries({ queryKey: metricsCatalogKey(slug) })
}

/**
 * Put metrics back to the statuses they had before a status change — the
 * Undo on the success toast. One bulk call per previous status.
 */
async function restoreStatuses(
  slug: string,
  previous: ReadonlyArray<{ id: string; status: MetricStatus }>,
): Promise<void> {
  const byStatus = new Map<MetricStatus, string[]>()
  for (const { id, status } of previous) {
    byStatus.set(status, [...(byStatus.get(status) ?? []), id])
  }
  for (const [status, metricIds] of byStatus) {
    await metricsCatalogApi.bulkUpdate(slug, { metric_ids: metricIds, status })
  }
}

function pluralMetrics(count: number): string {
  return count === 1 ? '1 metric' : `${formatNumber(count)} metrics`
}

/**
 * Metrics catalog body — the stat rollup, filters, and metric rows. Rendered as
 * the "Catalog" tab inside {@link MetricsPage}; owns its own data fetching and
 * spacing but not the page header/tab chrome (the parent provides those).
 */
export function MetricsCatalog({ slug }: { slug?: string }) {
  const qc = useQueryClient()
  // Reorder, bulk status and every row action are EditorUserDep; a viewer gets
  // the catalog to read and drill into, without controls that end in a 403.
  const canWrite = useCanWriteProject()
  // Every filter lives in the URL rather than in component state: each kind is
  // deep-linkable (the demo's "metric building blocks" link to Fact / SQL /
  // Event composition), and opening a metric then pressing Back
  // returns to the same search, status and signal slice instead of a reset
  // catalog. Unknown values fall back to "no filter" rather than
  // querying a bogus one. Writes replace the history entry, so typing a search
  // does not bury the previous page under one entry per keystroke.
  const [searchParams, setSearchParams] = useSearchParams()
  const kindParam = searchParams.get('kind')
  const kindFilter: '' | MetricKind = METRIC_KINDS.includes(kindParam as MetricKind)
    ? (kindParam as MetricKind)
    : ''
  const statusParam = searchParams.get('status')
  const statusFilter: '' | MetricStatus = METRIC_STATUSES.includes(statusParam as MetricStatus)
    ? (statusParam as MetricStatus)
    : ''
  // Review status is a server-side filter like status and kind.
  const reviewParam = searchParams.get('review')
  const reviewFilter: '' | ReviewFilter =
    reviewParam === 'reviewed' || reviewParam === 'unreviewed' ? reviewParam : ''
  // Client-side derived filter driven by the operational stat cells; layered on
  // top of the server-side status/kind/search filters.
  const signalParam = searchParams.get('signal')
  const signalFilter: SignalFilter | null = SIGNAL_FILTERS.includes(signalParam as SignalFilter)
    ? (signalParam as SignalFilter)
    : null
  // The metrics that read one fact table, from its "Used by" link on the fact
  // tables list (F7). Server-side, like status and kind; it has no control of
  // its own in the bar, only a chip that clears it.
  const factTableFilter = searchParams.get('fact_table') ?? ''
  const factTablesQuery = useQuery({
    queryKey: factTablesKey(slug),
    queryFn: () => factTablesApi.list(slug!),
    enabled: !!slug && !!factTableFilter,
    meta: SILENT_ERROR_META,
  })
  const factTableName =
    factTablesQuery.data?.items.find(table => table.id === factTableFilter)?.display_name
    ?? 'a fact table'
  // The search box keeps its own text and writes the settled value to the URL.
  // Bound straight to `q`, every keystroke went through an async navigation
  // that commits in a transition: React reset the DOM value to the old `q` in
  // between, so the caret jumped to the end, IME composition broke and fast
  // typing lost characters. The URL still wins when it changes from outside
  // (Back, Clear filters): `writtenSearch` is the last `q` this box wrote, so
  // its own write landing is not mistaken for an outside change.
  const urlSearch = searchParams.get('q') ?? ''
  const [searchInput, setSearchInput] = useState(urlSearch)
  const [writtenSearch, setWrittenSearch] = useState(urlSearch)
  const [seenUrlSearch, setSeenUrlSearch] = useState(urlSearch)
  if (urlSearch !== seenUrlSearch) {
    setSeenUrlSearch(urlSearch)
    if (urlSearch !== writtenSearch) {
      setWrittenSearch(urlSearch)
      setSearchInput(urlSearch)
    }
  }
  const searchWriteTimer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(searchWriteTimer.current), [])
  const setFilterParams = (patch: Partial<Record<FilterParam, string | null>>) => {
    setSearchParams(
      (previous) => {
        const params = new URLSearchParams(previous)
        for (const [key, value] of Object.entries(patch)) {
          if (value) params.set(key, value)
          else params.delete(key)
        }
        return params
      },
      { replace: true },
    )
  }
  const [selectedIds, setSelectedIds] = useState<ReadonlySet<string>>(new Set())
  const search = useDebouncedValue(searchInput, SEARCH_URL_WRITE_MS)

  const baseListKey = metricsCatalogListKey(slug, statusFilter, kindFilter, search, reviewFilter)
  // Under the same catalog prefix, so every write still invalidates it.
  const queryKey = factTableFilter ? [...baseListKey, { factTable: factTableFilter }] : baseListKey
  const metricsQuery = useQuery({
    queryKey,
    queryFn: () =>
      fetchWholeCatalog(slug!, {
        status: statusFilter ? [statusFilter] : undefined,
        kind: kindFilter || undefined,
        search: search || undefined,
        reviewed: reviewFilter ? reviewFilter === 'reviewed' : undefined,
        factTableId: factTableFilter || undefined,
      }),
    enabled: !!slug,
    staleTime: 30_000,
    // A new filter or search is a new cache entry. Without this every change
    // swapped the whole table for "Loading…" and the stats for "—", then
    // repainted, losing the scroll position on each pause in typing.
    // The previous rows stay, dimmed, until the new ones land.
    placeholderData: keepPreviousData,
  })
  const isRefreshing = metricsQuery.isPlaceholderData
  // The stat strip is a project-level summary, so it reads the UNFILTERED
  // catalog: counted from the filtered list, a search made the project look
  // like it had 0 metrics. With no filter on this is the same cache
  // entry as the list above, so it costs no second request.
  const summaryQuery = useQuery({
    queryKey: metricsCatalogListKey(slug, '', '', ''),
    queryFn: () => fetchWholeCatalog(slug!, {}),
    enabled: !!slug,
    staleTime: 30_000,
    meta: SILENT_ERROR_META,
  })
  const summary = summaryQuery.data
  const summaryItems = useMemo(() => summary?.items ?? [], [summary])

  const data = metricsQuery.data
  const metrics = useMemo(() => data?.items ?? [], [data])
  const total = data ? data.total : 0
  // More rows on the server than arrived: say so, and keep whole-catalog
  // actions (reorder) off, rather than acting on a slice.
  const isTruncated = !!data && metrics.length < total
  // Internal names of the loaded catalog — the collision set for the "Duplicate
  // as draft" copy-name suffixing.
  const existingNames = useMemo(() => new Set(metrics.map(m => m.name)), [metrics])
  // Server-side, so it sits on the same basis as the Metrics total beside it.
  // Counting the loaded page made the two stats disagree the moment the catalog
  // outgrew one page.
  const summaryTotal = summary ? summary.total : 0
  const active = summary?.active_total ?? summaryItems.filter(m => m.status === 'active').length
  // Staleness is judged against a clock that keeps moving. A "now" frozen at
  // mount never counted a metric that went stale while the tab stayed open —
  // the normal life of a monitoring surface. A fresh fetch moves it
  // too, so the count agrees with the rows it just received.
  const tickMs = useNow(60_000)
  const nowMs = Math.max(tickMs, metricsQuery.dataUpdatedAt)
  // Operational rollups over the whole catalog: no
  // filter — search, status, kind or the stat toggles themselves — changes them.
  const anomalyCount = useMemo(() => summaryItems.filter(hasActiveSignal).length, [summaryItems])
  const staleCount = useMemo(
    () => summaryItems.filter(m => isStaleMetric(m, nowMs)).length,
    [summaryItems, nowMs],
  )
  // Widest sparkline in the loaded set → the real "last N points" the Trend
  // column represents. Each spark is a trailing, bounded window
  // of metric-value buckets, so the point count is the honest, data-derived
  // label; the wall-clock window differs per metric interval and isn't knowable
  // at the column level.
  const trendPoints = useMemo(
    () => metrics.reduce((max, m) => Math.max(max, m.spark.length), 0),
    [metrics],
  )
  // Client-side view over the loaded list, applied on top of the server-side
  // status/kind/search filters.
  const visibleMetrics = useMemo(() => {
    if (signalFilter === 'anomalies') return metrics.filter(hasActiveSignal)
    if (signalFilter === 'stale') return metrics.filter(m => isStaleMetric(m, nowMs))
    return metrics
  }, [metrics, signalFilter, nowMs])
  // The coached demo scenario points at ONE row, not every row:
  // the collect step reads as an example ("pick a metric"), so N callouts would
  // be noise. Both ids are null outside a demo scenario, and the mark itself is
  // an early return, so nothing below changes for a real project.
  const scenarioMetricId = useScenarioArtifacts().metricId
  const coachTargetId = visibleMetrics[0]?.id ?? null
  // The debounced search counts too: for 250ms after "Clear" the list still
  // holds the old search's (empty) result, which is not "no metrics yet".
  const hasFilters =
    !!statusFilter
    || !!kindFilter
    || !!reviewFilter
    || !!factTableFilter
    || !!searchInput
    || !!search
    || !!signalFilter
  // Loaded with no metrics AND no active filters — the true "nothing here yet"
  // state, distinct from loading, error, and "filters matched nothing".
  const isEmpty = !metricsQuery.isError && !!data && metrics.length === 0 && !hasFilters

  // Reorder only makes sense against the full, unfiltered catalog: a partial
  // list can't express the canonical order the backend persists. Placeholder
  // rows belong to the previous filter, so they are not that list either.
  const canReorder =
    canWrite && !hasFilters && !isTruncated && !isRefreshing && metrics.length > 1

  // The active filters, named, for the "nothing matched" state.
  const activeFilterLabels = [
    searchInput ? `search “${searchInput}”` : null,
    statusFilter ? `status ${METRIC_STATUS_LABEL[statusFilter]}` : null,
    kindFilter ? `kind ${METRIC_KIND_LABEL[kindFilter]}` : null,
    reviewFilter === 'reviewed' ? 'reviewed metrics' : null,
    reviewFilter === 'unreviewed' ? 'metrics not yet reviewed' : null,
    factTableFilter ? `metrics reading ${factTableName}` : null,
    signalFilter === 'anomalies' ? 'metrics with anomalies' : null,
    signalFilter === 'stale' ? 'stale metrics' : null,
  ].filter((label): label is string => label !== null)

  const selected = useMemo(
    () => visibleMetrics.filter(m => selectedIds.has(m.id)),
    [visibleMetrics, selectedIds],
  )
  const allSelected = visibleMetrics.length > 0 && selected.length === visibleMetrics.length

  const toggleSelected = (id: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }
  // From "some selected" the header box clears, as its minus sign promises;
  // only an empty selection selects everything.
  const toggleAll = () => {
    setSelectedIds(selected.length > 0 ? new Set() : new Set(visibleMetrics.map(m => m.id)))
  }
  // Filter/search changes swap the visible set; carrying hidden selections
  // across views would let a later bulk action silently hit rows the user
  // no longer sees — so every view change starts with a clean slate. A
  // server-side filter change also swaps the loaded set out from under the
  // client-side signal filter, so it drops that too.
  const setServerFilter = (param: Exclude<FilterParam, 'signal'>, value: string) => {
    setSelectedIds(new Set())
    setFilterParams({ [param]: value, signal: null })
  }
  // Toggling a stat cell filters the table to that operational slice; clicking
  // the active one clears it. Every view change starts with a clean selection
  // so a later bulk action can't hit hidden rows.
  const changeSearch = (value: string) => {
    setSelectedIds(new Set())
    setSearchInput(value)
    window.clearTimeout(searchWriteTimer.current)
    searchWriteTimer.current = window.setTimeout(() => {
      setWrittenSearch(value)
      setFilterParams({ q: value, signal: null })
    }, SEARCH_URL_WRITE_MS)
  }
  const toggleSignalFilter = (filter: SignalFilter) => {
    setSelectedIds(new Set())
    setFilterParams({ signal: signalFilter === filter ? null : filter })
  }
  const clearFilters = () => {
    setSelectedIds(new Set())
    window.clearTimeout(searchWriteTimer.current)
    setSearchInput('')
    setWrittenSearch('')
    setFilterParams({ q: null, status: null, kind: null, review: null, signal: null, fact_table: null })
  }

  const bulkStatusMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: async (status: MetricStatus) => {
      // Captured before the call: the Undo puts back what each row WAS.
      const previous = selected
        .filter(m => m.status !== status)
        .map(m => ({ id: m.id, status: m.status }))
      await metricsCatalogApi.bulkUpdate(slug!, {
        metric_ids: selected.map(m => m.id),
        status,
      })
      return { status, count: selected.length, previous }
    },
    onSuccess: ({ status, count, previous }) => {
      setSelectedIds(new Set())
      invalidateCatalog(qc, slug)
      // One click used to archive N metrics and stop their collection with no
      // way back. The toast carries the way back.
      toast.success(
        `${pluralMetrics(count)} set to ${METRIC_STATUS_LABEL[status].toLowerCase()}.`,
        previous.length > 0 && slug
          ? {
              action: {
                label: 'Undo',
                onClick: () => {
                  restoreStatuses(slug, previous).then(
                    () => invalidateCatalog(qc, slug),
                    (error: unknown) => {
                      invalidateCatalog(qc, slug)
                      toast.error(`Could not undo — ${getErrorMessage(error)}`)
                    },
                  )
                },
              },
            }
          : undefined,
      )
    },
  })

  // Marks every selected metric reviewed in one call.
  const bulkReviewMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: async () => {
      await metricsCatalogApi.bulkUpdate(slug!, {
        metric_ids: selected.map(m => m.id),
        reviewed: true,
      })
      return selected.length
    },
    onSuccess: count => {
      setSelectedIds(new Set())
      invalidateCatalog(qc, slug)
      toast.success(`${pluralMetrics(count)} marked reviewed.`)
    },
  })

  const reorderMut = useMutation({
    // Its own toast below; the rows snapping back needs a reason next to it.
    meta: SILENT_ERROR_META,
    mutationFn: (metricIds: string[]) =>
      metricsCatalogApi.reorder(slug!, { metric_ids: metricIds }),
    onMutate: async (metricIds: string[]) => {
      // Optimistic: repaint rows in the dropped order while the PATCH runs.
      await qc.cancelQueries({ queryKey })
      const previous = qc.getQueryData<MetricDefinitionListResponse>(queryKey)
      if (previous) {
        const byId = new Map(previous.items.map(item => [item.id, item]))
        qc.setQueryData<MetricDefinitionListResponse>(queryKey, {
          ...previous,
          items: metricIds
            .map(id => byId.get(id))
            .filter((item): item is MetricDefinitionListItem => item !== undefined),
        })
      }
      return { previous }
    },
    onError: (error, _ids, context) => {
      if (context?.previous) qc.setQueryData(queryKey, context.previous)
      // The rows jump back; without a word that looks like the drop misfired.
      toast.error(`Could not save the new order — ${getErrorMessage(error)}`)
    },
    onSettled: () => {
      invalidateCatalog(qc, slug)
    },
  })

  // Shared pointer+keyboard sensor pair — keyboard so the handle's injected
  // "press space to lift" instructions are actually true.
  const sensors = useEventsDndSensors()
  const handleDragEnd = (event: DragEndEvent) => {
    const { active: dragged, over } = event
    if (!over || dragged.id === over.id) return
    const oldIndex = metrics.findIndex(m => m.id === dragged.id)
    const newIndex = metrics.findIndex(m => m.id === over.id)
    if (oldIndex < 0 || newIndex < 0) return
    reorderMut.mutate(arrayMove(metrics, oldIndex, newIndex).map(m => m.id))
  }

  return (
    <div className={isEmpty ? 'flex min-h-[calc(100vh-14rem)] flex-col gap-6' : 'space-y-6'}>
      {metricsQuery.isError ? (
        <ErrorState
          title="Metrics unavailable"
          error={metricsQuery.error}
          onRetry={() => {
            void metricsQuery.refetch()
          }}
          retryLabel="Retry"
          compact
        />
      ) : (
        <MiniStatStrip boxed phoneGrid className={isEmpty ? 'opacity-60' : undefined}>
          {/* Pending values are a skeleton bar with no tone, never "—" or a
              green 0 that reads as an answer (#237). */}
          <MiniStat label="Metrics" value={summary ? formatNumber(summaryTotal) : <StatValueSkeleton />} />
          <MiniStat
            label="Active"
            value={summary ? formatNumber(active) : <StatValueSkeleton />}
            tone={summary ? 'success' : undefined}
          />
          {/* A pressable stat doubles as a one-click table filter:
              MiniStat renders the <button aria-pressed>
              itself and cancels its own hover padding, so the phone grid's
              second row lines up with the first. */}
          <MiniStat
            // Scoped label, not the bare word "Anomalies": this counts CATALOG
            // METRICS whose latest scan still carries a signal, while the
            // sidebar's "Anomalies" badge is every significant open signal
            // across every scope (event, event type, project total). A project
            // whose anomalies all sit outside the metric catalog therefore
            // renders 0 here beside a red 3 in the nav ~300px away, and a
            // reader stops trusting both numbers. "With" keeps
            // that scope beside the "Metrics" tile without repeating it.
            label="With anomalies"
            value={
              summary ? (
                <span style={{ color: anomalyCount > 0 ? 'var(--danger)' : undefined }}>
                  {formatNumber(anomalyCount)}
                </span>
              ) : (
                <StatValueSkeleton />
              )
            }
            tone={summary && anomalyCount > 0 ? 'danger' : 'neutral'}
            onPress={() => toggleSignalFilter('anomalies')}
            pressed={signalFilter === 'anomalies'}
            title="Catalog metrics whose latest scan still carries an anomaly signal. The Anomalies page counts open signals across every scope, so its total can be higher."
          />
          <MiniStat
            label="Stale"
            value={
              summary ? (
                <span style={{ color: staleCount > 0 ? 'var(--warning)' : undefined }}>
                  {formatNumber(staleCount)}
                </span>
              ) : (
                <StatValueSkeleton />
              )
            }
            tone={summary && staleCount > 0 ? 'warning' : 'neutral'}
            onPress={() => toggleSignalFilter('stale')}
            pressed={signalFilter === 'stale'}
            title="Catalog metrics whose latest bucket is more than three collection intervals old."
          />
        </MiniStatStrip>
      )}

      {!metricsQuery.isError &&
        (isEmpty ? (
          <div className="flex flex-1 items-center justify-center">
            <EmptyState
              icon={LineChart}
              title="No metrics yet"
              description="Metrics turn a SQL query, a warehouse aggregation, or an event ratio into a tracked time series with anomaly detection. Create one to start collecting."
              action={
                slug && canWrite ? (
                  <Button asChild size="sm">
                    <Link to={projectPath(currentOrgSlug(), slug, '/metrics/new')} className="no-underline">
                      <Plus className="h-3.5 w-3.5" />
                      New metric
                    </Link>
                  </Button>
                ) : undefined
              }
            />
          </div>
        ) : (
          <Panel
            title="Catalog"
            subtitle={
              data
                ? `${
                    hasFilters && summary
                      ? `${formatNumber(visibleMetrics.length)} of ${pluralMetrics(summaryTotal)}`
                      : `${formatNumber(total)} total`
                  }${isRefreshing ? ' · Updating…' : ''}`
                : undefined
            }
          >
            {/* The app's one filter bar: search, then "{Label}: {value}"
                chips that apply instantly. Native selects here drew a different
                height, font and dropdown from every other control. */}
            <FilterBar
              active={hasFilters}
              onClear={clearFilters}
              className="border-b px-4 py-2"
            >
              <FilterSearch things="metrics" value={searchInput} onValueChange={changeSearch} />
              <FilterSelect
                label="Status"
                value={statusFilter || ANY_FILTER}
                onValueChange={value => setServerFilter('status', value === ANY_FILTER ? '' : value)}
                options={STATUS_FILTER_OPTIONS}
              />
              <FilterSelect
                label="Kind"
                value={kindFilter || ANY_FILTER}
                onValueChange={value => setServerFilter('kind', value === ANY_FILTER ? '' : value)}
                options={KIND_FILTER_OPTIONS}
              />
              <FilterSelect
                label="Review status"
                value={reviewFilter || ANY_FILTER}
                onValueChange={value => setServerFilter('review', value === ANY_FILTER ? '' : value)}
                options={REVIEW_FILTER_OPTIONS}
              />
              {factTableFilter && (
                <Button
                  type="button"
                  variant="outline"
                  size="xs"
                  aria-label={`Stop showing only metrics that read ${factTableName}`}
                  onClick={() => {
                    setSelectedIds(new Set())
                    setFilterParams({ fact_table: null, signal: null })
                  }}
                >
                  Reads {factTableName}
                  <X aria-hidden="true" />
                </Button>
              )}
            </FilterBar>
            {canWrite && selected.length > 0 && (
              <div
                className="flex flex-wrap items-center gap-2 border-b px-4 py-2 border-border-subtle bg-bg-sunken"
              >
                <span className="text-body-sm font-medium text-fg">
                  {selected.length} selected
                </span>
                {METRIC_STATUSES.map(status => (
                  <Button
                    key={status}
                    size="sm"
                    variant="outline"
                    disabled={bulkStatusMut.isPending}
                    onClick={() => bulkStatusMut.mutate(status)}
                  >
                    Set {METRIC_STATUS_LABEL[status].toLowerCase()}
                  </Button>
                ))}
                <Button
                  size="sm"
                  variant="outline"
                  disabled={bulkReviewMut.isPending}
                  onClick={() => bulkReviewMut.mutate()}
                >
                  Mark reviewed
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setSelectedIds(new Set())}
                >
                  Clear
                </Button>
                {(bulkStatusMut.isError || bulkReviewMut.isError) && (
                  <span className="text-caption text-danger">
                    {getErrorMessage(bulkStatusMut.error ?? bulkReviewMut.error)}
                  </span>
                )}
              </div>
            )}
            {isTruncated && (
              <div
                role="status"
                className="border-b px-4 py-2 text-body-sm border-border-subtle text-warning"
              >
                Showing {formatNumber(metrics.length)} of {formatNumber(total)} metrics.
                Narrow the list with a search or filter to reach the rest; reordering is off
                until the whole catalog is listed.
              </div>
            )}
            {metricsQuery.isLoading ? (
              <SectionSkeleton variant="rows" label="Loading metrics…" />
            ) : visibleMetrics.length === 0 ? (
              // Names what is filtering, the stat toggle included: it is not an
              // obvious control to undo. The one-click way out is the
              // filter bar's "Clear filters" directly above.
              <div
                className="flex flex-wrap items-center gap-x-3 gap-y-2 px-4 py-6 text-body-sm text-fg-tertiary"
              >
                <span>
                  {activeFilterLabels.length > 0
                    ? `No metrics match ${activeFilterLabels.join(', ')}.`
                    : 'No metrics match the current filters.'}
                </span>
              </div>
            ) : (
              // DndContext renders dnd-kit's own <div role="status"> live region as
              // an INLINE child (@dnd-kit/core 6.3.1 only portals it when an
              // `accessibility.container` DOM node is handed to it, and a ref to
              // one is null on the render that mounts the region). Left inside
              // role="table" that is a child with no permitted role — ARIA allows
              // only row, rowgroup and caption there — so axe reports
              // aria-required-children and assistive tech can no longer trust the
              // table's row structure. Hoisting the provider ABOVE the table keeps
              // the drag announcements and moves them out of the grid; it is the
              // shape EventsTable.tsx already uses. SortableContext renders no DOM
              // and every useSortable in MetricRow is still inside the provider, so
              // nothing about dragging changes.
              <DndContext
                sensors={sensors}
                collisionDetection={closestCenter}
                onDragEnd={handleDragEnd}
              >
                <div
                  className="overflow-x-auto transition-opacity"
                  style={{ opacity: isRefreshing ? 0.6 : undefined }}
                >
                  <div
                    role="table"
                    aria-label="Metrics"
                    aria-busy={isRefreshing || undefined}
                    className="md:min-w-[652px] lg:min-w-[748px]"
                  >
                    <div role="rowgroup">
                      <div
                        role="row"
                        className={`${canWrite ? METRIC_GRID : VIEWER_GRID} border-b py-2 micro-label border-border-subtle text-fg-tertiary`}
                      >
                        {canWrite && (
                          <>
                            <span role="columnheader" aria-label="Reorder" />
                            <span role="columnheader">
                              <Checkbox
                                aria-label="Select all metrics"
                                checked={allSelected ? true : selected.length > 0 ? 'indeterminate' : false}
                                onCheckedChange={toggleAll}
                              />
                            </span>
                          </>
                        )}
                        <span role="columnheader" className="max-md:col-span-2">Metric</span>
                        {/* The card shows these two under the name, unlabelled;
                            screen readers still get the header. */}
                        <span role="columnheader" className="text-right max-md:sr-only">Latest</span>
                        {/* "20 pts" said nothing about the time span; the
                            count of collections sits in the tooltip. */}
                        <span
                          role="columnheader"
                          className={PHONE_CELL.dropped}
                          title={trendPoints > 0 ? `Last ${trendPoints} collections` : undefined}
                        >
                          Trend
                        </span>
                        <span role="columnheader" className="max-md:sr-only">Status</span>
                        <span role="columnheader" className={`text-right ${UPDATED_CELL}`}>Updated</span>
                        {canWrite && <span role="columnheader" aria-label="Actions" />}
                      </div>
                    </div>
                    <SortableContext
                      items={visibleMetrics.map(m => m.id)}
                      strategy={verticalListSortingStrategy}
                    >
                      <div role="rowgroup">
                        {visibleMetrics.map(metric => (
                          <MetricRow
                            key={metric.id}
                            metric={metric}
                            slug={slug}
                            canReorder={canReorder}
                            canWrite={canWrite}
                            existingNames={existingNames}
                            isSelected={selectedIds.has(metric.id)}
                            onToggleSelected={() => toggleSelected(metric.id)}
                            isCoachTarget={metric.id === coachTargetId}
                            isScenarioMetric={metric.id === scenarioMetricId}
                          />
                        ))}
                      </div>
                    </SortableContext>
                  </div>
                </div>
              </DndContext>
            )}
          </Panel>
        ))}
    </div>
  )
}

interface MetricRowProps {
  metric: MetricDefinitionListItem
  slug?: string
  canReorder: boolean
  /** False for a viewer: no select box and no row menu (every item writes). */
  canWrite: boolean
  existingNames: ReadonlySet<string>
  isSelected: boolean
  onToggleSelected: () => void
  /** The single row the demo scenario's collect step points at. */
  isCoachTarget: boolean
  /** This row is the metric the demo scenario is tracking to its chart. */
  isScenarioMetric: boolean
}

function MetricRow({
  metric,
  slug,
  canReorder,
  canWrite,
  existingNames,
  isSelected,
  onToggleSelected,
  isCoachTarget,
  isScenarioMetric,
}: MetricRowProps) {
  const navigate = useNavigate()
  const href = slug ? getMetricMonitoringPath(slug, metric.id) : undefined
  // The roster names an owned metric's owner; rows share the one request,
  // and a catalog with no owners makes none.
  const { data: users } = useQuery({
    queryKey: usersKey(),
    queryFn: () => usersApi.list(),
    enabled: !!metric.owner_id,
    meta: SILENT_ERROR_META,
  })
  const owner = metric.owner_id ? users?.find(user => user.id === metric.owner_id) : undefined
  const ownerName = owner ? owner.name || owner.email : null
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({
    id: metric.id,
    disabled: !canReorder,
  })
  const signal = metric.latest_signal
  // Only a signal from the latest scan reflects an active anomaly. A "recent"
  // (older) signal means the most recent scan was clean, so the current
  // observation is normal — don't pulse the dot, tone the value, or mark the
  // last (now-normal) spark point.
  const isActiveSignal = !!signal && signal.state !== 'recent'
  const signalTone: ChipTone | undefined = isActiveSignal
    ? signal.direction === 'drop'
      ? 'warning'
      : 'danger'
    : undefined
  const anomalyIdx = isActiveSignal && metric.spark.length > 0 ? metric.spark.length - 1 : null
  const cell = canWrite ? PHONE_CELL : VIEWER_PHONE_CELL

  // Latest-cell tooltip: prefer the actual bucket time of the
  // latest value, then a signal's bucket, else fall back to the collection
  // cadence so the cell always carries some temporal context.
  const bucketIso = metric.latest_bucket ?? metric.latest_signal?.bucket ?? null
  const latestTitle = bucketIso
    ? `Latest point: ${formatDateTime(bucketIso)}`
    : metric.interval
      ? `Collected ${METRIC_INTERVAL_LABEL[metric.interval].toLowerCase()}`
      : undefined

  // No tabIndex and no key handler on the row: the name Link is the keyboard
  // route to the same page, so a focusable row only added a second Tab stop
  // per row that did the same thing, announced as every cell run together.
  // The row click stays for the pointer, as a bigger target.
  return (
    // eslint-disable-next-line jsx-a11y/click-events-have-key-events -- pointer-only convenience; the name Link is the keyboard route
    <div
      ref={setNodeRef}
      role="row"
      // Height follows the Appearance density: `--row-h` is the floor,
      // so compact rows sit tighter and comfy rows open up like the Events table.
      className={`${canWrite ? METRIC_GRID : VIEWER_GRID} min-h-(--row-h) border-b py-1.5 last:border-0 ${
        href ? 'cursor-pointer transition-colors hover:bg-[var(--surface-hover)]' : 'cursor-default'
      } border-border-subtle`}
      style={{
        transform: CSS.Transform.toString(transform),
        transition,
        opacity: isDragging ? 0.6 : undefined,
        position: 'relative',
        zIndex: isDragging ? 1 : undefined,
      }}
      onClick={href ? () => navigate(href) : undefined}
    >
      {canWrite && (
        <>
          <span role="cell" className={PHONE_CELL.grip}>
            {canReorder ? (
              <button
                type="button"
                aria-label={`Reorder ${metric.display_name}`}
                // Dragging a row on a phone is impractical; the handle stays
                // for a pointer from md up.
                className="flex cursor-grab touch-none items-center justify-center rounded-sm p-0.5 hover:bg-[var(--surface-hover)] active:cursor-grabbing max-md:hidden text-fg-tertiary"
                onClick={event => event.stopPropagation()}
                {...attributes}
                {...listeners}
              >
                <GripVertical className="h-3.5 w-3.5" />
              </button>
            ) : null}
          </span>
          <span role="cell" className={PHONE_CELL.select}>
            <Checkbox
              aria-label={`Select ${metric.display_name}`}
              checked={isSelected}
              onCheckedChange={onToggleSelected}
              onClick={event => event.stopPropagation()}
            />
          </span>
        </>
      )}
      <span role="cell" className={`flex min-w-0 items-center gap-2 ${cell.name}`}>
        {signalTone ? (
          <Dot tone={signalTone} pulse size={7} />
        ) : (
          <span
            className="inline-block h-[7px] w-[7px] shrink-0 rounded-full"
            style={{ background: metric.color }}
          />
        )}
        {href ? (
          // The scenario completes see-chart the moment the chart route opens, so
          // a mark on the chart itself would never be read: it points here, at the
          // link to the metric the user just collected.
          <ScenarioCoachMark step="live-loop/see-chart" when={isScenarioMetric}>
            <Link
              to={href}
              onClick={event => event.stopPropagation()}
              // A wide kind chip can still clip a long name, and the name is the
              // one cell a reader cannot reconstruct from the rest of the row —
              // so it carries its own tooltip, as the Latest cell below already
              // does.
              title={metric.display_name}
              className="truncate text-body-sm font-medium no-underline hover:underline text-fg"
            >
              {metric.display_name}
            </Link>
          </ScenarioCoachMark>
        ) : (
          <span className="truncate text-body-sm font-medium" title={metric.display_name}>
            {metric.display_name}
          </span>
        )}
        {/* A kind tag is an outline pill, a status a soft one. */}
        <Chip variant="outline" title={METRIC_KIND_LABEL[metric.kind]}>
          {KIND_CHIP_LABEL[metric.kind]}
        </Chip>
        {/* Who answers for the metric. In the name's flexible track:
            the 84px Status cell has no room left beside its chip and check. */}
        {ownerName && <UserAvatar name={ownerName} size={18} label={`Owner: ${ownerName}`} />}
      </span>
      <span
        role="cell"
        title={latestTitle}
        // A figure, not an identifier: sans with tabular digits,
        // right-aligned so magnitudes line up.
        className={`tnum truncate text-body-sm font-medium md:text-right ${cell.latest}`}
        style={{ color: signalTone ? `var(--${signalTone})` : 'var(--fg)' }}
      >
        {formatMetricValue(metric.latest_value, metric.unit)}
      </span>
      <span role="cell" className={PHONE_CELL.dropped}>
        {metric.spark.length > 0 ? (
          <Sparkline data={metric.spark} color={metric.color} anomalyIdx={anomalyIdx} width={96} height={22} />
        ) : (
          <span className="text-caption text-fg-tertiary">
            —
          </span>
        )}
      </span>
      <span role="cell" className={`flex items-center gap-1 ${cell.status}`}>
        <Chip tone={STATUS_TONE[metric.status]}>
          {METRIC_STATUS_LABEL[metric.status]}
        </Chip>
        {/* The review state events already show, so a reader can tell which
            metrics are vetted. */}
        {metric.reviewed && (
          <span title="Reviewed" className="inline-flex shrink-0 text-success">
            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
            <span className="sr-only">Reviewed</span>
          </span>
        )}
      </span>
      <span
        role="cell"
        className={`tnum text-right text-micro ${UPDATED_CELL} text-fg-tertiary`}
      >
        {formatRelativeTime(metric.updated_at)}
      </span>
      {canWrite && (
        <span role="cell" className={`flex justify-end ${PHONE_CELL.menu}`}>
          {slug ? (
            <MetricRowMenu
              metric={metric}
              slug={slug}
              existingNames={existingNames}
              isCoachTarget={isCoachTarget}
            />
          ) : null}
        </span>
      )}
    </div>
  )
}

interface MetricRowMenuProps {
  metric: MetricDefinitionListItem
  slug: string
  existingNames: ReadonlySet<string>
  isCoachTarget: boolean
}

/**
 * Per-row overflow (kebab) menu: Edit, Duplicate as draft, Collect now, and
 * Archive/Restore. Reuses the shared Radix DropdownMenu primitive (same one the
 * monitoring detail page uses). The trigger stops click propagation so opening
 * the menu never fires the row's navigate-on-click (mirrors the reorder handle /
 * checkbox); the menu content is portaled, so item clicks never bubble to the
 * row either.
 */
function MetricRowMenu({ metric, slug, existingNames, isCoachTarget }: MetricRowMenuProps) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const { notifyMetricCollectStarted } = useDemoScenarioActions()
  const isArchived = metric.status === 'archived'

  const duplicateMut = useMutation({
    mutationFn: async () => {
      const def = await metricsCatalogApi.get(slug, metric.id)
      // The loaded list is only the filtered view, so a hidden `<name>_copy`
      // can still be taken: on a name clash take the next suffix rather than
      // failing the whole action.
      const taken = new Set(existingNames)
      for (let attempt = 1; ; attempt += 1) {
        const name = makeCopyName(def.name, taken)
        try {
          return await metricsCatalogApi.create(
            slug,
            buildDuplicatePayload(def, `${def.display_name} (copy)`, name),
          )
        } catch (error) {
          const nameTaken = error instanceof ApiError && error.status === 409
          if (!nameTaken || attempt >= MAX_COPY_NAME_ATTEMPTS) throw error
          taken.add(name)
        }
      }
    },
    onSuccess: created => {
      invalidateCatalog(qc, slug)
      toast.success('Metric duplicated as a draft.')
      navigate(projectPath(currentOrgSlug(), slug, `/metrics/${created.id}/edit`))
    },
    // No onError: the global backstop already toasts this exact message.
  })

  const statusMut = useMutation({
    mutationFn: (status: MetricStatus) => metricsCatalogApi.update(slug, metric.id, { status }),
    onSuccess: (_data, status) => {
      invalidateCatalog(qc, slug)
      // Archiving stops collection; the toast carries the way back,
      // names the metric and stays long enough to be read: a misclicked
      // Archive went unnoticed behind the default ~4s toast.
      const previousStatus = metric.status
      const name = `“${metric.display_name}”`
      toast.success(
        status === 'archived' ? `${name} archived — collection stopped.` : `${name} restored.`,
        {
          duration: ARCHIVE_TOAST_MS,
          action: {
            label: 'Undo',
            onClick: () => {
              metricsCatalogApi.update(slug, metric.id, { status: previousStatus }).then(
                () => invalidateCatalog(qc, slug),
                (error: unknown) => toast.error(`Could not undo — ${getErrorMessage(error)}`),
              )
            },
          },
        },
      )
    },
  })

  // The review state metrics carry but no screen could set.
  const reviewMut = useMutation({
    mutationFn: (reviewed: boolean) => metricsCatalogApi.update(slug, metric.id, { reviewed }),
    onSuccess: (_data, reviewed) => {
      invalidateCatalog(qc, slug)
      toast.success(
        reviewed
          ? `“${metric.display_name}” marked reviewed.`
          : `“${metric.display_name}” marked not reviewed.`,
      )
    },
  })

  // The watch is detached from this row: search, a filter, a stat toggle or
  // leaving the page all unmount the row, and each used to end the watch
  // silently, breaking the "you will be notified" promise. The list is
  // refreshed on success AND on error, so the row's status never stays stale.
  const isCollecting = useIsMetricCollectionWatched(slug, metric.id)
  const collectMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: () => metricsCatalogApi.collect(slug, metric.id),
    onSuccess: () => {
      toast.success('Collection started — you will be notified when it finishes.')
      // The slug travels with the watch: leaving the project mid-run must not
      // repoint the poll at another project's metric id.
      startMetricCollectionWatch(
        { slug, metricId: metric.id, displayName: metric.display_name },
        { onSettled: () => invalidateCatalog(qc, slug) },
      )
      // Only a collect the USER started advances the scenario — the demo's tick
      // manufactures collections of its own. Inert elsewhere.
      notifyMetricCollectStarted(metric.id)
    },
    // Its own toast (silenced in the backstop): what failed, and why.
    onError: error => toast.error(`Could not start collection — ${getErrorMessage(error)}`),
  })

  const busy =
    duplicateMut.isPending
    || statusMut.isPending
    || reviewMut.isPending
    || collectMut.isPending
    || isCollecting

  return (
    <DropdownMenu>
      {/* One row carries the collect mark, so the coaching reads as an example
          rather than a per-row instruction. Anchoring the trigger (not the menu)
          keeps the mark visible while the menu is still closed. */}
      <ScenarioCoachMark step="live-loop/collect-metric" when={isCoachTarget}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label={`Actions for ${metric.display_name}`}
            className="flex items-center justify-center rounded-sm p-0.5 hover:bg-[var(--surface-hover)] text-fg-tertiary"
            onClick={event => event.stopPropagation()}
          >
            <MoreVertical className="h-3.5 w-3.5" />
          </button>
        </DropdownMenuTrigger>
      </ScenarioCoachMark>
      {/* The portaled content still bubbles clicks through the REACT tree (portal
          synthetic events), so without this stop the row's navigate-on-click fires
          for every item selection and unmounts the row mid-action. */}
      <DropdownMenuContent
        align="end"
        sideOffset={6}
        className="w-[184px]"
        onClick={event => event.stopPropagation()}
      >
        <DropdownMenuItem
          className="text-body-sm"
          onSelect={() => navigate(projectPath(currentOrgSlug(), slug, `/metrics/${metric.id}/edit`))}
        >
          <Pencil className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" /> Edit
        </DropdownMenuItem>
        <DropdownMenuItem
          className="text-body-sm"
          disabled={busy}
          onSelect={() => duplicateMut.mutate()}
        >
          <Copy className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" /> Duplicate as draft
        </DropdownMenuItem>
        {/* The collect step's second gesture, once its menu is open. */}
        <ScenarioCoachMark
          step="live-loop/collect-metric"
          followUp
          when={isCoachTarget}
          tag="Then click here"
          side="left"
        >
          <DropdownMenuItem
            className="text-body-sm"
            disabled={busy}
            onSelect={() => collectMut.mutate()}
          >
            <RefreshCw className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" /> Collect now
          </DropdownMenuItem>
        </ScenarioCoachMark>
        <DropdownMenuItem
          className="text-body-sm"
          disabled={busy}
          onSelect={() => reviewMut.mutate(!metric.reviewed)}
        >
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" />{' '}
          {metric.reviewed ? 'Mark not reviewed' : 'Mark reviewed'}
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          className="text-body-sm"
          variant={isArchived ? 'default' : 'destructive'}
          disabled={busy}
          onSelect={() => statusMut.mutate(isArchived ? 'active' : 'archived')}
        >
          {isArchived ? (
            <>
              <ArchiveRestore className="h-3.5 w-3.5 shrink-0 text-fg-tertiary" /> Restore
            </>
          ) : (
            <>
              <Archive className="h-3.5 w-3.5 shrink-0" /> Archive
            </>
          )}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
