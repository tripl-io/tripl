import { useId, useState } from 'react'
import { ArrowDownUp, ChevronDown, Download, ListFilter, ListPlus, MoreHorizontal, Plus, X } from 'lucide-react'
import type { FieldDefinition, MetaFieldDefinition } from '@/types'
import { EVENT_STATUS_LABELS, EVENT_STATUSES, type EventStatus } from '@/lib/eventStatus'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { FilterBar, FilterBarItem, FilterSearch, FilterSelect } from '@/components/ui/filter-bar'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ColumnsMenu } from './ColumnsMenu'
import { SavedViewsMenu } from './SavedViewsMenu'
import type { EventsSavedView } from './savedViews'
import type { EventsSortOrder } from './useEventsQuery'

/**
 * The Status trigger in the FilterSelect chip geometry (DS-15): 28px, caption
 * text, "{Label}: {value}". Status is a multi-select menu, so it cannot be a
 * FilterSelect itself. `whitespace-nowrap`: "Status / any" wrapped inside the
 * chip on a phone (EV-1).
 */
const CHIP_TRIGGER_CLASS = 'h-7 w-auto gap-1.5 whitespace-nowrap text-caption font-normal'
const CHIP_UNSET_CLASS = 'border-dashed bg-transparent text-fg-muted'
const CHIP_SET_CLASS = 'border-accent bg-accent-soft text-fg'

/** The silent-days values the Activity filter offers as presets. */
const SILENT_DAY_PRESETS = [1, 7, 30]

/** "no filter" for the single-value FilterSelects below. */
const ANY = '__all__'

/** Plain words for a silent-days preset: "Silent > 1d" was jargon (EV-15). */
function silentDaysLabel(days: number): string {
  return `No events for ${days}+ day${days === 1 ? '' : 's'}`
}

