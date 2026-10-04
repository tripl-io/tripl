import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import type { ScanDryRunEvent, ScanDryRunNameWarning, ScanDryRunResponse } from '@/types'

import { ScanDryRunSummary } from './ScanDryRunSummary'

function event(name: string, rows: number, over: Partial<ScanDryRunEvent> = {}): ScanDryRunEvent {
  return {
    name,
    source_name: name,
    event_type: 'Purchase',
    approx_row_count: rows,
    share_of_sample: rows / 1000,
    status: 'new',
    grouped_by_rule: null,
    count_confidence: 'exact',
    ...over,
  }
}

function dryRun(over: Partial<ScanDryRunResponse> = {}): ScanDryRunResponse {
  return {
    window_from: null,
    window_to: null,
    sampled_rows: 1000,
    sample_row_limit: 5000,
    sample_is_complete: true,
    breakdown_combinations: 12,
    events: [],
    events_truncated: false,
    max_events_reached: false,
    fields: [],
    templated_columns: [],
    reserved_columns: [],
    unmapped_columns: [],
    warnings: [],
    errors: [],
    ...over,
  }
}

const THREE_EVENTS = [
  event('Purchase Completed', 648),
  event('Signup Started', 240),
  event('Cart Viewed', 112, { status: 'existing' }),
]

