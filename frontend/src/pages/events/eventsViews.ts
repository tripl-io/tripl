import type { EventType } from '@/types'

/** The catalog's views: the queues had routes, but nothing on the page led to them. */
export const EVENT_VIEWS = [
  { tab: 'all', label: 'All' },
  { tab: 'review', label: 'Review queue' },
  { tab: 'archived', label: 'Archived' },
] as const

/** The page title for each view: the review route used to say "Events". */
export function eventsPageTitle(activeTab: string, activeType: EventType | null): string {
  if (activeType) return `${activeType.display_name} events`
  if (activeTab === 'review') return 'Review queue'
  if (activeTab === 'archived') return 'Archived events'
  return 'Events'
}
