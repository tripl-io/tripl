import { formatRelativeTime } from '@/lib/datetime'
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { GitCompare } from 'lucide-react'

import { eventTypesApi } from '@/api/eventTypes'
import { ScenarioCoachMark } from '@/demo/ScenarioCoachMark'
import { useDemoScenarioActions } from '@/demo/demoScenarioContext'
import { SCENARIO_SEEDED } from '@/demo/scenarioModel'
import { Button } from '@/components/ui/button'
import { ErrorState } from '@/components/error-state'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { chipVariants } from '@/components/primitives/chip-variants'
import { cn, getErrorMessage } from '@/lib/utils'
import { eventTypeDriftsKey, projectEventsKey, projectEventTypesKey } from '@/lib/queryKeys'
import { SILENT_ERROR_META } from '@/lib/errorFeedback'
import { useCanWriteProject } from '@/lib/permissions'
import { countOf, pluralize } from '@/lib/plural'

const DRIFT_LABEL: Record<string, string> = {
  new_field: 'new',
  missing_field: 'missing',
  type_changed: 'type',
  enum_violation: 'enum',
  required_null_violation: 'required',
  regex_violation: 'regex',
  range_violation: 'range',
}

const CONTRACT_DRIFT_TYPES: Record<string, true> = {
  enum_violation: true,
  required_null_violation: true,
  regex_violation: true,
  range_violation: true,
}


