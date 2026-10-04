import { useMemo, useState } from "react"
import { useQueries, useQuery } from "@tanstack/react-query"
import { ChevronDown, Loader2, Plus, Trash2, X } from "lucide-react"
import type {
  AlertRuleFilterField,
  AlertRuleFilterOperator,
  EventType,
} from "@/types"
import { eventsApi } from "@/api/events"
import { metricsCatalogApi } from "@/api/metricsCatalog"
import { useActiveBranchId } from "@/hooks/useBranch"
import { useDebouncedValue } from "@/hooks/useDebouncedValue"
import { eventNameLabel } from "@/lib/eventName"
import { Chip } from "@/components/primitives/chip"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { IconButton } from "@/components/ui/icon-button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import {
  DIRECTION_VALUE_OPTIONS,
  FILTER_FIELD_OPTIONS,
  FILTER_OPERATOR_OPTIONS,
  isSingleValueOperator,
  makeFilterUid,
  type RuleFilterDraft,
} from "./constants"
import {
  eventKey,
  eventsPickerKey,
  metricDefinitionKey,
  metricsCatalogListKey,
} from "@/lib/queryKeys"

type PickerOption = { value: string; label: string }

// Event catalogs are unbounded — production projects hold 2,400+ events — so the
// event picker queries the server per keystroke instead of the alerting tab
// downloading the whole catalog on mount to filter it in the browser.
// Anything past this page is reachable by typing, and the
// footer says how much is hidden rather than truncating silently.
const EVENT_PAGE_SIZE = 50

// The metric catalog is searched the same way, for the same reason: it has no
// upper bound either, and the picker only needs the page that matches.
const METRIC_PAGE_SIZE = 50

/** What a server-searched value picker needs from its option source. */
type ServerOptions = {
  options: PickerOption[]
  selectedLabels: [string, string][]
  loading: boolean
  hiddenCount: number
}

export function FilterEditor({
  filters,
  eventTypes,
  slug,
  onChange,
  rowErrors = {},
  error,
}: {
  filters: RuleFilterDraft[]
  eventTypes: EventType[]
  slug: string
  onChange: (filters: RuleFilterDraft[]) => void
  /**
   * Why a row cannot be saved, keyed by its `uid`. A row with no values used
   * to be dropped from the payload without a word, so the rule saved broader
   * than the form showed (ALR-5); the dialog now refuses the submit and the
   * row says why.
   */
  rowErrors?: Record<string, string>
  /** A server rejection that names the filters. */
  error?: string | null
}) {
  const addFilter = () => {
    onChange([
      ...filters,
      { uid: makeFilterUid(), field: 'event_type', operator: 'in', values: [] },
    ])
  }

  const updateFilter = (uid: string, patch: Partial<RuleFilterDraft>) => {
    onChange(
      filters.map(filter => (filter.uid === uid ? { ...filter, ...patch } : filter)),
    )
  }

  const removeFilter = (uid: string) => {
    onChange(filters.filter(filter => filter.uid !== uid))
  }

  return (
    <div className="grid gap-2">
      <div className="flex items-center justify-between">
        <Label>Filters</Label>
        <Button type="button" size="sm" variant="outline" onClick={addFilter}>
          <Plus className="mr-2 h-4 w-4" />
          Add filter
        </Button>
      </div>
      {filters.length === 0 ? (
        <p className="text-body-sm text-fg-tertiary">
          No filters: every signal ticked above can alert.
        </p>
      ) : (
        <div className="space-y-2">
          {filters.map((filter, index) => (
            <FilterRow
              key={filter.uid}
              filter={filter}
              eventTypes={eventTypes}
              slug={slug}
              onChange={patch => updateFilter(filter.uid, patch)}
              onRemove={() => removeFilter(filter.uid)}
              error={rowErrors[filter.uid]}
              position={index + 1}
            />
          ))}
        </div>
      )}
      {error && <p className="text-body-sm text-destructive">{error}</p>}
    </div>
  )
}

/** Server-side options for the `event` filter field.
 *
 * Two independent reads, because an event filter needs two different things:
 * a *page* of candidates to pick from (searched, capped, fetched only once the
 * popover opens) and the *names* of ids already saved on the rule, which may be
 * nowhere in that page. The per-id reads reuse the key EventsPage and Overview
 * use, so an event already on screen elsewhere costs no request at all.
 */
