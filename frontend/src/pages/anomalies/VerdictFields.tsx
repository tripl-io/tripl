import { useId } from 'react'

import { Label } from '@/components/ui/label'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { Textarea } from '@/components/ui/textarea'
import {
  EXPECTED_REASON_OPTIONS,
  VERDICT_NOTE_MAX_LENGTH,
  type ReasonChoice,
} from '@/lib/signalVerdict'
import type { SignalVerdictKind } from '@/types'

const REASON_OPTIONS = EXPECTED_REASON_OPTIONS.map((option) => ({
  value: option.reason as ReasonChoice,
  label: option.label,
}))

const NOTE_PLACEHOLDER: Record<SignalVerdictKind, string> = {
  expected: 'e.g. Spring campaign launch',
  tracking_bug: 'e.g. The iOS build stopped sending the event after 5.2',
  false_positive: 'e.g. A quiet scope; this size of move is normal here',
  real_issue: 'e.g. Checkout fails on Android since the release',
}

/** Why Save is disabled while an expected verdict has no reason yet. */
export const REASON_HINT = 'Pick a reason to save.'

/**
 * The fields a verdict carries besides its kind (#254): for `expected` the
 * reason — required, four fixed values — and for every verdict a free-text
 * note. Shared by the row menu's dialog and the drilldown's Signal card so the
 * two ask the same thing in the same words.
 *
 * The reason group is named "Reason (required)", its caption says so too, and
 * until one is picked a visible hint says why saving is not possible yet; the
 * caller passes `reasonHintId` and points its Save button's aria-describedby
 * at it while the hint shows. The note is labelled "Note" for every verdict
 * so it does not read as a second reason field.
 */
export function VerdictFields({
  verdict,
  reason,
  onReasonChange,
  note,
  onNoteChange,
  reasonHintId,
}: {
  verdict: SignalVerdictKind
  reason: ReasonChoice
  onReasonChange: (reason: ReasonChoice) => void
  note: string
  onNoteChange: (note: string) => void
  /** Id for the "Pick a reason" hint, for the Save button's aria-describedby. */
  reasonHintId: string
}) {
  const noteId = useId()
  return (
    <div className="grid gap-3">
      {verdict === 'expected' && (
        <div className="grid gap-2">
          {/* A group names itself; this is its visible caption. */}
          <span className="text-body font-medium" aria-hidden="true">
            Reason <span className="font-normal text-fg-tertiary">(required)</span>
          </span>
          <SegmentedControl<ReasonChoice>
            aria-label="Reason (required)"
            size="sm"
            value={reason}
            onChange={onReasonChange}
            options={REASON_OPTIONS}
            className="w-fit"
          />
          {reason === '' && (
            <p id={reasonHintId} className="text-body-sm text-fg-tertiary">
              {REASON_HINT}
            </p>
          )}
        </div>
      )}
      <div className="grid gap-2">
        <Label htmlFor={noteId} optional>
          Note
        </Label>
        <Textarea
          id={noteId}
          value={note}
          maxLength={VERDICT_NOTE_MAX_LENGTH}
          onChange={(event) => onNoteChange(event.target.value)}
          placeholder={NOTE_PLACEHOLDER[verdict]}
          rows={3}
        />
      </div>
    </div>
  )
}
