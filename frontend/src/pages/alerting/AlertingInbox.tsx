import { memo, useCallback, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { BellRing } from 'lucide-react'

import { MAX_INBOX_NOTE_LENGTH, alertingApi } from '@/api/alerting'
import { Chip } from '@/components/primitives/chip'
import { EmptyState } from '@/components/empty-state'
import { Panel } from '@/components/settings/kit'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Textarea } from '@/components/ui/textarea'
import {
  alertInboxStatusLabel,
  alertInboxStatusTone,
  incidentDirectionGlyph,
  incidentMagnitudeLabel,
  incidentMagnitudeTitle,
  incidentReasonLabel,
  incidentWorstDeltaLabel,
  isHandledInboxStatus,
  priorDecisionLabel,
} from '@/lib/alertStatus'
import { formatDateTime, formatRelativeTime } from '@/lib/datetime'
import {
  getScopeMonitoringPath,
  getScopeNavigationTarget,
  type ScopeNavigationTarget,
} from '@/lib/monitoring'
import {
  INBOX_MUTE_CHOICES,
  muteChoiceName,
  muteChoiceUntilIso,
  muteName,
  unmuteName,
} from '@/lib/mutePresets'
import { VIEWER_READ_ONLY_NOTICE, useCanWriteProject } from '@/lib/permissions'
import { ReadOnlyNotice, SectionSkeleton } from '@/components/states'
import { countOf } from '@/lib/plural'
import { cn, getErrorMessage } from '@/lib/utils'
import type {
  AlertInboxAction,
  AlertInboxGroup,
  AlertInboxListResponse,
  AlertInboxStatus,
} from '@/types'

import { noteBudgetLabel } from './constants'
import { InboxFilterBar } from './InboxFilterBar'
import { liveInboxActionError, type InboxActionFailure } from './inboxActionErrors'
import { incidentDeltaBadge, incidentHeadline } from './inboxCardLabels'
import {
  EMPTY_INBOX_FILTERS,
  INBOX_LOOKBACK_DAYS,
  hasActiveInboxFilters,
  type InboxFilterState,
} from './inboxFilters'
import { IncidentDeliveries } from './IncidentDeliveries'
import { IncidentSummary } from './IncidentSummary'
import { OwnersNotify } from './OwnersNotify'
import { useNoteDraft, type NoteDraftStore } from './noteDraftStore'
import { currentOrgSlug, projectPath } from '@/lib/navigation'

/** The status filter, where `''` is "All" — the state with no `status=` param. */
export type InboxStatusFilter = AlertInboxStatus | ''

/**
 * One action request, as the card raises it.
 *
 * The action union is imported rather than redeclared: this file used to own a
 * five-member `InboxAction` copy that the API type then grew past when `note`
 * was added, and a card cannot ask for something the endpoint rejects.
 *
 * `mutedUntil` is resolved from an {@link INBOX_MUTE_CHOICES} entry at click
 * time, so the instant the confirmation names and the instant that is written
 * are the same one.
 *
 * Its three states are NOT interchangeable, and nothing downstream may branch
 * on truthiness:
 *
 *  - `undefined` — this action carries no mute value at all (every action that
 *    is not `mute`).
 *  - a string — mute until that instant.
 *  - `null` — mute with NO end date. The most far-reaching action on the page,
 *    and the one that looks most like "nothing was chosen" at a glance. Every
 *    guard on the path to the request body therefore tests `action === 'mute'`;
 *    a `&& mutedUntil` there silently drops the key from the body or skips the
 *    confirmation for exactly this case.
 */
export interface InboxActionVariables {
  group: AlertInboxGroup
  action: AlertInboxAction
  mutedUntil?: string | null
}

/** How long the list reaches back, server-side (INBOX_LOOKBACK_DAYS).
 *
 *  Derived rather than spelled: the date filter's own bound reads the same
 *  constant, and the day those two disagreed would be the day the page promised
 *  a window its controls could not reach. */
const LOOKBACK_LABEL = `last ${INBOX_LOOKBACK_DAYS} days`

/**
 * What the list actually covers, which is no longer only the window.
 *
 * The window is a window on DELIVERIES, and silencing an incident is the act of
 * stopping its deliveries — so a still-silenced incident used to fall out of the
 * list 30 days after somebody muted it, taking its own Unmute control with it.
 * The server now holds those past the window, which makes a bare "last 30 days"
 * a promise the page no longer keeps: the count beside it includes them.
 *
 * Said on the subtitle rather than only in the docs, because this line IS the
 * page's statement of what it is showing — and an operator reading "last 30
 * days" above a two-month-old muted incident would reasonably conclude the page
 * was broken.
 */
const COVERAGE_LABEL = `${LOOKBACK_LABEL} + still silenced`

/**
 * The same sentence when the server could NOT reach back 30 days.
 *
 * The list is capped at a fixed number of delivery rows as well as by the
 * window, and the cap is applied on delivery recency BEFORE incidents are
 * grouped — so a loud enough project gets a shorter window, with the oldest
 * incidents simply missing. Missing is indistinguishable from handled, which
 * makes {@link COVERAGE_LABEL} an unconditional claim the page cannot always
 * keep.
 *
 * `+ still silenced` survives the swap: the rescue that clause names runs
 * whatever the cap did, so dropping it here would trade one wrong sentence for
 * another. The truncation is stated as a REPLACEMENT for the window clause, not
 * alongside it, because "last 30 days" and "since 3 Aug" in one line are two
 * answers to the question the line exists to answer.
 */
function coverageLabel(windowTruncatedAt: string | null): string {
  if (!windowTruncatedAt) return COVERAGE_LABEL
  return `since ${formatDateTime(windowTruncatedAt)} + still silenced`
}

