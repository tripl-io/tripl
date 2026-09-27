import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { DndContext } from '@dnd-kit/core'
import { SortableContext } from '@dnd-kit/sortable'
import type {
  EventFieldValue,
  EventFieldVariableValue,
  EventListItem,
  EventMetricPoint,
  EventTypeBrief,
  FieldDefinition,
  MetaFieldDefinition,
  MonitoringSignal,
  Variable,
} from '@/types'
import { TooltipProvider } from '@/components/ui/tooltip'
import { BranchContext } from '@/components/branch-context-internal'
import { formatDateTime } from '@/lib/datetime'
import { EventRow } from './EventRow'
import { resolveFieldValue, resolveFieldValueRow } from './useEventsFiltering'

const HOUR_MS = 60 * 60 * 1000
const LATEST = Date.parse('2026-06-10T23:00:00Z')

// 48h hourly series: prior 24h at `priorPerHour`, recent 24h at `recentPerHour`.
// expected_count stays null (non-anomaly) — the delta must come from raw volume.
//
// Anchored on the clock the row renders against, not on a frozen date: the Δ
// cell splits the series on NOW, because "Δ · 24h" is a claim about the last 24
// hours (tripl-oooj). A fixture pinned to LATEST would sit outside both windows
// and every delta assertion here would pass on an em dash.
function windowSeries(
  priorPerHour: number,
  recentPerHour: number,
  endsAt = Date.now(),
): EventMetricPoint[] {
  const points: EventMetricPoint[] = []
  for (let hoursAgo = 47; hoursAgo >= 0; hoursAgo -= 1) {
    points.push({
      bucket: new Date(endsAt - hoursAgo * HOUR_MS).toISOString(),
      count: hoursAgo < 24 ? recentPerHour : priorPerHour,
      expected_count: null,
      stddev: null,
      is_anomaly: false,
      anomaly_direction: null,
      z_score: null,
    })
  }
  return points
}

const EVENT_TYPE: EventTypeBrief = {
  id: 'et-1',
  name: 'pv',
  display_name: 'Page View',
  color: '#3355ff',
}

function makeEvent(overrides: Partial<EventListItem> = {}): EventListItem {
  return {
    id: 'evt-1',
    project_id: 'proj-1',
    event_type_id: 'et-1',
    name: 'checkout_completed',
    // null, not the name: an event no scan has claimed carries no identity, and
    // a double that stamped one would assert the wrong world is normal.
    source_name: null,
    // Empty by default: most rows carry no free-text title, and the row must
    // not reserve space for one (tripl-kjhi.3).
    title: '',
    description: '',
    order: 0,
    status: 'implemented',
    sunset_at: null,
    last_seen_at: null,
    owner_id: null,
    reviewed: false,
    metric_breakdown_columns: [],
    drift_count: 0,
    monitored: false,
    tags: [],
    field_values: [],
    meta_values: [],
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
    ...overrides,
  }
}

function makeSignal(overrides: Partial<MonitoringSignal> = {}): MonitoringSignal {
  return {
    scan_config_id: 'scan-1',
    scope_type: 'event',
    scope_ref: 'evt-1',
    state: 'latest_scan',
    event_id: 'evt-1',
    event_type_id: 'et-1',
    bucket: new Date(LATEST).toISOString(),
    actual_count: 480,
    expected_count: 120,
    stddev: 40,
    z_score: 9,
    direction: 'spike',
    incident_child: false,
    unit: null,
    detected_at: null,
    ...overrides,
  }
}

