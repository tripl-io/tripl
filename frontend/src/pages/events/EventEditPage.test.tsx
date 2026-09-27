import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { toast } from 'sonner'

import { ApiError } from '@/api/client'
import { eventCommentsApi } from '@/api/eventComments'
import { eventsApi } from '@/api/events'
import { planBranchesApi } from '@/api/planBranches'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { BranchContext } from '@/components/branch-context-internal'
import type { EventType } from '@/types'

import EventEditPage from './EventForm'
import { readCreatedEvents } from './createdEventsHandoff'

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn(), info: vi.fn() } }))

vi.mock('@/api/events', () => ({
  eventsApi: {
    create: vi.fn(),
    update: vi.fn(),
    get: vi.fn(),
    list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    byNames: vi.fn().mockResolvedValue({ items: [] }),
  },
}))
vi.mock('@/api/eventComments', () => ({
  eventCommentsApi: {
    list: vi.fn().mockResolvedValue([]),
    create: vi.fn(),
    remove: vi.fn(),
    action: vi.fn(),
  },
}))
vi.mock('@/api/eventTypes', () => ({
  eventTypesApi: { list: vi.fn() },
}))
vi.mock('@/api/metaFields', () => ({
  metaFieldsApi: { list: vi.fn().mockResolvedValue([]) },
}))
vi.mock('@/api/variables', () => ({
  variablesApi: { list: vi.fn().mockResolvedValue([]) },
}))
vi.mock('@/api/users', () => ({
  usersApi: { list: vi.fn().mockResolvedValue([]) },
}))
vi.mock('@/api/planBranches', () => ({
  planBranchesApi: { list: vi.fn().mockResolvedValue({ items: [], total: 0 }) },
}))
vi.mock('@/api/scans', () => ({
  scansApi: { list: vi.fn().mockResolvedValue([]) },
}))
vi.mock('@/api/ai', () => ({
  aiApi: {
    status: vi.fn().mockResolvedValue({ enabled: false }),
    describeEvent: vi.fn(),
  },
}))

const EVENT_TYPE = {
  id: 'et-1',
  name: 'checkout',
  display_name: 'Checkout',
  field_definitions: [],
} as unknown as EventType

const CREATED = {
  id: 'ev-new',
  event_type_id: 'et-1',
  name: 'checkout:completed',
  title: '',
  description: '',
  status: 'draft',
  sunset_at: null,
  metric_breakdown_columns: [],
  tags: [],
  field_values: [],
  meta_values: [],
  warnings: [],
}

let queryClient: QueryClient

beforeEach(async () => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const { eventTypesApi } = await import('@/api/eventTypes')
  vi.mocked(eventTypesApi.list).mockResolvedValue([EVENT_TYPE])
  vi.mocked(eventsApi.create).mockResolvedValue(CREATED as never)
  vi.mocked(eventsApi.get).mockResolvedValue({ ...CREATED, branch_id: null } as never)
})

afterEach(() => {
  queryClient.clear()
  vi.clearAllMocks()
})

function renderAtNew() {
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/p/demo/events/all/new']}>
        <Routes>
          <Route path="/p/:slug/events/:tab/new" element={<EventEditPage />} />
          <Route path="/p/:slug/events/:tab/:eventId/edit" element={<EventEditPage />} />
          <Route path="/p/:slug/events/:tab" element={<div>events list</div>} />
          <Route path="/p/:slug/events" element={<div>events list</div>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

async function fillAndCreate(note: string) {
  const composer = await screen.findByLabelText(/posted as the first comment/i)
  fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'checkout:completed' } })
  fireEvent.change(composer, { target: { value: note } })
  fireEvent.click(screen.getByRole('button', { name: 'Create event' }))
}

/**
 * tripl-htfn.1 — "comments appeared, but only after creating and then editing
 * an event; at the moment of creating one there are no comments."
 */
describe('EventEditPage — a question raised while the event is being authored', () => {
  it('offers the discussion box on the create form, saying it is not the spec', async () => {
    renderAtNew()

    const composer = await screen.findByLabelText(/posted as the first comment/i)
    expect(composer).toBeInTheDocument()
    // The distinction is the whole reason this box is not Description: every
    // other field travels with the event into the implementer's spec.
    expect(screen.getByText(/kept out of the spec/i)).toBeInTheDocument()
  })

  it('posts the drafted note against the event that was just created', async () => {
    vi.mocked(eventCommentsApi.create).mockResolvedValue({} as never)
    renderAtNew()

    await fillAndCreate('Should this fire on cancel too?')

    await waitFor(() => expect(eventsApi.create).toHaveBeenCalled())
    // The id comes from the CREATE response, not from the route — there is no
    // event id in the route this was posted from.
    await waitFor(() =>
      expect(eventCommentsApi.create).toHaveBeenCalledWith(
        'demo',
        'ev-new',
        'Should this fire on cancel too?',
        null,
      ),
    )
  })

  it('posts nothing when the box was left empty', async () => {
    renderAtNew()

    await screen.findByLabelText(/posted as the first comment/i)
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'checkout:completed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create event' }))

    await waitFor(() => expect(eventsApi.create).toHaveBeenCalled())
    expect(eventCommentsApi.create).not.toHaveBeenCalled()
  })

  it('keeps the words when the note fails to post, on the event that now exists', async () => {
    // The event is created and the comment request fails. Closing the form here
    // would drop what the author wrote, and leaving them on a create form for
    // an event that already exists invites a duplicate.
    vi.mocked(eventCommentsApi.create).mockRejectedValue(new Error('network is down'))
    renderAtNew()

    await fillAndCreate('Does this fire on cancel?')

    // Landed on the created event, so its thread is what is on screen…
    const restored = await screen.findByLabelText('Write a comment')
    expect(restored).toHaveValue('Does this fire on cancel?')
    // …and the reason it is still a draft is stated rather than swallowed.
    expect(await screen.findByRole('alert')).toHaveTextContent(/note was not posted/i)
    expect(screen.queryByText('events list')).not.toBeInTheDocument()
  })
})