export function EventsToolbar({
  search,
  onSearchChange,
  isFilterPending,
  filterStatuses,
  tabDefaultStatuses = null,
  onFilterStatusesChange,
  filterSilentDays,
  onFilterSilentDaysChange,
  filterReviewed,
  onFilterReviewedChange,
  filterOpenQuestions,
  onFilterOpenQuestionsChange,
  filterProperty = '',
  onClearFilterProperty,
  sortOrder,
  onSortOrderChange,
  healthAvailable = false,
  hasActiveFilters,
  onClearFilters,
  savedViews,
  activeSavedViewName,
  savedViewName,
  onSavedViewNameChange,
  onSaveCurrentView,
  onApplySavedView,
  onDeleteSavedView,
  columnsMenuOpen,
  onColumnsMenuOpenChange,
  hiddenColumns,
  hideLastSeen,
  reviewedPinned,
  offscreenColumnCount,
  fieldColumns,
  metaFields,
  onToggleColumn,
  onExportCsv,
  canExport,
  isExporting,
  onNewEvent,
  onBulkNew,
  showSavedViews = true,
}: {
  search: string
  onSearchChange: (value: string) => void
  isFilterPending: boolean
  filterStatuses: EventStatus[]
  /** What the tab narrows to with no status picked (review, archived); `null`
   *  where nothing picked means everything but archived. */
  tabDefaultStatuses?: EventStatus[] | null
  onFilterStatusesChange: (value: EventStatus[]) => void
  filterSilentDays: number | undefined
  onFilterSilentDaysChange: (value: number | undefined) => void
  /** `undefined` = any; true/false isolate reviewed / still-unreviewed rows. */
  filterReviewed: boolean | undefined
  onFilterReviewedChange: (value: boolean | undefined) => void
  /** `undefined` = any; true/false isolate events with / without an unanswered
   *  discussion thread. */
  filterOpenQuestions: boolean | undefined
  onFilterOpenQuestionsChange: (value: boolean | undefined) => void
  /** Only events whose property list carries this property (F23): set from
   *  the Properties catalog, so the chip only shows it and clears it. */
  filterProperty?: string
  onClearFilterProperty?: () => void
  sortOrder: EventsSortOrder
  onSortOrderChange: (value: EventsSortOrder) => void
  /** Health scores exist on the main plan only (F15, #268): the "Least
   *  healthy first" sort and the Health column toggle are offered there. */
  healthAvailable?: boolean
  hasActiveFilters: boolean
  onClearFilters: () => void
  savedViews: EventsSavedView[]
  activeSavedViewName: string | null
  savedViewName: string
  onSavedViewNameChange: (value: string) => void
  onSaveCurrentView: () => void
  onApplySavedView: (view: EventsSavedView) => void
  onDeleteSavedView: (name: string) => void
  columnsMenuOpen: boolean
  onColumnsMenuOpenChange: (open: boolean) => void
  hiddenColumns: Set<string>
  hideLastSeen: boolean
  reviewedPinned: boolean
  offscreenColumnCount: number
  fieldColumns: FieldDefinition[]
  metaFields: MetaFieldDefinition[]
  onToggleColumn: (key: string) => void
  onExportCsv: () => void
  /** False while the loaded page does not belong to the current filters — the
   *  export sweeps from that page's count, so firing it early writes an empty
   *  file that reads like "nothing matched". */
  canExport: boolean
  isExporting: boolean
  /** Omitted for a viewer: creating events is an editor's job (EVT-9). */
  onNewEvent?: () => void
  onBulkNew?: () => void
  /** Off where the table is embedded in another page: a saved view navigates
   *  to the events route, away from the host. */
  showSavedViews?: boolean
}) {
  // A silent-days value from a shared link that no preset names used to leave
  // the single-value select showing "Any", so it gets an item of its own that
  // says what is actually applied (EVT-35).
  const customSilentDays =
    filterSilentDays !== undefined && !SILENT_DAY_PRESETS.includes(filterSilentDays)
      ? filterSilentDays
      : undefined
  const silentDayOptions = [
    ...SILENT_DAY_PRESETS,
    ...(customSilentDays !== undefined ? [customSilentDays] : []),
  ].map(days => ({ value: String(days), label: silentDaysLabel(days) }))
  // Below sm the chips fold behind one "Filters (n)" toggle; from sm up they
  // are always shown and the toggle is not rendered (EV-1).
  const [filtersOpen, setFiltersOpen] = useState(false)
  const filtersId = useId()
  const activeChipCount =
    (filterStatuses.length > 0 ? 1 : 0) +
    (filterSilentDays !== undefined ? 1 : 0) +
    (filterReviewed !== undefined ? 1 : 0) +
    (filterOpenQuestions !== undefined ? 1 : 0) +
    (filterProperty ? 1 : 0)
  // A search is a filter to the reader: "Clear filters" appears for it and
  // clears it too (EV-16).
  const anythingToClear = hasActiveFilters || search.trim() !== ''
  return (
    // One wrapping row, ordered per breakpoint (EV-1 / EV-2). The old two
    // groups — filters `flex-1 min-w-0`, actions `ml-auto shrink-0` — let the
    // action buttons draw over the Activity and Reviewed chips at 390, and at
    // 768/1024 squeezed the filters into a five-row column.
    //   phone: search + New event / Filters (n) + Views + Columns + More / chips
    //   sm-lg: search … Views + Columns + More + New event / chips
    //   lg+:   search, chips, then the actions, on one line where they fit
    <div className="mb-3 flex flex-wrap items-center gap-2">
      {/* Primary — find: full-text filter */}
      <div className="relative order-1 flex min-w-0 flex-1 basis-40 sm:max-w-[320px] lg:flex-none lg:basis-[240px]">
        <FilterSearch
          things="events"
          value={search}
          onValueChange={onSearchChange}
          className="max-w-none min-w-0"
          aria-busy={isFilterPending || undefined}
        />
        {isFilterPending && (
          <span
            aria-hidden="true"
            className="pulse-dot pointer-events-none absolute right-2.5 top-1/2 h-1.5 w-1.5 -translate-y-1/2 rounded-full bg-accent"
            title="Updating results"
          />
        )}
      </div>

      {onNewEvent && (
        // Primary — create. Beside the search on a phone, last from sm up.
        <Button onClick={onNewEvent} size="sm" className="order-2 sm:order-4">
          <Plus />
          New event
        </Button>
      )}
      {/* Ends the phone's first line, so the secondary controls start a new one. */}
      <div aria-hidden="true" className="order-2 basis-full sm:hidden" />

      <div className="order-3 flex flex-wrap items-center gap-2 sm:ml-auto lg:order-3">
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="sm:hidden"
          aria-expanded={filtersOpen}
          aria-controls={filtersId}
          onClick={() => setFiltersOpen(open => !open)}
        >
          <ListFilter />
          {activeChipCount > 0 ? `Filters (${activeChipCount})` : 'Filters'}
        </Button>
        {/* Secondary — shape the table: saved views + columns */}
        {showSavedViews && (
          <SavedViewsMenu
            views={savedViews}
            activeViewName={activeSavedViewName}
            draftName={savedViewName}
            onDraftNameChange={onSavedViewNameChange}
            onSave={onSaveCurrentView}
            onApply={onApplySavedView}
            onDelete={onDeleteSavedView}
          />
        )}
        <ColumnsMenu
          open={columnsMenuOpen}
          onOpenChange={onColumnsMenuOpenChange}
          tagsHidden={hiddenColumns.has('tags')}
          lastSeenHidden={hideLastSeen}
          fieldColumns={fieldColumns}
          metaFields={metaFields}
          hiddenColumns={hiddenColumns}
          offscreenColumnCount={offscreenColumnCount}
          reviewedPinned={reviewedPinned}
          healthAvailable={healthAvailable}
          onToggle={onToggleColumn}
        />

        {/* Utility — export, collapsed into an overflow menu so the toolbar
            never needs a horizontal scrollbar. The unbuilt "Ask AI" entry is
            gone rather than badged "soon": a menu whose every entry is
            unavailable teaches users not to open menus. */}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm" aria-label="More actions">
              <MoreHorizontal />
              <span className="max-sm:sr-only">More</span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" sideOffset={6} className="w-[212px]">
            <DropdownMenuItem
              className="text-body-sm"
              disabled={isExporting || !canExport}
              onSelect={onExportCsv}
              title={
                canExport
                  ? 'Download every event matching the current filters and sort as CSV'
                  : 'Available once the current view has finished loading'
              }
            >
              <Download className="size-3.5 shrink-0 text-fg-tertiary" />
              {isExporting ? 'Exporting…' : 'Export CSV'}
            </DropdownMenuItem>
            {onBulkNew && (
              <DropdownMenuItem
                className="text-body-sm"
                onSelect={onBulkNew}
                title="Create a run of events from a pasted list"
              >
                <ListPlus className="size-3.5 shrink-0 text-fg-tertiary" />
                Add many events…
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>

      {/* The shared filter bar (DS-15): "{Label}: {value}" chips that apply
          instantly, then "Clear filters" while anything is set. A full line of
          its own below lg, between search and actions from lg up. */}
      <FilterBar
        className={cn(
          'order-5 w-full lg:order-2 lg:w-auto lg:min-w-0 lg:flex-1',
          !filtersOpen && 'max-sm:hidden',
        )}
        active={anythingToClear}
        onClear={onClearFilters}
      >
        <div id={filtersId} className="contents">
        {/* Folds into the phone "Filters (n)" sheet with the chips beside it. */}
        <FilterBarItem active={filterStatuses.length > 0}>
          <StatusFilter
            value={filterStatuses}
            tabDefault={tabDefaultStatuses}
            onChange={onFilterStatusesChange}
          />
        </FilterBarItem>
        {filterProperty && onClearFilterProperty && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-active
            className={cn(CHIP_TRIGGER_CLASS, CHIP_SET_CLASS, 'max-w-[260px]')}
            aria-label={`Property filter: ${filterProperty}. Clear it`}
            title="Events whose property list carries this property"
            onClick={onClearFilterProperty}
          >
            <span className="font-medium">Property:</span>
            <span className="mono truncate">{filterProperty}</span>
            <X aria-hidden="true" className="size-3.5 opacity-60" />
          </Button>
        )}
        <FilterSelect
          label="Activity"
          value={filterSilentDays === undefined ? ANY : String(filterSilentDays)}
          onValueChange={value => onFilterSilentDaysChange(value === ANY ? undefined : Number(value))}
          options={silentDayOptions}
          anyValue={ANY}
          anyLabel="Any"
        />
        {/* Reviewed is a separate axis from status (an event can be reviewed
            and still in_review), and until now it had no readable surface at
            all: no filter, no counter, and a column hidden by default. Without
            this control "Mark reviewed" wrote a flag the operator could never
            see or isolate. */}
        <FilterSelect
          label="Verified"
          value={filterReviewed === undefined ? ANY : String(filterReviewed)}
          onValueChange={value => onFilterReviewedChange(value === ANY ? undefined : value === 'true')}
          options={[
            { value: 'true', label: 'Yes' },
            { value: 'false', label: 'No' },
          ]}
          anyValue={ANY}
          anyLabel="Any"
        />
        {/* The discussion gave events a place to raise a
            question; until threads could be resolved there was no way to ask
            which events are still waiting on one. Server-side,
            like every filter here, so it sees the whole catalog and not one
            loaded page — and twin-aware, so it answers on a branch too. */}
        <FilterSelect
          label="Questions"
          value={filterOpenQuestions === undefined ? ANY : String(filterOpenQuestions)}
          onValueChange={value =>
            onFilterOpenQuestionsChange(value === ANY ? undefined : value === 'true')
          }
          options={[
            { value: 'true', label: 'Open' },
            { value: 'false', label: 'None open' },
          ]}
          anyValue={ANY}
          anyLabel="Any"
        />
        {/* Sort orders the rows, it filters nothing, so it does not wear the
            dashed filter-chip look (EV-14): a quiet ghost control with a sort
            icon, never tinted as "set", not counted by "Clear filters". The
            48h column header toggles the same order on desktop. */}
        <Select
          value={sortOrder}
          onValueChange={value => onSortOrderChange(value as EventsSortOrder)}
        >
          <SelectTrigger
            className="h-7 w-auto gap-1.5 whitespace-nowrap border-transparent bg-transparent text-caption font-normal text-fg-muted shadow-none hover:bg-surface-hover"
            aria-label="Sort order"
          >
            <ArrowDownUp aria-hidden="true" className="size-3.5" />
            <span className="font-medium">Sort:</span>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="catalog">Catalog order</SelectItem>
            <SelectItem value="volume">Busiest first</SelectItem>
            {healthAvailable && <SelectItem value="health">Least healthy first</SelectItem>}
          </SelectContent>
        </Select>
        </div>
      </FilterBar>
    </div>
  )
}

/**
 * The status filter as a real multi-select (EVT-35). The list endpoint takes
 * repeated `?status=` params and a shared link can already carry several, but
 * a single-value select could only show or pick one. Nothing ticked is the
 * default — every status but archived, or the tab's own default — which is why
 * it reads "any" rather than listing all seven.
 */
function StatusFilter({
  value,
  tabDefault,
  onChange,
}: {
  value: EventStatus[]
  tabDefault: EventStatus[] | null
  onChange: (value: EventStatus[]) => void
}) {
  // What the list is actually filtered by. On the review and archived tabs an
  // empty pick is not "any": it is the tab's own status, and the control says
  // so instead of reading "any" over a list of archived events.
  const applied = value.length > 0 ? value : tabDefault ?? []
  const summary =
    applied.length === 0 ? 'Any' : applied.map(status => EVENT_STATUS_LABELS[status]).join(', ')
  const toggle = (status: EventStatus, checked: boolean) => {
    // Kept in the canonical order, so the URL a combination produces does not
    // depend on the order the boxes were ticked in. Built on what is applied,
    // so ticking Draft on the review tab means In Review and Draft.
    onChange(EVENT_STATUSES.filter(s => (s === status ? checked : applied.includes(s))))
  }
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="outline"
          size="sm"
          aria-label="Status filter"
          data-active={value.length > 0 || undefined}
          className={cn(
            CHIP_TRIGGER_CLASS,
            'max-w-[260px]',
            value.length > 0 ? CHIP_SET_CLASS : CHIP_UNSET_CLASS,
          )}
        >
          <span className="font-medium">Status:</span>
          <span className="truncate">{summary}</span>
          <ChevronDown aria-hidden="true" className="size-3.5 opacity-50" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-[200px]">
        <DropdownMenuCheckboxItem
          checked={value.length === 0}
          // Picking stays open so several statuses can be ticked in one visit.
          onSelect={event => event.preventDefault()}
          onCheckedChange={() => onChange([])}
        >
          {tabDefault ? 'Tab default' : 'Any status'}
        </DropdownMenuCheckboxItem>
        <DropdownMenuSeparator />
        {EVENT_STATUSES.map(status => (
          <DropdownMenuCheckboxItem
            key={status}
            checked={applied.includes(status)}
            onSelect={event => event.preventDefault()}
            onCheckedChange={checked => toggle(status, checked === true)}
          >
            {EVENT_STATUS_LABELS[status]}
          </DropdownMenuCheckboxItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