interface AlertingInboxProps {
  slug: string
  // Optional, not defaulted to an empty list: the empty state below has to tell
  // "not loaded yet" apart from "loaded and empty".
  inbox: AlertInboxListResponse | undefined
  // …and `isError` is the third fact. "No correlated alert groups" asserts the
  // reassuring one, and a failed request is the opposite fact about a queue
  // somebody is deciding whether to look at.
  isLoading: boolean
  isError: boolean
  loadError: unknown
  // The `?incident=` group, fetched by id and pinned, when it is NOT in the
  // pages loaded below. An alert's own deep link used to dead-end: it only
  // pre-expanded a card that was never fetched.
  pinnedGroup: AlertInboxGroup | null
  hasRules: boolean
  statusFilter: InboxStatusFilter
  onStatusFilterChange: (next: InboxStatusFilter) => void
  // Everything narrowing the list past its status. Owned by the page for the
  // same reason `statusFilter` is: it lives in the URL and it is part of the
  // query key, so this component may render it but must not hold it.
  filters: InboxFilterState
  onFiltersChange: (next: InboxFilterState) => void
  // "Show all" as ONE write. Both halves live in the URL, and react-router's
  // `setSearchParams` builds each call from the params of the last render — so
  // calling the status setter and then the filter setter in one click let the
  // second write restore the status the first had just cleared. Optional: when
  // absent, the two setters are called in turn.
  onClearAllFilters?: () => void
  // The empty state's "Show all": every status, filters off. Distinct from
  // Clear when the page opens on a status of its own (AL-14) — clearing back
  // to an empty Open queue would show nothing new. Falls back to Clear.
  onShowAll?: () => void
  // The status the page opens on; the filter bar does not count it as a
  // filter the reader set. '' (All) when absent.
  defaultStatusFilter?: InboxStatusFilter
  onLoadMore: () => void
  hasMore: boolean
  isLoadingMore: boolean
  // Draft notes and expansion both outlive a section switch, so they are owned
  // by the page, not by this conditionally-rendered component. The drafts are
  // a store rather than state, so a keystroke re-renders the one card being
  // typed in instead of the page and every card on it (ALR-29).
  noteDraftStore: NoteDraftStore
  expandedIncidents: ReadonlySet<string>
  toggleIncident: (correlationGroupId: string) => void
  // …and so is the bulk selection, threaded in exactly the way those two are.
  // It lives on the page and not here for a reason this component
  // cannot serve: the page owns the query whose rows these ids index into, so it
  // is the only thing that can prune a selection when a status filter changes or
  // a refetch drops rows. It also owns the floating bulk bar, which is fixed to
  // the viewport rather than to this list.
  //
  // These are always passed as a matched pair with the bar; a card renders its
  // checkbox from the same `canWrite` gate that decides its action row, so a
  // viewer cannot build a selection they would not be allowed to act on.
  selectedIncidents: ReadonlySet<string>
  toggleIncidentSelected: (correlationGroupId: string, selected: boolean) => void
  // Set several at once, for the header "select all shown" box and the
  // shift-click range. Separate from the single toggle rather than
  // folded into it because the page has to add or drop the whole batch in ONE
  // state update: fifty sequential toggles would each re-run the pruning pass
  // below them, and the bulk bar would count up one incident at a time.
  setIncidentsSelected: (correlationGroupIds: readonly string[], selected: boolean) => void
  onAction: (variables: InboxActionVariables) => void
  // The rows an action is in flight for. A single shared `isActionPending`
  // disabled all ~80 buttons on the page, so triage was strictly serial and the
  // row you touched showed nothing at all. A SET, not one id:
  // with one id, acting on B while A was in flight re-enabled A's buttons and
  // pinned any failure of A on B (ALR-28).
  pendingGroupIds: ReadonlySet<string>
  // …and each row's own failure. The error used to render once, below every
  // card, ~3,000px from the row it was about. Each failure carries the card
  // state it failed against, and shows only while the live card still matches
  // it — a colleague's resolve or a bulk action retires it.
  actionErrors: ReadonlyMap<string, InboxActionFailure>
  // Where a reader with no rules is sent. Rules moved off the destination cards
  // into their own section, so this points at Monitors — adding a
  // channel is not what unblocks an empty inbox, adding a rule is.
  onGoToMonitors: () => void
  focusDeliveryId?: string
  focusItemKey?: string
}

/**
 * The Inbox section of the alerting page: correlated incident groups and the
 * actions that close them.
 */
