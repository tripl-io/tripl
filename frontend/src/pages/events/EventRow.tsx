import { Fragment, memo, useCallback } from 'react'
import type { ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useSortable } from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { Check, GripVertical, Pencil } from 'lucide-react'
import type {
  EventFieldValue,
  EventListItem,
  EventMetricPoint,
  EventTypeBrief,
  FieldDefinition,
  MetaFieldDefinition,
  MonitoringSignal,
  Variable,
} from '@/types'
import { Checkbox } from '@/components/ui/checkbox'
import { TableCell, TableRow } from '@/components/ui/table'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { Chip } from '@/components/primitives/chip'
import { CodeToken } from '@/components/primitives/code-token'
import { Dot } from '@/components/primitives/dot'
import { EVENT_STATUS_DOT_TONE, EVENT_STATUS_LABELS } from '@/lib/eventStatus'
import type { EventStatus } from '@/lib/eventStatus'
import { eventNameLabel } from '@/lib/eventName'
import { SIGNAL_LEVEL, rowSignalLevel } from '@/lib/statusLexicon'
import { getMonitoringPath } from '@/lib/monitoring'
import { resolveMetaFieldHref } from '@/lib/metaFields'
import { useActiveBranchId, useBranchLinkProps } from '@/hooks/useBranch'
import { VariableValueContextTrigger } from '@/components/variable-value-contexts'
import { EventName } from '@/components/event-name'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { useDemoScenario } from '@/demo/demoScenarioContext'
import { SCENARIO_SEEDED } from '@/demo/scenarioModel'
import { EventWindowMetricsCell } from './EventWindowMetricsCell'
import { PINNED_EVENT_CONTENT_MAX_WIDTH, PINNED_EVENT_CELL_STYLE } from './useEventsTableOverflow'
import {
  PHONE_DROPPED_CELL,
  PHONE_NAME_CELL,
  PHONE_NAME_CONTENT,
  PHONE_QUIET_CELL,
  PHONE_ROW,
  PHONE_SELECT_CELL,
} from './eventsPhoneCard'
import {
  computeWindowDelta,
  describeWindowDelta,
  formatRelativeTime,
  splitTemplateValue,
} from './utils'
import { useCanWriteProject } from '@/lib/permissions'
import { formatDateTime } from '@/lib/datetime'

/** The one action a row dispatches; the table has no per-row menu. */
export type RowAction = 'edit'

function renderTemplateValue(value: string, variables?: Variable[]): ReactNode {
  const parts = splitTemplateValue(value, variables)
  if (parts.length === 1 && !parts[0]?.token) return value
  // A token reads as data, not a link: the quiet sunken CodeToken instead of
  // saturated accent mono, which was the brightest text in the table (EV-13).
  return parts.map((part, i) =>
    part.token ? (
      part.known === false ? (
        // Warning tone = the ${token} resolves to no variable (name,
        // source_name or binding) — it will never receive observed values.
        <CodeToken key={i} className="text-warning" title="Unknown variable token">
          {part.text}
        </CodeToken>
      ) : (
        <CodeToken key={i} className="text-fg-secondary" title="Variable: filled in from observed values">
          {part.text}
        </CodeToken>
      )
    ) : (
      <Fragment key={i}>{part.text}</Fragment>
    ),
  )
}

/** Clicks on these inside a row keep their own meaning; the rest open the event. */
const ROW_INTERACTIVE = 'a, button, input, label, select, textarea, [role="checkbox"], [role="button"], [data-no-row-click]'

/**
 * The newest bucket with volume, for a row whose `last_seen_at` is unset but
 * whose 48h series has events: "never" beside thousands of events broke trust
 * in the whole row (EV-8).
 */
function lastBucketWithVolume(points: EventMetricPoint[]): string | null {
  let latest: string | null = null
  let latestAt = Number.NEGATIVE_INFINITY
  for (const point of points) {
    if (!(point.count > 0)) continue
    const at = Date.parse(point.bucket)
    if (Number.isFinite(at) && at > latestAt) {
      latestAt = at
      latest = point.bucket
    }
  }
  return latest
}

