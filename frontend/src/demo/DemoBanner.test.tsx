import type { ReactNode } from 'react'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { BranchProvider } from '@/components/branch-context'
import { projectsApi } from '@/api/projects'
import { ApiError } from '@/api/client'
import { eventTypesKey, projectKey, projectsKey } from '@/lib/queryKeys'
import type { Project, Role } from '@/types'
import { DemoBanner } from './DemoBanner'
import { DemoScenarioProvider } from './DemoScenarioProvider'
import { readScenarioState, writeScenarioState } from './scenarioModel'
import { liveLoopState } from './scenarioTestState'
import { at } from '@/test/at'

const WELCOME_DISMISS_KEY = 'tripl-demo-welcome-dismissed:demo-1'

function makeProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p-1',
    name: 'Demo workspace',
    slug: 'demo-1',
    description: '',
    app_version_keep_releases: 5,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    is_demo: true,
    generation_status: 'ready',
    demo_recipe_version: 'v3',
    demo_seeded_at: '2026-07-10T00:00:00Z',
    demo_last_tick_at: null,
    created_by_user_id: 'creator-1',
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
    ...overrides,
  }
}

function authValue({ id, role }: { id: string; role: Role }): AuthContextValue {
  return {
    user: {
      id,
      email: `${id}@example.com`,
      name: id,
      role,
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

function LocationProbe() {
  const location = useLocation()
  return <span data-testid="path">{location.pathname}</span>
}

function renderBanner(options: {
  project?: Project
  auth?: AuthContextValue
  initialPath?: string
  queryClient?: QueryClient
  resetTimeoutMs?: number
  reseedPollMs?: number
  reseedWatchMs?: number
  scenario?: ReactNode
} = {}) {
  const project = options.project ?? makeProject()
  const auth = options.auth ?? authValue({ id: 'creator-1', role: 'member' })
  const initialPath = options.initialPath ?? `/p/${project.slug}/overview`
  const queryClient = options.queryClient ?? new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  // Wrapped in a real BranchProvider and a real DemoScenarioProvider, as the
  // banner is in the app Layout inside both — the reset path has to be able to
  // clear the persisted branch selection AND the chapter progress.
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <MemoryRouter initialEntries={[initialPath]}>
          <DemoScenarioProvider project={project} pollIntervalMs={10_000}>
            <BranchProvider slug={project.slug}>
              <DemoBanner
                project={project}
                resetTimeoutMs={options.resetTimeoutMs}
                reseedPollMs={options.reseedPollMs}
                reseedWatchMs={options.reseedWatchMs}
                scenario={options.scenario}
              />
              <LocationProbe />
            </BranchProvider>
          </DemoScenarioProvider>
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

/** The one stop Reset and Delete sit behind (#238). */
function manageTrigger(): HTMLElement {
  return screen.getByRole('button', { name: /^manage demo/i, hidden: true })
}

async function openManageMenu(): Promise<HTMLElement> {
  fireEvent.keyDown(manageTrigger(), { key: 'Enter' })
  return screen.findByRole('menu', { hidden: true })
}

/** Opens "Manage demo" and picks Reset… or Delete…. */
async function chooseAction(name: RegExp): Promise<void> {
  const menu = await openManageMenu()
  fireEvent.click(within(menu).getByRole('menuitem', { name, hidden: true }))
}

describe('DemoBanner', () => {
  it('labels the workspace as local synthetic data with its recipe version', () => {
    renderBanner()

    // Synthetic/local is never conflated with real/external data.
    expect(screen.getByText('Local synthetic data')).toBeInTheDocument()
    expect(screen.getByText('Demo workspace')).toBeInTheDocument()
    expect(screen.getByText('recipe v3')).toBeInTheDocument()
  })

  // One row, not a banner with the scenario strip stacked under it.
  it('carries the scenario inside its own row, between who it is and what it offers', () => {
    renderBanner({
      scenario: (
        <section aria-label="Demo scenario" data-demo-scenario="">
          <button type="button">Dismiss</button>
        </section>
      ),
    })

    const scenario = screen.getByRole('region', { name: 'Demo scenario' })
    const row = scenario.parentElement
    if (!row) throw new Error('the scenario has no row')
    expect(row).toContainElement(screen.getByText('Local synthetic data'))
    expect(row).toContainElement(manageTrigger())
    // In reading (and so tab) order: identity, scenario, then the actions.
    const order = [
      screen.getByText('Local synthetic data'),
      screen.getByRole('button', { name: 'Dismiss' }),
      screen.getByRole('button', { name: /what’s simulated/i }),
    ]
    order.slice(1).forEach((node, index) => {
      expect(order[index]?.compareDocumentPosition(node)).toBe(Node.DOCUMENT_POSITION_FOLLOWING)
    })
  })

  it('folds into a pill on a phone that opens the whole bar', () => {
    renderBanner()

    const pill = screen.getByRole('button', { name: /demo workspace tools/i })
    expect(pill).toHaveAttribute('aria-expanded', 'false')
    // What it opens holds every control, so none is out of reach behind it.
    const panel = document.getElementById(pill.getAttribute('aria-controls') ?? '')
    if (!panel) throw new Error('the pill controls nothing')
    expect(panel).toContainElement(manageTrigger())
    expect(panel).toContainElement(screen.getByRole('button', { name: /tour & chapters/i }))

    fireEvent.click(pill)
    expect(pill).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(pill)
    expect(pill).toHaveAttribute('aria-expanded', 'false')
  })

  it('keeps Reset and Delete behind one menu, out of the tab order (#238)', async () => {
    renderBanner()

    // A keyboard user tabbed through two destructive buttons on every demo
    // surface before reaching the page; now it is one stop that opens a menu.
    expect(screen.queryByRole('button', { name: /^reset/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^delete/i })).not.toBeInTheDocument()
    expect(manageTrigger()).toHaveAttribute('aria-haspopup', 'menu')

    const menu = await openManageMenu()
    const items = within(menu).getAllByRole('menuitem')
    expect(items.map((item) => item.textContent)).toEqual(['Reset…', 'Delete…'])
    for (const item of items) expect(item).not.toHaveAttribute('aria-disabled')
  })

  it('reports freshness from the runtime tick, not the seed time', () => {
    // demo_seeded_at is floored to the hour, so a demo seeded at 10:59 carries
    // 10:00 and would read "refreshed 59m ago" the moment it appeared — and no
    // runtime tick ever moved it. Until the first tick there is nothing to claim.
    renderBanner({
      project: makeProject({ demo_seeded_at: '2026-07-10T00:00:00Z', demo_last_tick_at: null }),
    })
    expect(screen.getByText('freshly seeded')).toBeInTheDocument()
    expect(screen.queryByText(/updated/i)).not.toBeInTheDocument()
  })

  it('shows when the runtime tick last advanced the demo', () => {
    renderBanner({
      project: makeProject({ demo_last_tick_at: '2026-07-10T00:00:00Z' }),
    })
    expect(screen.getByText(/^updated /i)).toBeInTheDocument()
    expect(screen.queryByText('freshly seeded')).not.toBeInTheDocument()
  })

  it('resets only after confirmation, via the demo-scoped endpoint', async () => {
    const resetSpy = vi.spyOn(projectsApi, 'resetDemo').mockResolvedValue(makeProject())

    renderBanner()
    await chooseAction(/^reset…$/i)

    // A confirmation dialog gates the destructive action.
    const confirm = await screen.findByRole('button', { name: /reset demo/i })
    expect(resetSpy).not.toHaveBeenCalled()
    fireEvent.click(confirm)

    await waitFor(() => expect(resetSpy).toHaveBeenCalledWith('demo-1', expect.any(AbortSignal)))
  })

  it('after reset, leaves the now-dead detail URL and drops the stored branch', async () => {
    // A re-seed recreates every entity with a NEW id, so the metric this page is
    // showing no longer exists, and the persisted branch id would make every
    // branch-aware query fail with "Branch not found".
    window.localStorage.setItem('tripl-branch:demo-1', 'branch-abc')
    const resetSpy = vi.spyOn(projectsApi, 'resetDemo').mockResolvedValue(makeProject())

    renderBanner({ initialPath: '/p/demo-1/metrics/metric-99' })
    await chooseAction(/^reset…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /reset demo/i }))

    await waitFor(() => expect(resetSpy).toHaveBeenCalledWith('demo-1', expect.any(AbortSignal)))
    await waitFor(() =>
      expect(screen.getByTestId('path')).toHaveTextContent('/p/demo-1/overview'),
    )
    expect(window.localStorage.getItem('tripl-branch:demo-1')).toBeNull()
  })

  it('deletes after confirmation and returns to the projects list', async () => {
    const deleteSpy = vi.spyOn(projectsApi, 'deleteDemo').mockResolvedValue(undefined as never)

    renderBanner()
    await chooseAction(/^delete…$/i)

    const confirm = await screen.findByRole('button', { name: /delete demo/i })
    fireEvent.click(confirm)

    await waitFor(() => expect(deleteSpy).toHaveBeenCalledWith('demo-1'))
    await waitFor(() => expect(screen.getByTestId('path')).toHaveTextContent('/workspace'))
  })

  it('hides reset/delete from a non-creator, non-owner user', () => {
    renderBanner({ auth: authValue({ id: 'someone-else', role: 'member' }) })

    expect(screen.queryByRole('button', { name: /^manage demo/i })).not.toBeInTheDocument()
  })

  it('hides reset/delete from the creator once they are demoted to viewer', () => {
    // The backend gates both routes on EditorUserDep before it looks at the
    // creator, so the demoted creator's click could only ever answer 403. The
    // demotion is a `viewer` row in this project, which the server reports.
    renderBanner({
      auth: authValue({ id: 'creator-1', role: 'member' }),
      project: makeProject({ my_role: 'viewer', can_mutate: false }),
    })

    expect(screen.queryByRole('button', { name: /^manage demo/i })).not.toBeInTheDocument()
  })

  it('shows reset/delete to a workspace owner even if they did not create the demo', async () => {
    renderBanner({ auth: authValue({ id: 'owner-9', role: 'owner' }) })

    const menu = await openManageMenu()
    expect(within(menu).getByRole('menuitem', { name: /^reset…$/i })).toBeInTheDocument()
    expect(within(menu).getByRole('menuitem', { name: /^delete…$/i })).toBeInTheDocument()
  })
})

describe('DemoBanner — the way back into the guided onboarding', () => {
  it('opens the tour without restoring a panel the user put away', async () => {
    // Dismissing the welcome panel used to remove the tour and the chapter
    // picker for good; then this button restored the panel on every click,
    // bundling two intents. It opens the tour, which offers the panel back.
    window.localStorage.setItem(WELCOME_DISMISS_KEY, '1')
    renderBanner()

    fireEvent.click(screen.getByRole('button', { name: /Tour & chapters/i }))

    expect(await screen.findByRole('dialog', { name: /Product tour/i })).toBeInTheDocument()
    expect(window.localStorage.getItem(WELCOME_DISMISS_KEY)).toBe('1')

    fireEvent.click(screen.getByRole('button', { name: /show the welcome panel/i }))
    expect(window.localStorage.getItem(WELCOME_DISMISS_KEY)).toBeNull()
  })

  it('offers the way back to a viewer, who has no Reset to fall back on', () => {
    renderBanner({
      auth: authValue({ id: 'someone-else', role: 'member' }),
      project: makeProject({ my_role: 'viewer', can_mutate: false }),
    })

    expect(screen.getByRole('button', { name: /Tour & chapters/i })).toBeInTheDocument()
  })

  it('a re-seed clears the dismissal and the chapter progress with the data', async () => {
    // A reset that left every chapter "completed" and the panel dismissed gave
    // back a fresh dataset with no guidance and no way into any.
    window.localStorage.setItem(WELCOME_DISMISS_KEY, '1')
    writeScenarioState('demo-1', liveLoopState('live-loop/see-chart', { status: 'completed' }))
    const resetSpy = vi.spyOn(projectsApi, 'resetDemo').mockResolvedValue(makeProject())

    renderBanner()
    await chooseAction(/^reset…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /reset demo/i }))

    await waitFor(() => expect(resetSpy).toHaveBeenCalledWith('demo-1', expect.any(AbortSignal)))
    await waitFor(() => expect(window.localStorage.getItem(WELCOME_DISMISS_KEY)).toBeNull())
    expect(readScenarioState('demo-1').chapters['live-loop']).toEqual({
      status: 'active',
      step: 'live-loop/run-scan',
    })
  })
})

