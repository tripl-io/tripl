import { api, withBranch } from './client'

/** One entry of an event's property list (F23). `values` is this event's
 *  override of the allowed values; null when the global list applies. */
export interface VariableEventOverride {
  id: string
  variable_id: string
  event_id: string
  event_name: string
  values: string[] | null
  required: boolean
}

export const variableOverridesApi = {
  list: (slug: string, variableId: string, branchId?: string | null) =>
    api.get<VariableEventOverride[]>(
      withBranch(`/projects/${slug}/variables/${variableId}/event-overrides`, branchId),
    ),
  upsert: (
    slug: string,
    variableId: string,
    eventId: string,
    values: string[],
    branchId?: string | null,
  ) =>
    api.put<VariableEventOverride>(
      withBranch(`/projects/${slug}/variables/${variableId}/event-overrides/${eventId}`, branchId),
      { values },
    ),
  /** Drop the override and keep the property entry (and its requiredness). */
  clearValues: (slug: string, variableId: string, eventId: string, branchId?: string | null) =>
    api.put<VariableEventOverride>(
      withBranch(`/projects/${slug}/variables/${variableId}/event-overrides/${eventId}`, branchId),
      { values: null },
    ),
  del: (slug: string, variableId: string, eventId: string, branchId?: string | null) =>
    api.del(
      withBranch(`/projects/${slug}/variables/${variableId}/event-overrides/${eventId}`, branchId),
    ),
}
