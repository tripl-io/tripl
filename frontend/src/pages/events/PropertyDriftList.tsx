import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'

import { propertyDriftsApi, type PropertyDrift, type PropertyDriftAction } from '@/api/propertyDrifts'
import { Button } from '@/components/ui/button'
import { Chip } from '@/components/primitives/chip'
import { currentOrgSlug, projectPath } from '@/lib/navigation'
import {
  acceptLabel,
  describePropertyDrift,
  isActivePropertyDrift,
  PROPERTY_DRIFT_KIND_LABEL,
  sortPropertyDrifts,
} from '@/lib/propertyDrift'
import {
  activePropertyDriftsKey,
  eventPropertyDriftsKey,
  projectEventKey,
  projectHealthRootKey,
  projectPropertyDriftsKey,
  projectsKey,
  projectVariablesKey,
  typeChangePropertyDriftsKey,
} from '@/lib/queryKeys'
import { getErrorMessage } from '@/lib/utils'

const SNOOZE_MS = 7 * 24 * 60 * 60 * 1000

/**
 * The open property drifts (F23, #306) of one event — or, without `eventId`,
 * of the whole project — with their triage actions. Renders nothing when there
 * is nothing to look at, so it can be mounted unconditionally.
 *
 * - `eventId`: the event's new and missing-required properties.
 * - `variableIds`: the event's property list; type changes of those
 *   properties are shown too (a type change is per property, not per event,
 *   so the server cannot filter it by event). The event page's property grid
 *   passes it; without it the event list leaves type changes out.
 *
 * Accept changes the plan on main, as the button says: a new property joins
 * the event's list, a missing required one becomes optional, a type change
 * retypes the property. Dismiss marks the drift a false positive. `readOnly`
 * (a viewer) lists the drifts without the actions.
 */
export function PropertyDriftList({
  slug,
  eventId,
  variableIds,
  readOnly = false,
}: {
  slug: string
  eventId?: string
  variableIds?: readonly string[]
  readOnly?: boolean
}) {
  const qc = useQueryClient()
  const projectWide = eventId === undefined
  const withTypeChanges = !projectWide && variableIds !== undefined && variableIds.length > 0

  const listQuery = useQuery({
    queryKey: projectWide ? activePropertyDriftsKey(slug) : eventPropertyDriftsKey(slug, eventId),
    queryFn: () => propertyDriftsApi.list(slug, { eventId, activeOnly: true }),
  })
  const typeChangeQuery = useQuery({
    queryKey: typeChangePropertyDriftsKey(slug),
    queryFn: () => propertyDriftsApi.list(slug, { kind: 'type_change', activeOnly: true }),
    enabled: withTypeChanges,
  })

  const actionMut = useMutation({
    mutationFn: ({ driftId, body }: { driftId: string; body: PropertyDriftAction }) =>
      propertyDriftsApi.act(slug, driftId, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: projectPropertyDriftsKey(slug) })
      // Accept edits the plan (the event's list, or the property's type), and
      // every triage moves the open counts the summary and health score show.
      qc.invalidateQueries({ queryKey: projectVariablesKey(slug) })
      qc.invalidateQueries({ queryKey: projectEventKey(slug) })
      qc.invalidateQueries({ queryKey: projectHealthRootKey(slug) })
      qc.invalidateQueries({ queryKey: projectsKey() })
    },
  })

  const wanted = new Set(variableIds ?? [])
  const typeChanges = withTypeChanges
    ? (typeChangeQuery.data?.items ?? []).filter(drift => wanted.has(drift.variable_id))
    : []
  // `active_only` already asks the server for these; filtering again keeps a
  // row the user just snoozed from lingering until the refetch lands.
  const drifts = sortPropertyDrifts(
    [...(listQuery.data?.items ?? []), ...typeChanges].filter(drift => isActivePropertyDrift(drift)),
  )

  if (drifts.length === 0) return null

  const act = (drift: PropertyDrift, body: PropertyDriftAction) =>
    actionMut.mutate({ driftId: drift.id, body })

  return (
    <section
      aria-label="Property drift"
      className="rounded-md border border-warning/40 bg-warning-soft p-3"
    >
      <div className="mb-1 text-body-sm font-semibold uppercase tracking-wide text-warning">
        Property drift — the property list against what scans saw
      </div>
      <ul className="space-y-1.5">
        {drifts.map(drift => (
          <li key={drift.id} className="rounded-sm border bg-background px-2 py-1.5">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-mono text-body-sm font-medium">{`\${${drift.variable_name}}`}</span>
                  <Chip size="xs" variant="outline">{PROPERTY_DRIFT_KIND_LABEL[drift.kind]}</Chip>
                  {projectWide && drift.event_id && (
                    <Link
                      to={projectPath(currentOrgSlug(), slug, `/monitoring/event/${drift.event_id}`)}
                      className="text-body-sm text-fg-secondary underline-offset-2 hover:underline"
                    >
                      {drift.event_name ?? 'Event'}
                    </Link>
                  )}
                  {projectWide && !drift.event_id && (
                    <span className="text-body-sm text-fg-tertiary">All events</span>
                  )}
                </div>
                <div className="mt-0.5 text-body-sm text-fg-secondary">{describePropertyDrift(drift)}</div>
              </div>
              {!readOnly && (
                <div className="flex shrink-0 flex-wrap gap-1">
                  <Button
                    type="button"
                    size="xs"
                    variant="outline"
                    disabled={actionMut.isPending}
                    onClick={() => act(drift, { action: 'accept' })}
                  >
                    {acceptLabel(drift)}
                  </Button>
                  <Button
                    type="button"
                    size="xs"
                    variant="ghost"
                    disabled={actionMut.isPending}
                    // Seven days from the click, not from the render.
                    onClick={() => act(drift, {
                      action: 'snooze',
                      snoozed_until: new Date(Date.now() + SNOOZE_MS).toISOString(),
                    })}
                  >
                    Snooze 7d
                  </Button>
                  <Button
                    type="button"
                    size="xs"
                    variant="ghost"
                    className="text-fg-tertiary"
                    disabled={actionMut.isPending}
                    onClick={() => act(drift, { action: 'false_positive' })}
                  >
                    Dismiss
                  </Button>
                </div>
              )}
            </div>
          </li>
        ))}
      </ul>
      {actionMut.isError && (
        <p className="mt-2 text-body text-destructive">{getErrorMessage(actionMut.error)}</p>
      )}
    </section>
  )
}