describe('ScanDryRunSummary — the panel names events, not rows', () => {
  // The whole promise quick-start.md made and the panel did not keep: five raw
  // warehouse rows named neither an event nor a field.
  it('counts the events and names every one of them', () => {
    render(<ScanDryRunSummary dryRun={dryRun({ events: THREE_EVENTS })} />)

    expect(screen.getByText('Would create 3 events')).toBeInTheDocument()
    expect(screen.getByText('Purchase Completed')).toBeInTheDocument()
    expect(screen.getByText('Signup Started')).toBeInTheDocument()
    expect(screen.getByText('Cart Viewed')).toBeInTheDocument()
    // Two of the three are new; the split has to account for all three.
    expect(screen.getByText('· 2 new · 1 already in your plan')).toBeInTheDocument()
  })

  // A grouped scan can produce the same event name under two event types, and a
  // run creates both. Keying the list on the name alone collapsed them into one
  // React child, so the second row lost its identity across re-renders.
  it('keeps one name under two event types as two distinct rows', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      render(
        <ScanDryRunSummary
          dryRun={dryRun({
            events: [
              event('home', 50, { event_type: 'click', status: 'existing' }),
              event('home', 30, { event_type: 'view' }),
            ],
          })}
        />,
      )

      expect(screen.getAllByText('home')).toHaveLength(2)
      expect(screen.getByText('· 1 new · 1 already in your plan')).toBeInTheDocument()
      const logged = consoleError.mock.calls.map(call => call.join(' ')).join('\n')
      expect(logged).not.toMatch(/same key/i)
    } finally {
      consoleError.mockRestore()
    }
  })

  // The honesty assertion. The sample is the most common column combinations,
  // capped — when the cap was hit the count is a FLOOR, and a flat "3 events"
  // would be the one lie this feature exists to avoid.
  it('says "at least" and never a flat count when the sample is incomplete', () => {
    render(
      <ScanDryRunSummary
        dryRun={dryRun({ events: THREE_EVENTS, sample_is_complete: false, events_truncated: true })}
      />,
    )

    expect(screen.getByText(/Would create at least 3 events/)).toBeInTheDocument()
    expect(screen.queryByText('Would create 3 events')).toBeNull()
    expect(
      screen.getByText(/More distinct events exist than this preview looked at/),
    ).toBeInTheDocument()
  })

  // The note used to open with "Raise the sample row limit". `sample_row_limit`
  // is backend-only: toDryRunRequest never sends it, no form field exposes it,
  // and Limits offers Lookback, Row cap per run and Row cap per metrics run —
  // none of which is it. An analyst who follows the first remedy hunts the
  // settings for a control that does not exist.
  it('names only remedies the product actually has', () => {
    render(
      <ScanDryRunSummary
        dryRun={dryRun({ events: THREE_EVENTS, sample_is_complete: false, events_truncated: true })}
      />,
    )

    expect(document.body.textContent).not.toMatch(/sample row limit/i)
    expect(
      screen.getByText(
        'More distinct events exist than this preview looked at. Narrow the base query to see them all.',
      ),
    ).toBeInTheDocument()
  })

  // The lookback is a real remedy only when a window was applied. Without a time
  // column resolve_lookback_window returns None, so pointing at Limits →
  // Lookback (hours) there is the same defect one control along.
  it('offers the lookback only when a window was actually applied', () => {
    const truncated = {
      events: THREE_EVENTS,
      sample_is_complete: false,
      events_truncated: true,
    }
    const { unmount } = render(<ScanDryRunSummary dryRun={dryRun(truncated)} />)
    expect(document.body.textContent).not.toMatch(/Lookback/)
    unmount()

    render(
      <ScanDryRunSummary
        dryRun={dryRun({
          ...truncated,
          window_from: '2026-08-07T12:00:00Z',
          window_to: '2026-08-08T12:00:00Z',
        })}
      />,
    )
    expect(
      screen.getByText(
        'More distinct events exist than this preview looked at. Narrow the base query,'
        + ' or shorten Limits → Lookback (hours), to see them all.',
      ),
    ).toBeInTheDocument()
  })

  it('names the fields it would add and their types', () => {
    render(
      <ScanDryRunSummary
        dryRun={dryRun({
          fields: [
            { name: 'props', type: 'json', status: 'new', event_type: 'Purchase' },
            { name: 'user_id', type: 'string', status: 'exists', event_type: 'Purchase' },
          ],
        })}
      />,
    )

    expect(screen.getByText('Would add 1 field')).toBeInTheDocument()
    expect(screen.getByText('props')).toBeInTheDocument()
    expect(screen.getByText('json')).toBeInTheDocument()
    expect(screen.getByText('user_id')).toBeInTheDocument()
  })

  it('says so plainly when no column is new', () => {
    render(
      <ScanDryRunSummary
        dryRun={dryRun({
          fields: [{ name: 'user_id', type: 'string', status: 'exists', event_type: 'Purchase' }],
        })}
      />,
    )

    expect(screen.getByText('No new fields — every column is already mapped.')).toBeInTheDocument()
  })

  // The contradiction three reviewers found independently. With an explicit
  // Event type the backend sets may_create_fields=False, so `fields` is ALWAYS
  // empty — and the panel printed "every column is already mapped" in the same
  // render as its own "Unmapped columns: platform_family" list, with a
  // "Create 1 field" button underneath. A user who reads the bold line and stops
  // creates a scan believing every column is captured.
  it('never claims every column is mapped while listing columns that are not', () => {
    render(<ScanDryRunSummary dryRun={dryRun({ fields: [], unmapped_columns: ['platform_family'] })} />)

    expect(screen.queryByText('No new fields — every column is already mapped.')).toBeNull()
    expect(document.body.textContent).not.toMatch(/every column is already mapped/)
    expect(
      screen.getByText(
        'No new fields — a run only fills the fields this event type already declares.'
        + ' The columns it does not declare are listed below.',
      ),
    ).toBeInTheDocument()
    // And the list it points at is still there, so the headline and the detail
    // now say the same thing.
    expect(
      screen.getByText(/platform_family — no field matches them/),
    ).toBeInTheDocument()
  })

  // The sibling of the assertion above, and the one the fix for "an empty window
  // reports every column as unmapped" opened up: with no rows the dry run has no
  // targets, so `unmapped_columns` is now [] as well as `fields` — and the same
  // fallthrough that used to be blocked by the unmapped list landed on "every
  // column is already mapped". Nothing was analysed, and a run over a non-empty
  // window would create a field for every unreserved column on this path.
  it('claims nothing about fields when it read no rows', () => {
    render(
      <ScanDryRunSummary
        dryRun={dryRun({
          window_from: '2026-08-07T12:00:00Z',
          window_to: '2026-08-08T12:00:00Z',
          sampled_rows: 0,
          breakdown_combinations: 0,
          events: [],
          fields: [],
          unmapped_columns: [],
        })}
      />,
    )

    expect(document.body.textContent).not.toMatch(/every column is already mapped/)
    expect(document.body.textContent).not.toMatch(/No new fields/)
    // The events body is the same class of claim: 0 rows read is not 0 events.
    expect(document.body.textContent).not.toMatch(/Would create/)
    // What is left is the one thing the answer does know, and its remedy.
    expect(
      screen.getByText(/The query returned no rows in this window/),
    ).toBeInTheDocument()
  })

  // Unbounded read, same rule — the remedy differs, the silence does not.
  it('claims nothing about fields when the whole query was empty', () => {
    render(<ScanDryRunSummary dryRun={dryRun({ sampled_rows: 0, breakdown_combinations: 0 })} />)

    expect(document.body.textContent).not.toMatch(/every column is already mapped/)
    expect(document.body.textContent).not.toMatch(/Would create/)
    expect(
      screen.getByText('The query returned no rows, so there is nothing to describe yet.'),
    ).toBeInTheDocument()
  })

  // The event-type chip exists to keep two same-named rows from reading as one
  // double-counted event. On the grouped path a NULL or empty event-type column
  // becomes the group name '' (cardinality.py), so the chip rendered an empty
  // span — no chip at all on exactly the row that needed one.
  it('names the group whose event-type column is empty', () => {
    render(
      <ScanDryRunSummary
        dryRun={dryRun({
          events: [
            event('home', 120, { event_type: '' }),
            event('home', 80, { event_type: 'checkout' }),
          ],
        })}
      />,
    )

    expect(screen.getAllByText('home')).toHaveLength(2)
    expect(screen.getByText('checkout')).toBeInTheDocument()
    // The nameless group gets a label a reader can act on, not a blank span.
    expect(screen.getByText('(no event type)')).toBeInTheDocument()
    const rows = screen.getAllByText('home').map(node => node.closest('li'))
    for (const row of rows) {
      expect(row?.textContent).toMatch(/\(no event type\)|checkout/)
    }
  })

  // A collapsed column is why the user got 3 events instead of 3000 — a step
  // function of a threshold they are editing on the same form, not a property of
  // their data. Unnamed, the count reads as a fact about the warehouse.
  it('explains the cardinality collapse that produced the count', () => {
    render(
      <ScanDryRunSummary
        dryRun={dryRun({
          events: THREE_EVENTS,
          templated_columns: [{ column: 'country', distinct_values: 214, threshold: 100 }],
        })}
      />,
    )

    expect(
      screen.getByText(
        /country has more than 100 distinct values, so its events are named with a \{country\} template instead of one event per value\./,
      ),
    ).toBeInTheDocument()
  })

  it('reports the event cap as a stop, not as a total', () => {
    render(
      <ScanDryRunSummary dryRun={dryRun({ events: THREE_EVENTS, max_events_reached: true })} />,
    )

    expect(screen.getByText('Stopped at 3 events. The real scan stops there too.')).toBeInTheDocument()
  })

  it('names the window it read, so an absent event is not read as an impossible one', () => {
    render(
      <ScanDryRunSummary
        dryRun={dryRun({
          window_from: '2026-08-07T12:00:00Z',
          window_to: '2026-08-08T12:00:00Z',
          sampled_rows: 4812,
          breakdown_combinations: 143,
        })}
      />,
    )

    expect(
      screen.getByText(/4,812 rows in the 143 most common column combinations\./),
    ).toBeInTheDocument()
    expect(screen.getByText(/^From /)).toBeInTheDocument()
    expect(screen.queryByText(/No time window/)).toBeNull()
  })

  // Three docs promised the dry run reads the scan's lookback window. Without a
  // time column resolve_lookback_window returns None and the read is the whole
  // base query — the panel used to print nothing at all there, so the missing
  // bound was invisible on the one screen built to be trusted about it.
  it('says the read was unbounded when there was no window', () => {
    render(
      <ScanDryRunSummary
        dryRun={dryRun({ window_from: null, window_to: null, sampled_rows: 4812 })}
      />,
    )

    expect(
      screen.getByText(/No time window — the whole base query was read · 4,812 rows in the/),
    ).toBeInTheDocument()
  })

  it('shows the event type beside a field when the fields span several (#247)', () => {
    render(
      <ScanDryRunSummary
        dryRun={dryRun({
          fields: [
            { name: 'event_name', type: 'string', status: 'new', event_type: 'screen_view' },
            { name: 'event_name', type: 'string', status: 'new', event_type: 'click' },
          ],
        })}
      />,
    )

    expect(screen.getByText('Would add 2 fields across 2 event types')).toBeInTheDocument()
    expect(screen.getByText('screen_view')).toBeInTheDocument()
    expect(screen.getByText('click')).toBeInTheDocument()
  })
})

