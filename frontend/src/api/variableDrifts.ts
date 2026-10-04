import { api, withBranch } from './client'
import type { components } from '../types/api.gen'

type Schemas = components['schemas']
type DriftResponse = Schemas['VariableValueDriftResponse']
type ActionRequest = Schemas['VariableValueDriftActionRequest']

// Taken from the generated OpenAPI schema rather than restated: a
// backend change to `SchemaDriftStatus` or the action enum now fails to compile
// here instead of drifting silently.
export type VariableValueDriftStatus = Schemas['SchemaDriftStatus']
export type VariableValueDriftAction = ActionRequest['action']
export type VariableValueDriftAcceptScope = ActionRequest['scope']

/**
 * The generated response, with the nullable fields required. The schema marks
 * them optional only because they carry a default; the server always sends
 * them (null when unset), and the readers compare against `null`.
 */
type AlwaysSent = 'resolution_note' | 'resolved_at' | 'resolved_by' | 'snoozed_until'
export type VariableValueDrift = Omit<DriftResponse, AlwaysSent> &
  Required<Pick<DriftResponse, AlwaysSent>>

export type VariableValueDriftList = Omit<Schemas['VariableValueDriftListResponse'], 'items'> & {
  items: VariableValueDrift[]
}

/** The action body; `scope` has a server default, so it may be left out. */
export type VariableValueDriftActionBody = Omit<ActionRequest, 'scope' | 'note' | 'snoozed_until'> & {
  scope?: ActionRequest['scope']
  note?: string
  snoozed_until?: string
}

export const variableDriftsApi = {
  list: (
    slug: string,
    filters?: { variableId?: string; eventId?: string },
    branchId?: string | null,
  ) => {
    const params = new URLSearchParams()
    if (filters?.variableId) params.set('variable_id', filters.variableId)
    if (filters?.eventId) params.set('event_id', filters.eventId)
    const query = params.toString()
    const path = `/projects/${slug}/properties/drifts${query ? `?${query}` : ''}`
    return api.get<VariableValueDriftList>(withBranch(path, branchId))
  },
  action: (
    slug: string,
    driftId: string,
    data: VariableValueDriftActionBody,
    branchId?: string | null,
  ) =>
    api.post<VariableValueDrift>(
      withBranch(`/projects/${slug}/properties/drifts/${driftId}/action`, branchId),
      data,
    ),
}
