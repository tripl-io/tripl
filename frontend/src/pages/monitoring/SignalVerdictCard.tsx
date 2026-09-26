import { useId, useState } from 'react'
import { Link } from 'react-router-dom'
import { MessageSquarePlus } from 'lucide-react'

import { Chip } from '@/components/primitives/chip'
import { Button } from '@/components/ui/button'
import { alertInboxStatusLabel, alertInboxStatusTone } from '@/lib/alertStatus'
import { formatTimestamp } from '@/lib/datetime'
import { formatSignalEffect } from '@/lib/monitoring'
import { getAlertingPath } from '@/lib/navigation'
import {
  VERDICT_OPTIONS,
  signalCommentDraft,
  verdictAttribution,
  verdictFieldsReady,
  verdictLabel,
  verdictTone,
  type ReasonChoice,
} from '@/lib/signalVerdict'
import { signalDirectionColor } from '@/lib/statusLexicon'
import type { AlertInboxStatus, MonitoringSignal, SignalVerdictKind } from '@/types'
import { useConfirm } from '@/hooks/useConfirm'
import { VerdictFields } from '@/pages/anomalies/VerdictFields'
import {
  CLEAR_AND_REOPEN_CONFIRM,
  canClearVerdict,
  canSetVerdict,
  clearVerdictLabel,
  clearVerdictReopensIncident,
  signalIncidentId,
  useSignalTriage,
} from '@/pages/anomalies/signalTriage'

/** What saving each verdict does to the incident of a routed signal. */
const INCIDENT_EFFECT: Record<SignalVerdictKind, string> = {
  expected: 'resolves the incident',
  tracking_bug: 'acknowledges the incident',
  false_positive: 'marks the incident a false positive',
  real_issue: 'acknowledges the incident',
}

/**
 * The drilldown's "Signal" card (#254): the verdict on the flagged bucket, set
 * where the signal is investigated rather than only in the inbox.
 *
 * It shows the current verdict with who set it and when, and the incident the
 * signal was routed into (status, and a link to that inbox item) or "Not
 * routed". An editor picks a verdict — expected (with its reason), tracking
 * bug, false positive, real issue — adds a note, and saves; on a routed signal
 * the save updates the incident, which stays the source of truth, so an
 * incident-sourced verdict is changed from here but cleared only in the inbox,
 * and clearing a signal's own verdict on a routed signal reopens its incident
 * (the button says so and asks first).
 * A tracking bug on an event offers the event's discussion, prefilled with the
 * signal. A viewer reads the verdict and the incident, nothing else.
 *
 * Its choice and note are local, seeded from the signal's verdict: the parent
 * keys it on the bucket and the verdict so a new verdict re-seeds it.
 */
