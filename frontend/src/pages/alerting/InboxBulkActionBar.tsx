import { useCallback, useRef, useState } from 'react'
import { MoreHorizontal, X } from 'lucide-react'

import { MAX_BULK_INBOX_ACTION_GROUPS, MAX_INBOX_NOTE_LENGTH } from '@/api/alerting'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Textarea } from '@/components/ui/textarea'
import {
  INBOX_MUTE_CHOICES,
  muteChoiceName,
  muteChoiceUntilIso,
  muteName,
} from '@/lib/mutePresets'
import { countOf } from '@/lib/plural'
import type { AlertInboxBulkAction } from '@/types'

import { noteBudgetLabel } from './constants'

/**
 * One triage decision raised by the bar, before it knows which incidents it
 * applies to.
 *
 * The bar deliberately does NOT hold the ids. Selection lives on
 * `ProjectAlertingTab` beside the query whose rows it indexes into, so the
 * page is the only thing that can prune a selection when a filter change or a
 * refetch drops rows out from under it. The bar knows a COUNT — which is all
 * its words need — and the page attaches the ids at click time.
 *
 * `mutedUntil` has the same three non-interchangeable states as the
 * single-incident {@link InboxActionVariables}, for the same reason:
 * `undefined` = no mute value at all, a string = mute until that
 * instant, `null` = mute with NO end. Nothing downstream may branch on
 * truthiness — a `&& mutedUntil` drops the key from the request body for
 * exactly the most far-reaching choice on the bar.
 */
export interface InboxBulkActionRequest {
  action: AlertInboxBulkAction
  mutedUntil?: string | null
  /**
   * The one sentence explaining the whole batch, copied onto every selected
   * incident.
   *
   * Absent, not empty, when there is nothing to say. The server's rule is that
   * a note it is not GIVEN is a stored note it does not touch, while an empty
   * string is an explicit clear — so an empty string sent from here would not
   * mean "no note", it would wipe the note off every selected row at once,
   * which is not a decision this bar offers.
   */
  note?: string
}

/**
 * How the bar names what it is about to act on: "4 selected incidents".
 *
 * Every control on the bar is announced with this as its target, so a screen
 * reader user hears the size of the blast radius on the button itself and not
 * only in the count at the far left of a fixed bar they may never have visited.
 * It is also what makes the shared mute name builder work here: `muteName` and
 * `muteChoiceName` take a plain string target precisely so they can be given
 * something that is not a scope.
 */
function selectionTarget(count: number): string {
  return countOf(count, 'selected incident', 'selected incidents')
}

/**
 * What to do about a selection the server will not accept, said BEFORE acting.
 *
 * The batch route caps the id list at {@link MAX_BULK_INBOX_ACTION_GROUPS} and
 * refuses a longer one with a 422. Nothing on the page used to
 * bound the selection, and the inbox is an ACCUMULATING infinite list at 50 rows
 * per page — so a fifth "Load more" puts 250 tickable rows on screen and the
 * operator learns about the cap by watching a decision they had already
 * committed to fail with a validation error about a list length.
 *
 * The sentence names the limit and the arithmetic, because "too many" leaves the
 * reader counting ticks: over the cap the only thing they can do is untick a
 * specific number of rows, so the message is that number. It is a bar-level
 * message rather than a per-button tooltip for the reason the whole bar exists —
 * one decision, one place to read about it — and the actions go disabled beside
 * it rather than staying clickable, since a button that posts a request known in
 * advance to be refused is the affordance this replaces.
 */
function overCapNotice(overBy: number): string {
  return (
    `Over the ${MAX_BULK_INBOX_ACTION_GROUPS}-incident limit — untick `
    + `${countOf(overBy, 'incident', 'incidents')} to act on the rest.`
  )
}

interface InboxBulkActionBarProps {
  selectedCount: number
  /** One request is in flight for the whole batch — there is no per-row state. */
  isPending: boolean
  onAction: (request: InboxBulkActionRequest) => void
  onClear: () => void
}

