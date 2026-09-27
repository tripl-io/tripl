import { useState } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { searchApi } from '@/api/search'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import ProjectGeneralSection from './ProjectGeneralSection'
import { UnsavedChangesProvider, type UnsavedWork } from '@/components/settings/unsaved-changes'
import { at } from '@/test/at'
import type { Role } from '@/types'

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const PROJECT = {
  id: 'project-1',
  name: 'Demo',
  slug: 'demo',
  description: '',
  app_version_keep_releases: 3,
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
    latest_scan_job: null,
    latest_signal: null,
  },
}

function authValue(role: Role): AuthContextValue {
  return {
    user: {
      id: `${role}-1`,
      email: `${role}@example.com`,
      name: role,
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

function ownerAuthValue(): AuthContextValue {
  return authValue('owner')
}

function renderSection(
  auth: AuthContextValue = ownerAuthValue(),
  initialProject?: typeof PROJECT,
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  if (initialProject) {
    queryClient.setQueryDefaults(['project', initialProject.slug], { staleTime: Infinity })
    queryClient.setQueryData(['project', initialProject.slug], initialProject)
  }
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <MemoryRouter initialEntries={['/settings/project/general']}>
          <ProjectGeneralSection slug="demo" />
        </MemoryRouter>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ProjectGeneralSection', () => {
  it('updates the shared app-version retention policy', async () => {
    let patchBody: { app_version_keep_releases?: number } | null = null
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo') && (init?.method ?? 'GET') === 'GET') {
        return jsonResponse(PROJECT)
      }
      if (url.endsWith('/api/v1/projects/demo') && init?.method === 'PATCH') {
        patchBody = JSON.parse(String(init.body)) as { app_version_keep_releases?: number }
        return jsonResponse({ ...PROJECT, ...patchBody })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderSection()

    const input = await screen.findByLabelText('Releases to keep')
    expect(input).toHaveValue(3)
    fireEvent.change(input, { target: { value: '4' } })
    // One Save for the page (ST-3); it sends only the part that changed.
    fireEvent.click(screen.getByRole('button', { name: /Save changes/ }))

    await waitFor(() => {
      expect(patchBody).toEqual({ app_version_keep_releases: 4 })
    })
  })

  it('has one save bar with Discard for both cards (ST-3)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo')) return jsonResponse(PROJECT)
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderSection()

    const nameInput = await screen.findByLabelText('Name')
    await waitFor(() => expect(nameInput).toHaveValue('Demo'))
    // No per-card Save buttons any more.
    expect(screen.getAllByRole('button', { name: /Save/ })).toHaveLength(1)
    const save = screen.getByRole('button', { name: /Save changes/ })
    const discard = screen.getByRole('button', { name: 'Discard' })
    expect(save).toBeDisabled()
    expect(discard).toBeDisabled()

    fireEvent.change(nameInput, { target: { value: 'Demo 2' } })
    fireEvent.change(screen.getByLabelText('Releases to keep'), { target: { value: '5' } })
    expect(save).toBeEnabled()
    fireEvent.click(discard)
    expect(nameInput).toHaveValue('Demo')
    expect(screen.getByLabelText('Releases to keep')).toHaveValue(3)
    expect(save).toBeDisabled()
  })

  it('marks a bad slug and a bad release count as errors, and says the form is edited (ST-2)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo')) return jsonResponse(PROJECT)
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderSection()

    const slugInput = await screen.findByLabelText('Slug')
    await waitFor(() => expect(slugInput).toHaveValue('demo'))
    fireEvent.change(slugInput, { target: { value: 'Bad Slug' } })

    expect(slugInput).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByText('Unsaved changes')).toBeInTheDocument()

    const releases = screen.getByLabelText('Releases to keep')
    fireEvent.change(releases, { target: { value: '0' } })
    expect(releases).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByText(/Enter a whole number from 1 to/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Save changes/ })).toBeDisabled()
  })

  it('rebuilds the search index', async () => {
    const reindex = vi.spyOn(searchApi, 'reindex').mockResolvedValue({
      documents_indexed: 42,
      embeddings_scheduled: false,
    })

    renderSection(ownerAuthValue(), PROJECT)

    fireEvent.click(screen.getByRole('button', { name: /Rebuild index/i }))

    expect(await screen.findByText('Indexed 42 documents.')).toBeInTheDocument()
    expect(reindex).toHaveBeenCalledWith('demo')
  })

  it('prefixes the slug with this instance host, not a stand-in domain', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo')) return jsonResponse(PROJECT)
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderSection()

    // The affix read a hardcoded "example.com/p/" on every install (tripl-gex5),
    // so the one screen that shows a reader their project's address showed
    // somebody else's. Asserted against the host the tree is served from, which
    // is the whole point — no literal can satisfy this on two different hosts.
    expect(await screen.findByText(`${window.location.host}/p/`)).toBeInTheDocument()
  })

  it('hides the unfinished "Coming soon" project-config fields for release', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo')) return jsonResponse(PROJECT)
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderSection()

    // Wait for the project to load (the Save button renders once it resolves).
    expect((await screen.findAllByRole('button', { name: /Save/i })).length).toBeGreaterThan(0)

    // The preview-only fields (accent color + the Defaults pair) are gone.
    expect(screen.queryByText('Accent color')).toBeNull()
    expect(screen.queryByText('Default branch')).toBeNull()
    expect(screen.queryByText('Default environment')).toBeNull()

    // Timezone used to be in that list. It is now a REAL setting, not a
    // preview: alert delivery schedules are wall-clock times read in the
    // project's zone, so it is rendered, enabled, and saved with the rest of
    // the identity card.
    expect(screen.getByLabelText('Timezone')).toBeEnabled()

    // No inert "Coming soon" badges survive in the release view.
    expect(screen.queryByText(/Coming soon/i)).toBeNull()

    // Every remaining <select> in the section is an enabled danger-zone period
    // control — there are no disabled preview selects left.
    screen
      .getAllByRole('combobox')
      .forEach((select) => expect(select).not.toBeDisabled())
  })

  it('cross-links to the in-app tracking plan and alerting (#238 ST-5)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo')) return jsonResponse(PROJECT)
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderSection()

    expect(
      await screen.findByRole('button', { name: 'Tracking plan & alerting' }),
    ).toBeInTheDocument()
  })

  it('exposes a Delete project danger action for owners', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo')) return jsonResponse(PROJECT)
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderSection()

    expect(await screen.findByRole('button', { name: /Delete project/ })).toBeInTheDocument()
  })

  it('shows both reset danger actions to owners', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo')) return jsonResponse(PROJECT)
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderSection()

    expect(await screen.findByRole('button', { name: 'Reset anomalies' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reset drifts' })).toBeInTheDocument()
  })

  it('hides the reset danger actions from non-owners', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo')) return jsonResponse(PROJECT)
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderSection(authValue('member'))

    // Wait for the project to load, then confirm the whole owner-only danger
    // zone is absent rather than a card of buttons the editor can never press.
    // A non-editor reads the details as text, not as disabled inputs (#252 ST-18).
    expect(await screen.findByText('Demo')).toBeInTheDocument()
    expect(screen.queryByLabelText('Name')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reset anomalies' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reset drifts' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Delete project/ })).not.toBeInTheDocument()
    expect(screen.queryByText('Danger zone')).not.toBeInTheDocument()
  })

  it('lets the member who created the project edit it', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo')) {
        return jsonResponse({ ...PROJECT, created_by_user_id: 'member-1' })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderSection(authValue('member'))

    expect(await screen.findByLabelText('Name')).toBeEnabled()
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
  })

  it("renders another member's project read-only and says who can edit it", async () => {
    // `_require_project_manager`: only the creator or an owner may PATCH, so
    // live fields here were a form whose Save could only answer 403.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo')) {
        return jsonResponse({ ...PROJECT, created_by_user_id: 'someone-else' })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderSection(authValue('member'))

    // Values as text, not dashed dead inputs (ST-18).
    expect(await screen.findByText('Demo')).toBeInTheDocument()
    expect(screen.queryByLabelText('Name')).toBeNull()
    expect(screen.queryByLabelText('Releases to keep')).toBeNull()
    expect(screen.queryByRole('button', { name: /Save changes/ })).toBeNull()
    expect(screen.getByRole('note')).toHaveTextContent(/creator or an owner/)
    // Reindex is a plain project mutation, which shared projects allow editors.
    expect(screen.getByRole('button', { name: 'Rebuild index' })).toBeEnabled()
  })

  it('leaves Rebuild index out when the project says this user may not mutate it (ST-18)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo')) {
        return jsonResponse({ ...PROJECT, created_by_user_id: 'someone-else', can_mutate: false })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderSection(authValue('member'))

    await screen.findByText('Demo')
    expect(screen.queryByRole('button', { name: 'Rebuild index' })).toBeNull()
    expect(screen.queryByText('Maintenance')).toBeNull()
  })

  it('tells a viewer once why the form is read-only', async () => {
    // A viewer is an organization member whose row in this project is `viewer`.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo')) {
        return jsonResponse({ ...PROJECT, my_role: 'viewer', can_mutate: false })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderSection(authValue('member'))

    expect(await screen.findByText('Demo')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/viewer role/)
    // No dead controls under a notice that says nothing can change (ST-18).
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Rebuild index' })).toBeNull()
    expect(screen.queryByRole('button', { name: /Save changes/ })).toBeNull()
  })

  it('resets anomalies with the chosen period after confirmation', async () => {
    let resetBody: { before?: string | null; after?: string | null } | null = null
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo') && (init?.method ?? 'GET') === 'GET') {
        return jsonResponse(PROJECT)
      }
      if (
        url.endsWith('/api/v1/projects/demo/danger/reset-anomalies') &&
        init?.method === 'POST'
      ) {
        const body = JSON.parse(String(init?.body))
        // The dry run that feeds the confirm is not the reset (ST-39).
        if (body.dry_run) return jsonResponse({ metric_anomalies: 5, metric_breakdown_anomalies: 2 })
        resetBody = body
        return jsonResponse({ metric_anomalies: 5, metric_breakdown_anomalies: 2 })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })

    renderSection()

    // Pick "Older than 7 days" (not the 30-day default) to prove the period ships.
    fireEvent.change(await screen.findByLabelText('Reset anomalies period'), {
      target: { value: '7d' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Reset anomalies' }))

    // Confirm in the irreversible-action dialog, which counts what goes (ST-39).
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Permanently delete 5 anomalies and 2 breakdown anomalies (older than 7 days)')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Reset anomalies' }))

    expect(
      await screen.findByText('Cleared 5 anomalies and 2 breakdown anomalies.'),
    ).toBeInTheDocument()

    expect(resetBody).not.toBeNull()
    const body = resetBody as unknown as { before: string; after: string | null; dry_run?: boolean }
    expect(body.after).toBeNull()
    expect(body.dry_run).toBeUndefined()
    // The cutoff must be ~7 days ago (the chosen window), not the 30-day default.
    const sevenDaysMs = 7 * 24 * 60 * 60 * 1000
    expect(Math.abs(Date.now() - Date.parse(body.before) - sevenDaysMs)).toBeLessThan(60_000)
  })
})

