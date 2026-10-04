import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation, useParams } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { EventType, FieldDefinition } from '@/types'
import { BranchContext } from '@/components/branch-context-internal'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { projectEventTypesKey } from '@/lib/queryKeys'
import { EventTypesTab, FieldsEditor } from './EventTypesTab'
import { EventTypeDetail } from './EventTypeDetailView'
import { at } from '@/test/at'
import { personaAuth } from '@/test/persona'
import { SessionProject } from '@/test/PersonaProject'

/**
 * Radix tabs select on mouse-down (and keyboard), not on click, so a bare
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

function field(over: Partial<FieldDefinition> & { id: string; name: string }): FieldDefinition {
  return {
    event_type_id: 'type-1',
    display_name: over.display_name ?? over.name,
    field_type: 'string',
    is_required: false,
    enum_options: null,
    description: '',
    order: 0,
    sensitivity: 'none',
    contract_max_bad_rate: 0,
    ...over,
  }
}

function eventType(over: Partial<EventType> & { id: string; name: string }): EventType {
  return {
    project_id: 'project-1',
    display_name: over.display_name ?? over.name,
    description: '',
    color: '#3b82f6',
    order: 0,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    field_definitions: [],
    ...over,
  }
}

const CHECKOUT = eventType({
  id: 'type-1',
  name: 'checkout',
  display_name: 'Checkout',
  description: 'Revenue-critical.',
  field_definitions: [
    field({ id: 'f-1', name: 'order_id', display_name: 'Order ID', is_required: true, order: 0 }),
    field({ id: 'f-2', name: 'email', display_name: 'Email', sensitivity: 'pii', order: 1 }),
  ],
})

const VIEWER = personaAuth('viewer')

function renderWithRoutes(
  initialPath: string,
  fetchImpl: typeof fetch,
  auth: AuthContextValue | null = null,
) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(fetchImpl)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <SessionProject session={auth}>
          <MemoryRouter initialEntries={[initialPath]}>
            <Routes>
              <Route path="/p/:slug/event-types/:itemId" element={<DetailRoute />} />
              <Route path="/p/:slug/event-types" element={<EventTypesTab slug="demo" />} />
              <Route path="/p/:slug/events/:tab" element={<div>events for tab</div>} />
            </Routes>
          </MemoryRouter>
        </SessionProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

function DetailRoute() {
  return <EventTypeDetail slug="demo" eventTypeId="type-1" />
}

/** The same routes, with a feature branch selected. */
function renderInBranch(initialPath: string, fetchImpl: typeof fetch) {
  vi.spyOn(globalThis, 'fetch').mockImplementation(fetchImpl)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <BranchContext.Provider value={{ branchId: 'branch-1', setBranchId: () => {}, slug: 'demo' }}>
        <MemoryRouter initialEntries={[initialPath]}>
          <Routes>
            <Route path="/p/:slug/event-types/:itemId" element={<DetailRoute />} />
            <Route path="/p/:slug/event-types" element={<EventTypesTab slug="demo" />} />
          </Routes>
        </MemoryRouter>
      </BranchContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('EventTypesTab list', () => {
  it('renders types in a table with fields/sensitive columns', async () => {
    renderWithRoutes('/p/demo/event-types', async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    expect(await screen.findByText('Checkout')).toBeInTheDocument()
    expect(screen.getByText('checkout_*')).toBeInTheDocument()
    // sensitive count chip (1 PII field)
    const row = screen.getByText('Checkout').closest('tr') as HTMLElement
    expect(within(row).getByText('2')).toBeInTheDocument() // 2 fields
  })

  it('heads a single-type project "1 type", not "1 types"', async () => {
    // A project has exactly one event type for as long as onboarding takes, so
    // "All types / 1 types" greeted every new project. The suite already
    // rendered this one-item list above without asserting the subtitle, which is
    // how it survived. `countOf` from @/lib/plural, same as the Scans list.
    renderWithRoutes('/p/demo/event-types', async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    expect(await screen.findByText('1 type')).toBeInTheDocument()
    expect(screen.queryByText('1 types')).not.toBeInTheDocument()
  })

  it('opens a page-style create view instead of a dialog', async () => {
    renderWithRoutes('/p/demo/event-types', async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    fireEvent.click(await screen.findByRole('button', { name: /New type/i }))
    expect(await screen.findByText('New event type')).toBeInTheDocument()
    // no dialog role present
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Cancel/i }))
    expect(await screen.findByText('All types')).toBeInTheDocument()
  })

  it('offers a viewer no New type, and says why once', async () => {
    renderWithRoutes(
      '/p/demo/event-types',
      async (input) => {
        const url = String(input)
        if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
        if (url.endsWith('/api/v1/projects/demo/event-type-owners')) return mockJsonResponse([])
        throw new Error(`Unhandled fetch: ${url}`)
      },
      VIEWER,
    )

    expect(await screen.findByText('Checkout')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/viewer role/)
    expect(screen.queryByRole('button', { name: /New type/i })).not.toBeInTheDocument()
  })

  it('shows an understandable merge approval ("Open") instead of "open merge"', async () => {
    renderWithRoutes('/p/demo/event-types', async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      if (url.endsWith('/api/v1/projects/demo/event-type-owners'))
        return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const row = (await screen.findByText('Checkout')).closest('tr') as HTMLElement
    // Only once the owners have answered: an unanswered request is "—", not a
    // guess of "Open to merge".
    expect(await within(row).findByText('Open to merge')).toBeInTheDocument()
    // The column says what it is about.
    expect(screen.getByRole('columnheader', { name: 'Merge approval' })).toBeInTheDocument()
    // the cryptic raw words are gone
    expect(screen.queryByText('open merge')).not.toBeInTheDocument()
  })

  it('marks an owner-gated type as needing owner approval', async () => {
    const owner = {
      id: 'o-1',
      event_type_id: 'type-1',
      user_id: 'u-1',
      user_email: 'ada@x.io',
      user_name: 'Ada',
      granted_by: null,
      created_at: '2026-01-01T00:00:00Z',
    }
    renderWithRoutes('/p/demo/event-types', async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      if (url.endsWith('/api/v1/projects/demo/event-type-owners'))
        return mockJsonResponse([owner])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const row = (await screen.findByText('Checkout')).closest('tr') as HTMLElement
    await waitFor(() => expect(within(row).getByText('Owner approval')).toBeInTheDocument())
  })

  it('renders the list as an accessible table with a full-word Required header', async () => {
    renderWithRoutes('/p/demo/event-types', async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      if (url.endsWith('/api/v1/projects/demo/event-type-owners'))
        return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    await screen.findByText('Checkout')
    const table = screen.getByRole('table', { name: 'Event types' })
    expect(within(table).getByRole('columnheader', { name: 'Required' })).toBeInTheDocument()
    expect(within(table).queryByRole('columnheader', { name: 'Req' })).not.toBeInTheDocument()
  })
})

describe('FieldsEditor fields table', () => {
  it('labels the required column with the full word, not "Req"', () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <FieldsEditor slug="demo" eventType={CHECKOUT} branchId={null} />
      </QueryClientProvider>,
    )

    const table = screen.getByRole('table')
    expect(within(table).getByRole('columnheader', { name: 'Required' })).toBeInTheDocument()
    expect(within(table).queryByRole('columnheader', { name: 'Req' })).not.toBeInTheDocument()
  })

  it('shows the backend 409 when a scan names events by the field being deleted', async () => {
    // services/field_service._reject_if_a_scan_names_events_by refuses this
    // deletion; without an alert the row simply stays and nothing explains why,
    // which is how the guard would be invisible from the plan UI.
    // Wording copied from scan_config_lookup.name_format_conflict_detail, whole
    // rather than abbreviated: *scan*, not "scan config", and both plurals
    // spelled out. The backend owns that rule and
    // test_name_format_conflict_vocabulary enforces it, so this fixture is a
    // sample of what arrives rather than a second definition of it — which only
    // holds if it is the actual sentence.
    const detail =
      "Cannot delete this field. The field 'order_id' is used by the event name " +
      "format of 1 scan: 'Old events (iOS)' ({order_id}). Without it the scan " +
      'cannot build an event name and every collection fails with ' +
      "'the event name format references unknown keys'. Edit the scan's " +
      'Event name format so it no longer references this column, then delete the field.'
    // A fresh Response per call, routed by URL: the confirm now asks
    // POST /impact what the field affects (#257), and a single shared Response
    // would be consumed by that request before the DELETE could read it.
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input instanceof Request ? input.url : input)
      if (url.includes('/impact')) return mockJsonResponse({ items: [] })
      return new Response(JSON.stringify({ detail }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <FieldsEditor slug="demo" eventType={CHECKOUT} branchId={null} />
      </QueryClientProvider>,
    )

    fireEvent.click(at(screen.getAllByRole('button', { name: /^Delete field / }), 0))
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))

    // The WHOLE detail, not a fragment of it: the design chose to render the
    // shared 409 untouched rather than have this tab rewrite the backend's
    // wording into the web UI's nouns. A partial match would still pass if
    // someone added that rewriter and it silently stopped matching.
    expect(await screen.findByRole('alert')).toHaveTextContent(detail)
  })
})

