import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { MAX_BULK_INBOX_ACTION_GROUPS } from '@/api/alerting'

import { InboxBulkActionBar, type InboxBulkActionRequest } from './InboxBulkActionBar'

/**
 * The four verbs the bar offers, matched by their leading word.
 *
 * Regexes rather than whole names because every control is announced with the
 * selection size in it ("Acknowledge 201 selected incidents"), and these tests
 * are about the cap, not about how a count is formatted.
 */
const ACTION_NAMES = [
  /^Add a note to /,
  /^Acknowledge /,
  /^Resolve /,
  /^Mute /,
  /^Reopen /,
]

function renderBar(selectedCount: number) {
  const onAction = vi.fn<(request: InboxBulkActionRequest) => void>()
  const onClear = vi.fn()
  const bar = (count: number) => (
    <InboxBulkActionBar
      selectedCount={count}
      isPending={false}
      onAction={onAction}
      onClear={onClear}
    />
  )
  const utils = render(bar(selectedCount))
  return {
    ...utils,
    onAction,
    onClear,
    // The bar stays MOUNTED at zero selection and renders null, so one selection
    // ending and the next beginning is a prop change on one component and not a
    // remount. The leak test below turns on exactly that.
    setCount: (count: number) => utils.rerender(bar(count)),
  }
}

/** Open the note box on a `selectedCount`-sized selection and put `text` in it. */
function writeNote(selectedCount: number, text: string) {
  fireEvent.click(
    screen.getByRole('button', { name: `Add a note to ${selectedCount} selected incidents` }),
  )
  const box = screen.getByRole('textbox', {
    name: `Note on ${selectedCount} selected incidents`,
  })
  fireEvent.change(box, { target: { value: text } })
  return box
}

describe('InboxBulkActionBar — a selection the server would refuse', () => {
  it('acts normally at the cap itself, and says nothing about it', () => {
    // The boundary is inclusive on both sides of the wire: the server's
    // `max_length` admits exactly this many ids, so a bar that warned here would
    // block a batch the route would have accepted.
    renderBar(MAX_BULK_INBOX_ACTION_GROUPS)

    for (const name of ACTION_NAMES) {
      expect(screen.getByRole('button', { name })).toBeEnabled()
    }
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('stops the actions one incident over, and says exactly how many to untick', () => {
    // Reachable, not hypothetical: the inbox is an accumulating infinite list at
    // 50 rows per page, so "Load more" walks the selection past 200 and the only
    // feedback used to be a raw 422 about a list length — AFTER the operator had
    // committed to the decision.
    const { onAction } = renderBar(MAX_BULK_INBOX_ACTION_GROUPS + 1)

    expect(screen.getByRole('status')).toHaveTextContent(
      'Over the 200-incident limit — untick 1 incident to act on the rest.',
    )
    for (const name of ACTION_NAMES) {
      expect(screen.getByRole('button', { name })).toBeDisabled()
    }

    // Disabled means disabled: no request leaves, and the mute disclosure cannot
    // be opened to reach the duration buttons behind it either — those are
    // actions too, and "Until I unmute" is the furthest-reaching one there is.
    fireEvent.click(screen.getByRole('button', { name: /^Acknowledge / }))
    fireEvent.click(screen.getByRole('button', { name: /^Mute / }))
    expect(onAction).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: /for 24h$/ })).toBeNull()
  })

  it('counts the surplus, not the selection', () => {
    // "Too many" leaves the reader counting ticks. The number they need is how
    // many rows to undo, and it is the only thing they can act on here.
    renderBar(MAX_BULK_INBOX_ACTION_GROUPS + 12)

    expect(screen.getByRole('status')).toHaveTextContent(
      'Over the 200-incident limit — untick 12 incidents to act on the rest.',
    )
  })

  it('leaves the way out open', () => {
    // Clearing is one of the two routes back under the cap (unticking rows is
    // the other), so it is the one control that must survive the guard.
    const { onClear } = renderBar(MAX_BULK_INBOX_ACTION_GROUPS + 1)

    const clear = screen.getByRole('button', { name: 'Clear selection' })
    expect(clear).toBeEnabled()
    fireEvent.click(clear)
    expect(onClear).toHaveBeenCalledTimes(1)
  })
})