function renderRow(
  ev: EventListItem,
  windowData: EventMetricPoint[],
  rowSignal?: MonitoringSignal,
  {
    variables = [] as Variable[],
    fieldColumns = [] as FieldDefinition[],
    getFieldValue = () => '',
    getFieldValueRow = () => undefined,
    branchId = null,
    setBranchId = () => {},
    metaFields = [] as MetaFieldDefinition[],
    metaValueMap,
    reorderable,
    justCreated,
  }: {
    reorderable?: boolean
    justCreated?: boolean
    variables?: Variable[]
    fieldColumns?: FieldDefinition[]
    metaFields?: MetaFieldDefinition[]
    /** Every value this row holds per meta field — a list, so a field with
     *  `allow_multiple` renders one link per value. */
    metaValueMap?: Map<string, string[]>
    getFieldValue?: (event: EventListItem, field: FieldDefinition) => string
    getFieldValueRow?: (event: EventListItem, field: FieldDefinition) => EventFieldValue | undefined
    /** Active branch the row is rendered under; null (the default) is main. */
    branchId?: string | null
    setBranchId?: (next: string | null) => void
  } = {},
) {
  return render(
    <MemoryRouter>
      <RowLocationProbe />
      <BranchContext.Provider value={{ branchId, setBranchId, slug: 'proj-1' }}>
      <TooltipProvider>
        <DndContext>
          <SortableContext items={[ev.id]}>
            <table>
              <tbody>
                <EventRow
                  ev={ev}
                  eventType={EVENT_TYPE}
                  selected={false}
                  hideType={false}
                  hideStatus={false}
                  hideReviewed={false}
                  hideMonitor={false}
                  hideOwner={false}
                  hideDelta={false}
                  usersById={new Map()}
                  hideTags={false}
                  hideLastSeen={false}
                  fieldColumns={fieldColumns}
                  metaFields={metaFields}
                  variables={variables}
                  slug="proj-1"
                  expandedFieldId={null}
                  rowSignal={rowSignal}
                  windowTotal={windowData.length}
                  windowData={windowData}
                  metaValueMap={metaValueMap}
                  getFieldValue={getFieldValue}
                  getFieldValueRow={getFieldValueRow}
                  onToggleSelected={() => {}}
                  onToggleExpanded={() => {}}
                  onRowAction={() => {}}
                  reorderable={reorderable}
                  justCreated={justCreated}
                />
              </tbody>
            </table>
          </SortableContext>
        </DndContext>
      </TooltipProvider>
      </BranchContext.Provider>
    </MemoryRouter>,
  )
}

/** Where a row click navigated to. */
function RowLocationProbe() {
  const location = useLocation()
  return (
    <span data-testid="row-location" hidden>
      {location.pathname}
    </span>
  )
}

const TEMPLATE_FIELD = {
  id: 'field-variant',
  event_type_id: 'et-1',
  name: 'variant',
  display_name: 'Variant',
  field_type: 'string',
  is_required: false,
  enum_options: null,
  order: 0,
} as unknown as FieldDefinition

const TEMPLATE_VARIABLE: Variable = {
  id: 'var-1',
  project_id: 'proj-1',
  name: 'variant',
  source_name: null,
  variable_type: 'string',
  description: '',
  allowed_values: [],
  bindings: ['payload.variant'],
}