/** A drop at least this deep reads as a possible tracking break. */
const DELTA_DROP_PCT = -50
/** A rise at least this large (volume doubled) is worth a second look. */
const DELTA_RISE_PCT = 100

/**
 * The Δ figure's colour from its own sign and size. Small moves stay muted, so
 * the column does not read "everything is dropping"; a halving is danger (the
 * classic sign of a broken integration) and a doubling is warning.
 */
function deltaColor(pct: number): string {
  if (pct <= DELTA_DROP_PCT) return 'var(--danger)'
  if (pct >= DELTA_RISE_PCT) return 'var(--warning)'
  return 'var(--fg-muted)'
}

// A muted em-dash placeholder for a cell with no value. The `title` keeps the
// bare "—" from reading as broken and lets it be told apart from a real 0.
function NoData({ title, className }: { title: string; className?: string }): ReactNode {
  return (
    <span className={className ?? 'text-caption'} style={{ color: 'var(--fg-faint)' }} title={title}>
      —
    </span>
  )
}

export type EventRowProps = {
  ev: EventListItem
  eventType: EventTypeBrief | undefined
  selected: boolean
  hideType: boolean
  hideStatus: boolean
  hideReviewed: boolean
  hideMonitor: boolean
  hideOwner: boolean
  hideDelta: boolean
  usersById: Map<string, { name: string | null; email: string }>
  hideTags: boolean
  hideLastSeen: boolean
  fieldColumns: FieldDefinition[]
  metaFields: MetaFieldDefinition[]
  variables?: Variable[]
  slug: string
  expandedFieldId: string | null
  rowSignal: MonitoringSignal | undefined
  windowTotal: number | undefined
  windowData: EventMetricPoint[]
  /** The 48h metrics for this row have not answered yet: its cells show a
   *  placeholder, not the "—" that means "no data" (EV-20). */
  metricsPending?: boolean
  /** Field id → every value this row holds for it, in API order. */
  metaValueMap: Map<string, string[]> | undefined
  getFieldValue: (ev: EventListItem, f: FieldDefinition) => string
  /** The value row behind `getFieldValue` — same lookup, carrying the contexts. */
  getFieldValueRow: (ev: EventListItem, f: FieldDefinition) => EventFieldValue | undefined
  onToggleSelected: (id: string, checked: boolean) => void
  onToggleExpanded: (cellKey: string | null) => void
  onRowAction: (action: RowAction, ev: EventListItem) => void
  /**
   * The rows are in catalog order, so dragging one means something. False
   * under "Busiest first", where a drag would renumber the catalog into volume
   * order (EVT-3); the handle is not offered then.
   */
  reorderable?: boolean
  /** Virtualizer hooks: measure this row's real height at this index. */
  measureRef?: (el: HTMLTableRowElement | null) => void
  virtualIndex?: number
  /** Created by the form the reader just left: marked so it can be found in a
   *  long list (AU-20, AU-21, JR-13). */
  justCreated?: boolean
}

