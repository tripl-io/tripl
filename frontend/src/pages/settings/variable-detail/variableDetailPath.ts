
import { currentOrgSlug, projectPath } from '@/lib/navigation'
/** The sections of the variable page, in tab order. */
export const VARIABLE_DETAIL_TABS = [
  { id: 'definition', label: 'Definition' },
  { id: 'events', label: 'Events' },
  { id: 'drift', label: 'Drift' },
  { id: 'overrides', label: 'Overrides' },
  { id: 'observed', label: 'Observed' },
] as const

export type VariableDetailTab = (typeof VARIABLE_DETAIL_TABS)[number]['id']

export function isVariableDetailTab(value: string | null): value is VariableDetailTab {
  return VARIABLE_DETAIL_TABS.some((tab) => tab.id === value)
}

/** `/p/:slug/variables/:id`, optionally on one tab. */
export function variableDetailPath(slug: string, variableId: string, tab?: VariableDetailTab): string {
  const base = projectPath(currentOrgSlug(), slug, `/variables/${variableId}`)
  return tab && tab !== 'definition' ? `${base}?tab=${tab}` : base
}

/** The list with the variable's row focused: where the page's back link goes. */
export function variableListPath(slug: string, focusId?: string): string {
  const base = projectPath(currentOrgSlug(), slug, '/variables')
  return focusId ? `${base}?focus=${encodeURIComponent(focusId)}` : base
}

/** `/events?property=<name>`: the events list narrowed to one property (F23). */
export function eventsWithPropertyPath(slug: string, propertyName: string): string {
  return projectPath(currentOrgSlug(), slug, `/events?property=${encodeURIComponent(propertyName)}`)
}