describe('DemoBanner — reset and delete failures', () => {
  it('reports a failed reset, lets the user try again, and does not leave the page', async () => {
    const resetSpy = vi
      .spyOn(projectsApi, 'resetDemo')
      .mockRejectedValue(new ApiError('Demo reset failed', 500))

    renderBanner({ initialPath: '/p/demo-1/metrics/metric-99' })
    await chooseAction(/^reset…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /reset demo/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Demo reset failed')
    expect(resetSpy).toHaveBeenCalledTimes(1)
    // The progress dialog is gone and the controls are usable again.
    expect(screen.queryByRole('dialog', { name: /re-seeding demo workspace/i })).not.toBeInTheDocument()
    const menu = await openManageMenu()
    expect(within(menu).getByRole('menuitem', { name: /^reset…$/i })).not.toHaveAttribute('aria-disabled')
    expect(within(menu).getByRole('menuitem', { name: /^delete…$/i })).not.toHaveAttribute('aria-disabled')
    // Nothing was replaced, so the page the user was on still exists.
    expect(screen.getByTestId('path')).toHaveTextContent('/p/demo-1/metrics/metric-99')
  })

  it('narrates a reset that is still running and locks both actions until it answers', async () => {
    vi.spyOn(projectsApi, 'resetDemo').mockReturnValue(new Promise<never>(() => {}))

    renderBanner()
    await chooseAction(/^reset…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /reset demo/i }))

    expect(
      await screen.findByRole('dialog', { name: /re-seeding demo workspace/i }),
    ).toBeInTheDocument()
    expect(manageTrigger()).toHaveAccessibleName('Manage demo (Resetting…)')
  })

  it('keeps a running reset locked when the user opens the limits or the tour', async () => {
    vi.spyOn(projectsApi, 'resetDemo').mockReturnValue(new Promise<never>(() => {}))

    renderBanner()
    await chooseAction(/^reset…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /reset demo/i }))
    await screen.findByRole('dialog', { name: /re-seeding demo workspace/i })

    // Neither is gated on the reset, and clearing an error must not detach it.
    fireEvent.click(screen.getByRole('button', { name: /what’s simulated/i, hidden: true }))
    fireEvent.click(screen.getByRole('button', { name: /tour & chapters/i, hidden: true }))

    expect(manageTrigger()).toHaveAccessibleName('Manage demo (Resetting…)')
  })

  it('clears the error on the next thing the user does', async () => {
    vi.spyOn(projectsApi, 'resetDemo').mockRejectedValue(new ApiError('Demo reset failed', 500))

    renderBanner()
    await chooseAction(/^reset…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /reset demo/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Demo reset failed')

    fireEvent.click(screen.getByRole('button', { name: /what’s simulated/i }))

    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
  })

  it('does not caption a delete with the reset that failed before it', async () => {
    vi.spyOn(projectsApi, 'resetDemo').mockRejectedValue(new ApiError('Demo reset failed', 500))
    vi.spyOn(projectsApi, 'deleteDemo').mockRejectedValue(new ApiError('Demo delete refused', 409))

    renderBanner()
    await chooseAction(/^reset…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /reset demo/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Demo reset failed')

    await chooseAction(/^delete…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /delete demo/i }))

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Demo delete refused'))
    expect(screen.queryByText('Demo reset failed')).not.toBeInTheDocument()
  })
})

