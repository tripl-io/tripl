import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MetaFieldDefinition, Project } from '@/types'
import { AuthContext, type AuthContextValue } from '@/components/auth-context'
import { authAs } from '@/test/auth'
import { ActiveProjectContext } from '@/components/active-project-context'
import { metaFieldsApi } from '@/api/metaFields'
import { MetaFieldsTab } from './MetaFieldsTab'
import { personaAuth, viewerProject } from '@/test/persona'

vi.mock('@/api/metaFields', () => ({
  metaFieldsApi: {
    list: vi.fn(),
    create: vi.fn(),
    update: vi.fn(),
    usage: vi.fn(),
    del: vi.fn(),
  },
}))

function metaField(over: Partial<MetaFieldDefinition> & { id: string; name: string }) {
  return {
    project_id: 'project-1',
    display_name: over.display_name ?? over.name,
    field_type: 'string',
    is_required: false,
    allow_multiple: false,
    enum_options: null,
    default_value: null,
    link_template: null,
    order: 0,
    sensitivity: 'none',
    ...over,
  } as MetaFieldDefinition
}

function renderTab(
  fields: MetaFieldDefinition[] | null = [],
  {
    auth = null,
    project,
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
  }: { auth?: AuthContextValue | null; project?: Project; queryClient?: QueryClient } = {},
) {
  // `null` leaves the list mock to the test, for a pending or failing load.
  if (fields) vi.mocked(metaFieldsApi.list).mockResolvedValue(fields)
  return render(
    <QueryClientProvider client={queryClient}>
      <AuthContext.Provider value={auth}>
        {/* What the app shell provides once it has resolved the URL's project. */}
        <ActiveProjectContext.Provider value={project}>
          {/* The header links to Event types. */}
          <MemoryRouter>
            <MetaFieldsTab slug="demo" />
          </MemoryRouter>
        </ActiveProjectContext.Provider>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('MetaFieldsTab — Allow multiple', () => {
  it('sends the flag when the box is ticked', async () => {
    vi.mocked(metaFieldsApi.create).mockResolvedValue(metaField({ id: 'mf-1', name: 'jira_keys' }))
    renderTab()

    fireEvent.click(screen.getByRole('button', { name: /New meta field/i }))
    fireEvent.change(screen.getByLabelText('Name'), {
      target: { value: 'jira_keys' },
    })
    fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Jira keys' } })
    fireEvent.click(screen.getByLabelText('Multiple values'))
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() =>
      expect(metaFieldsApi.create).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ name: 'jira_keys', allow_multiple: true }),
        null,
      ),
    )
  })

  it('does not offer the box for a type that cannot hold a list', async () => {
    renderTab()

    fireEvent.click(screen.getByRole('button', { name: /New meta field/i }))
    expect(screen.getByLabelText('Multiple values')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'date' } })
    expect(screen.queryByLabelText('Multiple values')).not.toBeInTheDocument()
  })

  it('drops a tick left behind by a type switch instead of sending a 422', async () => {
    vi.mocked(metaFieldsApi.create).mockResolvedValue(metaField({ id: 'mf-1', name: 'shipped' }))
    renderTab()

    fireEvent.click(screen.getByRole('button', { name: /New meta field/i }))
    fireEvent.change(screen.getByLabelText('Name'), {
      target: { value: 'shipped' },
    })
    fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Shipped' } })
    fireEvent.click(screen.getByLabelText('Multiple values'))
    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'boolean' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() =>
      expect(metaFieldsApi.create).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ field_type: 'boolean', allow_multiple: false }),
        null,
      ),
    )
  })

  it('warns that stored values stay when the flag is turned off', async () => {
    renderTab([metaField({ id: 'mf-1', name: 'jira_keys', allow_multiple: true })])

    fireEvent.click(await screen.findByRole('button', { name: 'Edit jira_keys' }))
    expect(screen.getByLabelText('Multiple values')).toBeChecked()
    expect(screen.queryByText(/keeps the first value only/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('Multiple values'))
    expect(screen.getByText(/keeps the first value only/)).toBeInTheDocument()
  })
})