describe('FieldsEditor field edit subpage', () => {
  function openNewField() {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      mockJsonResponse({}),
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <FieldsEditor slug="demo" eventType={CHECKOUT} branchId={null} />
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Add field' }))
    return { fetchSpy }
  }

  it('is a form whose Save is its submit button, so Enter saves', () => {
    openNewField()
    expect(screen.getByRole('heading', { name: 'New field' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add field' })).toHaveAttribute('type', 'submit')
    expect(screen.getByRole('button', { name: 'Cancel' })).toHaveAttribute('type', 'button')
  })

  it('says why a new field without a name is not saved', () => {
    const { fetchSpy } = openNewField()
    fireEvent.click(screen.getByRole('button', { name: 'Add field' }))

    expect(screen.getByRole('alert')).toHaveTextContent('A new field needs a name.')
    expect(screen.getByLabelText('Name')).toHaveAttribute('aria-invalid', 'true')
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('asks before Cancel throws away a half-filled field, and keeps it on Cancel', async () => {
    openNewField()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'order_id' } })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    const confirm = await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    fireEvent.click(within(confirm).getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.getByLabelText('Name')).toHaveValue('order_id')

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() =>
      expect(screen.queryByRole('heading', { name: 'New field' })).not.toBeInTheDocument(),
    )
  })

  it('leaves an untouched field page at once', () => {
    openNewField()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('heading', { name: 'New field' })).not.toBeInTheDocument()
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })
})