describe('ProjectGeneralSection — reset preview', () => {
  it('says when the reset could not be counted instead of dropping the count silently', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo') && (init?.method ?? 'GET') === 'GET') {
        return jsonResponse(PROJECT)
      }
      if (url.endsWith('/danger/reset-drifts') && init?.method === 'POST') {
        return new Response(JSON.stringify({ detail: 'Count unavailable' }), {
          status: 503,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderSection()

    fireEvent.click(await screen.findByRole('button', { name: 'Reset drifts' }))

    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Permanently delete schema and distribution drift detections')
    expect(dialog).toHaveTextContent("The count couldn't be read first. This cannot be undone.")
  })
})

describe('ProjectGeneralSection — retire unused variables', () => {
  it('shows a failed preview after an earlier retire, instead of the stale retire result', async () => {
    const counts = {
      scanned: 10, retirable: 4, retired: 0, kept_referenced: 0, kept_observed: 0,
      kept_documented: 0, kept_user_edited: 0, kept_excluded: 0,
    }
    let calls = 0
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo') && (init?.method ?? 'GET') === 'GET') {
        return jsonResponse(PROJECT)
      }
      if (url.endsWith('/danger/retire-unused-variables') && init?.method === 'POST') {
        calls += 1
        const { dry_run: dryRun } = JSON.parse(String(init.body)) as { dry_run: boolean }
        if (calls === 3) {
          return new Response(JSON.stringify({ detail: 'Preview unavailable' }), {
            status: 503,
            headers: { 'Content-Type': 'application/json' },
          })
        }
        return jsonResponse(dryRun ? counts : { ...counts, retirable: 0, retired: 4 })
      }
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderSection()

    fireEvent.click(await screen.findByRole('button', { name: 'Preview' }))
    expect(await screen.findByText('4 of 10 variables can be retired.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Retire$/ }))
    fireEvent.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Retire variables' }),
    )
    expect(await screen.findByText('Retired 4 of 10 variables.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Preview' }))
    expect(await screen.findByText('Preview unavailable')).toBeInTheDocument()
    expect(screen.queryByText('Retired 4 of 10 variables.')).not.toBeInTheDocument()
  })
})