describe('MetaFieldsTab — read-only visitors', () => {
  const FIELD = metaField({ id: 'mf-1', name: 'jira_link', display_name: 'Jira link' })

  it('offers a viewer no write controls, and says why once', async () => {
    renderTab([FIELD], { auth: personaAuth('viewer'), project: viewerProject() })

    expect(await screen.findByText('jira_link')).toBeInTheDocument()
    expect(screen.getByRole('note')).toHaveTextContent(/viewer role/)
    expect(screen.queryByRole('button', { name: /New meta field/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit Jira link' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete Jira link' })).not.toBeInTheDocument()
  })

  it("treats a member in another user's demo as read-only, as the API does", async () => {
    const demo = { slug: 'demo', is_demo: true, created_by_user_id: 'someone-else' } as Project
    renderTab([FIELD], { auth: authAs('member'), project: demo })

    expect(await screen.findByText('jira_link')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New meta field/ })).not.toBeInTheDocument()
  })

  it('lets a member write in a demo they created', async () => {
    const demo = { slug: 'demo', is_demo: true, created_by_user_id: 'member-1' } as Project
    renderTab([FIELD], { auth: authAs('member'), project: demo })

    expect(await screen.findByRole('button', { name: 'Edit Jira link' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /New meta field/ })).toBeInTheDocument()
  })
})