describe('EventTypeDetail tabbed page', () => {
  function detailFetch(input: RequestInfo | URL) {
    const url = String(input)
    if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
    if (url.endsWith('/api/v1/projects/demo/event-types/type-1/owners')) return mockJsonResponse([])
    if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
    if (url.endsWith('/api/v1/projects/demo/members')) return mockJsonResponse([])
    throw new Error(`Unhandled fetch: ${url}`)
  }

  it('shows tabs and the summary tab by default', async () => {
    renderWithRoutes('/p/demo/event-types/type-1', async (input) => detailFetch(input))

    expect(await screen.findAllByText('Checkout')).not.toHaveLength(0)
    expect(screen.getByRole('tab', { name: 'Events' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Summary' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Settings' })).toBeInTheDocument()
    // summary stats
    expect(await screen.findByText('Required fields')).toBeInTheDocument()
  })

  it('renders settings tab with page-style cards and no dialogs', async () => {
    renderWithRoutes('/p/demo/event-types/type-1', async (input) => detailFetch(input))

    selectTab(await screen.findByRole('tab', { name: 'Settings' }))
    expect(await screen.findByText('General')).toBeInTheDocument()
    expect(screen.getByText('Fields')).toBeInTheDocument()
    expect(screen.getByText('Owners')).toBeInTheDocument()
    expect(screen.getByText('Danger zone')).toBeInTheDocument()
    expect(screen.getByText('gates merge')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('edits a field via an in-place subpage (no popup)', async () => {
    renderWithRoutes('/p/demo/event-types/type-1', async (input) => detailFetch(input))

    selectTab(await screen.findByRole('tab', { name: 'Settings' }))
    // open the field edit subpage for order_id
    fireEvent.click(await screen.findByText('order_id'))
    expect(await screen.findByText('Edit field · order_id')).toBeInTheDocument()
    // data contract section is present in the subpage
    expect(screen.getByText('Data contract')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    // The other cards step aside while the field page is open.
    expect(screen.getByText('Danger zone')).not.toBeVisible()
    expect(screen.queryByRole('button', { name: 'Save changes' })).not.toBeInTheDocument()
    // back to fields list
    fireEvent.click(screen.getByRole('button', { name: /Fields/i }))
    await waitFor(() => expect(screen.queryByText('Edit field · order_id')).not.toBeInTheDocument())
    expect(screen.getByText('Danger zone')).toBeVisible()
  })

  it('shows a viewer the settings with no way to change them', async () => {
    renderWithRoutes('/p/demo/event-types/type-1', async (input) => detailFetch(input), VIEWER)

    selectTab(await screen.findByRole('tab', { name: 'Settings' }))
    expect(await screen.findByText('General')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/viewer role/)
    expect(screen.queryByRole('button', { name: /Save changes/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Add field/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit field' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete field' })).not.toBeInTheDocument()
    expect(screen.queryByText('Danger zone')).not.toBeInTheDocument()
    // The General card reads as a definition, not as disabled controls.
    expect(screen.queryByDisplayValue('Revenue-critical.')).not.toBeInTheDocument()
    expect(screen.getByText('Type name').tagName).toBe('DT')
    expect(screen.getByText('Display name').tagName).toBe('DT')

    // A field row is information for a viewer, not a way into the editor.
    fireEvent.click(screen.getByText('order_id'))
    expect(screen.queryByText('Edit field · order_id')).not.toBeInTheDocument()
  })

  it('asks before a tab switch throws away a field draft', async () => {
    renderWithRoutes('/p/demo/event-types/type-1', async (input) => detailFetch(input))

    selectTab(await screen.findByRole('tab', { name: 'Settings' }))
    fireEvent.click(await screen.findByRole('button', { name: /Add field/i }))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'coupon' } })

    selectTab(screen.getByRole('tab', { name: 'Summary' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    fireEvent.click(within(confirm).getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.getByRole('tab', { name: 'Settings' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByLabelText('Name')).toHaveValue('coupon')

    selectTab(screen.getByRole('tab', { name: 'Summary' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() =>
      expect(screen.getByRole('tab', { name: 'Summary' })).toHaveAttribute('aria-selected', 'true'),
    )
  })

  it('switches tabs at once with no draft', async () => {
    renderWithRoutes('/p/demo/event-types/type-1', async (input) => detailFetch(input))

    selectTab(await screen.findByRole('tab', { name: 'Settings' }))
    fireEvent.click(await screen.findByRole('button', { name: /Add field/i }))
    selectTab(screen.getByRole('tab', { name: 'Summary' }))

    expect(screen.getByRole('tab', { name: 'Summary' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()
  })

  it('opens a page-style add-field subpage', async () => {
    renderWithRoutes('/p/demo/event-types/type-1', async (input) => detailFetch(input))

    selectTab(await screen.findByRole('tab', { name: 'Settings' }))
    fireEvent.click(await screen.findByRole('button', { name: /Add field/i }))
    expect(await screen.findByText('New field')).toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })
})

describe('EventTypesTab in branch context', () => {
  it('does not ask for owners, which live on main under main ids', async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/v1/projects/demo/event-types?branch=branch-1'))
        return mockJsonResponse([CHECKOUT])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderInBranch('/p/demo/event-types', fetchImpl as unknown as typeof fetch)

    expect(await screen.findByText('Checkout')).toBeInTheDocument()
    const asked = fetchImpl.mock.calls.map(([input]) => String(input))
    expect(asked.some((url) => url.includes('/owners'))).toBe(false)
    // Nor does the list pretend to know: the Owner column stays hidden.
    expect(screen.queryByText('Owner')).not.toBeInTheDocument()
    // …and so does Merge approval, which used to call every type "ungated" here —
    // wrong for exactly the types whose owners will gate this branch.
    expect(screen.queryByRole('columnheader', { name: 'Merge approval' })).not.toBeInTheDocument()
    expect(screen.queryByText('Open to merge')).not.toBeInTheDocument()
  })

  it('leaves the merge-gate chip off the branch detail instead of claiming "no owners"', async () => {
    const fetchImpl = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/v1/projects/demo/event-types?branch=branch-1'))
        return mockJsonResponse([CHECKOUT])
      if (url.includes('/api/v1/projects/demo/event-types/type-1?branch=branch-1'))
        return mockJsonResponse(CHECKOUT)
      if (url.includes('/api/v1/projects/demo/event-types/type-1/'))
        return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    renderInBranch('/p/demo/event-types/type-1', fetchImpl as unknown as typeof fetch)

    expect(await screen.findByRole('heading', { name: 'Checkout' })).toBeInTheDocument()
    const asked = fetchImpl.mock.calls.map(([input]) => String(input))
    expect(asked.some((url) => url.includes('/owners'))).toBe(false)
    expect(screen.queryByText(/anyone can merge/i)).not.toBeInTheDocument()
  })
})

describe('EventTypesTab list states and rows', () => {
  it('shows a skeleton, not "No event types yet", while the list loads', async () => {
    renderWithRoutes('/p/demo/event-types', () => new Promise<Response>(() => {}))

    expect(await screen.findByLabelText('Loading event types')).toBeInTheDocument()
    expect(screen.queryByText(/No event types yet/)).not.toBeInTheDocument()
  })

  it('shows a failed load as an error with a retry, not as an empty list', async () => {
    renderWithRoutes('/p/demo/event-types', async () =>
      new Response(JSON.stringify({ detail: 'Database is unavailable' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    expect(await screen.findByText("Couldn't load event types")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.queryByText(/No event types yet/)).not.toBeInTheDocument()
  })

  it('opens a type through a real link, keeping the row a table row', async () => {
    renderWithRoutes('/p/demo/event-types', async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      if (url.endsWith('/api/v1/projects/demo/event-type-owners')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const link = await screen.findByRole('link', { name: 'Checkout' })
    expect(link).toHaveAttribute('href', '/p/demo/event-types/type-1')
    const table = screen.getByRole('table', { name: 'Event types' })
    expect(within(table).queryAllByRole('button')).toHaveLength(0)
    expect(within(table).getAllByRole('row').length).toBeGreaterThan(1)
  })
})

describe('FieldsEditor field form', () => {
  function openNewField() {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () =>
      mockJsonResponse({}),
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <FieldsEditor slug="demo" eventType={CHECKOUT} branchId={null} />
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'Add field' }))
    return { fetchSpy }
  }

  it('names every input by its label and describes it by its hint', () => {
    openNewField()

    for (const label of ['Name', 'Display name', 'Type', 'Sensitivity', 'Required', 'Description', 'Max invalid share', 'Null share', 'Regex']) {
      expect(screen.getByLabelText(label)).toBeInTheDocument()
    }
    expect(screen.getByLabelText('Max invalid share')).toHaveAccessibleDescription(
      /Share of values \(0–1\) allowed to break the rules below/,
    )
    expect(screen.getByLabelText('Max invalid share')).toHaveAttribute('inputmode', 'decimal')
  })

  it('offers only the contract rules the field type can use', () => {
    openNewField()
    // A string field: a pattern, no bounds.
    expect(screen.getByLabelText('Regex')).toBeInTheDocument()
    expect(screen.queryByLabelText('Min')).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'number' } })
    expect(screen.getByLabelText('Min')).toBeInTheDocument()
    expect(screen.getByLabelText('Max')).toBeInTheDocument()
    expect(screen.queryByLabelText('Regex')).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'enum' } })
    expect(screen.queryByLabelText('Regex')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Min')).not.toBeInTheDocument()
    expect(screen.getByText('Values outside the enum options count as invalid.')).toBeInTheDocument()
    // The shares apply to every type.
    expect(screen.getByLabelText('Max invalid share')).toBeInTheDocument()
    expect(screen.getByLabelText('Null share')).toBeInTheDocument()
  })

  it('refuses a contract number that does not parse instead of dropping the rule', () => {
    const { fetchSpy } = openNewField()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'order_id' } })
    fireEvent.change(screen.getByLabelText('Null share'), { target: { value: 'abc' } })

    expect(screen.getByLabelText('Null share')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText('Null share')).toHaveAccessibleDescription(/between 0 and 1/)
    fireEvent.click(screen.getByRole('button', { name: 'Add field' }))
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('says Min above Max, and refuses to save it', () => {
    const { fetchSpy } = openNewField()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'order_id' } })
    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'number' } })
    fireEvent.change(screen.getByLabelText('Min'), { target: { value: '10' } })
    fireEvent.change(screen.getByLabelText('Max'), { target: { value: '2' } })

    expect(screen.getByText('Max must be at least Min.')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Add field' }))
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('saves a Python/RE2 pattern JavaScript cannot compile, with a note', async () => {
    const { fetchSpy } = openNewField()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'order_id' } })
    fireEvent.change(screen.getByLabelText('Regex'), { target: { value: '(?i)^checkout_' } })

    expect(screen.getByText(/The server checks it when you save/)).toBeInTheDocument()
    expect(screen.getByLabelText('Regex')).not.toHaveAttribute('aria-invalid')
    fireEvent.click(screen.getByRole('button', { name: 'Add field' }))

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const [, init] = at(fetchSpy.mock.calls, 0)
    expect(JSON.parse(String(init?.body))).toMatchObject({ contract_regex: '(?i)^checkout_' })
  })

  it('lets a field with a saved RE2 pattern be edited and saved', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async () => mockJsonResponse({}))
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    const typed = eventType({
      id: 'type-1',
      name: 'checkout',
      field_definitions: [
        field({ id: 'f-1', name: 'step', contract_regex: '(?P<step>[a-z]+)' }),
      ],
    })
    render(
      <QueryClientProvider client={queryClient}>
        <FieldsEditor slug="demo" eventType={typed} branchId={null} />
      </QueryClientProvider>,
    )
    fireEvent.click(screen.getByRole('button', { name: 'step' }))
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: 'Funnel step' } })
    // An unchanged saved pattern gets no note at all.
    expect(screen.queryByText(/The server checks it/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save field' }))

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
  })

  it('never turns a blank Max invalid share into the strictest setting', () => {
    const { fetchSpy } = openNewField()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'order_id' } })
    fireEvent.change(screen.getByLabelText('Max invalid share'), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add field' }))

    expect(screen.getByLabelText('Max invalid share')).toHaveAttribute('aria-invalid', 'true')
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('reads a decimal comma as a point', async () => {
    const { fetchSpy } = openNewField()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'order_id' } })
    fireEvent.change(screen.getByLabelText('Null share'), { target: { value: '0,5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add field' }))

    await waitFor(() => expect(fetchSpy).toHaveBeenCalled())
    const [, init] = at(fetchSpy.mock.calls, 0)
    expect(JSON.parse(String(init?.body))).toMatchObject({
      contract_required_max_null_rate: 0.5,
      contract_max_bad_rate: 0,
    })
  })
})