export function AlertingInbox({
  slug,
  inbox,
  isLoading,
  isError,
  loadError,
  pinnedGroup,
  hasRules,
  statusFilter,
  onStatusFilterChange,
  filters,
  onFiltersChange,
  onClearAllFilters,
  onShowAll,
  defaultStatusFilter = '',
  onLoadMore,
  hasMore,
  isLoadingMore,
  noteDraftStore,
  expandedIncidents,
  toggleIncident,
  selectedIncidents,
  toggleIncidentSelected,
  setIncidentsSelected,
  onAction,
  pendingGroupIds,
  actionErrors,
  onGoToMonitors,
  focusDeliveryId,
  focusItemKey,
}: AlertingInboxProps) {
  const items = inbox?.items ?? []
  const total = inbox?.total ?? 0
  // Every inbox action is editor-only server-side (deps.py `require_editor`),
  // and this page used to render all five buttons on all fifty rows to a viewer
  // whose every click came back 403. Read once for the section;
  // the cards below omit their action cluster entirely rather than showing 250
  // disabled buttons with no explanation attached to any of them.
  const canWrite = useCanWriteProject()

  // Which OTHER loaded incidents share a scope. Two groups on the same scope
  // are the same event seen through different detectors, and muting one leaves
  // the other paging — the card has to say so, or "I muted it and it came back"
  // is the only available reading.
  const siblingsByGroupId = useMemo(() => {
    const byScope = new Map<string, AlertInboxGroup[]>()
    const loaded = inbox?.items ?? []
    const rendered = pinnedGroup ? [pinnedGroup, ...loaded] : loaded
    for (const group of rendered) {
      const bucket = byScope.get(group.scope_ref)
      if (bucket) bucket.push(group)
      else byScope.set(group.scope_ref, [group])
    }
    const out = new Map<string, AlertInboxGroup[]>()
    for (const group of rendered) {
      const others = (byScope.get(group.scope_ref) ?? []).filter(
        other => other.correlation_group_id !== group.correlation_group_id,
      )
      if (others.length > 0) out.set(group.correlation_group_id, others)
    }
    return out
  }, [inbox, pinnedGroup])

  const openCount = items.filter(group => !isHandledInboxStatus(group.status)).length
  const handledCount = items.length - openCount

  // Every card on screen, in render order: the pinned deep-linked one first (it
  // sits above the list), then the loaded pages. This is both what "select all
  // N shown" covers and the axis a shift-click range walks, and it is the ONLY
  // set either may touch — it is exactly the set `ProjectAlertingTab`'s pruning
  // contract guarantees, so neither control can leave a selected id off screen.
  const selectableIds = useMemo(() => {
    // Empty in the loading and error branches, because the body renders zero
    // cards in both. The error one is the reachable case: the inbox polls on an
    // interval, and TanStack v5 sets status 'error' on a failed background
    // refetch while KEEPING `data` — so `inbox` still holds the loaded pages
    // under a panel that shows only "Could not load". Derived from `inbox`
    // alone, the header offered "Select all 50 shown" over an empty panel, and
    // one click built a selection of fifty incidents nobody could see.
    if (isLoading || isError) return []
    const loaded = (inbox?.items ?? []).map(group => group.correlation_group_id)
    return pinnedGroup ? [pinnedGroup.correlation_group_id, ...loaded] : loaded
  }, [inbox, pinnedGroup, isLoading, isError])
  const selectedShownCount = selectableIds.filter(id => selectedIncidents.has(id)).length
  const allShownSelected =
    selectableIds.length > 0 && selectedShownCount === selectableIds.length

  // The last checkbox the operator touched, as the anchor a shift-click extends
  // from. A ref and not state: it changes nothing on screen by itself, and
  // re-rendering fifty cards to remember where the last click was would be a
  // render per tick of a sweep.
  const rangeAnchorId = useRef<string | null>(null)

  /**
   * One card's checkbox, with shift extending the range from the last one
   * touched — so clearing a page of 50 is two clicks rather than 50.
   *
   * The whole range takes the state the CLICKED box just moved to, which is what
   * every list with this gesture does: shift-click a ticked box to clear the run,
   * an unticked one to fill it. Both ends are on screen and both were chosen by
   * the operator, so this creates no selection they cannot see — the property
   * `InboxBulkActionBar` refuses "select all N matching" to protect.
   */
  // Stable across renders that do not change the list, so the memoized cards
  // below are not all re-rendered by one of them changing (ALR-29).
  const selectIncident = useCallback((
    correlationGroupId: string,
    selected: boolean,
    extendRange: boolean,
  ) => {
    const anchorIndex = rangeAnchorId.current
      ? selectableIds.indexOf(rangeAnchorId.current)
      : -1
    const clickedIndex = selectableIds.indexOf(correlationGroupId)
    rangeAnchorId.current = correlationGroupId
    // No anchor yet, or an anchor whose row has since been pruned: fall back to
    // the plain single toggle rather than guessing at a range.
    if (!extendRange || anchorIndex < 0 || clickedIndex < 0 || anchorIndex === clickedIndex) {
      toggleIncidentSelected(correlationGroupId, selected)
      return
    }
    const start = Math.min(anchorIndex, clickedIndex)
    const end = Math.max(anchorIndex, clickedIndex)
    setIncidentsSelected(selectableIds.slice(start, end + 1), selected)
  }, [selectableIds, toggleIncidentSelected, setIncidentsSelected])

  // Bumped by "Show all", which clears the filters from OUTSIDE the bar: a
  // scope typed inside the debounce window lives only in the bar's own draft,
  // and remounting is the one reset that also drops the pending debounce —
  // otherwise it fired afterwards and re-applied what was just cleared (ALR-50).
  const [filterBarGeneration, setFilterBarGeneration] = useState(0)
  // Status and every other filter off, in one write when the page offers one.
  const clearAllFilters = () => {
    if (onClearAllFilters) {
      onClearAllFilters()
    } else {
      onStatusFilterChange('')
      onFiltersChange(EMPTY_INBOX_FILTERS)
    }
  }

  const windowTruncatedAt = inbox?.window_truncated_at ?? null
  const subtitle = isLoading
    ? 'Loading…'
    : isError
      ? 'Could not load'
      : `Showing ${items.length} of ${total} · ${coverageLabel(windowTruncatedAt)}`

  const errorMessageFor = (group: AlertInboxGroup) => {
    const failure = liveInboxActionError(actionErrors, group)
    return failure ? getErrorMessage(failure.error) : null
  }

  const renderCard = (group: AlertInboxGroup, isPinned: boolean) => (
    <IncidentCard
      key={group.correlation_group_id}
      slug={slug}
      group={group}
      isPinned={isPinned}
      siblings={siblingsByGroupId.get(group.correlation_group_id) ?? NO_SIBLINGS}
      noteDraftStore={noteDraftStore}
      isExpanded={expandedIncidents.has(group.correlation_group_id)}
      toggleIncident={toggleIncident}
      isSelected={selectedIncidents.has(group.correlation_group_id)}
      onSelectChange={selectIncident}
      onAction={onAction}
      canWrite={canWrite}
      isPending={pendingGroupIds.has(group.correlation_group_id)}
      errorMessage={errorMessageFor(group)}
      focusDeliveryId={focusDeliveryId}
      focusItemKey={focusItemKey}
    />
  )

  return (
    <div className="min-w-0 space-y-4">
      {/* Once, at the head of the section — not on each of the cards, which is
          the same sentence up to fifty times for one fact about the account. */}
      {!canWrite && (
        <ReadOnlyNotice>{VIEWER_READ_ONLY_NOTICE}</ReadOnlyNotice>
      )}
      {/* Without a rule nothing can correlate, so an empty list here would
          read as "no incidents" when the truth is "nothing can produce
          one". Say which, and where to fix it. */}
      {/* "0 incidents", the word the rest of the UI uses — not "0 groups",
          an internal term (AL-18). The CTA is a button, not an underlined word
          mid-sentence; viewers get the sentence without it, since a rule is
          an editor write. */}
      {!hasRules && (
        <Panel title="Inbox" subtitle="0 incidents">
          <EmptyState
            size="sm"
            headingLevel={3}
            icon={BellRing}
            title="No alert rules yet"
            description="Nothing can raise an incident until a rule watches a scan. When a rule fires, its incidents show up here."
            action={
              canWrite ? (
                <Button size="sm" onClick={onGoToMonitors}>
                  Create a rule
                </Button>
              ) : undefined
            }
          />
        </Panel>
      )}
      {hasRules && (
      <Panel
        title="Inbox"
        subtitle={subtitle}
        // The Panel's `right` slot was going unused while the list it heads had
        // no control of any kind: 37 of 57 production incidents were reachable
        // by no means at all.
        right={
          <div className="flex flex-wrap items-center gap-3">
            {/* "Select all N shown", not "select all N matching" — the number in
                the label is the number of cards under it, so what the bulk bar
                is about to act on is on screen and countable. That is the line
                `InboxBulkActionBar` draws: what it refuses is a one-click sweep
                of rows nobody has seen, not a sweep of the page in front of
                you, which today costs 50 ticks on 16px boxes. */}
            {canWrite && selectableIds.length > 0 && (
              <div className="flex items-center gap-1.5">
                <Checkbox
                  checked={allShownSelected ? true : selectedShownCount > 0 ? 'indeterminate' : false}
                  onCheckedChange={checked => setIncidentsSelected(selectableIds, checked === true)}
                  aria-label={`Select all ${selectableIds.length} shown incidents`}
                  title="Shift-click two incident checkboxes to select everything between them."
                />
                <span className="whitespace-nowrap text-caption text-fg-tertiary">
                  Select all {selectableIds.length} shown
                </span>
              </div>
            )}
          </div>
        }
      >
        <div className="space-y-3 p-4">
          {/* Above the truncation notice and the list alike: the controls that
              decide what is below them must not move as the answer changes, and
              a reader who filtered into an empty result needs the control that
              did it on screen, not scrolled past. */}
          <InboxFilterBar
            key={filterBarGeneration}
            value={filters}
            onChange={onFiltersChange}
            status={statusFilter}
            onStatusChange={onStatusFilterChange}
            statusCounts={inbox?.status_counts}
            defaultStatus={defaultStatusFilter}
            onClearAll={clearAllFilters}
          />
          {/* ABOVE the loading/error/empty/list ternary, not inside its last
              branch. The cap drops rows before grouping and before the status
              filter, so a filter whose matching incidents were the dropped ones
              renders ZERO cards — and that reader is the one who most needs to
              know the window was cut, because a bare "No resolved incidents."
              reads as an answer about the project rather than about the page.

              Safe in every branch: `window_truncated_at` comes off the response,
              so it is null while loading and null on an error, and this renders
              nothing in both. */}
          {windowTruncatedAt && (
            <p
              role="status"
              className="rounded-md border border-dashed p-3 text-body-sm text-fg-tertiary"
            >
              This project sent more alerts in the last 30 days than the Inbox reads at
              once, so the list starts at {formatDateTime(windowTruncatedAt)}. An incident
              whose last delivery was before then is not here unless it is still silenced —
              it has not been resolved, and its own link still opens it.
            </p>
          )}
          {isLoading ? (
            // Card-shaped, not a sentence in a dashed box (#237).
            <SectionSkeleton variant="list" rows={3} label="Loading incidents…" />
          ) : isError ? (
            <p role="alert" className="rounded-lg border border-dashed p-4 text-body text-destructive">
              Could not load the inbox: {getErrorMessage(loadError)}
            </p>
          ) : items.length === 0 && !pinnedGroup ? (
            <div className="rounded-lg border border-dashed p-4 text-body text-fg-tertiary">
              {statusFilter || hasActiveInboxFilters(filters) ? (
                <>
                  {/* Naming the filter is the difference between "nothing has
                      ever happened here" and "nothing matches what you asked
                      for" — and the second one is undoable. */}
                  {/* The window is deliberately NOT named here any more.
                      A silenced incident is now held past it, so
                      "no muted incidents in the last 30 days" would state a
                      bound that does not govern the very filter it describes —
                      and ?status=muted is precisely the filter the rescue
                      serves. The filter's own name still does the work this
                      sentence exists for. */}
                  {/* Both halves are named because either can be the one that
                      emptied the page, and a sentence that mentions only the
                      status sends a reader to clear a filter that was not the
                      cause. */}
                  No {statusFilter ? `${alertInboxStatusLabel(statusFilter).toLowerCase()} ` : ''}
                  incidents
                  {hasActiveInboxFilters(filters) ? ' match these filters' : ''}.{' '}
                  <button
                    type="button"
                    onClick={() => {
                      if (onShowAll) onShowAll()
                      else clearAllFilters()
                      setFilterBarGeneration(generation => generation + 1)
                    }}
                    className="underline underline-offset-2"
                  >
                    Show all
                  </button>
                </>
              ) : (
                // "Incidents", as the rest of the UI calls them — not
                // "correlated alert groups" (AL-18).
                `No incidents in the ${LOOKBACK_LABEL}. When a rule fires, it shows up here.`
              )}
            </div>
          ) : (
            <>
              {/* Counted over the rows actually loaded, and labelled as such:
                  the server returns a total but no per-status breakdown, and a
                  number that looks project-wide while describing one page is
                  how "52 open" turns into a decision nobody can retrace. */}
              {!statusFilter && items.length > 0 && (
                <p className="text-body-sm text-fg-tertiary">
                  Of the {countOf(items.length, 'incident', 'incidents')} loaded: {openCount} open ·{' '}
                  {handledCount} handled
                </p>
              )}
              {pinnedGroup && (
                <div className="space-y-1">
                  <p className="text-body-sm text-fg-tertiary">
                    Linked from an alert. This incident is outside the list below.
                  </p>
                  {renderCard(pinnedGroup, true)}
                </div>
              )}
              <div className="space-y-2">{items.map(group => renderCard(group, false))}</div>
              {hasMore && (
                <Button
                  variant="outline"
                  size="sm"
                  className="max-sm:h-9"
                  disabled={isLoadingMore}
                  onClick={onLoadMore}
                >
                  {isLoadingMore ? 'Loading…' : `Load more (${total - items.length} left)`}
                </Button>
              )}
            </>
          )}
        </div>
      </Panel>
      )}
    </div>
  )
}