/**
 * The floating bar that acts on every selected incident in one request.
 *
 * Chrome, position and the "<N> selected" phrasing are copied from
 * `pages/events/BulkActionBar` on purpose: this is the second bulk surface in
 * the app, and an operator who has met the first one should not have to learn
 * that this one is the same idea. What differs is only the verbs.
 *
 * WHAT IS NOT HERE, and must not be added:
 *
 *  - "False positive". It is the only inbox action refused in bulk, and the
 *    refusal is not a limitation to work around: direction is part of the
 *    correlation key, so a single scope's spike and drop are two incidents in
 *    this list, and marking both would ratchet that one scope's detection
 *    thresholds twice for one human decision with nothing in the record saying
 *    it was one click. The type of {@link InboxBulkActionRequest.action} makes
 *    the button unspellable rather than merely absent, so this comment is not
 *    the only guard. It stays available on each incident's own action row.
 *  - "Select all N matching". The events bar offers it because a status change
 *    there is cheap and reversible; here the cheapest thing on the bar silences
 *    alerting for a whole scope, and the batch is capped at
 *    {@link MAX_BULK_INBOX_ACTION_GROUPS} anyway. Selecting is deliberately
 *    per-incident, i.e. deliberately deliberate.
 *
 *    The Inbox header's "Select all N shown" and its shift-click range
 *     are not that control and do not reopen this: N is the number
 *    of cards under the box, every one of them is on screen, and the count on
 *    this bar therefore still describes something the operator can look at. What
 *    is refused above is a sweep of rows nobody has seen.
 *
 * Renders nothing at zero selection — a fixed bar over an untouched list is
 * chrome charging rent on the queue it sits on top of.
 */