describe('ProjectGeneralSection unsaved-changes guard (WS-13)', () => {
  function renderWithShell(auth: AuthContextValue) {
    const registered: (UnsavedWork | null)[] = []
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={auth}>
          <MemoryRouter initialEntries={['/settings/project/general']}>
            <UnsavedChangesProvider value={{ registerUnsaved: work => registered.push(work) }}>
              <ProjectGeneralSection slug="demo" />
            </UnsavedChangesProvider>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
    return { lastRegistered: () => registered.at(-1) ?? null }
  }

  function mockProject() {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo')) return jsonResponse(PROJECT)
      throw new Error(`Unhandled fetch: ${url}`)
    })
  }

  it('registers the draft with the settings shell while either card is edited', async () => {
    mockProject()
    const { lastRegistered } = renderWithShell(ownerAuthValue())

    const nameInput = await screen.findByLabelText('Name')
    await waitFor(() => expect(nameInput).toHaveValue('Demo'))
    expect(lastRegistered()).toBeNull()

    fireEvent.change(nameInput, { target: { value: 'Demo 2' } })
    await waitFor(() => expect(lastRegistered()).not.toBeNull())
    // No other settings page renders this draft, so every destination loses it.
    expect(lastRegistered()!.keptBy('project/general')).toBe(false)

    fireEvent.change(nameInput, { target: { value: 'Demo' } })
    await waitFor(() => expect(lastRegistered()).toBeNull())

    fireEvent.change(screen.getByLabelText('Releases to keep'), { target: { value: '5' } })
    await waitFor(() => expect(lastRegistered()).not.toBeNull())
    // The rail marks General as holding unsaved edits (ST-3).
    expect(lastRegistered()!.dirtyPaths).toEqual(['project/general'])
  })
})