describe('DemoBanner — a reset that never answers', () => {
  function stallReset() {
    let signal: AbortSignal | undefined
    vi.spyOn(projectsApi, 'resetDemo').mockImplementation(
      (_slug: string, sig?: AbortSignal) =>
        new Promise<Project>((_resolve, reject) => {
          signal = sig
          sig?.addEventListener('abort', () => {
            reject(new ApiError('Request to the backend timed out.', 408))
          })
        }),
    )
    return () => signal
  }

  it('stops waiting after the timeout and says the server may still be re-seeding', async () => {
    const signal = stallReset()
    // The re-seed has not landed: the project still has its old id.
    vi.spyOn(projectsApi, 'get').mockResolvedValue(makeProject())

    renderBanner({ resetTimeoutMs: 20, reseedPollMs: 60_000 })
    await chooseAction(/^reset…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /reset demo/i }))

    // The request is actually aborted, and the progress modal gives way to an
    // honest one that can be closed.
    const stalled = await screen.findByRole('dialog', { name: /reset is still running/i })
    expect(signal()?.aborted).toBe(true)
    expect(stalled).toHaveTextContent(/may still be re-seeding/i)
    expect(screen.queryByRole('dialog', { name: /re-seeding demo workspace/i })).not.toBeInTheDocument()
    // Told once, by the dialog — not again under the banner.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    // Both the footer Close and the dialog's X close it.
    fireEvent.click(at(within(stalled).getAllByRole('button', { name: /^close$/i }), 0))

    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: /reset is still running/i })).not.toBeInTheDocument(),
    )
    // The server may still be re-seeding, so a second reset (or a delete)
    // would race it.
    expect(manageTrigger()).toHaveAccessibleName('Manage demo (Resetting…)')
    const menu = await openManageMenu()
    expect(within(menu).getByRole('menuitem', { name: /^resetting…$/i })).toHaveAttribute('aria-disabled', 'true')
    expect(within(menu).getByRole('menuitem', { name: /^delete…$/i })).toHaveAttribute('aria-disabled', 'true')
  })

  it('drops everything tied to the old ids at once, without waiting to know', async () => {
    stallReset()
    vi.spyOn(projectsApi, 'get').mockResolvedValue(makeProject())
    window.localStorage.setItem('tripl-branch:demo-1', 'branch-abc')
    window.localStorage.setItem(WELCOME_DISMISS_KEY, '1')
    const progress = liveLoopState('live-loop/see-chart', { status: 'completed' })
    writeScenarioState('demo-1', progress)
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    queryClient.setQueryData(eventTypesKey('demo-1', null), [{ id: 'old-seeded-row' }])

    renderBanner({
      queryClient,
      initialPath: '/p/demo-1/metrics/metric-99',
      resetTimeoutMs: 20,
      reseedPollMs: 60_000,
    })
    await chooseAction(/^reset…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /reset demo/i }))
    await screen.findByRole('dialog', { name: /reset is still running/i })

    // Harmless if the server rolled back, and "Refresh now" or "Close" would
    // otherwise keep a stored branch id and cached rows for deleted entities.
    await waitFor(() => expect(screen.getByTestId('path')).toHaveTextContent('/p/demo-1/overview'))
    expect(window.localStorage.getItem('tripl-branch:demo-1')).toBeNull()
    expect(queryClient.getQueryCache().find({ queryKey: eventTypesKey('demo-1', null) })).toBeUndefined()
    // The guidance is not touched until the re-seed is known to have landed.
    expect(window.localStorage.getItem(WELCOME_DISMISS_KEY)).toBe('1')
    expect(readScenarioState('demo-1').chapters['live-loop']).toEqual(progress.chapters['live-loop'])
  })

  it('finishes the reset when the server shows the re-seeded project', async () => {
    stallReset()
    // A re-seed replaces the project row: a new id under the same slug.
    const getSpy = vi
      .spyOn(projectsApi, 'get')
      .mockResolvedValueOnce(makeProject())
      .mockResolvedValue(makeProject({ id: 'p-2' }))
    window.localStorage.setItem(WELCOME_DISMISS_KEY, '1')
    writeScenarioState('demo-1', liveLoopState('live-loop/see-chart', { status: 'completed' }))

    renderBanner({ resetTimeoutMs: 20, reseedPollMs: 10 })
    await chooseAction(/^reset…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /reset demo/i }))

    await waitFor(() => expect(getSpy).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(window.localStorage.getItem(WELCOME_DISMISS_KEY)).toBeNull())
    expect(readScenarioState('demo-1').chapters['live-loop']).toEqual({
      status: 'active',
      step: 'live-loop/run-scan',
    })
    // Nothing left to say, and nothing left to race.
    await waitFor(() =>
      expect(screen.queryByRole('dialog', { name: /reset is still running/i })).not.toBeInTheDocument(),
    )
    await waitFor(() => expect(manageTrigger()).toHaveAccessibleName('Manage demo'))
    const menu = await openManageMenu()
    expect(within(menu).getByRole('menuitem', { name: /^reset…$/i })).not.toHaveAttribute('aria-disabled')
    expect(within(menu).getByRole('menuitem', { name: /^delete…$/i })).not.toHaveAttribute('aria-disabled')
  })

  it('offers Reset again once the watch runs out without a re-seed', async () => {
    stallReset()
    vi.spyOn(projectsApi, 'get').mockResolvedValue(makeProject())
    window.localStorage.setItem(WELCOME_DISMISS_KEY, '1')

    renderBanner({ resetTimeoutMs: 20, reseedPollMs: 10, reseedWatchMs: 30 })
    await chooseAction(/^reset…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /reset demo/i }))
    const stalled = await screen.findByRole('dialog', { name: /reset is still running/i })
    fireEvent.click(at(within(stalled).getAllByRole('button', { name: /^close$/i }), 0))

    await waitFor(() => expect(manageTrigger()).toHaveAccessibleName('Manage demo'))
    // Taken as rolled back: the progress through the old dataset stands.
    expect(window.localStorage.getItem(WELCOME_DISMISS_KEY)).toBe('1')
  })
})