describe('EventTypeDetail settings', () => {
  const OWNER = {
    id: 'o-1',
    event_type_id: 'type-1',
    user_id: 'u-1',
    user_email: 'ada@x.io',
    user_name: 'Ada',
    granted_by: null,
    created_at: '2026-01-01T00:00:00Z',
  }

  it('opens the tab named in ?tab=', async () => {
    renderWithRoutes('/p/demo/event-types/type-1?tab=settings', async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      if (url.endsWith('/owners')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/members')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events?')) return mockJsonResponse({ items: [], total: 0 })
      throw new Error(`Unhandled fetch: ${url}`)
    })

    expect(await screen.findByRole('tab', { name: 'Settings' })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByText('General')).toBeInTheDocument()
  })

  it('keeps Delete shut until the affected events are counted', async () => {
    renderWithRoutes('/p/demo/event-types/type-1?tab=settings', async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      if (url.endsWith('/owners')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/members')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events?')) return new Promise<Response>(() => {})
      throw new Error(`Unhandled fetch: ${url}`)
    })

    expect(await screen.findByText('Danger zone')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled()
    expect(screen.queryByText(/nothing else is affected/)).not.toBeInTheDocument()
  })

  it('states the worst case when the count failed, instead of "nothing else is affected"', async () => {
    renderWithRoutes('/p/demo/event-types/type-1?tab=settings', async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      if (url.endsWith('/owners')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/members')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events?'))
        return new Response(JSON.stringify({ detail: 'boom' }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        })
      throw new Error(`Unhandled fetch: ${url}`)
    })

    expect(await screen.findByText(/Could not count the events that use this type/)).toBeInTheDocument()
    expect(screen.queryByText(/nothing else is affected/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled()
  })

  it('confirms an owner removal and shows a refusal', async () => {
    const calls: string[] = []
    renderWithRoutes('/p/demo/event-types/type-1?tab=settings', async (input, init) => {
      const url = String(input)
      if (init?.method === 'DELETE') {
        calls.push(url)
        return new Response(JSON.stringify({ detail: 'Owner role required' }), {
          status: 403,
          headers: { 'Content-Type': 'application/json' },
        })
      }
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      if (url.endsWith('/owners')) return mockJsonResponse([OWNER])
      if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/members')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events?')) return mockJsonResponse({ items: [], total: 0 })
      throw new Error(`Unhandled fetch: ${url}`)
    })

    fireEvent.click(await screen.findByRole('button', { name: 'Remove owner Ada' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Remove owner' })
    expect(within(confirm).getByText(/anyone will be able to merge/)).toBeInTheDocument()
    expect(calls).toEqual([])
    fireEvent.click(within(confirm).getByRole('button', { name: 'Remove' }))

    expect(await screen.findByText(/Could not remove the owner: Owner role required/)).toBeInTheDocument()
    expect(calls).toHaveLength(1)
  })

  it('offers only project members and instance owners as new owners', async () => {
    renderWithRoutes('/p/demo/event-types/type-1?tab=settings', async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      if (url.endsWith('/owners')) return mockJsonResponse([OWNER])
      if (url.endsWith('/api/v1/projects/demo/members')) {
        return mockJsonResponse([
          // Already an owner, so not offered again.
          { user_id: 'u-1', name: 'Ada', email: 'ada@x.io', role: 'editor', added_at: '2026-01-01T00:00:00Z' },
          { user_id: 'u-2', name: 'Grace', email: 'grace@x.io', role: 'viewer', added_at: '2026-01-01T00:00:00Z' },
        ])
      }
      // A roster member who is not in the project must not be offered; an
      // instance owner, who never holds a member row, is.
      if (url.endsWith('/api/v1/users')) {
        return mockJsonResponse([
          { id: 'u-3', name: 'Linus', email: 'linus@x.io', role: 'editor', created_at: '2026-01-01T00:00:00Z' },
          { id: 'u-4', name: 'Boss', email: 'boss@x.io', role: 'owner', created_at: '2026-01-01T00:00:00Z' },
          // Also listed as a member: offered once.
          { id: 'u-2', name: 'Grace', email: 'grace@x.io', role: 'owner', created_at: '2026-01-01T00:00:00Z' },
        ])
      }
      if (url.includes('/api/v1/projects/demo/events?')) return mockJsonResponse({ items: [], total: 0 })
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const picker = await screen.findByLabelText('New owner')
    expect(within(picker).getByRole('option', { name: 'Grace · grace@x.io' })).toBeInTheDocument()
    expect(within(picker).queryByRole('option', { name: 'Ada · ada@x.io' })).toBeNull()
    expect(within(picker).queryByRole('option', { name: 'Linus · linus@x.io' })).toBeNull()
    expect(await within(picker).findByRole('option', { name: 'Boss · boss@x.io' })).toBeInTheDocument()
    expect(within(picker).getAllByRole('option', { name: 'Grace · grace@x.io' })).toHaveLength(1)
  })

  it('disables Save until the general settings change, and says when they saved', async () => {
    let types = [CHECKOUT]
    renderWithRoutes('/p/demo/event-types/type-1?tab=settings', async (input, init) => {
      const url = String(input)
      if (init?.method === 'PATCH' && url.endsWith('/event-types/type-1')) {
        const body = JSON.parse(String(init.body))
        types = [{ ...CHECKOUT, ...body }]
        return mockJsonResponse(types[0])
      }
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse(types)
      if (url.endsWith('/owners')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/members')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events?')) return mockJsonResponse({ items: [], total: 0 })
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const save = await screen.findByRole('button', { name: 'Save changes' })
    expect(save).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Checkout flow' } })
    expect(save).toBeEnabled()
    fireEvent.click(save)

    expect(await screen.findByText('Saved')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
  })
})

describe('EventTypeDetail across a branch switch', () => {
  const BRANCH_COPY = { ...CHECKOUT, id: 'type-9' }

  function ParamRoute() {
    const { itemId } = useParams()
    const location = useLocation()
    return (
      <>
        <p data-testid="path">{location.pathname}</p>
        <EventTypeDetail slug="demo" eventTypeId={itemId ?? ''} />
      </>
    )
  }

  it('follows the type by name to its id on the new branch', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.includes('/api/v1/projects/demo/event-types?branch=branch-1'))
        return mockJsonResponse([BRANCH_COPY])
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      if (url.endsWith('/owners')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const tree = (branchId: string | null) => (
      <QueryClientProvider client={queryClient}>
        <BranchContext.Provider value={{ branchId, setBranchId: () => {}, slug: 'demo' }}>
          <MemoryRouter initialEntries={['/p/demo/event-types/type-1']}>
            <Routes>
              <Route path="/p/:slug/event-types/:itemId" element={<ParamRoute />} />
            </Routes>
          </MemoryRouter>
        </BranchContext.Provider>
      </QueryClientProvider>
    )
    const view = render(tree(null))
    expect(await screen.findByRole('heading', { name: 'Checkout' })).toBeInTheDocument()

    view.rerender(tree('branch-1'))

    await waitFor(() =>
      expect(screen.getByTestId('path')).toHaveTextContent('/p/demo/event-types/type-9'),
    )
    expect(screen.getByRole('heading', { name: 'Checkout' })).toBeInTheDocument()
    expect(screen.queryByText(/does not exist|Event type not found/)).not.toBeInTheDocument()
  })
})

describe('FieldsEditor reordering', () => {
  it('moves the row at once, announces it, and keeps focus on a working button', async () => {
    let types = [CHECKOUT]
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/fields/reorder')) {
        const ids = JSON.parse(String(init?.body)) as string[] | { field_ids: string[] }
        const order = Array.isArray(ids) ? ids : ids.field_ids
        types = [
          {
            ...CHECKOUT,
            field_definitions: CHECKOUT.field_definitions.map((f) => ({ ...f, order: order.indexOf(f.id) })),
          },
        ]
        return mockJsonResponse({})
      }
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse(types)
      if (url.endsWith('/owners')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/members')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events?')) return mockJsonResponse({ items: [], total: 0 })
      throw new Error(`Unhandled fetch: ${url}`)
    })
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/p/demo/event-types/type-1?tab=settings']}>
          <Routes>
            <Route path="/p/:slug/event-types/:itemId" element={<DetailRoute />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    const up = await screen.findByRole('button', { name: 'Move email up' })
    // Focus opens the button's tooltip, a state update of its own.
    act(() => up.focus())
    fireEvent.click(up)

    expect(await screen.findByText('email moved to position 1 of 2')).toBeInTheDocument()
    // Now first, so Move up is disabled; focus went to the button that works.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Move email down' })).toHaveFocus(),
    )
    expect(screen.getByRole('button', { name: 'Move email up' })).toBeDisabled()
  })
})

