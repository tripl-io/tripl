import { api } from './client'
import type { PlannedEvent, PlannedWindowSuggestion } from '../types'

export interface PlannedEventInput {
  label: string
  description?: string | null
  starts_at: string
  ends_at: string
  /** Null expects either direction. */
  direction?: 'spike' | 'drop' | null
  scope_type?: 'project_total' | 'event_type' | 'event' | 'metric' | null
  scope_ref?: string | null
}

export const plannedEventsApi = {
  list: (
    slug: string,
    params?: {
      scope_type?: string
      scope_ref?: string
      from?: string
      to?: string
    },
  ) => {
    const sp = new URLSearchParams()
    if (params?.scope_type) sp.set('scope_type', params.scope_type)
    if (params?.scope_ref) sp.set('scope_ref', params.scope_ref)
    if (params?.from) sp.set('time_from', params.from)
    if (params?.to) sp.set('time_to', params.to)
    const qs = sp.toString()
    return api.get<PlannedEvent[]>(`/projects/${slug}/planned-events${qs ? `?${qs}` : ''}`)
  },

  /** Recurring windows the project's expected verdicts point at (#271). */
  suggestions: (slug: string) =>
    api.get<PlannedWindowSuggestion[]>(`/projects/${slug}/planned-events/suggestions`),

  create: (slug: string, data: PlannedEventInput) =>
    api.post<PlannedEvent>(`/projects/${slug}/planned-events`, data),

  update: (slug: string, id: string, data: Partial<PlannedEventInput>) =>
    api.patch<PlannedEvent>(`/projects/${slug}/planned-events/${id}`, data),

  delete: (slug: string, id: string) =>
    api.del<void>(`/projects/${slug}/planned-events/${id}`),
}
