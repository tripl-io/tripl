import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AuthContextValue } from '@/components/auth-context'
import { AuthContext } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import { eventTypesApi } from '@/api/eventTypes'
import { relationsApi } from '@/api/relations'
import type { EventType, EventTypeRelation } from '@/types'
import { RelationsTab } from './RelationsTab'
import { personaAuth } from '@/test/persona'
import { SessionProject } from '@/test/PersonaProject'

vi.mock('@/api/relations', () => ({
  relationsApi: { list: vi.fn(), create: vi.fn(), update: vi.fn(), del: vi.fn() },
}))
vi.mock('@/api/eventTypes', () => ({
  eventTypesApi: { list: vi.fn() },
}))

const TYPES = [
  { id: 'et-1', name: 'purchase', display_name: 'Purchase', field_definitions: [{ id: 'f-1', name: 'user_id' }] },
  { id: 'et-2', name: 'signup', display_name: 'Signup', field_definitions: [{ id: 'f-2', name: 'user_id' }] },
] as unknown as EventType[]

const RELATION = {
  id: 'rel-1',
  source_event_type_id: 'et-1',
  target_event_type_id: 'et-2',
  source_field_id: 'f-1',
  target_field_id: 'f-2',
  relation_type: 'shared_field',
} as unknown as EventTypeRelation