function useEventOptions({
  slug,
  enabled,
  search,
  selectedValues,
}: {
  slug: string
  enabled: boolean
  search: string
  selectedValues: string[]
}): ServerOptions {
  const branchId = useActiveBranchId()
  const debouncedSearch = useDebouncedValue(search)

  const listQuery = useQuery({
    queryKey: eventsPickerKey(slug, branchId, 'alert-filter', debouncedSearch),
    queryFn: () =>
      eventsApi.list(
        slug,
        { search: debouncedSearch || undefined, limit: EVENT_PAGE_SIZE, offset: 0 },
        branchId,
      ),
    enabled,
    staleTime: 60_000,
  })

  const selectedLabels = useQueries({
    queries: selectedValues.map(eventId => ({
      queryKey: eventKey(slug, branchId, eventId),
      queryFn: () => eventsApi.get(slug, eventId, branchId),
      staleTime: 60_000,
    })),
    combine: results =>
      results.flatMap(result =>
        result.data
          ? ([[result.data.id, eventNameLabel(result.data.name)]] as [string, string][])
          : [],
      ),
  })

  const items = useMemo(() => listQuery.data?.items ?? [], [listQuery.data])
  // A stored name of "" paints an option with no text and no accessible name —
  // a picker row a screen reader announces as nothing but "button", on the one
  // event a user would most want to find in order to clean it up.
  //
  // Both label sources are wrapped HERE rather than at the `?? value` fallbacks
  // that read them: `Map.get` returns '' as a HIT, so `??` never fires for an
  // empty stored name. Wrapping at the source makes `labelByValue` non-empty by
  // construction, which is what the chips and the collapsed trigger print — and
  // leaves `?? value` covering the case it is actually for, a selected id whose
  // per-id read has not resolved yet and whose uuid is the right stand-in.
  const options = useMemo(
    () => items.map(event => ({ value: event.id, label: eventNameLabel(event.name) })),
    [items],
  )

  return {
    options,
    selectedLabels,
    // A stale page stays visible while the next search lands, so report fetching
    // rather than loading — otherwise the list flickers empty on every keystroke.
    loading: listQuery.isFetching,
    hiddenCount: Math.max(0, (listQuery.data?.total ?? 0) - items.length),
  }
}

/** Server-side options for the `metric` filter field (JR-15).
 *
 * The values are MetricDefinition ids — a catalog signal's scope_ref — so the
 * picker lists the metrics catalog rather than anything event-shaped. Same two
 * reads as {@link useEventOptions}: a searched page once the popover opens, and
 * one read per id already on the rule, under the key the monitoring drilldown
 * and the metric form use for the same definition.
 */
function useMetricOptions({
  slug,
  enabled,
  search,
  selectedValues,
}: {
  slug: string
  enabled: boolean
  search: string
  selectedValues: string[]
}): ServerOptions {
  const debouncedSearch = useDebouncedValue(search)

  const listQuery = useQuery({
    // Under the catalog's own prefix, so a metric created or renamed elsewhere
    // invalidates this page too; the `alert-filter` slot keeps it apart from
    // the catalog screen's whole-catalog walk, which caches a different shape.
    queryKey: metricsCatalogListKey(slug, 'alert-filter', '', debouncedSearch),
    queryFn: () =>
      metricsCatalogApi.list(slug, {
        search: debouncedSearch || undefined,
        limit: METRIC_PAGE_SIZE,
        offset: 0,
      }),
    enabled,
    staleTime: 60_000,
  })

  const selectedLabels = useQueries({
    queries: selectedValues.map(metricId => ({
      queryKey: metricDefinitionKey(slug, metricId),
      queryFn: () => metricsCatalogApi.get(slug, metricId),
      staleTime: 60_000,
    })),
    combine: results =>
      results.flatMap(result =>
        result.data
          ? ([[result.data.id, metricLabel(result.data)]] as [string, string][])
          : [],
      ),
  })

  const items = useMemo(() => listQuery.data?.items ?? [], [listQuery.data])
  const options = useMemo(
    () => items.map(metric => ({ value: metric.id, label: metricLabel(metric) })),
    [items],
  )

  return {
    options,
    selectedLabels,
    loading: listQuery.isFetching,
    hiddenCount: Math.max(0, (listQuery.data?.total ?? 0) - items.length),
  }
}

/** The catalog's display name, or its machine name when that is blank. */
function metricLabel(metric: { display_name: string; name: string }) {
  return metric.display_name.trim() || metric.name
}