describe('MetaFieldsTab — load and delete states', () => {
  const FIELD = metaField({ id: 'mf-1', name: 'jira_link', display_name: 'Jira link' })

  it('shows a skeleton, not "No meta fields yet", while the list loads', async () => {
    vi.mocked(metaFieldsApi.list).mockReturnValue(new Promise(() => {}))
    renderTab(null)

    expect(await screen.findByLabelText('Loading meta fields')).toBeInTheDocument()
    expect(screen.queryByText('No meta fields yet')).not.toBeInTheDocument()
  })

  it('shows a failed load as an error with a retry, not as an empty list', async () => {
    vi.mocked(metaFieldsApi.list).mockRejectedValue(new Error('boom'))
    renderTab(null)

    expect(await screen.findByText("Couldn't load meta fields")).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument()
    expect(screen.queryByText('No meta fields yet')).not.toBeInTheDocument()
  })

  it('says a failed delete failed', async () => {
    vi.mocked(metaFieldsApi.del).mockRejectedValue(new Error('Field is referenced'))
    // No usage count: the confirm falls back to naming the loss in words.
    vi.mocked(metaFieldsApi.usage).mockRejectedValue(new Error('usage unavailable'))
    renderTab([FIELD], { auth: authAs('member') })

    fireEvent.click(await screen.findByRole('button', { name: 'Delete Jira link' }))
    // The confirm names what goes, and its button names what it deletes.
    expect(await screen.findByText(/Removes every Jira link value from the events that carry one/)).toBeInTheDocument()
    fireEvent.click(await screen.findByRole('button', { name: 'Delete meta field' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Field is referenced')
  })

  it('counts the values and events a delete removes when the usage answers', async () => {
    vi.mocked(metaFieldsApi.usage).mockResolvedValue({ value_count: 3, event_count: 2 })
    renderTab([FIELD], { auth: authAs('member') })

    fireEvent.click(await screen.findByRole('button', { name: 'Delete Jira link' }))
    expect(
      await screen.findByText("Removes 3 Jira link values from 2 events. This can't be undone."),
    ).toBeInTheDocument()
  })

  it("refreshes the branch review's project-wide meta-field cache after an edit", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    // What BranchesTab reads the ticket link template from.
    queryClient.setQueryData(['metaFields', 'demo'], [FIELD])
    vi.mocked(metaFieldsApi.update).mockResolvedValue(FIELD)
    renderTab([FIELD], { auth: authAs('member'), queryClient })

    fireEvent.click(await screen.findByRole('button', { name: 'Edit Jira link' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(queryClient.getQueryState(['metaFields', 'demo'])?.isInvalidated).toBe(true),
    )
  })
})

describe('MetaFieldsTab — inline validation', () => {
  it('flags every empty required field inline instead of a browser bubble', async () => {
    renderTab()

    fireEvent.click(screen.getByRole('button', { name: /New meta field/i }))
    expect(screen.getByRole('dialog', { name: 'New meta field' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    for (const label of ['Name', 'Display name']) {
      const input = screen.getByLabelText(label)
      expect(input).not.toHaveAttribute('required')
      expect(input).toHaveAttribute('aria-invalid', 'true')
      expect(input).toHaveAccessibleDescription('Required')
    }
    expect(metaFieldsApi.create).not.toHaveBeenCalled()
  })

  it('removes an enum option with a labelled X button', async () => {
    renderTab()

    fireEvent.click(screen.getByRole('button', { name: /New meta field/i }))
    fireEvent.change(screen.getByLabelText('Type'), { target: { value: 'enum' } })
    const input = screen.getByLabelText('Enum options')
    fireEvent.change(input, { target: { value: 'high' } })
    fireEvent.keyDown(input, { key: 'Enter' })

    fireEvent.click(screen.getByRole('button', { name: 'Remove option high' }))
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Remove option high' })).not.toBeInTheDocument(),
    )
  })
})

describe('MetaFieldsTab — naming (#238)', () => {
  it('is titled "Meta fields" and points per-type fields at Event types', async () => {
    renderTab()

    expect(screen.getByRole('heading', { level: 1, name: 'Meta fields' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'event type' })).toHaveAttribute(
      'href',
      '/p/demo/event-types',
    )
  })
})

describe('MetaFieldsTab — link template (#244)', () => {
  function openCreateWithLink(template: string) {
    fireEvent.click(screen.getByRole('button', { name: /New meta field/i }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'jira' } })
    fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Jira' } })
    fireEvent.click(screen.getByLabelText('Display as link'))
    fireEvent.change(screen.getByLabelText('Link template'), { target: { value: template } })
  }

  it('refuses a template with no ${value} and says where the key goes', async () => {
    renderTab()
    openCreateWithLink('https://jira.example.com/browse/')
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    expect(await screen.findByText(/where the key goes/)).toBeInTheDocument()
    expect(screen.getByLabelText('Link template')).toHaveAttribute('aria-invalid', 'true')
    expect(metaFieldsApi.create).not.toHaveBeenCalled()
  })

  it('reads {value} as ${value}, previews the link and saves it normalised', async () => {
    vi.mocked(metaFieldsApi.create).mockResolvedValue(metaField({ id: 'mf-1', name: 'jira' }))
    renderTab()
    openCreateWithLink('https://jira.example.com/{value}')

    expect(screen.getByText('https://jira.example.com/PROJ-1234')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() =>
      expect(metaFieldsApi.create).toHaveBeenCalledWith(
        'demo',
        expect.objectContaining({ link_template: 'https://jira.example.com/${value}' }),
        null,
      ),
    )
  })
})

describe('MetaFieldsTab — phone width', () => {
  it('lets a long link template wrap, and edges the pinned actions over what they cover', async () => {
    renderTab([metaField({ id: 'mf-1', name: 'jira', link_template: 'https://jira.example.com/browse/PROJ-1' })])

    const link = await screen.findByText('Link: https://jira.example.com/browse/PROJ-1')
    expect(link).toHaveClass('wrap-anywhere')
    const actions = link.closest('tr')?.lastElementChild
    expect(actions).toHaveClass('sticky', 'right-0')
    expect(actions?.className).toMatch(/shadow-\[/)
  })
})

describe('MetaFieldsTab — unsaved dialogs (prelaunch)', () => {
  const FIELD = metaField({ id: 'mf-1', name: 'jira_link', display_name: 'Jira link' })

  it('asks before Escape drops a typed new meta field, and opens the next one empty', async () => {
    renderTab([], { auth: authAs('member') })
    // Worded like its siblings: "No event types yet", "No relations yet".
    expect(await screen.findByText('No meta fields yet')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /New meta field/i }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'owner_team' } })
    fireEvent.keyDown(screen.getByRole('dialog', { name: 'New meta field' }), { key: 'Escape' })

    fireEvent.click(await screen.findByRole('button', { name: 'Keep editing' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(screen.getByLabelText('Name')).toHaveValue('owner_team')

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New meta field' })).not.toBeInTheDocument())

    // Discarded means discarded: the next New meta field starts empty.
    fireEvent.click(screen.getAllByRole('button', { name: /New meta field/i })[0]!)
    expect(screen.getByLabelText('Name')).toHaveValue('')
  })

  it('asks before Escape drops an edited meta field, and not when nothing changed', async () => {
    renderTab([FIELD], { auth: authAs('member') })

    fireEvent.click(await screen.findByRole('button', { name: 'Edit Jira link' }))
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Edit Jira link' }))
    fireEvent.change(screen.getByLabelText('Display name'), { target: { value: 'Ticket' } })
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' })
    expect(await screen.findByRole('alertdialog')).toHaveTextContent('Leave without saving?')
    expect(metaFieldsApi.update).not.toHaveBeenCalled()
  })
})
