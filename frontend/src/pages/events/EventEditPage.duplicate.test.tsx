import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import { eventsApi } from '@/api/events'
import { eventTypesApi } from '@/api/eventTypes'
import { planBranchesApi } from '@/api/planBranches'
import { BranchContext } from '@/components/branch-context-internal'
import { eventKey } from '@/lib/queryKeys'
import type { EventType } from '@/types'

import EventEditPage from './EventForm'

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
vi.mock('@/api/eventTypes', () => ({ eventTypesApi: { list: vi.fn() } }))
vi.mock('@/api/metaFields', () => ({ metaFieldsApi: { list: vi.fn().mockResolvedValue([]) } }))
vi.mock('@/api/variables', () => ({ variablesApi: { list: vi.fn().mockResolvedValue([]) } }))
vi.mock('@/api/users', () => ({ usersApi: { list: vi.fn().mockResolvedValue([]) } }))
vi.mock('@/api/planBranches', () => ({
  planBranchesApi: { list: vi.fn().mockResolvedValue({ items: [], total: 0 }) },
}))
vi.mock('@/api/scans', () => ({ scansApi: { list: vi.fn().mockResolvedValue([]) } }))
vi.mock('@/api/ai', () => ({
  aiApi: { status: vi.fn().mockResolvedValue({ enabled: false }), describeEvent: vi.fn() },
}))

const field = (id: string, eventTypeId: string) => ({
  id,
  event_type_id: eventTypeId,
  name: 'variant',
  display_name: 'Variant',
  field_type: 'string',
  is_required: false,
  enum_options: null,
  order: 0,
})
const MAIN_TYPE = {
  id: 'et-1',
  name: 'checkout',
  display_name: 'Checkout',
  field_definitions: [field('f-variant', 'et-1')],
} as unknown as EventType
/** The branch copy of MAIN_TYPE: new ids, same names. */
const BRANCH_TYPE = {
  ...MAIN_TYPE,
  id: 'et-b',
  field_definitions: [field('fb-variant', 'et-b')],
} as unknown as EventType

const SOURCE = {
  id: 'ev-1',
  event_type_id: 'et-1',
  event_type: { id: 'et-1', name: 'checkout', display_name: 'Checkout', color: '' },
  name: 'checkout_started',
  source_name: null,
  title: '',
  description: 'Fires when checkout opens',
  status: 'live',
  sunset_at: null,
  owner_id: null,
  metric_breakdown_columns: [],
  required_presence_threshold: null,
  tags: [],
  field_values: [{ id: 'fv-1', field_definition_id: 'f-variant', value: '${screen}' }],
  meta_values: [],
  branch_id: null,
}

let queryClient: QueryClient

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.mocked(eventTypesApi.list).mockResolvedValue([MAIN_TYPE])
  vi.mocked(eventsApi.get).mockResolvedValue(SOURCE as never)
})

afterEach(() => {
  queryClient.clear()
  vi.clearAllMocks()
})

/** A data router, so the unsaved-changes blocker is live as in the app. */
function renderAt(entry: string, branchId: string | null = null) {
  const page = (
    <BranchContext.Provider value={{ branchId, setBranchId: vi.fn(), slug: 'demo' }}>
      <EventEditPage />
    </BranchContext.Provider>
  )
  const router = createMemoryRouter(
    [
      { path: '/p/:slug/events/:tab/new', element: page },
      { path: '/p/:slug/events/:tab/:eventId/edit', element: page },
      { path: '/p/:slug/events', element: <div>events list</div> },
    ],
    { initialEntries: [entry] },
  )
  const wrap = (children: ReactNode) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  render(wrap(<RouterProvider router={router} />))
  return router
}

