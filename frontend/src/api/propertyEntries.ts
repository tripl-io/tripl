import { api, withBranch } from './client'
import type { PropertySchema, VariableType } from '../types'

/**
 * An event's property list and one property's events (F23). Both read the
 * same rows — a `variable_event_value_overrides` entry per (property, event) —
 * from either side. The single-entry writes stay in `variableOverrides.ts`.
 */

/** One entry of an event's property list, `GET /events/{id}/properties`. */
export interface EventPropertyEntry {
  id: string
  variable_id: string
  name: string
  variable_type: VariableType
  json_schema: PropertySchema | null
  description: string
  required: boolean
  /** This event's override of the allowed values; null when the global list applies. */
  values: string[] | null
  /** The allowed values in force for this event. */
  effective_values: string[]
  /** Share of the event's rows that carried the property at the last scan. */
  presence_rate: number | null
  /** Whether presence reaches the event's threshold; null when unknown. */
  suggested_required: boolean | null
}

/** One event whose property list carries the property, `GET /properties/{id}/events`. */
export interface PropertyEventEntry {
  event_id: string
  event_name: string
  event_type_id: string
  status: string
  required: boolean
  values: string[] | null
  effective_values: string[]
  presence_rate: number | null
  /** The event's own threshold; null means the default (0.95). */
  required_presence_threshold: number | null
  suggested_required: boolean | null
}

/** The patch a bulk edit applies to each listed event's entry. */
export interface PropertyEntriesBulkPatch {
  required?: boolean
  /** null drops the override; the global list applies again. */
  values?: string[] | null
}

export interface PropertyEntriesBulkResult {
  created: number
  updated: number
  removed: number
}

/** The backend's ceiling on one bulk request (schemas/property_events.py). */
export const PROPERTY_BULK_EVENT_LIMIT = 5000

export const propertyEntriesApi = {
  forEvent: (slug: string, eventId: string, branchId?: string | null) =>
    api.get<EventPropertyEntry[]>(withBranch(`/projects/${slug}/events/${eventId}/properties`, branchId)),
  forProperty: (slug: string, variableId: string, branchId?: string | null) =>
    api.get<PropertyEventEntry[]>(withBranch(`/projects/${slug}/properties/${variableId}/events`, branchId)),
  /** Add the property to one event's list, or patch its entry there. */
  set: (
    slug: string,
    variableId: string,
    eventId: string,
    patch: PropertyEntriesBulkPatch,
    branchId?: string | null,
  ) =>
    api.put<unknown>(
      withBranch(`/projects/${slug}/properties/${variableId}/event-overrides/${eventId}`, branchId),
      patch,
    ),
  /** Take the property off one event's list; its override goes with it. */
  remove: (slug: string, variableId: string, eventId: string, branchId?: string | null) =>
    api.del(withBranch(`/projects/${slug}/properties/${variableId}/event-overrides/${eventId}`, branchId)),
  /** One patch on many events: adds the property where an event lacks it. All or nothing. */
  bulkSet: (
    slug: string,
    variableId: string,
    eventIds: string[],
    patch: PropertyEntriesBulkPatch,
    branchId?: string | null,
  ) =>
    api.post<PropertyEntriesBulkResult>(
      withBranch(`/projects/${slug}/properties/${variableId}/event-overrides/bulk`, branchId),
      { event_ids: eventIds, ...patch },
    ),
  bulkRemove: (slug: string, variableId: string, eventIds: string[], branchId?: string | null) =>
    api.post<PropertyEntriesBulkResult>(
      withBranch(`/projects/${slug}/properties/${variableId}/event-overrides/bulk-delete`, branchId),
      { event_ids: eventIds },
    ),
}