describe('ProjectGeneralSection — #207', () => {
  type Handler = (method: string, init?: RequestInit) => Response | undefined

  function mockDemo(handler: Handler = () => undefined) {
    const calls: { method: string; url: string; body?: string }[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      calls.push({ method, url, body: init?.body ? String(init.body) : undefined })
      if (url.endsWith('/api/v1/projects/demo')) {
        const handled = handler(method, init)
        if (handled) return handled
        if (method === 'GET') return jsonResponse(PROJECT)
      }
      if (url.endsWith('/api/v1/projects')) return jsonResponse([PROJECT])
      throw new Error(`Unhandled fetch: ${method} ${url}`)
    })
    return calls
  }

  function errorResponse(status: number, detail: string) {
    return new Response(JSON.stringify({ detail }), {
      status,
      headers: { 'Content-Type': 'application/json' },
    })
  }

  it('no longer shows dead Archive and Transfer ownership rows (WS-11)', async () => {
    mockDemo()
    renderSection()

    expect(await screen.findByRole('button', { name: /Delete project/ })).toBeInTheDocument()
    expect(screen.queryByText('Archive project')).not.toBeInTheDocument()
    expect(screen.queryByText('Transfer ownership')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Archive' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Transfer' })).not.toBeInTheDocument()
  })

  it('asks for the slug before deleting and shows a refusal in place (WS-9, WS-10)', async () => {
    const calls = mockDemo((method) =>
      method === 'DELETE' ? errorResponse(403, 'Only owners may delete projects') : undefined,
    )
    renderSection()

    fireEvent.click(await screen.findByRole('button', { name: /Delete project/ }))
    const dialog = await screen.findByRole('alertdialog')
    const confirm = within(dialog).getByRole('button', { name: /Delete project/ })
    expect(confirm).toBeDisabled()

    fireEvent.change(within(dialog).getByLabelText(/type demo to confirm/i), {
      target: { value: 'demo' },
    })
    fireEvent.click(confirm)

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      /Could not delete the project/,
    )
    expect(calls.filter((call) => call.method === 'DELETE')).toHaveLength(1)
  })

  it('announces a failed save as an error, not a hint (WS-15)', async () => {
    mockDemo((method) =>
      method === 'PATCH' ? errorResponse(409, 'A project with this slug already exists') : undefined,
    )
    renderSection()

    const nameInput = await screen.findByLabelText('Name')
    await waitFor(() => expect(nameInput).toHaveValue('Demo'))
    fireEvent.change(nameInput, { target: { value: 'Demo 2' } })
    fireEvent.click(at(screen.getAllByRole('button', { name: /Save/ }), 0))

    expect(await screen.findByRole('alert')).toHaveTextContent(/already exists/)
  })

  it('confirms a successful save (WS-15)', async () => {
    // The server keeps what it saved: the refetch after the save must answer
    // with the new name, or the form reads as edited again and "Saved" hides.
    let saved = PROJECT
    mockDemo((method) => {
      if (method === 'PATCH') {
        saved = { ...PROJECT, name: 'Demo 2' }
        return jsonResponse(saved)
      }
      return method === 'GET' ? jsonResponse(saved) : undefined
    })
    renderSection()

    const nameInput = await screen.findByLabelText('Name')
    await waitFor(() => expect(nameInput).toHaveValue('Demo'))
    fireEvent.change(nameInput, { target: { value: 'Demo 2' } })
    fireEvent.click(at(screen.getAllByRole('button', { name: /Save/ }), 0))

    expect(await screen.findByText('Saved')).toBeInTheDocument()
  })

  it('offers IANA zones instead of free text, and saves the chosen one (WS-16)', async () => {
    const calls = mockDemo((method, init) =>
      method === 'PATCH'
        ? jsonResponse({ ...PROJECT, ...(JSON.parse(String(init?.body)) as object) })
        : undefined,
    )
    renderSection()

    const zone = await screen.findByLabelText('Timezone')
    expect(zone.tagName).toBe('SELECT')
    await waitFor(() => expect(zone).toHaveValue('UTC'))
    expect(within(zone).getByRole('option', { name: 'Europe/Moscow' })).toBeInTheDocument()

    fireEvent.change(zone, { target: { value: 'Europe/Moscow' } })
    fireEvent.click(at(screen.getAllByRole('button', { name: /Save/ }), 0))

    await waitFor(() => {
      const patch = calls.find((call) => call.method === 'PATCH')
      expect(patch && JSON.parse(patch.body ?? '{}')).toMatchObject({ timezone: 'Europe/Moscow' })
    })
  })

  it('keeps a stored zone the browser does not know, flagged, without blocking Save', async () => {
    const calls = mockDemo((method, init) =>
      method === 'GET'
        ? jsonResponse({ ...PROJECT, timezone: 'Mars/Olympus_Mons' })
        : method === 'PATCH'
          ? jsonResponse({ ...PROJECT, ...(JSON.parse(String(init?.body)) as object) })
          : undefined,
    )
    renderSection()

    const zone = await screen.findByLabelText('Timezone')
    await waitFor(() => expect(zone).toHaveValue('Mars/Olympus_Mons'))
    expect(
      within(zone).getByRole('option', { name: 'Mars/Olympus_Mons (not recognised)' }),
    ).toBeInTheDocument()
    expect(zone).not.toHaveAttribute('aria-invalid', 'true')

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Demo 2' } })
    const save = at(screen.getAllByRole('button', { name: /Save/ }), 0)
    expect(save).toBeEnabled()
    fireEvent.click(save)

    await waitFor(() => {
      const patch = calls.find((call) => call.method === 'PATCH')
      expect(patch && JSON.parse(patch.body ?? '{}')).toMatchObject({
        name: 'Demo 2',
        timezone: 'Mars/Olympus_Mons',
      })
    })
  })

  it('reports a renamed slug upward so the page stays bound (WS-8)', async () => {
    const renamed = { ...PROJECT, slug: 'demo-renamed' }
    const requested: string[] = []
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      requested.push(`${method} ${url}`)
      if (url.endsWith('/api/v1/projects/demo') && method === 'PATCH') return jsonResponse(renamed)
      if (url.endsWith('/api/v1/projects/demo')) return jsonResponse(PROJECT)
      if (url.endsWith('/api/v1/projects/demo-renamed')) return jsonResponse(renamed)
      if (url.endsWith('/api/v1/projects')) return jsonResponse([renamed])
      throw new Error(`Unhandled fetch: ${method} ${url}`)
    })
    const onSlugChanged = vi.fn()
    // Stands in for SettingsArea: it rebinds the section to the slug it hears.
    function Harness() {
      const [slug, setSlug] = useState('demo')
      return (
        <ProjectGeneralSection
          slug={slug}
          onSlugChanged={(next) => {
            onSlugChanged(next)
            setSlug(next)
          }}
        />
      )
    }
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={ownerAuthValue()}>
          <MemoryRouter initialEntries={['/settings/project/general']}>
            <Harness />
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )

    const slugInput = await screen.findByLabelText('Slug')
    await waitFor(() => expect(slugInput).toHaveValue('demo'))
    fireEvent.change(slugInput, { target: { value: 'demo-renamed' } })
    fireEvent.click(at(screen.getAllByRole('button', { name: /Save/ }), 0))

    await waitFor(() => expect(onSlugChanged).toHaveBeenCalledWith('demo-renamed'))
    expect(await screen.findByText('Saved')).toBeInTheDocument()
    expect(screen.queryByText(/Could not load this project|Project not found/)).not.toBeInTheDocument()
    expect(screen.getByLabelText('Slug')).toHaveValue('demo-renamed')
    // The dead address is never asked for again once the rename lands.
    const patchIndex = requested.findIndex((entry) => entry.startsWith('PATCH '))
    expect(
      requested
        .slice(patchIndex + 1)
        .filter((entry) => entry.startsWith('GET ') && entry.endsWith('/api/v1/projects/demo')),
    ).toEqual([])
  })
})
