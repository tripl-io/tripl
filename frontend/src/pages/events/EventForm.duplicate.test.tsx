import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createElement, type ComponentProps, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import type { Event as TEvent, EventType, MetaFieldDefinition } from '@/types'
import { eventsApi } from '@/api/events'
import { planBranchesApi } from '@/api/planBranches'
import { BranchContext } from '@/components/branch-context-internal'
import { EventForm } from './EventForm'
import type { EventDuplicate } from './duplicateEvent'

vi.mock('@/api/duplicates', () => ({
  MAX_DUPLICATE_CANDIDATES: 500,
  duplicatesApi: {
    check: vi.fn().mockResolvedValue({ items: [], threshold: 0.88 }),
    clusters: vi.fn(),
    dismiss: vi.fn(),
  },
}))
vi.mock('@/api/events', () => ({
  eventsApi: {
    create: vi.fn(),
    update: vi.fn(),
    // No holder on the server: every block below comes from the form itself.
    byNames: vi.fn().mockResolvedValue({ items: [] }),
    list: vi.fn().mockResolvedValue({ items: [], total: 0 }),
    get: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('@/api/dependencies', () => ({
  dependenciesApi: { impact: vi.fn().mockResolvedValue({ items: [] }), get: vi.fn() },
}))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))
vi.mock('@/api/users', () => ({ usersApi: { list: vi.fn().mockResolvedValue([]) } }))
vi.mock('@/api/planBranches', () => ({
  planBranchesApi: { list: vi.fn().mockResolvedValue({ items: [], total: 0 }) },
}))
vi.mock('@/api/scans', () => ({ scansApi: { list: vi.fn().mockResolvedValue([]) } }))
vi.mock('@/api/ai', () => ({
  aiApi: { status: vi.fn().mockResolvedValue({ enabled: false }), describeEvent: vi.fn() },
}))

const VARIANT_FIELD = {
  id: 'field-variant',
  event_type_id: 'et-1',
  name: 'variant',
  display_name: 'Variant',
  field_type: 'string',
  is_required: false,
  enum_options: null,
  order: 0,
}
const FREE_TYPE = {
  id: 'et-1',
  name: 'checkout',
  display_name: 'Checkout',
  field_definitions: [VARIANT_FIELD],
} as unknown as EventType
const RULED_TYPE = { ...FREE_TYPE, event_name_format: 'pv:{variant}' } as EventType

function duplicateOf(name: string, identity = name): EventDuplicate {
  return {
    source: { id: 'ev-src', name, identity, eventTypeId: 'et-1' },
    seed: {
      eventTypeId: 'et-1',
      name: name.startsWith('pv:') ? '' : name,
      title: 'Checkout started',
      description: 'Fires when checkout opens',
      ownerId: '',
      metricBreakdownColumns: [],
      tags: ['checkout'],
      fieldValues: { 'field-variant': name.startsWith('pv:') ? name.slice(3) : '${screen}' },
      metaValues: {},
      requiredPresenceThreshold: 0.9,
    },
    dropped: [],
  }
}

let queryClient: QueryClient

function wrapper({ children }: { children: ReactNode }) {
  return createElement(
    QueryClientProvider,
    { client: queryClient },
    createElement(MemoryRouter, null, children),
  )
}

function renderForm(props: Partial<ComponentProps<typeof EventForm>>) {
  return render(
    createElement(EventForm, {
      slug: 'demo',
      eventTypes: [FREE_TYPE],
      metaFields: [],
      projectVariables: [],
      event: null,
      onClose: () => {},
      ...props,
    }),
    { wrapper },
  )
}

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  vi.mocked(eventsApi.create).mockResolvedValue({
    id: 'ev-new',
    name: 'checkout_completed',
    event_type_id: 'et-1',
    warnings: [],
  } as never)
})

afterEach(() => {
  queryClient.clear()
  vi.clearAllMocks()
})