describe('review 204 follow-ups', () => {
  it('keeps a field draft on screen when a refetch of the list fails', async () => {
    let failing = false
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) {
        return failing
          ? new Response(JSON.stringify({ detail: 'Bad gateway' }), {
              status: 502,
              headers: { 'Content-Type': 'application/json' },
            })
          : mockJsonResponse([CHECKOUT])
      }
      if (url.endsWith('/owners')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/users')) return mockJsonResponse([])
      if (url.endsWith('/api/v1/projects/demo/members')) return mockJsonResponse([])
      if (url.includes('/api/v1/projects/demo/events?')) return mockJsonResponse({ items: [], total: 0 })
      throw new Error(`Unhandled fetch: ${url}`)
    })
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/p/demo/event-types/type-1?tab=settings']}>
          <Routes>
            <Route path="/p/:slug/event-types/:itemId" element={<DetailRoute />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )
    fireEvent.click(await screen.findByRole('button', { name: /Add field/i }))
    fireEvent.change(await screen.findByLabelText('Name'), { target: { value: 'coupon' } })

    failing = true
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: projectEventTypesKey('demo') })
    })

    expect(await screen.findByText(/Couldn't refresh this event type: Bad gateway/)).toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveValue('coupon')
    expect(screen.queryByText("Couldn't load this event type")).not.toBeInTheDocument()
  })

  it('renders both tables through the shared table component', async () => {
    renderWithRoutes('/p/demo/event-types', async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT])
      if (url.endsWith('/api/v1/projects/demo/event-type-owners')) return mockJsonResponse([])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const table = await screen.findByRole('table', { name: 'Event types' })
    expect(table).toHaveAttribute('data-slot', 'table')
  })
})