describe('EventEditPage layout and exits', () => {
  it('says the event was created, with a way to open it (AU-21)', async () => {
    renderAtNew()
    await screen.findByLabelText(/posted as the first comment/i)
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'checkout:completed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create event' }))

    await waitFor(() =>
      expect(toast.success).toHaveBeenCalledWith(
        'Created checkout:completed',
        expect.objectContaining({ action: expect.objectContaining({ label: 'Open' }) }),
      ),
    )
  })

  it('hands the new event to the list, which scrolls to and marks it (AU-21)', async () => {
    renderAtNew()
    await screen.findByLabelText(/posted as the first comment/i)
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'checkout:completed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create event' }))

    await waitFor(() => expect(readCreatedEvents('demo')).toEqual([CREATED.id]))
  })

  it('shows the form taking shape while it loads, not a sentence (AU-43)', async () => {
    const { eventTypesApi } = await import('@/api/eventTypes')
    vi.mocked(eventTypesApi.list).mockImplementation(() => new Promise(() => {}))
    renderAtNew()

    expect(await screen.findByRole('status')).toHaveTextContent('Loading the event form…')
  })

  it('says a missing event is not found, with the way back, and no retry (SH-33)', async () => {
    vi.mocked(eventsApi.get).mockRejectedValue(new ApiError('Event not found', 404))
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/p/demo/events/all/ev-gone/edit']}>
          <Routes>
            <Route path="/p/:slug/events/:tab/:eventId/edit" element={<EventEditPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('heading', { name: 'Event not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to Events' })).toHaveAttribute('href', '/p/demo/events')
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('puts the draft discussion note above the Create button (EVT-44)', async () => {
    renderAtNew()

    const composer = await screen.findByLabelText(/posted as the first comment/i)
    const create = screen.getByRole('button', { name: 'Create event' })
    // An author working top to bottom reaches the note before the button that
    // creates the event and leaves the page.
    expect(composer.compareDocumentPosition(create) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it("keeps the list's query string when a cold-opened editor closes (EVT-38)", async () => {
    function ListLocation() {
      const location = useLocation()
      return <div data-testid="list-location">{`${location.pathname}${location.search}`}</div>
    }
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/p/demo/events/all/new?branch=b-1&tag=checkout']}>
          <Routes>
            <Route path="/p/:slug/events/:tab/new" element={<EventEditPage />} />
            <Route path="/p/:slug/events" element={<ListLocation />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(await screen.findByTestId('list-location')).toHaveTextContent(
      '/p/demo/events?branch=b-1&tag=checkout',
    )
  })
})

describe('EventEditPage branch banner (EVT-42)', () => {
  const BRANCHES = {
    total: 2,
    items: [
      { id: 'main-id', project_id: 'p', name: 'main', kind: 'main', status: 'merged' },
      { id: 'br-1', project_id: 'p', name: 'PROJ-1', kind: 'working', status: 'draft' },
    ],
  }

  function renderBranchEdit(mainEventId: string | null) {
    vi.mocked(planBranchesApi.list).mockResolvedValue(BRANCHES as never)
    vi.mocked(eventsApi.get).mockResolvedValue({
      ...CREATED,
      id: 'ev-branch',
      event_type: { id: 'et-1', name: 'checkout', display_name: 'Checkout' },
      source_name: null,
      order: 0,
      owner_id: null,
      reviewed: false,
      last_seen_at: null,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
      branch_id: 'br-1',
      main_event_id: mainEventId,
    } as never)
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/p/demo/events/all/ev-branch/edit']}>
          <BranchContext.Provider value={{ branchId: 'br-1', setBranchId: vi.fn(), slug: 'demo' }}>
            <Routes>
              <Route path="/p/:slug/events/:tab/:eventId/edit" element={<EventEditPage />} />
            </Routes>
          </BranchContext.Provider>
        </MemoryRouter>
      </QueryClientProvider>,
    )
  }

  it("opens the event's main twin when the server names one", async () => {
    renderBranchEdit('ev-main')
    const link = await screen.findByRole('link', { name: 'View main plan' })
    expect(link).toHaveAttribute('href', '/p/demo/events/all/ev-main/edit')
  })

  it('falls back to the list on main for an event created on the branch', async () => {
    renderBranchEdit(null)
    const link = await screen.findByRole('link', { name: 'View main plan' })
    expect(link).toHaveAttribute('href', '/p/demo/events')
  })
})

describe('EventEditPage on the wrong branch (AU-1 / PL-2)', () => {
  it('shows a main event opened on a branch read-only, with the switch in place of Save', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({
      total: 2,
      items: [
        { id: 'main-id', project_id: 'p', name: 'main', kind: 'main', status: 'merged' },
        { id: 'br-1', project_id: 'p', name: 'PROJ-1', kind: 'working', status: 'draft' },
      ],
    } as never)
    vi.mocked(eventsApi.get).mockResolvedValue({ ...CREATED, id: 'ev-main', branch_id: 'main-id' } as never)
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/p/demo/events/all/ev-main/edit']}>
          <BranchContext.Provider value={{ branchId: 'br-1', setBranchId: vi.fn(), slug: 'demo' }}>
            <Routes>
              <Route path="/p/:slug/events/:tab/:eventId/edit" element={<EventEditPage />} />
            </Routes>
          </BranchContext.Provider>
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(await screen.findByText(/cannot be saved from here/)).toBeInTheDocument()
    // A save from here answered "Event not found" and lost the edit.
    expect(screen.queryByRole('button', { name: 'Save event' })).toBeNull()
    expect(document.querySelector('fieldset')).toBeDisabled()
    const switches = screen.getAllByRole('link', { name: 'Switch to main' })
    expect(switches.length).toBeGreaterThan(0)
    for (const link of switches) expect(link).toHaveAttribute('href', '/p/demo/events/all/ev-main/edit')
  })

  function renderMainEventOnBranch() {
    vi.mocked(eventsApi.get).mockResolvedValue({ ...CREATED, id: 'ev-main', branch_id: 'main-id' } as never)
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/p/demo/events/all/ev-main/edit']}>
          <BranchContext.Provider value={{ branchId: 'br-1', setBranchId: vi.fn(), slug: 'demo' }}>
            <Routes>
              <Route path="/p/:slug/events/:tab/:eventId/edit" element={<EventEditPage />} />
            </Routes>
          </BranchContext.Provider>
        </MemoryRouter>
      </QueryClientProvider>,
    )
  }

  it('holds the form back until the branch list says whether it may be saved', async () => {
    // The list never answers: the form used to render editable, Save live, in
    // the meantime.
    vi.mocked(planBranchesApi.list).mockReturnValue(new Promise(() => {}) as never)
    renderMainEventOnBranch()

    await waitFor(() => expect(planBranchesApi.list).toHaveBeenCalled())
    expect(screen.getByText('Loading event…')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save event' })).toBeNull()
  })

  it('still locks a row from elsewhere when the branch list fails', async () => {
    vi.mocked(planBranchesApi.list).mockRejectedValue(new Error('boom'))
    renderMainEventOnBranch()

    expect(
      await screen.findByText('This event lives outside the branch being edited, so it cannot be saved from here.'),
    ).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save event' })).toBeNull()
    expect(document.querySelector('fieldset')).toBeDisabled()
  })
})

describe('EventEditPage for a viewer (#237 AU-33 / JR-18)', () => {
  const VIEWER: AuthContextValue = {
    user: {
      id: 'viewer-1',
      email: 'viewer@example.com',
      name: 'Viewer',
      role: 'viewer',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    },
    status: 'authenticated',
    error: null,
    isLoggingOut: false,
    logout: async () => {},
    refresh: () => {},
  }

  function renderAsViewer(entry: string) {
    render(
      <QueryClientProvider client={queryClient}>
        <AuthContext.Provider value={VIEWER}>
          <MemoryRouter initialEntries={[entry]}>
            <Routes>
              <Route path="/p/:slug/events/:tab/new" element={<EventEditPage />} />
              <Route path="/p/:slug/events/:tab/:eventId/edit" element={<EventEditPage />} />
              <Route path="/p/:slug/events" element={<div>events list</div>} />
              <Route path="/p/:slug/monitoring/event/:eventId" element={<div>event detail</div>} />
            </Routes>
          </MemoryRouter>
        </AuthContext.Provider>
      </QueryClientProvider>,
    )
  }

  it('opens the event page built for reading instead of a disabled form', async () => {
    renderAsViewer('/p/demo/events/all/ev-1/edit')
    expect(await screen.findByText('event detail')).toBeInTheDocument()
    expect(document.querySelector('fieldset')).toBeNull()
  })

  it('returns a viewer from the create form to the list, saying why', async () => {
    renderAsViewer('/p/demo/events/all/new')
    expect(await screen.findByText('events list')).toBeInTheDocument()
    expect(toast.info).toHaveBeenCalledWith('Only editors can add events.', expect.anything())
  })
})