describe('EventRow Δ · 24h and Signal cells', () => {
  it('renders a populated 24h delta (not a dash) from the window series', () => {
    // prior 24h = 24 * 10 = 240, recent 24h = 24 * 20 = 480 → +100%.
    renderRow(makeEvent(), windowSeries(10, 20))
    const cell = screen.getByText('+100%')
    expect(cell).toBeInTheDocument()
    // Both windows are whole, so the incomplete-window marker stays off.
    expect(cell).not.toHaveTextContent('*')
  })

  // tripl-oooj: the fresh demo's own payload. Collection ends ~2h before now, so
  // the series spans 46h rather than 47h — and the span guard blanked the whole
  // column on it, while the same points carried a sound double-digit delta. The
  // number is shown and marked; the tooltip states what is actually covered.
  it('prints a marked delta, not a dash, when the series ends before now', () => {
    const endsAt = Date.now() - 2 * HOUR_MS
    const lagging: EventMetricPoint[] = []
    for (let hoursAgo = 45; hoursAgo >= 0; hoursAgo -= 1) {
      lagging.push({
        // Ages 2h…47h relative to now: 22 hourly buckets land in the last 24h,
        // the other 24 in the 24h before it.
        bucket: new Date(endsAt - hoursAgo * HOUR_MS).toISOString(),
        count: hoursAgo < 22 ? 2400 : 2000,
        expected_count: null,
        stddev: null,
        is_anomaly: false,
        anomaly_direction: null,
        z_score: null,
      })
    }
    renderRow(makeEvent(), lagging)

    // recent = 22 * 2400 = 52,800 vs prior = 24 * 2000 = 48,000 → +10%.
    // Marked with a dotted underline, not an asterisk on every row (EV-7).
    const cell = screen.getByText('+10%')
    expect(cell).not.toHaveTextContent('*')
    expect(cell).toHaveAttribute('data-partial', 'true')
    expect(cell).toHaveClass('decoration-dotted')
    const title = cell.getAttribute('title') ?? ''
    expect(title).toContain('Last 24h 52,800 vs 48,000 in the 24h before it')
    expect(title).toContain('the last 24h are covered to 22 of 24 hours')
    expect(title).toContain('the series ends 2h before now')
    // The sentence this replaces was asserted for every blank cell, including
    // this one — where the prior window holds 48,000 events.
    expect(screen.queryByTitle(/No prior 24h window/)).not.toBeInTheDocument()
  })

  it('keeps a small move muted (EV-7)', () => {
    // prior 240 vs recent 216 → -10%.
    renderRow(makeEvent(), windowSeries(10, 9))
    expect(screen.getByText('-10%')).toHaveStyle({ color: 'var(--fg-muted)' })
  })

  it('tones the delta by its own sign and size, not by the row signal (EV-7)', () => {
    // A spike signal beside a -10% figure: the figure stays muted instead of
    // turning the signal's colour and contradicting it.
    const { unmount } = renderRow(makeEvent(), windowSeries(10, 9), makeSignal())
    expect(screen.getByText('-10%')).toHaveStyle({ color: 'var(--fg-muted)' })
    unmount()
    // A halving reads as a possible tracking break, signal or not.
    renderRow(makeEvent(), windowSeries(20, 10))
    expect(screen.getByText('-50%')).toHaveStyle({ color: 'var(--danger)' })
  })

  it('tones a doubling as warning (EV-7)', () => {
    renderRow(makeEvent(), windowSeries(10, 20))
    expect(screen.getByText('+100%')).toHaveStyle({ color: 'var(--warning)' })
  })

  it('keeps a covered but quiet row to a dash, with the coverage in its title (EV-6)', () => {
    renderRow(makeEvent({ monitored: true }), windowSeries(10, 20))
    expect(screen.queryByText('Monitored')).not.toBeInTheDocument()
    expect(
      screen.getByTitle('No open signal. A monitor (alert rule) covers this event.'),
    ).toBeInTheDocument()
  })

  it('shows an em-dash Signal cell when the event is not covered', () => {
    renderRow(makeEvent({ monitored: false }), windowSeries(10, 20))
    expect(screen.queryByText('Monitored')).not.toBeInTheDocument()
    expect(
      screen.getByTitle('No open signal, and no monitor (alert rule) covers this event'),
    ).toBeInTheDocument()
  })
})

