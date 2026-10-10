// @vitest-environment jsdom
import { createElement, type ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { ApiError } from '@/api/client'
import { SETTINGS_NAV } from '@/components/settings/nav'
import { editPageTitle } from '@/components/shell-chrome-context'
import { buildNavGroups } from '@/lib/navigation'
import { projectQueryOptions, projectsQueryOptions } from '@/lib/queryKeys'
import type { Project } from '@/types'
import {
  NOT_FOUND_TITLE_LABEL,
  buildDocumentTitle,
  composeDocumentTitle,
  resolveTitleFromPath,
  useDocumentTitle,
} from './useDocumentTitle'

const SEP = ' · '

describe('buildDocumentTitle', () => {
  it('joins the segments, most specific first, and ends with the app', () => {
    expect(buildDocumentTitle('Anomalies', 'Acme')).toBe(`Anomalies${SEP}Acme${SEP}tripl`)
    expect(buildDocumentTitle('Screen View', 'Event type', 'Acme')).toBe(
      `Screen View${SEP}Event type${SEP}Acme${SEP}tripl`,
    )
    expect(buildDocumentTitle('Settings')).toBe(`Settings${SEP}tripl`)
  })

  it('drops blank, null and missing segments so no empty separators appear', () => {
    expect(buildDocumentTitle('Settings', null)).toBe(`Settings${SEP}tripl`)
    expect(buildDocumentTitle('Settings', undefined, '  ')).toBe(`Settings${SEP}tripl`)
    expect(buildDocumentTitle('')).toBe('tripl')
    expect(buildDocumentTitle()).toBe('tripl')
  })

  it('trims surrounding whitespace on each segment', () => {
    expect(buildDocumentTitle('  Coverage  ', '  Acme  ')).toBe(`Coverage${SEP}Acme${SEP}tripl`)
  })
})

describe('resolveTitleFromPath: project routes', () => {
  it('labels a project surface and carries its slug', () => {
    expect(resolveTitleFromPath('/p/acme/anomalies')).toEqual({ label: 'Anomalies', slug: 'acme' })
    expect(resolveTitleFromPath('/p/acme/overview')).toEqual({ label: 'Overview', slug: 'acme' })
    expect(resolveTitleFromPath('/o/org-1/p/acme/duplicates')).toEqual({ label: 'Duplicates', slug: 'acme' })
    // A bare project address redirects to the project's home.
    expect(resolveTitleFromPath('/p/acme')).toEqual({ label: 'Overview', slug: 'acme' })
  })

  it('titles the Annotations page by its name, not as a 404', () => {
    // The page worked; its tab, history entry and bookmark said "Page not found".
    expect(resolveTitleFromPath('/p/acme/annotations')).toEqual({ label: 'Annotations', slug: 'acme' })
  })

  it('titles every sidebar page by the label it is listed under', () => {
    // The guard that would have caught Annotations: a page the sidebar gains
    // cannot fall through to the 404 title.
    for (const group of buildNavGroups('acme', undefined)) {
      for (const item of group.items) {
        expect(resolveTitleFromPath(item.href).label).toBe(item.label)
      }
    }
  })

  it('names an unmatched project sub-path as not-found while keeping the slug', () => {
    expect(resolveTitleFromPath('/p/acme/this-route-does-not-exist')).toEqual({
      label: NOT_FOUND_TITLE_LABEL,
      slug: 'acme',
    })
  })

  it('keeps redirect-only surfaces on a real label so they never flash not-found', () => {
    expect(resolveTitleFromPath('/p/acme/settings/alerting')).toMatchObject({ label: 'Alerting' })
    expect(resolveTitleFromPath('/p/acme/settings/audit')).toMatchObject({ label: 'Audit log' })
    expect(resolveTitleFromPath('/p/acme/settings/scans')).toMatchObject({ label: 'Scans' })
    expect(resolveTitleFromPath('/p/acme/settings/event-types/abc123')).toMatchObject({ label: 'Event types' })
    expect(resolveTitleFromPath('/p/acme/fact-tables')).toMatchObject({ label: 'Fact tables' })
    expect(resolveTitleFromPath('/p/acme/monitors')).toMatchObject({ label: 'Alert rules' })
  })

  it('names the sub-surfaces that are their own destination', () => {
    expect(resolveTitleFromPath('/p/acme/metrics/fact-tables')).toEqual({
      label: 'Fact tables',
      slug: 'acme',
      kind: 'Fact table',
    })
    // The page's own heading, which the breadcrumb leaf repeats.
    expect(resolveTitleFromPath('/p/acme/settings/monitoring')).toMatchObject({ label: 'Detection settings' })
    // General is project configuration, named by its parent.
    expect(resolveTitleFromPath('/p/acme/settings/general')).toMatchObject({ label: 'Project settings' })
  })

  it('keeps the Events tabs on the Events label', () => {
    // Filtered views of one catalog: a tab switch must not rewrite the tab.
    for (const path of ['/p/acme/events', '/p/acme/events/review', '/p/acme/events/checkout_completed']) {
      expect(resolveTitleFromPath(path).label).toBe('Events')
    }
  })

  it('names the create and edit pages instead of passing for their list', () => {
    expect(resolveTitleFromPath('/p/acme/events/all/new').label).toBe('New event')
    expect(resolveTitleFromPath('/p/acme/events/all/bulk').label).toBe('Add many events')
    expect(resolveTitleFromPath('/p/acme/events/all/e-1/edit')).toEqual({
      label: 'Edit event',
      slug: 'acme',
      kind: 'Event',
    })
    expect(resolveTitleFromPath('/p/acme/metrics/new').label).toBe('New metric')
    expect(resolveTitleFromPath('/p/acme/metrics/fact-tables/new').label).toBe('New fact table')
  })

  it('names what one row of a surface is, in the singular', () => {
    const kinds: Array<[string, string]> = [
      ['/p/acme/events/all/e-1/edit', 'Event'],
      ['/p/acme/event-types/et-1', 'Event type'],
      ['/p/acme/variables/v-1', 'Property'],
      ['/p/acme/branches/b-1', 'Plan branch'],
      ['/p/acme/docs/project/setup', 'Note'],
      ['/p/acme/metrics/m-1/edit', 'Metric'],
      // Not "Metric": a fact table's editor sits under Metrics too.
      ['/p/acme/metrics/fact-tables/ft-1/edit', 'Fact table'],
      ['/p/acme/monitors/r-1', 'Alert rule'],
      ['/p/acme/scans/s-1', 'Scan'],
      ['/p/acme/monitoring/event-type/et-1', 'Event type volume'],
      ['/p/acme/monitoring/event/ev-1/breakdowns', 'Event'],
      ['/p/acme/monitoring/metric/m-1', 'Metric'],
    ]
    for (const [path, kind] of kinds) {
      expect(resolveTitleFromPath(path).kind).toBe(kind)
    }
  })

  it('has no row kind off a row', () => {
    expect(resolveTitleFromPath('/p/acme/monitoring').kind).toBeUndefined()
    expect(resolveTitleFromPath('/p/acme/monitors').kind).toBeUndefined()
    expect(resolveTitleFromPath('/p/acme/event-types').kind).toBeUndefined()
  })
})

describe('resolveTitleFromPath: settings', () => {
  it('frames every rail section with its group', () => {
    for (const groups of Object.values(SETTINGS_NAV)) {
      for (const group of groups) {
        for (const item of group.items) {
          expect(resolveTitleFromPath(`/settings/${item.path}`)).toMatchObject({
            label: item.label,
            scope: `${group.label} settings`,
          })
        }
      }
    }
    expect(resolveTitleFromPath('/settings/members')).toEqual({ label: 'Members', scope: 'Organization settings' })
    expect(resolveTitleFromPath('/settings/instance/runtime')).toEqual({ label: 'Runtime', scope: 'Platform settings' })
    expect(resolveTitleFromPath('/settings/profile')).toEqual({ label: 'Profile', scope: 'Account settings' })
  })

  it('names the project a project section changes, from its address', () => {
    expect(resolveTitleFromPath('/settings/project/general', '?project=acme')).toEqual({
      label: 'General',
      scope: 'Project settings',
      slug: 'acme',
    })
    // Only a project section reads it.
    expect(resolveTitleFromPath('/settings/members', '?project=acme')).not.toHaveProperty('slug')
  })

  it('keeps the rail label on a route deeper than its rail entry', () => {
    expect(resolveTitleFromPath('/settings/data-sources/0f8fad5b-d9cb-469f-a165-70867728950e')).toEqual({
      label: 'Data sources',
      scope: 'Organization settings',
    })
  })

  it('titles a pre-takeover address as the section it redirects to', () => {
    expect(resolveTitleFromPath('/settings/runtime')).toEqual({ label: 'Runtime', scope: 'Platform settings' })
    expect(resolveTitleFromPath('/settings/users')).toEqual({ label: 'Members', scope: 'Organization settings' })
    expect(resolveTitleFromPath('/data-sources')).toEqual({ label: 'Data sources', scope: 'Organization settings' })
    expect(resolveTitleFromPath('/account')).toEqual({ label: 'Profile', scope: 'Account settings' })
  })

  it('keeps a settings title on the frame before an unknown section redirects', () => {
    expect(resolveTitleFromPath('/settings')).toEqual({ label: 'Settings' })
    expect(resolveTitleFromPath('/settings/organization/no-such-section')).toEqual({ label: 'Settings' })
    expect(resolveTitleFromPath('/settings/instance/no-such-section')).toEqual({ label: 'Settings' })
  })

  it('calls a settings address with no route behind it not-found, as the page does', () => {
    // It used to read "Settings" over the 404 page.
    for (const path of ['/settings/foo', '/settings/project', '/settings/project/foo', '/settings/instance']) {
      expect(resolveTitleFromPath(path)).toEqual({ label: NOT_FOUND_TITLE_LABEL })
    }
  })
})

describe('resolveTitleFromPath: pages outside projects and settings', () => {
  it('labels the pages a new member or a new account opens first', () => {
    expect(resolveTitleFromPath('/invite/abc123')).toEqual({ label: 'Invitation' })
    expect(resolveTitleFromPath('/verify-email')).toEqual({ label: 'Verify email' })
    expect(resolveTitleFromPath('/auth')).toEqual({ label: 'Sign in' })
  })

  it('labels the workspace and the root, and names unmatched paths not-found', () => {
    expect(resolveTitleFromPath('/')).toEqual({ label: 'All projects' })
    expect(resolveTitleFromPath('/workspace')).toEqual({ label: 'All projects' })
    expect(resolveTitleFromPath('/o/org-1')).toEqual({ label: 'All projects' })
    expect(resolveTitleFromPath('/nope')).toEqual({ label: NOT_FOUND_TITLE_LABEL })
  })
})

describe('composeDocumentTitle', () => {
  it('names a project page after the project, never its slug', () => {
    const route = resolveTitleFromPath('/p/demo-0793b8/overview')
    expect(composeDocumentTitle(route, { projectName: 'Demo Project' })).toBe(
      `Overview${SEP}Demo Project${SEP}tripl`,
    )
    // Until the name is known the project is left out, not echoed from the address.
    expect(composeDocumentTitle(route)).toBe(`Overview${SEP}tripl`)
  })

  it('says "Project not found" for a project the shell could not find', () => {
    const route = resolveTitleFromPath('/p/no-such-project/overview')
    expect(composeDocumentTitle(route, { projectMissing: true })).toBe(`Project not found${SEP}tripl`)
  })

  it('leads a row page with the row, then its kind, then the project', () => {
    expect(
      composeDocumentTitle(resolveTitleFromPath('/p/acme/branches/b-1'), {
        entity: 'feature/checkout-funnel',
        projectName: 'Acme',
      }),
    ).toBe(`feature/checkout-funnel${SEP}Plan branch${SEP}Acme${SEP}tripl`)
    expect(
      composeDocumentTitle(resolveTitleFromPath('/p/acme/events/all/e-1/edit'), {
        entity: editPageTitle('Home Screen View'),
        projectName: 'Acme',
      }),
    ).toBe(`Edit${SEP}Home Screen View${SEP}Event${SEP}Acme${SEP}tripl`)
  })

  it('frames a settings section with its group, and names the project it changes', () => {
    expect(
      composeDocumentTitle(resolveTitleFromPath('/settings/project/general', '?project=acme'), {
        projectName: 'Acme',
      }),
    ).toBe(`General${SEP}Project settings${SEP}Acme${SEP}tripl`)
    expect(composeDocumentTitle(resolveTitleFromPath('/settings/organization/general'))).toBe(
      `Details${SEP}Organization settings${SEP}tripl`,
    )
    // An unknown `?project=` is the section's to answer; the tab names no project.
    expect(
      composeDocumentTitle(resolveTitleFromPath('/settings/project/general', '?project=ghost'), {
        projectMissing: true,
      }),
    ).toBe(`General${SEP}Project settings${SEP}tripl`)
  })
})

describe('useDocumentTitle', () => {
  const originalTitle = document.title
  afterEach(() => {
    document.title = originalTitle
  })

  function renderAt(path: string, client: QueryClient, entity: string | null = null) {
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(
        QueryClientProvider,
        { client },
        createElement(MemoryRouter, { initialEntries: [path] }, children),
      )
    return renderHook(() => useDocumentTitle(entity), { wrapper })
  }

  function withProjects(projects: Array<Pick<Project, 'slug' | 'name'>>): QueryClient {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    client.setQueryData(projectsQueryOptions().queryKey, projects as Project[])
    return client
  }

  it('writes the route title, naming the project from the shell’s cache', () => {
    renderAt('/p/acme/anomalies', withProjects([{ slug: 'acme', name: 'Acme Corp' }]))
    expect(document.title).toBe(`Anomalies${SEP}Acme Corp${SEP}tripl`)
  })

  it('names the project from the project endpoint when the list does not show it', () => {
    // A demo the list hides while it seeds still has a name.
    const client = withProjects([])
    client.setQueryData(projectQueryOptions('fresh').queryKey, { slug: 'fresh', name: 'Fresh demo' } as Project)
    renderAt('/p/fresh/overview', client)
    expect(document.title).toBe(`Overview${SEP}Fresh demo${SEP}tripl`)
  })

  it('leads with the entity a detail page has named', () => {
    renderAt('/p/acme/monitors/r-1', withProjects([{ slug: 'acme', name: 'Acme Corp' }]), 'Checkout drop')
    expect(document.title).toBe(`Checkout drop${SEP}Alert rule${SEP}Acme Corp${SEP}tripl`)
  })

  it('says "Project not found" once the shell has been told there is no such project', async () => {
    const client = withProjects([{ slug: 'acme', name: 'Acme Corp' }])
    await client.prefetchQuery({
      queryKey: projectQueryOptions('ghost').queryKey,
      queryFn: () => Promise.reject(new ApiError('Not found', 404)),
    })
    renderAt('/p/ghost/overview', client)
    await waitFor(() => expect(document.title).toBe(`Project not found${SEP}tripl`))
  })
})
