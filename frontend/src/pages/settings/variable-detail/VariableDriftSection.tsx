import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { variableDriftsApi } from '@/api/variableDrifts'
import { useDemoScenarioActions } from '@/demo/demoScenarioContext'
import { SCENARIO_SEEDED } from '@/demo/scenarioModel'
import { eventNameLabel } from '@/lib/eventName'
import { variableDriftsKey } from '@/lib/queryKeys'
import type { Variable } from '@/types'
import { DriftReviewList } from './DriftReviewList'

/**
 * Value drift for ONE variable, row by event. The review itself is
 * `DriftReviewList`, shared with the event page.
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
  const { notifyStepCompleted } = useDemoScenarioActions()
  const { data: driftList } = useQuery({
    queryKey: variableDriftsKey(slug, branchId, variable.id),
    queryFn: () => variableDriftsApi.list(slug, { variableId: variable.id }, branchId),
  })
  const drifts = driftList?.items ?? []

  // Nothing while the list is on its way: "no drift" before the answer would
  // be a claim the page cannot make yet.
  if (drifts.length === 0) return driftList ? <>{empty}</> : null

  return (
    <DriftReviewList
      slug={slug}
      branchId={branchId}
      drifts={drifts}
      canWrite={canWrite}
      rowLabel={drift => eventNameLabel(drift.event_name)}
      coachFirstRow={variable.name === SCENARIO_SEEDED.driftVariableName}
      // Any drift action is reviewing the drift — inert outside the demo's
      // variables chapter (the reducer drops every other step).
      onReviewed={() => notifyStepCompleted('variables/see-drift')}
    />
  )
}
