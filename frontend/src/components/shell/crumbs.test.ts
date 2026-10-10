import { describe, expect, it } from 'vitest'
import { editPageTitle } from '@/components/shell-chrome-context'
import { resolveProjectEditorPage } from '@/lib/navigation'
import { resolveCrumbs, topBarHeading } from './crumbs'

const PROJECT = { label: 'Demo', to: '/p/demo/overview' }

describe('resolveCrumbs: one row of a list surface', () => {
  it.each([
    ['/p/demo/event-types/et-1', 'Plan', 'Event types', '/p/demo/event-types'],
    ['/p/demo/variables/v-1', 'Plan', 'Properties', '/p/demo/variables'],
    ['/p/demo/branches/b-1', 'Plan', 'Plan branches', '/p/demo/branches'],
    ['/p/demo/scans/s-1', 'Govern', 'Scans', '/p/demo/scans'],
    ['/p/demo/docs/project/guides/setup', 'Plan', 'Docs', '/p/demo/docs'],
  ])('files %s under %s › %s', (path, area, list, listHref) => {
    // The list's crumb used to be the one the row's name replaced, so the trail
    // read "Plan › Screen View" and skipped the list it came from.
    const route = resolveCrumbs(path, 'demo', 'Demo')
    expect(route.crumbs).toEqual([PROJECT, { label: area }, { label: list, to: listHref }])
    expect(topBarHeading(route, 'Screen View')).toEqual({ crumbs: route.crumbs, title: 'Screen View' })
    // Until the row has named itself, the list stands in as the title.
    expect(topBarHeading(route, null)).toEqual({ crumbs: [PROJECT, { label: area }], title: list })
  })

  it('leaves a list and its tabs on the plain nav trail', () => {
    expect(resolveCrumbs('/p/demo/event-types', 'demo', 'Demo')).toEqual({
      crumbs: [PROJECT, { label: 'Plan' }],
      title: 'Event types',
    })
    expect(resolveCrumbs('/p/demo/events/review', 'demo', 'Demo')).toEqual({
      crumbs: [PROJECT, { label: 'Plan' }],
      title: 'Events',
    })
  })
})

describe('resolveCrumbs: create and edit pages', () => {
  it('names the event editor under Events, and the event once it is loaded', () => {
    // It passed for the Events list: crumb "Plan › Events", no event named.
    const route = resolveCrumbs('/p/demo/events/all/e-1/edit', 'demo', 'Demo')
    expect(route).toEqual({
      crumbs: [PROJECT, { label: 'Plan' }, { label: 'Events', to: '/p/demo/events' }],
      title: 'Edit event',
      entityAction: 'Edit',
    })
    expect(topBarHeading(route, editPageTitle('Home Screen View'))).toEqual({
      crumbs: [...route.crumbs, { label: 'Home Screen View' }],
      title: 'Edit',
    })
  })

  it('names the event create pages instead of passing for the list', () => {
    expect(resolveCrumbs('/p/demo/events/all/new', 'demo', 'Demo').title).toBe('New event')
    expect(resolveCrumbs('/p/demo/events/checkout/bulk', 'demo', 'Demo').title).toBe('Add many events')
  })

  it('keeps Fact tables between Metrics and a fact table editor', () => {
    expect(resolveCrumbs('/p/demo/metrics/fact-tables/ft-1/edit', 'demo', 'Demo')).toEqual({
      crumbs: [
        PROJECT,
        { label: 'Observe' },
        { label: 'Metrics', to: '/p/demo/metrics' },
        { label: 'Fact tables', to: '/p/demo/metrics/fact-tables' },
      ],
      title: 'Edit fact table',
      entityAction: 'Edit',
    })
    expect(resolveCrumbs('/p/demo/metrics/new', 'demo', 'Demo')).toEqual({
      crumbs: [PROJECT, { label: 'Observe' }, { label: 'Metrics', to: '/p/demo/metrics' }],
      title: 'New metric',
    })
  })
})

describe('resolveCrumbs: no page here', () => {
  it('says "Page not found", as the page and the tab do, on an unmatched project path', () => {
    expect(resolveCrumbs('/p/demo/no-such-page', 'demo', 'Demo')).toEqual({
      crumbs: [PROJECT],
      title: 'Page not found',
    })
  })

  it('does not call a settings address in the shell "Settings": the takeover mounts outside it', () => {
    // Only the catch-all renders an unknown /settings/<x> inside the shell.
    expect(resolveCrumbs('/settings/foo')).toEqual({ crumbs: [], title: 'Page not found' })
    // The old top-level addresses still name the frame before their redirect.
    expect(resolveCrumbs('/data-sources/ds-1')).toEqual({ crumbs: [], title: 'Settings' })
    expect(resolveCrumbs('/users')).toEqual({ crumbs: [], title: 'Settings' })
  })
})

describe('resolveProjectEditorPage', () => {
  it('reads the address with or without its organization', () => {
    expect(resolveProjectEditorPage('/o/acme/p/demo/metrics/new')?.title).toBe('New metric')
    expect(resolveProjectEditorPage('/p/demo/metrics/m-1/edit')).toEqual({ title: 'Edit metric', entityAction: 'Edit' })
  })

  it('is null off a create or edit page', () => {
    expect(resolveProjectEditorPage('/p/demo/metrics')).toBeNull()
    expect(resolveProjectEditorPage('/p/demo/metrics/fact-tables')).toBeNull()
    expect(resolveProjectEditorPage('/p/demo/events/all/e-1')).toBeNull()
    expect(resolveProjectEditorPage('/settings/members')).toBeNull()
  })
})
