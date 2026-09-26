import { useId, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { formatTimestamp } from '@/lib/datetime'
import { VERDICT_OPTIONS, verdictFieldsReady, type ReasonChoice } from '@/lib/signalVerdict'
import type { SignalExpectedReason, SignalVerdictKind } from '@/types'
import { VerdictFields } from './VerdictFields'

const TITLE: Record<SignalVerdictKind, string> = {
  expected: 'Mark as expected',
  tracking_bug: 'Mark as a tracking bug',
  false_positive: 'Mark as a false positive',
  real_issue: 'Mark as a real issue',
}

/** What each verdict does to the incident a routed signal belongs to. */
const INCIDENT_EFFECT: Record<SignalVerdictKind, string> = {
  expected: 'resolves its incident',
  tracking_bug: 'acknowledges its incident',
  false_positive: 'marks its incident a false positive',
  real_issue: 'acknowledges its incident',
}

/**
 * A verdict from the Anomalies row menu (#254): the kind was picked in the
 * menu, the dialog asks the rest — the reason for `expected`, and a note for
 * any. On a routed signal it says what the verdict does to the incident, which
 * is where the verdict is really written. Mounted only while open, so its
 * fields start empty each time.
 */
export function SignalVerdictDialog({
  verdict,
  bucket,
  scopeLabel,
  routed,
  pending,
  onConfirm,
  onClose,
}: {
  verdict: SignalVerdictKind
  bucket: string
  scopeLabel: string
  /** The signal belongs to an incident. */
  routed: boolean
  pending: boolean
  onConfirm: (fields: { expectedReason: SignalExpectedReason | null; note: string | null }) => void
  onClose: () => void
}) {
  const [reason, setReason] = useState<ReasonChoice>('')
  const [note, setNote] = useState('')
  const option = VERDICT_OPTIONS.find((candidate) => candidate.verdict === verdict)
  const ready = verdictFieldsReady(verdict, reason)
  const reasonHintId = useId()
  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent>
        <form
          noValidate
          className="flex min-h-0 flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (!ready) return
            onConfirm({ expectedReason: reason === '' ? null : reason, note: note.trim() || null })
          }}
        >
          <DialogHeader>
            <DialogTitle>{TITLE[verdict]}</DialogTitle>
            <DialogDescription>
              {scopeLabel} at {formatTimestamp(bucket)}. {option?.hint}
              {routed && ` This signal belongs to an incident: the verdict ${INCIDENT_EFFECT[verdict]}.`}
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            <VerdictFields
              verdict={verdict}
              reason={reason}
              onReasonChange={setReason}
              note={note}
              onNoteChange={setNote}
              reasonHintId={reasonHintId}
            />
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={pending || !ready}
              aria-describedby={ready ? undefined : reasonHintId}
            >
              {TITLE[verdict]}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
