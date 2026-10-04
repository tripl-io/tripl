import { useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { variableDriftsApi } from '@/api/variableDrifts'
import { Chip } from '@/components/primitives/chip'
import { CodeToken } from '@/components/primitives/code-token'
import { Button } from '@/components/ui/button'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { useDemoScenarioActions } from '@/demo/demoScenarioContext'
import { SCENARIO_SEEDED } from '@/demo/scenarioModel'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { eventNameLabel } from '@/lib/eventName'
import { variableDriftsKey, variableOverridesKey, variablesKey } from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'
import {
  collapsedDriftLabel,
  DRIFT_REVIVE_LABEL,
  driftReviewState,
  driftStatusNote,
  useDriftReviewClock,
} from '@/lib/variableDrift'
import type { Variable } from '@/types'

/**
 * Value drift for ONE variable: observed values outside the documented list,
 * with Accept / Accept for event / Snooze 7d / False positive on each active
 * row. Every action applies at once — none waits for a Save.
 *
 * `empty` is what to render when the variable has no drift at all: the dialog
 * shows nothing, the variable page's Drift tab says so.
 */
export function VariableDriftSection({
  slug,
  branchId,
  variable,
  canWrite,
  empty = null,
}: {
  slug: string
  branchId: string | null
  variable: Variable
  canWrite: boolean
  empty?: ReactNode
}) {
  const qc = useQueryClient()
  const { notifyStepCompleted } = useDemoScenarioActions()
  // Covers everything the backend does not count as open right now — snoozed
  // into the future as well as resolved.
  const [showQuietDrifts, setShowQuietDrifts] = useState(false)

  const { data: driftList } = useQuery({
    queryKey: variableDriftsKey(slug, branchId, variable.id),
    queryFn: () => variableDriftsApi.list(slug, { variableId: variable.id }, branchId),
  })
  const driftItems = driftList?.items ?? []
  // One `now` for the whole render, so a drift cannot be classified against one
  // instant here and a different one further down — and it advances the moment
  // the nearest snooze runs out. The view can stay open a long time, so a
  // clock frozen at mount would keep a lapsed snooze collapsed here while the
  // badge in the list counted the drift as open. The hook carries
  // the timer and the reasoning.
  const driftNow = useDriftReviewClock(driftItems)
  const activeDrifts = driftItems.filter(drift => driftReviewState(drift, driftNow) === 'active')
  // Snoozed rows sit with the resolved ones, not with the active ones. The row's
  // drift badge comes from `get_open_drift_counts`, which drops a future-snoozed
  // row, so this view used to present as needing attention exactly the drift
  // the table beside it had just counted as zero.
  const snoozedDrifts = driftItems.filter(drift => driftReviewState(drift, driftNow) === 'snoozed')
  // Kept reachable rather than filtered away: a scan only reopens an accepted
  // row for values outside the accepted set, so undoing the acceptance itself
  // has to be possible from here.
  const resolvedDrifts = driftItems.filter(drift => driftReviewState(drift, driftNow) === 'resolved')
  const quietDrifts = [...snoozedDrifts, ...resolvedDrifts]
  // Paired with the state the row was sorted by, so the pill and the action
  // group cannot disagree with the list the row was put in.
  const visibleDrifts = (showQuietDrifts ? [...activeDrifts, ...quietDrifts] : activeDrifts)
    .map(drift => ({ drift, state: driftReviewState(drift, driftNow) }))

  const driftActionMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: ({ driftId, action, scope, snoozedUntil }: {
      driftId: string
      action: 'accept' | 'snooze' | 'false_positive' | 'reopen'
      scope?: 'global' | 'event'
      snoozedUntil?: string
    }) => variableDriftsApi.action(slug, driftId, { action, scope, snoozed_until: snoozedUntil }, branchId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: variableDriftsKey(slug, branchId, variable.id) })
      qc.invalidateQueries({ queryKey: variablesKey(slug, branchId) })
      qc.invalidateQueries({ queryKey: variableOverridesKey(slug, branchId, variable.id) })
      // Any drift action is reviewing the drift — inert outside the demo's
      // variables chapter (the reducer drops every other step).
      notifyStepCompleted('variables/see-drift')
    },
  })

  const snoozeDrift = (driftId: string) => {
    const until = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000).toISOString()
    driftActionMut.mutate({ driftId, action: 'snooze', snoozedUntil: until })
  }

  // Nothing while the list is on its way: "no drift" before the answer would
  // be a claim the page cannot make yet.
  if (driftItems.length === 0) return driftList ? <>{empty}</> : null

  const actionClass = 'h-6 px-2 text-caption'
  return (
    <div className={activeDrifts.length > 0 ? 'rounded-md border border-warning/40 bg-warning-soft p-3' : 'rounded-md border bg-muted/30 p-3'}>
      <div className={`mb-1 text-body-sm font-semibold uppercase tracking-wide ${activeDrifts.length > 0 ? 'text-warning' : 'text-fg-tertiary'}`}>
        Value drift — observed values outside the documented list
      </div>
      {/* These buttons act on their own, so a Save or Cancel elsewhere does
          not undo them; saying so ends the guess. */}
      {canWrite && activeDrifts.length > 0 && (
        <p className="mb-1.5 text-caption text-fg-tertiary">Each action applies at once.</p>
      )}
      {visibleDrifts.length > 0 && (
        <ul className="space-y-1.5">
          {visibleDrifts.map(({ drift, state }, driftIndex) => (
            <li key={drift.id} className="rounded-sm border bg-background px-2 py-1.5">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-body-sm font-medium">
                    {eventNameLabel(drift.event_name)}
                    {/* Keyed on the review state, not on the raw status: a
                        snooze whose time has passed is active again, and
                        labelling that row "snoozed" would tell the reader the
                        opposite of what the badge counts. The note carries the
                        expiry, so a deferral says when it comes back.
                        */}
                    {state !== 'active' && (
                      <Chip variant="outline" size="xs" className="ml-1.5">{driftStatusNote(drift, driftNow)}</Chip>
                    )}
                  </div>
                  <div className="mt-0.5 flex flex-wrap gap-1">
                    {drift.observed_values.map(value => (
                      <CodeToken key={value} className="border-warning/40" title={value}>{value}</CodeToken>
                    ))}
                  </div>
                </div>
                {/* A viewer reads the drift; the verdicts are not theirs to
                    give, so they are not offered (#237 rule 4). */}
                {canWrite && (
                  <ScenarioCoachMark
                    step="variables/see-drift"
                    // The action group is the useful target; anchoring the
                    // whole row makes the callout cover the form above it.
                    // Only an ACTIVE row: a collapsed one — snoozed or
                    // resolved — offers nothing but the button that puts it
                    // back on the open list.
                    when={driftIndex === 0 && state === 'active' && variable.name === SCENARIO_SEEDED.driftVariableName}
                  >
                    {/* The review row belongs to an ACTIVE drift. A collapsed
                        row gets the single action that puts it back on the open
                        list, because acting on a drift the view has just said
                        needs no attention should start by saying it does.
                        Both readings post the same `reopen`. */}
                    <div className="flex shrink-0 flex-wrap gap-1">
                      {state === 'active' ? (
                        <>
                          <Button type="button" size="sm" variant="outline" className={actionClass} disabled={driftActionMut.isPending} onClick={() => driftActionMut.mutate({ driftId: drift.id, action: 'accept', scope: 'global' })}>
                            Accept
                          </Button>
                          <Button type="button" size="sm" variant="outline" className={actionClass} disabled={driftActionMut.isPending} onClick={() => driftActionMut.mutate({ driftId: drift.id, action: 'accept', scope: 'event' })}>
                            Accept for event
                          </Button>
                          <Button type="button" size="sm" variant="ghost" className={actionClass} disabled={driftActionMut.isPending} onClick={() => snoozeDrift(drift.id)}>
                            Snooze 7d
                          </Button>
                          <Button type="button" size="sm" variant="ghost" className={`${actionClass} text-fg-tertiary`} disabled={driftActionMut.isPending} onClick={() => driftActionMut.mutate({ driftId: drift.id, action: 'false_positive' })}>
                            False positive
                          </Button>
                        </>
                      ) : (
                        <Button type="button" size="sm" variant="outline" className={actionClass} disabled={driftActionMut.isPending} onClick={() => driftActionMut.mutate({ driftId: drift.id, action: 'reopen' })}>
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
        <Button type="button" size="sm" variant="ghost" className="mt-1.5 h-6 px-2 text-caption text-fg-tertiary" onClick={() => setShowQuietDrifts(value => !value)}>
          {showQuietDrifts ? 'Hide' : 'Show'} {quietDrifts.length}{' '}
          {collapsedDriftLabel({ snoozed: snoozedDrifts.length, resolved: resolvedDrifts.length })}
        </Button>
      )}
      {driftActionMut.isError && (
        <p role="alert" className="mt-2 text-body text-destructive">{getErrorMessage(driftActionMut.error)}</p>
      )}
    </div>
  )
}
