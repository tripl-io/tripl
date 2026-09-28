import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { projectMembersApi } from '@/api/projectMembers'
import { projectsApi } from '@/api/projects'
import { usersApi } from '@/api/users'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import type { Project } from '@/types'
import SettingsArea from './SettingsArea'
import { at } from '@/test/at'

const LAST_SLUG_STORAGE_KEY = 'tripl-last-project-slug'

function ownerAuthValue(): AuthContextValue {
  return {
    user: {
      id: 'owner-1',
      email: 'owner@example.com',
      name: 'owner',
      role: 'owner',
      is_platform_admin: false,
      orgs: [],
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    status: 'authenticated',
    error: null,
    isLoggingOut: false,
    logout: async () => {},
    refresh: () => {},
  }
}

function project(slug: string, name: string): Project {
  return {
    id: slug,
    name,
    slug,
    description: '',
    app_version_keep_releases: 5,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    summary: {
      event_type_count: 0,
      event_count: 0,
      active_event_count: 0,
      implemented_event_count: 0,
      review_pending_event_count: 0,
      archived_event_count: 0,
      variable_count: 0,
      scan_count: 0,
      alert_destination_count: 0,
      alert_rule_count: 0,
      monitoring_signal_count: 0,
      firing_monitor_count: 0,
      open_incident_count: 0,
      failing_scan_config_count: 0,
      latest_scan_job: null,
      latest_signal: null,
    },
  }
}

// GET /projects returns acme-android first — the project the takeover used to
// bind to when nothing had been chosen (tripl-jfm3.32).
const projects = [project('acme-android', 'Acme Android'), project('acme-ios', 'Acme iOS')]

function renderArea(section: string, search = '', auth: AuthContextValue = ownerAuthValue()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        {/* A DATA router: the layout's unsaved-work guard uses useBlocker,
            which has no context under a plain MemoryRouter. */}
        <RouterProvider
          router={createMemoryRouter([{ path: '*', element: <SettingsArea section={section} /> }], {
            initialEntries: [`/settings/${section}${search}`],
          })}
        />
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  window.localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('SettingsArea project binding', () => {
  it('asks the user to pick a project instead of binding to the first one', async () => {
    vi.spyOn(projectsApi, 'list').mockResolvedValue(projects)

    renderArea('project/general')

    expect(await screen.findByRole('heading', { name: 'Pick a project' })).toBeInTheDocument()
    // The page still says which page it is (#237 ST-36).
    expect(screen.getByRole('heading', { level: 1, name: 'General' })).toBeInTheDocument()
    // Never silently binds to whichever project happened to sort first: the
    // projects are offered as choices, not applied.
    expect(screen.getByRole('button', { name: /Acme Android/ })).toBeInTheDocument()
    // The way back falls back to the workspace, not to /p/acme-android, and
    // says so instead of promising a project (ST-4).
    expect(screen.getByRole('link', { name: 'Back to workspace' })).toHaveAttribute(
      'href',
      '/workspace',
    )
  })

  it('binds the project the user picks from the empty state (tripl-kr4u)', async () => {
    vi.spyOn(projectsApi, 'list').mockResolvedValue(projects)

    // Plan rules is the project-scoped section with no data fetching of its own.
    renderArea('project/plan-rules')

    fireEvent.click(await screen.findByRole('button', { name: /Acme iOS/ }))

    expect(await screen.findByRole('heading', { name: 'Plan rules' })).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByRole('link', { name: /^Back to (?!workspace)/ })).toHaveAttribute(
        'href',
        '/p/acme-ios/overview',
      )
    })
    // Persisted the same way the sidebar persists it, so a reload keeps it.
    expect(window.localStorage.getItem(LAST_SLUG_STORAGE_KEY)).toBe('acme-ios')
  })

  it('opens Access for the bound project (tripl-vefw)', async () => {
    vi.spyOn(projectsApi, 'list').mockResolvedValue(projects)
    vi.spyOn(projectsApi, 'get').mockResolvedValue(project('acme-ios', 'Acme iOS'))
    const list = vi.spyOn(projectMembersApi, 'list').mockResolvedValue([
      {
        user_id: 'u-ada',
        name: 'Ada',
        email: 'ada@example.com',
        role: 'editor',
        added_at: '2026-01-01T00:00:00Z',
      },
    ])
    vi.spyOn(usersApi, 'list').mockResolvedValue([])

    renderArea('project/members', '?project=acme-ios')

    expect(await screen.findByRole('heading', { level: 1, name: 'Access' })).toBeInTheDocument()
    expect(await screen.findByText('ada@example.com')).toBeInTheDocument()
    expect(list).toHaveBeenCalledWith('acme-ios', expect.anything())
  })

  it('binds to the last project the user actually visited', async () => {
    vi.spyOn(projectsApi, 'list').mockResolvedValue(projects)
    window.localStorage.setItem(LAST_SLUG_STORAGE_KEY, 'acme-ios')

    renderArea('project/general')

    await waitFor(() => {
      expect(screen.getByRole('link', { name: /^Back to (?!workspace)/ })).toHaveAttribute(
        'href',
        '/p/acme-ios/overview',
      )
    })
    // The rail names the bound project, and it is the one the user last opened.
    expect(screen.getByText('Acme iOS')).toBeInTheDocument()
    expect(screen.queryByText('Acme Android')).not.toBeInTheDocument()
  })

  it('binds the project named in the address over another tab\'s last visit (SHELL-20)', async () => {
    vi.spyOn(projectsApi, 'list').mockResolvedValue(projects)
    // Another tab has since visited acme-ios; this one came from acme-android.
    window.localStorage.setItem(LAST_SLUG_STORAGE_KEY, 'acme-ios')

    renderArea('project/plan-rules', '?project=acme-android')

    await waitFor(() => {
      expect(screen.getByRole('link', { name: /^Back to (?!workspace)/ })).toHaveAttribute(
        'href',
        '/p/acme-android/overview',
      )
    })
    // Moving between project sections keeps the binding in the address.
    expect(screen.getByRole('link', { name: 'General' })).toHaveAttribute(
      'href',
      '/settings/project/general?project=acme-android',
    )
  })

  it('ignores a stale last-visited slug that is no longer a project', async () => {
    vi.spyOn(projectsApi, 'list').mockResolvedValue(projects)
    window.localStorage.setItem(LAST_SLUG_STORAGE_KEY, 'deleted-project')

    renderArea('project/general')

    expect(await screen.findByRole('heading', { name: 'Pick a project' })).toBeInTheDocument()
  })

  it('does not claim the workspace is empty while the project list is loading', async () => {
    let resolveList: (value: Project[]) => void = () => {}
    vi.spyOn(projectsApi, 'list').mockReturnValue(
      new Promise<Project[]>((resolve) => {
        resolveList = resolve
      }),
    )
    window.localStorage.setItem(LAST_SLUG_STORAGE_KEY, 'acme-ios')

    renderArea('project/general')

    // Nothing warms the ['projects'] cache on a /settings/* route — these mount
    // outside Layout — so this is the first thing a cold load shows, including
    // to a user with two projects and a valid last-visited slug.
    expect(screen.queryByText(/no project on this workspace yet/i)).toBeNull()
    expect(screen.queryByRole('link', { name: /Create one in the workspace/i })).toBeNull()

    resolveList(projects)

    await waitFor(() => {
      expect(screen.getByRole('link', { name: /^Back to (?!workspace)/ })).toHaveAttribute(
        'href',
        '/p/acme-ios/overview',
      )
    })
  })

  it('names an empty workspace only once the list has actually resolved', async () => {
    vi.spyOn(projectsApi, 'list').mockResolvedValue([])

    renderArea('project/general')

    expect(await screen.findByText(/no project on this workspace yet/i)).toBeInTheDocument()
  })

  it('says the project list failed rather than that there are no projects', async () => {
    vi.spyOn(projectsApi, 'list').mockRejectedValue(new Error('boom'))

    renderArea('project/general')

    expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument()
    expect(screen.queryByText(/no project on this workspace yet/i)).toBeNull()
  })

  it('retries the project list in place instead of asking for a reload (WS-32)', async () => {
    const list = vi.spyOn(projectsApi, 'list').mockRejectedValue(new Error('boom'))

    renderArea('project/general')

    fireEvent.click(await screen.findByRole('button', { name: 'Try again' }))

    await waitFor(() => expect(list).toHaveBeenCalledTimes(2))
    expect(screen.queryByText(/Reload the page/i)).toBeNull()
  })

  it('reports a failed project list once on a section that is not project-scoped (fj5g.6)', async () => {
    // The list is silent app-wide because Layout owns its error card, and these
    // routes mount outside Layout: without this the failure went unreported.
    vi.spyOn(projectsApi, 'list').mockRejectedValue(new Error('boom'))

    renderArea('api-keys')

    await screen.findByRole('heading', { name: 'Projects could not be loaded' })
    const cards = screen
      .getAllByRole('alert')
      .filter((node) => within(node).queryByRole('heading', { name: 'Projects could not be loaded' }))
    expect(cards).toHaveLength(1)
    expect(within(at(cards, 0)).getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it('still renders workspace sections with no project bound', async () => {
    vi.spyOn(projectsApi, 'list').mockResolvedValue(projects)

    renderArea('api-keys')

    expect(await screen.findByText('All keys')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Pick a project' })).not.toBeInTheDocument()
  })
})

describe('SettingsArea follows a slug rename (WS-8)', () => {
  function mockRenamableProjects() {
    let current = projects
    vi.spyOn(projectsApi, 'list').mockImplementation(async () => current)
    vi.spyOn(projectsApi, 'get').mockImplementation(async (slug: string) => {
      const found = current.find((p) => p.slug === slug)
      if (!found) throw new Error(`404 ${slug}`)
      return found
    })
    const update = vi
      .spyOn(projectsApi, 'update')
      .mockImplementation(async (slug: string, data: { slug?: string }) => {
        const renamed = { ...project(data.slug ?? slug, 'Acme iOS'), id: 'acme-ios' }
        current = current.map((p) => (p.slug === slug ? renamed : p))
        return renamed
      })
    return { update }
  }

  async function rename(to: string) {
    const slugInput = await screen.findByLabelText('Slug')
    fireEvent.change(slugInput, { target: { value: to } })
    fireEvent.click(at(screen.getAllByRole('button', { name: /Save/ }), 0))
  }

  it('keeps a project picked from the empty state bound after renaming it', async () => {
    const { update } = mockRenamableProjects()

    renderArea('project/general')
    fireEvent.click(await screen.findByRole('button', { name: /Acme iOS/ }))
    await waitFor(() => expect(screen.getByLabelText('Slug')).toHaveValue('acme-ios'))

    await rename('acme-ios-2')

    await waitFor(() => expect(update).toHaveBeenCalledTimes(1))
    await waitFor(() => {
      expect(screen.getByRole('link', { name: /^Back to (?!workspace)/ })).toHaveAttribute(
        'href',
        '/p/acme-ios-2/overview',
      )
    })
    expect(screen.queryByText(/Could not load this project|Project not found/)).not.toBeInTheDocument()
    expect(screen.getByLabelText('Slug')).toHaveValue('acme-ios-2')

    // A second save goes to the new address, not the dead one.
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Acme iOS app' } })
    fireEvent.click(at(screen.getAllByRole('button', { name: /Save/ }), 0))
    await waitFor(() => expect(update).toHaveBeenCalledTimes(2))
    expect(update.mock.calls[1]?.[0]).toBe('acme-ios-2')
  })

  it('rewrites ?project= in the address after a rename', async () => {
    mockRenamableProjects()

    renderArea('project/general', '?project=acme-ios')
    await waitFor(() => expect(screen.getByLabelText('Slug')).toHaveValue('acme-ios'))

    await rename('acme-ios-2')

    await waitFor(() => {
      expect(screen.getByRole('link', { name: 'Plan rules' })).toHaveAttribute(
        'href',
        '/settings/project/plan-rules?project=acme-ios-2',
      )
    })
    expect(screen.queryByText(/Could not load this project|Project not found/)).not.toBeInTheDocument()
  })
})

describe('SettingsArea owner-only sections (#237 ST-17 / ST-36)', () => {
  it('titles the page and explains the owner gate with a way out', async () => {
    vi.spyOn(projectsApi, 'list').mockResolvedValue(projects)
    const owner = ownerAuthValue()
    const member: AuthContextValue = { ...owner, user: owner.user && { ...owner.user, role: 'member' } }

    renderArea('organization/email', '', member)

    expect(await screen.findByRole('heading', { level: 1, name: 'Email' })).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/Owner role is required/)
    expect(screen.getByRole('link', { name: 'Go to Profile' })).toHaveAttribute(
      'href',
      '/settings/profile',
    )
  })

  it('keeps the Platform console from an organization owner who is not a platform admin (F20 PR9)', async () => {
    vi.spyOn(projectsApi, 'list').mockResolvedValue(projects)

    renderArea('instance/email')

    expect(await screen.findByRole('heading', { level: 1, name: 'Mail relay' })).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/Platform admin is required/)
    expect(screen.getByRole('link', { name: 'Go to Profile' })).toHaveAttribute(
      'href',
      '/settings/profile',
    )
  })
})
