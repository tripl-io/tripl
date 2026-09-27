import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import ProjectsPage from './ProjectsPage'
import { SLUG_ERROR } from '@/lib/slug'

/**
 * Creating and deleting projects from the workspace page (#207): the typed
 * delete confirmation, inline delete failures, the create dialog's reset and
 * slug handling, and the a11y of the stat and project cards.
 */

const OWNER: AuthContextValue = {
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

const BETA = {
  id: 'proj-2',
  name: 'Beta',
  slug: 'beta',
  description: 'Near-complete plan.',
  created_at: '2026-05-01T09:00:00Z',
  updated_at: '2026-05-10T09:00:00Z',
  summary: {
    event_type_count: 4,
    event_count: 400,
    active_event_count: 323,
    implemented_event_count: 320,
    review_pending_event_count: 1,
    archived_event_count: 0,
    variable_count: 3,
    scan_count: 0,
    alert_destination_count: 1,
    alert_rule_count: 0,
    monitoring_signal_count: 0,
    failing_scan_config_count: 0,
    latest_scan_job: null,
    latest_signal: null,
  },
}

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function urlOf(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
}

type Call = { method: string; url: string; body?: string }

function mockApi(options: { deleteStatus?: number } = {}) {
  const calls: Call[] = []
  vi.spyOn(globalThis, 'fetch').mockImplementation(
    (input: RequestInfo | URL, init?: RequestInit) => {
      const url = urlOf(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      calls.push({ method, url, body: init?.body ? String(init.body) : undefined })
      if (method === 'DELETE' && url.endsWith('/api/v1/projects/beta')) {
        const status = options.deleteStatus ?? 204
        return Promise.resolve(
          status === 204
            ? new Response(null, { status: 204 })
            : jsonResponse({ detail: 'Deletion refused' }, status),
        )
      }
      if (method === 'POST' && url.endsWith('/api/v1/projects')) {
        return Promise.resolve(jsonResponse({ ...BETA, id: 'new', slug: 'new-project' }))
      }
      if (url.endsWith('/api/v1/projects')) return Promise.resolve(jsonResponse([BETA]))
      if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([]))
      return Promise.reject(new Error(`Unexpected request: ${method} ${url}`))
    },
  )
  return calls
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={OWNER}>
        <MemoryRouter>
          <ProjectsPage />
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

async function openDeleteDialog() {
  const trigger = await screen.findByRole('button', { name: /project actions for beta/i })
  fireEvent.keyDown(trigger, { key: 'Enter' })
  fireEvent.click(await screen.findByRole('menuitem', { name: /delete project/i }))
  return screen.findByRole('alertdialog')
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ProjectsPage — deleting a project (WS-9, WS-10)', () => {
  it('arms Delete only once the slug is typed, and says what goes', async () => {
    const calls = mockApi()
    renderPage()

    const dialog = await openDeleteDialog()
    expect(dialog).toHaveTextContent(/scans, metrics, monitors, alert rules/)
    expect(dialog).toHaveTextContent(/cannot be undone/i)

    const confirm = within(dialog).getByRole('button', { name: /delete project/i })
    expect(confirm).toBeDisabled()

    const input = within(dialog).getByLabelText(/type beta to confirm/i)
    fireEvent.change(input, { target: { value: 'bet' } })
    expect(confirm).toBeDisabled()
    fireEvent.change(input, { target: { value: 'beta' } })
    expect(confirm).toBeEnabled()

    fireEvent.click(confirm)
    await waitFor(() => {
      expect(calls.filter((call) => call.method === 'DELETE')).toHaveLength(1)
    })
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
  })

  it('shows a failed delete inside the dialog instead of failing silently', async () => {
    mockApi({ deleteStatus: 500 })
    renderPage()

    const dialog = await openDeleteDialog()
    fireEvent.change(within(dialog).getByLabelText(/type beta to confirm/i), {
      target: { value: 'beta' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /delete project/i }))

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      /Could not delete the project/,
    )
    // Still open, so the user can retry or cancel knowing it did not happen.
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()
    expect(screen.getByText('Beta')).toBeInTheDocument()
  })

  it('shuts every card menu while a delete is still settling', async () => {
    const GAMMA = { ...BETA, id: 'proj-3', name: 'Gamma', slug: 'gamma' }
    let listCalls = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      (input: RequestInfo | URL, init?: RequestInit) => {
        const url = urlOf(input)
        const method = (init?.method ?? 'GET').toUpperCase()
        if (method === 'DELETE') return Promise.resolve(new Response(null, { status: 204 }))
        if (url.endsWith('/api/v1/projects')) {
          listCalls += 1
          // The refetch after the delete never settles, so the mutation stays
          // pending after its dialog has closed.
          return listCalls === 1
            ? Promise.resolve(jsonResponse([BETA, GAMMA]))
            : new Promise<Response>(() => {})
        }
        if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([]))
        return Promise.reject(new Error(`Unexpected request: ${method} ${url}`))
      },
    )
    renderPage()

    const dialog = await openDeleteDialog()
    fireEvent.change(within(dialog).getByLabelText(/type beta to confirm/i), {
      target: { value: 'beta' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /delete project/i }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())

    // Beta is still listed and still says so; Gamma cannot start a second
    // delete that would reset Beta's pending state.
    expect(screen.getByText('Deleting…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /project actions for gamma/i })).toBeDisabled()
  })

  it('forgets what a demo remembered about a deleted project (DEMO-17)', async () => {
    mockApi()
    window.localStorage.setItem('tripl-tour:beta', '2')
    window.localStorage.setItem('tripl-demo-scenario:beta', '{}')
    window.sessionStorage.setItem('tripl-demo-hints-muted:beta', '1')
    renderPage()

    const dialog = await openDeleteDialog()
    fireEvent.change(within(dialog).getByLabelText(/type beta to confirm/i), {
      target: { value: 'beta' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /delete project/i }))

    await waitFor(() => expect(window.localStorage.getItem('tripl-tour:beta')).toBeNull())
    expect(window.localStorage.getItem('tripl-demo-scenario:beta')).toBeNull()
    expect(window.sessionStorage.getItem('tripl-demo-hints-muted:beta')).toBeNull()
  })

  it('forgets the typed slug when the dialog is cancelled', async () => {
    mockApi()
    renderPage()

    let dialog = await openDeleteDialog()
    fireEvent.change(within(dialog).getByLabelText(/type beta to confirm/i), {
      target: { value: 'beta' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())

    dialog = await openDeleteDialog()
    expect(within(dialog).getByLabelText(/type beta to confirm/i)).toHaveValue('')
  })
})

describe('ProjectsPage — creating a project (WS-17, WS-18)', () => {
  it('starts a reopened dialog empty, with auto-slug working again', async () => {
    mockApi()
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /new project/i }))
    fireEvent.change(screen.getByLabelText(/project name/i), { target: { value: 'Old' } })
    // The URL field is folded under "Customize URL" (SH-29).
    fireEvent.click(screen.getByRole('button', { name: 'Customize URL' }))
    fireEvent.change(screen.getByLabelText(/project url/i), { target: { value: 'hand-typed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: /new project/i }))
    expect(screen.getByLabelText(/project name/i)).toHaveValue('')
    // Folded again, and empty: the preview shows the placeholder address.
    expect(screen.queryByLabelText(/project url/i)).not.toBeInTheDocument()
    expect(screen.getByText('/p/your-project')).toBeInTheDocument()

    // The slug follows the name again: the "edited by hand" flag was reset.
    fireEvent.change(screen.getByLabelText(/project name/i), { target: { value: 'Café Ölmotor' } })
    expect(screen.getByText('/p/cafe-olmotor')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Customize URL' }))
    expect(screen.getByLabelText(/project url/i)).toHaveValue('cafe-olmotor')
  })

  it('falls back to project-<n> for a name with no Latin letters', async () => {
    mockApi()
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /new project/i }))
    fireEvent.change(screen.getByLabelText(/project name/i), { target: { value: 'Аналитика' } })
    // The first project-<n> no existing project uses.
    expect(screen.getByText('/p/project-1')).toBeInTheDocument()
  })

  it('explains an invalid slug inline instead of the browser tooltip', async () => {
    const calls = mockApi()
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: /new project/i }))
    fireEvent.change(screen.getByLabelText(/project name/i), { target: { value: 'Shop' } })
    fireEvent.click(screen.getByRole('button', { name: 'Customize URL' }))
    const slugInput = screen.getByLabelText(/project url/i)
    fireEvent.change(slugInput, { target: { value: 'Bad Slug' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    expect(await screen.findByText(SLUG_ERROR)).toBeInTheDocument()
    expect(slugInput).toHaveAttribute('aria-invalid', 'true')
    expect(slugInput).toHaveAccessibleDescription(SLUG_ERROR)
    expect(calls.filter((call) => call.method === 'POST')).toHaveLength(0)
  })
})

describe('ProjectsPage — card semantics (WS-42, WS-43)', () => {
  it('opens each attention stat with its term, not its value', async () => {
    mockApi()
    renderPage()

    const term = await screen.findByText('In review', { selector: 'dt' })
    expect(term.tagName).toBe('DT')
    const list = term.closest('dl')
    expect(list?.querySelector('dt, dd')).toBe(term)
  })

  it('exposes implementation progress as a progressbar with a value', async () => {
    mockApi()
    renderPage()

    const bar = await screen.findByRole('progressbar', { name: 'Implementation progress' })
    expect(bar).toHaveAttribute('aria-valuenow', '99')
    expect(bar).toHaveAttribute('aria-valuetext', '320 of 323 active events implemented')
  })
})

describe('ProjectsPage — the demo cap (DEMO-27)', () => {
  it('disables Generate demo project at the cap and says why', async () => {
    const demos = [1, 2, 3].map((n) => ({
      ...BETA,
      id: `demo-${n}`,
      name: `Demo ${n}`,
      slug: `demo-${n}`,
      is_demo: true,
      created_by_user_id: 'owner-1',
    }))
    vi.spyOn(globalThis, 'fetch').mockImplementation((input: RequestInfo | URL) => {
      const url = urlOf(input)
      if (url.endsWith('/api/v1/projects')) return Promise.resolve(jsonResponse(demos))
      if (url.endsWith('/api/v1/data-sources')) return Promise.resolve(jsonResponse([]))
      return Promise.reject(new Error(`Unexpected request: ${url}`))
    })
    renderPage()

    await screen.findByText('Demo 1')
    const button = screen.getByRole('button', { name: /generate demo project/i })
    expect(button).toBeDisabled()
    expect(button).toHaveAccessibleDescription(/3 of 3 demos/)
  })

  it('leaves it enabled below the cap', async () => {
    mockApi()
    renderPage()

    await screen.findByText('Beta')
    expect(screen.getByRole('button', { name: /generate demo project/i })).toBeEnabled()
  })
})
