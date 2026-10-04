import { api, withBranch } from './client'
import type { EventTypeRelation } from '../types'

export const relationsApi = {
  list: (slug: string, branchId?: string | null) =>
    api.get<EventTypeRelation[]>(withBranch(`/projects/${slug}/relations`, branchId)),
  create: (
    slug: string,
    data: {
      source_event_type_id: string
      target_event_type_id: string
      source_field_id: string
      target_field_id: string
      relation_type?: string
      description?: string
    },
    branchId?: string | null,
  ) =>
    api.post<EventTypeRelation>(
      withBranch(`/projects/${slug}/relations`, branchId),
      data,
    ),
  // Edit in place; an end is re-checked server-side when it moves.
  update: (
    slug: string,
    id: string,
    data: Partial<{
      source_event_type_id: string
      target_event_type_id: string
      source_field_id: string
      target_field_id: string
      relation_type: string
      description: string
    }>,
    branchId?: string | null,
  ) =>
    api.patch<EventTypeRelation>(withBranch(`/projects/${slug}/relations/${id}`, branchId), data),
  del: (slug: string, id: string, branchId?: string | null) =>
    api.del(withBranch(`/projects/${slug}/relations/${id}`, branchId)),
}