function renderTab(auth: AuthContextValue | null, { seed = true }: { seed?: boolean } = {}) {
  if (seed) vi.mocked(relationsApi.list).mockResolvedValue([RELATION])
  vi.mocked(eventTypesApi.list).mockResolvedValue(TYPES)
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        <SessionProject session={auth}>
          <RelationsTab slug="demo" />
        </SessionProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('RelationsTab', () => {
  it('offers an editor Add and Delete', async () => {
    renderTab(authAs('member'))

    expect(
      await screen.findByRole('button', { name: 'Delete relation between purchase.user_id and signup.user_id' }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /New relation/ })).toBeInTheDocument()
  })

  it('offers a viewer neither, and says why once', async () => {
    renderTab(personaAuth('viewer'))

    expect(await screen.findByText('purchase.user_id')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/viewer role/)
    expect(screen.queryByRole('button', { name: /New relation/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Delete relation/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Edit relation/ })).not.toBeInTheDocument()
  })

  it('names the joined fields in each row and in the delete confirm', async () => {
    renderTab(authAs('member'))

    expect(await screen.findByText('purchase.user_id')).toBeInTheDocument()
    expect(screen.getByText('signup.user_id')).toBeInTheDocument()

    fireEvent.click(
      screen.getByRole('button', { name: 'Delete relation between purchase.user_id and signup.user_id' }),
    )
    const confirm = await screen.findByRole('alertdialog')
    expect(within(confirm).getByText('Remove the relation purchase.user_id → signup.user_id?')).toBeInTheDocument()
  })

  it('says a failed delete failed instead of leaving the row in silence', async () => {
    vi.mocked(relationsApi.del).mockRejectedValue(new Error('Relation is in use'))
    renderTab(authAs('member'))

    fireEvent.click(
      await screen.findByRole('button', { name: 'Delete relation between purchase.user_id and signup.user_id' }),
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Relation is in use')
  })

  it('shows a skeleton, not "No relations yet", while the list loads', async () => {
    vi.mocked(relationsApi.list).mockReturnValue(new Promise(() => {}))
    renderTab(authAs('member'), { seed: false })

    expect(await screen.findByLabelText('Loading relations')).toBeInTheDocument()
    expect(screen.queryByText('No relations yet')).not.toBeInTheDocument()
  })

  it('shows a failed load as an error with a retry, not as an empty list', async () => {
    vi.mocked(relationsApi.list).mockRejectedValue(new Error('boom'))
    renderTab(authAs('member'), { seed: false })

    expect(await screen.findByText("Couldn't load relations")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.queryByText('No relations yet')).not.toBeInTheDocument()
  })

  it('shows the relation type in words and the join in one cell', async () => {
    renderTab(authAs('member'))

    const source = await screen.findByText('purchase.user_id')
    expect(screen.getByText('shared field')).toBeInTheDocument()
    expect(source.closest('td')).toHaveTextContent('purchase.user_id→ to signup.user_id')
  })

  it('lets the join wrap on a phone instead of running under the pinned actions', async () => {
    renderTab(authAs('member'))

    const target = await screen.findByText('signup.user_id')
    // The target takes a line of its own below `sm`; a long name breaks where it must.
    expect(target).toHaveClass('max-sm:block')
    expect(target.closest('td')).toHaveClass('wrap-anywhere')
    const actions = target.closest('tr')?.lastElementChild
    expect(actions).toHaveClass('sticky', 'right-0')
    expect(actions?.className).toMatch(/shadow-\[/)
  })

  it('groups each end of a new relation and previews the join', async () => {
    renderTab(authAs('member'))
    await screen.findByText('purchase.user_id')

    fireEvent.click(screen.getByRole('button', { name: /New relation/ }))
    const dialog = await screen.findByRole('dialog', { name: 'New relation' })

    // A field select waits for its type.
    expect(within(dialog).getByRole('combobox', { name: 'From field' })).toBeDisabled()
    expect(within(dialog).getByText('Pick a field on each side to preview the join.')).toBeInTheDocument()

    fireEvent.change(within(dialog).getByRole('combobox', { name: 'From event type' }), { target: { value: 'et-1' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'From field' }), { target: { value: 'f-1' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'To event type' }), { target: { value: 'et-2' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'To field' }), { target: { value: 'f-2' } })

    expect(within(dialog).getByText(/^Joins/)).toHaveTextContent('Joins purchase.user_id → signup.user_id')
    expect(within(dialog).getByRole('button', { name: 'Create' })).toBeEnabled()
  })

  it('edits a relation in place with the same From/To dialog', async () => {
    vi.mocked(relationsApi.update).mockResolvedValue(RELATION)
    renderTab(authAs('member'))

    fireEvent.click(
      await screen.findByRole('button', { name: 'Edit relation between purchase.user_id and signup.user_id' }),
    )
    const dialog = await screen.findByRole('dialog', { name: 'Edit relation' })

    // Seeded with the relation's ends; Save waits for a change.
    expect(within(dialog).getByRole('combobox', { name: 'From event type' })).toHaveValue('et-1')
    expect(within(dialog).getByRole('combobox', { name: 'To field' })).toHaveValue('f-2')
    const save = within(dialog).getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    // Swap the ends.
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'From event type' }), { target: { value: 'et-2' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'From field' }), { target: { value: 'f-2' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'To event type' }), { target: { value: 'et-1' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'To field' }), { target: { value: 'f-1' } })
    fireEvent.click(save)

    await waitFor(() => expect(relationsApi.update).toHaveBeenCalledWith(
      'demo',
      'rel-1',
      {
        source_event_type_id: 'et-2',
        target_event_type_id: 'et-1',
        source_field_id: 'f-2',
        target_field_id: 'f-1',
      },
      null,
    ))
    expect(relationsApi.create).not.toHaveBeenCalled()
  })

  describe('with no relations yet', () => {
    function renderEmpty(types: EventType[]) {
      vi.mocked(relationsApi.list).mockResolvedValue([])
      vi.mocked(eventTypesApi.list).mockResolvedValue(types)
      const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      const auth = authAs('member')
      return render(
        <QueryClientProvider client={queryClient}>
          <AuthContext.Provider value={auth}>
            <SessionProject session={auth}>
              <MemoryRouter>
                <RelationsTab slug="demo" />
              </MemoryRouter>
            </SessionProject>
          </AuthContext.Provider>
        </QueryClientProvider>,
      )
    }

    it('sends a project with no fielded event type to Event types, not to an empty dialog', async () => {
      renderEmpty([{ id: 'et-1', name: 'purchase', display_name: 'Purchase', field_definitions: [] }] as unknown as EventType[])

      // Both lists have to load: until the types do, nothing is known to block.
      expect(await screen.findByRole('link', { name: 'Go to event types' })).toHaveAttribute(
        'href',
        '/p/demo/event-types',
      )
      expect(screen.getByText('No relations yet')).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /Create your first relation/ })).not.toBeInTheDocument()
      // New relation is off, and says why next to it.
      const create = screen.getByRole('button', { name: /New relation/ })
      expect(create).toBeDisabled()
      expect(create).toHaveAccessibleDescription('Create an event type with at least one field first.')
    })

    it('invites the first relation once a type has a field, with one example on the page', async () => {
      renderEmpty(TYPES)

      expect(await screen.findByRole('button', { name: /Create your first relation/ })).toBeInTheDocument()
      await waitFor(() => expect(eventTypesApi.list).toHaveBeenCalled())
      expect(screen.getByRole('button', { name: /New relation/ })).toBeEnabled()
      expect(screen.queryByText(/Purchase\.user_id/)).not.toBeInTheDocument()
    })
  })
})

describe('RelationsTab — unsaved relation (prelaunch)', () => {
  it('asks before Escape drops picked ends, and closes an untouched dialog at once', async () => {
    renderTab(authAs('member'))
    await screen.findByText('purchase.user_id')

    fireEvent.click(screen.getByRole('button', { name: /New relation/ }))
    let dialog = await screen.findByRole('dialog', { name: 'New relation' })
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New relation' })).not.toBeInTheDocument())
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /New relation/ }))
    dialog = await screen.findByRole('dialog', { name: 'New relation' })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'From event type' }), { target: { value: 'et-1' } })
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(await screen.findByRole('alertdialog')).toHaveTextContent('Leave without saving?')
    fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(within(screen.getByRole('dialog', { name: 'New relation' })).getByRole('combobox', { name: 'From event type' })).toHaveValue('et-1')
  })
})
