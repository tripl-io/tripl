import { memo, type ReactNode } from 'react'
import { Link, useInRouterContext } from 'react-router-dom'
import { Ban, Pencil, Trash2 } from 'lucide-react'
import { IconButton } from '@/components/ui/icon-button'
import { Chip } from '@/components/primitives/chip'
import { CodeToken } from '@/components/primitives/code-token'
import { TableCell, TableRow } from '@/components/ui/table'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { SCENARIO_SEEDED } from '@/demo/scenarioModel'
import { countOf } from '@/lib/plural'
import type { Variable } from '@/types'

// Chips past this count collapse into a "+N" counter — a variable with dozens
// of documented or observed values must not dominate the table's node budget.
const MAX_CHIPS = 6
// Up to this many event names render as a plain list; above it they collapse
// into a <details> disclosure so the row stays one line tall.
const MAX_INLINE_EVENTS = 3

export interface VariablesTableRowProps {
  variable: Variable
  typeLabel: string
  selected: boolean
  focused: boolean
  rowRef?: React.Ref<HTMLTableRowElement>
  /** False for a read-only visitor: no selection, exclude or delete. Edit stays,
   * because its dialog is where the drift and observed values are read. */
  canWrite?: boolean
  onToggleSelect: (id: string) => void
  onEdit: (variable: Variable) => void
  onExclude: (variable: Variable) => void
  onDelete: (variable: Variable) => void
  /** Where an "Observed in" event name links (AU-29). Without it — or outside
   * a router — the names are plain text. */
  eventHref?: (eventId: string) => string
  /** Where the `${name}` token links: the variable's own page (AU-26). Without
   * it, or outside a router, the token is plain text. */
  detailHref?: (variableId: string) => string
  /** Where the "On N events" line links: the property's Events tab (F23). */
  listedEventsHref?: (variableId: string) => string
}

