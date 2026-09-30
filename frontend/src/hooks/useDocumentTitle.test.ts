// @vitest-environment jsdom
import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import {
  buildDocumentTitle,
  entityTitleLabel,
  resolveEntityKind,
  resolveTitleFromPath,
  useDocumentTitle,
} from './useDocumentTitle'

const SEP = ' · '

describe('buildDocumentTitle', () => {
  it('includes the page label and project slug for a project-scoped route', () => {
    expect(buildDocumentTitle('Anomalies', 'acme')).toBe(
      `Anomalies${SEP}acme${SEP}tripl`,
    )
    expect(buildDocumentTitle('Events', 'acme')).toBe(`Events${SEP}acme${SEP}tripl`)
  })

  it('omits the slug segment when there is no active project', () => {
    expect(buildDocumentTitle('Settings')).toBe(`Settings${SEP}tripl`)
    expect(buildDocumentTitle('Settings', null)).toBe(`Settings${SEP}tripl`)
    expect(buildDocumentTitle('Settings', '')).toBe(`Settings${SEP}tripl`)
  })

  it('drops blank or whitespace-only segments so no empty separators appear', () => {
    expect(buildDocumentTitle('')).toBe('tripl')
    expect(buildDocumentTitle('   ')).toBe('tripl')
    expect(buildDocumentTitle('   ', '  ')).toBe('tripl')
  })

  it('trims surrounding whitespace on each segment', () => {
    expect(buildDocumentTitle('  Coverage  ', '  acme  ')).toBe(
      `Coverage${SEP}acme${SEP}tripl`,
    )
  })
})

describe('useDocumentTitle', () => {
  const originalTitle = document.title
  afterEach(() => {
    document.title = originalTitle
  })

  it('writes the composed title to document.title', () => {
    renderHook(() => useDocumentTitle('Metrics', 'acme'))
    expect(document.title).toBe(`Metrics${SEP}acme${SEP}tripl`)
  })

  it('updates document.title when the label or slug changes', () => {
    const { rerender } = renderHook(
      ({ label, slug }: { label: string; slug?: string }) =>
        useDocumentTitle(label, slug),
      { initialProps: { label: 'Events', slug: 'acme' } },
    )
    expect(document.title).toBe(`Events${SEP}acme${SEP}tripl`)

    rerender({ label: 'Reconciliation', slug: 'acme' })
    expect(document.title).toBe(`Reconciliation${SEP}acme${SEP}tripl`)
  })
})