describe('EventRow name and type cells', () => {
  // tripl-fa8l: an href is what makes cmd/middle-click, "copy link address" and
  // the status-bar preview work; an onClick-only <button> offered none of them.
  it('renders the event name as a link to its monitoring page', () => {
    renderRow(makeEvent(), windowSeries(10, 20))

    const link = screen.getByRole('link', { name: 'checkout_completed' })
    expect(link).toHaveAttribute('href', '/p/proj-1/monitoring/event/evt-1')
  })

  // tripl-wkwv.5: windy-ios holds one event whose name is the empty string. The
  // anchor's only child was <EventName name="">, which rendered nothing — a
  // zero-width click target with no accessible name, on the one row a user would
  // most want to open in order to rename or archive it.
  it('keeps a clickable, announceable link when the event has no name', () => {
    renderRow(makeEvent({ name: '' }), windowSeries(10, 20))

    const link = screen.getByRole('link', { name: '(unnamed event)' })
    expect(link).toHaveAttribute('href', '/p/proj-1/monitoring/event/evt-1')
    // The row's other controls were labelled "Select " and "Edit " — a trailing
    // space and nothing else.
    expect(screen.getByLabelText('Select (unnamed event)')).toBeInTheDocument()
    expect(screen.getByLabelText('Edit (unnamed event)')).toBeInTheDocument()
    expect(screen.getByLabelText('Drag to reorder (unnamed event)')).toBeInTheDocument()
  })

  // tripl-w9od: the sidebar and Settings call this type "Page View"; the table
  // answered with the internal key "pv" and no legend anywhere.
  it('badges the type with its display name, not its internal key', () => {
    renderRow(makeEvent(), windowSeries(10, 20))

    expect(screen.getByText('Page View')).toBeInTheDocument()
    expect(screen.queryByText('pv')).not.toBeInTheDocument()
  })

  // tripl-kjhi.7: a row link copied out of a branch catalog carried no
  // `?branch=`, so it opened a 404 in a fresh session — the event only exists
  // on that branch. The href must carry the branch AND the click must set it,
  // because the provider reads the param only when it mounts.
  it('carries the active branch on the detail link and sets it on click', () => {
    const setBranchId = vi.fn()
    renderRow(makeEvent(), windowSeries(10, 20), undefined, { branchId: 'br-1', setBranchId })

    const link = screen.getByRole('link', { name: 'checkout_completed' })
    expect(link).toHaveAttribute('href', '/p/proj-1/monitoring/event/evt-1?branch=br-1')

    fireEvent.click(link)
    expect(setBranchId).toHaveBeenCalledWith('br-1', { updateUrl: false })
  })

  it('links to the plain path on main, with no branch param to copy', () => {
    renderRow(makeEvent(), windowSeries(10, 20))

    expect(screen.getByRole('link', { name: 'checkout_completed' })).toHaveAttribute(
      'href',
      '/p/proj-1/monitoring/event/evt-1',
    )
  })

  // tripl-kjhi.3: the free-text title is the human label for a machine
  // identity, so it sits beside the name. It is real text (not a tooltip), yet
  // the link's accessible name stays the identity people search by.
  it('shows the title beside the identity, only when the event has one', () => {
    const { unmount } = renderRow(
      makeEvent({ title: 'Purchase finished' }),
      windowSeries(10, 20),
    )

    expect(screen.getByText('Purchase finished')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'checkout_completed' })).toBeInTheDocument()
    unmount()

    renderRow(makeEvent({ title: '' }), windowSeries(10, 20))
    expect(screen.queryByTitle('')).not.toBeInTheDocument()
    expect(screen.queryByText('Purchase finished')).not.toBeInTheDocument()
  })

  it('marks a row the reader has just created, in words as well as colour (AU-21)', () => {
    const { unmount } = renderRow(makeEvent(), windowSeries(10, 20), undefined, { justCreated: true })

    expect(screen.getByText('New')).toBeInTheDocument()
    expect(document.querySelector('tr')).toHaveAttribute('data-created', 'true')
    unmount()

    renderRow(makeEvent(), windowSeries(10, 20))
    expect(screen.queryByText('New')).not.toBeInTheDocument()
    expect(document.querySelector('tr')).not.toHaveAttribute('data-created')
  })
})

