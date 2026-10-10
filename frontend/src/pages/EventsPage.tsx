import { useCallback, useMemo, useState } from 'react'
import { Link, Navigate, useLocation, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { FileQuestion } from 'lucide-react'
import { toast } from 'sonner'
import { usersApi } from '@/api/users'
import { useConfirm } from '@/hooks/useConfirm'
import { useActiveBranchId } from '@/hooks/useBranch'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/empty-state'
import { ErrorState } from '@/components/error-state'
import { SCENARIO_SEEDED } from '@/demo/scenarioModel'
import { EVENT_STATUS_LABELS, type EventStatus } from '@/lib/eventStatus'
import { getErrorMessage } from '@/lib/utils'
import { getMonitoringPath } from '@/lib/monitoring'
import type { EventListItem } from '@/types'

import { BulkActionBar } from './events/BulkActionBar'
import { bulkUpdateConfirmation } from './events/bulkConfirm'
import { EventsHeader, EventTypeDriftBadges, type EventTypeDrift } from './events/EventsHeader'
import { EventsTable } from './events/EventsTable'
import { EventsFirstRun } from './events/EventsFirstRun'
import { EventsToolbar } from './events/EventsToolbar'
import { TabMetricsCard } from './events/TabMetricsCard'
import {
  buildEventsCsvColumns,
  downloadCsv,
  eventsCsvFilename,
  toCsv,
} from './events/eventsCsv'
import {
  HEALTH_COL_KEY,
  shownColumnKey,
  typeSpecificFieldKeys,
  useColumnVisibility,
  withDefaultHidden,
} from './events/useColumnVisibility'
import { useEventsBulkDelete } from './events/useEventsBulkDelete'
import { ConfirmImpactMessage } from '@/components/dependencies/ImpactNotice'
import { useEventsDndSensors } from './events/useEventsDndSensors'
import {
  buildBulkUndo,
  bulkPreviousValues,
  useEventMutations,
  type BulkUpdatePatch,
} from './events/useEventMutations'
import { useEventRowActions } from './events/useEventRowActions'
import { useEventsPageData } from './events/useEventsPageData'
import { useEventRowMetrics } from './events/useEventRowMetrics'
import {
  filterEventsByColumns,
  resolveFieldValue,
  resolveMetaValue,
  useEventsFiltering,
} from './events/useEventsFiltering'
import { tabDefaultStatuses, useEventsQuery } from './events/useEventsQuery'
import { useEventsRouteState } from './events/useEventsRouteState'
import { useEventsSelection } from './events/useEventsSelection'
import { useEventRowSignals, useEventsSignals } from './events/useEventsSignals'
import { useEventsHealth } from './events/useEventsHealth'
import { useEventsTableOverflow } from './events/useEventsTableOverflow'
import { useEventsTableVirtualization } from './events/useEventsTableVirtualization'
import { useCreatedEventsHighlight } from './events/useCreatedEventsHighlight'
import { useEventsViewState } from './events/useEventsViewState'
import { useSavedViews } from './events/useSavedViews'
import { unappliedChartFilters } from './events/utils'
import { useCanWriteProject } from '@/lib/permissions'
import { ReadOnlyNotice } from '@/components/states'
import { usersKey } from '@/lib/queryKeys'
import { currentOrgSlug, projectPath } from '@/lib/navigation'
import { countOf } from '@/lib/plural'

interface EventsPageProps {
  /** Lock the page to a single event type (by name), decoupling it from the
   *  `:tab` route segment. Set when embedding the table in another surface. */
  lockType?: string
  /** Embedded mode: hide the page-level header + aggregate chart and drop the
   *  full-height min-height so the table fits inside a host container/tab. */
  embedded?: boolean
}

/**
 * `/events/:tab/:eventId` is a link to the editor. The redirect happens here,
 * before the list mounts: rendered inside the page it ran after every list,
 * tag, count, signal and metrics query had already been sent, all thrown away
 * by the navigation.
 *
 * A viewer cannot edit, so the same link takes them to the event's read view,
 * the monitoring detail page, instead of an editable-looking form.
 */
export default function EventsPage(props: EventsPageProps = {}) {
  const { slug, tab, eventId } = useParams<{ slug: string; tab?: string; eventId?: string }>()
  const { search } = useLocation()
  const canWrite = useCanWriteProject()
  if (slug && eventId && !canWrite) {
    return (
      <Navigate
        to={`${getMonitoringPath(slug, { scope_type: 'event', scope_ref: eventId })}${search}`}
        replace
      />
    )
  }
  if (slug && eventId) {
    const activeTab = props.lockType || tab || 'all'
    // Carry the query string through. `?branch=` is the one that matters: the
    // branch provider reads it only when it mounts, so dropping it here turns a
    // shared branch-diff link into a main-plan edit — which then renders a
    // normal form and 404s at Save, because the read is lenient and the write
    // is not. useEventsRouteState.openEvent already preserves them on the way in.
    return <Navigate to={projectPath(currentOrgSlug(), slug, `/events/${activeTab}/${eventId}/edit${search}`)} replace />
  }
  return <EventsListPage {...props} />
}

/** How a finished bulk update reads in its toast. */
function bulkUpdateSummary(
  count: number,
  patch: BulkUpdatePatch,
  ownerName: (id: string) => string,
): string {
  const events = countOf(count, 'event', 'events')
  if (patch.status) return `Set ${events} to ${EVENT_STATUS_LABELS[patch.status]}`
  if (patch.reviewed) return `Marked ${events} reviewed`
  if (patch.owner_id === null) return `Unassigned ${events}`
  if (patch.owner_id) return `Assigned ${events} to ${ownerName(patch.owner_id)}`
  return `Updated ${events}`
}

function EventsListPage({ lockType, embedded = false }: EventsPageProps) {
  const {
    activeTab,
    openEvent,
    openNewEvent,
    showForm,
    slug,
  } = useEventsRouteState(lockType)
  const branchId = useActiveBranchId()
  // Viewers read the plan; every create, bulk, reorder and edit affordance is
  // an editor's, and each used to end in a 403 toast.
  const canWrite = useCanWriteProject()
  const { search: locationSearch } = useLocation()
  const usersQuery = useQuery({ queryKey: usersKey(), queryFn: () => usersApi.list() })
  const usersById = useMemo(
    () =>
      new Map(
        (usersQuery.data ?? []).map((u) => [u.id, { name: u.name, email: u.email }]),
      ),
    [usersQuery.data],
  )
  // Same shape as showForm above: the toolbar entry flips a flag and the
  // render below redirects, so both authoring surfaces are reached the one way.
  const [showBulk, setShowBulk] = useState(false)
  const [expandedCell, setExpandedCell] = useState<string | null>(null)
  const {
    hiddenColumns: storedHiddenColumns,
    toggleColumn: toggleStoredColumn,
    updateColumns,
    colMenuOpen,
    setColMenuOpen,
  } = useColumnVisibility()
  const { confirm, dialog } = useConfirm()
  const {
    savedViews,
    savedViewName,
    setSavedViewName,
    activeSavedViewName,
    saveCurrentView,
    applySavedView,
    deleteSavedView,
  } = useSavedViews({ slug, activeTab, confirm })
  const {
    eventTypes,
    eventTypesLoaded,
    metaFields,
    allTags,
    inReviewCount,
    inReviewCountPending,
    dataError,
    refetchPageData,
  } = useEventsPageData({ slug, branchId })

  const {
    search,
    setSearch,
    filterStatuses,
    setFilterStatuses,
    filterTag,
    setFilterTag,
    filterSilentDays,
    setFilterSilentDays,
    filterReviewed,
    setFilterReviewed,
    filterOpenQuestions,
    setFilterOpenQuestions,
    filterProperty,
    setFilterProperty,
    sort,
    setSort,
    fieldFilters,
    updateFieldFilter,
    metaFilters,
    updateMetaFilter,
    debouncedSearch,
    debouncedFieldFilters,
    debouncedMetaFilters,
    isFilterPending,
    filterEtId,
    queryStatuses,
    serverFilters,
    isUnknownTab,
    eventsQuery,
    reportFirstPageInView,
    rawEvents,
    total,
    fetchAllMatching,
  } = useEventsQuery({ slug, activeTab, eventTypes, eventTypesLoaded, branchId })

  const { projectTotalSignal, eventTypeSignals, signalsPending } = useEventsSignals({ slug })

  const activeEt = useMemo(
    () => eventTypes.find(e => e.name === activeTab) ?? null,
    [activeTab, eventTypes],
  )

  const {
    fieldColumns,
    allFieldDefs,
    eventTypesById,
    fieldEnumOptions,
    metaValuesByEvent,
    getFieldValueRow,
    getFieldValue,
    events,
  } = useEventsFiltering({
    rawEvents,
    eventTypes,
    metaFields,
    activeEt,
    debouncedFieldFilters,
    debouncedMetaFilters,
  })

  // On the All and queue tabs, a field only some types define starts hidden:
  // it was a column of dashes for every other type. The Columns menu
  // still lists it, and ticking it there is remembered like any other choice.
  // A column with an active filter (an `f.<name>` from a link or saved view)
  // stays out of that default: hiding its header, where the filter is shown
  // and edited, left the rows narrowed by something the reader could not see.
  const typeSpecificFields = useMemo(() => {
    if (activeEt) return new Set<string>()
    const keys = typeSpecificFieldKeys(eventTypes, fieldColumns)
    for (const column of fieldColumns) {
      if ((fieldFilters[column.name] ?? '') !== '') keys.delete(`f:${column.id}`)
    }
    return keys
  }, [activeEt, eventTypes, fieldColumns, fieldFilters])
  const hiddenColumns = useMemo(
    () => withDefaultHidden(storedHiddenColumns, typeSpecificFields),
    [storedHiddenColumns, typeSpecificFields],
  )
  const toggleColumn = useCallback(
    (key: string) => {
      if (!typeSpecificFields.has(key)) {
        toggleStoredColumn(key)
        return
      }
      if (hiddenColumns.has(key)) updateColumns([shownColumnKey(key)], [key])
      else updateColumns([], [shownColumnKey(key)])
    },
    [hiddenColumns, toggleStoredColumn, typeSpecificFields, updateColumns],
  )

  const {
    activeTabLabel,
    activeTabSignal,
    clearAllFilters,
    colCount: baseColCount,
    hasActiveFilters,
    hideDelta,
    hideLastSeen,
    hideMonitor,
    hideOwner,
    hideReviewed,
    hideStatus,
    hideTags,
    isTabChartOpen,
    setIsTabChartOpen,
    visibleFieldColumns,
    visibleMetaFields,
  } = useEventsViewState({
    slug: slug ?? '',
    activeTab,
    activeEt,
    eventTypeSignals,
    fieldColumns,
    fieldFilters,
    filterStatuses,
    filterSilentDays,
    filterReviewed,
    filterOpenQuestions,
    filterProperty,
    filterTag,
    hiddenColumns,
    metaFields,
    metaFilters,
    projectTotalSignal,
  })

  const {
    selectedEventIds,
    selectedCount,
    selectedVisibleEventIds,
    allVisibleSelected,
    someVisibleSelected,
    selectedSet,
    visibleEventIds,
    toggleEventSelected,
    toggleAllVisibleSelected,
    selectAll,
    selectMany,
    clearSelection,
  } = useEventsSelection({
    events,
    // A selection belongs to the result set it was made in: a new tab, branch
    // or server filter drops it. Sort order is not part of it: it
    // reorders the same set, so switching sort keeps the selection.
    scopeKey: JSON.stringify([activeTab, branchId, { ...serverFilters, order_by: undefined }]),
  })

  const mutations = useEventMutations({
    slug,
    branchId,
    onBulkDeleteSuccess: clearSelection,
    onBulkUpdateSuccess: clearSelection,
  })
  const { bulkDeleteMut, bulkUpdateMut, bulkUndoMut } = mutations

  // Drag-reorder renumbers the catalog order of the rows it is sent, so it is
  // only offered while the rows ARE in catalog order. Under "Busiest first" one
  // small drag rewrote the manual order of every loaded row into volume order,
  // with nothing visible changing. Filters and search keep catalog
  // order, so a drag among the rows they leave still means what it shows.
  // The same goes for "Least healthy first" (F15, #268).
  const canReorder = canWrite && sort === 'catalog'

  // Health scores exist for the main plan's non-archived events only: a branch
  // gets neither the column nor the sort, and the Archived tab no column of
  // dashes. The Columns menu governs it everywhere else.
  const healthAvailable = !branchId
  const hideHealth =
    !healthAvailable || activeTab === 'archived' || hiddenColumns.has(HEALTH_COL_KEY)
  const colCount = baseColCount + (hideHealth ? 0 : 1)

  // `useEventsFiltering` returns the exact `rawEvents` reference when no
  // client-side field/meta filter is active and a fresh filtered array
  // otherwise, so identity tells whether the server `total` still counts the
  // rows the table shows.
  const isClientFiltered = events !== rawEvents

  const dndSensors = useEventsDndSensors()

  // Lives on the page, not in EventsTable: the off-screen column count it
  // measures is reported by the toolbar's Columns chip, which renders above the
  // table.
  const { tableRef, offscreenColumnCount } = useEventsTableOverflow()

  const {
    tableScrollRef,
    virtualize,
    visibleRange,
    virtualItems,
    totalVirtualSize,
    measureRow,
    scrollToIndex,
    isScanningForMatches,
  } = useEventsTableVirtualization({
    events,
    total,
    eventsQuery,
    isClientFiltered,
    reportFirstPageInView,
  })

  // What the form the reader just left created: scrolled to and marked, so a
  // new row is not lost in a long catalog.
  const createdIds = useCreatedEventsHighlight({
    slug,
    events,
    virtualize,
    scrollToIndex,
    scrollRef: tableScrollRef,
  })

  const onToggleExpandedCell = useCallback((cellKey: string | null) => {
    setExpandedCell(prev => (prev === cellKey ? null : cellKey))
  }, [])

  const { handleDragEnd, onRowAction } = useEventRowActions({
    openEvent,
    mutations,
    visibleEventIds,
    selectedSet,
    canReorder,
  })

  const eventSignals = useEventRowSignals({ slug, events, virtualItems })
  const healthByEvent = useEventsHealth({ slug, events: rawEvents, enabled: !hideHealth })

  // Schema drift, once per event type. `drift_count` on a row is its
  // type's count, so the loaded rows name every drifting type they cover.
  const typeDrifts = useMemo((): EventTypeDrift[] => {
    const byType = new Map<string, EventTypeDrift>()
    for (const ev of rawEvents) {
      if (!(ev.drift_count > 0)) continue
      const existing = byType.get(ev.event_type_id)
      const coach = ev.name === SCENARIO_SEEDED.schemaDriftEventName
      if (existing) {
        existing.count = Math.max(existing.count, ev.drift_count)
        existing.coach ||= coach
        continue
      }
      const type = eventTypes.find(et => et.id === ev.event_type_id)
      byType.set(ev.event_type_id, {
        eventTypeId: ev.event_type_id,
        label: type?.display_name ?? type?.name ?? 'Unknown type',
        count: ev.drift_count,
        coach,
      })
    }
    return [...byType.values()]
  }, [eventTypes, rawEvents])

  const shownTypeDrifts = useMemo(
    () => (activeEt ? typeDrifts.filter(d => d.eventTypeId === activeEt.id) : typeDrifts),
    [activeEt, typeDrifts],
  )

  const handleBulkDelete = useEventsBulkDelete({
    slug,
    branchId,
    selectedEventIds,
    selectedVisibleEventIds,
    bulkDeleteMut,
    confirm,
  })

  const ownerName = useCallback(
    (id: string) => {
      const user = usersById.get(id)
      return user ? user.name ?? user.email : 'the chosen owner'
    },
    [usersById],
  )

  // Bulk actions operate on the FULL selection (`selectedEventIds`), not just
  // the loaded rows, so "select all N matching" can sweep events that have not
  // scrolled into view yet. Each one asks first when the sweep reaches rows off
  // screen, is large, or archives, and says what it did when it lands,
  // with an Undo where every row's previous value is known.
  const runBulkUpdate = useCallback(async (patch: BulkUpdatePatch, actionLabel: string) => {
    const eventIds = selectedEventIds
    if (!eventIds.length) return
    // What each row holds in the list the table renders, taken before anything
    // changes, for the Undo.
    const undo = buildBulkUndo(eventIds, patch, bulkPreviousValues(rawEvents, eventIds))
    const confirmation = bulkUpdateConfirmation({
      selectedCount: eventIds.length,
      selectedVisibleCount: selectedVisibleEventIds.length,
      actionLabel,
      archives: patch.status === 'archived',
      deprecates: patch.status === 'deprecated',
    })
    // Retiring events lists what depends on them under the question (#257):
    // a warning, the Apply stays armed. Archive is sent as a deprecation — the
    // events stay in the plan, out of the active set.
    const retires = patch.status === 'archived' || patch.status === 'deprecated'
    if (confirmation && !(await confirm(
      retires && slug
        ? {
            ...confirmation,
            message: (
              <ConfirmImpactMessage
                message={confirmation.message}
                slug={slug}
                branchId={branchId}
                changes={eventIds.map(id => ({ kind: 'event' as const, id, change: 'deprecate' as const }))}
              />
            ),
          }
        : confirmation,
    ))) return
    bulkUpdateMut.mutate({ eventIds, ...patch }, {
      onSuccess: () => {
        toast.success(bulkUpdateSummary(eventIds.length, patch, ownerName), {
          // One mutation for every group: it leaves the selection the operator
          // has made since alone and refreshes the lists once. A failure
          // raises the global error toast.
          action: undo ? { label: 'Undo', onClick: () => bulkUndoMut.mutate(undo) } : undefined,
        })
      },
    })
  }, [
    branchId,
    bulkUndoMut,
    bulkUpdateMut,
    confirm,
    ownerName,
    rawEvents,
    selectedEventIds,
    selectedVisibleEventIds,
    slug,
  ])

  const handleBulkSetStatus = useCallback((status: EventStatus) => {
    void runBulkUpdate({ status }, `Set status to ${EVENT_STATUS_LABELS[status]}`)
  }, [runBulkUpdate])

  const handleBulkMarkReviewed = useCallback(() => {
    void runBulkUpdate({ reviewed: true }, 'Mark as verified')
  }, [runBulkUpdate])

  // `null` is a value here, not an absence: the bulk patch keys off which fields
  // were SENT, so `owner_id: null` clears the owner across the selection and an
  // omitted `owner_id` leaves it alone.
  const handleBulkAssignOwner = useCallback((userId: string | null) => {
    void runBulkUpdate(
      { owner_id: userId },
      userId === null ? 'Unassign owner' : `Assign to ${ownerName(userId)}`,
    )
  }, [ownerName, runBulkUpdate])

  // The per-column field/meta filters are client-side; the server sweeps below
  // know only the server filters. Everything that acts on "the current view"
  // beyond the loaded rows — select-all, CSV — re-applies them through this one
  // function, so the view the table shows is the view they act on. The full
  // column sets, not the visible ones: hiding a column in the picker does not
  // clear its filter, and the table still narrows by it.
  const applyColumnFilters = useCallback(
    (rows: EventListItem[]) =>
      filterEventsByColumns(rows, {
        fieldColumns,
        metaFields,
        fieldFilters: debouncedFieldFilters,
        metaFilters: debouncedMetaFilters,
        getFieldValue: (event, col) => resolveFieldValue(event, col, allFieldDefs),
        getMetaValue: resolveMetaValue,
      }),
    [allFieldDefs, debouncedFieldFilters, debouncedMetaFilters, fieldColumns, metaFields],
  )

  // Pull every id matching the current view and select them, so triage can
  // accept or archive an entire prefix/queue in one bulk action. The column
  // filters apply here too: selecting the server's 5,000 under a column filter
  // showing 12 rows, then deleting, deleted 5,000 events.
  const [isSelectingAll, setIsSelectingAll] = useState(false)
  const handleSelectAllMatching = useCallback(async () => {
    setIsSelectingAll(true)
    try {
      const ids = applyColumnFilters(await fetchAllMatching()).map(event => event.id)
      if (ids.length) selectAll(ids)
    } catch (error) {
      // The sweep is a bare awaited request — no query cache, no mutation, so
      // nothing else reports it and the button would just stop spinning with
      // the selection unchanged.
      toast.error(`Could not select all matching events — ${getErrorMessage(error)}`)
    } finally {
      setIsSelectingAll(false)
    }
  }, [applyColumnFilters, fetchAllMatching, selectAll])

  // CSV of the WHOLE filtered view, not just the pages scrolled so far: the
  // catalog runs to thousands of events and the only previous way out of it was
  // a "soon" badge. Server filters + sort come from the same
  // paging helper "select all matching" uses; the per-column field/meta filters
  // are client-side, so they are re-applied to the fetched rows here.
  const [isExporting, setIsExporting] = useState(false)
  // The sweep only knows there is anything to fetch from the loaded `total`,
  // which is 0 during the cold load and — under `placeholderData: prev` — still
  // the *previous* filters' count while a filter change is in flight. Exporting
  // in either window wrote a header-only file that reads exactly like "nothing
  // matched", so the action waits for a page that belongs to these filters.
  const canExportCsv = eventsQuery.isSuccess && !eventsQuery.isPlaceholderData
  const handleExportCsv = useCallback(() => {
    if (!slug || isExporting || !canExportCsv) return
    void (async () => {
      setIsExporting(true)
      try {
        const rows = applyColumnFilters(await fetchAllMatching())
        // Never hand back a header-only file: an empty CSV is indistinguishable
        // from a broken export, so say which of the two this is.
        if (rows.length === 0) {
          toast.info('No events match the current filters — nothing to export.')
          return
        }
        const columns = buildEventsCsvColumns({
          activeTypeName: activeEt?.name ?? null,
          eventTypesById,
          usersById,
          fieldDefsById: allFieldDefs,
          fieldColumns: visibleFieldColumns,
          metaFields: visibleMetaFields,
          hideStatus,
          hideReviewed,
          hideTags,
          hideLastSeen,
          hideOwner,
        })
        downloadCsv(eventsCsvFilename(slug, activeTab), toCsv(columns, rows))
      } catch (error) {
        // Same hole as the id sweep: an ApiError here reaches no query cache and
        // no error boundary, so without this the item flips back to "Export CSV"
        // with no file and no message — read as "nothing matched".
        toast.error(`Could not export CSV — ${getErrorMessage(error)}`)
      } finally {
        setIsExporting(false)
      }
    })()
  }, [
    activeEt,
    activeTab,
    allFieldDefs,
    applyColumnFilters,
    canExportCsv,
    eventTypesById,
    fetchAllMatching,
    hideLastSeen,
    hideOwner,
    hideReviewed,
    hideStatus,
    hideTags,
    isExporting,
    slug,
    usersById,
    visibleFieldColumns,
    visibleMetaFields,
  ])

  const { eventWindowMetricsByEvent, eventRowSignals, rowMetricsSettled } = useEventRowMetrics({
    slug,
    events,
    eventSignals,
    // Sparkline metrics are fetched only for the rows on screen: filling the
    // column for every accumulated row was the events page's dominant cost.
    virtualItems,
  })

  const retryLoad = useCallback(() => {
    void Promise.all([
      eventsQuery.refetch(),
      ...refetchPageData(),
    ])
  }, [eventsQuery, refetchPageData])

  const blockingError = eventsQuery.error ?? dataError

  // A project with no events yet has nothing to filter, sort, column, or chart,
  // so the page is a first-run empty state instead: no toolbar, no stat strip,
  // no volume card and no table frame. Guard on the *unfiltered* result:
  // an active filter or search that merely matches nothing on a populated
  // project must keep the full toolbar so the user can still clear it.
  //
  // Also gate on the events query having SETTLED: `total` is 0 while it loads, so
  // without this a populated project would flash the minimal toolbar on every
  // cold load before snapping to the full one. `total` is the
  // branch-aware server count (not the main-branch project summary), so this stays
  // correct on working branches; the one accepted edge is an archived-only "all"
  // tab reading 0, recoverable via the tab bar.
  //
  // Only on the "all" tab: an empty Review queue, Archived tab or type tab is an
  // empty slice of a populated project, and collapsing there took away saved
  // views, columns, sort, export and "Add many events".
  // Only for an unfiltered page that has arrived: clearing a search that
  // matched nothing keeps that empty page on screen, through the debounce and
  // the refetch, and reading it as an empty project swapped the toolbar out and
  // took focus from the search box (the next "c" then opened a new event).
  const hasNoEvents =
    activeTab === 'all' &&
    eventsQuery.isSuccess &&
    !eventsQuery.isPlaceholderData &&
    total === 0 &&
    !hasActiveFilters &&
    !search &&
    !debouncedSearch

  // Editing is a full page, not an inline Sheet. The "New event" action toggles
  // showForm, which is redirected here to the dedicated new route (row edits
  // navigate to /events/:tab/:eventId, which EventsPage redirects before this
  // component mounts).
  // Always keep the :tab segment so the new/edit routes (/events/:tab/new and
  // /events/:tab/:eventId/edit) match — '/events/new' would otherwise resolve to
  // the /events/:tab list route with tab='new'. tab='all' is handled everywhere.
  // The query string travels too, for the `?branch=` reason given above.
  const eventsBase = projectPath(currentOrgSlug(), slug, `/events/${activeTab}`)
  if (slug && showForm) {
    return <Navigate to={`${eventsBase}/new${locationSearch}`} replace />
  }
  if (slug && showBulk) {
    return <Navigate to={`${eventsBase}/bulk${locationSearch}`} replace />
  }

  // A type tab that names no type: a stale bookmark to a deleted or renamed
  // type used to list every event under that type's heading.
  if (isUnknownTab && !blockingError) {
    return (
      <EmptyState
        icon={FileQuestion}
        title="Unknown event type"
        description={`This project has no event type named "${activeTab}". It may have been renamed or deleted.`}
        action={
          embedded ? undefined : (
            <Button asChild size="sm" variant="outline">
              <Link to={projectPath(currentOrgSlug(), slug, `/events${locationSearch}`)}>Show all events</Link>
            </Button>
          )
        }
      />
    )
  }

  return (
    <div
      // The table's scroller measures itself against this root.
      data-events-page=""
      className={embedded ? 'flex min-h-[420px] flex-col' : 'flex min-h-[calc(100vh-7rem)] flex-col'}
      // Room for the floating bulk bar, so it never sits over the table's last
      // rows and footer while a selection is open.
      style={canWrite && selectedCount > 0 ? { paddingBottom: '7rem' } : undefined}
    >
      {dialog}

      {!embedded && (
        <EventsHeader
          total={total}
          totalPending={eventsQuery.isPending}
          columnFilter={
            isClientFiltered ? { matching: events.length, checked: rawEvents.length } : null
          }
          inReviewCount={inReviewCount}
          inReviewPending={inReviewCountPending}
          projectTotalSignal={projectTotalSignal}
          eventTypeSignals={eventTypeSignals}
          signalsPending={signalsPending}
          activeType={activeEt}
          activeTab={activeTab}
          slug={slug}
          typeDrifts={shownTypeDrifts}
          hideStats={hasNoEvents}
        />
      )}
      {/* The embedded table (an event type's detail view) has no header, and
          the rows no longer carry the badge, so the type's drift
          shows here or nowhere. */}
      {embedded && slug && shownTypeDrifts.length > 0 && (
        <div className="mb-3 flex items-center">
          <EventTypeDriftBadges slug={slug} typeDrifts={shownTypeDrifts} namesType={!!activeEt} />
        </div>
      )}

      {blockingError && (
        <ErrorState
          title="Could not load events"
          description="The event catalog did not load. Check your connection and try again."
          error={blockingError}
          onRetry={retryLoad}
        />
      )}

      {!blockingError && (
        <>
          {!canWrite && <ReadOnlyNotice className="mb-3" />}
          {hasNoEvents ? (
            <EventsFirstRun
              slug={slug}
              canWrite={canWrite}
              noEventTypes={eventTypesLoaded && eventTypes.length === 0}
              onNewEvent={openNewEvent}
              onBulkNew={() => setShowBulk(true)}
            />
          ) : (
            <EventsToolbar
              search={search}
              onSearchChange={setSearch}
              isFilterPending={isFilterPending}
              filterStatuses={filterStatuses}
              tabDefaultStatuses={tabDefaultStatuses(activeTab)}
              onFilterStatusesChange={setFilterStatuses}
              filterSilentDays={filterSilentDays}
              onFilterSilentDaysChange={setFilterSilentDays}
              filterReviewed={filterReviewed}
              onFilterReviewedChange={setFilterReviewed}
              filterOpenQuestions={filterOpenQuestions}
              onFilterOpenQuestionsChange={setFilterOpenQuestions}
              filterProperty={filterProperty}
              onClearFilterProperty={() => setFilterProperty('')}
              sortOrder={sort}
              onSortOrderChange={setSort}
              healthAvailable={healthAvailable && activeTab !== 'archived'}
              hasActiveFilters={hasActiveFilters}
              onClearFilters={clearAllFilters}
              savedViews={savedViews}
              activeSavedViewName={activeSavedViewName}
              savedViewName={savedViewName}
              onSavedViewNameChange={setSavedViewName}
              onSaveCurrentView={() => { void saveCurrentView() }}
              showSavedViews={!embedded}
              onApplySavedView={applySavedView}
              onDeleteSavedView={name => void deleteSavedView(name)}
              columnsMenuOpen={colMenuOpen}
              onColumnsMenuOpenChange={setColMenuOpen}
              hiddenColumns={hiddenColumns}
              hideLastSeen={hideLastSeen}
              reviewedPinned={activeTab === 'review'}
              offscreenColumnCount={offscreenColumnCount}
              fieldColumns={fieldColumns}
              metaFields={metaFields}
              onToggleColumn={toggleColumn}
              onExportCsv={handleExportCsv}
              canExport={canExportCsv}
              isExporting={isExporting}
              onNewEvent={canWrite ? openNewEvent : undefined}
              onBulkNew={canWrite ? () => setShowBulk(true) : undefined}
            />
          )}

          <BulkActionBar
            selectedCount={canWrite ? selectedCount : 0}
            selectedVisibleCount={selectedVisibleEventIds.length}
            // Under a column filter the server total is not the match count,
            // so the button offers "all matching" without a number.
            matchingTotal={isClientFiltered ? null : total}
            onSelectAllMatching={() => { void handleSelectAllMatching() }}
            isSelectingAll={isSelectingAll}
            isDeleting={bulkDeleteMut.isPending}
            isUpdating={bulkUpdateMut.isPending || bulkUndoMut.isPending}
            onSetStatus={handleBulkSetStatus}
            onMarkReviewed={handleBulkMarkReviewed}
            onAssignOwner={handleBulkAssignOwner}
            owners={usersQuery.data ?? []}
            onDelete={() => { void handleBulkDelete() }}
            onClear={clearSelection}
          />

          {slug && !embedded && !hasNoEvents && (
            <TabMetricsCard
              slug={slug}
              activeEt={activeEt}
              activeTabLabel={activeTabLabel}
              activeTabSignal={activeTabSignal}
              isOpen={isTabChartOpen}
              onOpenChange={setIsTabChartOpen}
              branchId={branchId}
              filters={{
                filterEtId,
                debouncedSearch,
                queryStatuses,
                filterTag,
              }}
              unappliedFilters={unappliedChartFilters({
                filterSilentDays,
                filterReviewed,
                filterOpenQuestions,
                filterProperty,
                hasColumnFilters:
                  Object.values(fieldFilters).some(Boolean) || Object.values(metaFilters).some(Boolean),
              })}
            />
          )}

          {!hasNoEvents && (
          <div
            className="rounded-card border overflow-hidden border-border"
          >
            <EventsTable
              tableScrollRef={tableScrollRef}
              tableRef={tableRef}
              isTabChartOpen={isTabChartOpen}
              dndSensors={dndSensors}
              handleDragEnd={handleDragEnd}
              visibleEventIds={visibleEventIds}
              canReorder={canReorder}
              allVisibleSelected={allVisibleSelected}
              someVisibleSelected={someVisibleSelected}
              toggleAllVisibleSelected={toggleAllVisibleSelected}
              activeEt={activeEt}
              hideStatus={hideStatus}
              hideReviewed={hideReviewed}
              hideMonitor={hideMonitor}
              hideHealth={hideHealth}
              healthByEvent={healthByEvent}
              hideOwner={hideOwner}
              hideDelta={hideDelta}
              usersById={usersById}
              hideTags={hideTags}
              hideLastSeen={hideLastSeen}
              allTags={allTags}
              filterTag={filterTag}
              setFilterTag={setFilterTag}
              visibleFieldColumns={visibleFieldColumns}
              fieldFilters={fieldFilters}
              updateFieldFilter={updateFieldFilter}
              fieldEnumOptions={fieldEnumOptions}
              visibleMetaFields={visibleMetaFields}
              metaFilters={metaFilters}
              updateMetaFilter={updateMetaFilter}
              events={events}
              total={total}
              loadedCount={rawEvents.length}
              isClientFiltered={isClientFiltered}
              isScanningForMatches={isScanningForMatches}
              isLoading={eventsQuery.isPending}
              virtualize={virtualize}
              virtualItems={virtualItems}
              visibleRange={visibleRange}
              totalVirtualSize={totalVirtualSize}
              measureRow={measureRow}
              colCount={colCount}
              expandedCell={expandedCell}
              eventWindowMetricsByEvent={eventWindowMetricsByEvent}
              eventRowSignals={eventRowSignals}
              metaValuesByEvent={metaValuesByEvent}
              eventTypesById={eventTypesById}
              slug={slug!}
              selectedSet={selectedSet}
              getFieldValue={getFieldValue}
              getFieldValueRow={getFieldValueRow}
              toggleEventSelected={toggleEventSelected}
              selectMany={selectMany}
              onToggleExpandedCell={onToggleExpandedCell}
              onRowAction={onRowAction}
              // Lets a zero-row table explain which query came back empty
              // instead of always offering "create your first event".
              emptyContext={{
                activeTab,
                hasActiveFilters,
                search,
                typeLabel: activeEt?.display_name,
              }}
              onNewEvent={canWrite ? openNewEvent : undefined}
              onClearFilters={clearAllFilters}
              sortOrder={sort}
              onSortOrderChange={setSort}
              rowMetricsSettled={rowMetricsSettled}
              createdIds={createdIds}
            />
          </div>
          )}
        </>
      )}
    </div>
  )
}
