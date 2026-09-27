import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DemoScenarioProvider } from '@/demo/DemoScenarioProvider'
import {
  buildChapterSteps,
  initialScenarioState,
  readScenarioState,
} from '@/demo/scenarioModel'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import type { Project, ScanConfig } from '@/types'
import { ScanConfigDetail } from './ScanConfigDetailView'
import { at } from '@/test/at'

// CodeMirror (pulled in by the configuration tab) needs layout jsdom can't give.
vi.mock('@uiw/react-codemirror', () => ({
  default: ({ value }: { value: string }) => <textarea readOnly value={value} />,
}))

vi.mock('@/hooks/useBranch', () => ({
  useActiveBranchId: () => null,
}))

const SLUG = 'demo'
const STEPS = buildChapterSteps(SLUG, 'live-loop', initialScenarioState())
const RUN_SCAN_INSTRUCTION = STEPS[0].instruction
const WATCH_SCAN_INSTRUCTION = at(STEPS, 1).instruction

/**
 * Radix tabs select on mouse-down (and on focus), not on click, so a bare
 * `fireEvent.click` never reaches them.
 */
function selectTab(tab: HTMLElement) {
  fireEvent.mouseDown(tab, { button: 0, ctrlKey: false })
  fireEvent.click(tab)
}

function mockJsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function demoProject(overrides: Partial<Project> = {}): Project {
  return {
    id: 'p-1',
    name: 'Demo',
    slug: SLUG,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    is_demo: true,
    generation_status: 'ready',
    ...overrides,
  } as Project
}

const scanConfig: ScanConfig = {
  id: 'scan-1',
  data_source_id: 'ds-1',
  project_id: 'p-1',
  event_type_id: null,
  name: 'Main scan',
  base_query: 'SELECT * FROM analytics.events',
  event_type_column: null,
  time_column: 'created_at',
  event_name_format: null,
  json_value_paths: [],
  event_group_rules: [],
  metric_breakdown_columns: [],
  metric_breakdown_values_limit: null,
  distribution_drift_fields: [],
  cardinality_threshold: 100,
  interval: '1h',
  replay_chunk_interval: '1h',
  scan_lookback_hours: null,
  scan_row_limit: null,
  metrics_row_limit: null,
  app_version_column: null,
  app_version_keep_releases: null,
  app_version_prerelease_pattern: null,
  app_version_active_share_min: null,
  platform_column: null,
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
}

const job = (id: string, status: string) => ({
  id,
  scan_config_id: 'scan-1',
  status,
  started_at: '2026-02-01T00:00:00Z',
  completed_at: null,
  result_summary: null,
  error_message: null,
  created_at: '2026-02-01T00:00:00Z',
  updated_at: '2026-02-01T00:00:00Z',
})

/**
 * The run endpoint answers with `job-new`; the feed already carries an unrelated
 * `job-tick` — the job the demo's own runtime tick would have produced — so a
 * coach mark on the feed proves the *user's* run was singled out.
 */
function setupFetch(runCalls: { method: string; url: string }[] = []) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    if (url.includes('/platform-presence')) {
      return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
    }
    if (url.includes('/scans/scan-1/run')) {
      runCalls.push({ method, url })
      return mockJsonResponse(job('job-new', 'pending'))
    }
    // The scenario's own watch polls one job by id.
    if (url.includes('/scans/scan-1/jobs/')) return mockJsonResponse(job('job-new', 'running'))
    // Before the POST nothing is running, or Run now would be off (#247 DA-6);
    // after it, the user's run heads the feed above the tick's own job.
    if (url.includes('/scans/scan-1/jobs')) {
      return mockJsonResponse(
        runCalls.length > 0
          ? [job('job-new', 'running'), job('job-tick', 'completed')]
          : [job('job-tick', 'completed')],
      )
    }
    if (url.endsWith('/projects/demo/scans')) return mockJsonResponse([scanConfig])
    if (url.includes('/data-sources')) return mockJsonResponse([])
    if (url.includes('event-types') || url.includes('eventTypes')) return mockJsonResponse([])
    throw new Error(`Unhandled fetch: ${url}`)
  })
}

