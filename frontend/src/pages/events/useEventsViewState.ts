import { useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import type {
  EventType,
  FieldDefinition,
  MetaFieldDefinition,
  MonitoringSignal,
} from '@/types'
import type { EventStatus } from '@/lib/eventStatus'
import { orgStorageKey } from '@/lib/activeOrg'

/** Per-project key holding which tabs have their volume chart open. */
export function chartOpenStorageKey(slug: string): string {
  return orgStorageKey(`tripl.eventsChartOpen.${slug}`)
}

function readOpenCharts(slug: string): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(chartOpenStorageKey(slug))
    if (raw === null) return {}
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    return Object.fromEntries(
      Object.entries(parsed).filter((entry): entry is [string, boolean] => typeof entry[1] === 'boolean'),
    )
  } catch {
    return {}
  }
}

export function useEventsViewState({
  slug,
  activeTab,
  activeEt,
  eventTypeSignals,
  fieldColumns,
  fieldFilters,
  filterStatuses,
  filterSilentDays,
  filterReviewed,
  filterOpenQuestions,
  filterProperty = '',
  filterTag,
  hiddenColumns,
  metaFields,
  metaFilters,
  projectTotalSignal,
}: {
  slug: string
  activeTab: string
  activeEt: EventType | null
  eventTypeSignals: Map<string, MonitoringSignal>
  fieldColumns: FieldDefinition[]
  fieldFilters: Record<string, string>
  filterStatuses: EventStatus[]
  filterSilentDays: number | undefined
  filterReviewed: boolean | undefined
  filterOpenQuestions: boolean | undefined
  /** `?property=` (F23): a filter like the rest, cleared with them. */
  filterProperty?: string
  filterTag: string
  hiddenColumns: Set<string>
  metaFields: MetaFieldDefinition[]
  metaFilters: Record<string, string>
  projectTotalSignal: MonitoringSignal | null
}) {
  const [, setSearchParams] = useSearchParams()
  // Keyed by the project it was read for, so switching projects re-reads the
  // stored choice instead of carrying the previous project's toggles over.
  const [openChartsState, setOpenChartsState] = useState(() => ({
    slug,
    charts: readOpenCharts(slug),
  }))
  const openCharts =
    openChartsState.slug === slug ? openChartsState.charts : readOpenCharts(slug)

  // The volume chart starts collapsed: open, its 260px pushed the table below
  // the fold on every visit, for one unannotated line (it replaces
  // the earlier open default). A per-tab toggle is remembered per project, so the
  // reader who opens it keeps it open across reloads.
  const isTabChartOpen = openCharts[activeTab] ?? false
  const setIsTabChartOpen = useCallback((open: boolean) => {
    setOpenChartsState(prev => {
      const base = prev.slug === slug ? prev.charts : readOpenCharts(slug)
      const charts = { ...base, [activeTab]: open }
      try {
        localStorage.setItem(chartOpenStorageKey(slug), JSON.stringify(charts))
      } catch { /* storage full or blocked: the choice lasts the session */ }
      return { slug, charts }
    })
  }, [activeTab, slug])
  const activeTabSignal = useMemo(() => {
    if (activeTab === 'all') return projectTotalSignal
    if (!activeEt) return null
    return eventTypeSignals.get(activeEt.id) ?? null
  }, [activeEt, activeTab, eventTypeSignals, projectTotalSignal])
  const activeTabLabel = useMemo(() => {
    if (activeEt) return activeEt.display_name
    // Sentence case, like every other label.
    if (activeTab === 'review') return 'Review queue'
    if (activeTab === 'archived') return 'Archived events'
    return 'All events'
  }, [activeEt, activeTab])

  const visibleFieldColumns = useMemo(
    () => fieldColumns.filter(f => !hiddenColumns.has(`f:${f.id}`)),
    [fieldColumns, hiddenColumns],
  )
  const visibleMetaFields = useMemo(
    () => metaFields.filter(mf => !hiddenColumns.has(`m:${mf.id}`)),
    [metaFields, hiddenColumns],
  )
  const hideTags = hiddenColumns.has('tags')
  const hideLastSeen = hiddenColumns.has('last_seen')
  const hideStatus = hiddenColumns.has('status')
  // The review tab is the one screen where the reviewed flag is the point, and
  // it is hidden by default — so bulk "Mark reviewed" there changed nothing the
  // operator could see. Force the column on for that tab; the picker still
  // governs every other tab.
  const hideReviewed = hiddenColumns.has('reviewed') && activeTab !== 'review'
  const hideMonitor = hiddenColumns.has('monitor')
  const hideOwner = hiddenColumns.has('owner')
  const hideDelta = hiddenColumns.has('delta')
  const colCount =
    1 +
    1 +
    1 +
    (activeEt ? 0 : 1) +
    (hideStatus ? 0 : 1) +
    (hideReviewed ? 0 : 1) +
    (hideMonitor ? 0 : 1) +
    (hideDelta ? 0 : 1) +
    1 +
    (hideTags ? 0 : 1) +
    (hideLastSeen ? 0 : 1) +
    (hideOwner ? 0 : 1) +
    visibleFieldColumns.length +
    visibleMetaFields.length

  const hasActiveFilters = filterStatuses.length > 0 || filterTag !== '' || filterSilentDays !== undefined ||
    filterReviewed !== undefined ||
    filterOpenQuestions !== undefined ||
    filterProperty !== '' ||
    Object.values(fieldFilters).some(v => v !== '') ||
    Object.values(metaFilters).some(v => v !== '')

  // Clears the search too, in the same URL write: to the reader a search that
  // matches nothing is one more filter, and "Clear filters" left it in place.
  // One updater, because two back-to-back setSearchParams calls both
  // start from the same params and the second undoes the first.
  const clearAllFilters = useCallback(() => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      next.delete('q')
      next.delete('status')
      next.delete('tag')
      next.delete('silent_days')
      next.delete('reviewed')
      next.delete('questions')
      next.delete('property')
      Array.from(next.keys()).filter(k => k.startsWith('f.') || k.startsWith('m.')).forEach(k => next.delete(k))
      return next
    }, { replace: true })
  }, [setSearchParams])

  return {
    activeTabLabel,
    activeTabSignal,
    clearAllFilters,
    colCount,
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
  }
}
