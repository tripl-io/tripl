import type { MonitoringSignal } from '@/types'

/** How each scope kind is spelled ahead of the thing that fired. */
const SCOPE_KIND_LABELS: Record<string, string> = {
  event_type: 'Event type',
  event: 'Event',
  metric: 'Metric',
  schema_drift: 'Schema drift',
  distribution_drift: 'Distribution drift',
  variable_value_drift: 'Value drift',
  release_regression: 'Release regression',
  source_freshness: 'Data late',
  lifecycle: 'Lifecycle',
  property_drift: 'Property drift',
}

function scopeKind(signal: MonitoringSignal): string {
  return SCOPE_KIND_LABELS[signal.scope_type] ?? signal.scope_type
}

/**
 * "<Scope> · <name>" for one signal, or null when the server could not name it.
 *
 * One definition, because there were three and one had drifted: the top bar's
 * copy ended in `return \`event ${shortRef}\``, so every scope it did not name
 * explicitly — metric, schema drift, distribution drift, variable-value drift,
 * release regression — was announced to the operator as an *event*.
 * Those scopes only became reachable in the bell when it
 * switched to the expanded list, which is how a wrong fallback
 * that had always been there started lying out loud.
 *
 * The name rides on the signal. `_attach_scope_names` resolves event ->
 * `Event.name`, event_type -> `EventType.display_name` and metric -> the
 * catalog `display_name` in three batched queries for the whole page, so no
 * surface downloads a catalog to label a row: the bell fanned out one GET per
 * event id plus the event-type list and the metrics catalog, Overview kept a
 * second copy of that same machinery, and both printed "Event d4c684dd" until
 * it landed.
 *
 * A null name is terminal, never "not here yet": the scope FKs are
 * `ondelete=SET NULL`, and the kinds with no entity behind them (schema,
 * distribution, release regression, value drift) are never named at all. Render
 * {@link unnamedScopeLabel} for it — printing the ref in its place is the whole
 * of the fix.
 */
export function signalScopeLabel(signal: MonitoringSignal): string | null {
  if (signal.scope_type === 'project_total') return 'Project total'
  if (!signal.scope_name) return null
  return `${scopeKind(signal)} · ${signal.scope_name}`
}

/**
 * What an unnameable scope is called, in words.
 *
 * Only these three kinds are ever looked up, so a null name beside a populated
 * `scope_ref` means the entity is gone. The rest have no entity behind them and
 * are never named at all, so they get the neutral word rather than a "deleted"
 * that would not be true.
 */
const UNNAMED_SCOPE_NOUN: Partial<Record<MonitoringSignal['scope_type'], string>> = {
  event: 'deleted event',
  event_type: 'deleted event type',
  metric: 'deleted metric',
}

export function unnamedScopeLabel(signal: MonitoringSignal): string {
  return UNNAMED_SCOPE_NOUN[signal.scope_type] ?? 'unnamed scope'
}

/**
 * "<Scope> <ref8>" — for an accessible name or a `title`, never for the row.
 *
 * A hex prefix reads as a name, so the incident the activity rail calls
 * `spot_auto_change_model` shows up as "Event d4c684dd" and the two surfaces
 * disagree about what fired. Keeping the ref out of the visible
 * label but in the DOM is what still lets an operator match an unnameable row
 * back to the detector.
 */
export function signalScopeRefLabel(signal: MonitoringSignal): string {
  if (signal.scope_type === 'project_total') return 'Project total'
  return `${scopeKind(signal)} ${signal.scope_ref.slice(0, 8)}`
}