describe('EventForm filled in from a duplicate (tripl-4zzc.7)', () => {
  it('opens as a new Draft event carrying the source values', () => {
    renderForm({ duplicate: duplicateOf('checkout_started') })

    expect(screen.getByRole('heading', { name: 'New event' })).toBeInTheDocument()
    expect(screen.getByLabelText(/^Name/)).toHaveValue('checkout_started')
    expect(screen.getByLabelText(/^Title/)).toHaveValue('Checkout started')
    expect(screen.getByLabelText(/^Description/)).toHaveValue('Fires when checkout opens')
    // Copied byte for byte, token and all.
    expect(screen.getByLabelText(/^Variant/)).toHaveValue('${screen}')
    expect(screen.getByLabelText(/^Status/)).toHaveValue('draft')
  })

  it("blocks the source's own name until it changes, without claiming this form created it", async () => {
    renderForm({ duplicate: duplicateOf('checkout_started') })

    expect(screen.getByText(/the event you are duplicating\. Change the name/)).toBeInTheDocument()
    expect(screen.queryByText(/This form has just created/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create event' })).toBeDisabled()

    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'checkout_completed' } })
    expect(screen.queryByText(/the event you are duplicating/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create event' })).toBeEnabled()
  })

  it('sends a Draft with the copied threshold, and no lifecycle of the source', async () => {
    renderForm({ duplicate: duplicateOf('checkout_started') })
    fireEvent.change(screen.getByLabelText(/^Name/), { target: { value: 'checkout_completed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create event' }))

    await waitFor(() => expect(eventsApi.create).toHaveBeenCalled())
    const [, body] = vi.mocked(eventsApi.create).mock.calls[0]!
    expect(body).toMatchObject({
      event_type_id: 'et-1',
      name: 'checkout_completed',
      status: 'draft',
      sunset_at: null,
      tags: ['checkout'],
      required_presence_threshold: 0.9,
      field_values: [{ field_definition_id: 'field-variant', value: '${screen}' }],
    })
    expect(body).not.toHaveProperty('superseded_by_event_id')
  })

  it('blocks a rule-governed copy at once, before the server is asked', () => {
    renderForm({ eventTypes: [RULED_TYPE], duplicate: duplicateOf('pv:b1') })

    // The copied values recompose the source's identity.
    expect(screen.getByLabelText(/^Name/)).toHaveValue('pv:b1')
    expect(screen.getByText(/These values name pv:b1, the event you are duplicating/)).toBeInTheDocument()
    // The source name is not seeded into a box the rule owns.
    expect(screen.queryByText(/is not used/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create event' })).toBeDisabled()

    fireEvent.change(screen.getByLabelText(/^Variant/), { target: { value: 'b2' } })
    expect(screen.queryByText(/the event you are duplicating/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create event' })).toBeEnabled()
  })

  it('offers Duplicate on an editable saved event only', () => {
    const onDuplicate = vi.fn()
    const saved = {
      id: 'ev-src',
      event_type_id: 'et-1',
      name: 'checkout_started',
      title: '',
      description: '',
      status: 'live',
      sunset_at: null,
      owner_id: null,
      metric_breakdown_columns: [],
      tags: [],
      field_values: [],
      meta_values: [],
    } as unknown as TEvent
    const { unmount } = renderForm({ event: saved, onDuplicate })
    const button = screen.getByRole('button', { name: 'Duplicate' })
    // Not a second submit button inside the form.
    expect(button).toHaveAttribute('type', 'button')
    fireEvent.click(button)
    expect(onDuplicate).toHaveBeenCalledTimes(1)
    unmount()

    renderForm({ event: saved, onDuplicate, lockedReason: 'Lives on main.' })
    expect(screen.queryByRole('button', { name: 'Duplicate' })).toBeNull()
  })

  it('has no Duplicate on a new event', () => {
    renderForm({ onDuplicate: vi.fn() })
    expect(screen.queryByRole('button', { name: 'Duplicate' })).toBeNull()
  })
})

describe('EventForm duplicate on a branch named after a ticket', () => {
  const JIRA_FIELD: MetaFieldDefinition = {
    id: 'mf-jira',
    project_id: 'project-1',
    name: 'jira',
    display_name: 'Jira',
    field_type: 'string',
    is_required: false,
    enum_options: null,
    default_value: null,
    link_template: 'https://jira.example/browse/${value}',
    order: 0,
    sensitivity: 'none',
  }

  function reloadIsGuarded(): boolean {
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    return event.defaultPrevented
  }

  // The leave guard's state, read through the reload prompt it arms: Cancel,
  // Back and a reload all ask exactly when this is true.
  async function renderOnBranch(metaValues: Record<string, string[]>) {
    vi.mocked(planBranchesApi.list).mockResolvedValue(
      { items: [{ id: 'b-proj', name: 'PROJ-4770', kind: 'working' }], total: 1 } as never,
    )
    const duplicate = duplicateOf('checkout_started')
    render(
      createElement(EventForm, {
        slug: 'demo',
        eventTypes: [FREE_TYPE],
        metaFields: [JIRA_FIELD],
        projectVariables: [],
        event: null,
        onClose: () => {},
        duplicate: { ...duplicate, seed: { ...duplicate.seed, metaValues } },
      }),
      {
        wrapper: ({ children }: { children: ReactNode }) =>
          createElement(
            QueryClientProvider,
            { client: queryClient },
            createElement(
              BranchContext.Provider,
              { value: { branchId: 'b-proj', setBranchId: () => {}, slug: 'demo' } },
              createElement(MemoryRouter, null, children),
            ),
          ),
      },
    )
    // The branch list has arrived and the prefill effect has had its turn.
    await waitFor(() => expect(planBranchesApi.list).toHaveBeenCalled())
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 50))
    })
  }

  it('opens untouched when the copied key is the branch\'s own', async () => {
    await renderOnBranch({ 'mf-jira': ['PROJ-4770'] })
    expect(screen.getByLabelText('Jira')).toHaveValue('PROJ-4770')
    expect(reloadIsGuarded()).toBe(false)
  })

  it('keeps a copied key from another ticket, unmarked as edited', async () => {
    await renderOnBranch({ 'mf-jira': ['PROJ-1'] })
    expect(screen.getByLabelText('Jira')).toHaveValue('PROJ-1')
    expect(reloadIsGuarded()).toBe(false)
  })

  it('fills the branch key when the source had none', async () => {
    await renderOnBranch({})
    expect(screen.getByLabelText('Jira')).toHaveValue('PROJ-4770')
    expect(reloadIsGuarded()).toBe(false)
  })
})
