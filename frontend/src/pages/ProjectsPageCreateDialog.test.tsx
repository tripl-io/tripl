import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { projectTemplatesApi } from '@/api/projectTemplates'
import { ecommerceTemplate } from '@/test/projectTemplates'
import { CreateProjectDialog } from './ProjectsPageCreateDialog'
import { TEMPLATE_HINT } from './projectTemplateCopy'

// The template list is its own request; mocked at the module so `fetch` below
// sees only the create (F21).
vi.mock('@/api/projectTemplates', () => ({
  projectTemplatesApi: { list: vi.fn() },
}))

// The shape the backend really returns, with the suggestion lists emptied:
// these tests are about creating, not the picker's disclosure.
const ECOMMERCE = ecommerceTemplate({ metric_suggestions: [], alert_suggestions: [] })

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{location.pathname}</output>
}

function renderDialog(onClose: () => void = () => {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/']}>
        <CreateProjectDialog onClose={onClose} existingSlugs={[]} />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function createdResponse(templateBranchId: string | null) {
  return new Response(
    JSON.stringify({
      id: 'p1',
      name: 'Shop Web',
      slug: 'shop-web',
      description: '',
      app_version_keep_releases: 5,
      created_at: '2026-09-27T00:00:00Z',
      updated_at: '2026-09-27T00:00:00Z',
      summary: {},
      template_branch_id: templateBranchId,
    }),
    { status: 201, headers: { 'Content-Type': 'application/json' } },
  )
}

function sentBody(fetchSpy: { mock: { calls: unknown[][] } }): Record<string, unknown> {
  const init = fetchSpy.mock.calls
    .map((call) => call[1] as RequestInit | undefined)
    .find((candidate) => candidate?.method === 'POST')
  expect(init).toBeDefined()
  return JSON.parse(String(init?.body)) as Record<string, unknown>
}

beforeEach(() => {
  vi.mocked(projectTemplatesApi.list).mockResolvedValue([ECOMMERCE])
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('CreateProjectDialog', () => {
  it('is titled like the button that opens it (DS-29)', () => {
    renderDialog()
    expect(screen.getByRole('dialog', { name: 'New project' })).toBeInTheDocument()
    expect(screen.getByLabelText(/Description/)).toHaveAccessibleName('Description (optional)')
  })

  it('marks a missing name inline, focuses it and sends nothing (AU-4)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    renderDialog()

    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    const name = screen.getByLabelText('Project name')
    expect(name).toHaveAttribute('aria-invalid', 'true')
    expect(name).toHaveAccessibleDescription('Give the project a name.')
    expect(name).not.toHaveAttribute('required')
    await waitFor(() => expect(name).toHaveFocus())
    expect(fetchSpy).not.toHaveBeenCalled()
  })

  it('shows the URL a name becomes and folds the field under "Customize URL" (SH-29)', () => {
    renderDialog()

    expect(screen.getByLabelText('Project name')).toHaveAttribute('placeholder', 'e.g. iOS app')
    expect(screen.queryByLabelText(/slug/i)).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Project name'), { target: { value: 'Shop Web' } })
    expect(screen.getByText('/p/shop-web')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Customize URL' }))
    expect(screen.getByLabelText('Project URL')).toHaveValue('shop-web')
  })

  it('opens the URL field and marks it when the server says the slug is taken (SH-29)', async () => {
    // existingSlugs cannot rule this out: the list hides seeding and failed
    // demos, whose slugs are still held.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Project with this slug already exists' }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    renderDialog()

    fireEvent.change(screen.getByLabelText('Project name'), { target: { value: 'Shop Web' } })
    expect(screen.queryByLabelText('Project URL')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    const slug = await screen.findByLabelText('Project URL')
    expect(slug).toHaveValue('shop-web')
    expect(slug).toHaveAttribute('aria-invalid', 'true')
    expect(slug).toHaveAccessibleDescription(
      'Another project already uses this URL. Choose a different one.',
    )
    await waitFor(() => expect(slug).toHaveFocus())
    expect(screen.queryByText('Could not create project')).not.toBeInTheDocument()

    // Editing the slug clears the server's verdict on the old one.
    fireEvent.change(slug, { target: { value: 'shop-web-2' } })
    expect(slug).not.toHaveAttribute('aria-invalid')
    expect(screen.queryByText(/already uses this URL/)).not.toBeInTheDocument()
    expect(screen.queryByText('Could not create project')).not.toBeInTheDocument()
  })

  it('creates a blank project with the same body as before and opens its overview', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(createdResponse(null))
    const onClose = vi.fn()
    renderDialog(onClose)

    await screen.findByRole('radio', { name: 'E-commerce' })
    expect(screen.getByRole('radio', { name: 'Blank project' })).toHaveAttribute(
      'aria-checked',
      'true',
    )
    expect(screen.queryByText(TEMPLATE_HINT)).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Project name'), { target: { value: 'Shop Web' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('/p/shop-web/overview'))
    expect(sentBody(fetchSpy)).toEqual({ name: 'Shop Web', slug: 'shop-web', description: '' })
    expect(onClose).toHaveBeenCalled()
  })

  it('sends the chosen template and opens its draft branch for review (F21)', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(createdResponse('b-42'))
    const onClose = vi.fn()
    renderDialog(onClose)

    fireEvent.change(screen.getByLabelText('Project name'), { target: { value: 'Shop Web' } })
    fireEvent.click(await screen.findByRole('radio', { name: 'E-commerce' }))

    // The helper text says where the plan lands, and the group announces it.
    expect(screen.getByText(TEMPLATE_HINT)).toBeInTheDocument()
    expect(screen.getByRole('radiogroup', { name: 'Start from' })).toHaveAccessibleDescription(
      TEMPLATE_HINT,
    )
    expect(screen.queryByRole('button', { name: 'Create' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Create from template' }))

    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('/p/shop-web/branches/b-42'),
    )
    expect(sentBody(fetchSpy)).toEqual({
      name: 'Shop Web',
      slug: 'shop-web',
      description: '',
      template_id: 'ecommerce',
    })
    expect(onClose).toHaveBeenCalled()
  })

  it('goes back to a blank create when the template is deselected', async () => {
    renderDialog()

    fireEvent.click(await screen.findByRole('radio', { name: 'E-commerce' }))
    expect(screen.getByRole('button', { name: 'Create from template' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('radio', { name: 'Blank project' }))
    expect(screen.getByRole('button', { name: 'Create' })).toBeInTheDocument()
    expect(screen.queryByText(TEMPLATE_HINT)).not.toBeInTheDocument()
  })
})