describe('resolveTitleFromPath', () => {
  it('labels a project-scoped surface and carries its slug', () => {
    expect(resolveTitleFromPath('/p/acme/anomalies')).toEqual({ label: 'Anomalies', slug: 'acme' })
    expect(resolveTitleFromPath('/p/acme/overview')).toEqual({ label: 'Overview', slug: 'acme' })
    expect(resolveTitleFromPath('/p/acme/duplicates')).toEqual({ label: 'Duplicates', slug: 'acme' })
    // A bare project path lands on Events (the default surface).
    expect(resolveTitleFromPath('/p/acme')).toEqual({ label: 'Events', slug: 'acme' })
  })

  it('names an unmatched project sub-path as not-found while keeping the slug', () => {
    // tripl-jfm3.3: `/p/acme/<no-such-surface>` renders the 404 page, so the tab
    // must say so instead of inheriting the Events label. The slug is still
    // valid, so the project keeps naming the tab.
    expect(resolveTitleFromPath('/p/acme/this-route-does-not-exist')).toEqual({
      label: 'Page not found',
      slug: 'acme',
    })
    expect(resolveTitleFromPath('/p/acme/no-such-surface-either')).toEqual({
      label: 'Page not found',
      slug: 'acme',
    })
  })

  it('titles the top-level Scans surface and its detail pages', () => {
    // Scans moved out of `/settings` and is a real surface segment now; without
    // an entry in PROJECT_SURFACE_LABELS the tab would read "Page not found" on
    // a page that renders perfectly.
    expect(resolveTitleFromPath('/p/acme/scans')).toEqual({ label: 'Scans', slug: 'acme' })
    // A scan id is not a sub-surface, so the detail page inherits "Scans".
    expect(resolveTitleFromPath('/p/acme/scans/scan-1')).toEqual({ label: 'Scans', slug: 'acme' })
    // The legacy path still resolves — it redirects, and the frame before the
    // redirect commits must not flash not-found.
    expect(resolveTitleFromPath('/p/acme/settings/scans')).toEqual({
      label: 'Scans',
      slug: 'acme',
    })
  })

  it('titles the Plan, Observe and Govern surfaces at their top-level routes (JR-25)', () => {
    const cases: Array<[string, string]> = [
      ['/p/acme/event-types', 'Event types'],
      ['/p/acme/event-types/abc123', 'Event types'],
      ['/p/acme/meta-fields', 'Meta fields'],
      ['/p/acme/variables', 'Properties'],
      ['/p/acme/variables/v-1', 'Properties'],
      ['/p/acme/relations', 'Relations'],
      ['/p/acme/branches', 'Plan branches'],
      ['/p/acme/branches/b-1', 'Plan branches'],
      ['/p/acme/history', 'Plan history'],
      ['/p/acme/alerting', 'Alerting'],
      ['/p/acme/alerting/d-1', 'Alerting'],
      ['/p/acme/audit', 'Audit log'],
    ]
    for (const [path, label] of cases) {
      expect(resolveTitleFromPath(path)).toEqual({ label, slug: 'acme' })
    }
  })

  it('keeps redirect-only surfaces on a real label so they never flash not-found', () => {
    // The old /settings/<surface> addresses redirect to the top-level routes.
    expect(resolveTitleFromPath('/p/acme/settings/alerting')).toEqual({
      label: 'Alerting',
      slug: 'acme',
    })
    expect(resolveTitleFromPath('/p/acme/settings/audit')).toEqual({
      label: 'Audit log',
      slug: 'acme',
    })
    expect(resolveTitleFromPath('/p/acme/fact-tables')).toEqual({
      label: 'Fact tables',
      slug: 'acme',
    })
    expect(resolveTitleFromPath('/data-sources')).toEqual({ label: 'Data sources' })
    expect(resolveTitleFromPath('/account')).toEqual({ label: 'Profile' })
  })

  it('labels a sub-surface that is its own destination rather than its parent surface', () => {
    // Old /settings/<surface> addresses, which redirect to the top-level route.
    expect(resolveTitleFromPath('/p/acme/settings/event-types')).toEqual({
      label: 'Event types',
      slug: 'acme',
    })
    expect(resolveTitleFromPath('/p/acme/settings/meta-fields')).toEqual({
      label: 'Meta fields',
      slug: 'acme',
    })
    expect(resolveTitleFromPath('/p/acme/metrics/fact-tables')).toEqual({
      label: 'Fact tables',
      slug: 'acme',
    })
    // Deeper segments (detail ids, editors) inherit the sub-surface label.
    expect(resolveTitleFromPath('/p/acme/settings/event-types/abc123')).toEqual({
      label: 'Event types',
      slug: 'acme',
    })
    expect(resolveTitleFromPath('/p/acme/metrics/fact-tables/new')).toEqual({
      label: 'Fact tables',
      slug: 'acme',
    })
  })

  it('names the settings sub-surfaces that name themselves (tripl-34tw, tripl-ebib)', () => {
    // These two shared "Project settings" with the general tab while their own
    // headings read "Detection settings" and "Plan history" — the only two
    // routes in the production walk where the tab, the breadcrumb and the page
    // heading all disagreed. The label here is the page's own heading.
    expect(resolveTitleFromPath('/p/acme/settings/monitoring')).toEqual({
      label: 'Detection settings',
      slug: 'acme',
    })
    expect(resolveTitleFromPath('/p/acme/settings/history')).toEqual({
      label: 'Plan history',
      slug: 'acme',
    })
  })

  it('keeps the project configuration tab on the parent settings label', () => {
    // `general` really is project configuration rather than a destination of its
    // own, so it stays named by its parent.
    expect(resolveTitleFromPath('/p/acme/settings/general')).toEqual({
      label: 'Project settings',
      slug: 'acme',
    })
    // `/events/event-types` is not a real route: it matches the Events tab
    // pattern and renders a filtered catalog, so it must stay on "Events".
    expect(resolveTitleFromPath('/p/acme/events/event-types')).toEqual({
      label: 'Events',
      slug: 'acme',
    })
  })

  it('keeps every other sub-path of a surface on the parent surface label', () => {
    // Events tabs are filtered views of the same catalog, not their own surface.
    expect(resolveTitleFromPath('/p/acme/events')).toEqual({ label: 'Events', slug: 'acme' })
    expect(resolveTitleFromPath('/p/acme/events/review')).toEqual({ label: 'Events', slug: 'acme' })
    expect(resolveTitleFromPath('/p/acme/events/archived')).toEqual({ label: 'Events', slug: 'acme' })
    // A per-event-type tab, and an event opened from one, are still Events.
    expect(resolveTitleFromPath('/p/acme/events/checkout_completed')).toEqual({
      label: 'Events',
      slug: 'acme',
    })
    expect(resolveTitleFromPath('/p/acme/events/all/42')).toEqual({ label: 'Events', slug: 'acme' })
    // The metric editors stay on Metrics.
    expect(resolveTitleFromPath('/p/acme/metrics')).toEqual({ label: 'Metrics', slug: 'acme' })
    expect(resolveTitleFromPath('/p/acme/metrics/new')).toEqual({ label: 'Metrics', slug: 'acme' })
  })

  it('labels the full-takeover Settings routes that mount outside the shell (no slug)', () => {
    expect(resolveTitleFromPath('/settings/members')).toEqual({ label: 'Members' })
    expect(resolveTitleFromPath('/settings/data-sources')).toEqual({ label: 'Data sources' })
    expect(resolveTitleFromPath('/settings')).toEqual({ label: 'Settings' })
  })

  it('gives each two-segment settings section its own title (tripl-xl9r)', () => {
    // Eleven routes used to share three titles, because the lookup read only the
    // first path segment and the rail's paths are a mix of one and two segments.
    // The owner-operator configuring an instance has several of these open at
    // once and could not tell the tabs apart, and history search for "Storage"
    // found nothing.
    expect(resolveTitleFromPath('/settings/instance/runtime')).toEqual({ label: 'Runtime' })
    expect(resolveTitleFromPath('/settings/instance/storage')).toEqual({ label: 'Storage' })
    expect(resolveTitleFromPath('/settings/instance/security')).toEqual({
      label: 'Security & access',
    })
    expect(resolveTitleFromPath('/settings/project/plan-rules')).toEqual({ label: 'Plan rules' })
    expect(resolveTitleFromPath('/settings/project/members')).toEqual({ label: 'Access' })

    // The seven instance sections are distinguishable from each other, which is
    // the property that was actually broken.
    const instanceTitles = [
      'runtime',
      'ai',
      'email',
      'security',
      'storage',
      'observability',
      'system',
    ].map((section) => resolveTitleFromPath(`/settings/instance/${section}`).label)
    expect(new Set(instanceTitles).size).toBe(instanceTitles.length)
    // The account-level Security section keeps its own, different name.
    expect(resolveTitleFromPath('/settings/security')).toEqual({ label: 'Password & sessions' })
  })

  it('keeps the rail label on a route deeper than its rail entry', () => {
    // Opening a data source from the list navigates to /settings/data-sources/<id>,
    // which the rail lists only one segment shorter. Matching the full section
    // path alone dropped it to the generic "Settings".
    expect(
      resolveTitleFromPath('/settings/data-sources/0f8fad5b-d9cb-469f-a165-70867728950e'),
    ).toEqual({ label: 'Data sources' })
    // The same inheritance one level down from a two-segment rail entry.
    expect(resolveTitleFromPath('/settings/instance/runtime/anything')).toEqual({
      label: 'Runtime',
    })
  })

  it('falls back to the section parent for a path the settings rail does not list', () => {
    // Bare parents and the retired top-level sections that only redirect into a
    // child still have to name a tab rather than reading "Settings" or flashing
    // not-found.
    expect(resolveTitleFromPath('/settings/instance')).toEqual({ label: 'Instance settings' })
    expect(resolveTitleFromPath('/settings/project')).toEqual({ label: 'Project settings' })
    expect(resolveTitleFromPath('/settings/instance/no-such-section')).toEqual({
      label: 'Instance settings',
    })
  })

  it('labels the invite acceptance page rather than calling it not-found', () => {
    // /invite/:token is a real rendered page that sets no title of its own, and
    // it is the first (often only) tripl page an invited member opens — reading
    // "Page not found" on it looked like a dead link (tripl-l33u.12).
    expect(resolveTitleFromPath('/invite/abc123')).toEqual({ label: 'Invitation' })
    expect(buildDocumentTitle(resolveTitleFromPath('/invite/abc123').label)).toBe(
      `Invitation${SEP}tripl`,
    )
  })

  it('labels auth, workspace and the root, and names unmatched paths not-found', () => {
    expect(resolveTitleFromPath('/auth')).toEqual({ label: 'Sign in' })
    expect(resolveTitleFromPath('/')).toEqual({ label: 'All projects' })
    expect(resolveTitleFromPath('/workspace')).toEqual({ label: 'All projects' })
    expect(resolveTitleFromPath('/nope')).toEqual({ label: 'Page not found' })
    expect(buildDocumentTitle(resolveTitleFromPath('/nope').label)).toBe(
      `Page not found${SEP}tripl`,
    )
  })
})