export const EventRow = memo(function EventRow({
  ev,
  eventType,
  selected,
  hideType,
  hideStatus,
  hideReviewed,
  hideMonitor,
  hideOwner,
  hideDelta,
  usersById,
  hideTags,
  hideLastSeen,
  fieldColumns,
  metaFields,
  variables,
  slug,
  expandedFieldId,
  rowSignal,
  windowTotal,
  windowData,
  metricsPending = false,
  metaValueMap,
  getFieldValue,
  getFieldValueRow,
  onToggleSelected,
  onToggleExpanded,
  onRowAction,
  reorderable = true,
  measureRef,
  virtualIndex,
  justCreated = false,
}: EventRowProps) {
  // Reorder, select-for-bulk and edit are all editor actions; a viewer gets
  // the row without them (the cells stay, so the columns line up).
  const canWrite = useCanWriteProject()
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: ev.id, disabled: !canWrite || !reorderable })
  const rowRef = useCallback(
    (el: HTMLTableRowElement | null) => {
      setNodeRef(el)
      measureRef?.(el)
    },
    [setNodeRef, measureRef],
  )
  const dragStyle: React.CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.6 : undefined,
    position: 'relative',
    zIndex: isDragging ? 1 : undefined,
  }
  // One incident, one saturated indicator: the Signal-cell chip is the
  // single act-on-me affordance. The name-cell dot stays on the (orthogonal)
  // lifecycle status, the SignalLink arrow is dropped, and the volume sparkline
  // is neutralised when a signal is live — so a single anomaly no longer reads
  // as four stacked red marks across the row.
  const signalLevel = rowSignal ? rowSignalLevel(rowSignal.state) : null
  const historicalAnomalyIdx = windowData.findIndex((p) => p.is_anomaly)
  // Suppress the red sparkline dot while a signal is live (the chip already
  // carries it); keep it for rows whose only cue is a past-window anomaly.
  const sparklineAnomalyIdx = rowSignal
    ? null
    : historicalAnomalyIdx >= 0
      ? historicalAnomalyIdx
      : null
  const statusTone = EVENT_STATUS_DOT_TONE[(ev.status as EventStatus) ?? 'draft'] ?? 'neutral'
  // Every place this row puts the name into a STRING — an aria-label, a native
  // title, the sparkline's labels. A blank name made those read "Select " and
  // "Edit " with a trailing space (tripl-wkwv.5). The link's own text keeps the
  // RAW name, because <EventName> needs it to paint ∅ empty segments.
  const nameLabel = eventNameLabel(ev.name)

  // The detail link carries the ACTIVE branch, not just the path. The provider
  // reads `?branch=` only when it mounts, so a row link copied out of a branch
  // catalog without it opened a 404 in a fresh session — the event only exists
  // on that branch (tripl-kjhi.7). useBranchLinkProps bundles the query param
  // with the on-click branch set, so in-app and pasted navigation agree.
  const activeBranchId = useActiveBranchId()
  const branchLink = useBranchLinkProps()
  const detailLink = branchLink(
    getMonitoringPath(slug, { scope_type: 'event', scope_ref: ev.id }),
    activeBranchId,
  )

  // The whole row opens the event's detail page, not just the name (EV-27).
  // The name stays the real anchor (new tab, copy link); a click on anything
  // interactive in the row, or one that ends a text selection, keeps its own
  // meaning.
  const navigate = useNavigate()
  const onRowClick = (event: React.MouseEvent<HTMLTableRowElement>) => {
    if (event.defaultPrevented || event.button !== 0) return
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    // React bubbles a click through a portal along the component tree, so a
    // click inside a cell's popover (rendered under <body>) reaches this row
    // too. Only a click on the row's own DOM opens the event.
    if (!(event.target instanceof Node) || !event.currentTarget.contains(event.target)) return
    const target = event.target as HTMLElement
    if (target.closest(ROW_INTERACTIVE)) return
    if (window.getSelection()?.toString()) return
    detailLink.onClick()
    void navigate(detailLink.to)
  }

  // The edit affordance is dimmed until the row is hovered, but the row the
  // coached scenario points at shows it at full strength while the edit-event
  // chapter's mark is on it (context bypasses the memo, and outside a demo the
  // inert context never matches).
  const { active: scenarioActive, step: scenarioStep, hintsMuted } = useDemoScenario()
  const coachEdit =
    scenarioActive &&
    !hintsMuted &&
    scenarioStep.id === 'edit-event/open-editor' &&
    ev.name === SCENARIO_SEEDED.editedEventName

  return (
    <TableRow
      ref={rowRef}
      data-index={virtualIndex}
      style={dragStyle}
      data-state={selected ? 'selected' : undefined}
      data-created={justCreated || undefined}
      className={`group/row cursor-pointer ${PHONE_ROW}${justCreated ? ' bg-success-soft' : ''}`}
      onClick={onRowClick}
    >
      {/* The handle and checkbox cells are hit targets of their own: a click
          that misses the small control inside must not open the event. */}
      <TableCell className="w-8 px-1" data-no-row-click={canWrite || undefined}>
        {canWrite && reorderable && (
          // Hover-revealed only where the pointer can hover: on a touch screen
          // an invisible handle cannot be found at all (EVT-21).
          <button
            type="button"
            className="flex h-6 w-6 cursor-grab touch-none items-center justify-center rounded-sm text-fg-tertiary opacity-0 transition-opacity hover:bg-muted focus-visible:opacity-100 group-hover/row:opacity-100 group-focus-within/row:opacity-100 pointer-coarse:opacity-100 active:cursor-grabbing"
            aria-label={`Drag to reorder ${nameLabel}`}
            {...attributes}
            {...listeners}
          >
            <GripVertical className="h-3.5 w-3.5" />
          </button>
        )}
      </TableCell>
      <TableCell
        className={`tripl-pin-l w-10 pl-5 ${PHONE_SELECT_CELL}`}
        data-no-row-click={canWrite || undefined}
      >
        {canWrite && (
          <Checkbox
            checked={selected}
            onCheckedChange={(checked) => onToggleSelected(ev.id, checked === true)}
            aria-label={`Select ${nameLabel}`}
          />
        )}
      </TableCell>
      <TableCell
        className={`tripl-pin-l border-r font-medium ${PHONE_NAME_CELL}`}
        style={{ ...PINNED_EVENT_CELL_STYLE, borderColor: 'var(--border-subtle)' }}
      >
        {/* Capped, so the name, title and badges truncate. Cells never wrap
            and auto table layout sizes a cell to its content, so a 120-char
            scan-generated name made the sticky cluster wider than a phone and
            every other column scrolled underneath it (EVT-7). The cap sits on
            this box, not the cell: browsers ignore max-width on table cells. */}
        <div
          className={`flex items-center gap-2 align-middle ${PHONE_NAME_CONTENT}`}
          style={{ maxWidth: PINNED_EVENT_CONTENT_MAX_WIDTH }}
        >
          {/* Only when the Status column is hidden: beside it the dot said the
              same thing again, in colours close to the type colours (EV-9). */}
          {hideStatus && (
            <Dot
              tone={statusTone}
              pulse={false}
              size={6}
              label={`Status: ${EVENT_STATUS_LABELS[(ev.status as EventStatus) ?? 'draft'] ?? ev.status}`}
            />
          )}
          <Tooltip>
            <TooltipTrigger asChild>
              {/* A real anchor, not a button: triaging a 2641-event catalog
                  means opening rows in background tabs, and cmd/ctrl/middle
                  click, "copy link address" and the status-bar preview all
                  need an href. react-router's Link leaves modified clicks to
                  the browser (tripl-fa8l). */}
              <Link
                to={detailLink.to}
                onClick={detailLink.onClick}
                // Sans, not mono: "Home Screen View" is a display name, not
                // code. Mono made the pinned column ~25% wider and set one
                // entity in a different face from every other page (DS-17 /
                // EV-10). The cell's font-medium carries the weight.
                className="min-w-0 truncate text-left text-body-sm hover:underline underline-offset-4"
                // Native title only when there's no description to show in the
                // richer tooltip — avoids a double (native + Radix) popover.
                title={ev.description ? undefined : nameLabel}
              >
                <EventName name={ev.name} />
              </Link>
            </TooltipTrigger>
            {ev.description && (
              <TooltipContent side="bottom" align="start" className="max-w-xs whitespace-normal">
                {ev.description}
              </TooltipContent>
            )}
          </Tooltip>
          {/* The free-text title beside the identity (tripl-kjhi.3). Inline, not
              a second line: the virtualizer sizes every row to ROW_H_ESTIMATE
              and never measures (useEventsTableVirtualization), so a taller
              titled row would shift every row under it. Rendered only when set,
              so an untitled row gains no blank gap either. */}
          {ev.title && (
            <span
              className="min-w-0 truncate text-caption text-fg-tertiary"
              title={ev.title}
            >
              {ev.title}
            </span>
          )}
          {/* The pinned cell paints its own background over the row's tint, so
              the mark that survives the sticky column is a word, not a colour. */}
          {justCreated && (
            <Chip tone="success" size="xs">
              New
            </Chip>
          )}
          {/* An unanswered question on this event's discussion. A count, not a
              dot: the filter beside it says "open questions", and a marker that
              cannot say how many leaves the operator guessing whether the row
              matched for one reason or several (tripl-h2sx.26). */}
          {(ev.open_question_count ?? 0) > 0 && (
            <Chip
              size="xs"
              className="tnum"
              title={`${ev.open_question_count} unanswered question${ev.open_question_count === 1 ? '' : 's'} in the discussion`}
            >
              ?{ev.open_question_count}
            </Chip>
          )}
          {/* An open lifecycle finding: a deprecated event still sending past
              its sunset date, or a successor that has gone silent (#258). The
              list only says that one exists; the event page names it. The
              title is for the pointer only, so the chip carries its own
              sentence for a screen reader instead of a bare "Lifecycle". */}
          {ev.lifecycle_warning && (
            <Chip
              tone="warning"
              size="xs"
              data-testid="event-lifecycle-chip"
              title="Lifecycle warning: past its sunset date and still receiving data, or a successor with no data in 7 days. Open the event for details."
            >
              <span aria-hidden="true">Lifecycle</span>
              <span className="sr-only">
                Lifecycle warning: sunset overdue or successor silent. Open the event for details.
              </span>
            </Chip>
          )}
          {canWrite && (
            <ScenarioCoachMark step="edit-event/open-editor" when={coachEdit}>
              <button
                type="button"
                onClick={() => onRowAction('edit', ev)}
                aria-label={`Edit ${nameLabel}`}
                // Always there, quiet until the row is hovered or focused: a
                // hover-only pencil was the one way to the edit form (EV-27).
                className={`flex size-6 shrink-0 items-center justify-center rounded-sm text-fg-tertiary transition-opacity hover:bg-muted focus-visible:opacity-100 group-hover/row:opacity-100 group-focus-within/row:opacity-100 pointer-coarse:opacity-100 ${
                  coachEdit ? 'opacity-100' : 'opacity-40'
                }`}
              >
                <Pencil className="size-3" aria-hidden="true" />
              </button>
            </ScenarioCoachMark>
          )}
        </div>
      </TableCell>
      {!hideMonitor && (
        <TableCell className={rowSignal ? undefined : PHONE_QUIET_CELL}>
          {/* "Open"/"Recent", never "Live": Live is the lifecycle status in
              green one column over, and one word must map to one tone
              (EV-5 / DS-7). The label comes from SIGNAL_LEVEL. With no open
              signal the cell is a faint dash: a "Monitored" pill on 16 of 17
              rows drowned the one chip the column exists for (EV-6); the
              coverage stays in the dash's title. A phone card drops the
              quiet cell: with no column over it the dash was a stray mark. */}
          {rowSignal ? (
            <Chip tone={signalLevel?.tone ?? 'danger'} size="xs">
              {signalLevel?.label ?? SIGNAL_LEVEL.firing.label}
            </Chip>
          ) : (
            <NoData
              title={
                ev.monitored
                  ? 'No open signal. A monitor (alert rule) covers this event.'
                  : 'No open signal, and no monitor (alert rule) covers this event'
              }
            />
          )}
        </TableCell>
      )}
      <TableCell className="w-32 text-right">
        <div className="flex items-center justify-end align-middle">
          <EventWindowMetricsCell
            eventName={nameLabel}
            color={eventType?.color}
            totalCount={windowTotal}
            data={windowData}
            anomalyIdx={sparklineAnomalyIdx}
            signalTone={null}
            pending={metricsPending}
          />
        </div>
      </TableCell>
      {!hideDelta && (
        // Kept on a phone card, beside the count: it is the row's trend (EV-28).
        <TableCell className="tnum text-right text-caption">
          {(() => {
            if (metricsPending) {
              return (
                <span
                  aria-hidden="true"
                  className="ml-auto block h-3 w-8 animate-pulse rounded-sm bg-surface-hover motion-reduce:animate-none"
                />
              )
            }
            const delta = computeWindowDelta(windowData)
            // One sentence for every outcome, naming what was compared and how
            // much of each 24h window was there to compare (tripl-oooj).
            const title = describeWindowDelta(delta)
            const pct = delta.pct
            if (pct == null) {
              return <NoData title={title} />
            }
            // Coloured by the figure itself, never by the row's signal: a
            // spike signal beside a -5% figure painted the -5% red and made
            // the two contradict louder (EV-7). Volume change is not good or
            // bad by itself, so only a large move is toned.
            const color = deltaColor(pct)
            return (
              <span
                style={{ color }}
                title={title}
                // A window the series does not fully cover is marked with a
                // dotted underline and explained by the title, instead of an
                // asterisk on nearly every row (EV-7).
                className={delta.partial ? 'underline decoration-dotted underline-offset-2' : undefined}
                data-partial={delta.partial || undefined}
              >
                {pct >= 0 ? '+' : ''}
                {pct.toFixed(0)}%
              </span>
            )
          })()}
        </TableCell>
      )}
      {!hideLastSeen && (() => {
        // Unset `last_seen_at` next to 48h volume said "never" beside thousands
        // of events: the two come from different sources. Fall back to the
        // newest bucket of the series the 48h column draws (EV-8).
        const seenInSeries = ev.last_seen_at ? null : lastBucketWithVolume(windowData)
        const seenAt = ev.last_seen_at ?? seenInSeries
        return (
          <TableCell
            className={`text-caption tnum ${PHONE_DROPPED_CELL}`}
            style={{ color: seenAt ? 'var(--fg-subtle)' : 'var(--fg-faint)' }}
            // Humanized like every other instant in the app (DS-25), not the
            // raw ISO string.
            title={
              ev.last_seen_at
                ? formatDateTime(ev.last_seen_at)
                : seenInSeries
                  ? `Latest volume in the collected 48h series: ${formatDateTime(seenInSeries)}`
                  : 'Never observed in collected metrics'
            }
          >
            {formatRelativeTime(seenAt)}
          </TableCell>
        )
      })()}
      {!hideStatus && (
        <TableCell>
          <Chip tone={statusTone} size="xs">
            {EVENT_STATUS_LABELS[(ev.status as EventStatus) ?? 'draft'] ?? ev.status}
          </Chip>
        </TableCell>
      )}
      {!hideType && (
        <TableCell>
          {/* display_name, not `name`: the sidebar, the page heading and
              Settings all call these types "Pageview"/"Structured Event", and
              only this chip answered with the internal key ("pv"/"se") — an
              undocumented two-letter mapping the reader had to learn, with the
              colour dot unable to help when all types share one colour
              (tripl-w9od). Truncated with a title so the wider label does not
              push more columns off-screen. */}
          <Chip size="xs" title={eventType?.display_name ?? eventType?.name ?? undefined}>
            <span
              className="inline-block h-1.5 w-1.5 shrink-0 rounded-full"
              style={{ backgroundColor: eventType?.color }}
            />
            <span className="max-w-[14ch] truncate">
              {eventType?.display_name ?? eventType?.name ?? ''}
            </span>
          </Chip>
        </TableCell>
      )}
      {!hideReviewed && (
        <TableCell
          className={`text-center ${PHONE_DROPPED_CELL}`}
          aria-label={ev.reviewed ? 'Verified' : 'Not verified'}
        >
          {ev.reviewed ? (
            <Check
              className="mx-auto h-3.5 w-3.5 text-success"
              aria-hidden="true"
            />
          ) : (
            <span
              aria-hidden="true"
              className="text-caption text-fg-tertiary"
              title="Not verified"
            >
              —
            </span>
          )}
        </TableCell>
      )}
      {!hideTags && (
        <TableCell>
          <div className="flex flex-wrap gap-1">
            {ev.tags.map((t) => (
              <Chip key={t.id} size="xs">{t.name}</Chip>
            ))}
            {ev.tags.length === 0 && <NoData title="No tags" />}
          </div>
        </TableCell>
      )}
      {!hideOwner && (
        <TableCell className={`text-caption ${PHONE_DROPPED_CELL}`}>
          {(() => {
            const u = ev.owner_id ? usersById.get(ev.owner_id) : undefined
            return u ? (
              <span className="text-fg-tertiary">{u.name ?? u.email}</span>
            ) : (
              <NoData title="No owner" />
            )
          })()}
        </TableCell>
      )}
      {fieldColumns.map((f) => {
        // The text and the popover must name the SAME value row, so both come
        // from the one lookup (useEventsFiltering). Re-deriving the row here by
        // id alone silently dropped the popover on the "All" tab, where a column
        // belongs to whichever event type was deduped first (tripl-xv77.1).
        const fieldValue = getFieldValueRow(ev, f)
        const val = getFieldValue(ev, f)
        const cellKey = `${ev.id}-${f.id}`
        const isExpanded = expandedFieldId === f.id
        const isLong = typeof val === 'string' && val.length > 30
        return (
          <TableCell
            key={f.id}
            className={`text-body-sm ${isExpanded ? '' : 'max-w-40'} ${PHONE_DROPPED_CELL}`}
          >
            {isExpanded ? (
              <div className="flex items-start gap-1.5">
                <button
                  type="button"
                  className="block min-w-0 text-left"
                  onClick={() => onToggleExpanded(cellKey)}
                  aria-expanded={true}
                  aria-label={`Collapse ${f.display_name}`}
                >
                  <pre className="max-w-sm whitespace-pre-wrap break-all font-mono text-caption">{renderTemplateValue((() => {
                    try { return JSON.stringify(JSON.parse(val), null, 2) } catch { return val }
                  })(), variables)}</pre>
                </button>
                <VariableValueContextTrigger contexts={fieldValue?.variable_values} />
              </div>
            ) : isLong ? (
              <span className="flex min-w-0 items-center gap-1.5">
                <button
                  type="button"
                  className="block min-w-0 truncate text-left"
                  onClick={() => onToggleExpanded(cellKey)}
                  aria-expanded={false}
                  aria-label={`Expand ${f.display_name}`}
                >
                  {renderTemplateValue(val, variables)}
                </button>
                <VariableValueContextTrigger contexts={fieldValue?.variable_values} />
              </span>
            ) : (
              <span className="flex min-w-0 items-center gap-1.5">
                {val === '' ? (
                  <NoData title="No data" className="min-w-0 text-caption" />
                ) : (
                  <span
                    className="min-w-0"
                    style={val === '0' ? { color: 'var(--fg-faint)' } : undefined}
                  >
                    {renderTemplateValue(val, variables)}
                  </span>
                )}
                <VariableValueContextTrigger contexts={fieldValue?.variable_values} />
              </span>
            )}
          </TableCell>
        )
      })}
      {metaFields.map((mf) => {
        // Every value gets its own rendering. A field with `allow_multiple`
        // holds several, and joining them first would hand the link template a
        // string it wraps into one broken address (tripl-h2sx.31).
        const values = metaValueMap?.get(mf.id) ?? []
        const first = values[0] ?? ''
        return (
          <TableCell
            key={mf.id}
            className={`text-fg-tertiary max-w-40 truncate text-body-sm ${PHONE_DROPPED_CELL}`}
          >
            {values.length === 0 ? (
              <NoData title="No data" />
            ) : mf.field_type === 'boolean' ? (
              <Chip tone={first === 'true' ? 'success' : 'neutral'} size="xs">
                {first === 'true' ? 'Yes' : 'No'}
              </Chip>
            ) : (
              <span
                className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5"
                title={values.join(', ')}
              >
                {values.map(value => {
                  const href = resolveMetaFieldHref(mf, value)
                  return href ? (
                    <a
                      key={value}
                      href={href}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="truncate text-primary underline-offset-4 hover:underline"
                    >
                      {value}
                    </a>
                  ) : (
                    <span key={value} className="truncate">{value}</span>
                  )
                })}
              </span>
            )}
          </TableCell>
        )
      })}
    </TableRow>
  )
})
