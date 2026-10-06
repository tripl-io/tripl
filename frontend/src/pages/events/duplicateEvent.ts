import type { Event as TEvent, EventType, FieldDefinition, MetaFieldDefinition } from '@/types'
import { isNumberFieldValue, normalizeMetricBreakdownColumns } from './eventFormValues'

/**
 * What a new event starts from when it is a duplicate of an existing one, in
 * the form's own shapes and with every id already one of the active plan's.
 * Status, sunset date and successor are not here on purpose: they describe the
 * source's lifecycle, and a new event starts as Draft.
 */
export interface EventSeed {
  eventTypeId: string
  /** The source's name for a type without a scan rule; empty under a rule,
   *  which composes the name from the field values instead. */
  name: string
  title: string
  description: string
  ownerId: string
  metricBreakdownColumns: string[]
  tags: string[]
  fieldValues: Record<string, string>
  metaValues: Record<string, string[]>
  /** Sent with the create only: the form has no control for it. */
  requiredPresenceThreshold: number | null
}

/** The event being duplicated, as the form names and checks it. */
export interface DuplicateSource {
  id: string
  name: string
  /** What a scan matches the source on: the identity a rule-governed copy
   *  would collide with. */
  identity: string
  eventTypeId: string
}

export interface EventDuplicate {
  source: DuplicateSource
  seed: EventSeed
  /** Values that could not be carried onto the active plan's definitions. */
  dropped: string[]
}

/**
 * The seed for duplicating `source` onto the plan the form is editing.
 *
 * Ids are branch-local: a branch copy of a type, field or meta field has a new
 * id, and a deep link can name a main event while a branch is active. So each
 * id is kept when the active lists have it and otherwise followed by name,
 * through `sourceEventTypes` / `sourceMetaFields` — the lists the source's ids
 * come from, when they are not the active ones. What cannot be followed is
 * named in `dropped` rather than lost without a word. Null when the source's
 * type does not exist on this plan at all.
 *
 * A number value that is not a number (a scan may have stored `N/A`) is
 * dropped too: on the new event it would count as typed, and the form refuses
 * to create an event with it.
 */
export function duplicateSeed(
  source: TEvent,
  {
    eventTypes,
    metaFields,
    sourceEventTypes = eventTypes,
    sourceMetaFields = metaFields,
  }: {
    eventTypes: readonly EventType[]
    metaFields: readonly MetaFieldDefinition[]
    sourceEventTypes?: readonly EventType[]
    sourceMetaFields?: readonly MetaFieldDefinition[]
  },
): EventDuplicate | null {
  const targetType =
    eventTypes.find(et => et.id === source.event_type_id)
    ?? eventTypes.find(et => et.name === source.event_type?.name)
  if (!targetType) return null

  const dropped: string[] = []
  const sourceFields = new Map<string, FieldDefinition>(
    (sourceEventTypes.find(et => et.id === source.event_type_id)?.field_definitions ?? [])
      .map(field => [field.id, field]),
  )
  const targetById = new Map(targetType.field_definitions.map(field => [field.id, field]))
  const targetByName = new Map(targetType.field_definitions.map(field => [field.name, field]))
  const fieldValues: Record<string, string> = {}
  for (const fv of source.field_values) {
    if (fv.value === '') continue
    const sourceField = sourceFields.get(fv.field_definition_id)
    const target =
      targetById.get(fv.field_definition_id)
      ?? (sourceField ? targetByName.get(sourceField.name) : undefined)
    const label = target?.display_name ?? sourceField?.display_name ?? 'A field value'
    if (!target || (target.field_type === 'number' && !isNumberFieldValue(fv.value))) {
      dropped.push(label)
      continue
    }
    fieldValues[target.id] = fv.value
  }

  const sourceMeta = new Map(sourceMetaFields.map(mf => [mf.id, mf]))
  const metaById = new Map(metaFields.map(mf => [mf.id, mf]))
  const metaByName = new Map(metaFields.map(mf => [mf.name, mf]))
  const metaValues: Record<string, string[]> = {}
  for (const mv of source.meta_values) {
    if (mv.value === '') continue
    const sourceField = sourceMeta.get(mv.meta_field_definition_id)
    const target =
      metaById.get(mv.meta_field_definition_id)
      ?? (sourceField ? metaByName.get(sourceField.name) : undefined)
    if (!target) {
      const label = sourceField?.display_name ?? 'A meta value'
      if (!dropped.includes(label)) dropped.push(label)
      continue
    }
    ;(metaValues[target.id] ??= []).push(mv.value)
  }

  return {
    source: {
      id: source.id,
      name: source.name,
      identity: source.source_name || source.name,
      eventTypeId: targetType.id,
    },
    seed: {
      eventTypeId: targetType.id,
      name: targetType.event_name_format ? '' : source.name,
      title: source.title ?? '',
      description: source.description ?? '',
      ownerId: source.owner_id ?? '',
      metricBreakdownColumns: normalizeMetricBreakdownColumns(source.metric_breakdown_columns ?? []),
      tags: (source.tags ?? []).map(tag => tag.name),
      fieldValues,
      metaValues,
      requiredPresenceThreshold: source.required_presence_threshold ?? null,
    },
    dropped,
  }
}

/** The query parameter that names the event a new one is duplicated from. */
export const DUPLICATE_FROM_PARAM = 'from'

/**
 * The create form's URL for duplicating `sourceId`, keeping the page's query
 * string — `?branch=` above all, so the copy lands on the plan the source was
 * opened on. A link, not router state, so a reload or a pasted URL rebuilds the
 * same prefill from the server.
 */
export function duplicateHref(newEventPath: string, search: string, sourceId: string): string {
  const params = new URLSearchParams(search)
  params.set(DUPLICATE_FROM_PARAM, sourceId)
  return `${newEventPath}?${params.toString()}`
}

/** `search` without the duplicate's `from`: what a list or a blank form keeps. */
export function withoutDuplicateFrom(search: string): string {
  const params = new URLSearchParams(search)
  params.delete(DUPLICATE_FROM_PARAM)
  const rest = params.toString()
  return rest ? `?${rest}` : ''
}