describe('EventEditPage duplicating an event (tripl-4zzc.7)', () => {
  it('loads the source on the active branch and opens a new event filled in from it', async () => {
    let resolve: (value: unknown) => void = () => {}
    vi.mocked(eventsApi.get).mockReturnValue(new Promise(r => { resolve = r }) as never)
    vi.mocked(eventTypesApi.list).mockResolvedValue([BRANCH_TYPE])
    renderAt('/p/demo/events/all/new?from=ev-b&branch=br-1', 'br-1')

    await waitFor(() => expect(eventsApi.get).toHaveBeenCalledWith('demo', 'ev-b', 'br-1'))
    // The form seeds itself once: it waits for the source.
    expect(screen.getByRole('status')).toHaveTextContent('Loading the event form…')
    expect(screen.queryByLabelText(/^Name/)).toBeNull()

    resolve({
      ...SOURCE,
      id: 'ev-b',
      event_type_id: 'et-b',
      field_values: [{ id: 'fv-1', field_definition_id: 'fb-variant', value: '${screen}' }],
      branch_id: 'br-1',
    })
    expect(await screen.findByRole('heading', { name: 'New event' })).toBeInTheDocument()
    expect(screen.getByLabelText(/^Name/)).toHaveValue('checkout_started')
    expect(screen.getByLabelText(/^Variant/)).toHaveValue('${screen}')
    expect(screen.getByText(/Status starts as Draft/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'checkout_started' })).toHaveAttribute(
      'href',
      '/p/demo/events/all/ev-b/edit?branch=br-1',
    )
  })

  it('says a missing source plainly, with a way to a blank form', async () => {
    vi.mocked(eventsApi.get).mockRejectedValue(new ApiError('Event not found', 404))
    renderAt('/p/demo/events/all/new?from=ev-gone&tag=checkout')

    expect(
      await screen.findByRole('heading', { name: 'Could not load the event to duplicate' }),
    ).toBeInTheDocument()
    // Not a retry for a row that is gone, and never a silent blank form.
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
    expect(screen.queryByLabelText(/^Name/)).toBeNull()
    expect(screen.getByRole('link', { name: 'Start a blank event' })).toHaveAttribute(
      'href',
      '/p/demo/events/all/new?tag=checkout',
    )
  })

  it("follows a main event's ids onto the branch copy of its type", async () => {
    vi.mocked(eventTypesApi.list).mockImplementation(async (_slug, branchId) =>
      branchId ? [BRANCH_TYPE] : [MAIN_TYPE],
    )
    renderAt('/p/demo/events/all/new?from=ev-1', 'br-1')

    expect(await screen.findByLabelText(/^Variant/)).toHaveValue('${screen}')
    expect(eventTypesApi.list).toHaveBeenCalledWith('demo', null)
    expect(screen.queryByText(/Not copied/)).toBeNull()
  })

  it('remounts the form when Duplicate is pressed on a cached source', async () => {
    const router = renderAt('/p/demo/events/all/ev-1/edit?tag=checkout')
    expect(await screen.findByRole('heading', { name: 'Edit · checkout_started' })).toBeInTheDocument()
    expect(screen.getByLabelText(/^Status/)).toHaveValue('live')
    // The source is in the cache: no skeleton unmounts the form between the two.
    expect(queryClient.getQueryData(eventKey('demo', null, 'ev-1'))).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Duplicate' }))

    expect(await screen.findByRole('heading', { name: 'New event' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/p/demo/events/all/new')
    expect(router.state.location.search).toBe('?tag=checkout&from=ev-1')
    // The create form's own state, not the edit form's left over.
    expect(screen.getByLabelText(/^Status/)).toHaveValue('draft')
    expect(screen.getByRole('button', { name: 'Create event' })).toBeDisabled()
    // An untouched copy is clean: no dialog appeared on the way.
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('asks once about unsaved edits, and stays on Keep editing', async () => {
    const router = renderAt('/p/demo/events/all/ev-1/edit')
    const description = await screen.findByLabelText(/^Description/)
    fireEvent.change(description, { target: { value: 'An edit not saved' } })

    fireEvent.click(screen.getByRole('button', { name: 'Duplicate' }))
    expect(await screen.findAllByRole('alertdialog')).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(router.state.location.pathname).toBe('/p/demo/events/all/ev-1/edit')
    expect(screen.getByLabelText(/^Description/)).toHaveValue('An edit not saved')
  })

  it('leaves after one Discard, and the copy holds the saved values', async () => {
    const router = renderAt('/p/demo/events/all/ev-1/edit')
    fireEvent.change(await screen.findByLabelText(/^Description/), {
      target: { value: 'An edit not saved' },
    })

    fireEvent.click(screen.getByRole('button', { name: 'Duplicate' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))

    expect(await screen.findByRole('heading', { name: 'New event' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/p/demo/events/all/new')
    // A second guard would have asked again here.
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(screen.getByLabelText(/^Description/)).toHaveValue('Fires when checkout opens')
  })

  it('offers no Duplicate on an event that lives on another plan', async () => {
    vi.mocked(planBranchesApi.list).mockResolvedValue({
      total: 2,
      items: [
        { id: 'main-id', project_id: 'p', name: 'main', kind: 'main', status: 'merged' },
        { id: 'br-1', project_id: 'p', name: 'PROJ-1', kind: 'working', status: 'draft' },
      ],
    } as never)
    vi.mocked(eventsApi.get).mockResolvedValue({ ...SOURCE, branch_id: 'main-id' } as never)
    renderAt('/p/demo/events/all/ev-1/edit', 'br-1')

    expect(await screen.findByText(/cannot be saved from here/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Duplicate' })).toBeNull()
  })
})