export function SignalVerdictCard({
  slug,
  signal,
  canWrite,
  onOpenComment,
}: {
  slug: string
  signal: MonitoringSignal
  canWrite: boolean
  /** Opens the event's comment composer with this text; event scope only. */
  onOpenComment?: (draft: string) => void
}) {
  const headingId = useId()
  const reasonHintId = useId()
  const unchangedHintId = useId()
  const triage = useSignalTriage(slug)
  const { confirm, dialog } = useConfirm()
  const verdict = signal.verdict ?? null
  const [choice, setChoice] = useState<SignalVerdictKind | null>(verdict?.verdict ?? null)
  const [reason, setReason] = useState<ReasonChoice>(verdict?.expected_reason ?? '')
  const [note, setNote] = useState(verdict?.source === 'signal' ? (verdict.note ?? '') : '')

  const incidentId = signalIncidentId(signal)
  const incidentStatus: AlertInboxStatus | null =
    signal.incident?.status ?? signal.incident_status ?? null
  const editable = canWrite && canSetVerdict(signal)
  // The incident chip's text; the link's accessible name starts with it (WCAG 2.5.3).
  const incidentText = `Incident${incidentStatus ? ` · ${alertInboxStatusLabel(incidentStatus)}` : ''}`
  const unchanged =
    verdict !== null
    && choice === verdict.verdict
    && (reason || null) === (verdict.expected_reason ?? null)
    && (note.trim() || null) === (verdict.note ?? null)
  const fieldsReady = choice !== null && verdictFieldsReady(choice, reason)
  const ready = fieldsReady && !unchanged
  // Why Save is disabled, said next to it: the reason hint (in the fields) or
  // "No changes to save".
  const saveHintId = !fieldsReady ? reasonHintId : unchanged ? unchangedHintId : undefined
  const hint = VERDICT_OPTIONS.find((option) => option.verdict === choice)?.hint
  const showComment =
    onOpenComment && canWrite && (choice === 'tracking_bug' || verdict?.verdict === 'tracking_bug')

  const save = () => {
    if (!choice || !ready) return
    triage.run(signal, {
      kind: 'verdict',
      verdict: choice,
      expectedReason: choice === 'expected' && reason !== '' ? reason : null,
      note: note.trim() || null,
    })
  }

  const clearVerdict = async () => {
    if (clearVerdictReopensIncident(signal) && !(await confirm(CLEAR_AND_REOPEN_CONFIRM))) return
    triage.run(signal, { kind: 'clearVerdict' })
  }

  return (
    <section
      aria-labelledby={headingId}
      data-testid="signal-verdict-card"
      className="rounded-card border bg-(--surface) px-4 py-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-baseline gap-x-2">
          <h2 id={headingId} className="text-body font-semibold text-fg">
            Signal
          </h2>
          <span className="text-body-sm text-fg-secondary">
            <span style={{ color: signalDirectionColor(signal.direction) }}>
              {signal.direction === 'drop' ? 'Drop' : 'Spike'} {formatSignalEffect(signal)}
            </span>{' '}
            at {formatTimestamp(signal.bucket)}
          </span>
        </div>
        {incidentId ? (
          <Link
            to={getAlertingPath(slug, { incidentId })}
            className="no-underline"
            aria-label={`${incidentText} — open in Alerting`}
          >
            <Chip tone={incidentStatus ? alertInboxStatusTone(incidentStatus) : 'neutral'}>
              {incidentText}
            </Chip>
          </Link>
        ) : (
          <Chip tone="neutral" variant="outline" title="No alert rule routed this signal to an incident">
            Not routed
          </Chip>
        )}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-2 text-body-sm">
        {verdict ? (
          <>
            <Chip tone={verdictTone(verdict.verdict)}>{verdictLabel(verdict)}</Chip>
            <span className="text-fg-tertiary">{verdictAttribution(verdict)}</span>
          </>
        ) : (
          <span className="text-fg-secondary">Needs a verdict.</span>
        )}
      </div>
      {verdict?.note && (
        <p className="mt-1 whitespace-pre-wrap text-body-sm text-fg-secondary">{verdict.note}</p>
      )}

      {editable && (
        <div className="mt-3 grid gap-3 border-t pt-3">
          <div role="group" aria-label="Verdict" className="flex flex-wrap gap-2">
            {VERDICT_OPTIONS.map((option) => (
              <Button
                key={option.verdict}
                type="button"
                size="sm"
                variant={choice === option.verdict ? 'secondary' : 'outline'}
                aria-pressed={choice === option.verdict}
                onClick={() => setChoice(option.verdict)}
              >
                {option.label}
              </Button>
            ))}
          </div>
          {choice && (
            <>
              {hint && <p className="text-body-sm text-fg-tertiary">{hint}</p>}
              <VerdictFields
                verdict={choice}
                reason={reason}
                onReasonChange={setReason}
                note={note}
                onNoteChange={setNote}
                reasonHintId={reasonHintId}
              />
              {incidentId && (
                <p className="text-body-sm text-fg-tertiary">
                  This signal belongs to an incident: saving {INCIDENT_EFFECT[choice]}.
                </p>
              )}
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  size="sm"
                  disabled={!ready || triage.isPending}
                  aria-describedby={saveHintId}
                  onClick={save}
                >
                  Save verdict
                </Button>
                {canClearVerdict(signal) && (
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    disabled={triage.isPending}
                    onClick={() => void clearVerdict()}
                  >
                    {clearVerdictLabel(signal)}
                  </Button>
                )}
                {fieldsReady && unchanged && (
                  <span id={unchangedHintId} className="text-body-sm text-fg-tertiary">
                    No changes to save.
                  </span>
                )}
              </div>
            </>
          )}
        </div>
      )}

      {showComment && (
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="mt-3"
          onClick={() => onOpenComment?.(signalCommentDraft(signal, note.trim() || verdict?.note))}
        >
          <MessageSquarePlus aria-hidden="true" />
          Open a comment on the event
        </Button>
      )}
      {dialog}
    </section>
  )
}