/**
 * What each navigation destination is called, and the caveat its handful of
 * visible words have no room for.
 *
 * Two entries because the two pages chart different entities: announcing an
 * event-type page as "view event volume" would re-make the event/event-type
 * conflation the deny-set is fenced against. The words name the
 * CHART the reader lands on rather than the entity ("open event", "details"),
 * which is what stops the click reading as corroboration of a release-cohort
 * comparison neither page can reproduce.
 */
const NAVIGATION_DESTINATION: Record<
  ScopeNavigationTarget['scope'],
  { words: string; title: string }
> = {
  event: {
    words: 'event volume',
    title:
      "Opens this event's own monitoring page. It charts the event's volume against the seasonal baseline over that page's own range — not the release-cohort comparison this incident made. Its By version tab holds release regressions for the current latest release only, so it will not show this comparison once a newer release ships.",
  },
  event_type: {
    words: 'event type volume',
    title:
      "Opens this event type's own monitoring page. It charts the type's aggregated volume against the seasonal baseline over that page's own range — not the release-cohort comparison this incident made. Its By version tab holds release regressions for the current latest release only, so it will not show this comparison once a newer release ships.",
  },
}

/** The separator between the meta row's facts; decoration, so not read out. */
function MetaDot() {
  return <span aria-hidden="true">·</span>
}

/** One shared empty list, so a card without siblings keeps a stable prop. */
const NO_SIBLINGS: readonly AlertInboxGroup[] = []

/** Short enough to be spoken as part of a button name, long enough to identify. */
function scopeSummary(group: AlertInboxGroup): string {
  const names = group.scope_names.join(', ')
  if (!names) return 'this incident'
  return names.length > 60 ? `${names.slice(0, 59)}…` : names
}