function renderDetail(project: Project, auth: AuthContextValue | null = null) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <MemoryRouter initialEntries={[`/p/${SLUG}/scans/scan-1`]}>
          <DemoScenarioProvider project={project} pollIntervalMs={10}>
            <ScanConfigDetail slug={SLUG} scanConfigId="scan-1" />
          </DemoScenarioProvider>
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

describe('ScanConfigDetail — role gating (DATA-6)', () => {
  it('lets an editor run the scan but shows the configuration read-only', async () => {
    setupFetch()
    renderDetail(demoProject({ is_demo: false }), {
      user: {
        id: 'editor-1',
        email: 'editor@example.com',
        name: 'Editor',
        role: 'member',
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
    })

    expect(await screen.findByRole('button', { name: /Run now/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit' })).not.toBeInTheDocument()

    selectTab(screen.getByRole('tab', { name: 'Configuration' }))
    const panel = await screen.findByRole('tabpanel')
    expect(within(panel).getByRole('note')).toHaveTextContent(
      'Only an owner can change, replay or delete a scan.',
    )
    expect(within(panel).queryByRole('button', { name: /Save/ })).not.toBeInTheDocument()
    expect(within(panel).queryByRole('button', { name: /Delete/ })).not.toBeInTheDocument()
    expect(within(panel).queryByRole('button', { name: /Replay/ })).not.toBeInTheDocument()
    // The schema lookup behind SQL autocomplete is editor-scoped on a route this
    // user cannot edit through, and read-only SQL has no use for it anyway.
    const fetched = vi.mocked(globalThis.fetch).mock.calls.map(([input]) => String(input))
    expect(fetched.some(url => url.includes('/schema'))).toBe(false)
  })
})

afterEach(() => {
  vi.restoreAllMocks()
  window.localStorage.clear()
})

describe('ScanConfigDetail — header status wording', () => {
  it('heads a successful scan "Succeeded", the same word the run row uses — never "Healthy"', async () => {
    // The header read its label from STATUS_META, which mapped `ok` → "Healthy",
    // while the run pill two lines below read SCAN_RUN_STATUS and said
    // "Succeeded" — one run, two words. "Healthy" is also the Monitors lexeme,
    // and a scan has no alert rule to be healthy or firing about.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.includes('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.includes('/scans/scan-1/jobs')) {
        return mockJsonResponse([
          { ...job('job-ok', 'completed'), completed_at: '2026-02-01T00:00:10Z' },
        ])
      }
      if (url.endsWith('/projects/demo/scans')) return mockJsonResponse([scanConfig])
      if (url.includes('/data-sources')) return mockJsonResponse([])
      if (url.includes('event-types') || url.includes('eventTypes')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderDetail(demoProject({ is_demo: false }))

    // Scope to the header line so the run table's own pill cannot satisfy this.
    const heading = await screen.findByRole('heading', { name: 'Main scan' })
    await waitFor(() => {
      expect(within(heading.parentElement!).getByText('Succeeded')).toBeInTheDocument()
    })
    // The negative assertion is the point: "Healthy" must not appear on any
    // scan surface (statusLexicon reserves it for monitors).
    expect(screen.queryByText('Healthy')).toBeNull()
  })
})

describe('ScanConfigDetail — a scan that does not exist (#237 SH-33)', () => {
  it('says "Scan not found" with the way back to Scans, not an error', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/projects/demo/scans')) return mockJsonResponse([])
      if (url.includes('/data-sources')) return mockJsonResponse([])
      if (url.includes('event-types') || url.includes('eventTypes')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderDetail(demoProject({ is_demo: false }))

    expect(await screen.findByRole('heading', { name: 'Scan not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to Scans' })).toHaveAttribute('href', '/p/demo/scans')
    expect(screen.queryByRole('alert')).toBeNull()
  })
})

describe('ScanConfigDetail — Run now while a run is in flight (#247 DA-6)', () => {
  it('turns Run now off and says Running… while the latest run is active', async () => {
    const runCalls: { method: string; url: string }[] = []
    setupFetch(runCalls)
    renderDetail(demoProject({ is_demo: false }))

    fireEvent.click(await screen.findByRole('button', { name: /Run now/i }))

    const running = await screen.findByRole('button', { name: /Running…/ })
    expect(running).toBeDisabled()
    expect(runCalls).toHaveLength(1)
  })
})

describe('ScanConfigDetail — coached demo scenario', () => {
  it('binds the scenario to the ScanJob the run POST returned', async () => {
    const runCalls: { method: string; url: string }[] = []
    setupFetch(runCalls)
    renderDetail(demoProject())

    fireEvent.click(await screen.findByRole('button', { name: /Run now/i }))

    await waitFor(() => expect(readScenarioState(SLUG).chapters['live-loop']?.step).toBe('live-loop/watch-scan'))
    expect(at(runCalls, 0).method).toBe('POST')
    // The artifact is the job this POST returned — not any job in the feed.
    expect(readScenarioState(SLUG).chapters['live-loop']?.artifacts).toMatchObject({
      scanConfigId: 'scan-1',
      scanJobId: 'job-new',
    })
  })

  it('coaches Run now, then the one feed row carrying the user\'s own run', async () => {
    setupFetch()
    renderDetail(demoProject())

    // Step 1 points at the action that starts the chain.
    expect(await screen.findByRole('note')).toHaveTextContent(RUN_SCAN_INSTRUCTION)

    fireEvent.click(screen.getByRole('button', { name: /Run now/i }))

    // Step 2 moves onto the run history — one mark, on job-new, never on the
    // tick's own job-tick row.
    await waitFor(() => expect(screen.getByRole('note')).toHaveTextContent(WATCH_SCAN_INSTRUCTION))
    expect(screen.getAllByRole('note')).toHaveLength(1)
  })

  it('leaves a non-demo project untouched: no coach mark, no scenario', async () => {
    setupFetch()
    renderDetail(demoProject({ is_demo: false }))

    fireEvent.click(await screen.findByRole('button', { name: /Run now/i }))

    await waitFor(() => expect(screen.getByText('Main scan')).toBeInTheDocument())
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
    expect(window.localStorage.getItem(`tripl-demo-scenario:${SLUG}`)).toBeNull()
  })
})

describe('ScanConfigDetail — unsaved configuration edits (DATA-12)', () => {
  const owner: AuthContextValue = {
    user: {
      id: 'owner-1',
      email: 'owner@example.com',
      name: 'Owner',
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

  function renderAt(path: string) {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={owner}>
          <MemoryRouter initialEntries={[path]}>
            <DemoScenarioProvider project={demoProject({ is_demo: false })} pollIntervalMs={10}>
              <ScanConfigDetail slug={SLUG} scanConfigId="scan-1" />
            </DemoScenarioProvider>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
  }

  it('jumps between the ends of the tab strip with End and Home (DS-35)', async () => {
    setupFetch()
    renderAt(`/p/${SLUG}/scans/scan-1`)

    const overview = await screen.findByRole('tab', { name: 'Overview' })
    fireEvent.keyDown(overview, { key: 'End' })

    // Radix moves focus on the next tick. Activation is manual (a switch can
    // raise the unsaved-changes dialog), so Enter selects the focused tab.
    const configuration = screen.getByRole('tab', { name: 'Configuration' })
    await waitFor(() => expect(configuration).toHaveFocus())
    fireEvent.keyDown(configuration, { key: 'Enter' })
    expect(configuration).toHaveAttribute('aria-selected', 'true')

    fireEvent.keyDown(configuration, { key: 'Home' })
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Overview' })).toHaveFocus())
    fireEvent.keyDown(screen.getByRole('tab', { name: 'Overview' }), { key: 'Enter' })
    await waitFor(() => expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true'))
  })

  it('opens on the tab the URL names, so a reload keeps the reader on Configuration', async () => {
    setupFetch()
    renderAt(`/p/${SLUG}/scans/scan-1?tab=configuration`)

    expect(await screen.findByRole('tab', { name: 'Configuration' })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByLabelText('Name')).toHaveValue('Main scan')
  })

  it('asks before a tab switch unmounts edited configuration, and keeps it on Cancel', async () => {
    setupFetch()
    renderAt(`/p/${SLUG}/scans/scan-1`)

    selectTab(await screen.findByRole('tab', { name: 'Configuration' }))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Renamed scan' } })

    selectTab(screen.getByRole('tab', { name: 'Overview', hidden: true }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    fireEvent.click(within(confirm).getByRole('button', { name: 'Keep editing' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.getByRole('tab', { name: 'Configuration' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByLabelText('Name')).toHaveValue('Renamed scan')

    selectTab(screen.getByRole('tab', { name: 'Overview' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true'),
    )
  })

  it('keeps an edit typed while a save was in flight unsaved', async () => {
    const saveable = { ...scanConfig, event_type_column: 'event_name' }
    let answerSave: (response: Response) => void = () => {}
    let saveSent = false
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.endsWith('/projects/demo/scans/scan-1') && method === 'PATCH') {
        saveSent = true
        return new Promise<Response>(resolve => {
          answerSave = resolve
        })
      }
      if (url.endsWith('/projects/demo/scans')) return mockJsonResponse([saveable])
      if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse([])
      if (url.includes('/data-sources')) return mockJsonResponse([])
      if (url.includes('event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${method} ${url}`)
    })
    renderAt(`/p/${SLUG}/scans/scan-1?tab=configuration`)

    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Sent name' } })
    fireEvent.click(at(screen.getAllByRole('button', { name: 'Save changes' }), 0))
    await waitFor(() => expect(saveSent).toBe(true))
    // Typed after the request left, before it answered.
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Typed during save' } })
    answerSave(mockJsonResponse({ ...saveable, name: 'Sent name' }))
    // The save answered, but the form no longer holds what it sent: "Saved."
    // would be a claim about text that is not on screen (DATA-14).
    expect(await screen.findByText('Unsaved changes.')).toBeInTheDocument()
    expect(screen.queryByText('Saved.')).not.toBeInTheDocument()

    selectTab(screen.getByRole('tab', { name: 'Overview' }))
    expect(
      await screen.findByRole('alertdialog', { name: 'Leave without saving?' }),
    ).toBeInTheDocument()
  })

  it('has one Save for the whole form, and says Saved. only until the next edit (DATA-14)', async () => {
    const saveable = { ...scanConfig, event_type_column: 'event_name' }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.endsWith('/projects/demo/scans/scan-1') && method === 'PATCH') {
        return mockJsonResponse({ ...saveable, name: 'Renamed' })
      }
      if (url.endsWith('/projects/demo/scans')) return mockJsonResponse([saveable])
      if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse([])
      if (url.includes('/data-sources')) return mockJsonResponse([])
      if (url.includes('event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${method} ${url}`)
    })
    renderAt(`/p/${SLUG}/scans/scan-1?tab=configuration`)

    const name = await screen.findByLabelText('Name')
    // Nothing to save yet, so no Save bar: a disabled Save in an empty grey
    // strip read as a dead footer (#247 DA-26).
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument()

    fireEvent.change(name, { target: { value: 'Renamed' } })
    expect(screen.getAllByRole('button', { name: 'Save changes' })).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    expect(await screen.findByText('Saved.')).toBeInTheDocument()

    fireEvent.change(name, { target: { value: 'Renamed again' } })
    expect(screen.queryByText('Saved.')).not.toBeInTheDocument()
    expect(screen.getByText('Unsaved changes.')).toBeInTheDocument()
  })

  it('discards edits back to the saved configuration (#247 DA-26)', async () => {
    const saveable = { ...scanConfig, event_type_column: 'event_name' }
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/projects/demo/scans')) return mockJsonResponse([saveable])
      if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse([])
      if (url.includes('/data-sources')) return mockJsonResponse([])
      if (url.includes('event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderAt(`/p/${SLUG}/scans/scan-1?tab=configuration`)

    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'Half-typed' } })
    expect(screen.getByText('Unsaved changes.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }))

    expect(await screen.findByLabelText('Name')).toHaveValue(saveable.name)
    expect(screen.queryByText('Unsaved changes.')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument()
  })

  it('opens Replay from the header as a dialog, not from the Danger zone (#247 DA-8)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/projects/demo/scans')) return mockJsonResponse([scanConfig])
      if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse([])
      if (url.includes('/data-sources')) return mockJsonResponse([])
      if (url.includes('event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderAt(`/p/${SLUG}/scans/scan-1?tab=configuration`)

    const panel = await screen.findByRole('tabpanel')
    expect(within(panel).queryByRole('button', { name: /Replay/ })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Replay a period/ }))
    expect(await screen.findByRole('dialog', { name: 'Replay a past period' })).toBeInTheDocument()
  })

  it('takes a deleted scan out of the cached list before leaving (DATA-4)', async () => {
    let deleted = false
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (url.endsWith('/projects/demo/scans/scan-1') && method === 'DELETE') {
        deleted = true
        return new Response(null, { status: 204 })
      }
      if (url.endsWith('/projects/demo/scans')) return mockJsonResponse(deleted ? [] : [scanConfig])
      if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse([])
      if (url.includes('/data-sources')) return mockJsonResponse([])
      if (url.includes('event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${method} ${url}`)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 60_000 } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={owner}>
          <MemoryRouter initialEntries={[`/p/${SLUG}/scans/scan-1?tab=configuration`]}>
            <DemoScenarioProvider project={demoProject({ is_demo: false })} pollIntervalMs={10}>
              <ScanConfigDetail slug={SLUG} scanConfigId="scan-1" />
            </DemoScenarioProvider>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    const confirm = await screen.findByRole('alertdialog')
    fireEvent.click(within(confirm).getByRole('button', { name: 'Delete' }))

    await waitFor(() => expect(deleted).toBe(true))
    await waitFor(() =>
      expect(queryClient.getQueryData<ScanConfig[]>(['scans', SLUG])?.map(sc => sc.id)).toEqual([]),
    )
    expect(queryClient.getQueryData(['scanJobs', SLUG, 'scan-1'])).toBeUndefined()
  })

  it('puts the scheduler\'s next metrics run in the header (i9mt.16 DA-5)', async () => {
    const nextRunAt = new Date(Date.now() + 3 * 24 * 3_600_000 + 60_000).toISOString()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async input => {
      const url = String(input)
      if (url.endsWith('/projects/demo/scans/scan-1')) {
        return mockJsonResponse({ ...scanConfig, last_metrics_run_at: null, next_metrics_run_at: nextRunAt })
      }
      if (url.endsWith('/projects/demo/scans')) return mockJsonResponse([scanConfig])
      if (url.includes('/platform-presence')) {
        return mockJsonResponse({ scan_config_id: 'scan-1', platform_column: null, platforms: [], items: [] })
      }
      if (url.includes('/scans/scan-1/jobs')) return mockJsonResponse([])
      if (url.includes('/scans/activity')) return mockJsonResponse({ window_from: '', window_to: '', items: [] })
      if (url.includes('/data-sources')) return mockJsonResponse([])
      if (url.includes('event-types')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderAt(`/p/${SLUG}/scans/scan-1`)

    expect(await screen.findByText('Next run in 3d')).toBeInTheDocument()
  })

  it('switches tabs at once while nothing is edited', async () => {
    setupFetch()
    renderAt(`/p/${SLUG}/scans/scan-1?tab=configuration`)

    await screen.findByLabelText('Name')
    selectTab(screen.getByRole('tab', { name: 'Overview' }))
    expect(screen.getByRole('tab', { name: 'Overview' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })
})