describe('ScanDryRunSummary — a draft that would swamp the plan (#247)', () => {
  const combinatorial = Array.from({ length: 30 }, (_, index) =>
    event(`event_name=screen_${index} | button_id= | product_id= | amount=`, 10),
  )

  it('warns, naming the count and the columns joined into each name', () => {
    render(<ScanDryRunSummary dryRun={dryRun({ events: combinatorial })} />)

    const warning = screen.getByTestId('dry-run-explosion')
    expect(warning).toHaveTextContent('This draft would add 30 new events, one per combination of 4 columns.')
    expect(warning).toHaveTextContent('Event name format')
    expect(warning).toHaveTextContent('Event groups')
  })

  it('opens the control each bold term names', () => {
    const onFixNaming = vi.fn()
    render(<ScanDryRunSummary dryRun={dryRun({ events: combinatorial })} onFixNaming={onFixNaming} />)

    fireEvent.click(screen.getByRole('button', { name: 'Event name format' }))
    fireEvent.click(screen.getByRole('button', { name: 'Event groups' }))
    expect(onFixNaming.mock.calls).toEqual([['format'], ['groups']])
  })

  it('stays quiet on an ordinary answer', () => {
    render(<ScanDryRunSummary dryRun={dryRun({ events: THREE_EVENTS })} />)
    expect(screen.queryByTestId('dry-run-explosion')).toBeNull()
  })
})