describe('DemoBanner — deleting leaves nothing behind in storage', () => {
  it('forgets the tour, scenario, welcome and hint state of the deleted demo', async () => {
    vi.spyOn(projectsApi, 'deleteDemo').mockResolvedValue(undefined as never)
    window.localStorage.setItem('tripl-tour:demo-1', '3')
    window.localStorage.setItem(WELCOME_DISMISS_KEY, '1')
    window.sessionStorage.setItem('tripl-demo-hints-muted:demo-1', '1')
    window.localStorage.setItem('tripl-tour:other-demo', '2')
    writeScenarioState('demo-1', liveLoopState('live-loop/see-chart', { status: 'completed' }))

    renderBanner()
    await chooseAction(/^delete…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /delete demo/i }))

    await waitFor(() => expect(screen.getByTestId('path')).toHaveTextContent('/workspace'))
    expect(window.localStorage.getItem('tripl-tour:demo-1')).toBeNull()
    expect(window.localStorage.getItem(WELCOME_DISMISS_KEY)).toBeNull()
    expect(window.localStorage.getItem('tripl-demo-scenario:demo-1')).toBeNull()
    expect(window.sessionStorage.getItem('tripl-demo-hints-muted:demo-1')).toBeNull()
    // Only that demo's.
    expect(window.localStorage.getItem('tripl-tour:other-demo')).toBe('2')
  })
})