describe('EventRow template token rendering', () => {
  it('keeps known variable tokens quiet and tints unknown tokens amber', () => {
    renderRow(
      makeEvent({
        field_values: [{ id: 'fv-1', field_definition_id: TEMPLATE_FIELD.id, value: '${variant}/${missing}' }],
      }),
      [],
      undefined,
      {
        variables: [TEMPLATE_VARIABLE],
        fieldColumns: [TEMPLATE_FIELD],
        getFieldValue: () => '${variant}/${missing}',
      },
    )

    expect(screen.getByText('${variant}')).not.toHaveClass('text-warning')
    expect(screen.getByText('${missing}')).toHaveClass('text-warning')
    // A quiet code token, not accent-coloured mono that reads as a link (EV-13).
    expect(screen.getByText('${variant}')).toHaveAttribute('data-slot', 'code-token')
  })
})

// Two event types that each define a field called `page`. FieldDefinition is
// unique per (event_type_id, name), so these are genuinely different rows — the
// exact shape the "All" tab's name-deduped columns produce.
const PAGE_FIELD_PV: FieldDefinition = {
  id: 'field-pv-page',
  event_type_id: 'et-1',
  name: 'page',
  display_name: 'Page',
  field_type: 'string',
  is_required: false,
  enum_options: null,
  description: '',
  order: 0,
  sensitivity: 'none',
}

const PAGE_FIELD_SE: FieldDefinition = {
  ...PAGE_FIELD_PV,
  id: 'field-se-page',
  event_type_id: 'et-2',
}

const OBSERVED_PAGES: EventFieldVariableValue = {
  id: 'vv-page',
  variable_id: 'var-page',
  variable_name: 'page',
  source_column: 'payload.page',
  value_kind: 'low',
  observed_count: 4820,
  values: ['/checkout', '/cart'],
}

// tripl-xv77.1: on the default "All" tab the column a row renders under often
// belongs to a DIFFERENT event type — whichever type was deduped first. The cell
// resolved its text through the name fallback and its contexts through a second,
// id-only lookup, so on windy-ios the majority of context-carrying values printed
// a value with no way to see what had been observed behind it.
describe('EventRow observed-values popover', () => {
  const ALL_FIELD_DEFS = new Map([
    [PAGE_FIELD_PV.id, PAGE_FIELD_PV],
    [PAGE_FIELD_SE.id, PAGE_FIELD_SE],
  ])

  // Renders the row under page-view's `page` column — the one the All tab keeps.
  function renderUnderWinningColumn(ev: EventListItem) {
    return renderRow(ev, windowSeries(10, 20), undefined, {
      fieldColumns: [PAGE_FIELD_PV],
      getFieldValue: (event, col) => resolveFieldValue(event, col, ALL_FIELD_DEFS),
      getFieldValueRow: (event, col) => resolveFieldValueRow(event, col, ALL_FIELD_DEFS),
    })
  }

  function pageValue(fieldDefinitionId: string, contexts?: EventFieldVariableValue[]) {
    return {
      id: 'fv-page',
      field_definition_id: fieldDefinitionId,
      value: '/checkout',
      variable_values: contexts,
    }
  }

  it('offers the popover on a row whose event type did not win the column', () => {
    renderUnderWinningColumn(
      makeEvent({
        event_type_id: 'et-2',
        field_values: [pageValue(PAGE_FIELD_SE.id, [OBSERVED_PAGES])],
      }),
    )

    // The value itself always rendered — the name fallback found it. Only the
    // trigger beside it went missing, which is what made the loss invisible.
    expect(screen.getByText('/checkout')).toBeInTheDocument()
    expect(screen.getByLabelText('Observed variable values')).toBeInTheDocument()
  })

  // A control, not the guard: the id lookup already matched here, so this passed
  // before the fix too. It holds the case the fix must not have cost.
  it("offers the popover on a row of the column's own event type", () => {
    renderUnderWinningColumn(
      makeEvent({ field_values: [pageValue(PAGE_FIELD_PV.id, [OBSERVED_PAGES])] }),
    )

    expect(screen.getByLabelText('Observed variable values')).toBeInTheDocument()
  })

  // The negative control, and likewise green before the fix: the trigger appears
  // because the resolved row HAS contexts, not because every value now gets one.
  it('offers no popover when the matched value carries no observed contexts', () => {
    renderUnderWinningColumn(
      makeEvent({ event_type_id: 'et-2', field_values: [pageValue(PAGE_FIELD_SE.id)] }),
    )

    expect(screen.getByText('/checkout')).toBeInTheDocument()
    expect(screen.queryByLabelText('Observed variable values')).not.toBeInTheDocument()
  })
})

