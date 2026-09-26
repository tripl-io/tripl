import { api } from './client'
import type { ChartAnnotation } from '../types'

export interface ChartAnnotationInput {
  bucket: string
  label: string
  description?: string | null
  color?: string
  scope_type?: 'project_total' | 'event_type' | 'event' | 'metric' | null
  scope_ref?: string | null
  /** Omitted means `manual`; `release` is reserved to the metrics worker. */
  source?: 'manual' | 'api'
  url?: string | null
}

export const chartAnnotationsApi = {
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
    return api.get<ChartAnnotation[]>(`/projects/${slug}/annotations${qs ? `?${qs}` : ''}`)
  },

  create: (slug: string, data: ChartAnnotationInput) =>
    api.post<ChartAnnotation>(`/projects/${slug}/annotations`, data),

  delete: (slug: string, id: string) =>
    api.del<void>(`/projects/${slug}/annotations/${id}`),
}