describe('detail-page tab titles (JR-33)', () => {
  it('names the kind of entity a detail route shows', () => {
    expect(resolveEntityKind('/p/acme/monitoring/event-type/et-1')).toBe('Event type volume')
    expect(resolveEntityKind('/p/acme/monitoring/event/ev-1/breakdowns')).toBe('Event')
    expect(resolveEntityKind('/p/acme/monitoring/metric/m-1')).toBe('Metric')
    expect(resolveEntityKind('/p/acme/monitors/r-1')).toBe('Alert rule')
    expect(resolveEntityKind('/p/acme/scans/s-1')).toBe('Scan')
  })

  it('is null off a detail route', () => {
    expect(resolveEntityKind('/p/acme/monitoring')).toBeNull()
    expect(resolveEntityKind('/p/acme/monitors')).toBeNull()
    expect(resolveEntityKind('/p/acme/events')).toBeNull()
    expect(resolveEntityKind('/settings/members')).toBeNull()
  })

  it('leads the tab with the entity name, then its kind', () => {
    expect(buildDocumentTitle(entityTitleLabel('Screen View', 'Event type volume'))).toBe(
      `Screen View${SEP}Event type volume${SEP}tripl`,
    )
    expect(buildDocumentTitle(entityTitleLabel('Spike & drift watch', 'Alert rule'))).toBe(
      `Spike & drift watch${SEP}Alert rule${SEP}tripl`,
    )
  })
})
