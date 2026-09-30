import type { PropertyDrift, PropertyDriftKind } from '@/api/propertyDrifts'

/**
 * Words for property drift (F23, #306), shared by the event page's list and the
 * Properties page's roster. The alert message says the same things
 * (`alert_templates._PROPERTY_DRIFT_KIND_LABELS`), so a reader who followed an
 * alert here finds the headline it quoted.
 */
export const PROPERTY_DRIFT_KIND_LABEL: Record<PropertyDriftKind, string> = {
  missing_required: 'Missing required property',
  type_change: 'Property type changed',
  new_property: 'New property',
}

/**
 * Most serious first: a required property going missing and a type change
 * break the event's consumers; a new property is the plan behind the data.
 */
const KIND_ORDER: Record<PropertyDriftKind, number> = {
  missing_required: 0,
  type_change: 1,
  new_property: 2,
}

export function sortPropertyDrifts(drifts: readonly PropertyDrift[]): PropertyDrift[] {
  return [...drifts].sort(
    (a, b) =>
      KIND_ORDER[a.kind] - KIND_ORDER[b.kind]
      || a.variable_name.localeCompare(b.variable_name)
      || (a.event_name ?? '').localeCompare(b.event_name ?? ''),
  )
}

function percent(rate: number | undefined): string {
  const value = Math.round((rate ?? 0) * 1000) / 10
  return `${Number.isInteger(value) ? value.toFixed(0) : value.toFixed(1)}%`
}

/** What the scan saw, in one sentence. */
export function describePropertyDrift(drift: PropertyDrift): string {
  const { detail } = drift
  switch (drift.kind) {
    case 'missing_required':
      if (!detail.presence_rate) return 'Required, but absent from every row the scan read.'
      return `Required, but carried on ${percent(detail.presence_rate)} of rows (threshold ${percent(detail.threshold)}).`
    case 'type_change':
      return `Typed ${detail.expected_type ?? '?'}, but the scan saw ${detail.observed_type ?? '?'} values.`
    case 'new_property':
      return `Carried on ${percent(detail.presence_rate)} of rows, but not on the event's property list.`
  }
}

/** What "Accept" changes in the plan, as the button says it. */
export function acceptLabel(drift: PropertyDrift): string {
  switch (drift.kind) {
    case 'missing_required':
      return 'Make optional'
    case 'type_change':
      return drift.detail.observed_type ? `Retype to ${drift.detail.observed_type}` : 'Retype'
    case 'new_property':
      return 'Add to list'
  }
}

/** Active as the backend counts it: open, or snoozed until a past instant. */
export function isActivePropertyDrift(drift: PropertyDrift, now: number = Date.now()): boolean {
  if (drift.status === 'open') return true
  if (drift.status !== 'snoozed') return false
  return drift.snoozed_until === null || Date.parse(drift.snoozed_until) <= now
}
