import { useState, type ReactNode } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { variableDriftsApi, type VariableValueDrift } from '@/api/variableDrifts'
import { Chip } from '@/components/primitives/chip'
import { CodeToken } from '@/components/primitives/code-token'
import { Button } from '@/components/ui/button'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { invalidatePropertyEntries } from '@/lib/propertyEntries'
import { branchVariableDriftsKey, variablesKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import {
  collapsedDriftLabel,
  DRIFT_REVIVE_LABEL,
  driftReviewState,
  driftStatusNote,
  useDriftReviewClock,
} from '@/lib/variableDrift'

const SNOOZE_MS = 7 * 24 * 60 * 60 * 1000

/**
 * The value-drift review block: observed values outside the documented list,
 * with Accept / Accept for event / Snooze 7d / False positive on each active
 * row. Every action applies at once — none waits for a Save. The property's
 * page lists its drifts by event, the event's page by property; this is the
 * one review both read, so the two cannot drift apart on who may act, how a
 * failure is reported, or what an action refreshes.
 *
 * Rows that are not asking for attention are collapsed behind a toggle rather
 * than filtered out, so an acceptance stays undoable — and a snooze stays
 * visible — from either side.
 */
export function DriftReviewList({
  slug,
  branchId,
  drifts,
  canWrite,
  rowLabel,
  coachFirstRow = false,
  onReviewed,
}: {
  slug: string
  branchId: string | null
  /** Must not be empty: the caller decides what an empty list shows. */
  drifts: readonly VariableValueDrift[]
  /** A viewer reads the drift; the verdicts are not theirs to give (#237 rule 4). */
  canWrite: boolean
  /** The row's name: the event on a property's page, the property on an event's. */
  rowLabel: (drift: VariableValueDrift) => ReactNode
  /** The demo's drift step rings the first row's Accept. */
  coachFirstRow?: boolean
  onReviewed?: () => void
}) {
  const qc = useQueryClient()
  // Covers everything the backend does not count as open right now — snoozed
  // into the future as well as resolved.
  const [showQuiet, setShowQuiet] = useState(false)

  // One `now` for the whole render, so a drift cannot be classified against one
  // instant here and a different one further down — and it advances the moment
  // the nearest snooze runs out. The view can stay open a long time, so a
  // clock frozen at mount would keep a lapsed snooze collapsed here while the
  // badge in the variables table counted the drift as open. The hook carries
  // the timer and the reasoning.
  const now = useDriftReviewClock(drifts)
  const activeDrifts = drifts.filter(drift => driftReviewState(drift, now) === 'active')
  // Snoozed rows sit with the resolved ones, not with the active ones. The
  // table's drift badge comes from `get_open_drift_counts`, which drops a
  // future-snoozed row, so leaving it in the warning list made this block
  // present as needing attention exactly the drift the badge counted as zero.
  const snoozedDrifts = drifts.filter(drift => driftReviewState(drift, now) === 'snoozed')
  // Kept reachable rather than filtered away: a scan only reopens an accepted
  // row for values outside the accepted set, so undoing the acceptance itself
  // has to be possible from here.
  const resolvedDrifts = drifts.filter(drift => driftReviewState(drift, now) === 'resolved')
  const quietDrifts = [...snoozedDrifts, ...resolvedDrifts]
  // Paired with the state the row was sorted by, so the pill and the action
  // group cannot disagree with the list the row was put in.
  const visibleDrifts = (showQuiet ? [...activeDrifts, ...quietDrifts] : activeDrifts)
    .map(drift => ({ drift, state: driftReviewState(drift, now) }))

  const actionMut = useMutation({
    // Reported inline below; the global toast would say it a second time.
    meta: SILENT_ERROR_META,
    mutationFn: ({ driftId, action, scope, snoozedUntil }: {
      driftId: string
      action: 'accept' | 'snooze' | 'false_positive' | 'reopen'
      scope?: 'global' | 'event'
      snoozedUntil?: string
    }) => variableDriftsApi.action(slug, driftId, { action, scope, snoozed_until: snoozedUntil }, branchId),
    onSuccess: () => {
      // The branch prefix: the event's list and the property's list both.
      void qc.invalidateQueries({ queryKey: branchVariableDriftsKey(slug, branchId) })
      // The table's drift badge, and Accept's widened documented list.
      void qc.invalidateQueries({ queryKey: variablesKey(slug, branchId) })
      // Accept writes the property-entry rows: Accept for event the event's own
      // list, Accept the documented one an entry without a list falls back to.
      // One event's drifts span several properties, so no single key names them.
      invalidatePropertyEntries(qc, slug, branchId)
      onReviewed?.()
    },
  })

  const snooze = (driftId: string) => {
    // Seven days from the CLICK, not from `now` — that one is the render's
    // classification instant and can be arbitrarily old on a page left open.
    const until = new Date(Date.now() + SNOOZE_MS).toISOString()
    actionMut.mutate({ driftId, action: 'snooze', snoozedUntil: until })
  }

  const hasActive = activeDrifts.length > 0
  return (
    <div className={hasActive ? 'rounded-md border border-warning/40 bg-warning-soft p-3' : 'rounded-md border bg-muted/30 p-3'}>
      <div className={`mb-1 text-body-sm font-semibold uppercase tracking-wide ${hasActive ? 'text-warning' : 'text-fg-tertiary'}`}>
        Value drift — observed values outside the documented list
      </div>
      {/* These buttons act on their own, so a Save or Cancel elsewhere does
          not undo them; saying so ends the guess. */}
      {canWrite && hasActive && (
        <p className="mb-1.5 text-caption text-fg-tertiary">Each action applies at once.</p>
      )}
      {visibleDrifts.length > 0 && (
        <ul className="space-y-1.5">
          {visibleDrifts.map(({ drift, state }, index) => (
            <li key={drift.id} className="rounded-sm border bg-background px-2 py-1.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-body-sm font-medium">
                    {rowLabel(drift)}
                    {/* Keyed on the review state, not on the raw status: a
                        snooze whose time has passed is active again, and
                        labelling that row "snoozed" would tell the reader the
                        opposite of what the badge counts. The note carries the
                        expiry, so a deferral says when it comes back. */}
                    {state !== 'active' && (
                      <Chip variant="outline" size="xs" className="ml-1.5">{driftStatusNote(drift, now)}</Chip>
                    )}
                  </div>
                  <div className="mt-0.5 flex flex-wrap gap-1">
                    {drift.observed_values.map(value => (
                      <CodeToken key={value} className="border-warning/40" title={value}>{value}</CodeToken>
                    ))}
                  </div>
                </div>
                {canWrite && (
                  <ScenarioCoachMark
                    step="variables/see-drift"
                    // Only an ACTIVE row: a collapsed one — snoozed or
                    // resolved — offers nothing but the button that puts it
                    // back on the open list.
                    when={coachFirstRow && index === 0 && state === 'active'}
                  >
                    {/* The review row belongs to an ACTIVE drift. A collapsed
                        row gets the single action that puts it back on the open
                        list, because acting on a drift the view has just said
                        needs no attention should start by saying it does.
                        Both readings post the same `reopen`. */}
                    <div className="flex shrink-0 flex-wrap gap-1">
                      {state === 'active' ? (
                        <>
                          {/* The step's ring is on Accept itself: around the
                              whole group, it left four buttons to answer
                              "Accept is here". */}
                          <ScenarioCoachMark
                            step="variables/see-drift"
                            followUp
                            when={coachFirstRow && index === 0}
                          >
                            <Button type="button" size="xs" variant="outline" disabled={actionMut.isPending} onClick={() => actionMut.mutate({ driftId: drift.id, action: 'accept', scope: 'global' })}>
                              Accept
                            </Button>
                          </ScenarioCoachMark>
                          <Button type="button" size="xs" variant="outline" disabled={actionMut.isPending} onClick={() => actionMut.mutate({ driftId: drift.id, action: 'accept', scope: 'event' })}>
                            Accept for event
                          </Button>
                          <Button type="button" size="xs" variant="ghost" disabled={actionMut.isPending} onClick={() => snooze(drift.id)}>
                            Snooze 7d
                          </Button>
                          <Button type="button" size="xs" variant="ghost" className="text-fg-tertiary" disabled={actionMut.isPending} onClick={() => actionMut.mutate({ driftId: drift.id, action: 'false_positive' })}>
                            False positive
                          </Button>
                        </>
                      ) : (
                        <Button type="button" size="xs" variant="outline" disabled={actionMut.isPending} onClick={() => actionMut.mutate({ driftId: drift.id, action: 'reopen' })}>
                          {DRIFT_REVIVE_LABEL[state]}
                        </Button>
                      )}
                    </div>
                  </ScenarioCoachMark>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
      {quietDrifts.length > 0 && (
        <Button type="button" size="xs" variant="ghost" className="mt-1.5 text-fg-tertiary" onClick={() => setShowQuiet(value => !value)}>
          {showQuiet ? 'Hide' : 'Show'} {quietDrifts.length}{' '}
          {collapsedDriftLabel({ snoozed: snoozedDrifts.length, resolved: resolvedDrifts.length })}
        </Button>
      )}
      {actionMut.isError && (
        <p role="alert" className="mt-2 text-body text-destructive">{getErrorMessage(actionMut.error)}</p>
      )}
    </div>
  )
}
