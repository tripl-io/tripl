import type { ReactNode } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthContext } from '@/components/auth-context'
import { PersonaProject } from '@/test/PersonaProject'
import { personaAuth, type Persona } from '@/test/persona'
import type { DocBacklinksResponse } from '@/types/docs'
import { newNoteHref } from '@/lib/docLinks'
import { DocFieldNotes, DocNotesSection } from './DocNotesSection'

vi.mock('@/api/docs', () => ({
  docsApi: { backlinks: vi.fn() },
}))

import { docsApi } from '@/api/docs'

function response(items: DocBacklinksResponse['items']): DocBacklinksResponse {
  return { kind: 'event', name: 'checkout_started', qualifier: null, items }
}

function renderWith(ui: ReactNode, persona: Persona = 'member') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <AuthContext.Provider value={personaAuth(persona)}>
        <PersonaProject persona={persona}>
          <MemoryRouter>{ui}</MemoryRouter>
        </PersonaProject>
      </AuthContext.Provider>
    </QueryClientProvider>,
  )
}

describe('DocNotesSection (F22)', () => {
  beforeEach(() => {
    vi.mocked(docsApi.backlinks).mockReset()
  })

  it('serves the F24 kinds too: a metric card asks for metric backlinks', async () => {
    vi.mocked(docsApi.backlinks).mockResolvedValue({
      kind: 'metric',
      name: 'signup_rate',
      qualifier: null,
      items: [
        {
          scope: 'project',
          path: 'metrics/signup.md',
          title: 'Signup rate explained',
          description: '',
          audience: 'both',
          link_raw: '[[metric:signup_rate]]',
        },
      ],
    })
    renderWith(<DocNotesSection slug="demo" kind="metric" name="signup_rate" />)
    expect(await screen.findByText('Signup rate explained')).toBeInTheDocument()
    expect(screen.getByText('Docs that link to this metric')).toBeInTheDocument()
    expect(docsApi.backlinks).toHaveBeenCalledWith(
      'demo',
      { kind: 'metric', name: 'signup_rate', qualifier: null },
      expect.anything(),
    )
  })

  it('offers a variable note pre-filled with the variable link', async () => {
    vi.mocked(docsApi.backlinks).mockResolvedValue({ kind: 'variable', name: 'country', qualifier: null, items: [] })
    renderWith(<DocNotesSection slug="demo" kind="variable" name="country" />)
    expect(await screen.findByText(/No notes link to this variable yet/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'New note about this' }).getAttribute('href')).toContain(
      encodeURIComponent('[[variable:country]]'),
    )
  })

  it('lists the notes that link here, organization notes marked', async () => {
    vi.mocked(docsApi.backlinks).mockResolvedValue(
      response([
        {
          scope: 'project',
          path: 'recipes/checkout.md',
          title: 'Checkout queries',
          description: 'How to count a checkout',
          audience: 'agent',
          link_raw: '[[event:checkout_started]]',
        },
        {
          scope: 'organization',
          path: 'warehouse/gotchas.md',
          title: 'Warehouse gotchas',
          description: '',
          audience: 'both',
          link_raw: '[[event:checkout_started]]',
        },
      ]),
    )
    renderWith(<DocNotesSection slug="demo" kind="event" name="checkout_started" />)

    expect(await screen.findByRole('heading', { name: 'Notes' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Checkout queries' })).toHaveAttribute(
      'href',
      '/p/demo/docs/project/recipes/checkout.md',
    )
    expect(screen.getByRole('link', { name: 'Warehouse gotchas' })).toHaveAttribute(
      'href',
      '/p/demo/docs/organization/warehouse/gotchas.md',
    )
    expect(screen.getByText('Organization')).toBeInTheDocument()
    expect(screen.getByText('For agents')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /New note about this/ })).toHaveAttribute(
      'href',
      newNoteHref('demo', 'event', 'checkout_started'),
    )
    expect(docsApi.backlinks).toHaveBeenCalledWith(
      'demo',
      { kind: 'event', name: 'checkout_started', qualifier: null },
      expect.anything(),
    )
  })

  it('is hidden for a viewer when nothing links here', async () => {
    vi.mocked(docsApi.backlinks).mockResolvedValue(response([]))
    const { container } = renderWith(<DocNotesSection slug="demo" kind="event" name="checkout_started" />, 'viewer')
    await vi.waitFor(() => expect(docsApi.backlinks).toHaveBeenCalled())
    await Promise.resolve()
    expect(container).toBeEmptyDOMElement()
  })

  it('offers an editor the first note when nothing links here', async () => {
    vi.mocked(docsApi.backlinks).mockResolvedValue(response([]))
    renderWith(<DocNotesSection slug="demo" kind="event_type" name="checkout" />)
    const link = await screen.findByRole('link', { name: 'New note about this' })
    expect(link.getAttribute('href')).toBe(
      `/p/demo/docs?new=1&link=${encodeURIComponent('[[event-type:checkout]]')}`,
    )
  })

  it('pre-fills a qualified field link', () => {
    expect(newNoteHref('demo', 'field', 'amount', 'checkout')).toBe(
      `/p/demo/docs?new=1&link=${encodeURIComponent('[[field:checkout/amount]]')}`,
    )
  })
})

describe('DocFieldNotes (F22)', () => {
  beforeEach(() => {
    vi.mocked(docsApi.backlinks).mockReset()
  })

  it('asks nothing until opened, then shows only fields with notes', async () => {
    vi.mocked(docsApi.backlinks).mockImplementation(async (_slug, params) => ({
      kind: 'field',
      name: params.name,
      qualifier: params.qualifier ?? null,
      items:
        params.name === 'amount'
          ? [
              {
                scope: 'project',
                path: 'fields/amount.md',
                title: 'Amount is in cents',
                description: '',
                audience: 'both',
                link_raw: '[[field:checkout/amount]]',
              },
            ]
          : [],
    }))
    renderWith(<DocFieldNotes slug="demo" eventTypeName="checkout" fieldNames={['amount', 'currency']} />)
    expect(docsApi.backlinks).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Field notes' }))
    expect(await screen.findByRole('link', { name: 'Amount is in cents' })).toBeInTheDocument()
    expect(docsApi.backlinks).toHaveBeenCalledTimes(2)
    expect(docsApi.backlinks).toHaveBeenCalledWith(
      'demo',
      { kind: 'field', name: 'currency', qualifier: 'checkout' },
      expect.anything(),
    )
    expect(screen.queryByText('currency')).toBeNull()
  })
})