function VariablesTableRowImpl({
  variable,
  typeLabel,
  selected,
  focused,
  rowRef,
  canWrite = true,
  onToggleSelect,
  onEdit,
  onExclude,
  onDelete,
  eventHref,
  detailHref,
  listedEventsHref,
}: VariablesTableRowProps) {
  // Everything the row shows ships with the list response — event names and
  // observed values included — so a row costs zero extra requests.
  const inRouter = useInRouterContext()
  // Ids when the server sent them, so each name can open its event (AU-29);
  // names alone otherwise. Same order and cap either way.
  const eventRefs: { id: string | null; name: string }[] =
    variable.event_refs ?? (variable.event_names ?? []).map(name => ({ id: null, name }))
  const eventNames = eventRefs.map(ref => ref.name)
  const eventCount = variable.event_count ?? eventNames.length
  const eventLabel = (ref: { id: string | null; name: string }): ReactNode =>
    ref.id && eventHref && inRouter ? (
      <Link to={eventHref(ref.id)} className="text-accent no-underline hover:underline">
        {ref.name}
      </Link>
    ) : (
      ref.name
    )
  const hiddenEvents = Math.max(0, eventCount - eventNames.length)
  const observedValues = variable.sample_values ?? []
  const contextCount = variable.context_count ?? 0
  const documentedValues = variable.allowed_values ?? []
  const bindings = variable.bindings ?? []
  const driftCount = variable.open_drift_count ?? 0
  // The events whose property list carries it (F23), apart from where scans saw it.
  const listedCount = variable.listed_event_count ?? 0
  const listedLabel = listedCount > 0
    ? `On ${countOf(listedCount, 'event', 'events')} · ${variable.required_event_count ?? 0} required`
    : null

  return (
    <TableRow
      ref={rowRef}
      data-focused={focused || undefined}
      className={focused ? 'bg-primary/5 outline outline-1 outline-primary/40' : undefined}
    >
      <TableCell className="align-top">
        {canWrite && (
          <input
            type="checkbox"
            aria-label={`Select property ${variable.name}`}
            checked={selected}
            onChange={() => onToggleSelect(variable.id)}
          />
        )}
      </TableCell>
      <TableCell className="font-mono text-body-sm align-top">
        {/* Pills never wrap and never shrink; the variable name absorbs the
            squeeze instead. In a ~225px column the drift badge broke inside its
            own pill — "1" on one line, "drift" on the next — which reads as a
            rendering fault on the one signal this page asks a reader to act on.
            `whitespace-nowrap` is the house pattern here; the same
            badge in ScansTab already carries it. */}
        <div className="flex min-w-0 items-center gap-2">
          {detailHref && inRouter ? (
            <Link
              to={detailHref(variable.id)}
              className="min-w-0 truncate rounded-sm no-underline hover:underline"
              aria-label={`Open property ${variable.name}`}
            >
              <code className="rounded-sm bg-primary/10 px-1.5 py-0.5 text-primary" title={`\${${variable.name}}`}>
                {`\${${variable.name}}`}
              </code>
            </Link>
          ) : (
            <code className="min-w-0 truncate rounded-sm bg-primary/10 px-1.5 py-0.5 text-primary" title={`\${${variable.name}}`}>
              {`\${${variable.name}}`}
            </code>
          )}
          {/* The badge taxonomy (DS-6): the type is a kind tag, the drift
              count a warning status. Both pills, in sans. */}
          <Chip variant="outline" size="xs" className="font-mono">
            {typeLabel}
          </Chip>
          {driftCount > 0 && (
            <Chip tone="warning" size="xs" className="font-sans" title="Observed values outside the documented list">
              {driftCount} drift{driftCount === 1 ? '' : 's'}
            </Chip>
          )}
        </div>
        {listedLabel && (
          <div className="mt-1 font-sans text-micro text-fg-tertiary">
            {listedEventsHref && inRouter ? (
              <Link
                to={listedEventsHref(variable.id)}
                className="no-underline hover:underline"
                title="Events whose property list carries this property"
              >
                {listedLabel}
              </Link>
            ) : (
              listedLabel
            )}
          </div>
        )}
        {bindings.length > 0 && (
          <div className="mt-1 space-y-0.5">
            {bindings.map(binding => (
              <div key={binding} className="max-w-52 truncate text-micro text-fg-tertiary" title={binding}>
                ↳ {binding}
              </div>
            ))}
          </div>
        )}
      </TableCell>
      <TableCell className="text-body-sm align-top">
        {eventCount === 0 ? (
          <span className="text-fg-tertiary">—</span>
        ) : eventNames.length <= MAX_INLINE_EVENTS ? (
          // Keyed by position: two event types can each hold an event of the
          // same name, and a duplicate key made React drop one of them (PLAN-33).
          <ul className="space-y-0.5">
            {eventRefs.map((ref, index) => (
              <li key={index}>{eventLabel(ref)}</li>
            ))}
          </ul>
        ) : (
          <details>
            <summary className="cursor-pointer text-fg-tertiary">Seen in {eventCount} events</summary>
            <ul className="mt-1 space-y-0.5">
              {eventRefs.map((ref, index) => (
                <li key={index}>{eventLabel(ref)}</li>
              ))}
              {hiddenEvents > 0 && (
                <li className="text-fg-tertiary">+{hiddenEvents} more</li>
              )}
            </ul>
          </details>
        )}
      </TableCell>
      <TableCell className="text-body-sm text-fg-tertiary align-top">{variable.description}</TableCell>
      <TableCell className="align-top">
        {documentedValues.length > 0 ? (
          <div className="flex max-w-sm flex-wrap gap-1">
            {documentedValues.slice(0, MAX_CHIPS).map(value => (
              <CodeToken key={value} className="max-w-28" title={value}>{value}</CodeToken>
            ))}
            {documentedValues.length > MAX_CHIPS && (
              <span className="text-micro text-fg-tertiary">+{documentedValues.length - MAX_CHIPS}</span>
            )}
          </div>
        ) : (
          <span className="text-body-sm text-fg-tertiary">—</span>
        )}
      </TableCell>
      <TableCell>
        {/* Two unrelated silences used to render the same em-dash: no event
            references this variable at all, and every context that does
            reference it came back with nothing in it. Only the second is worth
            an operator's attention, and this page is where they look — so the
            cell names which one it is. `context_count` already rides along on
            the list row, so saying it costs no request. */}
        {observedValues.length > 0 ? (
          <div className="flex max-w-sm flex-wrap gap-1">
            {observedValues.slice(0, MAX_CHIPS).map(value => (
              <CodeToken key={value} className="max-w-28" title={value}>{value}</CodeToken>
            ))}
            {observedValues.length > MAX_CHIPS && (
              <span className="text-micro text-fg-tertiary">+{observedValues.length - MAX_CHIPS}</span>
            )}
          </div>
        ) : contextCount > 0 ? (
          <span
            className="text-body-sm text-fg-tertiary"
            title={`${contextCount} value context${contextCount === 1 ? '' : 's'}, none holding a value`}
          >
            No values stored
          </span>
        ) : (
          <span className="text-body-sm text-fg-tertiary">—</span>
        )}
      </TableCell>
      {/* Pinned to the right edge like its header, so a phone reader can act
          on a row without first finding the sideways scroll (AU-27). */}
      <TableCell className="sticky right-0 bg-surface">
        <div className="flex gap-1 justify-end">
          {/* Exactly one row carries the inspect mark: the seeded drifting
              variable, so the coaching reads as an example. */}
          <ScenarioCoachMark
            step="variables/inspect-values"
            when={variable.name === SCENARIO_SEEDED.driftVariableName}
          >
            <IconButton variant="ghost" className="h-7 w-7" label={`Edit property ${variable.name}`} tooltip="Edit" onClick={() => onEdit(variable)}>
              <Pencil className="h-3 w-3" aria-hidden="true" />
            </IconButton>
          </ScenarioCoachMark>
          {canWrite && (
            <>
              <IconButton variant="ghost" className="h-7 w-7 text-fg-tertiary hover:text-warning" label={`Exclude property ${variable.name} from scans`} tooltip="Exclude from scans" onClick={() => onExclude(variable)}>
                <Ban className="h-3 w-3" aria-hidden="true" />
              </IconButton>
              <IconButton variant="ghost" className="h-7 w-7 text-fg-tertiary hover:text-destructive" label={`Delete property ${variable.name}`} tooltip="Delete" onClick={() => onDelete(variable)}>
                <Trash2 className="h-3 w-3" aria-hidden="true" />
              </IconButton>
            </>
          )}
        </div>
      </TableCell>
    </TableRow>
  )
}

/** Memoized on purpose: the table renders a page of these and ticking ONE
 * checkbox must not re-render the rest (measured at ~300 ms–3 s per
 * click when every row re-rendered). Props are the variable object straight off
 * the query cache plus primitives and callbacks the parent keeps stable, so
 * reference equality holds between renders. */
export const VariablesTableRow = memo(VariablesTableRowImpl)
