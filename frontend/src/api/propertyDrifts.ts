import { api } from './client'

/** A difference between an event's property list and what a scan saw (F23). */
export type PropertyDriftKind = 'new_property' | 'missing_required' | 'type_change'
export type PropertyDriftStatus = 'open' | 'accepted' | 'snoozed' | 'false_positive'

export interface PropertyDrift {
  id: string
  variable_id: string
  variable_name: string
  /** null for a type change: the sampler cannot tell which event carried the value. */
  event_id: string | null
  event_name: string | null
  scan_config_id: string | null
  kind: PropertyDriftKind
  detail: {
    presence_rate?: number
    threshold?: number
    expected_type?: string
    observed_type?: string
    observed_schema?: Record<string, unknown> | null
  }
  status: PropertyDriftStatus
  resolution_note: string | null
  snoozed_until: string | null
  resolved_at: string | null
  resolved_by: string | null
  detected_at: string
}

export interface PropertyDriftList {
  items: PropertyDrift[]
  total: number
}

export type PropertyDriftAction =
  | { action: 'accept' | 'false_positive' | 'reopen'; note?: string }
  | { action: 'snooze'; snoozed_until: string; note?: string }

export const propertyDriftsApi = {
  list: (
    slug: string,
    filters: { variableId?: string; eventId?: string; kind?: PropertyDriftKind; activeOnly?: boolean } = {},
  ) => {
    const params = new URLSearchParams()
    if (filters.variableId) params.set('variable_id', filters.variableId)
    if (filters.eventId) params.set('event_id', filters.eventId)
    if (filters.kind) params.set('kind', filters.kind)
    if (filters.activeOnly) params.set('active_only', 'true')
    const query = params.toString()
    return api.get<PropertyDriftList>(
      `/projects/${slug}/variables/property-drifts${query ? `?${query}` : ''}`,
    )
  },
  act: (slug: string, driftId: string, body: PropertyDriftAction) =>
    api.post<PropertyDrift>(`/projects/${slug}/variables/property-drifts/${driftId}/action`, body),
}