describe('ScanDryRunSummary — name warnings (F12, #265)', () => {
  const EXPLOSION: ScanDryRunNameWarning = {
    code: 'combinatorial_explosion',
    event_type: 'Screen',
    message: '120 new names under Screen differ only in screen_id.',
    count: 120,
    slot: 1,
    slot_label: 'screen_id',
    pattern: 'view_*',
    samples: ['view_a1', 'view_b2', 'view_c3', 'view_d4'],
  }
  const DUPLICATE: ScanDryRunNameWarning = {
    code: 'duplicate',
    event_type: 'Screen',
    message: 'paywall_screen_view looks like paywall_view.',
    samples: [],
    name: 'paywall_screen_view',
    duplicate_of: { event_id: 'ev-paywall', name: 'paywall_view', score: 0.93, status: 'live' },
  }

  it('describes a combinatorial explosion: count, slot, pattern and a few samples', () => {
    render(<ScanDryRunSummary dryRun={dryRun({ name_warnings: [EXPLOSION] })} />)
    const box = screen.getByTestId('dry-run-name-explosion')
    expect(box).toHaveTextContent('120 new names under Screen differ only in screen_id (view_*)')
    expect(box).toHaveTextContent('For example: view_a1, view_b2, view_c3')
    expect(box).not.toHaveTextContent('view_d4')
  })

  it('names the duplicate and links to the planned event', () => {
    render(
      <MemoryRouter>
        <ScanDryRunSummary slug="demo" dryRun={dryRun({ name_warnings: [DUPLICATE] })} />
      </MemoryRouter>,
    )
    const block = screen.getByTestId('dry-run-name-duplicates')
    expect(block).toHaveTextContent('1 new event looks like one already in your plan')
    expect(block).toHaveTextContent('paywall_screen_view looks like paywall_view (93%)')
    expect(within(block).getByRole('link', { name: 'paywall_view' })).toHaveAttribute(
      'href',
      '/p/demo/monitoring/event/ev-paywall',
    )
  })

  it('names the duplicate as plain text where there is no project to link into', () => {
    render(<ScanDryRunSummary dryRun={dryRun({ name_warnings: [DUPLICATE] })} />)
    expect(screen.getByTestId('dry-run-name-duplicates')).toHaveTextContent('paywall_view')
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('renders nothing for an answer without name warnings', () => {
    render(<ScanDryRunSummary dryRun={dryRun({ events: THREE_EVENTS })} />)
    expect(screen.queryByTestId('dry-run-name-warnings')).toBeNull()
  })
})