export function EventDriftBadge({
  slug,
  eventTypeId,
  count,
  typeLabel,
  coached = false,
}: {
  slug: string
  eventTypeId: string
  count: number
  /** The type's display name, shown on the badge when several types' badges
   *  sit side by side and the count alone cannot say whose it is. */
  typeLabel?: string
  /** The demo's review-drift step points at this badge; its Accept is the second gesture. */
  coached?: boolean
}) {
  const [open, setOpen] = useState(false)
  const qc = useQueryClient()
  const { notifyStepCompleted } = useDemoScenarioActions()
  // Triage is an editor action; a viewer still reads the drift list.
  const canWrite = useCanWriteProject()

  const driftsQuery = useQuery({
    meta: SILENT_ERROR_META,
    queryKey: eventTypeDriftsKey(slug, eventTypeId),
    queryFn: () => eventTypesApi.listDrifts(slug, eventTypeId),
    enabled: open,
    staleTime: 30_000,
  })
  const actionMut = useMutation({
    meta: SILENT_ERROR_META,
    mutationFn: ({
      driftId,
      action,
    }: {
      driftId: string
      action: 'accept' | 'snooze' | 'false_positive' | 'reopen'
    }) => {
      const snoozedUntil = new Date(Date.now() + 7 * 86_400_000).toISOString()
      return eventTypesApi.applyDriftAction(slug, driftId, {
        action,
        ...(action === 'snooze' ? { snoozed_until: snoozedUntil } : {}),
      })
    },
    onSuccess: (_data, { action }) => {
      qc.invalidateQueries({ queryKey: eventTypeDriftsKey(slug, eventTypeId) })
      qc.invalidateQueries({ queryKey: projectEventTypesKey(slug) })
      qc.invalidateQueries({ queryKey: projectEventsKey(slug) })
      // Accepting a schema drift lands the reconcile chapter's last step —
      // inert outside the demo scenario (the reducer drops other steps).
      if (action === 'accept') notifyStepCompleted('reconcile/review-drift')
    },
  })

  if (count <= 0) return null

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        // Drop the last failure so a stale 409 does not greet the next open.
        if (!next) actionMut.reset()
      }}
    >
      <PopoverTrigger asChild>
        {/* The drift/warning flag of the badge taxonomy: a soft
            warning pill at the xs size, not a 16px uppercase square tag. It
            stays a <button>, so it borrows the Chip's classes. It says what it
            counts: "Purchase 2" beside the title read as a count of Purchase
            events or a notification. */}
        <button
          type="button"
          className={cn(chipVariants({ tone: 'warning', size: 'xs' }), 'hover:bg-warning/25')}
          aria-label={`${countOf(count, 'schema drift', 'schema drifts')} on ${typeLabel ? `event type ${typeLabel}` : 'this event type'}`}
        >
          <GitCompare aria-hidden />
          <span className="tnum">{count}</span>
          <span>{pluralize(count, 'schema drift', 'schema drifts')}</span>
          {typeLabel && <span className="max-w-[14ch] truncate">· {typeLabel}</span>}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-72 p-2 text-body-sm">
        <div className="mb-1 flex items-center justify-between">
          <span className="font-semibold">Schema drift</span>
          <span className="text-micro text-fg-tertiary">last 30 days</span>
        </div>
        {/* What each action does, once, instead of three unexplained buttons. */}
        {canWrite && (
          <p className="mb-2 text-caption text-fg-tertiary">
            Accept adds the change to the plan. Snooze hides it for 7 days.
          </p>
        )}
        {driftsQuery.isLoading && <div className="text-fg-tertiary">Loading…</div>}
        {/* Through ErrorState, so a 401 under the session-expired dialog
            reads as paused, not as a red auth failure. */}
        {driftsQuery.isError && (
          <ErrorState compact headingLevel={3} title="Failed to load drifts" error={driftsQuery.error} />
        )}
        {driftsQuery.data && driftsQuery.data.items.length === 0 && (
          <div className="text-fg-tertiary">No drifts in this window.</div>
        )}
        {/* Without this the backend's 409 (accepting a drift for a column the
            scan's event name format needs) is invisible: the button just stops
            pending and the drift stays open with no explanation.
            The backend sends a plain string detail, which api/client.ts puts
            straight into ApiError.message, so it renders verbatim. */}
        {actionMut.isError && (
          <div role="alert" className="mb-1 text-destructive">
            {getErrorMessage(actionMut.error)}
          </div>
        )}
        {driftsQuery.data && driftsQuery.data.items.length > 0 && (
          <ul className="space-y-1">
            {driftsQuery.data.items.map((drift) => (
              <li
                key={drift.id}
                className="flex items-center justify-between gap-2 rounded-sm px-1 py-0.5 hover:bg-muted/50"
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-caption" title={drift.field_name}>
                    {drift.field_name}
                  </div>
                  <div className="text-micro text-fg-tertiary">
                    {/* A dotted name is a property's JSON path (F23): its
                        contract comes from the property's type and values. */}
                    {drift.field_name.includes('.') && drift.drift_type in CONTRACT_DRIFT_TYPES
                      ? 'property '
                      : ''}
                    {DRIFT_LABEL[drift.drift_type] ?? drift.drift_type}
                    {drift.observed_type && drift.declared_type
                      ? ` · ${drift.declared_type} → ${drift.observed_type}`
                      : drift.observed_type
                        ? ` · ${drift.observed_type}`
                        : drift.declared_type
                          ? ` · declared ${drift.declared_type}`
                          : ''}
                  </div>
                  {drift.sample_value && (
                    <div
                      className="truncate font-mono text-micro text-fg-tertiary"
                      title={drift.sample_value}
                    >
                      e.g. {drift.sample_value}
                    </div>
                  )}
                  {canWrite && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {drift.status === 'open' || drift.status === 'snoozed' ? (
                        <>
                          <ScenarioCoachMark
                            step="reconcile/review-drift"
                            followUp
                            when={
                              coached &&
                              drift.field_name === SCENARIO_SEEDED.schemaDriftFieldName &&
                              drift.drift_type === 'type_changed'
                            }
                            tag="Then click here"
                            side="left"
                          >
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={actionMut.isPending}
                              onClick={() => actionMut.mutate({ driftId: drift.id, action: 'accept' })}
                            >
                              Accept
                            </Button>
                          </ScenarioCoachMark>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={actionMut.isPending}
                            onClick={() => actionMut.mutate({ driftId: drift.id, action: 'snooze' })}
                          >
                            Snooze
                          </Button>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={actionMut.isPending}
                            onClick={() => actionMut.mutate({ driftId: drift.id, action: 'false_positive' })}
                          >
                            False positive
                          </Button>
                        </>
                      ) : (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={actionMut.isPending}
                          onClick={() => actionMut.mutate({ driftId: drift.id, action: 'reopen' })}
                        >
                          Reopen
                        </Button>
                      )}
                    </div>
                  )}
                </div>
                <span
                  className="shrink-0 text-micro tnum text-fg-tertiary"
                >
                  {formatRelativeTime(drift.detected_at)}
                </span>
              </li>
            ))}
          </ul>
        )}
      </PopoverContent>
    </Popover>
  )
}