describe('EventRow single saturated signal indicator', () => {
  // A live signal used to fan out into four saturated marks on one row (a
  // pulsing name dot, the signal chip, the SignalLink arrow, and a red
  // sparkline dot). The row now surfaces ONE act-on-me affordance — the signal
  // chip — so a single incident does not read as many. (tripl-dmch.12)
  //
  // The chip reads "Open", never "Firing": Firing belongs to monitors (alert
  // rules), and 30 rows saying "Firing" contradicted a Monitors page that
  // correctly said "No monitors yet" (tripl-jfm3.4). Nor "Live": that is the
  // green lifecycle status one column over (EV-5 / DS-7).
  it('renders the Open signal chip as the single indicator and drops the SignalLink arrow', () => {
    renderRow(makeEvent({ monitored: true }), windowSeries(10, 20), makeSignal())

    // The one kept, saturated affordance: the labelled Signal-cell chip.
    expect(screen.getByText('Open')).toBeInTheDocument()
    expect(screen.queryByText('Live')).not.toBeInTheDocument()
    expect(screen.queryByText('Firing')).not.toBeInTheDocument()
    // The redundant SignalLink arrow (previously aria-labelled from the signal
    // tone title) is removed, so it no longer double-signals the same incident.
    expect(screen.queryByLabelText('Open latest scan anomaly')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Open recent anomaly')).not.toBeInTheDocument()
  })

  it('still marks a past-window anomaly on rows with no live signal', () => {
    // No rowSignal ⇒ no signal chip; the sparkline keeps its historical anomaly
    // marker as the row's only cue (nothing to deduplicate against).
    const series = windowSeries(10, 20)
    const withAnomaly = series.map((p, i) =>
      i === series.length - 1 ? { ...p, is_anomaly: true } : p,
    )
    renderRow(makeEvent({ monitored: true }), withAnomaly)

    expect(screen.queryByText('Open')).not.toBeInTheDocument()
    // Covered but quiet is a dash, not a signal (EV-6).
    expect(screen.queryByText('Monitored')).not.toBeInTheDocument()
  })
})

describe('EventRow multi-value meta field (tripl-h2sx.31)', () => {
  const KEYS_FIELD = {
    id: 'mf-keys',
    project_id: 'p-1',
    name: 'jira_keys',
    display_name: 'Jira keys',
    field_type: 'string',
    is_required: false,
    allow_multiple: true,
    enum_options: null,
    default_value: null,
    link_template: 'https://jira.example/browse/${value}',
    order: 0,
    sensitivity: 'none',
  } as MetaFieldDefinition

  it('renders one link per value, not one link around them joined', () => {
    renderRow(makeEvent(), [], undefined, {
      metaFields: [KEYS_FIELD],
      metaValueMap: new Map([['mf-keys', ['WND-1', 'WND-2']]]),
    })

    const first = screen.getByRole('link', { name: 'WND-1' })
    const second = screen.getByRole('link', { name: 'WND-2' })
    expect(first).toHaveAttribute('href', 'https://jira.example/browse/WND-1')
    expect(second).toHaveAttribute('href', 'https://jira.example/browse/WND-2')
  })
})

describe('EventRow open questions', () => {
  it('marks a row whose discussion is still waiting, with the count', async () => {
    // The filter beside it says "open questions"; a marker that cannot say how
    // many leaves the reader guessing whether one thing is open or five
    // (tripl-h2sx.26).
    renderRow({ ...makeEvent(), open_question_count: 3 } as EventListItem, [])

    const marker = await screen.findByTitle('3 unanswered questions in the discussion')
    expect(marker).toHaveTextContent('?3')
  })

  it('singularises one question, and says nothing when none is open', () => {
    const { unmount } = renderRow(
      { ...makeEvent(), open_question_count: 1 } as EventListItem,
      [],
    )
    expect(screen.getByTitle('1 unanswered question in the discussion')).toBeInTheDocument()
    unmount()

    renderRow(makeEvent(), [])
    expect(screen.queryByTitle(/unanswered question/)).toBeNull()
  })
})

describe('EventRow lifecycle chip (#258)', () => {
  it('marks a row with an open lifecycle finding', () => {
    renderRow({ ...makeEvent(), lifecycle_warning: true } as EventListItem, [])

    const chip = screen.getByTestId('event-lifecycle-chip')
    expect(chip).toHaveAttribute('title', expect.stringMatching(/sunset date/))
    // Self-describing without the pointer-only title: the visible word is
    // hidden from assistive tech and a full sentence is read instead.
    expect(screen.getByText('Lifecycle')).toHaveAttribute('aria-hidden', 'true')
    expect(chip).toHaveTextContent(/Lifecycle warning: sunset overdue or successor silent/)
  })

  it('shows no chip when the flag is false or absent', () => {
    const { unmount } = renderRow({ ...makeEvent(), lifecycle_warning: false } as EventListItem, [])
    expect(screen.queryByTestId('event-lifecycle-chip')).toBeNull()
    unmount()

    renderRow(makeEvent(), [])
    expect(screen.queryByTestId('event-lifecycle-chip')).toBeNull()
  })
})

describe('EventRow reorder handle (EVT-3)', () => {
  it('offers the drag handle while the rows are in catalog order', () => {
    renderRow(makeEvent(), [])

    expect(screen.getByRole('button', { name: /Drag to reorder/ })).toBeInTheDocument()
  })

  it('offers no drag handle when a drag would rewrite the catalog order', () => {
    renderRow(makeEvent(), [], undefined, { reorderable: false })

    expect(screen.queryByRole('button', { name: /Drag to reorder/, hidden: true })).toBeNull()
  })
})

describe('EventRow schema drift (EVT-33)', () => {
  it('leaves the per-type drift count to the header, not every row', () => {
    renderRow({ ...makeEvent(), drift_count: 4 } as EventListItem, [])

    expect(screen.queryByRole('button', { name: /schema drift/, hidden: true })).toBeNull()
  })
})

describe('EventRow name typography (DS-17 / EV-10)', () => {
  it('sets the display name in the UI sans, not mono', () => {
    renderRow(makeEvent({ name: 'Home Screen View' }), [])

    const link = screen.getByRole('link', { name: 'Home Screen View' })
    expect(link).not.toHaveClass('mono')
    expect(link).not.toHaveClass('font-mono')
  })
})

describe('EventRow last seen (EV-8)', () => {
  it('reads the 48h series when last_seen_at is unset, instead of "never"', () => {
    renderRow(makeEvent({ last_seen_at: null }), windowSeries(10, 20))
    expect(screen.getByTitle(/Latest volume in the collected 48h series/)).toBeInTheDocument()
    expect(screen.queryByTitle('Never observed in collected metrics')).not.toBeInTheDocument()
  })

  it('humanizes the instant in the title instead of printing raw ISO (DS-25)', () => {
    const { unmount } = renderRow(makeEvent({ last_seen_at: null }), windowSeries(10, 20))
    const fallback = screen.getByTitle(/Latest volume in the collected 48h series/)
    expect(fallback.getAttribute('title')).not.toMatch(/\d{4}-\d{2}-\d{2}T/)
    unmount()
    renderRow(makeEvent({ last_seen_at: '2026-06-10T18:00:00Z' }), windowSeries(10, 20))
    expect(screen.queryByTitle('2026-06-10T18:00:00Z')).not.toBeInTheDocument()
    expect(screen.getByTitle(formatDateTime('2026-06-10T18:00:00Z'))).toBeInTheDocument()
  })
})

describe('EventRow row click (EV-27)', () => {
  it('opens the detail page from anywhere on the row, not only the name', () => {
    renderRow(makeEvent({ name: 'Home Screen View' }), [])
    fireEvent.click(screen.getByRole('link', { name: 'Home Screen View' }).closest('tr')!.querySelector('td:last-child')!)
    // The row's own cells are plain text; a click there follows the name's link.
    expect(screen.getByTestId('row-location')).toHaveTextContent('/p/proj-1/monitoring/event/')
  })

  it('keeps a click that misses the checkbox inside its cell from opening the event', () => {
    renderRow(makeEvent({ name: 'Home Screen View' }), [])
    const before = screen.getByTestId('row-location').textContent
    const checkboxCell = screen.getByRole('checkbox', { name: 'Select Home Screen View' }).closest('td')!
    fireEvent.click(checkboxCell)
    expect(screen.getByTestId('row-location').textContent).toBe(before)
  })

  it('ignores clicks inside a portaled popover opened from a cell', () => {
    const field = { ...TEMPLATE_FIELD }
    const valueRow: EventFieldValue = {
      id: 'fv-variant',
      field_definition_id: field.id,
      value: 'short',
      variable_values: [OBSERVED_PAGES],
    }
    renderRow(makeEvent({ name: 'Home Screen View' }), [], undefined, {
      fieldColumns: [field],
      getFieldValue: () => 'short',
      getFieldValueRow: () => valueRow,
    })
    const before = screen.getByTestId('row-location').textContent
    fireEvent.click(screen.getByRole('button', { name: 'Observed variable values' }))
    const dialog = screen.getByRole('dialog')
    // Plain text inside the popover, not a control: it bubbles to the row
    // through the React tree but is not in the row's DOM.
    fireEvent.click(dialog)
    expect(screen.getByTestId('row-location').textContent).toBe(before)
  })
})

// On a phone every row is a card (eventsPhoneCard.ts). A lone "—" opened each
// card's chip line and the name sat 24px right of its checkbox, as if a column
// were still between them.
describe('EventRow phone card', () => {
  it('drops the quiet Signal cell on a phone and keeps it on desktop', () => {
    renderRow(makeEvent({ monitored: false }), windowSeries(10, 20))
    const dash = screen.getByTitle('No open signal, and no monitor (alert rule) covers this event')
    // Hidden below md only: the desktop column still reads "quiet".
    expect(dash.closest('td')).toHaveClass('max-md:hidden')
    expect(dash.closest('td')?.className).not.toMatch(/(^|\s)hidden(\s|$)/)
  })

  it('keeps the Signal cell on a phone when there is a signal to show', () => {
    renderRow(makeEvent({ monitored: true }), windowSeries(10, 20), makeSignal())
    expect(screen.getByText('Open').closest('td')).not.toHaveClass('max-md:hidden')
  })

  it('sizes the checkbox cell to the checkbox, so the name follows it', () => {
    renderRow(makeEvent({ name: 'Home Screen View' }), [])
    const cell = screen.getByRole('checkbox', { name: 'Select Home Screen View' }).closest('td')!
    expect(cell).toHaveClass('w-10', 'max-md:w-auto')
  })

  it('leaves the handle cell empty when the list cannot be reordered, so the card hides it', () => {
    const { container } = renderRow(makeEvent(), [], undefined, { reorderable: false })
    const row = container.querySelector('tr')!
    expect(row.className).toContain('max-md:[&>td:empty]:hidden')
    expect(row.querySelector('td')!.childNodes).toHaveLength(0)
  })

  it('keeps the drag handle in its cell when the list can be reordered', () => {
    renderRow(makeEvent({ name: 'Home Screen View' }), [], undefined, { reorderable: true })
    expect(screen.getByRole('button', { name: 'Drag to reorder Home Screen View' })).toBeInTheDocument()
  })
})
