import { describe, expect, it } from 'vitest'
import { eventsEmptyCopy } from './emptyState'

const base = { activeTab: 'all', hasActiveFilters: false, search: '' }

describe('eventsEmptyCopy', () => {
  it('offers the first-run CTA only on an unfiltered "all" tab', () => {
    expect(eventsEmptyCopy(base)).toEqual({
      title: 'No events yet',
      description: 'Create your first event to get started.',
      isFirstRun: true,
    })
  })

  // The Archived tab of a project with 2,413 events used to read "No events yet
  // — create your first event to get started".
  it('explains an empty Archived tab instead of claiming the project has no events', () => {
    const copy = eventsEmptyCopy({ ...base, activeTab: 'archived' })

    expect(copy.title).toBe('No archived events')
    expect(copy.title).not.toMatch(/No events yet/)
    expect(copy.description).not.toMatch(/first event/i)
    expect(copy.isFirstRun).toBe(false)
  })

  it('explains an empty review queue as triaged, not as an empty project', () => {
    const copy = eventsEmptyCopy({ ...base, activeTab: 'review' })

    expect(copy.title).toBe('Nothing waiting for review')
    expect(copy.isFirstRun).toBe(false)
  })

  it('blames the filters when a filter or search matched nothing', () => {
    expect(eventsEmptyCopy({ ...base, hasActiveFilters: true }).title).toBe(
      'No events match these filters',
    )
    expect(eventsEmptyCopy({ ...base, search: 'checkout' }).title).toBe(
      'No events match these filters',
    )
  })

  // A filter that matches nothing is about the query even on a special tab.
  it('prefers the filter message over the tab message', () => {
    const copy = eventsEmptyCopy({ ...base, activeTab: 'archived', hasActiveFilters: true })

    expect(copy.title).toBe('No events match these filters')
  })

  it('names the event type on an empty per-type tab', () => {
    const copy = eventsEmptyCopy({ ...base, activeTab: 'checkout' })

    expect(copy.title).toBe('No events in checkout')
  })

  it('names the type by its display name, not the internal key (EVT-39)', () => {
    const copy = eventsEmptyCopy({ ...base, activeTab: 'se', typeLabel: 'Structured Event' })

    expect(copy.title).toBe('No events in Structured Event')
    // The table renders the "New event" action for a first run; the copy no
    // longer promises one in words.
    expect(copy.isFirstRun).toBe(true)
    expect(copy.description).not.toMatch(/Add one/)
  })
})