function FilterRow({
  filter,
  eventTypes,
  slug,
  onChange,
  onRemove,
  error,
  position,
}: {
  filter: RuleFilterDraft
  eventTypes: EventType[]
  slug: string
  onChange: (patch: Partial<RuleFilterDraft>) => void
  onRemove: () => void
  error?: string
  /** 1-based row number, so each row's remove button has its own name. */
  position: number
}) {
  const [pickerOpen, setPickerOpen] = useState(false)
  const [search, setSearch] = useState('')
  const errorIdBase = `filter-${filter.uid}`
  const fieldLabel =
    FILTER_FIELD_OPTIONS.find(option => option.value === filter.field)?.label ?? filter.field

  const isEventField = filter.field === 'event'
  const isMetricField = filter.field === 'metric'
  const single = isSingleValueOperator(filter.operator)
  const selectedValues = single ? filter.values.slice(0, 1) : filter.values

  const eventOptions = useEventOptions({
    slug,
    enabled: isEventField && pickerOpen,
    search,
    // Guard the per-id reads: on any other field these values are event-type
    // ids, metric ids or direction literals, and asking /events for them would 404.
    selectedValues: isEventField ? selectedValues : [],
  })
  const metricOptions = useMetricOptions({
    slug,
    enabled: isMetricField && pickerOpen,
    search,
    selectedValues: isMetricField ? selectedValues : [],
  })
  // Both catalogs are unbounded, so both are searched on the server.
  const serverOptions: ServerOptions | null = isEventField
    ? eventOptions
    : isMetricField
      ? metricOptions
      : null

  const staticOptions = useMemo<PickerOption[]>(() => {
    if (filter.field === 'event_type') {
      return eventTypes.map(eventType => ({ value: eventType.id, label: eventType.display_name }))
    }
    if (filter.field === 'direction') {
      return DIRECTION_VALUE_OPTIONS
    }
    // `event` and `metric` come from the server; anything else has no options
    // here rather than borrowing the direction ones (JR-15).
    return []
  }, [filter.field, eventTypes])

  // Static option sets are small and already in memory, so they filter in the
  // browser; event and metric options arrive from the server already filtered.
  const serverPage = serverOptions?.options
  const serverSelected = serverOptions?.selectedLabels
  const visibleOptions = useMemo(() => {
    if (serverPage) return serverPage
    if (!search) return staticOptions
    const needle = search.toLowerCase()
    return staticOptions.filter(option => option.label.toLowerCase().includes(needle))
  }, [serverPage, staticOptions, search])

  // Chips and the collapsed trigger label read from here, so it must cover
  // selected ids that the current search page does not contain.
  const labelByValue = useMemo(() => {
    const map = new Map<string, string>()
    for (const option of staticOptions) map.set(option.value, option.label)
    for (const option of serverPage ?? []) map.set(option.value, option.label)
    for (const [id, name] of serverSelected ?? []) map.set(id, name)
    return map
  }, [staticOptions, serverPage, serverSelected])

  // "Choose event types…" rather than "Select value": the row reads as a
  // sentence — "Event type · is one of · Choose event types…" (AL-39).
  const valuePlaceholder =
    filter.field === 'event_type'
      ? single ? 'Choose an event type…' : 'Choose event types…'
      : filter.field === 'event'
        ? single ? 'Choose an event…' : 'Choose events…'
        : filter.field === 'metric'
          ? single ? 'Choose a metric…' : 'Choose metrics…'
          : 'Choose a direction…'

  const onFieldChange = (nextField: AlertRuleFilterField) => {
    setSearch('')
    onChange({ field: nextField, values: [] })
  }

  const onOperatorChange = (nextOperator: AlertRuleFilterOperator) => {
    const nextSingle = isSingleValueOperator(nextOperator)
    onChange({
      operator: nextOperator,
      values: nextSingle ? filter.values.slice(0, 1) : filter.values,
    })
  }

  const toggleValue = (value: string) => {
    if (single) {
      onChange({ values: [value] })
      return
    }
    const next = filter.values.includes(value)
      ? filter.values.filter(item => item !== value)
      : [...filter.values, value]
    onChange({ values: next })
  }

  return (
    <div className="rounded-md border p-2 space-y-2">
      {/* Wraps below `sm`: the two fixed selects, the picker and the bin on
          one line left the picker 0-30px at 375px, its label unreadable
          (ALR-22). The picker takes a line of its own there; from `sm` up the
          row is one line again. */}
      <div className="flex flex-wrap items-center gap-2 sm:flex-nowrap">
        <Select value={filter.field} onValueChange={value => onFieldChange(value as AlertRuleFilterField)}>
          <SelectTrigger aria-label="Filter field" className="w-36"><SelectValue /></SelectTrigger>
          <SelectContent>
            {FILTER_FIELD_OPTIONS.map(option => (
              <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={filter.operator} onValueChange={value => onOperatorChange(value as AlertRuleFilterOperator)}>
          <SelectTrigger aria-label="Filter operator" className="w-36"><SelectValue /></SelectTrigger>
          <SelectContent>
            {FILTER_OPERATOR_OPTIONS.map(option => (
              <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
            ))}
          </SelectContent>
        </Select>
        <FilterValuePicker
          single={single}
          open={pickerOpen}
          onOpenChange={setPickerOpen}
          search={search}
          onSearchChange={setSearch}
          placeholder={isEventField ? 'Search events…' : isMetricField ? 'Search metrics…' : 'Search…'}
          emptyLabel={valuePlaceholder}
          options={visibleOptions}
          labelByValue={labelByValue}
          selectedValues={selectedValues}
          onToggle={toggleValue}
          loading={serverOptions?.loading ?? false}
          hiddenCount={serverOptions?.hiddenCount ?? 0}
          errorId={error ? `${errorIdBase}-error` : undefined}
        />
        {/* Named by position and field: several rows share this icon, and an
            unnamed "button" gave no hint which filter it removes (ALR-23). */}
        <IconButton
          label={`Remove filter ${position}: ${fieldLabel}`}
          className="h-8 w-8 text-fg-tertiary hover:text-destructive ml-auto"
          onClick={onRemove}
        >
          <Trash2 className="h-4 w-4" aria-hidden="true" />
        </IconButton>
      </div>
      {error && (
        <p id={`${errorIdBase}-error`} className="text-body-sm text-destructive">{error}</p>
      )}
      {selectedValues.length > 0 && !single && (
        <div className="flex flex-wrap gap-1">
          {selectedValues.map(value => (
            <Chip key={value} size="xs">
              <span className="truncate max-w-40">{labelByValue.get(value) ?? value}</span>
              <button
                type="button"
                aria-label={`Remove ${labelByValue.get(value) ?? value}`}
                className="hit-target-24 rounded-sm hover:text-destructive"
                onClick={() => toggleValue(value)}
              >
                <X className="h-3 w-3" aria-hidden="true" />
              </button>
            </Chip>
          ))}
        </div>
      )}
    </div>
  )
}

function FilterValuePicker({
  single,
  open,
  onOpenChange,
  search,
  onSearchChange,
  placeholder,
  emptyLabel,
  options,
  labelByValue,
  selectedValues,
  onToggle,
  loading,
  hiddenCount,
  errorId,
}: {
  single: boolean
  open: boolean
  onOpenChange: (open: boolean) => void
  search: string
  onSearchChange: (search: string) => void
  placeholder: string
  /** What the collapsed trigger says while nothing is picked. */
  emptyLabel: string
  options: PickerOption[]
  labelByValue: Map<string, string>
  selectedValues: string[]
  onToggle: (value: string) => void
  loading: boolean
  hiddenCount: number
  /** The row's error message, when it has one. */
  errorId?: string
}) {
  const triggerLabel = (() => {
    const [value] = selectedValues
    if (value === undefined) return emptyLabel
    if (single) return labelByValue.get(value) ?? value
    return `${selectedValues.length} selected`
  })()

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          // A full line of its own on a phone (see the row), and the rest of
          // the row from `sm` up.
          className="order-last w-full justify-between min-w-0 sm:order-none sm:w-auto sm:flex-1"
          aria-invalid={errorId ? true : undefined}
          aria-describedby={errorId}
        >
          <span className="truncate">{triggerLabel}</span>
          <ChevronDown className="h-4 w-4 shrink-0" />
        </Button>
      </PopoverTrigger>
      <PopoverContent className="w-80 space-y-2" align="start">
        <div className="relative">
          <Input
            aria-label="Search values"
            placeholder={placeholder}
            value={search}
            onChange={event => onSearchChange(event.target.value)}
          />
          {loading && (
            <Loader2 className="absolute right-2 top-1/2 h-4 w-4 -translate-y-1/2 animate-spin text-fg-tertiary" />
          )}
        </div>
        <div className="max-h-64 overflow-y-auto space-y-1">
          {options.map(option => {
            const checked = selectedValues.includes(option.value)
            if (single) {
              return (
                <button
                  key={option.value}
                  type="button"
                  className={`flex w-full items-center justify-between rounded-md px-2 py-1.5 text-left text-body hover:bg-muted ${checked ? 'bg-muted' : ''}`}
                  onClick={() => onToggle(option.value)}
                >
                  <span className="truncate">{option.label}</span>
                </button>
              )
            }
            return (
              <label key={option.value} className="flex items-center gap-2 text-body px-2 py-1.5 rounded-md hover:bg-muted">
                <Checkbox
                  checked={checked}
                  onCheckedChange={() => onToggle(option.value)}
                />
                <span className="truncate">{option.label}</span>
              </label>
            )
          })}
          {options.length === 0 && (
            <p className="text-body text-fg-tertiary px-2 py-1">
              {loading ? 'Searching…' : 'No matches.'}
            </p>
          )}
        </div>
        {hiddenCount > 0 && (
          <p className="text-body-sm text-fg-tertiary">
            {hiddenCount} more match — keep typing to narrow the list.
          </p>
        )}
      </PopoverContent>
    </Popover>
  )
}
