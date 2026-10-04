import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ScanConfigPreview, ScanDryRunResponse } from '@/types'

import { ScanPreviewPanel } from './ScanPreviewPanel'

const preview: ScanConfigPreview = {
  columns: [
    { name: 'event_name', type_name: 'String', is_nullable: false },
    { name: 'user_id', type_name: 'String', is_nullable: false },
  ],
  rows: [
    { event_name: 'purchase_completed', user_id: 'u-1' },
    { event_name: 'purchase_completed', user_id: 'u-2' },
    { event_name: 'signup_started', user_id: 'u-3' },
    { event_name: 'signup_started', user_id: 'u-4' },
    { event_name: 'cart_viewed', user_id: 'u-5' },
  ],
  json_columns: [],
}

function dryRun(over: Partial<ScanDryRunResponse> = {}): ScanDryRunResponse {
  return {
    window_from: null,
    window_to: null,
    sampled_rows: 1000,
    sample_row_limit: 5000,
    sample_is_complete: true,
    breakdown_combinations: 3,
    events: [
      {
        name: 'Purchase Completed',
        source_name: 'Purchase Completed',
        event_type: 'Purchase',
        approx_row_count: 648,
        share_of_sample: 0.648,
        status: 'new',
        grouped_by_rule: null,
        count_confidence: 'exact',
      },
    ],
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

function renderPanel(over: Partial<React.ComponentProps<typeof ScanPreviewPanel>> = {}) {
  return render(
    <ScanPreviewPanel
      preview={preview}
      dryRun={dryRun()}
      dryRunStale={false}
      dryRunPending={false}
      dryRunError={null}
      eventTargetMissing={false}
      onRecheck={() => {}}
      {...over}
    />,
  )
}

describe('ScanPreviewPanel — the answer leads, the rows are evidence', () => {
  // The inversion this task exists for. The five raw rows used to BE the panel;
  // they answer "did my query run" and nothing else, while the docs promised the
  // panel showed which events and fields tripl would create.
  it('leads with what the scan would create and keeps the sample rows collapsed', () => {
    renderPanel()

    expect(screen.getByTestId('scan-dry-run-summary')).toBeInTheDocument()
    expect(screen.getByText('Would create 1 event')).toBeInTheDocument()

    // Nothing from the warehouse rows is in the DOM yet — not a cell, not the
    // column headers, not the caption.
    expect(screen.queryByText('purchase_completed')).toBeNull()
    expect(screen.queryByText('Query preview — sample rows')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Show sample rows (5)' }))

    expect(screen.getAllByText('purchase_completed')).toHaveLength(2)
    // The screen-reader caption is not a casualty of the demotion.
    expect(screen.getByText('Query preview — sample rows')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Hide sample rows' })).toBeInTheDocument()
  })

  // "The order is the whole change" was the panel's own claim, and it was not
  // kept: the panel opened with a SECOND "Preview" heading (the Field wrapping
  // it is already labelled Preview) and a sentence about tripl's own latency
  // engineering — "JSON paths are discovered on demand to keep the preview fast"
  // — so the user read the word Preview twice and one clause of internal
  // plumbing before reaching the event names.
  it('opens with the answer, not with a restatement of its own plumbing', () => {
    const { container } = renderPanel()

    const answer = 'What this scan would create'
    // Sliced rather than `startsWith`, so a failure quotes what the panel put
    // in front of the answer instead of just "expected false to be true".
    expect(container.textContent?.slice(0, answer.length)).toBe(answer)
    expect(document.body.textContent).not.toMatch(/Column pickers use the sample rows/)
    expect(document.body.textContent).not.toMatch(/keep the preview fast/)
  })

  // A format referencing a key the rows cannot supply fails EVERY run of the
  // config. Reading it here instead of after two hundred failed production runs
  // is the single highest-value thing the dry run does.
  it('puts a name-format error above everything else in the panel', () => {
    renderPanel({
      dryRun: dryRun({
        events: [],
        errors: ["Unknown key 'category' in event name format; available keys: action, page"],
      }),
    })

    const fix = screen.getByText(
      'Fix the event name format — a scan with this format fails every run.',
    )
    expect(fix).toBeInTheDocument()
    expect(
      screen.getByText(/Event name format error: Unknown key 'category' in event name format/),
    ).toBeInTheDocument()

    // "Above everything else" is the claim, so assert the document order rather
    // than mere presence: the error must precede both the summary heading and
    // the sample-rows disclosure.
    const errors = screen.getByTestId('dry-run-errors')
    const heading = screen.getByText('What this scan would create')
    const rowsToggle = screen.getByRole('button', { name: /Show sample rows/ })
    expect(errors.compareDocumentPosition(heading)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
    expect(errors.compareDocumentPosition(rowsToggle)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
  })

  // A dry run describes one specific draft. Once the draft moves it stops being
  // true, and a stale "would create 3 events: A, B, C" is worse than no answer.
  it('says the answer no longer describes the form, and offers the redo', () => {
    const onRecheck = vi.fn()
    renderPanel({ dryRunStale: true, onRecheck })

    expect(
      screen.getByText('The form changed since this was worked out, so it no longer describes this scan.'),
    ).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Recalculate' }))
    expect(onRecheck).toHaveBeenCalledTimes(1)
  })

  it('still shows the sample rows when the dry run could not be computed, and offers the retry', () => {
    const onRecheck = vi.fn()
    // The message `scansApi.dryRun` actually throws when the job failed without
    // saying why — the panel must not be carrying a second, private vocabulary.
    renderPanel({
      dryRun: null,
      dryRunError: new Error('The check stopped without saying why.'),
      onRecheck,
    })

    expect(screen.queryByTestId('scan-dry-run-summary')).toBeNull()
    expect(
      screen.getByText('Could not work out what this scan would create'),
    ).toBeInTheDocument()
    // Without this the panel is a dead end: the rows are loaded, so nothing on
    // screen would ask the question again.
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(onRecheck).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Show sample rows (5)' }))
    expect(screen.getAllByText('purchase_completed')).toHaveLength(2)
  })
})

describe('ScanPreviewPanel — a draft that cannot name its events', () => {
  // The P0. A brand-new scan has no event type and no event type column — the
  // column is picked FROM the rows this panel is showing. The panel used to
  // report the worker's own precondition here as a failure: "Could not work out
  // what this scan would create" over "Scan failed: Either event_type_id or
  // event_type_column must be specified". Nothing failed; the question was
  // unanswerable, and saying so is what the user can act on.
  it('says the question is unanswerable and which control answers it', () => {
    renderPanel({ dryRun: null, eventTargetMissing: true })

    const note = screen.getByText(/Nothing tells this scan how to name events yet/)
    expect(note).toBeInTheDocument()
    expect(note).toHaveTextContent(
      'Pick an Event type, or the Event type column your event names are in.',
    )
    // No failure framing, and no database field names anywhere on the panel.
    expect(screen.queryByText('Could not work out what this scan would create')).toBeNull()
    expect(document.body.textContent).not.toMatch(/event_type_id|event_type_column/)
  })

  // A stale answer computed from a draft that COULD name events does not
  // describe one that cannot, and its "Recalculate" button would be a no-op.
  it('withholds an answer left over from a draft that could still name events', () => {
    renderPanel({ dryRunStale: true, eventTargetMissing: true })

    expect(screen.queryByTestId('scan-dry-run-summary')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Recalculate' })).toBeNull()
  })

  // Once the draft can name events, the same rows are one click from the answer
  // — without that, the first Load preview would leave the panel permanently
  // empty and the promised answer unreachable.
  it('offers the check as soon as the draft names its events', () => {
    const onRecheck = vi.fn()
    renderPanel({ dryRun: null, eventTargetMissing: false, onRecheck })

    fireEvent.click(screen.getByRole('button', { name: 'Show what this scan would create' }))
    expect(onRecheck).toHaveBeenCalledTimes(1)
  })
})