interface IncidentCardProps {
  slug: string
  group: AlertInboxGroup
  isPinned: boolean
  siblings: readonly AlertInboxGroup[]
  noteDraftStore: NoteDraftStore
  isExpanded: boolean
  toggleIncident: (correlationGroupId: string) => void
  isSelected: boolean
  // `extendRange` is the shift key at click time; the section above owns the
  // list order the range is measured against, so the card only reports it.
  onSelectChange: (correlationGroupId: string, selected: boolean, extendRange: boolean) => void
  onAction: (variables: InboxActionVariables) => void
  // Read from the auth context ONCE by the section above and threaded down, so
  // fifty cards cannot answer the same question fifty different ways.
  canWrite: boolean
  isPending: boolean
  errorMessage: string | null
  focusDeliveryId?: string
  focusItemKey?: string
}

/**
 * Memoized, with every prop the section passes kept stable (ALR-29): an update
 * that concerns one card — its action going pending, its checkbox — re-renders
 * that card and not the other 49–249.
 */
const IncidentCard = memo(function IncidentCard({
  slug,
  group,
  isPinned,
  siblings,
  noteDraftStore,
  isExpanded,
  toggleIncident,
  isSelected,
  onSelectChange,
  onAction,
  canWrite,
  isPending,
  errorMessage,
  focusDeliveryId,
  focusItemKey,
}: IncidentCardProps) {
  const noteDraft = useNoteDraft(noteDraftStore, group.correlation_group_id)
  const [muteOpen, setMuteOpen] = useState(false)
  // Open when there is already a note to amend or a draft to finish, collapsed
  // when there is not: 20 identical empty inputs were the widest element in
  // every card and added 20 tab stops to a list nobody was writing on.
  // Drafts outlive a section switch, so a returning reader
  // must not have to re-find the box holding what they typed.
  const [noteOpen, setNoteOpen] = useState(!!group.note || noteDraft.length > 0)
  // Hands the caret to the editor when — and only when — the operator opened it.
  // "Add note" used to cost two clicks and a hunt: reveal the box,
  // then go find it, which is most of what made writing one feel like paperwork.
  //
  // A flag armed by the click and a callback ref that spends it, rather than
  // `autoFocus`, for two independent reasons. `autoFocus` fires on MOUNT, and
  // `noteOpen` also starts true on a card that arrives with a stored note or a
  // surviving draft — so every such card on a 50-row page would race for the
  // caret on load and it would land in whichever one React committed last. And
  // jsx-a11y/no-autofocus rejects the prop outright, correctly: focus moved by a
  // user's own gesture is a different thing from focus seized at page load, and
  // this is the first.
  const noteFocusPending = useRef(false)
  const openNote = () => {
    noteFocusPending.current = true
    setNoteOpen(true)
  }
  // Guarded, because a callback ref re-runs on every render whose identity
  // changed — unguarded, this would drag focus back into the box each time the
  // draft changed, i.e. on every keystroke, from wherever the reader had tabbed.
  const focusNoteWhenOpened = (node: HTMLTextAreaElement | null) => {
    if (!node || !noteFocusPending.current) return
    noteFocusPending.current = false
    node.focus()
  }

  // Whether the shift key was down for the click that is about to change the
  // box. Radix drives `onCheckedChange` from the same click event but hands over
  // no event, and it composes a passed `onClick` BEFORE its own handler — so
  // this is set and then read within one gesture. Keyboard activation (Space)
  // dispatches a click too, with `shiftKey` false, which is the right answer:
  // shift+Space is not a range gesture anywhere.
  const rangeExtendPending = useRef(false)

  const id = group.correlation_group_id
  const target = scopeSummary(group)
  const scopePath = getScopeMonitoringPath(slug, group)
  // Null whenever `scopePath` is not — it is guarded by that function's
  // complement, so the two can never both have an answer and no
  // card can offer two links to one page. Which page it is decides the wording,
  // so the strings are resolved here rather than assumed to be the event's.
  const navTarget = getScopeNavigationTarget(slug, group)
  const navLink = navTarget && {
    path: navTarget.path,
    label: `view ${NAVIGATION_DESTINATION[navTarget.scope].words}`,
    // Named by the scope, like this row's checkbox and for the same reason: 30
    // links all announced "view event volume" are 30 controls a screen-reader
    // operator cannot tell apart.
    announce: `View ${NAVIGATION_DESTINATION[navTarget.scope].words} for ${target}`,
    title: NAVIGATION_DESTINATION[navTarget.scope].title,
  }
  const reason = incidentReasonLabel(group.direction, group.scope_types)
  const headline = incidentHeadline(group)
  const deltaBadge = incidentDeltaBadge(group)
  const worstDelta = incidentWorstDeltaLabel(group)
  const decision = priorDecisionLabel(group)
  const isMuted = group.status === 'muted'

  // Emptying the box is how a note is DELETED, and it used to be the one
  // gesture the editor refused. The server has always supported it
  // — `_apply_inbox_action_to_state` writes `state.note = note.strip() or None`,
  // so an empty string clears the column, and both request schemas keep the
  // empty string valid for exactly this — but the button was disabled at an
  // empty box and the request omitted the key anyway, so a wrong note was
  // permanent unless someone thought to overwrite it with a correction.
  //
  // The three states are distinct and the button has to name which one it is
  // in, because "Save note" over an empty box reads as a no-op:
  //   text, or text over a stored note → Save note
  //   empty over a stored note         → Clear note   (sends "")
  //   empty, nothing stored            → nothing to do, disabled
  const trimmedNoteDraft = noteDraft.trim()
  const isClearingNote = trimmedNoteDraft.length === 0 && !!group.note
  const canSaveNote = trimmedNoteDraft.length > 0 || isClearingNote
  const noteBudget = noteBudgetLabel(noteDraft.length)

  const runAction = (action: AlertInboxAction, mutedUntil?: string | null) => {
    setMuteOpen(false)
    onAction({ group, action, mutedUntil })
  }

  return (
    <div
      id={`incident-${id}`}
      className="rounded-card border p-3 text-body-sm"
      style={isPinned ? { borderColor: 'var(--accent)' } : undefined}
    >
      <div className="flex items-start gap-2">
      {/* The leading checkbox, and the ONE thing that turns this list into a
          queue you can sweep. Gated on the same `canWrite` as the
          action row below, and for the same reason: every inbox action is
          editor-only server-side, so a selection a viewer could build is a
          selection nothing on the page would let them spend.

          Named by the SCOPE, through the same `scopeSummary` every action button
          on this card is named by. That is deliberate reuse and not convenience:
          two incidents on one scope differ only in direction or signal kind, and
          a checkbox announced as "Select incident 3 of 12" would be the one
          control on the card that cannot tell them apart. It also means a test
          — and a speech-input user — addresses the checkbox and the buttons
          beside it by the same words.

          Outside the `canWrite &&` block that wraps the actions further down
          because it sits ABOVE the card body in the DOM, not below it; the guard
          is therefore repeated here rather than the block being stretched
          around half the card. */}
      {canWrite && (
        <Checkbox
          className="mt-0.5 shrink-0"
          checked={isSelected}
          onClick={event => {
            rangeExtendPending.current = event.shiftKey
          }}
          onCheckedChange={checked => {
            const extendRange = rangeExtendPending.current
            rangeExtendPending.current = false
            onSelectChange(id, checked === true, extendRange)
          }}
          title="Shift-click to select every incident between this one and the last one you ticked."
          // Names the INCIDENT, not just the scope. Direction is part of the
          // correlation key, so one scope firing both ways is two cards — the
          // sibling line below says exactly that — and two checkboxes both
          // announced "Select checkout_started" are two identical controls a
          // screen-reader operator cannot tell apart. Same reason-then-scope
          // wording as the mute confirmation, so the control and the sentence
          // that follows it name the same thing.
          aria-label={`Select ${reason} on ${target}`}
        />
      )}
      <div className="min-w-0 flex-1">
        {/* The title row (AL-12): status, then WHAT broke at the card's own
            heading size, then HOW MUCH as a signed badge on the right. It used
            to lead with a row of chips and put the scope on line two as a
            muted, underlined comma list, so nothing on the card supported
            scanning a queue. */}
        <div className="flex items-start gap-2">
          <Chip size="sm" className="mt-px shrink-0" tone={alertInboxStatusTone(group.status)}>
            {alertInboxStatusLabel(group.status)}
          </Chip>
          <div className="min-w-0 flex-1 break-words text-body font-semibold">
            {/* Linked, not just named: the card told you WHAT fired and gave
                you no way to go look at it, so answering "is this real" meant
                leaving for the events page and finding it by hand.
                `break-words`, not `truncate`: at 390px the identity used to
                clip to nine characters while the buttons kept full width. */}
            {scopePath ? (
              <Link to={scopePath} className="hover:underline">
                {headline.primary || 'Unnamed scope'}
              </Link>
            ) : (
              <span>{headline.primary || 'Unnamed scope'}</span>
            )}
            {headline.more > 0 && (
              <>
                {' '}
                <span
                  className="whitespace-nowrap text-body-sm font-normal text-fg-tertiary"
                  title={group.scope_names.join(', ')}
                >
                  +{headline.more} more
                </span>
              </>
            )}
            {/* A SEPARATE affordance, not the name made clickable.
                A release regression left a quarter of the
                production inbox as dead text: the name-as-link above is the
                substantiating gesture the deny-set in lib/monitoring.ts
                governs, and it is right to withhold it here — but "you cannot
                prove it from there" is not "you may not go there". A distinct
                link with its own words is what makes that difference sayable.

                Both halves of a release regression get it, and they land on
                different pages: an event-scoped one on the event, an
                event-TYPE-scoped one on the event type. `title` carries the
                caveat the visible words have no room for.

                Rendered only when `scopePath` is null, so the structure guards
                the double link too, not only the helper. */}
            {!scopePath && navLink && (
              <>
                {' '}
                <span className="text-body-sm font-normal text-fg-tertiary">
                  {'· '}
                  <Link
                    to={navLink.path}
                    aria-label={navLink.announce}
                    title={navLink.title}
                    className="hover:text-foreground hover:underline"
                  >
                    {navLink.label}
                  </Link>
                </span>
              </>
            )}
          </div>
          {deltaBadge && (
            <Chip
              size="md"
              tone={deltaBadge.tone}
              className="tnum shrink-0 font-semibold"
              title={incidentMagnitudeTitle(group)}
            >
              {deltaBadge.label}
            </Chip>
          )}
        </div>
        <div
          className="mt-1 text-body-sm text-fg-tertiary"
          title={incidentMagnitudeTitle(group)}
        >
          {incidentMagnitudeLabel(group)}
          {worstDelta && <> · {worstDelta}</>}
        </div>
        {/* The meta row: what kind of signal, how many items, when, and which
            rule on which scan — at 12.5px, not the 10px it was (AL-13). */}
        <div className="mt-1 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-body-sm text-fg-tertiary">
          {/* Allowed to wrap: a multi-kind incident's reason reads "↑ spike ·
              volume + event-type volume + metric + project volume", which as
              one nowrap line ran past the card at 375px (LIVE-19). */}
          <span className="break-words">
            {incidentDirectionGlyph(group.direction)} {reason}
          </span>
          <MetaDot />
          <span className="font-medium text-foreground">{countOf(group.item_count, 'item', 'items')}</span>
          {group.item_count > 1 && (
            <span>
              ({countOf(group.scope_names.length, 'distinct scope name', 'distinct scope names')} shown)
            </span>
          )}
          <MetaDot />
          <span className="whitespace-nowrap" title={formatDateTime(group.latest_delivery_at)}>
            {formatRelativeTime(group.latest_delivery_at)}
          </span>
          {group.false_positive_count > 0 && (
            <>
              <MetaDot />
              <span title="How many times this exact group has already been marked a false positive.">
                marked false positive {countOf(group.false_positive_count, 'time', 'times')}
              </span>
            </>
          )}
          <MetaDot />
          <span className="break-words">
            {/* Each rule is linked by ITS OWN id: `rules` pairs id with name, so
                the card can no longer send "Volume rule" to whichever monitor
                sorted first. The monitor page is where the coarse
                mute lives — the one control that fits "silence all of this". */}
            {group.rules.length > 0
              ? group.rules.map((rule, index) => (
                  <span key={rule.id}>
                    {index > 0 && ', '}
                    <Link
                      to={projectPath(currentOrgSlug(), slug, `/monitors/${rule.id}`)}
                      className="hover:text-foreground hover:underline"
                    >
                      {rule.name}
                    </Link>
                  </span>
                ))
              : group.rule_names.join(', ')}
            {group.scan_names.length > 0 && <> on {group.scan_names.join(', ')}</>}
          </span>
        </div>
        {siblings.length > 0 && (
          <div className="mt-1 text-body-sm text-fg-tertiary">
            {siblings.map(sibling => (
              <a
                key={sibling.correlation_group_id}
                href={`#incident-${sibling.correlation_group_id}`}
                className="underline underline-offset-2 hover:text-foreground"
                title="The same scope is also open as a different kind of signal. Silencing one does not silence the other — they are separate suppression keys."
              >
                also here as {incidentReasonLabel(sibling.direction, sibling.scope_types)}
              </a>
            ))}
          </div>
        )}
        {decision && (
          <div className="mt-1 text-body-sm text-fg-tertiary">{decision}</div>
        )}
      </div>
      </div>
      {/* Guarded on the EFFECTIVE flag, not on the timestamp: a lapsed mute
          used to render an "open" badge and "muted until <a date in the past>"
          on the same card.

          There are now THREE cases, and `muted` is the only signal that
          separates two of them, so the outer guard must stay exactly as it is:
            - in force, dated    → muted = true,  muted_until = <future>
            - in force, no end   → muted = true,  muted_until = null
            - lapsed             → muted = false, muted_until = null
          Rewriting this around `muted_until` — the obvious way to make the
          open-ended case render — puts the "Open badge + mute line" card
          straight back. And dropping the inner branch is just as bad the other
          way: an indefinite mute would then say nothing at all, which is the
          silent mute the indefinite-mute row exists to remove.

          The open-ended row keeps status `muted` like any other, so the chip
          and the Muted filter need no special case — but its sort key is frozen
          and it never lapses, so it sinks out of the 30-day window for good and
          that filter is the only route back to its Unmute. */}
      {group.muted && (
        <div className="mt-2 text-body-sm text-fg-tertiary">
          {group.muted_until
            ? `muted until ${formatDateTime(group.muted_until)}`
            : 'muted — no end date, until you unmute it'}
        </div>
      )}
      {group.note && (
        <p className="mt-2 rounded-sm border-l-2 border-fg-tertiary/30 bg-muted/40 px-2 py-1 text-body-sm">
          {group.note}
        </p>
      )}
      {/* Who owns the affected event type / metric (F07, #260), readable by
          everyone; the one-off email to them is an editor's action. */}
      <OwnersNotify
        className="mt-2"
        owners={group.owners}
        canNotify={canWrite}
        notify={() => alertingApi.notifyIncidentOwners(slug, id)}
        target={target}
      />
      {/* A cited summary of the incident (F14, #267). Collapsed and fetched
          only when opened; renders nothing while AI is off. */}
      <IncidentSummary slug={slug} correlationGroupId={id} canWrite={canWrite} />

      {/* Omitted, not disabled, for a viewer: five buttons and a note box per
          card, greyed out and unexplained, is a worse page than one that does
          not offer what the API will refuse. The section says why once, at its
          head, and everything above this line — status, scope, magnitude, prior
          decision, the deliveries — is exactly as readable as it is for an
          editor. */}
      {canWrite && (
      <>
      {/* DOM order puts the note FIRST and `order` puts it back underneath:
          the actions used to precede it in the DOM while its placeholder
          promised the text would be "sent with the next action", so a keyboard
          user reached the action first and the note was never sent.
          */}
      <div className="mt-2 flex flex-col gap-2">
        <div className="order-2">
          {noteOpen ? (
            <div className="flex flex-col gap-1">
              {/* A textarea and not the one-line Input this used to be: the cap
                  is MAX_INBOX_NOTE_LENGTH, and two thousand characters through a
                  28px slot shows about one line of them at a time, so the reader
                  cannot see the sentence they are writing — let alone the pasted
                  error it is quoting. Three rows is the whole of
                  a normal note without turning an untouched card into a form. */}
              <Textarea
                ref={focusNoteWhenOpened}
                rows={3}
                aria-label={`Note on ${target}`}
                placeholder={group.note ? 'Replace the note…' : 'Why does this matter?'}
                maxLength={MAX_INBOX_NOTE_LENGTH}
                value={noteDraft}
                onChange={event => noteDraftStore.set(id, event.target.value)}
                // Ctrl/Cmd+Enter, never bare Enter. This is a textarea BECAUSE a
                // note is prose, so Enter has to go on making paragraphs; the
                // modifier is the shortcut every comment box already uses, which
                // is why it needs no teaching — and the button says so anyway.
                onKeyDown={event => {
                  if (!(event.metaKey || event.ctrlKey) || event.key !== 'Enter') return
                  if (isPending || !canSaveNote) return
                  event.preventDefault()
                  runAction('note')
                }}
                className="min-h-0 w-full py-1.5"
              />
              <div className="flex flex-wrap items-center gap-2">
                {/* An explicit save, because a note used to be reachable only as a
                    passenger on an action — so writing down WHY something was a
                    false positive meant first undoing the false positive.
                    `note` moves no status and stamps no
                    `acted_at`. */}
                <Button
                  size="sm"
                  variant="outline"
                  className="max-sm:h-9 max-sm:px-3"
                  title="Ctrl+Enter (⌘+Enter on a Mac) saves without leaving the box."
                  disabled={isPending || !canSaveNote}
                  onClick={() => runAction('note')}
                >
                  {isClearingNote ? 'Clear note' : 'Save note'}
                </Button>
                {noteBudget && (
                  // `role="status"`: it appears mid-sentence, while the reader is
                  // looking at their own typing rather than at the row below it.
                  <span role="status" className="text-caption text-fg-tertiary">
                    {noteBudget}
                  </span>
                )}
              </div>
            </div>
          ) : (
            <button
              type="button"
              onClick={openNote}
              className="inline-flex min-h-9 items-center text-body-sm text-fg-tertiary underline underline-offset-2 hover:text-foreground sm:min-h-0"
            >
              {group.note ? 'Edit note' : 'Add note'}
            </button>
          )}
        </div>

        <div className="order-1 flex flex-wrap items-center gap-1">
          {/* FIXED SLOTS. Ack and Reopen used to be conditional, so open rows
              read [Ack][Resolve][Mute][False positive] and muted rows read
              [Resolve][Mute][False positive][Reopen] — row 1's Mute overlapped
              row 3's False positive at the same x, and clicking down the list
              turned a snooze into the destructive action. Every
              slot now renders on every row; the inapplicable one is disabled. */}
          <Button
            size="sm"
            variant="outline"
            className="max-sm:h-9 max-sm:px-3"
            aria-label={`Acknowledge ${target}`}
            title="Stops re-delivery until the scope goes quiet, then this reopens by itself. Reversible."
            disabled={isPending || group.status !== 'open'}
            onClick={() => runAction('acknowledge')}
          >
            {/* The word the bulk bar uses too — one action, one label (AL-17). */}
            Acknowledge
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="max-sm:h-9 max-sm:px-3"
            aria-label={`Resolve ${target}`}
            title="Same suppression as Ack, different bucket in the filter. Reopens by itself once the scope goes quiet. Reversible."
            disabled={isPending || group.status === 'resolved'}
            onClick={() => runAction('resolve')}
          >
            Resolve
          </Button>
          <Button
            size="sm"
            variant="outline"
            className="max-sm:h-9 max-sm:px-3"
            aria-expanded={muteOpen}
            // Two WHOLE names, not one verb fragment glued to the target: the
            // "Mute <target>" half is the vocabulary the Monitors surfaces
            // announce too, so it comes from the shared module, while
            // "Change mute on <target>" stays a literal here. This surface is
            // the only one that can change a mute in place — a muted rule gets
            // a direct Unmute on both Monitors surfaces — so that half has no
            // second surface to drift from and hosting it in `mutePresets`
            // would push inbox-only state into their module. The asymmetry is
            // deliberate; the slot below is written the same way for the same
            // reason.
            aria-label={isMuted ? `Change mute on ${target}` : muteName(target)}
            title="Silences this exact scan + rule + scope + signal kind + direction — for a preset duration, or until you unmute it. The only action that survives the scope going quiet."
            disabled={isPending}
            onClick={() => setMuteOpen(current => !current)}
          >
            {isMuted ? 'Change mute' : 'Mute'}
          </Button>
          {/* The undo for a mute is named Unmute. The backend's `reopen` clears
              status AND `muted_until`, which is exactly un-muting — but it was
              labelled "Reopen", a word that does a second, different job on a
              resolved card, and "Unmute" appeared nowhere on the page while
              MonitorDetailPage had a literal Unmute button for the other mute
              system.

              Only the Unmute half is imported from `@/lib/mutePresets`. "Reopen
              <target>" is this surface's own word for lifting acknowledge,
              resolve and false-positive — it is not mute vocabulary and must
              never move into the mute module, or a rename there would silently
              relabel three non-mute actions. */}
          <Button
            size="sm"
            variant="outline"
            aria-label={isMuted ? unmuteName(target) : `Reopen ${target}`}
            title={
              isMuted
                ? 'Lifts the mute now — alerts for this resume immediately.'
                : 'Puts this back in the open queue. Alerts resume.'
            }
            disabled={isPending || group.status === 'open'}
            // On an open card the slot is KEPT, so False positive stays at the
            // same x on every row, but not drawn: a greyed
            // "Reopen" on an open incident read as an available action (AL-17).
            className={cn(
              'max-sm:h-9 max-sm:px-3',
              group.status === 'open' && 'invisible',
            )}
            onClick={() => runAction('reopen')}
          >
            {isMuted ? 'Unmute' : 'Reopen'}
          </Button>
          {/* Separated and confirmed: it is the only control on the page that
              changes DETECTION, permanently, and it sat 4px from Mute. */}
          {/* On a phone it gets a row of its own: with 36px buttons the row
              wraps anyway, and wrapping False positive in directly after
              Reopen put a permanent action one mis-tap from a reversible one
              (ALR-30). From `sm` up it sits inline behind its divider. */}
          <span
            className="mt-1 basis-full border-t pt-2 sm:ml-1 sm:mt-0 sm:basis-auto sm:border-l sm:border-t-0 sm:pl-2 sm:pt-0 border-border-subtle"
          >
            <Button
              size="sm"
              variant="outline"
              className="text-destructive max-sm:h-9 max-sm:px-3"
              aria-label={`Mark ${target} as a false positive`}
              title="Closes this incident and permanently makes detection stricter on its scopes only. Asks first, and reports how many scopes it actually changed."
              disabled={isPending || group.status === 'false_positive'}
              onClick={() => runAction('false_positive')}
            >
              False positive
            </Button>
          </span>
        </div>
      </div>

      {muteOpen && (
        <div className="mt-2 flex flex-wrap items-center gap-1 text-body-sm text-fg-tertiary">
          {/* Durations on the buttons, not a silent constant in the mutation:
              every mute was 7 days and nothing said so.

              INBOX_MUTE_CHOICES, not MUTE_PRESETS: the open-ended choice is
              offered HERE and only here. An incident with a NULL `muted_until`
              is muted forever; a RULE with a NULL `muted_until` is not muted at
              all (`is_rule_muted`), so the same button on the Monitors surfaces
              would do the opposite of its label. The list is
              composed in the shared module so this file cannot grow its own
              wording for it — and the SENTENCE each button is
              announced by comes from that module too, not just the list, so
              this surface cannot drift from the other two by rewording one
              `aria-label` in place. */}
          <span>{isMuted ? 'Change mute to' : 'Mute for'}</span>
          {INBOX_MUTE_CHOICES.map(choice => (
            <Button
              key={choice.label}
              size="sm"
              variant="outline"
              className="max-sm:h-9 max-sm:px-3"
              // The open-ended button's visible face and its accessible name
              // differ on purpose, and the reason now lives with the branch
              // that makes them differ — see `muteChoiceName`.
              // This is the only surface that can reach that branch at all: it
              // is the only one that maps INBOX_MUTE_CHOICES.
              aria-label={muteChoiceName(target, choice)}
              disabled={isPending}
              onClick={() => runAction('mute', muteChoiceUntilIso(choice))}
            >
              {choice.label}
            </Button>
          ))}
        </div>
      )}
      </>
      )}

      {/* Inside the failing card, not once below all twenty of them. */}
      {errorMessage && (
        <p role="alert" className="mt-2 text-body-sm text-destructive">
          {errorMessage}
        </p>
      )}

      {/* "What was sent" belongs to the incident, not to a
          second list: the message a reader is holding and the
          buttons that act on it are now the same card. */}
      <button
        type="button"
        aria-expanded={isExpanded}
        onClick={() => toggleIncident(id)}
        className="mt-2 inline-flex min-h-9 items-center text-body-sm underline underline-offset-2 text-fg-tertiary hover:text-foreground sm:min-h-0"
      >
        {isExpanded ? 'Hide' : 'Show'} what was sent (
        {countOf(group.delivery_count, 'delivery', 'deliveries')})
      </button>
      {isExpanded && (
        <IncidentDeliveries
          slug={slug}
          correlationGroupId={id}
          focusDeliveryId={focusDeliveryId}
          focusItemKey={focusItemKey}
        />
      )}
    </div>
  )
})