export function InboxBulkActionBar({
  selectedCount,
  isPending,
  onAction,
  onClear,
}: InboxBulkActionBarProps) {
  // Same disclosure shape as the incident card's mute: the durations are
  // revealed, never applied by the control that reveals them. A bar button
  // labelled just "Mute" that silently posts seven days is the defect
  // `mutePresets` was extracted to end, and it would be worse
  // here, where one click covers a screenful of incidents.
  const [muteOpen, setMuteOpen] = useState(false)
  // The batch's shared note. Owned HERE, unlike the per-incident
  // drafts on `ProjectAlertingTab`, because those are keyed by an incident that
  // outlives any one decision while this belongs to a selection that does not:
  // it is written for the batch in front of you and is meaningless against the
  // next one. The lifetimes then line up for free — a FAILED batch keeps its
  // selection, so the bar stays open and the typing survives to be retried,
  // while a successful one clears the selection and takes the draft with it.
  const [noteOpen, setNoteOpen] = useState(false)
  const [note, setNote] = useState('')
  const noteFocusPending = useRef(false)
  const focusNoteWhenOpened = (node: HTMLTextAreaElement | null) => {
    if (!node || !noteFocusPending.current) return
    noteFocusPending.current = false
    node.focus()
  }

  // The bar is fixed to the viewport, so it floats OVER the end of the list —
  // and on a phone, with the note and the mute presets open, it wraps to three
  // or four rows and covered the last cards' actions and "Load more", with no
  // way to scroll them out from under it. The spacer below reserves
  // exactly its height at the end of the section, measured rather than guessed
  // because its height is whatever its wrapping makes it.
  const [barHeight, setBarHeight] = useState(0)
  const barObserver = useRef<ResizeObserver | null>(null)
  const measureBar = useCallback((node: HTMLDivElement | null) => {
    barObserver.current?.disconnect()
    barObserver.current = null
    if (!node) return
    const update = () => setBarHeight(Math.ceil(node.getBoundingClientRect().height))
    update()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(update)
    observer.observe(node)
    barObserver.current = observer
  }, [])

  if (selectedCount === 0) {
    // The bar hides itself here but stays MOUNTED, so an expanded duration row
    // survives the selection being cleared and comes back already open on the
    // next one — offering "Until I unmute", the furthest-reaching control on the
    // page, to someone who has just ticked one new incident and never asked for
    // it. Adjusting state during render is React's documented way to follow a
    // prop, and the guard makes it converge in one extra render.
    if (muteOpen) setMuteOpen(false)
    // The note is reset for a sharper version of the same reason. It does not
    // merely reappear — it RIDES ALONG with whatever is pressed next, so a
    // sentence typed about one batch would be copied verbatim onto the next
    // operator's unrelated incidents. Clearing it at zero selection is what
    // keeps a note the property of the batch it describes.
    if (noteOpen) setNoteOpen(false)
    if (note) setNote('')
    return null
  }

  const target = selectionTarget(selectedCount)
  // How far past what the server will accept this selection is.
  // Every ACTION is switched off above the cap — see `overCapNotice` — while
  // "Clear selection" and the individual checkboxes stay live, because those are
  // the two ways back under it.
  const overCapBy = selectedCount - MAX_BULK_INBOX_ACTION_GROUPS
  const isOverCap = overCapBy > 0
  const actionsDisabled = isPending || isOverCap
  const trimmedNote = note.trim()
  const noteBudget = noteBudgetLabel(note.length)
  // The note is attached HERE rather than by each button, so there is exactly
  // one place that decides whether a batch carries one and no button can be
  // added later that quietly forgets to. Omitted when empty — see
  // `InboxBulkActionRequest.note` for why an empty string is not the same
  // request. `setMuteOpen(false)` collapses the duration row because it is a
  // menu that has been used; the note box deliberately stays as the operator
  // left it, since a note that rides along invisibly is the failure this whole
  // control is supposed to avoid.
  const run = (request: InboxBulkActionRequest) => {
    setMuteOpen(false)
    onAction(trimmedNote ? { ...request, note: trimmedNote } : request)
  }
  const openNote = () => {
    noteFocusPending.current = true
    setNoteOpen(true)
  }

  return (
    <>
    {/* In flow, where the bar is mounted — after the list — so the page can
        scroll the list's end clear of the bar: its height, its 18px offset
        from the viewport edge, and a little air. */}
    <div aria-hidden="true" data-bulk-bar-spacer="" style={{ height: barHeight + 18 + 12 }} />
    <div
      ref={measureBar}
      // Below `sm` the bar is a full-width strip pinned to the bottom edge,
      // clear of the home indicator, with one row of actions: as a
      // floating card it wrapped into a ~190px box in mid-screen.
      className="fixed inset-x-0 bottom-0 z-(--z-bar) flex flex-col gap-1.5 border-t px-3 pt-2 pb-[max(0.5rem,env(safe-area-inset-bottom))] sm:inset-x-auto sm:bottom-[18px] sm:left-1/2 sm:max-w-[calc(100vw-2rem)] sm:-translate-x-1/2 sm:rounded-card sm:border sm:py-1.5 sm:pl-3.5 sm:pr-2 bg-bg-elevated border-border-strong"
      style={{
        boxShadow: 'var(--shadow-lg)',
      }}
      // Named as a group so the bar is findable as one thing, and so its
      // presence is assertable without depending on which verbs it currently
      // offers.
      role="group"
      aria-label="Bulk incident actions"
    >
      {/* The note comes FIRST in the DOM, which is the same rule the incident
          card follows and for the same reason: its text is sent BY the action
          buttons, so a keyboard user who meets the buttons first spends the
          decision and the note never goes anywhere.

          It differs from the card in being first VISUALLY too — the card pushes
          its editor back underneath with `order`, because there the actions are
          the point of the row and the note is a footnote to one incident. Here
          the box only exists because somebody asked for it, and a compose area
          above the buttons that send it is the arrangement every other one of
          these has. */}
      {noteOpen && (
        <div className="flex flex-col gap-1 pr-1.5 pt-0.5">
          {/* Focus is moved imperatively, not with `autoFocus` — the same call
              the incident card makes, for the same two reasons (jsx-a11y rejects
              the prop, and focus a user asked for is not focus seized at load).
              The flag is armed by the toggle below and spent once here; without
              the guard a callback ref would drag the caret back on every
              keystroke, since each one re-renders with a new ref identity. */}
          <Textarea
            ref={focusNoteWhenOpened}
            rows={2}
            aria-label={`Note on ${target}`}
            placeholder="Why are these being handled together?"
            maxLength={MAX_INBOX_NOTE_LENGTH}
            value={note}
            onChange={event => setNote(event.target.value)}
            onKeyDown={event => {
              if (!(event.metaKey || event.ctrlKey) || event.key !== 'Enter') return
              if (actionsDisabled || trimmedNote.length === 0) return
              event.preventDefault()
              run({ action: 'note' })
            }}
            className="min-h-0 w-full min-w-60 py-1.5"
          />
          <div className="flex flex-wrap items-center gap-2">
            {/* Saves the note and NOTHING else — `note` moves no status and
                stamps no `acted_at`, on this route exactly as on the single one.
                Disabled at an empty box rather than offering the card's "Clear
                note": clearing is defined as sending an empty string, and doing
                that from here would wipe whatever each of N incidents
                separately had to say. The card can offer it because it is
                looking at the one note it would erase. */}
            <Button
              size="sm"
              variant="outline"
              className="max-sm:h-9"
              aria-label={`Save this note on ${target}`}
              title="Copies this note onto every selected incident, and moves no status. Ctrl+Enter (⌘+Enter on a Mac) does the same."
              disabled={actionsDisabled || trimmedNote.length === 0}
              onClick={() => run({ action: 'note' })}
            >
              Save note
            </Button>
            <span className="text-caption text-fg-tertiary">
              …or press an action below to save it with that.
            </span>
            {noteBudget && (
              <span role="status" className="text-caption text-fg-secondary">
                {noteBudget}
              </span>
            )}
          </div>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2 sm:gap-2.5">
      <span className="text-body-sm text-fg-secondary">
        <span className="tnum font-semibold text-fg">{selectedCount}</span> selected
      </span>
      {isOverCap && (
        // `role="status"`, so it is ANNOUNCED and not merely visible: every
        // button beside it — Note included — goes disabled in the same render,
        // and a row of controls that stops working without saying why is the
        // failure this line exists to prevent. Warning rather than danger —
        // nothing has gone wrong, the operator simply has to untick some rows.
        <span role="status" className="text-caption text-warning">
          {overCapNotice(overCapBy)}
        </span>
      )}
      <div className="h-5 w-px max-sm:hidden bg-border" />
      {/* No fixed-slot rule here, unlike the incident card. That
          rule exists because the card's buttons line up in a COLUMN across
          rows, so a slot that appears on one row and not the next moves the
          destructive action under a cursor aimed at a snooze. This bar is one
          row that never moves, and a selection spanning several statuses has no
          single status to disable against — every action here is meaningful on
          a mixed selection, and the server applies each one idempotently. */}
      {/* First among the verbs, which is a tab-order decision and not a visual
          one: everything to the right of it SPENDS the batch, and a note is
          only worth typing before that happens. Put after Reopen it would read
          the same and be unreachable in time. */}
      <Button
        size="sm"
        variant="outline"
        // Folded into "More" below `sm`.
        className="max-sm:hidden"
        aria-expanded={noteOpen}
        aria-label={`Add a note to ${target}`}
        title="One sentence, copied onto every selected incident. Saved on its own, or carried by whichever action you press next."
        disabled={actionsDisabled}
        onClick={() => {
          // Collapses only while EMPTY. Text that is about to ride along with
          // the next action must not be hideable — an invisible passenger note
          // landing on incidents nobody saw it attached to is the one failure
          // this control has to not have.
          if (noteOpen && trimmedNote.length === 0) {
            setNoteOpen(false)
            return
          }
          openNote()
        }}
      >
        Note
      </Button>
      <Button
        size="sm"
        variant="outline"
        className="max-sm:h-9"
        aria-label={`Acknowledge ${target}`}
        title="Stops re-delivery on each one until its scope goes quiet, then each reopens by itself. Reversible."
        disabled={actionsDisabled}
        onClick={() => run({ action: 'acknowledge' })}
      >
        Acknowledge
      </Button>
      <Button
        size="sm"
        variant="outline"
        className="max-sm:h-9"
        aria-label={`Resolve ${target}`}
        title="Same suppression as Acknowledge, different bucket in the filter. Each reopens by itself once its scope goes quiet. Reversible."
        disabled={actionsDisabled}
        onClick={() => run({ action: 'resolve' })}
      >
        Resolve
      </Button>
      <Button
        size="sm"
        variant="outline"
        // Folded into "More" below `sm`.
        className="max-sm:hidden"
        aria-expanded={muteOpen}
        // The shared "Mute <target>" sentence, given a count instead of a scope.
        // The incident card's other branch ("Change mute on …") has no meaning
        // here: a selection can hold muted and unmuted incidents at once, and
        // this control does the same thing to both.
        aria-label={muteName(target)}
        title="Silences each selected incident on its own exact scan + rule + scope + signal kind + direction — for a preset duration, or until you unmute it. Asks first."
        disabled={actionsDisabled}
        onClick={() => setMuteOpen(current => !current)}
      >
        Mute
      </Button>
      <Button
        size="sm"
        variant="outline"
        // Folded into "More" below `sm`.
        className="max-sm:hidden"
        // One word for one slot, and it is the surface's own word, not mute
        // vocabulary: `reopen` lifts acknowledge, resolve and false-positive as
        // well as a mute. The card can say "Unmute" because it knows the one
        // status it is looking at; a mixed selection does not, so the honest
        // name is the one that covers every case.
        aria-label={`Reopen ${target}`}
        title="Puts each one back in the open queue. Alerts resume, and any mute on them is lifted."
        disabled={actionsDisabled}
        onClick={() => run({ action: 'reopen' })}
      >
        Reopen
      </Button>
      {/* Phones get the count, the two decisions that close most incidents,
          and this menu for the rest — one row, not four. The items
          call the same handlers as the buttons they stand in for, so a note
          typed here still rides along with the next action. */}
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            size="sm"
            variant="outline"
            className="max-sm:h-9 sm:hidden"
            aria-label={`More actions for ${target}`}
            disabled={actionsDisabled}
          >
            <MoreHorizontal aria-hidden="true" />
            More
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="end"
          // Focus goes to what the item opened (the note box), not back to
          // the trigger.
          onCloseAutoFocus={event => event.preventDefault()}
        >
          <DropdownMenuItem onSelect={openNote}>Add a note</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => setMuteOpen(true)}>Mute…</DropdownMenuItem>
          <DropdownMenuItem onSelect={() => run({ action: 'reopen' })}>Reopen</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {muteOpen && (
        <div className="flex flex-wrap items-center gap-1 text-body-sm text-fg-secondary">
          <span>Mute for</span>
          {/* INBOX_MUTE_CHOICES, not MUTE_PRESETS, and not re-typed literals.
              The shared module documents the scope rule: the open-ended choice
              belongs to INCIDENTS, because a NULL `muted_until` on an incident
              is a mute that never lapses, while the same NULL on an alert RULE
              means not muted at all (`is_rule_muted`). These are incidents, so
              the open-ended choice IS offered here — and the sentence each
              button is announced by comes from the same module, so this fourth
              mute surface cannot drift from the other three by rewording an
              `aria-label` in place. */}
          {INBOX_MUTE_CHOICES.map(choice => (
            <Button
              key={choice.label}
              size="sm"
              variant="outline"
              className="max-sm:h-9"
              // Visible face and accessible name differ on the open-ended
              // button by design — "Mute 4 selected incidents for Until I
              // unmute" is not English. See `muteChoiceName` for the reasoning
              // and for the one accepted WCAG 2.5.3 deviation it carries.
              aria-label={muteChoiceName(target, choice)}
              disabled={actionsDisabled}
              onClick={() => run({ action: 'mute', mutedUntil: muteChoiceUntilIso(choice) })}
            >
              {choice.label}
            </Button>
          ))}
        </div>
      )}
      <div className="h-5 w-px max-sm:hidden bg-border" />
      <button
        type="button"
        onClick={onClear}
        className="ml-auto flex size-9 items-center justify-center rounded-sm sm:ml-0 sm:size-7 text-[var(--fg-subtle)] hover:text-[var(--fg)]"
        aria-label="Clear selection"
      >
        <X aria-hidden="true" className="size-3.5" />
      </button>
      </div>
    </div>
    </>
  )
}
