/**
 * Copy for the events table's zero-result state.
 *
 * The table used to render one hard-coded first-run card ("No events yet" /
 * "Create your first event to get started.") for every empty result, so the
 * Archived tab of a project with 2,413 events told the user it had none and
 * pointed them at the wrong action. The message has to describe
 * the query that came back empty, not the project.
 */
export interface EventsEmptyCopy {
  title: string
  description: string
  /** Whether the "create your first event" CTA is the right next step. */
  isFirstRun: boolean
}

export interface EventsEmptyContext {
  /** Active tab segment: 'all', 'review', 'archived', or an event-type name. */
  activeTab: string
  /** Any column/status/tag filter is narrowing the query. */
  hasActiveFilters: boolean
  /** Current search box contents. */
  search: string
  /** The event type's display name on a type tab. The tab segment is the
   *  internal key ("se"), which the type chip stopped showing. */
  typeLabel?: string
}

const TAB_EMPTY: Record<string, { title: string; description: string }> = {
  archived: {
    title: 'No archived events',
    description:
      'Events land here when you archive them — retiring an event from the plan keeps its history without cluttering the active list.',
  },
  review: {
    title: 'Nothing waiting for review',
    description:
      'Events discovered by a scan appear here until someone accepts or rejects them. Everything is triaged.',
  },
}

export function eventsEmptyCopy({
  activeTab,
  hasActiveFilters,
  search,
  typeLabel,
}: EventsEmptyContext): EventsEmptyCopy {
  // A filter or search that matches nothing is about the query, never the
  // project — it takes priority over the tab's own copy.
  if (hasActiveFilters || search.trim()) {
    return {
      title: 'No events match these filters',
      description: 'Clear the search or filters to see the rest of this tab.',
      isFirstRun: false,
    }
  }
  const tabCopy = TAB_EMPTY[activeTab]
  if (tabCopy) return { ...tabCopy, isFirstRun: false }
  if (activeTab !== 'all') {
    return {
      title: `No events in ${typeLabel || activeTab}`,
      description: 'This event type has no events yet.',
      isFirstRun: true,
    }
  }
  return {
    title: 'No events yet',
    description: 'Create your first event to get started.',
    isFirstRun: true,
  }
}