describe('DemoBanner — what a reset drops from the cache', () => {
  it('drops the seeded data but keeps the session and the project the shell resolves', async () => {
    vi.spyOn(projectsApi, 'resetDemo').mockResolvedValue(makeProject())
    vi.spyOn(projectsApi, 'list').mockResolvedValue([makeProject()])
    vi.spyOn(projectsApi, 'get').mockResolvedValue(makeProject())
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    const user = authValue({ id: 'creator-1', role: 'member' }).user
    queryClient.setQueryData(['auth', 'me'], user)
    queryClient.setQueryData(projectsKey(), [makeProject()])
    queryClient.setQueryData(projectKey('demo-1'), makeProject())
    queryClient.setQueryData(eventTypesKey('demo-1', null), [{ id: 'old-seeded-row' }])

    renderBanner({ queryClient })
    await chooseAction(/^reset…$/i)
    fireEvent.click(await screen.findByRole('button', { name: /reset demo/i }))

    await waitFor(() =>
      expect(queryClient.getQueryCache().find({ queryKey: eventTypesKey('demo-1', null) })).toBeUndefined(),
    )
    // Removing the session put the signed-in user back to 'loading' and
    // unmounted the whole app behind the route guard.
    expect(queryClient.getQueryData(['auth', 'me'])).toEqual(user)
    expect(queryClient.getQueryCache().find({ queryKey: projectsKey() })).toBeDefined()
    expect(queryClient.getQueryCache().find({ queryKey: projectKey('demo-1') })).toBeDefined()
  })
})