describe('InboxBulkActionBar — the note the batch shares', () => {
  it('keeps the box out of the way until it is asked for, then hands it the caret', () => {
    // The bar is a floating strip over a queue; a permanently open editor on it
    // would be the widest thing on the page for every operator who only wanted
    // to acknowledge four rows. Opening it has to cost one click and no hunt —
    // reveal-then-go-find is most of what made writing a note feel like
    // paperwork on the incident card.
    renderBar(3)
    expect(screen.queryByRole('textbox')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Add a note to 3 selected incidents' }))

    expect(screen.getByRole('textbox', { name: 'Note on 3 selected incidents' })).toHaveFocus()
  })

  it('saves a note on its own', () => {
    // `note` is the one inbox action that moves no status: it documents a
    // decision rather than taking one, and until now the bulk bar could not
    // express it at all.
    const { onAction } = renderBar(3)
    writeNote(3, 'checkout deploy at 14:02, expected')

    fireEvent.click(screen.getByRole('button', { name: 'Save this note on 3 selected incidents' }))

    expect(onAction).toHaveBeenCalledWith({
      action: 'note',
      note: 'checkout deploy at 14:02, expected',
    })
  })

  it('sends the same note with whichever action is pressed instead', () => {
    // The ask was "one comment AND mute them", not two round trips. The note is
    // attached in `run`, so this holds for every verb on the bar rather than for
    // whichever ones a future edit remembers.
    const { onAction } = renderBar(3)
    writeNote(3, 'known bad release')

    fireEvent.click(screen.getByRole('button', { name: /^Acknowledge / }))

    expect(onAction).toHaveBeenCalledWith({
      action: 'acknowledge',
      note: 'known bad release',
    })
  })

  it('saves on Ctrl+Enter without leaving the box', () => {
    const { onAction } = renderBar(2)
    const box = writeNote(2, 'same root cause')

    fireEvent.keyDown(box, { key: 'Enter', ctrlKey: true })

    expect(onAction).toHaveBeenCalledWith({ action: 'note', note: 'same root cause' })
  })

  it('leaves Enter alone, because a note is prose', () => {
    // The whole reason this is a textarea and not the one-line input the card
    // used to have. A bare Enter that submits makes the second paragraph
    // unreachable.
    const { onAction } = renderBar(2)
    const box = writeNote(2, 'first line')

    fireEvent.keyDown(box, { key: 'Enter' })

    expect(onAction).not.toHaveBeenCalled()
  })

  it('will not clear N notes from a box showing none of them', () => {
    // Clearing is defined as sending an empty string, and the incident card
    // offers it — because it is looking at the one note it would erase. Here an
    // empty box is not "no note", it is every selected incident's own note
    // erased at once, so the control stays shut rather than helpful.
    const { onAction } = renderBar(4)
    writeNote(4, '   ')

    const save = screen.getByRole('button', { name: 'Save this note on 4 selected incidents' })
    expect(save).toBeDisabled()
    fireEvent.click(save)
    expect(onAction).not.toHaveBeenCalled()
  })

  it('sends no note key at all when the box was opened and left alone', () => {
    // Not `note: ''`. An absent note means "leave each stored note alone" and an
    // empty one means "clear it", so an untouched box that sent a key would wipe
    // notes as a side effect of acknowledging.
    const { onAction } = renderBar(3)
    fireEvent.click(screen.getByRole('button', { name: 'Add a note to 3 selected incidents' }))

    fireEvent.click(screen.getByRole('button', { name: /^Resolve / }))

    expect(onAction).toHaveBeenCalledWith({ action: 'resolve' })
  })

  it('does not carry a note over to the next batch', () => {
    // THE leak this feature could have shipped. The note rides along with the
    // next action pressed, so a sentence typed about one selection, surviving
    // into a different one, would be copied verbatim onto incidents nobody wrote
    // it about — and the box is collapsed by default, so it would not even be on
    // screen while it happened.
    const { onAction, setCount } = renderBar(3)
    writeNote(3, 'checkout deploy')

    setCount(0)
    setCount(2)

    expect(screen.queryByRole('textbox')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /^Acknowledge / }))
    expect(onAction).toHaveBeenCalledWith({ action: 'acknowledge' })
  })

  it('cannot be hidden while it holds text', () => {
    // Same failure as above, reached by a different route: a note that is going
    // to ride along must stay visible, so the toggle collapses only when empty.
    renderBar(3)
    writeNote(3, 'still relevant')

    fireEvent.click(screen.getByRole('button', { name: 'Add a note to 3 selected incidents' }))

    expect(screen.getByRole('textbox', { name: 'Note on 3 selected incidents' })).toHaveValue(
      'still relevant',
    )
  })

  it('keeps the typing when the batch is refused', () => {
    // A failed bulk action leaves the selection ticked — see the page's
    // `onError`, which only toasts. The draft has to survive with it, or the
    // retry starts by retyping the sentence.
    const { setCount } = renderBar(3)
    writeNote(3, 'worth another go')

    setCount(3)

    expect(screen.getByRole('textbox', { name: 'Note on 3 selected incidents' })).toHaveValue(
      'worth another go',
    )
  })
})

// The bar is fixed to the viewport and wraps to several rows on a phone, so it
// covered the last cards' actions and "Load more" with nothing reserving room
// to scroll them out from under it (ALR-31).
describe('InboxBulkActionBar — room for the end of the list', () => {
  it('reserves its own height, plus its offset from the edge, in the flow', () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
      height: 120,
      width: 300,
      top: 0,
      left: 0,
      right: 300,
      bottom: 120,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    } as DOMRect)
    const { container } = renderBar(3)

    const spacer = container.querySelector<HTMLElement>('[data-bulk-bar-spacer]')
    expect(spacer).not.toBeNull()
    expect(spacer).toHaveAttribute('aria-hidden', 'true')
    // 120px of bar, its 18px offset, 12px of air.
    expect(spacer?.style.height).toBe('150px')
    vi.restoreAllMocks()
  })

  it('reserves nothing once the selection is gone', () => {
    const { container, setCount } = renderBar(3)

    setCount(0)

    expect(container.querySelector('[data-bulk-bar-spacer]')).toBeNull()
  })
})

// Below `sm` the bar is one row: the count, Acknowledge, Resolve and a "More"
// menu holding Note, Mute and Reopen (AL-16). The menu items must do what the
// buttons they stand in for do.
describe('InboxBulkActionBar — the phone "More" menu', () => {
  it('reopens the selection from the menu', async () => {
    const { onAction } = renderBar(3)

    const more = screen.getByRole('button', { name: 'More actions for 3 selected incidents' })
    fireEvent.pointerDown(more, { button: 0, ctrlKey: false })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Reopen' }))

    expect(onAction).toHaveBeenCalledWith({ action: 'reopen' })
  })

  it('opens the shared note box from the menu', async () => {
    renderBar(3)

    fireEvent.pointerDown(
      screen.getByRole('button', { name: 'More actions for 3 selected incidents' }),
      { button: 0, ctrlKey: false },
    )
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Add a note' }))

    expect(
      await screen.findByRole('textbox', { name: 'Note on 3 selected incidents' }),
    ).toBeInTheDocument()
  })
})