describe('EventTypesTab owners in one request', () => {
  it('asks once for the project, and reads a type without rows as open', async () => {
    const SIGNUP = eventType({ id: 'type-2', name: 'signup', display_name: 'Signup', order: 1 })
    const owner = {
      id: 'o-1',
      event_type_id: 'type-1',
      user_id: 'u-1',
      user_email: 'ada@x.io',
      user_name: 'Ada',
      granted_by: null,
      created_at: '2026-01-01T00:00:00Z',
    }
    const asked: string[] = []
    renderWithRoutes('/p/demo/event-types', async (input) => {
      const url = String(input)
      asked.push(url)
      if (url.endsWith('/api/v1/projects/demo/event-types')) return mockJsonResponse([CHECKOUT, SIGNUP])
      if (url.endsWith('/api/v1/projects/demo/event-type-owners')) return mockJsonResponse([owner])
      throw new Error(`Unhandled fetch: ${url}`)
    })

    const checkout = (await screen.findByText('Checkout')).closest('tr') as HTMLElement
    const signup = screen.getByText('Signup').closest('tr') as HTMLElement
    expect(await within(checkout).findByText('Owner approval')).toBeInTheDocument()
    expect(within(signup).getByText('Open to merge')).toBeInTheDocument()
    expect(asked.filter((url) => url.includes('owners'))).toEqual([
      expect.stringMatching(/\/event-type-owners$/),
    ])
  })
})
